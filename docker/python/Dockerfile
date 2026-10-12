FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 gdal-bin python3-gdal \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app