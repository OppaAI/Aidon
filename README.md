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

> # What this project wants to showcase for the Meta Global AI Developer Hackathon
>
> Built for the Meta Global AI Developer Hackathon (The entire codebase was made between October 26 – November 4, 2026).  
This project showcases four capabilities. Meta models power its main agentic reasoning and optional image and speech to text features, alongside the supporting models and services described below:
>
>- **Human Natural Language and Interview as source input** — Understands goals in plain natural language and can interview the user to fill in missing details, so people without programming or AI experience can drive an autonomous agent simply by talking to it. Plan mode is the default; users can choose Ask mode for a guided interview that produces a verified spec before planning. The same conversation works in chat, on WhatsApp or Slack, or on the command line.
>- **Grows its own capabilities with tools/skills creation** — drafting, sandbox-testing, and adopting tools and skills that plug into its agentic workflows, including niche ones tailored to a user's own task. A tool plugin is a directory with a manifest and module files: it is tested in the sandbox first, scanned for unsafe patterns, and adopted only on human approval, after which its tools are usable immediately in the current run and every later goal. Skills follow a candidate → validated → trusted lifecycle, and only trusted skills enter plan prompts.
>- **Keeps a human in the loop for decision** — every consequential action passes through human review and approval before it goes live, with verification and rollback on failure, so the agent's autonomy never outruns its owner's consent. Nothing unproven goes live: work runs in a sandbox first, is verified by code, and only failed steps are repaired (at most 3 attempts); the approval card itself fails closed on timeout, and approvals can also be given by replying approve or reject on WhatsApp or Slack.
>- **DAG graph generation of workflows after successful runs** — plans are represented as directed acyclic graphs of steps with dependencies, deliverables, and acceptance criteria, so the agent knows what depends on what and what "done" means for each step. Eligible successful workflows are saved and can be recalled and adapted to similar goals, reducing repeated planning work over time.

> **Meta models used:**
>
>- Muse Spark 1.3 — the reasoning engine: goal understanding, interviewing, plan and DAG construction, recalling and adapting past workflows, repairing failed steps, and verifying results. Lightweight phases can use a fast model tier to keep costs down.
>- Muse Image 1.0 — generates and edits images and diagrams for workflows that need them, through the same API key and endpoint as the chat model, with generation approval-gated and credit-aware.
>- Muse Voice Transcribe — speech-to-text that lets users speak to the agent in natural language instead of typing, via a transcribe tool backed by Meta's transcription API.
>
>*Note: Aidon is still under active development — it was built during this 10-day hackathon, and not all planned agentic capabilities are implemented yet.*

> ## Demo video
>
> 🎞️ [Open the standalone Aidon intro deck](demo/index.html)
> 
>▶️ [Watch the 3-minute demo on YouTube — coming soon](https://www.youtube.com/watch?v=YOUR_VIDEO_ID)
>
><!-- TODO: replace YOUR_VIDEO_ID with the real YouTube video ID after uploading -->



# Autonomous AI agent. You give it one goal — it does the rest.

Give Aidon a goal in plain language, in chat, on WhatsApp or Slack, or on the command line. Aidon will:

1. **Choose a mode** — Plan mode is the default. Choose Ask mode for a guided interview that turns chat answers into a verified spec; an attached spec sheet can also provide the requirements, or run straight through in Execute mode.
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

The demo uses Meta's Model API at runtime for Muse Spark reasoning and optional Muse Image and transcription, alongside the supporting services listed below.

## 🚀 Quickstart

```sh
./setup.sh
# Fill in your own endpoint, key, and model name in $AIDON_HOME/.env
./launch.sh            # WebUI at http://127.0.0.1:8765/
./launch.sh --cli      # conversational command-line chat
./launch.sh goal "Plan a dinner for two"
```

## 📚 Guides

- [Features](FEATURES.md) — the four hackathon capabilities and Mermaid workflow diagrams.
- [Installation](docs/INSTALLATION.md) — prerequisites, setup, and launch.
- [Configuration](docs/CONFIGURATION.md) — models, `.env`, and the Aidon home.
- [Usage](docs/USAGE.md) — WebUI and messaging, core loop, spec sheet, tools and plugins, triggers, research and live execution, budgets, trace, v1 API, and MCP.
- [Security notes](docs/SECURITY.md) — trust boundaries and safe operation.
- [Architecture](docs/ARCHITECTURE.md) — run order, components, storage, and security boundaries.
- [Roadmap](docs/ROADMAP.md) — what is built, what is not built yet, and what is post-hackathon, NOT built, NOT demoed.

## 🧠 Models

- **Main agentic model, submission and demo:** Muse Spark 1.3, standard.
- **Agent, tests only:** Muse Spark 1.3 Contributor, same capabilities, much cheaper. Switch by changing `AIDON_MODEL` only. Contributor inputs and outputs are used to train models, so tests must use non-sensitive test data only.
- **Speech, when voice is on:** Muse Voice Transcribe, speech-to-text only.
- **Images, optional, only if a workflow requires one:** Muse Image 1.0, as a tool, not as the agent.
- **Segmentation, for video editing:** SAM, via the Meta Model API Responses API.

## 🧰 Supporting stack (non-Meta)

Meta is one part of Aidon's model and infrastructure stack. The other components handle local conversation, decision routing, memory, voice output, and execution:

- **Front-end chat:** Ollama with Llama 3.2 handles lightweight conversational turns locally in the configured setup. This keeps the more expensive Muse Spark calls focused on agentic planning and tool work. Aidon supports Ollama's native chat API; this model choice is configuration, not a fixed default.
- **Decision model:** Clef 9B in the deployed setup handles compact routing and ranking decisions, including intent routing and workflow selection. The model ID is configurable. Modal serves the Clef inference endpoint.
- **Embedding model and vector store (RAG):** Google EmbeddingGemma 2 (`google/embeddinggemma-2`), loaded locally through Sentence Transformers and PyTorch, turns text and images into dense embedding vectors. Qdrant is the vector store that indexes and retrieves those vectors. SQLite provides durable fallback memory and stores conversations, interviews, and run/skill records. Optional FastEmbed/SPLADE adds learned sparse retrieval; keyword vectors are used when it is not enabled.
- **Speech output:** Fish Audio provides text-to-speech. Muse Voice Transcribe, listed above, handles speech-to-text.
- **Sandbox and inference hosting:** Modal runs the configured remote sandbox and Clef inference service. For local execution, Aidon also supports Bubblewrap and Docker; an SSH connection can target a VM the owner already operates.
- **Other optional integrations and libraries:** Tavily or SearXNG for web search; YouTube Data API, Gmail API, and Slack API for their respective features; Playwright MCP for browser automation; Three.js and three-vrm for the optional 3D avatar viewer. FFmpeg and Pillow support video and image processing. These integrations require their own local configuration or credentials when used.

Aidon has no central Aidon-hosted service: run the app and WebUI on your own computer or server. Local model, memory, and sandbox options keep their data on that machine. If you select Modal, the configured sandbox workdir is sent to Modal for remote execution; integrations such as Fish Audio, Gmail, or Slack also send the data required for that feature to their providers. Keys remain in your local Aidon configuration, outside this repository.

## 🗂️ Layout

Code lives in `src/`, grouped, not flat:

- `src/system/` — config, model client, and `main.py`, the entry point
- `src/memory/` — Qdrant vector memory, EmbeddingGemma embeddings, SQLite fallback and application records, entity graph, KB, experience, redaction
- `src/agentic/` — goal loop, DAG, adapt, swarm, interview, validation, budgets, trace, research
- `src/interface/` — WebUI, messaging, v1 API, MCP, triggers, scheduler, approval, run control
- `src/capabilities/` — sandbox and execution, tools and plugins, search, voice, GitHub, email, image, charts, Unity, Playwright MCP, SAM, video
- `src/social/` — one-way social publishing, Threads and Instagram, plus YouTube search, metadata, and transcripts
- `docker/` — Dockerfile, Compose configurations, build ignore rules, and the opt-in desktop demo launcher

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
