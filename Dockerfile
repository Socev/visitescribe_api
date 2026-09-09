# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim-bookworm

# libsndfile powers the deep FLAC verification: every decrypted chunk is fully
# decoded and, when STREAMINFO carries one, checked against its MD5.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VS_DATA_DIR=/data \
    VS_API_PORT=8080 \
    VS_ADMIN_PORT=8081

WORKDIR /app
COPY app/ /app/app/
COPY tools/ /app/tools/
COPY README.md /app/

# Runs as root, matching every shipping Olares application. The data volume is
# a hostPath created by the kubelet and owned by root; fsGroup does not apply
# to hostPath volumes, so a non-root container would be locked out of its own
# storage and crash-loop invisibly behind the installer.
RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys,os; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('VS_API_PORT','8080')+'/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
