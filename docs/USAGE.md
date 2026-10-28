# 📖 Usage

## 🖥️ WebUI and messaging

- WebUI: `./launch.sh` starts the WebUI server on
  `http://127.0.0.1:8765/` by default (`AIDON_WEBUI_PORT`
  overrides it; `webui serve` on its own defaults to
  port 8765), localhost only, with a chat
  thread, a left sidebar for projects and library, a
  Library tab, a settings and keys page showing hash
  prefixes only, never real keys, and approval cards
  in the UI, fail-closed on timeout. With arguments,
  `./launch.sh goal "..."` still runs the CLI.
- WhatsApp and Slack, both two-way, no Telegram,
  with the Aidon key names:
  `AIDON_WHATSAPP_TOKEN`, `AIDON_WHATSAPP_PHONE_ID`,
  `AIDON_WHATSAPP_VERIFY_TOKEN`,
  `AIDON_WHATSAPP_APP_SECRET`, `AIDON_WHATSAPP_OWNER`,
  and `SLACK_BOT_TOKEN`, plus `AIDON_SLACK_SIGNING_SECRET`,
  which is new, because old Slack was polling, not a
  webhook. WhatsApp is owner only, empty owner means
  inbound disabled, never open to everyone. Slack
  replies go to the same channel, in thread. A message
  `approve <id>` or `reject <id>` on either channel
  resolves a pending approval card, it does not become
  a goal. WhatsApp webhook at `/webhook/whatsapp`,
  with hub verification and, when an app secret is set,
  X-Hub-Signature-256 verification. Slack webhook at
  `/webhook/slack`, with HMAC signature verification
  before any event is parsed. An incoming message
  starts a DAG run through the same trigger
  dispatcher as scheduler, WebUI, ingest, webhook,
  and manual, and the reply goes back on the same
  transport, with the WhatsApp Cloud API and Slack
  chat.postMessage. Public reachability still needs a
  tunnel, localhost alone is not reachable by Meta
  or Slack, and the full WebUI must not be tunneled
  unauthenticated, use a webhook-only listener in
  front, as Aidon did.

## ✅ Pre-approvals, the same on every surface

A pre-approval lets Aidon run **reversible** steps for one
project without asking every time. It is not a blanket
auto-approve: anything that sends to a third party,
publishes, spends, deletes, installs, or adopts a plugin
always gets a card, even if it was somehow ticked.

Grant, list, and revoke from any of the four surfaces — the
WebUI chat box, the WebUI Settings page (Pre-approvals
panel), WhatsApp, or Slack — with the same words:

- `preapprove <project> <kind> [hours]` — grant, 8 hours by
  default. The kind can be a name or the number the menu
  shows, so a phone reply can be `preapprove dinner 1 8`.
- `preapprovals` — list what is active, with hours left.
- `unpreapprove <project> <kind|all>` — revoke one kind or
  everything for that project.

Kinds: `live` (reversible runs only, the final card is
skipped when the plan touches nothing but files), `spec`
(accept the spec sheet), `semantic` (promote facts to
long-term memory), `sandbox` (test steps in the isolated
workdir). That menu is the closed list: a word that is not
on it is refused with the menu, so an ordinary sentence
that happens to start with "preapprove" cannot invent a
grant.

From the CLI, the same store:

```bash
python -m src.system.main scheduler preapprove --spec dinner --kind live --hours 8
python -m src.system.main scheduler list
python -m src.system.main scheduler revoke --spec dinner --kind all
```

All four read and write one file, `<Aidon home>/preapprovals.json`,
so a grant made on your phone is the grant the WebUI panel
shows, and a CLI run or a scheduled job honours it too.

## 🔁 Core, in order

1. **Interpret** — the goal, in chat. Interview questions are
   chat turns, not cards, and they have no clock. The
   interview triggers from the chat message itself, and
   ends in a frozen spec: goal, deliverables, constraints,
   budget, deadline, recipients, files, notifications,
   assumptions, with no open questions. The spec is built
   from the questions, from memory, and from previous runs,
   then confirmed with a spec sheet card for user approval
   before any planning proceeds. The spec is verified by
   code and approved as a decision card, then it binds the
   planner. Run it with
   `python -m src.system.main interview "Plan a dinner"`, with
   `--run` to run the goal after the spec is approved.

## 📝 Spec sheet

You do not have to do the interview. Write your own spec
sheet, attach it in the chat box, and Aidon skips the
interview.

- Template: [examples/SPEC-SHEET-TEMPLATE.md](examples/SPEC-SHEET-TEMPLATE.md)
  in this repo, and `specs/template.md` in your Aidon home
  (`~/.aidon/specs/template.md` by default). Copy it, fill
  it in, save it, and attach it. The template contains no
  amounts, values, or example constraints, only one or a
  few sentences per section telling you what to write.
- Example: [examples/SPEC-SHEET-EXAMPLE.md](examples/SPEC-SHEET-EXAMPLE.md),
  a filled spec sheet for generating October 2026 monthly
  statements from invoice accounts, with customer totals,
  a pay by date, and email to customers. The example is
  fictional, for illustration only.
- Standard format: Markdown, human readable. Goal,
  Deliverables, Constraints, Budget, Deadline, Recipients,
  Files, Notifications, Assumptions, Open questions, in
  Markdown headings with bullet lists. Plain `Goal: ...`
  lines and JSON are also accepted on attach, but the
  sheet itself, the template, the stored file, and the
  approval card are all the same Markdown sheet, not a
  JSON dump. Budget is written in dollars and cents, or
  as cents. Empty sections read `None`, not `-`.
- Attached sheets are parsed, verified by code, and saved
  as `specs/<project>.json` and `specs/<project>.md`.
  An unverified sheet is not approved, and the run does
  not proceed. A verified sheet is confirmed with a spec
  sheet card, then it binds the planner, same as an
  interviewed spec.
- CLI: `python -m src.system.main interview "Plan a dinner" --attach my-spec.md --run`,
  or `python -m src.system.main goal --spec my-spec.md "Plan a dinner"`.
2. **Plan** — a plan with steps, deliverables, and criteria.
   Steps are tool calls, not strings: write, read, list,
   shell in the sandbox, and calculate, all confined to the
   workdir, with calculation by AST, never eval, plus any
   adopted plugin tools, and any skills as planner context.

## 🧰 Tool, skill, and plugin system

Aidon builds his own capability tools by climbing, this
module is the arena around that climb, not the tools
themselves. A tool is code, a skill is Markdown
instructions, a plugin is a bundle of both, with a
plugin.json manifest, living in `~/.aidon/plugins/<name>/`,
never in the repo.

- Adopt: `python -m src.system.main plugin scan <dir>`,
  then `python -m src.system.main plugin adopt <dir>`, then
  `python -m src.system.main plugin list`. Adoption is scan,
  then a decision card, then copy, in that order.
- Scan: path, line, and type only, never values, never
  an invented CVE. Blocking findings reject adoption
  even if the owner would approve: eval, exec, subprocess
  or shell, socket or network calls, and a secret
  assigned to a string value. Warnings, shown on the
  adoption card, are file writes in plugin code and
  dependencies that are `*`, `latest`, or unpinned.
- A plugin tool is pure: it takes args and returns text.
  It does not get the sandbox, the home, or a network
  client. File and network work stays with the sandbox
  tools and with later, explicitly approved capability
  plugins.
- Standalone skills, Markdown only, live in
  `~/.aidon/skills/<name>.md` and are passed to the
  planner as context, never executed as code.

## ⚡ Triggers: what starts a DAG run

Scheduler, message, WebUI, and the ingest folder are
all triggers, and they are all the same shape. A
trigger produces a TriggerEvent, the dispatcher hands
it to a run handler, the handler builds a spec and a
plan, and the approval gate still applies before any
live action. A trigger starts work, it never bypasses
approval, spec, or verification.

- Scheduler: a time arrives, for example 3:00pm on the
  first Wednesday of the month. Schedules supported:
  daily, weekly, monthly first weekday, monthly day,
  and simple cron. Jobs live in `~/.aidon/scheduler.json`.
- WhatsApp and Slack, both two-way: an incoming message
  starts a run, and the reply goes back on the same
  transport. No Telegram, by owner decision.
- WebUI: a message, a button, or an upload in the WebUI.
  The WebUI is built and calls the same dispatcher,
- Ingest folder: a new or changed file in the user's
  own ingest path, `~/.aidon/ingest` by default, or
  `AIDON_INGEST_DIR` if set. The watcher reports new
  files only, it does not move or delete user files.
- Webhook and manual: n8n-style triggers. Webhook is an
  HTTP POST, manual is a WebUI button or a CLI run.
  CLI: `python -m src.system.main trigger list`,
  `trigger ingest`, `trigger schedule`, and
  `trigger webhook <name> --goal "..."`.
- n8n mapping: Schedule to scheduler, Webhook to
  webhook, Manual to manual, File watcher to ingest,
  Message to WhatsApp or Slack. Email, RSS, and database
  triggers are not core triggers, they are plugins later,
  if a task needs them.

## 🔎 Real research, live execution, and incremental improving

Yes, Aidon can improve incrementally, and it does so
by recording what actually happened, not by re-planning
from scratch each time.

- Real research: a fact is verified only if a source
  URL, fetched with stdlib urllib, contains it, with
  the source and excerpt recorded. A model claim with
  no source excerpt stays unverified. Research runs
  outside the sandbox, which has network off by default.
- Live execution: after sandbox verification and owner
  approval, only a closed list of live actions runs:
  copy a verified file to a spec-named output path,
  with a snapshot backup first, and record a
  notification. No live shell, no live network, no
  live delete, no overwrite without a snapshot.
- Live verification: every live output must exist and
  be non-empty, and a ledger total must match the
  sandbox total. If live verification fails, rollback
  restores every snapshot, and the run is failed, not done.
- Incremental improving: each successful run records
  its research report and its live record in learned
  and in its Experience lesson, so the next run for a
  similar goal starts with the previous sources,
  verified facts, unverified claims, live outputs, and
  live failures, and only re-researches and re-tests
  what is missing or stale. Repair, experience reuse,
  and this lesson record together are the improvement
  loop, not a separate feature.

Demo capability tools themselves, dinner PDF, grocery
app, store run, and job search plus publish, are still
Aidon climbing work, through plugins, not arena code.
What is built here is the research, live, and
verification arena they will run in.

## 📊 Budgets, trace, v1 API, token auth, and MCP

- Budgets: 200,000 tokens estimated, 50 steps, 3,600
  seconds per run, by default, in config. A budget
  exceeded fails a run, it does not continue it.
  Token counts are estimates, about one token per
  four characters, unless a model client returns a
  real usage count.
- Trace: events in run order, interpret, research,
  recall, plan, sandbox, verify, approval, live, and
  learn, with no secrets and no file content values,
  written to `~/.aidon/trace/<run_id>.jsonl`.
- v1 API: `python -m src.system.main v1 serve --port 8001`,
  localhost only, at `/api/v1/`, with Bearer token
  auth, except health. Owner scope is full, agent
  scope is submit, read, steer, and cancel only for
  its own goals, never approve, never key management.
- Token auth: `python -m src.system.main v1 keygen --name owner --scope owner`,
  key shown once, only its SHA256 hash stored, in a
  0600 file, never shown again, only a hash prefix
  in key lists.
- MCP: `python -m src.system.main v1 mcp`, stdio JSON-RPC,
  token from `AIDON_MCP_TOKEN`, tools filtered by
  scope, wrapping the same v1 store as the v1 API.
  Streamable HTTP MCP is built: JSON-RPC over HTTP at
  `/mcp`, localhost only, Bearer token required.
3. **Sandbox** — the plan is tested in an isolated workdir.
   Bubblewrap, when installed, is the isolated backend. A
   subprocess fallback is labeled as not isolated, never as
   isolated. Docker is a second backend, for build, install,
   and test work: disposable container, only the workdir
   mounted, non-root, all capabilities dropped, network
   off by default, resource limits, pinned image, no
   docker.sock, no host credentials, and no silent
   fallback to a host shell if Docker is missing. The
   backend is chosen by the owner in config, never by the
   model. A full VM is post-hackathon, future only, not
   built here. Set `AIDON_EXECUTION_BACKEND=docker` to
   use Docker, `AIDON_DOCKER_IMAGE` for the pinned image,
   `AIDON_DOCKER_NETWORK=1` only when a task needs network.
4. **Verify** — a result is verified by evidence, not by
   assertion. Deliverables must exist and be non-empty,
   criteria are checked by code, and a ledger total is
   recomputed from its lines in cents. A vague criterion
   is reported unverified, never passed.
5. **Repair** — if a step or a check fails, the loop asks
   for a repaired plan, with passed deliverables listed
   as do-not-rewrite. Passed files are untouched, only
   failed steps are fixed, at most 3 attempts in total.
6. **Approve** — decisions are cards, and they fail closed on
   timeout. No answer means rejected, never approved by default.
   An external agent can submit and read, but it cannot approve
   on the owner's behalf.
7. **Execute** — only what was approved, and only after it.
8. **Learn** — what worked is recorded, so the next goal can
   reuse it. Memory starts blank, by design: new collections,
   no import from previous projects.

**KB parsers, future auto-install:** when Aidon detects a
file it cannot read, for example CAD or LaTeX, it records
the parser needed in the KB point and manifest, and keeps
the file in knowledge/, without inventing text. In the
future, Aidon climbs to that parser itself, research,
draft, sandbox-test, and adopt with owner approval, like
any capability tool. Parsers for CAD, LaTeX, and other
formats are not preinstalled.

## 🧠 Memory and KB, Qdrant for all

One database, Qdrant, no second database. One instance, local
mode under the Aidon home by default, or a server URL if
`AIDON_QDRANT_URL` is set, with separate collections.
Memory starts blank: no import from previous projects,
collections start empty, the first runs build memory from
scratch.

- **Working memory (STM):** chat history, last 20 exchanges,
  `working` collection. Never promoted on its own.
- **Episodic memory:** project-specific, from interview
  questions and task data, scoped by project, `episodic`
  collection.
- **Semantic memory (LTM, global):** facts, `semantic`
  collection.
- **Procedural memory (LTM, global):** Experiences, see below,
  `experience` collection.
- **Knowledge (KB, RAG):** ingested files, `knowledge`
  collection.

**Promotion, episodic to LTM:** at the end of a completed run,
Aidon may ask whether to put that run's episodic memories into
LTM. If `AIDON_AUTO_PROMOTE_TO_LTM=1`, promotion happens
automatically, otherwise it asks, and a "no" leaves episodic
memories project-scoped only.

**Embedder, production:** EmbeddingGemma 2, `google/embeddinggemma-2`,
440M: text plus vision, audio omitted, 768-dim, local via
sentence-transformers >= 6.1, bfloat16 or float32, never
float16. Documents use the `Document` prompt, queries use
`SearchQuery`. HashEmbedder is tests and framework proof only,
it is not semantic. Vector size must match the embedder: a
collection created at 128-dim cannot hold 768-dim vectors, so
changing embedders means a fresh home or a recreated
collection, never a silent resize.
**Recall, hybrid:** dense plus sparse, fused with RRF,
over-fetching 2x, in Qdrant with named vectors `dense` and
`sparse`, and the same RRF shape in the in-memory fallback.
Sparse is SPLADE when `AIDON_SPARSE_MODEL` is set and the
optional `sparse` extra is installed; otherwise it falls back
to keyword sparse weights with no extra download. `recall_both` embeds once
and recalls episodic plus knowledge. A recall is an access:
access count, last accessed, and distinct access days are
updated, and a deterministic forgetting curve, 21-day
half-life, 35-day grace, 0.02 prune threshold, finds
cleanup candidates. Prune is explicit only, never automatic.
**Projects and conversations:** project and conversation ids
are payload fields on working and episodic points, read back
from Qdrant by filtered scroll, oldest first, so a
conversation survives a restart without a second database.
**KB parsing:** text and markdown as UTF-8, HTML stripped,
docx via python-docx, PDF via pypdf, with a UTF-8 fallback
that never crashes an ingest and never invents text.
**Experience:** goal shape is sorted unique key tokens,
recall re-ranks by shape overlap plus vector score, and
store merges on save when shape overlap is at least 0.5
and a tool is shared, unioning DAG nodes by deliverable.

**Library, images and outputs:** image inputs, generated
images, charts, and other outputs are copied into the Aidon
home, at `~/.aidon/library/files/<id>/<name>` by default, or
`AIDON_HOME/library/files/<id>/<name>`. They are never stored
in the repository and never sent to a central service. The
memory point for that exchange, episodic run, or promoted LTM
fact carries the link in its payload: library_id,
library_path, library_url (`/api/library/<id>/file`), and
kind. Recalling the text therefore returns the image link,
so the caller can show or open the image alongside the text.
Charts carry their data file in the same library item, because
a vector cannot recover exact numbers from a picture.
The WebUI Library tab, when the WebUI is built, follows the
earlier design: left sidebar, Library view, thumbnails for
images, a memory_linked flag, click to open, delete to remove,
and file serving confined to `home/library`, never an
arbitrary path.

**KB:** dump files into `knowledge/` under the Aidon home, or
import them in the WebUI, which duplicates them into
`knowledge/` first, then auto-ingests them: chunk, embed, and
upsert to the knowledge collection for RAG. Images are one
knowledge point, not text chunks: image vector plus file name
plus knowledge path, so text recall can refer to them.
Scanning `knowledge/` ingests new or changed files and prunes
chunks for deleted files, without deleting your files.

**Experience:** a successful run's workflow, with its DAG
nodes, tools, and skills, stored in the experience collection.
Aidon builds an Experience after the first successful run,
and after every successful run after that. A failed or
rejected run does not become an Experience. On a new goal,
Aidon recalls Experiences first, and if one matches, its DAG
is the starting plan, verified in the sandbox like any other
plan. This is procedural memory, unified, not a second store.
It is what the earlier design called Knowledge.

Forget, recall, list, prune, and one-hop knowledge-graph
expansion (payload `links`) follow the same shape in every
collection.

## 🆕 New modules, built since the first roadmap

- Adaptive DAG, steer and cancel, 3D factory, run
  replay, 9-stage pills, VRM avatar viewer, DAG
  editor with merge and memory DAGs, entity graph.
- Semantic routing, webchat fast path, vector cache,
  Unity assets, Playwright MCP, Muse Image, code
  charts, fast model for local models.
- Metrics truth grading, elapsed excluding approval
  wait, Gmail inbox read, mission frozen snapshots,
  skill lifecycle and governor, cross-file validator,
  code repair, multi-agent research, model rolling
  summary, redaction at record time, pre-approval
  live gate, WhatsApp media to vision, v1 and MCP
  goal execution, CLI swarm.
- WebUI token auth: set AIDON_WEBUI_TOKEN in .env
  to require Bearer token for WebUI, localhost
  alone needs no token, remote without a token
  is denied.
- Post-hackathon, not built, not demoed: VM backend,
  Unity factory, procedural 3D assets, local server
  manager, stronger sandbox, dedicated live toolset,
  and SAM 3.1 segmentation, see [ROADMAP.md](ROADMAP.md).

