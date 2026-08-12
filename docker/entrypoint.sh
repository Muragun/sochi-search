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

wait_for_elasticsearch() {
    log "жду Elasticsearch: ${ES_URL:-http://elasticsearch:9200}"
    $PYTHON -B -m ops.wait_for_elasticsearch \
        --timeout "${ES_WAIT_TIMEOUT:-300}" \
        --interval 2 \
        --request-timeout 8
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
        log "запуск API на ${APP_HOST}:${APP_PORT}"
        exec $PYTHON -m uvicorn app_v2.main:app \
            --host "$APP_HOST" --port "$APP_PORT" \
            --proxy-headers --forwarded-allow-ips='*'
        ;;

    worker)
        wait_for_elasticsearch
        run_loop "${WORKER_INTERVAL:-120}" \
            $PYTHON -B -m crawler_v2.incremental_worker \
                --limit "${WORKER_LIMIT:-100}"
        ;;

    discovery)
        wait_for_elasticsearch
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
        run_loop "${PUBLICATION_DATE_INTERVAL:-300}" \
            $PYTHON -B -m crawler_v2.publication_date_worker \
                --limit "${PUBLICATION_DATE_LIMIT:-200}"
        ;;

    content-cleanup)
        wait_for_elasticsearch
        run_loop "${CONTENT_CLEANUP_INTERVAL:-600}" \
            $PYTHON -B -m crawler_v2.content_cleanup_worker \
                --limit "${CONTENT_CLEANUP_LIMIT:-200}"
        ;;

    backup)
        run_loop "${BACKUP_INTERVAL:-86400}" \
            $PYTHON -B -m ops.backup
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
