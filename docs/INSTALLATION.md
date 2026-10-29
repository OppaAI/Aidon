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
