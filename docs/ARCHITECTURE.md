# 🏗️ Architecture

Aidon is an autonomous agent: the user gives only a goal,
in plain language, and Aidon researches, plans, tests in
a sandbox, verifies, asks approval, executes live on
approval, and learns from the run, so the next similar
goal starts from what actually worked.

## 🔁 Run order

```mermaid
flowchart LR
    A[🎯 Goal] --> B{Run mode}
    B -->|Ask| C[💬 Interview and freeze spec]
    C --> D{Spec approval}
    D -->|reject| X[🛑 Stop]
    D -->|approve and proceed| E[🧠 Recall and research]
    B -->|Plan default / Execute| E
    E --> F[📋 Build and validate plan DAG]
    F --> G{Run mode}
    G -->|Plan: stop for review| Y[⏸️ Wait for proceed]
    Y --> H[🧪 Sandbox]
    G -->|Execute: continue| H
    H --> I[✅ Verify]
    I -->|failed| J[🔧 Repair failed steps]
    J --> H
    I --> K{Live action approval}
    K -->|rejected / timeout| X
    K -->|approved| L[🚀 Live action]
    L --> M[📚 Learn and save successful workflow]
```

1. Interpret: a chat message, an attached spec sheet,
   a trigger event, or a v1 or MCP goal submission. Plan
   mode is the interactive default; Ask and Execute are
   explicit alternatives.
2. Ask mode only: chat questions, without an approval
   clock, produce a frozen spec. The user can approve the
   spec on a decision card; proceeding then enters Plan
   mode. An attached spec sheet can supply requirements.
3. Recall: previous Experiences, episodic memory, and
   research sources for a similar goal, before planning
   fresh.
4. Research: facts verified only if a fetched source
   excerpt contains them, with source, date, and excerpt
   recorded. Unverified claims stay unverified.
5. Plan: steps, optional file deliverables, and criteria, as JSON,
   from the model, bound by the frozen spec.
6. Sandbox: steps run with sandbox tools, or with
   adopted plugin tools, in a bubblewrap sandbox, a
   Docker container, or on a VM over SSH, chosen by
   the owner, never by the model.
7. Verify: file deliverables must exist and be non-empty;
   output-only steps must complete successfully, and
   criteria are checked by code, ledger totals are
   recomputed by code. An optional model second opinion
   (AIDON_MODEL_VERIFIER, off by default) judges content
   only after code passes, and never overrides code.
8. Repair: only failed steps are repaired, passed
   files are untouched, at most 3 attempts in total.
9. Review and approval: Plan mode stops for plan review;
   Ask mode has a separate spec confirmation. A decision
   card also gates consequential live actions and fails
   closed on timeout.
10. Live: only a closed list of live actions, copy a
    verified file to a spec-named output with a
    snapshot backup, and record a notification.
    Live verification checks outputs exist and are
    non-empty, rollback restores snapshots on failure.
11. Learn: episodic memory, Experience, research report,
    and live record, with promotion to long-term memory
    that is automatic, deduplicated when identical and
    superseding when the same shape recurs.

## 🧩 Components

```mermaid
flowchart TB
    UI[🖥️ WebUI / CLI / WhatsApp / Slack]
    T[⚡ Trigger dispatcher]
    LOOP[🔁 Goal runner]
    TOOLS[🧰 Tools and plugins]
    SANDBOX[🔒 Bubblewrap, Docker, VM, or Modal]
    MEMORY[(🧠 SQLite + FAISS / Qdrant memory)]
    APPROVAL[👤 Approval gate]
    REPORT[📊 Trace, reports, metrics]
    UI --> T --> LOOP
    LOOP --> MEMORY
    LOOP --> TOOLS --> SANDBOX
    LOOP --> APPROVAL --> SANDBOX
    LOOP --> REPORT
```

- Model client: endpoint, key, and model name only,
  OpenAI-compatible chat completions, stdlib urllib,
  FakeClient for tests. No model vendor is hardcoded
  into the run logic.
- Presentation: the WebUI and conversational CLI share the same
  model/configuration path. WebUI TTS can stream Fish Audio chunks,
  while the final response footer reports elapsed time and token usage.
  These are presentation features, not a separate execution backend.
- Memory: SQLite-backed collections with FAISS dense indexes
  by default when installed; Qdrant is the automatic backend
  fallback. Plain SQLite remains the dependency-light fallback.
  Working,
  episodic, semantic, experience, and knowledge, with
  hybrid dense plus sparse recall, forgetting curve,
  grace, and explicit prune. Library files live in
  the Aidon home. SQLite also stores conversations and
  application records. Set `AIDON_MEMORY_BACKEND` to force
  `sqlite-faiss`, `qdrant`, or `sqlite`; automatic selection
  occurs at startup and does not dual-write stores. If FAISS
  fails during a run, SQLite exact search remains available. No memory
  service is hosted by Aidon.
- Tools: sandbox tools are arena plumbing, write,
  read, list, shell, and calculate, all confined to
  the workdir. Capability tools are plugins, adopted
  after a security scan and an owner decision card.
- Triggers: scheduler, WhatsApp, Slack, WebUI,
  ingest folder, webhook, and manual, all as
  TriggerEvents that start a goal run, never bypassing
  approval, spec, or verification. No Telegram source.
- Execution: bubblewrap backend, Docker backend,
  subprocess backend, VM backend over SSH to a VM
  you already run, and Modal backend running commands
  in Modal cloud sandboxes. Setup does not provision a VM;
  disposable VM per run is post-hackathon. Docker is
  disposable, workdir only mounted, non-root, all
  capabilities dropped, network off by default,
  resource limits, pinned image, no docker.sock,
  no host credentials, fail closed if Docker is missing.
- Capability hub and acquisition governor: the hub
  indexes every capability with lifecycle, health,
  availability, and verification, and triage reads it;
  ordinary skill recall is trusted-only. A per-run
  governor caps tools, skills, MCP servers, dependency
  sets, repair cycles, and acquisition seconds, with
  budgets that survive stops.
- Clef router: when `AIDON_ROUTER=clef` and
  `AIDON_CLEF_URL` is set, a decision model ranks intent
  and DAG candidates; an empty URL means no Clef
  anywhere.
- Diagnostics: rotating log, error records, and
  `aidon diagnostics <run_id>` reports. Diagnostics
  record only and never steer planning.
- Budgets and trace: token, step, and time budgets,
  enforced, with estimated tokens labeled as estimates.
  Trace events, one per stage, in run order, with no
  secrets, no tokens, and no file content values,
  written to trace files in the Aidon home.
- v1 API and MCP: external stable API at /api/v1/*,
  separate from internal WebUI /api/*, with Bearer
  token auth, owner and agent scopes, key generation
  with hash only stored in a 0600 file, and an MCP
  server wrapping the same v1 store, with tools
  filtered by scope. Agent scope cannot approve and
  cannot manage keys, in v1 or in MCP.

## 💾 Storage, in the Aidon home, never in the repo

- specs: spec JSON and Markdown sheet, per chat.
- plugins: adopted plugin directories, per name.
- skills: standalone Markdown skills, per name.
- library: library files, per item id.
- knowledge: knowledge files and manifest.
- memory.sqlite3: SQLite memory points and FAISS ID mappings.
- qdrant: optional Qdrant collections, local mode or server.
- learned: last run record, with research and live.
- trace: trace files, per run id.
- v1: key hashes and goal records, keys in 0600 file.
- live: live outputs snapshots and notifications.
- ingest: user files dropped for ingestion, by the user.

## 🛡️ Security boundaries

- Approval is for decisions, chat is for conversation.
  Questions have no clock and no card, decisions have
  a fail-closed clock.
- Secrets never in chat, never in a trace, never in a
  scan report, never in a key list, only as a hash.
- A plugin scan reports path, line, and type only,
  never values, and never an invented CVE id.
- A trigger starts work, it never approves work.
- A live action is a closed list, never an arbitrary shell.
- A budget exceeded fails a run, it does not continue it.
- A Docker container mounts only the workdir, never
  docker.sock, never host credentials, never a host home.

## 🗂️ Layout, grouped, not flat

Code lives in `src/`, grouped by what it does, so a reader can find a system without reading 70 files in one directory. `src/system/main.py` is the entry point, it imports from the groups, it does not contain the systems itself. `src/` itself contains only `__init__.py` and the group directories, no modules at the top level.

- `src/system/` — config, model client, and `main.py`, the entry point. The model client is endpoint, key, and model name only, OpenAI-compatible, stdlib, FakeClient for tests.
- `src/memory/` — SQLite + FAISS and Qdrant memory stores, embedder, entity graph, KB parsing, Experience, redaction, rolling summary, library links.
- `src/agentic/` — goal loop, DAG and DAG store, adapt, swarm, interview and spec, validation, model verifier, budgets, trace, counting, metrics, benchmark, kernel, routers, research.
- `src/interface/` — WebUI and robot avatar, messaging, v1 API, MCP server and client, triggers, scheduler loop, run control, approval and pre-approval gates.
- `src/capabilities/` — sandbox and execution backends, tools and plugins, skill lifecycle, live actions, search, voice, GitHub, email and Gmail, image generation and editing, charts, Unity assets, Playwright MCP, SAM segmentation, video pipeline.
- `src/social/` — one-way social publishing, Threads and Instagram, plus YouTube search, metadata, and transcripts.

Old flat layout, `src/*.py` with no sub-directory, is superseded by this grouped layout. Tests import from the grouped paths, for example `from src.memory.memory import Memory`, not `from src.memory import Memory`.
