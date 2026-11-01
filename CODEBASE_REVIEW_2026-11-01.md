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
- Read-only live Slack `auth.test` and `conversations.history` probes: the bot
  token and both configured conversations succeeded. No synthetic chat message
  was posted during these checks.

No substantive, identical function bodies of 15+ lines were found by an AST
comparison; targeted inspection nevertheless found and removed the duplicated
memory-ranking algorithm. The two notification outboxes have different storage
and caller contracts and were repaired without an unreviewed consolidation.

## Remaining work, in priority order

1. **Finish provider-specific acceptance checks.** Basic Slack and WhatsApp
   message/reply behavior has been confirmed by the owner on the deployed
   instance. The stopped-plan approval path has had fixes for ID handling,
   bare approval commands, and Slack thread replies, but a successful complete
   approval-to-finished-goal cycle is not yet established. Modal file transfer,
   meaningful result verification, browser refresh/recovery and model failures
   also need target-runtime checks. This end-to-end workflow is the next
   focused investigation; transport is not currently the reported blocker.
2. **WhatsApp durability and delivery recovery.** WhatsApp webhook goals remain
   synchronous and do not yet have a durable provider-message-ID inbox. Review
   retry deduplication and interruption behavior before unattended use.
3. **One full WebUI/poller per Aidon home.** The inbox worker, reply queues and
   run controls coordinate across processes, but this does not establish safe
   concurrent full WebUI servers sharing all files. Cursor files, conversation
   artifact publication and other file-backed stores need a separate supported
   deployment/concurrency contract and stress testing.
4. **Operational assurance:** enforce required CI checks in repository settings,
   establish backups/restore testing, resource and queue alerts, load testing,
   and image/OS vulnerability checks. The Compose healthcheck confirms listener
   liveness only; it does not certify model, memory or sandbox readiness.
5. **Quality and maintenance:** evaluate ordinary tasks on the intended small
   models, including semantic completeness and computed budgets. Deterministic
   checks and an optional model verifier cannot prove all content correct.
   Incremental typing, wider lint rules, remaining module decomposition and
   SQLite fallback scaling remain follow-up work.

The pre-existing `setup.sh` Qdrant version update and existing `uv.lock`
upgrades were preserved. The setup regression checks that Qdrant remains pinned
without hard-coding one obsolete release number.
