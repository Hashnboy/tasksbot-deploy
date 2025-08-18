#!/usr/bin/env bash
set -euo pipefail

if [ ! -f ".env" ]; then
  echo ".env not found. Create it from env/.env.example"
  exit 1
fi

source .env

echo "Setting webhook to ${WEBHOOK_BASE}/${TELEGRAM_TOKEN} ..."
curl -s --get   --data-urlencode "url=${WEBHOOK_BASE}/${TELEGRAM_TOKEN}"   --data-urlencode "secret_token=${WEBHOOK_SECRET:-}"   "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" | jq .

echo "Webhook info:"
curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getWebhookInfo" | jq .
