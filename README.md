# 🤖 Aidon

**Autonomous AI agent for safe, verifiable goal execution.**

[![Repo](https://img.shields.io/badge/Repo-OppaAI%2FAidon-D9578C)](https://github.com/OppaAI/Aidon)
![Status](https://img.shields.io/badge/Status-experimental-orange.svg)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Ubuntu](https://img.shields.io/badge/Ubuntu-22.04-E95420?logo=ubuntu&logoColor=white)
![NVIDIA](https://img.shields.io/badge/NVIDIA-accelerated-76B900?logo=nvidia&logoColor=white)<br>
![Sandbox](https://img.shields.io/badge/Sandbox-bubblewrap%20%7C%20Docker-2496ED?logo=docker&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-supported-2496ED?logo=docker&logoColor=white)
![Meta Models](https://img.shields.io/badge/Meta%20Models-Muse%20Spark%201.3-0467DF?logo=meta&logoColor=white)
![Memory](https://img.shields.io/badge/Memory-Qdrant-8E75B2)

---

## 👤 Project provenance

[![Author](https://img.shields.io/badge/Author-OppaAI-D9578C)](https://github.com/OppaAI)
![Contributors](https://img.shields.io/badge/Contributors-1-D9578C)
![Commits](https://img.shields.io/badge/Commits-131-D9578C)
![First day](https://img.shields.io/badge/First%20day-October%2026%2C%202026-D9578C)
![Credentials](https://img.shields.io/badge/Credentials-none%20committed-D9578C)

---

**Autonomous AI agent. You give it one goal — it does the rest.**

Give Aidon a goal in plain language, in chat, on WhatsApp or Slack, or on the command line. Aidon will:

1. **Interview** — chat questions only if needed, to a verified spec, or skip with an attached spec sheet
2. **Research and recall** — web search and YouTube, plus hybrid Qdrant recall over past episodes and knowledge, facts only if a source excerpt contains them
3. **Plan** — steps, deliverables, and criteria, as a DAG, not a loose list
4. **Sandbox** — execute in bubblewrap or Docker, verify by code, repair only failed steps, at most 3 attempts
5. **Approve (you)** — one decision card before any live action, fail-closed on timeout
6. **Live** — execute only what was approved, verify, rollback on failure
7. **Learn** — persist the DAG and new skills, so the next similar goal reuses and adapts, saving tokens and calls

## What is new here

The idea was validated with an earlier prototype, and all code
in this repository was written during the Meta Global AI
Developer Hackathon, Oct 26 - Nov 4, 2026. Nothing in this
repository was copied from that prototype: the design was read,
and the code was written fresh, part by part, and tested as it
was written.

The demo runs on Meta models through the Meta Model API. That
is a real part of what Aidon runs, not just a build-time choice.

## Quickstart

```sh
./setup.sh
# Fill in your own endpoint, key, and model name in $AIDON_HOME/.env
./launch.sh            # WebUI at http://127.0.0.1:8002/
./launch.sh goal "Plan a dinner for two"
```

## Guides

- [Installation](docs/INSTALLATION.md) — prerequisites, setup, and launch.
- [Configuration](docs/CONFIGURATION.md) — models, `.env`, and the Aidon home.
- [Usage](docs/USAGE.md) — WebUI and messaging, core loop, spec sheet, tools and plugins, triggers, research and live execution, budgets, trace, v1 API, and MCP.
- [Security notes](docs/SECURITY.md) — trust boundaries and safe operation.
- [Architecture](ARCHITECTURE.md) — run order, components, storage, and security boundaries.
- [Roadmap](ROADMAP.md) — what is built, what is not built yet, and what is post-hackathon, NOT built, NOT demoed.

## Models

- **Agent, submission and demo:** Muse Spark 1.3, standard.
- **Agent, tests only:** Muse Spark 1.3 Contributor, same capabilities, much cheaper. Switch by changing `AIDON_MODEL` only. Contributor inputs and outputs are used to train models, so tests must use non-sensitive test data only.
- **Speech, when voice is on:** Muse Voice Transcribe, speech-to-text only.
- **Images, optional, only if a workflow requires one:** Muse Image 1.0, as a tool, not as the agent.
- **Segmentation, for video editing:** SAM, via the Meta Model API Responses API.

Aidon is self-hosted by design: clone it to your own computer or server, run it there, and use the WebUI or call the API. There is no central Aidon cloud, and your data is not sent to one. Your memory, workdirs, and keys stay on your machine, under your Aidon home, outside this repository.

## Layout

Code lives in `src/`, grouped, not flat:

- `src/system/` — config, model client, and `main.py`, the entry point
- `src/memory/` — Qdrant memory, embedder, entity graph, KB, experience, redaction
- `src/agentic/` — goal loop, DAG, adapt, swarm, interview, validation, budgets, trace, research
- `src/interface/` — WebUI, messaging, v1 API, MCP, triggers, scheduler, approval, run control
- `src/capabilities/` — sandbox and execution, tools and plugins, search, voice, GitHub, email, image, charts, Unity, Playwright MCP, SAM, video
- `src/social/` — one-way social publishing, Threads and Instagram, plus YouTube search, metadata, and transcripts

Run it with `python -m src.system.main goal "..."`, or `./launch.sh goal "..."`. `src/` itself has no modules at the top level, only `__init__.py` and the groups.

## Future — post-hackathon (NOT built, NOT demoed)

The items below are ideas only, for direction. None of them is
in this repository, none is in the hackathon demo, and none was
built during Oct 26 - Nov 4.

1. **Procedural 3D game-asset generation.** A game maker asking
   for a crate, chair, or sword gets no usable mesh today.
2. **Dedicated live toolset.** Live tools with real network and
   credentials, distinct from sandbox tools.
3. **Stronger sandbox backend (spike).** A Tier-2 backend behind
   the isolation seam, with bubblewrap staying default until the
   probes say otherwise.
4. **Deterministic plan validation.** A canonical plan format
   plus a cross-file validator, so plan, ledger, and
   deliverables must agree by code before approval.
