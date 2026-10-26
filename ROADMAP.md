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

## Post-hackathon, NOT built, NOT demoed

- Full VM backend: disposable VM running Docker
  inside it, for untrusted code and separate-kernel work.
- Procedural 3D game-asset generation.
- Dedicated live toolset distinct from sandbox tools,
  beyond the closed live actions built now.
- Stronger sandbox backend spike, beyond bubblewrap
  and Docker.
- Deterministic plan validation and cross-file validator.
- Streamable HTTP MCP, beyond stdio MCP built now.
- Email, RSS, and database triggers, as plugins,
  beyond the core triggers built now.
- Voice transcription and image generation tools,
  beyond the model selection already decided.
