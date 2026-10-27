# ⚙️ Configuration

Aidon reads runtime settings from a local `.env` file. Keep secrets local and never commit `.env`.

Base installation is intentionally lightweight. Optional capabilities are installed with extras:

```sh
uv sync --extra memory   # Qdrant vector storage
uv sync --extra embed    # SentenceTransformers/Torchvision embeddings
uv sync --extra sparse   # FastEmbed/SPLADE sparse recall
uv sync --extra vision   # Pillow image and video handling
uv sync --extra kb       # PDF, Word, and HTML parsing
uv sync --extra docs     # EPUB and universal document tooling
```

`./setup.sh` offers these packages interactively; `./setup.sh --with-optional` installs all of them.

## Models

- **Agent, submission and demo:** Muse Spark 1.3, standard.
  Built for long-horizon, multi-step agentic work, with native
  tool calling and MCP support.
- **Agent, tests only:** Muse Spark 1.3 Contributor, same
  capabilities, much cheaper. Switch by changing `AIDON_MODEL`
  only, no code change. Contributor inputs and outputs are used
  to train models, so tests must use non-sensitive test data
  only, never a real private goal, and the demo and submission
  run on the standard model, not Contributor.
- **Speech, when voice is on:** Muse Voice Transcribe, for
  speech-to-text only. It transcribes what the user said, then
  the agent model does the work.
- **Images, optional, only if a workflow requires one:** Muse
  Image 1.0, as an image-generation tool, not as the agent.
  No image tool is used by default, and no core workflow
  requires an image to succeed.

Layout: the code lives in `src/` in six groups
  (agentic, capabilities, interface, memory, social,
  system), so
`pip install -e .` installs it and
`python -m src.system.main goal "..."` runs it.

Aidon is self-hosted by design: clone it to your own computer
or server, run it there, and use the WebUI or call the API.
There is no central Aidon cloud, and your data is not sent to
one. Your memory, workdirs, and keys stay on your machine,
under your Aidon home, outside this repository.

Important runtime settings include the model backend, execution isolation, memory, messaging, budgets, and the Aidon home:

- `AIDON_API_ENDPOINT` — endpoint URL for the model API
- `AIDON_API_KEY` — API key for that endpoint
- `AIDON_MODEL` — model name at that endpoint
- `AIDON_HOME` — where Aidon keeps its state (default `~/.aidon`)
- `AIDON_EXECUTION_BACKEND` — `bubblewrap` by default, or `docker`
- `AIDON_DOCKER_IMAGE` — Docker image when Docker execution is selected
- `AIDON_DOCKER_NETWORK` — keep `0` unless network access is required
- `AIDON_EMBED_MODEL` / `AIDON_SPARSE_MODEL` — dense and sparse memory models
- `AIDON_VERIFIER_MODEL` — optional independent local verifier model
- `AIDON_THINK_PHASES` — local-model phases that may use extended thinking
- `AIDON_SLACK_SIGNING_SECRET` — Slack webhook signing secret

See `examples/.env.example` for the complete commented configuration.

See `examples/.env.example` for the full list, with old key names only, no duplicates.
