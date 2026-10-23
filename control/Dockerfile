FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SM_SAVES_DIR=/saves \
    SM_LOGS_DIR=/logs \
    SM_BACKUPS_DIR=/backups \
    SM_STATE_DIR=/var/lib/sm-control

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin smcontrol \
    && mkdir -p /saves /logs /backups /var/lib/sm-control \
    && chown -R 1000:1000 /saves /logs /backups /var/lib/sm-control

WORKDIR /opt/sm-control
COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
