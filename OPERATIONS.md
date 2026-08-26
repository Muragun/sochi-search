# Sochi Search 2.4.5: эксплуатация crawler и PDF OCR

## Веб-панель и API прогресса

- панель: `/ui`;
- машинный статус: `/pipeline/status`;
- готовность API: `/health/ready`.

Панель обновляется каждые 30 секунд и только читает SQLite. Основные поля:

- `crawler.loaded_urls` — URL, уже доступные в поиске;
- `crawler.remaining_urls` — активные URL, ожидающие основной обработки;
- `crawler.processing_urls` — URL, захваченные прямо сейчас;
- `elasticsearch.documents` — поисковые фрагменты, а не число URL;
- `pdf_ocr.completed` — PDF, завершившие текущий `PDF_EXTRACTION_VERSION`
  (сейчас `pdf-ocr-v7`; строка поднимается при каждом изменении,
  влияющем на распознавание, и тогда корпус перечитывается заново);
- `pdf_ocr.fast_indexed` — PDF уже в поиске через `pdf-native-v1`, но ждут OCR;
- `pdf_ocr.remaining` — оставшаяся фоновая очередь;
- `pdf_ocr.processing` — текущий PDF OCR.

```bash
curl -fsS http://127.0.0.1:18084/health/ready
curl -fsS http://127.0.0.1:18084/pipeline/status
```

## Как теперь обрабатывается PDF

### Стадия 1: быстрый основной worker

`sochi-search-worker` не запускает Tesseract. Он извлекает нативный текст,
реквизиты HTML-карточки и сразу записывает `pdf-native-v1`. Если PyMuPDF не
завершился за 120 секунд, создаётся безопасный фрагмент по имени/метаданным.
Документ остаётся доступен поиску и становится кандидатом фонового OCR.

Характерные строки:

```text
PDF_PROCESS mode=fast timeout_seconds=120 ...
PDF extraction version: pdf-native-v1
PDF OCR отложено в фон: <число>
```

### Стадия 2: отдельный OCR worker

`sochi-search-pdf-ocr@1.timer` запускает один PDF за раз с низким CPU/IO
приоритетом. Основной crawler при этом продолжает HTML, DOCX и XLSX.

Пределы по умолчанию:

- `CRAWL_PDF_OCR_DPI=180`;
- `CRAWL_PDF_OCR_ROTATIONS=0`;
- `CRAWL_PDF_OCR_PAGE_TIMEOUT_SECONDS=45`;
- `CRAWL_PDF_OCR_DOCUMENT_BUDGET_SECONDS=360`;
- `CRAWL_PDF_PROCESS_TIMEOUT_SECONDS=600`;
- `CRAWL_PDF_OCR_MAX_PAGES=200`.

На первой странице дополнительно проверяются номер, дата и тип правового акта.
Метаданные связанной официальной HTML-карточки имеют приоритет над эвристикой
PDF.

Нормальный журнал ограниченного OCR:

```text
PDF_PROCESS mode=bounded timeout_seconds=600 page_timeout_seconds=45 document_budget_seconds=360
PDF_PROGRESS page=0/24 stage=opened elapsed_seconds=0
PDF_PROGRESS page=1/24 readable=1 ocr_attempted=1 ocr_used=1 ...
PDF_PAGE_TIMEOUT page=5 timeout_seconds=45
PDF_BUDGET_EXHAUSTED page=10/24 attempted=9 budget_seconds=360
PDF extraction version: pdf-ocr-v7
```

`PDF_PAGE_TIMEOUT` и `PDF_BUDGET_EXHAUSTED` не являются аварией worker. Уже
полученные страницы индексируются, медленные фиксируются как нечитаемые, а
процесс завершается с `failed: 0`.

## Ручная проверка конкретного PDF

Быстрая стадия:

```bash
cd /opt/sochi-search || exit 1

PDF_URL="http://sochi.ru/upload/iblock/e9b/e9b6cb5a2a704f1c00ee28226036d64d.pdf"

/usr/bin/flock \
  --wait 60 \
  /opt/sochi-search/run/incremental-worker.lock \
  /opt/sochi-search/.venv/bin/python -B \
  -m crawler_v2.incremental_worker \
  --url "$PDF_URL" \
  --force \
  --no-http-cache
```

Ограниченный OCR того же URL:

```bash
/usr/bin/flock \
  --wait 60 \
  /opt/sochi-search/run/pdf-ocr-worker.lock \
  /opt/sochi-search/.venv/bin/python -B \
  -m crawler_v2.incremental_worker \
  --url "$PDF_URL" \
  --pdf-ocr-backfill \
  --limit 1
```

Проверка временных файлов после завершения:

```bash
find /opt/sochi-search/run/downloads \
  -maxdepth 1 \
  -type f \
  -name 'sochi-search-pdf-*.tmp' \
  -print
```

Вывод должен быть пустым. Не удаляйте `.tmp`, пока связанный процесс активен.

## Службы и таймеры

```bash
systemctl is-active sochi-search-api-v2.service
systemctl is-active sochi-search-worker.timer
systemctl is-active sochi-search-discovery.timer
systemctl is-active sochi-search-publication-date.timer
systemctl is-active sochi-search-pdf-ocr@1.timer
systemctl is-active sochi-search-backup.timer
systemctl is-active sochi-search-snapshot.timer
```

В штатном режиме все значения — `active`.

Служба `content-cleanup` удалена в 2.5.0: её очередь была одноразовой и
отработала полностью (26577 из 26577). Если на машине остался её таймер,
он больше не нужен:

    sudo systemctl disable --now sochi-search-content-cleanup.timer

Запуск всех рабочих таймеров:

```bash
sudo systemctl enable --now \
  sochi-search-worker.timer \
  sochi-search-discovery.timer \
  sochi-search-publication-date.timer \
  sochi-search-pdf-ocr@1.timer
```

Остановка перед обновлением:

```bash
sudo systemctl stop \
  sochi-search-worker.timer \
  sochi-search-discovery.timer \
  sochi-search-publication-date.timer \
  sochi-search-pdf-ocr@1.timer

sudo systemctl stop \
  sochi-search-worker.service \
  sochi-search-discovery.service \
  sochi-search-publication-date.service \
  sochi-search-pdf-ocr@1.service
```

## Диагностика процессов PDF

```bash
pgrep -af 'crawler_v2.pdf_(extraction|page_ocr)_worker' \
  || echo "PDF_CHILD_PROCESSES=0"
```

После `Ctrl+C` новых версий вывод должен стать пустым. Systemd использует
`KillMode=control-group`, поэтому остановка service завершает активную страницу
и контейнерный PDF-процесс вместе.

Журнал:

```bash
sudo journalctl \
  -u sochi-search-worker.service \
  -u sochi-search-pdf-ocr@1.service \
  -n 200 \
  --no-pager \
  -o short-iso
```

## Очередь и блокировки

Основной worker планирует партию, но переводит в `processing` только текущий
URL. OCR service использует отдельный flock, а атомарный claim SQLite не даёт
двум worker взять одну строку.

Нормально видеть до двух PDF/URL в `processing`: один у основной очереди и
один у фонового OCR. При старте завершённые локальные PID освобождаются сразу;
возрастной `CRAWL_LOCK_TIMEOUT_MINUTES` остаётся резервом.

Установщик под обоими lock печатает:

```text
CRAWLER_PROCESSING_RECOVERED=<число>
PDF_TEMP_FILES_RECOVERED=<число>
DEPLOYED_QUEUE_RECOVERY=OK
DEPLOYED_PDF_TEMP_RECOVERY=OK
```

## Состояния crawler

- `discovered` — новый URL;
- `processing` — текущая работа;
- `indexed`, `indexed_existing`, `unchanged` — доступно в поиске;
- `retry`, `missing` — временная проблема;
- `gone`, `quarantined`, `skipped_corrupt`, `skipped_unsupported` —
  терминальная классификация.

`content-cleanup` удалена в 2.5.0: очередь отработала полностью.
Publication-date продолжает работать отдельным таймером.

## Резервные копии

Проверка последнего operational backup:

```bash
ARCHIVE="$(
  sudo find /opt/sochi-search/backups/operational \
    -maxdepth 1 \
    -type f \
    -name 'sochi-search-backup-*.tar.gz' \
    -printf '%T@ %p\n' \
  | sort -nr \
  | head -n 1 \
  | cut -d' ' -f2-
)"

sudo /opt/sochi-search/.venv/bin/python -B \
  -m ops.verify_backup "$ARCHIVE"
```

Ожидается:

```text
BACKUP_VERIFY=OK
BACKUP_RELEASE_VERSION=2.4.5
BACKUP_SQLITE_QUICK_CHECK=ok
```

Ручной backup:

```bash
sudo systemctl start sochi-search-backup.service
sudo journalctl -u sochi-search-backup.service -n 80 --no-pager
```

### Снимки индекса

`operational backup` забирает очередь SQLite и код. Индекса в нём нет, и
это осознанно: индекс производный. Но «производный» здесь не значит
«дешёвый». Собрать его заново — это перераспознать корпус, недели работы
обоих слотов OCR против часа восстановления из снимка.

Поэтому индекс снимается отдельной службой:

```bash
systemctl is-active sochi-search-snapshot.timer
```

**Сначала — разрешить кластеру писать снимки.** Elasticsearch кладёт их
сам и только внутрь каталогов из `path.repo`. Под Docker настройка
приходит из compose и уже стоит. При установке без Docker её нет, и
первый же запуск отвечает `doesn't match any of the locations specified
by path.repo`.

Проверить, настроено ли:

```bash
curl -s "http://127.0.0.1:9200/_nodes/settings?filter_path=**.path" | grep -o 'repo[^}]*'
```

Если пусто — завести каталог и дописать настройку:

```bash
sudo mkdir -p /var/lib/elasticsearch/snapshots
sudo chown elasticsearch:elasticsearch /var/lib/elasticsearch/snapshots

# в /etc/elasticsearch/elasticsearch.yml
path.repo: /var/lib/elasticsearch/snapshots

sudo systemctl restart elasticsearch
```

Настройка не динамическая: без перезапуска узла она не подхватится.
Путь после этого прописать в `.env`:

```bash
ES_SNAPSHOT_LOCATION=/var/lib/elasticsearch/snapshots
```

Значение по умолчанию (`/snapshots`) — это путь внутри контейнера, и на
машине без Docker оно не подойдёт.

Включить:

```bash
sudo systemctl enable --now sochi-search-snapshot.timer
```

Снимок вручную и что должно получиться:

```bash
sudo systemctl start sochi-search-snapshot.service
sudo journalctl -u sochi-search-snapshot.service -n 40 --no-pager
```

```text
SNAPSHOT_OK=1
SNAPSHOT_NAME=auto-20260826t040000z
SNAPSHOT_STATE=SUCCESS
SNAPSHOT_REMOVED_EXPIRED=0
```

Что лежит в хранилище:

```bash
sudo /opt/sochi-search/.venv/bin/python -B -m ops.snapshot --list
```

Три свойства, которые стоит знать до того, как понадобится.

**Снимки инкрементальные.** В хранилище дописываются только сегменты,
которых там ещё нет. Первый снимок стоит как весь индекс, каждый
следующий — как изменения за сутки.

**Уборка трогает только свои.** Удаляются снимки с приставкой `auto-`,
и только сверх `ES_SNAPSHOT_RETENTION` штук. Снимок, снятый руками перед
переездом (`docs/DEPLOY.md` называет его `full`), переживёт любое число
ночных запусков.

**Неудачный снимок ничего не удаляет.** Если хотя бы один шард не
попал в снимок, задача обрывается до уборки: иначе годных копий стало бы
меньше, а негодная добавилась.

**Место на диске — до первого запуска, а не после.** Хранилище лежит в
томе `essnapshots`, на том же диске, что и индекс. Первый снимок стоит
как весь индекс, около восьми гигабайт. Проверьте, что оно есть:

```bash
df -h /var/lib/docker
curl -s "http://127.0.0.1:9200/_cat/indices?h=index,store.size&v"
```

Свободного должно остаться с запасом на число снимков, которое вы
храните (`ES_SNAPSHOT_RETENTION`, по умолчанию 3). Дальше смотрите, во
что обходится хранилище на самом деле:

```bash
docker run --rm -v sochi-search_essnapshots:/s alpine du -sh /s
```

Пока идёт фоновое распознавание, документы переписываются и сегменты
сливаются, поэтому прирост за сутки заметно больше, чем на неподвижном
индексе. Первую неделю на эту цифру стоит смотреть.

При отказе диска целиком снимки не спасут — том на той же машине.
Копия, которая переживёт машину, — это `docs/DEPLOY.md`, раздел
«Перенос данных»: том выгружается в файл и уезжает с машины.

Восстановление описано там же.

## Правила изменения OCR-настроек

Меняйте один параметр за раз и сначала проверяйте один известный PDF. Увеличение
DPI, числа поворотов или page timeout повышает стоимость почти линейно либо
квадратично по числу пикселей. Возвращать `0,180,90,270` массово не следует:
именно четыре последовательных поворота вызвали многоминутные страницы в
контрольном PDF 56 109 652 байта.
