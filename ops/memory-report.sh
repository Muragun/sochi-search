#!/usr/bin/env bash
#
# Что занимает память на сервере и что из этого лишнее.
#
# Только чтение: ничего не запускает, не останавливает и не меняет.
# Запускать от обычного пользователя; отдельные разделы попросят sudo и
# без него просто пропустятся.
#
#   bash ops/memory-report.sh
#   bash ops/memory-report.sh > /tmp/memory.txt   # чтобы прислать вывод
#
set -uo pipefail

# printf выравнивает по байтам, а кириллица занимает по два байта на
# букву — без UTF-8 локали колонки разъезжаются. C.UTF-8 есть почти
# везде; если нет, выравнивание просто будет неровным.
for candidate in $(locale -a 2>/dev/null | grep -iE '^(c|en_US)\.utf-?8$'); do
    export LC_ALL="$candidate"
    break
done

say() { printf '\n=== %s ===\n' "$1"; }
mb()  { awk '{printf "%.0f", $1/1024}'; }

# Дополняет строку пробелами до нужной ширины в буквах, а не в байтах.
pad() {
    local text="$1" width="$2" length

    length=${#text}

    if [ "$length" -ge "$width" ]; then
        printf '%s' "$text"
        return
    fi

    printf '%s%*s' "$text" "$((width - length))" ""
}

PROJECT="${SOCHI_SEARCH_DIR:-/opt/sochi-search}"


say "1. Память машины"

free -h
echo
printf 'Занято под кэш файлов (это не потеря — ядро отдаст под запрос):\n'
awk '/^Cached:/ {printf "  %.1f ГБ\n", $2/1048576}' /proc/meminfo
printf 'swap использовано:\n'
awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2}
     END {printf "  %.1f ГБ из %.1f ГБ\n", (t-f)/1048576, t/1048576}' /proc/meminfo


say "2. Двадцать самых прожорливых процессов"

# RSS завышает общую картину: разделяемые страницы считаются каждому
# процессу. Для суммирования ниже используется PSS.
printf '%-8s %s %9s %6s %9s  %s\n' \
    "PID" "$(pad "ПОЛЬЗ." 10)" "ПАМЯТЬ" "CPU%" "ВРЕМЯ" "КОМАНДА"

ps -eo pid,user,rss,pcpu,etimes,args --sort=-rss --no-headers \
    | head -20 \
    | while read -r pid user rss cpu secs command; do
        [ "${rss:-0}" -lt 2048 ] && continue

        if [ "${#command}" -gt 74 ]; then
            command="${command:0:71}..."
        fi

        printf '%-8s %-10s %6s МБ %6s %8s с  %s\n' \
            "$pid" "$user" "$((rss / 1024))" "$cpu" "$secs" "$command"
    done


say "3. Сколько занимает каждая часть системы"

if [ -r /proc/self/smaps_rollup ]; then
    printf '%s %10s %10s  %s\n' "$(pad "ЧАСТЬ" 24)" "PSS" "RSS" "ПРОЦЕССОВ"

    total_pss=0

    for group in \
        "Elasticsearch|java.*elasticsearch" \
        "Поиск (API)|uvicorn|app_v2.main" \
        "Разбор очереди|incremental_worker" \
        "Распознавание|tesseract|pdf_extraction_worker" \
        "Обход сайта|discovery_worker" \
        "Даты публикации|publication_date_worker" \
        "Docker|dockerd|containerd" \
        "MariaDB|mariadbd|mysqld" \
        "PHP-FPM|php-fpm" \
        "Node|node " \
        "Nginx или Apache|nginx|httpd|apache2"
    do
        label="${group%%|*}"
        pattern="${group#*|}"

        # pgrep -f принимает расширенное регулярное выражение, поэтому
        # вертикальная черта здесь и есть «или». Экранировать её нельзя.
        pids=$(pgrep -f "$pattern" 2>/dev/null | grep -v "^$$\$" | tr '\n' ' ')
        [ -z "$pids" ] && continue

        pss=0; rss=0; count=0

        for pid in $pids; do
            [ -r "/proc/$pid/smaps_rollup" ] || continue
            p=$(awk '/^Pss:/ {s+=$2} END {print s+0}' "/proc/$pid/smaps_rollup" 2>/dev/null)
            r=$(awk '/^Rss:/ {s+=$2} END {print s+0}' "/proc/$pid/smaps_rollup" 2>/dev/null)
            pss=$((pss + ${p:-0}))
            rss=$((rss + ${r:-0}))
            count=$((count + 1))
        done

        [ "$count" -eq 0 ] && continue
        total_pss=$((total_pss + pss))

        printf '%s %7s МБ %7s МБ  %s\n' \
            "$(pad "$label" 24)" \
            "$(echo $pss | mb)" "$(echo $rss | mb)" "$count"
    done

    printf '\n%s %7s МБ\n' "$(pad "ИТОГО (PSS)" 24)" "$(echo $total_pss | mb)"
    printf '\nPSS честнее RSS: разделяемая память делится между процессами,\n'
    printf 'поэтому сумму PSS можно складывать, а сумму RSS — нет.\n'
else
    echo "  /proc/*/smaps_rollup недоступен, раздел пропущен"
fi


say "4. Контейнеры Docker"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker stats --no-stream \
        --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' \
        2>/dev/null || echo "  не удалось получить статистику"

    echo
    echo "Пределы памяти из настроек:"
    docker inspect --format \
        '  {{.Name}}: предел {{if eq .HostConfig.Memory 0}}не задан{{else}}{{div .HostConfig.Memory 1048576}} МБ{{end}}' \
        $(docker ps -q) 2>/dev/null | sed 's|/||'
else
    echo "  Docker недоступен или не запущен"
fi


say "5. Службы поиска и их состояние"

if command -v systemctl >/dev/null 2>&1 && systemctl --version >/dev/null 2>&1; then
    units=$(systemctl list-units 'sochi-search-*' --all --no-pager --no-legend 2>/dev/null)

    if [ -z "$units" ]; then
        echo "  служб sochi-search не найдено (система без systemd?)"
    else
        echo "$units" | awk '{printf "  %-44s %-10s %s\n", $1, $3, $4}'
    fi

    echo
    echo "Потребление памяти по службам:"
    for unit in $(systemctl list-units 'sochi-search-*.service' \
                  --state=active --no-pager --no-legend 2>/dev/null \
                  | awk '{print $1}'); do
        value=$(systemctl show "$unit" -p MemoryCurrent --value 2>/dev/null)

        if [ -n "$value" ] && [ "$value" != "[not set]" ] && [ "$value" -gt 0 ] 2>/dev/null; then
            printf '  %-44s %6s МБ\n' "$unit" "$((value / 1048576))"
        fi
    done
else
    echo "  systemctl недоступен"
fi


say "6. Что можно убрать"

echo "Проверяем то, что в 2.5.0 стало ненужным."
echo

# Очистка содержимого: одноразовая очередь, отработала
if command -v systemctl >/dev/null 2>&1 && systemctl --version >/dev/null 2>&1; then
    state=$(systemctl is-enabled sochi-search-content-cleanup.timer 2>/dev/null || echo "нет")

    if [ "$state" = "enabled" ]; then
        echo "  [убрать] sochi-search-content-cleanup.timer включён."
        echo "           Очередь одноразовая и отработала полностью."
        echo "           sudo systemctl disable --now sochi-search-content-cleanup.timer"
    else
        echo "  [ок] content-cleanup: $state"
    fi
fi

# Старый API v1 на отдельном порту
if command -v ss >/dev/null 2>&1; then
    legacy=$(ss -tlnp 2>/dev/null | grep -c ':18083' || true)

    if [ "${legacy:-0}" -gt 0 ]; then
        echo "  [убрать] на порту 18083 слушает прежний API (версия 1)."
        echo "           Он не используется поиском и держит память."
        ss -tlnp 2>/dev/null | grep ':18083' | sed 's/^/           /'
    else
        echo "  [ок] прежний API на 18083 не запущен"
    fi
fi

# Зависшие процессы разбора
stale=$(ps -eo pid,etimes,args 2>/dev/null \
        | awk '$2 > 7200 && /incremental_worker|pdf_extraction_worker|tesseract/ {print}')

if [ -n "$stale" ]; then
    echo "  [проверить] процессы работают дольше двух часов:"
    echo "$stale" | awk '{printf "           pid %s, %.1f ч\n", $1, $2/3600}'
    echo "           Обычный проход укладывается в минуты."
else
    echo "  [ок] зависших процессов разбора нет"
fi

# Файлы блокировок без процессов
if [ -d "$PROJECT/run" ]; then
    orphans=0

    for lock in "$PROJECT"/run/*.lock; do
        [ -e "$lock" ] || continue
        fuser "$lock" >/dev/null 2>&1 || orphans=$((orphans + 1))
    done

    if [ "$orphans" -gt 0 ]; then
        echo "  [норма] файлов блокировки без процесса: $orphans"
        echo "           Это не утечка: flock освобождает их сам."
    fi
fi


say "7. Место на диске"

df -h "$PROJECT" 2>/dev/null | tail -1 | awk '{printf "  раздел проекта: %s занято из %s (%s)\n", $3, $2, $5}'

for path in "$PROJECT/data" "$PROJECT/run" "$PROJECT/backups"; do
    [ -d "$path" ] && du -sh "$path" 2>/dev/null | awk '{printf "  %-40s %s\n", $2, $1}'
done

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo
    echo "Тома Docker:"
    docker system df -v 2>/dev/null | awk '/VOLUME NAME/,/^$/' | head -12 | sed 's/^/  /'
fi


say "Готово"

cat <<'INFO'

Как читать.

  Elasticsearch — самый крупный потребитель, и это нормально: он держит
  heap (ES_HEAP, по умолчанию 2 ГБ) плюс примерно столько же вне heap.
  Уменьшать его стоит, только если соседям не хватает.

  Кэш файлов в разделе 1 — не потеря памяти. Ядро отдаст его под запрос
  за миллисекунды; для Lucene этот кэш и есть скорость поиска.

  Разбор очереди и распознавание живут короткими всплесками. Если такой
  процесс висит часами — смотрите раздел 6.

  Если swap занят больше чем на пару сотен мегабайт при живом
  Elasticsearch, это плохо: он попадает в swap и поиск замедляется
  на порядок. Тогда уменьшайте ES_HEAP или число слотов распознавания.

INFO
