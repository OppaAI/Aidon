# Reliability and production review — 2026-11-01

This is a repository-wide automated review with targeted manual inspection,
confirmed repairs, and runtime checks on the owner's Docker host. It is not a
claim that every path is bug-free or that deployment is production-certified.
This review supersedes the current-status counts in the October 31 documents;
those documents retain their historical evidence. The owner has since confirmed
that Slack and WhatsApp are responding in the deployed Docker instance. That is
useful live evidence for basic messaging, but does not certify end-to-end goal
completion or recovery.

## Confirmed problems repaired

| Priority | Problem | Resulting behavior and evidence |
| --- | --- | --- |
| P1 | Legacy Slack webhooks overrode a working bot token, so stale hooks could cause HTTP 404 replies while polling worked. Webhook success was also parsed as JSON despite returning plain `ok`. | Bot replies use the Web API and preserve thread IDs; webhook-only sending accepts the documented plain response. Transport regressions are in `test_wiring.py`. |
| P1 | Slack polling waited for a goal to finish, preventing the same poller from reading the approval or answer that the goal needed. The polling path omitted mode, memory, question and attachment handling. | Authenticated polling and webhook messages use shared intake. Goals enter a SQLite inbox before history acknowledgement and execute on a separate worker; commands remain available. Signed webhook goal receipt returns before goal execution and duplicate message IDs are deduplicated. |
| P1 | Failed replies were lost after incoming messages were acknowledged. The cursor advanced even when handling raised. | Slack and WhatsApp reply queues persist before sending and retry without executing the goal again. Incoming history only advances after successful handling or durable queueing. Interrupted goals are retained for owner review rather than automatically replaying side effects. |
| P1 | Approval IDs restarted at `card-1`, so an old reply could approve a new, unrelated card. Resolved verdicts could also be changed. | Card IDs are random across restarts, decisions are synchronized, and resolved cards refuse later changes. Slack receives its approval card immediately. |
| P1 | Run-control read/modify/write operations could lose cancellation or steering notes during concurrent progress updates. Cancelling one run rejected every run's cards. | Writes are atomic and synchronized across threads and processes. Cancellation is retained, unreadable existing control state stops work, and approval cancellation is scoped by run. A three-process regression verifies all notes and cancellation survive. |
| P1 | The localhost guard accepted any string beginning `127.`, including an attacker-controlled hostname. | Only parsed loopback IP addresses and explicit localhost names pass. Regressions reject `127.attacker.example` and related hostnames. |
| P2 | Slack history ignored pagination and compared timestamps lexically. A new or corrupt cursor could replay historical goals. | History pages are collected before acknowledgement, timestamps are ordered numerically, attachment metadata is retained, and rate-limit retry delays are respected. New channels begin at startup time; corrupt cursors stop polling without overwriting evidence. |
| P2 | The standalone scheduler passed the whole comma-separated Slack channel setting as one channel ID and could poll the same history as the WebUI. | Channel IDs are parsed individually with per-channel cursors; the scheduler defers history polling to the WebUI when its poller is enabled. A regression covers whitespace and duplicate IDs. |
| P1 | Semantic mode detection could stop a scheduled goal at Plan-ready, after which the scheduler acknowledged the one-shot occurrence without executing it. | Scheduled goals now force Execute mode. A regression verifies that a semantic Plan suggestion cannot pause an explicitly executing run; the target Docker smoke test then produced `/data/workspace/scheduler-smoke.txt` containing `scheduler-ok`. |
| P2 | Messaging events lacked consistent conversation identities and dropped useful prior context. | Slack and WhatsApp carry stable IDs into memory, workflow artifacts and conversation context. Current input is excluded from history only when it is actually the last recorded turn. |
| P2 | Malformed plan IDs, tools, deliverables or dependencies could crash validation; duplicate IDs were accepted; recursive cycle checks overflowed on long DAGs. Corrupt checkpoint field types could crash restore. | Validation reports failures for bad types and duplicate IDs, uses iterative cycle detection, and ignores malformed checkpoints. |
| P2 | Docker omitted the operating manual. Installed wheels could not reliably locate it or owner profiles. Modal dependencies bypassed the lockfile. | Wheels include `share/aidon/AGENTS.md`, Docker includes the manual, owner documents load from `AIDON_HOME`, and Modal plus its dependencies are resolved in `uv.lock`. |
| P2 | Corrupt notification JSON was treated as an empty queue and overwritten. The registry-backed outbox marked explicit sender failures as delivered. | Corrupt queues are preserved and rejected; explicit failures stay pending. Queue writes flush before replacement and idle drains avoid unnecessary disk writes. |
| P2 | Active chat Markdown did not render ordinary HTTP or relative lab links correctly and inserted control characters. | Source, artifact and lab links render; executable URL schemes remain rejected; code/table rendering is covered by Node-executed regressions. |
| P2 | CI's `find -exec node --check` command could exit successfully after a failed JavaScript check. | JavaScript failures now propagate through `xargs`. CI and releases also check Python undefined names and syntax; a scheduled/PR dependency audit covers locked Docker runtime extras. |
| P3 | Fallback memory ranking duplicated the same algorithm and sorted full collections for a small result set. SQLite silently capped iteration/search at 100,000 points. Several imports and assignments were unused. | One shared top-k ranker preserves full-sort results and tie order. Complete iteration no longer has the arbitrary cap; payload updates are synchronized. Verified unused imports/locals were removed while compatibility re-exports remain. |

## Verification record

Final test counts, image activation and runtime evidence are recorded in
`PRODUCTION_READINESS.md`. Checks performed during this review include:

- Repository-wide Python compilation and Ruff correctness rules
  `F821,F822,F823,E9` across `src` and `scripts`.
- JavaScript syntax checks, shell syntax checks, wheel build, and verification
  that the wheel contains the operating manual.
- Focused failure-path tests and the complete pytest suite with localhost
  sockets enabled. The restricted execution sandbox cannot run the HTTP tests
  accurately; the unrestricted localhost run is the relevant suite result.
- Dependency vulnerability audit of the frozen Docker runtime extras
  (`embed`, `sparse`, `vision`, `modal`), without installing those runtime
  dependencies into the audit environment.
- The real CLI WebUI factory is covered by a regression that verifies WebUI,
  WhatsApp, and Slack register the same goal handler; fast-model routing has
  focused router tests. These checks do not replace a live configured-provider
  smoke test.
- Scheduler, cron, dispatcher-registration, Slack wiring, outbox, and sandbox
  regressions passed locally; Bubblewrap-only cases skipped because this runner
  cannot create the required namespace. On the target Docker host, standalone
  scheduler startup and dispatch were confirmed by a scheduled run that created
  `/data/workspace/scheduler-smoke.txt` with the expected contents. A second
  target-host check observed a persisted pending claim; after the scheduler was
  restarted, it reclaimed and completed the job. Its artifact contained
  `scheduler-ok` when the goal required `recovered-ok`, so scheduler recovery
  was confirmed but goal correctness was not. The trace shows the plan omitted
  acceptance criteria and the verifier accepted any non-empty file. A local
  code change now derives an exact-content criterion from explicit requests
  such as "containing exactly recovered-ok," including when the planner omits
  the explicitly requested filename. Focused tests passed, and after rebuilding
  the image, a target-host scheduled run reported done with the requested file;
  the owner confirmed it contained the exact requested sentinel. That run also
  produced an extra `exact-content.txt` artifact. A direct call through the
  adopted plugin registry and configured Modal backend returned `wrote` for a
  marker under `/tmp`, while that marker was absent from the Aidon container.
  This confirms the tested Modal plugin filesystem boundary; it is not a general
  security audit and does not test Bubblewrap.
- Read-only live Slack `auth.test` and `conversations.history` probes: the bot
  token and both configured conversations succeeded. No synthetic chat message
  was posted during these checks.

No substantive, identical function bodies of 15+ lines were found by an AST
comparison; targeted inspection nevertheless found and removed the duplicated
memory-ranking algorithm. The two notification outboxes have different storage
and caller contracts and were repaired without an unreviewed consolidation.

## Remaining work, in priority order

1. **Target-machine runtime checks.** Standalone scheduler startup, dispatch,
   pending-claim recovery, and a Modal plugin filesystem-isolation probe passed
   Docker-host checks. The probe only confirms that this plugin's `/tmp` write
   was absent from the Aidon container; it does not cover every escape path.
   Bubblewrap isolation remains unverified because the host disallows
   unprivileged namespaces. Also verify browser refresh/recovery and configured
   model failures, recording the actual backend and evidence.
2. **Full dispatcher wiring and workflow acceptance.** Basic Slack and WhatsApp
   message/reply behavior has been confirmed on the deployed instance. Focused
   local tests passed (51) across the actual CLI WebUI factory, registration of
   the shared WebUI/WhatsApp/Slack handler, trigger dispatch, and fast/strong
   model routing. The configured-provider/live composition is still unverified;
   the broader integration selection also hit this runner's socket restriction
   (7 socket-dependent tests could not start). On the Docker host, exercise a
   harmless goal from each of the three sources with the configured fast model,
   and separately complete a harmless
   goal through planning, approval, execution, and verified result delivery.
   The first target WebUI acceptance attempt is not complete: for a request to
   create `dispatcher-webui-check.txt`, Aidon stopped at Plan ready but proposed
   `exact-dispatcher.txt` and `dispatcher_test.py` instead. The owner has not
   proceeded with that mismatched plan; repeat with a clean conversation and
   require the requested filename before approval. A second attempt dropped
   the date suffix from an explicitly named filename. A deterministic plan
   check and regression now reject this mismatch before a plan can be shown
   for approval; rebuild the Docker image and confirm the repaired plan retains
   the exact requested filename before proceeding. After the rebuild, the
   WebUI retry proposed `dispatcher-webui-check-20261101.txt` exactly, ran the
   plan, and reported the file read back with the requested contents. The
   owner confirmed `webui-dispatch-ok`; the WebUI goal-to-verified-artifact leg
   now passes on the target host. Slack likewise accepted a top-level mention,
   planned and completed the goal, and reported the requested artifact, but the
   Slack file upload failed with `invalid_arguments`. Its plan also included two
   unrelated earlier artifacts. Validation now rejects extra deliverables when
   a goal requests exactly one named file. On the latest retry the plan had one
   deliverable and the artifact contents were read back correctly. A direct
   transport probe localized the rejection to `files.getUploadURLExternal`
   (`invalid_arguments`), before any bytes were uploaded. Its request now uses
   documented form fields, as does completion; upload bytes remain raw
   `application/octet-stream`. After rebuilding, the owner uploaded the same
   verified artifact through `send_slack_file` successfully. An earlier
   automatic retry still failed with generic `invalid_arguments`; after the
   services were rebuilt, the next Slack-triggered run completed and its
   `dispatcher-slack-check-retry-20261101_5.txt` artifact appeared in the
   channel with the expected contents. Direct and automatic Slack file delivery
   are verified. The owner asked for the upload to appear as a paperclip-style
   file in Aidon's thread; file completion now receives the originating
   thread timestamp. Focused tests cover form requests and thread placement;
   verify the thread placement on the target container after rebuilding.
   Scheduler startup, dispatch, pending-claim recovery, and exact-content
   verification now passed target-host checks. The exact-content regression
   first exposed a false success with `exact_test.py`; after rebuilding, a new
   run reported done with the explicitly requested file, whose contents the
   owner confirmed matched the sentinel. It also produced an extra file, which
   should be checked for user-facing clutter. This remains separate from
   scheduler claim recovery.
   Approval ID handling, bare approval commands, and Slack thread replies have
   fixes, but their complete end-to-end cycle is not yet established. This is
   the core workflow investigation to return to after the other checks;
   transport is not currently the reported blocker.
3. **Shared-home concurrency contract.** Scheduler JSON state assumes one
   scheduler process. The inbox worker, reply queues, and run controls coordinate
   across processes, but this does not establish safe concurrent full WebUI
   servers or schedulers sharing one Aidon home. If multiple processes sharing
   a home are supported, add coordination and stress tests for scheduler claims,
   cursors, artifact publication, and other file-backed stores. Otherwise,
   document and enforce the single-process deployment contract.
4. **WhatsApp durability and delivery recovery.** WhatsApp webhook goals remain
   synchronous and do not yet have a durable provider-message-ID inbox. Review
   retry deduplication and interruption behavior before unattended use.
5. **Operational assurance.** Enforce required CI checks in repository
   settings, establish backups/restore testing, resource and queue alerts, load
   testing, and image/OS vulnerability checks. The Compose healthcheck confirms
   listener liveness only; it does not certify model, memory, or sandbox
   readiness.
6. **Cleanup and maintenance.** Review incremental typing, wider lint rules,
   remaining module decomposition, SQLite fallback scaling, and cron-search
   efficiency. The two notification outboxes were inspected and retained
   because their storage and caller contracts differ; do not consolidate them
   solely because both are named outboxes. Evaluate ordinary tasks on the
   intended small models for semantic completeness and compute cost; deterministic
   checks and an optional model verifier cannot prove all content correct.

The pre-existing `setup.sh` Qdrant version update and existing `uv.lock`
upgrades were preserved. The setup regression checks that Qdrant remains pinned
without hard-coding one obsolete release number.
