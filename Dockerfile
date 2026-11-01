# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

# Pin the installer so image builds stay reproducible. uv is only needed
# during the build; the application runs from its virtual environment.
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIDON_HOME=/data \
    HF_HOME=/data/.cache/huggingface \
    FASTEMBED_CACHE_PATH=/data/.cache/fastembed \
    UV_PROJECT_ENVIRONMENT=/opt/aidon/.venv \
    UV_CACHE_DIR=/root/.cache/uv \
    UV_LINK_MODE=copy \
    PATH=/opt/aidon/.venv/bin:$PATH

WORKDIR /opt/aidon

RUN apt-get update \
    && apt-get install --no-install-recommends -y bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 aidon \
    && useradd --uid 10001 --gid aidon --create-home \
       --shell /usr/sbin/nologin aidon \
    && mkdir -p /data \
    && chown aidon:aidon /data

COPY pyproject.toml uv.lock README.md ./

# Install the locked runtime dependencies before copying application source.
# Source edits then reuse this expensive layer, including the ML packages.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project \
        --extra embed --extra sparse --extra vision

COPY src ./src

# Install Aidon itself into the already populated environment. Keep the
# production package non-editable; the dev Compose overlay supplies source.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --extra embed --extra sparse --extra vision

# Modal is only needed by the remote sandbox backend. Keep it in its own
# layer so adding/updating it does not invalidate the larger ML dependency
# install above. Pin it for reproducible Docker builds.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/aidon/.venv/bin/python "modal==1.6.3"

USER aidon:aidon
EXPOSE 8765
ENTRYPOINT ["aidon"]
CMD ["webui", "serve", "--port", "8765"]
