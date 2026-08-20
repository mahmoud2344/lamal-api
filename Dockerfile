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

# Extra trusted CA certificates, for hosts behind a TLS-inspecting proxy
# (corporate middleboxes, and consumer antivirus such as Norton Web Shield).
# Such a proxy re-signs pypi.org with its own root; the host trusts it but this
# container does not, so pip fails certificate verification.
#
# Drop the proxy's root as a .crt or .pem into ./certs/ and it is added to the
# trust store used for the install. The directory is empty by default, so a
# normal build verifies against the stock bundle and is unaffected.
COPY certs/ /tmp/extra-certs/

# Copy only what the build backend needs, so dependency layers stay cached
# when application code changes.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

ARG INSTALL_POSTGRES=false
# PIP_CERT rather than --cert: pip builds wheels in an isolated environment by
# spawning a second pip, and command-line flags do not reach that child. The
# environment variable does.
# `find` rather than a glob test: `ls a.crt b.pem` exits non-zero when *either*
# pattern fails to match, so a directory holding only .pem files would be
# silently ignored.
RUN set -eu; \
    EXTRA="$(find /tmp/extra-certs -maxdepth 1 -type f \
        \( -name '*.crt' -o -name '*.pem' -o -name '*.cer' \) 2>/dev/null || true)"; \
    if [ -n "$EXTRA" ]; then \
        : > /tmp/ca-bundle.crt; \
        if [ -f /etc/ssl/certs/ca-certificates.crt ]; then \
            cat /etc/ssl/certs/ca-certificates.crt >> /tmp/ca-bundle.crt; \
        fi; \
        for cert in $EXTRA; do cat "$cert" >> /tmp/ca-bundle.crt; done; \
        export PIP_CERT=/tmp/ca-bundle.crt SSL_CERT_FILE=/tmp/ca-bundle.crt; \
        echo "using extra CA certificates from ./certs/:"; \
        echo "$EXTRA"; \
    fi; \
    if [ "$INSTALL_POSTGRES" = "true" ]; then \
        pip install --no-cache-dir ".[postgres]"; \
    else \
        pip install --no-cache-dir "."; \
    fi; \
    rm -rf /tmp/extra-certs /tmp/ca-bundle.crt

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
