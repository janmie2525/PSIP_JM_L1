
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-gdal \
        gdal-bin \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir h3 psycopg2-binary

ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY app/ /app/
COPY data/ /data/
