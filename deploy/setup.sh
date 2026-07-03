#!/usr/bin/env bash
# Установка/обновление сервиса стратег-бота на VPS (Ubuntu).
# Запуск из папки проекта:  bash deploy/setup.sh
set -euo pipefail

SERVICE=strateg-bot
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "== Проект: $PROJECT_DIR =="

if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "!! Нет файла .env — создай его из .env.example и впиши секреты:"
  echo "   nano $PROJECT_DIR/.env"
  exit 1
fi

# python3 обязателен (используется только stdlib — pip не нужен)
command -v python3 >/dev/null 2>&1 || { echo "!! python3 не установлен: apt install -y python3"; exit 1; }

# systemd-юнит
sed "s#/root/strateg-mebelroom#${PROJECT_DIR}#g" "$PROJECT_DIR/deploy/${SERVICE}.service" \
  > "/etc/systemd/system/${SERVICE}.service"

systemctl daemon-reload
systemctl enable "${SERVICE}"
systemctl restart "${SERVICE}"
sleep 2
systemctl --no-pager --full status "${SERVICE}" || true

echo
echo "== Готово. Живые логи:  journalctl -u ${SERVICE} -f =="
