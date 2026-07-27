FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# onchainos CLI — marketplace identity resolve requires its TEE AK login
# (raw HTTP AK login is rejected with "An API key is required").
# PINNED to v4.2.2: the LAST release with headless AK login. v4.4.0 replaced
# `wallet login` with a browser-only social-login flow (init/open/poll) that
# returns ok:true without creating a session — useless on a server.
# sha256 from okx/onchainos-skills v4.2.2 checksums.txt.
ARG ONCHAINOS_VERSION=v4.2.2
ARG ONCHAINOS_SHA256=89eafa29fcf779758a742bb2fa80f799364306f7407d5cb3695d7ae8b5b8b713
ADD https://github.com/okx/onchainos-skills/releases/download/${ONCHAINOS_VERSION}/onchainos-x86_64-unknown-linux-gnu /usr/local/bin/onchainos
RUN echo "${ONCHAINOS_SHA256}  /usr/local/bin/onchainos" | sha256sum -c - \
    && chmod 755 /usr/local/bin/onchainos

ENV ONCHAINOS_BIN=/usr/local/bin/onchainos

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
