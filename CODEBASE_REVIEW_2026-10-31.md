# Codebase review — 2026-10-31

Reviewed commit `4b3ddf9`. This is a prioritized repair backlog, not a certification that the repository is bug-free. No application code was changed for this review.

## Confirmed findings

### 1. WebUI and messaging goal handlers cannot construct a run — critical

`src/system/cli_webui.py:63`, `:78`, `:151`, `:296`, `:306`; `src/system/main.py:665`.

`run_webui` was extracted from `main`, but its nested goal handler still refers to `_run_sandbox`, `_make_web_researcher`, `_make_governor`, `_goal_extra_tools`, `_network_available`, `make_deep_researcher`, `client`, and `model_router` without defining or receiving them. Its first access, `_run_sandbox`, raises `NameError` for a dispatched goal. The same handler is registered for WebUI, WhatsApp, and Slack. Importing the missing helpers alone is insufficient: the configured client and router must also be passed into the extracted function.

Fix: inject the configured runtime dependencies and exercise the actual CLI-created dispatcher with a fake model and isolated test home. Cover all three registered sources and fast-model routing. Existing route tests using constructed servers do not establish this wiring works.

### 2. Outbox loses notifications enqueued during a drain — high

`src/capabilities/outbox.py:58`, `:66`, `:79`, `:115`.

`drain` loads a snapshot and later overwrites the whole file with its own remaining list. A notification queued while the sender runs is overwritten. Reproduced without threads: have the sender call `queue()` for a second notification and return `True`; after `drain()`, the outbox is empty. Concurrent queue/drain operations have the same lost-update risk. Writes are also non-atomic, and a crash after an external send but before saving can repeat delivery; exactly-once delivery cannot be guaranteed by this file alone.

Fix: use transactional persistence/locking across producers and consumers, atomic state changes, and an explicit delivery/retry contract. Test concurrent enqueue/drain, restart, and failure after sending; avoid promising exactly-once delivery without receiver idempotency.

### 3. Inspecting due schedules consumes them without running them — high

`src/system/main.py:788`, `src/interface/triggers.py:419`.

`aidon trigger schedule` calls `check_due`, which immediately persists `last_run`, and then only prints the jobs. A subsequent scheduler tick sees those occurrences as already processed. The same early acknowledgement also means a crash or failure after `check_due` and before dispatch can lose a scheduled occurrence.

Fix: make inspection read-only and use a durable claim/acknowledgement policy for execution, with a clear retry and side-effect policy. Test inspection followed by tick, dispatch failure and restart.

### 4. Zoned cron schedules can be rejected or stop firing at a month boundary — high

`src/interface/triggers.py:196`, `:204`, `:333`.

When skipping a nonmatching month, `_next_cron` creates a naive `datetime` even when the input is timezone-aware. The next loop compares naive and aware values, raising `TypeError`; `_next_due_uncapped` catches it and returns `None`. Reproduced with `SchedulerJob('x', '0 9 1 12 *', 'x', tz='America/Vancouver').next_due(datetime(2026, 10, 31))`: it returns `None`, while the unzoned schedule returns December 1.

Fix: preserve the zone when advancing months and test transitions across months, years and daylight-saving changes.

### 5. Standalone scheduler service is broken — high if needed

`src/system/main.py:880`, `:928`, `:1017`.

The scheduler serve branch refers to undefined `server`, `msg`, and sender variables. The source and roadmap already document this. It cannot be used as the standalone service in its present state.

Fix: share or inject the required service construction, then run a bounded startup/dispatch/shutdown smoke test and practice-delivery tests. Preserve existing approval behavior.

### 6. Slack and WhatsApp file tools always fail — medium

`src/system/goal_tool_research.py:47`, `:144`, `:158`.

The registration function receives `confined` but both file tools call undefined `_confined`. Reproduced by invoking each registered function on a harmless temporary file: each returns a `NameError` failure before reaching the transport.

Fix: use the injected path-confinement function and test successful calls with mocked transports as well as traversal rejection.

### 7. Valid leap-day cron schedules are rejected — medium

`src/interface/triggers.py:197`, `src/interface/triggers.py:411`.

Cron searching stops after 366 days, so `0 0 29 2 *` after March 1, 2025 returns `None` even though it next fires in 2028. Job creation interprets that as a schedule that never fires.

Fix: use a search horizon capable of covering the calendar cycle, or calculate the next occurrence without a short fixed horizon. Test leap years and truly impossible dates.

## Dead code, duplication, and improvement candidates

- `src/capabilities/skill_import.py:29`: `_now` has no in-repository callers and refers to missing `datetime` and `timezone` imports. Confirm external compatibility before removing it. The `Skill` annotation warning in the same file is deferred by `from __future__ import annotations`; it is not evidence that ordinary imports fail, though runtime annotation introspection may need attention.
- Pyflakes reported 177 lines, mostly unused imports and locals. Several are deliberate compatibility re-exports after module decomposition, so do not bulk-delete them; review public imports and tests first. Add an undefined-name check to CI, with narrow documented exceptions, to catch extraction regressions.
- There are two notification outbox implementations (`src/capabilities/outbox.py` and `src/agentic/lifecycle.py`), with different storage and delivery semantics. Determine their intended responsibilities before consolidating; the duplicate names alone do not prove either is unused.
- `src/system/main.py` and `src/agentic/loop.py` remain large orchestration modules. More splitting is useful only when paired with end-to-end wiring tests: the WebUI and media findings show that extraction can silently break dependencies.
- `docs/ROADMAP.md`, the audit and readiness documents mix current status with historical claims. Reconcile them after validating the current head, especially desktop control and test results.
- Cron calculation currently scans candidate times and uses a fixed horizon. Calendar-aware jumps would improve efficiency and correctness for sparse schedules.

## Verification and scope

- Repository-wide `compileall` passed for Python under `src` and `scripts`. Pyflakes scanned the Python source and identified the undefined names above, among other warnings.
- `node --check` passed for non-vendored WebUI JavaScript.
- System-Python pytest run: 1,363 passed, 7 skipped, 36 failed, 19 errors. Many failed tests attempted socket operations forbidden by this execution environment; not every failure was individually classified. The project `.venv` does not have pytest. Repeat on the home PC or CI before attributing the remainder to code.
- The repository contains 206 Python source files and 175 Python test files. The static checks covered source broadly; manual review and reproductions were targeted. This review does not establish that every function, JavaScript behavior, dependency, integration, or security boundary has been manually checked. Browser, real provider, Modal, SSH and X11 checks remain pending as described in `PRODUCTION_READINESS.md`.

Suggested order: restore WebUI goal wiring; repair durable notification and scheduler handling; fix cron and media tools; then add CI coverage and review cleanup candidates. Deployments that rely on scheduler serve should prioritize it alongside scheduler handling.
