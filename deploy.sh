#!/usr/bin/env bash
#
# Развёртывание на новой машине.
#
# Скрипт проверяет условия, поднимает контейнеры, создаёт пустой индекс и
# печатает, что делать дальше. Запускать можно сколько угодно раз: то,
# что уже сделано, он не трогает.
#
#   bash deploy.sh
#
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.app.yml"

say()  { printf '\n=== %s ===\n' "$1"; }
warn() { printf '  ВНИМАНИЕ: %s\n' "$1"; }
fail() { printf '\nОШИБКА: %s\n' "$1" >&2; exit 1; }


say "1. Проверка условий"

command -v docker >/dev/null 2>&1 \
    || fail "Docker не установлен.
  Ubuntu/Debian: apt-get install -y docker.io docker-compose-plugin
  RHEL/CentOS:   dnf install -y docker docker-compose-plugin"

command -v curl >/dev/null 2>&1 \
    || fail "Нет curl. Ubuntu/Debian: apt-get install -y curl"

docker compose version >/dev/null 2>&1 \
    || fail "Нет docker compose v2 (пакет docker-compose-plugin).
  Старый docker-compose через дефис не подойдёт."

docker info >/dev/null 2>&1 \
    || fail "Демон Docker не отвечает.
  Проверьте:  systemctl status docker
  Если команда требует sudo — добавьте себя в группу:
    usermod -aG docker \$USER   (после этого перезайти в систему)"

free_gb=$(df --output=avail -BG . | tail -1 | tr -dc '0-9')
[ "${free_gb:-0}" -ge 20 ] \
    || fail "На диске свободно ${free_gb} ГБ, нужно хотя бы 20.
  Индекс на 80 тысяч документов занимает около 8 ГБ, плюс образы Docker."

total_ram=$(free -g | awk 'NR==2{print $2}')
cores=$(nproc)

echo "  docker:   $(docker --version | cut -d, -f1)"
echo "  диск:     ${free_gb} ГБ свободно"
echo "  машина:   ${total_ram} ГБ памяти, ${cores} ядер"

[ "${total_ram:-0}" -ge 6 ] || warn \
"меньше 8 ГБ памяти — уменьшите ES_HEAP в .env и оставьте один
           контейнер распознавания вместо двух"


say "2. Настройки"

if [ ! -f .env ]; then
    [ -f .env.production.example ] \
        || fail "Нет ни .env, ни .env.production.example. Репозиторий скопирован не целиком?"

    cp .env.production.example .env
    chmod 600 .env

    cat <<'HINT'

Создан файл .env — заполните в нём три обязательных значения:

  ADMIN_USERNAME         логин для входа в раздел управления
  ADMIN_PASSWORD_SHA256  хеш пароля
  API_BIND               адрес во внутренней сети, на котором слушать

Хеш пароля считается так (при вводе пароль не отображается):

  python3 -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"

Адрес машины во внутренней сети:

  hostname -I

Впишите значения в .env и запустите deploy.sh ещё раз.
HINT
    exit 1
fi

# shellcheck disable=SC1091
set -a; . ./.env; set +a

[ -n "${ADMIN_PASSWORD_SHA256:-}${ADMIN_PASSWORD:-}" ] \
    || fail "В .env не задан пароль управления.
  Заполните ADMIN_PASSWORD_SHA256 — иначе раздел /admin не откроется."

bind="${API_BIND:-127.0.0.1}"
port="${API_PORT:-18084}"

if [ "$bind" = "0.0.0.0" ]; then
    warn "API_BIND=0.0.0.0 — поиск будет доступен на всех адресах машины,
           включая внешний, если он есть. Для внутренней сети укажите
           конкретный адрес из вывода hostname -I"
elif [ "$bind" != "127.0.0.1" ]; then
    ip -4 -o addr show 2>/dev/null | grep -qw "$bind" || warn \
"адреса $bind нет среди адресов машины — Docker откажется открывать
           порт. Проверьте: hostname -I"
fi

echo "  логин управления: ${ADMIN_USERNAME:-admin}"
echo "  поиск слушает:    ${bind}:${port}"
echo "  память ES:        ${ES_HEAP:-2g} heap, предел ${ES_MEM_LIMIT:-3g}"


say "3. Сборка образа"
$COMPOSE build


say "4. Запуск Elasticsearch"
$COMPOSE up -d elasticsearch

printf '  жду готовности'
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:9200/_cluster/health" >/dev/null 2>&1; then
        printf ' готов\n'
        break
    fi
    printf '.'
    sleep 5
done

curl -fsS "http://127.0.0.1:9200/_cluster/health" >/dev/null 2>&1 || fail \
"Elasticsearch не поднялся за пять минут.
  Смотрите причину:  $COMPOSE logs elasticsearch
  Частая причина — vm.max_map_count. Лечится так:
    sysctl -w vm.max_map_count=262144
    echo 'vm.max_map_count=262144' >> /etc/sysctl.conf"


say "5. Создание индекса"

if curl -fsS "http://127.0.0.1:9200/${ES_INDEX:-sochi_search}/_count" >/dev/null 2>&1; then
    echo "  индекс ${ES_INDEX:-sochi_search} уже существует, пропускаю"
else
    $COMPOSE run --rm --no-deps api \
        python -B -m ops.reindex_v3 --bootstrap \
            --mapping elasticsearch/sochi_docs_v3.json
fi


say "6. Запуск приложения"
$COMPOSE up -d

printf '  жду ответа поиска'
for _ in $(seq 1 30); do
    if curl -fsS "http://${bind}:${port}/health/ready" >/dev/null 2>&1; then
        printf ' отвечает\n'
        break
    fi
    printf '.'
    sleep 2
done

echo
$COMPOSE ps


say "Готово"

documents=$(curl -fsS "http://127.0.0.1:9200/${ES_INDEX:-sochi_search}/_count" 2>/dev/null \
            | tr -d ' "' | sed -n 's/.*count:\([0-9]*\).*/\1/p')

cat <<INFO

Поиск:      http://${bind}:${port}/ui
Управление: http://${bind}:${port}/admin       (логин ${ADMIN_USERNAME:-admin})

Документов в индексе: ${documents:-0}
INFO

if [ "${documents:-0}" = "0" ]; then
cat <<'INFO'

Индекс пустой. Наполнить его можно двумя способами.

  Перенести данные со старой машины — примерно час:
     docs/DEPLOY.md, раздел «Перенос данных».

  Собрать заново — ничего не требует, но занимает около суток:
     откройте раздел управления, нажмите «Обход сайта»,
     затем «Разобрать очередь». Дальше всё пойдёт само.
INFO
fi

cat <<INFO

Состояние и журналы:
  $COMPOSE ps
  $COMPOSE logs --tail 50 worker
INFO
