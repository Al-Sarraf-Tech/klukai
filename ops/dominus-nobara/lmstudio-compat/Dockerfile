FROM docker.io/library/python:3.13.7-slim-bookworm@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --only-binary=:all: -r requirements.lock

COPY gateway ./gateway

USER 65532:65532
EXPOSE 1234

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1234/health', timeout=2).read()"]

CMD ["python", "-m", "uvicorn", "gateway.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "1234", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", ""]
