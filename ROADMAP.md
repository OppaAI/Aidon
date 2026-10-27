# Roadmap

Status, as of the current head: built, in the repo,
tested, and verified by API read-back, unless a line
says otherwise. Post-hackathon items are marked
post-hackathon, NOT built, NOT demoed, at the end.

## Built

- Skeleton: config, generic model client, approval gate,
  sandbox, goal loop, CLI.
- Memory: Qdrant for all, hybrid recall, forgetting
  curve, promotion, library, EmbeddingGemma 2, KB
  parsing, parser needed recording for CAD, LaTeX, and
  other formats not yet readable.
- Plan execution: sandbox tools, verifier by code,
  repair only failed steps, max 3 attempts.
- Interview to verified spec: chat questions, spec
  sheet in Markdown, template, example, attach to skip,
  prefill from memory and previous runs, spec sheet card.
- Tool, skill, and plugin system: loader, registry,
  security scan, adoption gate, CLI plugin commands.
- Execution backends: bubblewrap sandbox, Docker
  backend, VM documented only, not implemented.
- Triggers: scheduler, WhatsApp and Slack trigger
  interfaces, WebUI trigger interface, ingest folder,
  webhook, manual, no Telegram.
- Setup and launch: setup.sh and launch.sh, idempotent
  setup, pass-through launch, no auto Docker or VM install.
- Real research, live execution, live verification,
  and incremental improving: source-verified facts,
  closed live actions with snapshot rollback, research
  and live records in Experience lessons.
- Budgets, trace events, v1 API, token auth, and MCP:
  token, step, and time budgets, trace events in run
  order, v1 API with Bearer token and owner and agent
  scopes, key generation with hash only stored, MCP
  stdio server wrapping the same v1 store, tools
  filtered by scope.
- WebUI server: chat thread, left sidebar, approval
  cards in UI, settings and keys page with hash
  prefixes only, Library tab, localhost only, with
  launch.sh starting the WebUI by default.
- WhatsApp and Slack transports, both two-way, no
  Telegram: WhatsApp webhook with hub verification
  and app-secret signature verification, owner only,
  Slack webhook with HMAC signature verification
  before parsing, both dispatching through the same
  trigger dispatcher, replies back on the same
  transport, with real WhatsApp Cloud API and Slack
  chat.postMessage senders, using the old Aidon key
  names, and approve / reject by message. Real
  sending needs your own tokens, not included here,
  and not tested without those accounts, and public
  reachability still needs a tunnel.

- Old capability tools, rebuilt as arena modules,
  with fake-API tests, live keys not included:
  search (Tavily / SearXNG), YouTube search +
  metadata, Fish Audio TTS + Muse Voice Transcribe,
  Threads and Instagram publishing, approval-gated,
  GitHub search + fetch, scheduler loop, daily
  report, approval nudges, autonomy graduation,
  and email drafts with the old corrected-subject
  policy. Not built here: YouTube transcript path,
  Gmail MCP transport, live model routing, swarm,
  kernel mode, and semantic routing, see below.

## Not built yet, in the repo, for the hackathon

- Demo capability tools end to end: dinner menu PDF
  plus grocery app, store run, and job search plus
  publish, through plugins Aidon climbs to.
- Live verification on a real model and a real
  Qdrant and EmbeddingGemma 2 run: all runs so far
  in the new repo are FakeClient tests, plus a local
  HTTP research test. A real Muse Spark run and a
  real Qdrant run are still owed.
- Demo video, 3 minutes, required for submission.

## Built since the earlier roadmap, now in the repo

- Adaptive DAG, steer and cancel across WebUI,
  WhatsApp, Slack, CLI, v1, and MCP, with RunControl.
- WebUI avatar and mic, DAG editor, entity graph,
  3D factory, run replay, 9-stage pills, VRM avatar
  viewer with SVG fallback, Meta Transcribe via
  /api/transcribe when a key is set, library upload,
  settings edit with an allowlist, projects history,
  WebUI token auth for remote access, and DAG merge
  on save plus memory DAGs in the editor.
- Semantic routing in CLI, WebUI, and interview,
  with a write-intent guard, a webchat fast path
  from researcher facts, and a vector cache at
  <home>/semantic_router_vectors.json, pre-made on
  first run, reused after, ignored if the embedder
  or example lists change.
- Unity asset pipeline, Playwright MCP wiring,
  Muse Image generation and editing with 1 to 10
  reference images, code charts and graphs, and
  a fast model via AIDON_FAST_MODEL, same endpoint,
  different model name, for local small models.
- Metrics truth grading, elapsed excluding approval
  wait, Gmail inbox read, mission frozen snapshots
  ready for review, skill lifecycle and governor,
  cross-file validator, code-level repair with
  rollback, multi-agent research, model-written
  rolling summary with code fallback, automatic
  redaction at record time, pre-approval live gate,
  WhatsApp media to vision preparation, extra loop
  tools with approval for post and send, full
  runners in scheduler, ingest, and Slack polling,
  v1 and MCP actually running a goal, owner only,
  and CLI swarm with --swarm.
- Streamable HTTP MCP, cross-file validator, and
  image generation, all built now, no longer
  post-hackathon.
- SAM 3.1 segmentation, built now for demo video
  editing: segment and track objects in images
  and video frames, boxes, confidences, and mask
  metadata, via the Meta Model API Responses API.
- Video pipeline, built now, src/capabilities/video.py, CLI
  video edit: ffmpeg extracts frames, SAM segments
  each frame, PIL annotates boxes on frames, ffmpeg
  re-encodes to a new video. Box overlay, not
  pixel-mask compositing, mask decoding needs the
  optional meta-sam-parser package. ffmpeg is an
  external tool, checked at runtime, never faked.
- Daily report fixed: loop writes status and
  project_id to learned/last_run.json, report falls
  back to goal for old files, never empty.
- v1 serve and MCP wired in main: v1 run and MCP
  run_goal now run through the same GoalRunner as
  a CLI goal, approval asked on the server console,
  no longer 501 or execution not configured.

- Missing old-Aidon things, built now: CLI memory,
  skills, tools, dag, mission, outbox, export,
  schedule, sandbox check, chain, chat, and
  computer, plus WebUI tabs capabilities, memory,
  knowledge, and reports, plus computer use and
  screenshot as a tool layer, approval-gated, with
  no GUI action without a driver, and chain
  validation and outbox with failed sends kept
  queued. Screenshot and computer use as a full
  live desktop tool are still post-hackathon below,
  this is the tool layer, not a live GUI driver.

## Post-hackathon, NOT built, NOT demoed

- Full VM backend: disposable VM running Docker
  inside it, for untrusted code and separate-kernel work.
- Unity factory: a full Unity factory, editor
  install, project, and build, beyond the Unity
  asset pipeline built now, and beyond the Three.js
  factory built now.
- Procedural 3D game-asset generation, beyond the
  cube, sphere, and cylinder primitives built now.
- Dedicated live toolset distinct from sandbox tools,
  beyond the closed live actions built now.
- Stronger sandbox backend spike, beyond bubblewrap
  and Docker.
- Local server manager: start, stop, and download
  for local models, beyond the generic endpoint plus
  model name client built now, which already works
  with a local endpoint.
- Email, RSS, and database triggers, as plugins,
  beyond the core triggers built now.

- Screenshot, post-hackathon full live use, NOT built as a desktop
  tool yet, tool layer built above: capture the screen, not just a browser
  page, so Aidon can see what the user sees, with
  the user's permission, and with the screenshot
  treated as private data, never sent outside the
  machine without approval. Browser page screenshot
  already exists via Playwright MCP, desktop
  screenshot does not, and is not claimed here.
- Computer use, post-hackathon full live use, NOT built yet:
  mouse, keyboard, click, type, scroll, and app
  control on the user's own computer, driven by
  screenshots and vision, with every irreversible
  action behind the approval gate, and with a
  stop that actually stops. No GUI control is
  claimed here, and no mouse is moved in a
  headless run, because there is no display to
  move it on.

- Aidon fly-brain biological layer, semi-joke, last entry,
  as a joke, not a product direction, not demoed as
  sentience, not a real claim: the MaleCNS one, the
  male Drosophila central nervous system connectome,
  as a biological layer, with sentient emergence and
  reinforcement learning and decision, and all the
  good stuffs, as a joke. In Aiko's real fly work,
  MaleCNS v1.0 is about 166,700 neurons, brain plus
  optic lobes plus ventral nerve cord, with mushroom
  body, central complex, and other circuits used as
  modulatory subsystems, approach and avoid,
  curiosity, urgency, and so on, not a conscious
  agent, and RLHF is human feedback, not fly
  circuits. This entry is the joke version of that,
  for later, if anyone wants a toy reactive layer,
  and it would never be presented as Aidon becoming
  sentient, or as Aidon turning into a bug.
