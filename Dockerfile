# syntax=docker/dockerfile:1
#
# Multi-stage: build wheels once, ship only the runtime.
# Final image is ~200 MB and runs as a non-root user.

# --------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what the build backend needs, so dependency layers stay cached
# when application code changes.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

ARG INSTALL_POSTGRES=false
RUN if [ "$INSTALL_POSTGRES" = "true" ]; then \
        pip install --no-cache-dir ".[postgres]"; \
    else \
        pip install --no-cache-dir "."; \
    fi

# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="lamal-api" \
      org.opencontainers.image.description="REST API for Swiss LAMal/KVG health insurance premiums, built on official FOPH/BAG open data" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/mahmoud2344/lamal-api"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL="sqlite:////data/lamal.db" \
    HOST=0.0.0.0 \
    PORT=8000

COPY --from=builder /opt/venv /opt/venv

# Unprivileged user; /data is the only writable path the service needs.
RUN useradd --system --uid 10001 --create-home --home-dir /home/lamal lamal \
    && mkdir -p /data \
    && chown -R lamal:lamal /data

USER lamal
WORKDIR /home/lamal
VOLUME ["/data"]
EXPOSE 8000

# Reports unhealthy only if the HTTP layer is down. A container that has not
# finished its first sync is still "healthy" but reports data_loaded=false,
# so an orchestrator does not kill it mid-download.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=8).status==200 else 1)"]

ENTRYPOINT ["lamal-api"]
CMD ["serve"]
