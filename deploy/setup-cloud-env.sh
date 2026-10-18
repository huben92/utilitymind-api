#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -e .env ]]; then
  echo 'Existing .env retained.'
  exit 0
fi

umask 077
postgres_password="$(openssl rand -hex 24)"
clickhouse_password="$(openssl rand -hex 24)"
printf 'POSTGRES_PASSWORD=%s\nCLICKHOUSE_PASSWORD=%s\n' \
  "$postgres_password" "$clickhouse_password" > .env
mkdir -p data
chmod 700 data
echo 'Created private Compose credentials in api/.env.'
