FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIDON_HOME=/data

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

RUN python -m pip install --no-cache-dir .

USER aidon:aidon
EXPOSE 8765
ENTRYPOINT ["aidon"]
CMD ["webui", "serve", "--port", "8765"]
