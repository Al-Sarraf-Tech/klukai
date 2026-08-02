FROM python:3.11.13-slim-bookworm@sha256:cec9aa7aa96eea4fa036e9b82be1e6b325f2e3707f462d885868df51ec0a4b47

WORKDIR /app

# Runtime libraries used by Coqui, faster-whisper, and the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# The old image resolved these dynamically. Pin the known-compatible CUDA
# wheel pair and XTTS dependency set so rebuilding does not silently change
# the voice. CUDA wheels carry their userspace libraries; the Nobara host only
# needs the NVIDIA driver + container toolkit.
RUN python -m pip install --no-cache-dir \
      torch==2.5.1+cu124 torchaudio==2.5.1+cu124 \
      --index-url https://download.pytorch.org/whl/cu124 \
    && python -m pip install --no-cache-dir \
      TTS==0.22.0 transformers==4.45.2 soundfile==0.12.1

# Install remaining deps
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Auto-accept Coqui TTS license + allow unsafe model loading (needed for XTTS checkpoints)
ENV COQUI_TOS_AGREED=1
ENV TORCH_FORCE_WEIGHTS_ONLY_LOAD=0

COPY app/ ./app/
COPY reference/ ./reference/

# Run as a non-root user. Pre-create the dirs it writes to: /app/models
# (whisper download target) and the HOME caches XTTS/HF/torch use.
RUN groupadd --gid 10001 voiceapp \
    && useradd --create-home --uid 10001 --gid voiceapp \
       --shell /usr/sbin/nologin voiceapp \
    && mkdir -p /app/models/tts /app/models/cache /app/models/huggingface \
       /home/voiceapp/.cache /home/voiceapp/.local/share/tts \
    && chown -R voiceapp:voiceapp /app /home/voiceapp
USER voiceapp
ENV HOME=/home/voiceapp
ENV TTS_HOME=/app/models/tts
ENV HF_HOME=/app/models/huggingface
ENV XDG_CACHE_HOME=/app/models/cache

EXPOSE 8301

# Bind host is env-configurable inside the container (default 0.0.0.0). Host
# exposure is controlled by the Compose port mapping and host/Tailscale ACLs.
# Also set VOICE_API_TOKEN to require bearer auth.
CMD ["sh", "-c", "uvicorn app.main:app --host ${VOICE_BIND_HOST:-0.0.0.0} --port 8301 --log-level info"]
