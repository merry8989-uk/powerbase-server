# Powerbase server.
#
# One process, one SQLite file. The only thing outside the image is the /data
# volume, which holds the database and its backups.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is only here for the healthcheck below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first, so editing the source does not re-resolve them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV POWERBASE_DB=/data/powerbase.db \
    POWERBASE_BACKUP_DIR=/data/backups \
    POWERBASE_HOST=0.0.0.0 \
    POWERBASE_PORT=8080

# Runs as an unprivileged user: this process accepts untrusted input from the
# internet, so it should not be able to write to its own code.
RUN useradd --create-home --uid 10001 powerbase \
 && mkdir -p /data/backups \
 && chown -R powerbase:powerbase /data /app
USER powerbase

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/v1/health || exit 1

CMD ["python", "run.py"]
