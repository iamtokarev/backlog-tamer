#!/bin/sh
set -e

case "${1:-}" in
  info) method=getWebhookInfo ;;
  clear) method=deleteWebhook ;;
  *) printf 'Usage: %s {info|clear}\n' "$0" >&2; exit 2 ;;
esac

if [ -f .env ]; then
  . ./.env
fi

if [ -z "${TELEGRAM__BOT_TOKEN:-}" ]; then
  printf '%s\n' 'TELEGRAM__BOT_TOKEN is not set' >&2
  exit 1
fi

exec curl -sS "https://api.telegram.org/bot${TELEGRAM__BOT_TOKEN}/$method"
