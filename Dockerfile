FROM python:3.12-slim

ARG APP_UID=10001
ARG APP_GID=10001
ARG APP_VERSION=1.1.1

LABEL org.opencontainers.image.title="timetable-widget" \
      org.opencontainers.image.description="Konfigurierbares ÖPNV-Abfahrts-Widget mit GTFS-Realtime und DB-Timetables" \
      org.opencontainers.image.source="https://github.com/ifs-net/timetable-widget" \
      org.opencontainers.image.url="https://github.com/ifs-net/timetable-widget" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}"

WORKDIR /app

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY config/config.yaml.example /app/config/config.yaml.example

RUN mkdir -p /data /logs /config \
    && chown -R app:app /app /data /logs /config

USER app:app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).getcode() == 200 else 1)"

CMD ["python", "-u", "app.py"]

