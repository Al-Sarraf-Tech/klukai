# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Preserve CPython 3.14's ABI on Debian 13; distroless/python3 ships 3.13.
# Only verified runtime wheels, interpreter and missing native libraries cross
# into the final image. No package installer, compiler or shell is included.
ARG PYTHON_IMAGE=python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
ARG DISTROLESS_IMAGE=gcr.io/distroless/base-debian13:nonroot@sha256:a0d70d6a97cd697d9362bc2aae4a6560dd65817e365d0043b07325a97975dc91

FROM ${DISTROLESS_IMAGE} AS runtime-base
FROM ${PYTHON_IMAGE} AS builder
ARG SOURCE_DATE_EPOCH=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}
WORKDIR /build
COPY requirements-build.txt requirements.txt ./
# pip is fixed by the immutable builder image. Build tools have their own hash
# lock; the http-ece sdist is built offline without isolated dependency downloads.
RUN python -m pip install --require-hashes -r requirements-build.txt && \
    python -m pip download --no-deps --no-build-isolation --require-hashes \
      --dest /downloads -r requirements.txt
COPY package_runtime.py ./
RUN --network=none python -m pip wheel --no-index --find-links=/downloads \
      --no-deps --no-build-isolation --require-hashes -r requirements.txt \
      --wheel-dir=/wheels && \
    python package_runtime.py wheels && \
    python -m venv --without-pip /opt/venv && \
    python -m pip --python /opt/venv/bin/python install --no-index \
      --find-links=/wheels --no-deps --require-hashes --no-compile -r /runtime.lock
COPY --from=runtime-base / /distroless-root/
RUN --network=none python package_runtime.py runtime && \
    cp /wheel-materials.json /runtime/usr/share/klukai-runtime/ && \
    cp /runtime.lock /runtime/usr/share/klukai-runtime/ && \
    mkdir -p /runtime/images /runtime/audio /runtime/home/appuser /runtime/app && \
    chown 1000:1000 /runtime/images /runtime/audio /runtime/home/appuser

FROM ${DISTROLESS_IMAGE} AS runtime
ARG VCS_REF=unverified
ARG SOURCE_DATE_EPOCH=0
LABEL org.opencontainers.image.source="https://github.com/Al-Sarraf-Tech/klukai" \
    org.opencontainers.image.revision="${VCS_REF}" \
    cc.appnest.klukai.source-date-epoch="${SOURCE_DATE_EPOCH}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:/usr/local/bin" \
    LD_LIBRARY_PATH="/usr/local/lib:/opt/runtime-libs" \
    HOME="/home/appuser" XDG_CACHE_HOME="/tmp/klukai-cache"
WORKDIR /app
COPY --from=builder /runtime/ /
COPY --from=builder /opt/venv /opt/venv
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY seed_memories.py reannotate_existing.py healthcheck.py ./
USER 1000:1000
EXPOSE 8300
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/opt/venv/bin/python", "/app/healthcheck.py"]
ENTRYPOINT ["/opt/venv/bin/python"]
CMD ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300", "--log-level", "info"]
