FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1

# Non-root runtime user. Match it to the owner of ./data on the host with
# `docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)`
# or APP_UID/APP_GID in .env (compose passes them as build args).
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --non-unique --gid "$APP_GID" app \
 && useradd --non-unique --uid "$APP_UID" --gid "$APP_GID" --no-create-home \
      --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY zeekrmate/ zeekrmate/
COPY scripts/ scripts/
COPY sql/ sql/
RUN mkdir -p /app/data && chown app:app /app/data && chmod 700 /app/data

USER app

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "from zeekrmate.config import load_config; from zeekrmate import db; db.connect(load_config('config.json')).close()" || exit 1

CMD ["python", "-m", "zeekrmate"]
