# Переезд на другую машину

Проект разделён на три части, и переносить их нужно по-разному.

| Часть | Где живёт | Как переносится |
| --- | --- | --- |
| Код | Git | `git clone` |
| Настройки | `.env` | вручную, в репозиторий не попадает |
| Данные | SQLite и индекс Elasticsearch | резервная копия или повторный обход |

---

## 1. Репозиторий

В `.gitignore` уже внесены `data/`, `run/`, `backups/`, `.env` и
виртуальное окружение. В репозиторий попадает только код, юниты, образ и
документация.

```bash
cd /opt/sochi-search
git init
git add -A
git commit -m "Sochi Search 2.5.0"
git remote add origin <адрес закрытого репозитория>
git push -u origin main
```

Перед первым `git add` стоит убедиться, что секреты не попадают в индекс:

```bash
git status --porcelain | grep -E "\.env|\.sqlite3|backups/" || echo "секретов нет"
```

## 2. Контейнеры

Elasticsearch намеренно оставлен снаружи: он уже развёрнут, хранит данные
в своём томе, и заворачивать его в тот же compose означало бы смешать
перенос кода с переносом индекса — операции разного риска.

```bash
docker compose -f docker-compose.yml -f docker-compose.app.yml build
docker compose -f docker-compose.yml -f docker-compose.app.yml up -d
docker compose -f docker-compose.yml -f docker-compose.app.yml ps
```

Роли контейнеров повторяют прежние systemd-юниты:

| Контейнер | Что делает | Интервал |
| --- | --- | --- |
| `api` | поиск и служебный раздел | постоянно |
| `worker` | разбор очереди | `WORKER_INTERVAL`, по умолчанию 120 с |
| `discovery` | обход сайта | `DISCOVERY_INTERVAL`, 3600 с |
| `ocr-1`, `ocr-2` | распознавание PDF | `OCR_INTERVAL`, 60 с |
| `publication-date` | даты публикации | 300 с |
| `content-cleanup` | очистка текстов | 600 с |
| `backup` | резервные копии | 86400 с |

Пауза отсчитывается **после** завершения запуска, а не по расписанию,
поэтому долгий проход не накладывается на следующий.

Проверить, что образ собрался и тесты проходят внутри него:

```bash
docker compose -f docker-compose.app.yml run --rm api tests
```

## 3. Данные

### Очередь обхода

```bash
# на старой машине
systemctl start sochi-search-backup.service
ls -lt /opt/sochi-search/backups/operational/ | head -3

# перенести архив, затем на новой
tar -xzf sochi-search-backup-*.tar.gz
docker cp crawl_state.sqlite3 sochi-search-api:/app/data/
```

### Индекс Elasticsearch

Два пути на выбор.

**Снимок.** Быстро, но требует настроенного репозитория снимков на обеих
машинах.

```bash
curl -XPUT "http://127.0.0.1:9200/_snapshot/backup" -H 'Content-Type: application/json' \
  -d '{"type":"fs","settings":{"location":"/usr/share/elasticsearch/backup"}}'
curl -XPUT "http://127.0.0.1:9200/_snapshot/backup/move-2026?wait_for_completion=true"
```

**Повторное построение.** Медленнее, но не требует ничего: очередь в
SQLite содержит все адреса и хеши, воркеры соберут индекс заново. При
корпусе около 80 тысяч адресов это порядка суток.

## 4. Настройки

`.env` в репозиторий не попадает и переносится вручную. Обязательные
значения на новой машине:

```
ES_URL=http://elasticsearch:9200
ES_INDEX=sochi_search

ADMIN_USERNAME=admin
ADMIN_PASSWORD_SHA256=<хеш>
```

Хеш пароля считается так:

```bash
python3 -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
```

Без заданного пароля служебный раздел отвечает ошибкой, а не пускает
всех: молчаливый пропуск опаснее неработающей страницы.

## 5. Что проверить после переезда

```bash
curl -s http://127.0.0.1:18084/health/ready
curl -su admin http://127.0.0.1:18084/admin/api/state | head -40
docker compose -f docker-compose.app.yml logs --tail 40 worker
```

И глазами: открыть `/ui`, задать три-четыре реальных запроса, открыть
`/admin` и убедиться, что очередь двигается.

---

## Чего в репозитории пока нет

Базовые юниты `sochi-search-worker.service`,
`sochi-search-discovery.service`, `sochi-search-publication-date.service`
и `sochi-search-content-cleanup.service` существуют только на рабочем
сервере: в пакете лежат лишь их drop-in-файлы. Для контейнерного переезда
они не нужны — роли задаёт `entrypoint.sh`. Но если переносить проект без
контейнеров, эти четыре файла нужно скопировать с сервера и добавить в
репозиторий:

```bash
cp /etc/systemd/system/sochi-search-{worker,discovery,publication-date,content-cleanup}.service \
   /opt/sochi-search/systemd/
```
