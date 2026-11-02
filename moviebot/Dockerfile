# moviebot: the Telegram assistant of media-stack (built by docker compose from ./moviebot).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Unprivileged user. Compose can run the container as PUID:PGID instead, so the files the bot
# writes into ./state (answers to the guard scripts' questions, its own state) belong to the same
# user as the host scripts.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --home-dir /app \
       --shell /usr/sbin/nologin app

COPY bot.py /app/bot.py
COPY botlib/*.py /app/botlib/

USER app

# The webhook port stays inside the compose network (it is never published). Any HTTP answer,
# even the 404 for "/", means the bot's event loop is alive.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import http.client, os; c = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('WEBHOOK_PORT') or 8090), timeout=5); c.request('GET', '/'); c.getresponse()"]

CMD ["python", "-u", "/app/bot.py"]
