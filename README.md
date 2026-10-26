# Aidon

An autonomous agent that works from a plain-language goal.

The user gives a goal in chat. Aidon researches, plans, tests the
plan in a sandbox, verifies the result against evidence, asks
the human for approval before any live action, executes only
what was approved, and then records what it learned so the next
goal can reuse it.

## What is new here

The idea was validated with an earlier prototype, and all code
in this repository was written during the Meta Global AI
Developer Hackathon, Oct 26 - Nov 4, 2026. Nothing in this
repository was copied from that prototype: the design was read,
and the code was written fresh, part by part, and tested as it
was written.

The demo runs on Meta models through the Meta Model API. That
is a real part of what Aidon runs, not just a build-time choice.

## Run it

```sh
cp examples/.env.example .env
# Fill in your own endpoint, key, and model name in .env
python -m src.main goal "Plan a dinner for two"
```

Configuration is just an endpoint, a key, and a model name:

- `AIDON_API_BASE` — endpoint URL for the model API
- `AIDON_API_KEY` — API key for that endpoint
- `AIDON_MODEL` — model name at that endpoint
- `AIDON_HOME` — where Aidon keeps its state (default `~/.aidon`)

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

Layout: the code lives in `src/` only, no sub-directory, so
`pip install -e .` installs it and
`python -m src.main goal "..."` runs it.

Aidon is self-hosted by design: clone it to your own computer
or server, run it there, and use the WebUI or call the API.
There is no central Aidon cloud, and your data is not sent to
one. Your memory, workdirs, and keys stay on your machine,
under your Aidon home, outside this repository.

## Memory and KB, Qdrant for all

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
Sparse is SPLADE when `AIDON_SPARSE_MODEL` is set, otherwise
keyword sparse, no extra download. `recall_both` embeds once
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

## Core, in order

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
   `python -m src.main interview "Plan a dinner"`, with
   `--run` to run the goal after the spec is approved.

## Spec sheet

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
- CLI: `python -m src.main interview "Plan a dinner" --attach my-spec.md --run`,
  or `python -m src.main goal --spec my-spec.md "Plan a dinner"`.
2. **Plan** — a plan with steps, deliverables, and criteria.
   Steps are tool calls, not strings: write, read, list,
   shell in the sandbox, and calculate, all confined to the
   workdir, with calculation by AST, never eval, plus any
   adopted plugin tools, and any skills as planner context.

## Tool, skill, and plugin system

Aidon builds his own capability tools by climbing, this
module is the arena around that climb, not the tools
themselves. A tool is code, a skill is Markdown
instructions, a plugin is a bundle of both, with a
plugin.json manifest, living in `~/.aidon/plugins/<name>/`,
never in the repo.

- Adopt: `python -m src.main plugin scan <dir>`,
  then `python -m src.main plugin adopt <dir>`, then
  `python -m src.main plugin list`. Adoption is scan,
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
