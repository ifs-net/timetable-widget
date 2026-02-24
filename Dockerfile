FROM python:3.12-slim

ARG APP_UID=10001
ARG APP_GID=10001
ARG APP_VERSION=1.3.5-DEV
ARG APP_GIT_SHA=unknown
ARG APP_BUILD_DATE=unknown
ENV APP_VERSION=${APP_VERSION}
ENV APP_GIT_SHA=${APP_GIT_SHA}
ENV APP_BUILD_DATE=${APP_BUILD_DATE}

LABEL org.opencontainers.image.title="timetable-widget" \
      org.opencontainers.image.description="Konfigurierbares OePNV-Abfahrts-Widget mit GTFS-Realtime und DB-Timetables" \
      org.opencontainers.image.source="https://github.com/ifs-net/timetable-widget" \
      org.opencontainers.image.url="https://github.com/ifs-net/timetable-widget" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${APP_GIT_SHA}" \
      org.opencontainers.image.created="${APP_BUILD_DATE}"

WORKDIR /app

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY VERSION /app/VERSION
COPY app.py .
COPY web_views.py .
COPY providers_gtfs_rt.py .
COPY providers_db_timetables.py .
COPY service_polling.py .
COPY config/config.yaml.example /app/config/config.yaml.example

RUN mkdir -p /data /logs /config \
    && chown -R app:app /app /data /logs /config

USER app:app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).getcode() == 200 else 1)"

CMD ["python", "-u", "app.py"]
