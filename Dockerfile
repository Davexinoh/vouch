FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# onchainos CLI — marketplace identity resolve requires its TEE AK login
# (raw HTTP AK login is rejected with "An API key is required").
# Pinned release + sha256 from okx/onchainos-skills checksums.txt.
ARG ONCHAINOS_VERSION=v4.4.0
ARG ONCHAINOS_SHA256=c3c2c111792728d787279ddbc3554fe387afbff0fd1d085cd61c8633722cbbac
ADD https://github.com/okx/onchainos-skills/releases/download/${ONCHAINOS_VERSION}/onchainos-x86_64-unknown-linux-gnu /usr/local/bin/onchainos
RUN echo "${ONCHAINOS_SHA256}  /usr/local/bin/onchainos" | sha256sum -c - \
    && chmod 755 /usr/local/bin/onchainos

ENV ONCHAINOS_BIN=/usr/local/bin/onchainos

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
