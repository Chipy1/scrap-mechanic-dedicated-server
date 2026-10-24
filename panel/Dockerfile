FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SM_NOVNC_DIR=/opt/sm-panel/static/novnc

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'fastapi>=0.110,<1' 'uvicorn[standard]>=0.29,<1' 'httpx>=0.27,<1' 'python-multipart>=0.0.9,<1' \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin panel

WORKDIR /opt/sm-panel
COPY app ./app

RUN mkdir -p /opt/sm-panel/static \
    && curl -fsSL --retry 3 https://codeload.github.com/novnc/noVNC/tar.gz/refs/tags/v1.5.0 \
        | tar -xz -C /tmp \
    && mv /tmp/noVNC-1.5.0 /opt/sm-panel/static/novnc \
    && chown -R panel:panel /opt/sm-panel

USER 1000:1000
EXPOSE 8081
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8081", "--proxy-headers"]
