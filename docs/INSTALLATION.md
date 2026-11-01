# 🧰 Installation

## 🚀 Run it

```sh
./setup.sh
# Fill in your own endpoint, key, and model name in $AIDON_HOME/.env
./launch.sh goal "Plan a dinner for two"
```

Setup and launch, in that order, are the entry points.
`setup.sh` is idempotent, it creates the Aidon home with
workspace, ingest, plugins, skills, specs, and library
directories, copies `examples/.env.example` to
`$AIDON_HOME/.env` only if that file does not already
exist, and checks bubblewrap and Docker, reporting what
is available. It does not install Docker, and it does
not install a VM. Docker is installed by you, on the
machine where Aidon runs, if you want the Docker
backend: Docker Desktop on macOS or Windows, or
`docker.io` on Ubuntu, then log out and back in so the
docker group applies. A VM is not installed or
provisioned by setup; the VM backend connects over
SSH to a VM you already run, and a disposable VM
per run is post-hackathon.
`launch.sh` runs setup first if the home is missing,
then starts the WebUI server with the same home, backend, and
ingest path.

## Docker WebUI

Docker Compose runs the WebUI in a container and keeps Aidon's state in
`./aidon-data`. This Compose setup uses Linux host networking so the WebUI
can keep its localhost-only bind and localhost authentication behavior. It
is intended for Docker Engine on Linux. Copy the example configuration into
the data directory, add your model endpoint and key there, then start the
service:

```sh
mkdir -p aidon-data
cp examples/.env.example aidon-data/.env
chmod 600 aidon-data/.env
# Edit aidon-data/.env with your model endpoint, key, and model name.
export AIDON_UID="$(id -u)" AIDON_GID="$(id -g)"
docker compose up --build -d
```

The Docker image includes the dense and sparse embedding packages so memory
uses semantic embeddings instead of the test-only hash fallback. On first
use, the embedding models are downloaded into `aidon-data`; allow extra disk
space and network access for that initial download. If the dashboard still
shows `HashEmbedder`, check that `.env` does not set `AIDON_EMBEDDER=hash`.

If you already started an earlier Docker image and its dashboard showed
`HashEmbedder · QdrantStore`, its local Qdrant collections were created with
128-dimensional hash vectors. The semantic embedder uses 768 dimensions, so
back up and move that database before switching. Stop the container, then run
this from the repository root (the timestamped directory preserves the old
database; `.env` is untouched):

```sh
docker compose down
mv aidon-data/qdrant "aidon-data/qdrant-hash-backup-$(date +%Y%m%d-%H%M%S)"
export AIDON_UID="$(id -u)" AIDON_GID="$(id -g)"
docker compose up --build -d
```

The new Qdrant collections are created with the semantic embedder's dimension.
Keep the backup until you have confirmed the new memory works; hash vectors
cannot be reused as semantic vectors.

Open `http://127.0.0.1:8765/`. Choose another local port by setting
`AIDON_WEBUI_PORT` before `docker compose up`. Stop the service with
`docker compose down`; the `aidon-data` directory remains on the host. The
container runs as your supplied UID/GID (10001 by default), so the export
above lets it write files owned by your host user. Keep
the WebUI bound to localhost; do not change it to `0.0.0.0` or publish the
full WebUI to the internet. Host networking shares the host network stack,
so only run this container from a trusted Aidon image. This container
packaging does not certify that Bubblewrap or any
other execution backend can run inside your Docker host; validate the actual
backend separately before using execution features.

For a release, update `pyproject.toml`, move the corresponding notes from
`[Unreleased]` into a versioned `CHANGELOG.md` section, run CI, then push a
matching tag such as `v0.4.1`. The release workflow checks the tag against
the project version and changelog, publishes the GitHub Release, and pushes
the container image to GitHub Container Registry.

At minimum, configure an endpoint, key, and model. Heavy ML packages and optional features are offered interactively by `./setup.sh`; answer yes only for capabilities you need. Use `--with-optional` to accept every optional package without prompting:

- `AIDON_API_ENDPOINT` — endpoint URL for the model API
- `AIDON_API_KEY` — API key for that endpoint
- `AIDON_MODEL` — model name at that endpoint
- `AIDON_HOME` — where Aidon keeps its state (default `~/.aidon`)

The base install does not pull heavyweight ML packages. Use `uv sync --extra memory`,
`--extra embed`, `--extra sparse`, `--extra vision`, `--extra kb`, or `--extra docs`
for the corresponding capabilities. `./setup.sh --with-optional` installs all optional groups.

Vector memory: embedded local mode works for one process. For shared memory
across the WebUI, tick loop, and listeners, setup offers a Qdrant server
in Docker: answer yes at the prompt, or run `./setup.sh --with-qdrant`.
It starts the `aidon-qdrant` container (pinned image, data in
`~/.aidon/qdrant-server`), health-checks port 6333, and sets
`AIDON_QDRANT_URL=http://127.0.0.1:6333` in `.env`. Without a server,
the dashboard shows a memory-fallback warning instead of failing silently.
A previous binary + systemd install is retired automatically: its service
is stopped and disabled before the container starts.

Web search with the searxng backend works the same way: setup offers a
SearXNG container (`aidon-searxng` on localhost:8888, settings in
`~/.aidon/searxng`), or run `./setup.sh --with-searxng`. If you already
run your own SearXNG, keep its URL in `AIDON_SEARXNG_URL` and skip the offer.

Setup and launch, in that order, are the entry points, as in the Aidon guides.

```sh
./setup.sh
# Fill in your own endpoint, key, and model name in $AIDON_HOME/.env
./launch.sh goal "Plan a dinner for two"
```

A VM is not installed or provisioned by setup; the VM backend connects over SSH to a VM you already run, and a disposable VM per run is post-hackathon.
