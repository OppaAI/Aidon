# 🤖 Aidon

**Autonomous AI agent for safe, verifiable goal execution.**

[![Repo](https://img.shields.io/badge/Repo-OppaAI%2FAidon-D9578C)](https://github.com/OppaAI/Aidon)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Status](https://img.shields.io/badge/Status-experimental-orange.svg)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-22.04-E95420?logo=ubuntu&logoColor=white)](https://ubuntu.com/)
[![NVIDIA](https://img.shields.io/badge/NVIDIA-accelerated-76B900?logo=nvidia&logoColor=white)](https://www.nvidia.com/)<br>
[![Sandbox](https://img.shields.io/badge/Sandbox-bubblewrap%20%7C%20Docker-2496ED?logo=docker&logoColor=white)](https://github.com/containers/bubblewrap)
[![Docker](https://img.shields.io/badge/Docker-supported-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Meta Models](https://img.shields.io/badge/Meta%20Models-Muse%20Spark%201.3-0467DF?logo=meta&logoColor=white)](https://ai.meta.com/)
[![Memory](https://img.shields.io/badge/Memory-Qdrant-8E75B2)](https://qdrant.tech/)

---

[![Author](https://img.shields.io/badge/Author-OppaAI-D9578C)](https://github.com/OppaAI)
<img src="https://img.shields.io/badge/Contributors-1-3776AB" alt="Contributors: 1">
<img src="https://img.shields.io/github/commit-activity/t/OppaAI/Aidon?label=Commits&color=76B900" alt="Total commits">
<img src="https://img.shields.io/badge/Start%20date-October%2026%2C%202026-E95420" alt="Start date: October 26, 2026">
<img src="https://img.shields.io/badge/Credentials-none-6C757D" alt="Credentials: none">

---

**Autonomous AI agent. You give it one goal — it does the rest.**

Give Aidon a goal in plain language, in chat, on WhatsApp or Slack, or on the command line. Aidon will:

1. **Interview** — Ask mode is the default: chat questions first, to a verified spec; skip with an attached spec sheet, or run straight through in Execute mode
2. **Research and recall** — web search and YouTube, plus hybrid Qdrant recall over past episodes and knowledge, facts only if a source excerpt contains them
3. **Plan** — steps, deliverables, and criteria, as a DAG, not a loose list
4. **Sandbox** — execute in bubblewrap, Docker, subprocess, or on a VM over SSH, verify by code (with an optional model second opinion, off by default), repair only failed steps, at most 3 attempts
5. **Approve (you)** — one decision card before any live action, fail-closed on timeout
6. **Live** — execute only what was approved, verify, rollback on failure
7. **Learn** — persist the DAG and new skills, so the next similar goal reuses and adapts, saving tokens and calls

## ✨ What is new here

Aidon was created to address a simple problem: many people want to use AI to accomplish complex tasks but don't know 
how to write effective prompts or guide an AI through the necessary steps. So I say, why not let AI itself figure out 
the workflow on how to complete its user's goal, while utilizing as little time and tokens as possible.
Aidon aims to bridge that gap by enabling users to express their goals in natural language while the agent plans, builds 
the capabilities it needs, and validates its work.

All code in this repo was written from scratch during the Meta Global AI Developer Hackathon (October 26–November 4, 2026). 
The implementation is still under development, with components continue to be tested as more features are added and deployed.

The demo runs on Meta models through the Meta Model API. That
is a real part of what Aidon runs, not just a build-time choice.

## 🚀 Quickstart

```sh
./setup.sh
# Fill in your own endpoint, key, and model name in $AIDON_HOME/.env
./launch.sh            # WebUI at http://127.0.0.1:8765/
./launch.sh --cli      # conversational command-line chat
./launch.sh goal "Plan a dinner for two"
```

## 📚 Guides

- [Installation](docs/INSTALLATION.md) — prerequisites, setup, and launch.
- [Configuration](docs/CONFIGURATION.md) — models, `.env`, and the Aidon home.
- [Usage](docs/USAGE.md) — WebUI and messaging, core loop, spec sheet, tools and plugins, triggers, research and live execution, budgets, trace, v1 API, and MCP.
- [Security notes](docs/SECURITY.md) — trust boundaries and safe operation.
- [Architecture](docs/ARCHITECTURE.md) — run order, components, storage, and security boundaries.
- [Roadmap](docs/ROADMAP.md) — what is built, what is not built yet, and what is post-hackathon, NOT built, NOT demoed.

## 🧠 Models

- **Agent, submission and demo:** Muse Spark 1.3, standard.
- **Agent, tests only:** Muse Spark 1.3 Contributor, same capabilities, much cheaper. Switch by changing `AIDON_MODEL` only. Contributor inputs and outputs are used to train models, so tests must use non-sensitive test data only.
- **Speech, when voice is on:** Muse Voice Transcribe, speech-to-text only.
- **Images, optional, only if a workflow requires one:** Muse Image 1.0, as a tool, not as the agent.
- **Segmentation, for video editing:** SAM, via the Meta Model API Responses API.

Aidon is self-hosted by design: clone it to your own computer or server, run it there, and use the WebUI or call the API. There is no central Aidon cloud, and your data is not sent to one. Your memory, workdirs, and keys stay on your machine, under your Aidon home, outside this repository.

## 🗂️ Layout

Code lives in `src/`, grouped, not flat:

- `src/system/` — config, model client, and `main.py`, the entry point
- `src/memory/` — Qdrant memory, embedder, entity graph, KB, experience, redaction
- `src/agentic/` — goal loop, DAG, adapt, swarm, interview, validation, budgets, trace, research
- `src/interface/` — WebUI, messaging, v1 API, MCP, triggers, scheduler, approval, run control
- `src/capabilities/` — sandbox and execution, tools and plugins, search, voice, GitHub, email, image, charts, Unity, Playwright MCP, SAM, video
- `src/social/` — one-way social publishing, Threads and Instagram, plus YouTube search, metadata, and transcripts

Run it with `python -m src.system.main goal "..."`, or `./launch.sh goal "..."`. `src/` itself has no modules at the top level, only `__init__.py` and the groups.

## 🔮 Future — post-hackathon (to be implemented in the near future)

The items below are ideas only, for direction. The full items are
not in this repository; small parts of the first two exist (see below).

1. **Procedural 3D game-asset generation.** Cube, sphere, and
   cylinder primitives exist; a game maker asking for an arbitrary
   crate, chair, or sword gets no usable mesh yet.
2. **Dedicated live toolset.** Individual live tools exist
   (YouTube, webcam); a dedicated live toolset as a subsystem,
   distinct from sandbox tools, does not yet.
3. **Stronger sandbox backend (spike).** A Tier-2 backend behind
   the isolation seam, with bubblewrap staying default until the
   probes say otherwise.
4. **Deterministic plan validation.** A canonical plan format
   plus a cross-file validator, so plan, ledger, and
   deliverables must agree by code before approval.
5. **Biological Simulation Layer with Fruit fly connectome:**
   Explore bio-inspired neural circuits, reinforcement learning
   with reward signals, adaptive neural processes, and emergent
   decision-making, investigating whether increasingly integrated
   learning, memory, motivation, and self-regulation could give
   rise to more complex agent behaviors—and, as a long-term open
   research question, whether any form of machine sentience could
   emerge.
