FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04

WORKDIR /app

# Install Python 3.11 (Coqui TTS requires <3.12)
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common curl ffmpeg libsndfile1 \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3.11-dev \
    && rm -rf /var/lib/apt/lists/*

# Create venv with Python 3.11
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install PyTorch with CUDA
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cu126

# Install Coqui TTS (includes XTTS v2)
# Pin transformers<4.46 to avoid BeamSearchScorer removal
RUN pip install --no-cache-dir "transformers<4.46" "torchaudio<2.6" soundfile && pip install --no-cache-dir TTS

# Install remaining deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Keep Piper as fallback
RUN pip install --no-cache-dir piper-tts || true

# Auto-accept Coqui TTS license + allow unsafe model loading (needed for XTTS checkpoints)
ENV COQUI_TOS_AGREED=1
ENV TORCH_FORCE_WEIGHTS_ONLY_LOAD=0

COPY app/ ./app/
COPY reference/ ./reference/

# Run as a non-root user. Pre-create the dirs it writes to: /app/models
# (whisper download target) and the HOME caches XTTS/HF/torch use.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin voice \
    && mkdir -p /app/models /home/voice/.cache /home/voice/.local/share/tts \
    && chown -R voice:voice /app /home/voice
USER voice
ENV HOME=/home/voice

EXPOSE 8301

# Bind host is env-configurable inside the container (default 0.0.0.0). Host
# exposure is controlled by the Compose port mapping and host/Tailscale ACLs.
# Also set VOICE_API_TOKEN to require bearer auth.
CMD ["sh", "-c", "uvicorn app.main:app --host ${VOICE_BIND_HOST:-0.0.0.0} --port 8301 --log-level info"]
