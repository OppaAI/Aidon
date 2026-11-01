FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIDON_HOME=/data \
    HF_HOME=/data/.cache/huggingface \
    FASTEMBED_CACHE_PATH=/data/.cache/fastembed

WORKDIR /opt/aidon

RUN apt-get update \
    && apt-get install --no-install-recommends -y bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 aidon \
    && useradd --uid 10001 --gid aidon --create-home \
       --shell /usr/sbin/nologin aidon \
    && mkdir -p /data \
    && chown aidon:aidon /data

COPY pyproject.toml README.md ./
COPY src ./src

# The WebUI should start with semantic memory available. Installing only the
# base package makes _make_memory fall back to HashEmbedder, which is useful
# for offline tests but not real recall. These extras provide the dense and
# sparse embedding backends plus image support.
RUN python -m pip install --no-cache-dir '.[embed,sparse,vision]'

USER aidon:aidon
EXPOSE 8765
ENTRYPOINT ["aidon"]
CMD ["webui", "serve", "--port", "8765"]
