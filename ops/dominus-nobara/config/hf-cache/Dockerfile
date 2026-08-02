# syntax=docker/dockerfile:1.7
FROM python:3.12.11-slim@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY materialize.py /usr/local/bin/materialize-hf-cache

USER 1000:1001
ENTRYPOINT ["python", "/usr/local/bin/materialize-hf-cache"]

