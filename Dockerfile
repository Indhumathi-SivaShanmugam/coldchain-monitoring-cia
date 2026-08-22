# ---------- Stage 1: build dependencies in an isolated layer ----------
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- Stage 2: minimal runtime image ----------
FROM python:3.12-slim

LABEL maintainer="Indhumathi" \
      org.opencontainers.image.title="coldchain-telemetry-service" \
      org.opencontainers.image.description="Smart Cold Chain Monitoring - telemetry ingestion & alerting microservice" \
      org.opencontainers.image.source="https://github.com/<your-username>/coldchain-monitoring"

# Bring in pre-built dependencies only (keeps final image small)
COPY --from=builder /install /usr/local

WORKDIR /app
COPY app.py .
COPY templates/ templates/

# Non-root user for security (least-privilege principle)
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

ENV PORT=5000 \
    DB_PATH=/data/coldchain.db \
    SERVICE_VERSION=1.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
    sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','5000')+'/healthz').status==200 else sys.exit(1)"

# Gunicorn WSGI server for production-grade concurrency
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "app:app"]
