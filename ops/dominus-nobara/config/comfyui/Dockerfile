# syntax=docker/dockerfile:1.7
# Python platform manifest (linux/amd64), not the mutable multi-arch tag.
FROM python:3.12.11-slim@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49

ARG COMFYUI_COMMIT=322122449c9d2ba8b8df1bb517364527dd0615f1
ARG COMFYUI_ARCHIVE_SHA256=a7ce60980450f764f6b4684fbe97b600f6bdaa2a7a4ff9b4c0052ae4ebce0f2f

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface \
    TORCH_HOME=/cache/torch \
    TRITON_CACHE_DIR=/cache/triton \
    XDG_CACHE_HOME=/cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --require-hashes --only-binary=:all: -r /tmp/requirements.lock \
    && rm -f /tmp/requirements.lock

# Triton compiles its tiny CUDA driver shim and selected kernels on first use.
# Keep the compiler inside the container; generated objects land in the
# RAID-backed /cache mount rather than on the Nobara host root filesystem.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN install -d /opt/ComfyUI \
    && curl --fail --location --silent --show-error \
        "https://github.com/comfyanonymous/ComfyUI/archive/${COMFYUI_COMMIT}.tar.gz" \
        --output /tmp/comfyui.tar.gz \
    && echo "${COMFYUI_ARCHIVE_SHA256}  /tmp/comfyui.tar.gz" | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/comfyui.tar.gz \
        --directory /opt/ComfyUI --strip-components=1 \
    && rm -f /tmp/comfyui.tar.gz \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin comfy \
    && install -d -o comfy -g comfy /cache /data /data/input /data/output /data/user \
    && chown -R comfy:comfy /opt/ComfyUI

WORKDIR /opt/ComfyUI
USER comfy

EXPOSE 8188

ENTRYPOINT ["python", "main.py"]
CMD ["--listen", "0.0.0.0", "--port", "8188", "--disable-auto-launch"]
