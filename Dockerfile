# Sprint 001 safe runtime skeleton. No secrets are baked into the image.
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
ARG MEDIAREFINERY_INSTALL_TARGET=.

RUN groupadd --system mediarefinery \
    && useradd --system --gid mediarefinery --home-dir /app mediarefinery

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir "${MEDIAREFINERY_INSTALL_TARGET}"

RUN mkdir -p /config /data/state /data/tmp /data/reports \
    && chown -R mediarefinery:mediarefinery /app /config /data

USER mediarefinery

# Safe default: show CLI help. Operators must explicitly run scan.
CMD ["mediarefinery", "--help"]

