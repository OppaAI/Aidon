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
docker group applies. A VM is post-hackathon, future
only, not installed by setup, and not needed now.
`launch.sh` runs setup first if the home is missing,
then starts the WebUI server with the same home, backend, and
ingest path.

At minimum, configure an endpoint, key, and model. Optional features can be installed separately:

- `AIDON_API_ENDPOINT` — endpoint URL for the model API
- `AIDON_API_KEY` — API key for that endpoint
- `AIDON_MODEL` — model name at that endpoint
- `AIDON_HOME` — where Aidon keeps its state (default `~/.aidon`)

The base install does not pull heavyweight ML packages. Use `uv sync --extra memory`,
`--extra embed`, `--extra sparse`, `--extra vision`, `--extra kb`, or `--extra docs`
for the corresponding capabilities. `./setup.sh --with-optional` installs all optional groups.

Setup and launch, in that order, are the entry points, as in the Aidon guides.

```sh
./setup.sh
# Fill in your own endpoint, key, and model name in $AIDON_HOME/.env
./launch.sh goal "Plan a dinner for two"
```

A VM is post-hackathon, future only, not installed by setup, and not needed now.
