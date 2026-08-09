#!/bin/sh
# Dispatches on the first argument, matching docker-compose.yml's `command:`.
# Absent an argument (the api service), migrations run first — the compose
# file's own comment says "The API runs migrations on start, so there is no
# separate setup step" — and this is where that promise is kept.
set -e

case "$1" in
  worker)
    exec celery -A app.celery_app worker --loglevel=info
    ;;
  beat)
    exec celery -A app.celery_app beat --loglevel=info
    ;;
  *)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
esac
