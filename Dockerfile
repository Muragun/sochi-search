# Образ приложения: API и рабочие процессы.
#
# Elasticsearch намеренно остаётся снаружи. Он уже развёрнут, хранит
# данные в своём томе, и заворачивать его в тот же compose означало бы
# смешать перенос кода с переносом индекса — а это разные по риску
# операции.
#
# Python здесь новее, чем на рабочем сервере: 3.8 снят с поддержки в
# октябре 2024 года и обновлений безопасности больше не получает. Код
# совместим с 3.8, но в контейнере нет причин оставаться на нём.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Параллелизм задаётся числом контейнеров, а не потоками внутри
    # Tesseract: иначе процессы конкурируют за одни и те же ядра.
    OMP_THREAD_LIMIT=1 \
    SOCHI_SEARCH_DIR=/app

RUN apt-get update \
 && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        libgl1 \
        curl \
        util-linux \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --requirement requirements.txt

COPY app_v2 ./app_v2
COPY crawler_v2 ./crawler_v2
COPY ops ./ops
COPY elasticsearch ./elasticsearch
COPY ocr ./ocr
COPY tests ./tests
COPY docker/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

# Записываемые каталоги: состояние очереди, временные загрузки, кэш PDF.
RUN mkdir -p /app/data /app/run/downloads /app/run/pdf-cache \
             /app/run/admin-tasks /app/backups \
 && useradd --system --uid 10001 --home /app sochi \
 && chown -R sochi:sochi /app

USER sochi

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${APP_PORT:-8000}/health/live" || exit 1

ENTRYPOINT ["entrypoint"]
CMD ["api"]
