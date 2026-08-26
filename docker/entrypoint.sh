#!/usr/bin/env bash
#
# Точка входа образа. Заменяет systemd-юниты внутри контейнера.
#
# Каждая роль — отдельный контейнер. Повторяющиеся задачи выполняются
# простым циклом с паузой: это то же самое, что делали таймеры, только без
# systemd. Пауза отсчитывается после завершения запуска, а не по
# расписанию, поэтому долгий проход не накладывается на следующий.

set -euo pipefail

ROLE="${1:-api}"
shift || true

PYTHON=python
APP_PORT="${APP_PORT:-8000}"
APP_HOST="${APP_HOST:-0.0.0.0}"

log() {
    printf '%s [%s] %s\n' "$(date -Is)" "$ROLE" "$*"
}

# Очередь обхода живёт в SQLite, и её схему до сих пор создавали только
# разовые утилиты. На машине, развёрнутой с нуля, базы не было вовсе:
# обход падал с «Таблица crawl_urls не найдена». Вызов идемпотентный.
ensure_state_database() {
    $PYTHON -B -m ops.init_state || {
        log "не удалось подготовить базу очереди"
        return 1
    }
}


wait_for_elasticsearch() {
    log "жду Elasticsearch: ${ES_URL:-http://elasticsearch:9200}"
    $PYTHON -B -m ops.wait_for_elasticsearch \
        --timeout "${ES_WAIT_TIMEOUT:-300}" \
        --interval 2 \
        --request-timeout 8
}

# Резервное копирование за один проход: очередь и индекс.
#
# `ops.backup` забирает очередь SQLite и код — то, без чего непонятно,
# что и чем разбирали. `ops.snapshot` забирает индекс. Индекс считается
# производным, и формально это так, но собрать его заново — значит
# перераспознать корпус: недели работы обоих слотов OCR вместо часа
# восстановления из снимка.
#
# Задачи независимы, поэтому отказ одной не отменяет вторую: обе
# выполняются всегда, а роль сообщает о неуспехе после. Иначе временно
# недоступный кластер оставлял бы систему и без копии очереди.
run_backup_pass() {
    local queue_code=0
    local index_code=0

    $PYTHON -B -m ops.backup || queue_code=$?
    $PYTHON -B -m ops.snapshot || index_code=$?

    if [ "$queue_code" -ne 0 ]; then
        log "резервная копия очереди завершилась с кодом $queue_code"
    fi

    if [ "$index_code" -ne 0 ]; then
        log "снимок индекса завершился с кодом $index_code"
    fi

    [ "$queue_code" -eq 0 ] && [ "$index_code" -eq 0 ]
}

# Повторяет команду с паузой между запусками.
#
# Код 75 означает, что параллельный экземпляр уже держит блокировку, — это
# штатная ситуация, а не ошибка.
run_loop() {
    local interval="$1"
    shift

    while true; do
        set +e
        "$@"
        local code=$?
        set -e

        if [ "$code" -ne 0 ] && [ "$code" -ne 75 ]; then
            log "завершение с кодом $code, повтор через ${interval}s"
        fi

        sleep "$interval"
    done
}

case "$ROLE" in
    api)
        wait_for_elasticsearch
        ensure_state_database
        log "запуск API на ${APP_HOST}:${APP_PORT}"
        exec $PYTHON -m uvicorn app_v2.main:app \
            --host "$APP_HOST" --port "$APP_PORT" \
            --proxy-headers --forwarded-allow-ips='*'
        ;;

    worker)
        wait_for_elasticsearch
        ensure_state_database
        run_loop "${WORKER_INTERVAL:-120}" \
            $PYTHON -B -m crawler_v2.incremental_worker \
                --limit "${WORKER_LIMIT:-100}"
        ;;

    discovery)
        wait_for_elasticsearch
        ensure_state_database
        run_loop "${DISCOVERY_INTERVAL:-3600}" \
            $PYTHON -B -m crawler_v2.discovery_worker_v2 \
                --html-only \
                --html-max-pages "${DISCOVERY_MAX_PAGES:-1200}" \
                --html-max-depth "${DISCOVERY_MAX_DEPTH:-4}" \
                --html-max-pagination-page "${DISCOVERY_MAX_PAGINATION:-120}" \
                --max-new "${DISCOVERY_MAX_NEW:-10000}" \
                --report-samples 5 \
                --delay "${DISCOVERY_DELAY:-0.15}"
        ;;

    ocr)
        wait_for_elasticsearch
        ensure_state_database
        log "слот распознавания: ${OCR_SLOT:-1}"
        run_loop "${OCR_INTERVAL:-60}" \
            $PYTHON -B -m crawler_v2.incremental_worker \
                --pdf-ocr-backfill \
                --batch \
                --worker-slot "${OCR_SLOT:-1}" \
                --no-http-cache
        ;;

    publication-date)
        wait_for_elasticsearch
        ensure_state_database
        run_loop "${PUBLICATION_DATE_INTERVAL:-300}" \
            $PYTHON -B -m crawler_v2.publication_date_worker \
                --limit "${PUBLICATION_DATE_LIMIT:-200}"
        ;;


    backup)
        # Кластера эта роль намеренно не ждёт, в отличие от остальных.
        # Копия очереди от Elasticsearch не зависит вовсе, и лежачий
        # кластер не должен оставлять систему ещё и без неё. Снимок
        # разберётся сам: не достучится — сообщит и вернёт код.
        run_loop "${BACKUP_INTERVAL:-86400}" \
            run_backup_pass
        ;;

    tests)
        # Весь набор целиком. Тесты, которым нужны отсутствующие
        # библиотеки, объявляют пропуск сами, поэтому перечислять модули
        # руками не нужно — новый файл подхватится без правки роли.
        exec $PYTHON -m unittest discover -s tests -p "test_*.py"
        ;;

    shell)
        exec /bin/bash
        ;;

    *)
        # Любая другая роль трактуется как прямая команда: удобно для
        # разовых операций вроде переноса индекса.
        exec "$ROLE" "$@"
        ;;
esac
