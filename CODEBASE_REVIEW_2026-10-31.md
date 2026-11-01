# Codebase review — 2026-10-31

Reviewed commit `4b3ddf9`; code fixes below are in progress in the working tree. This is not a certification that the repository is bug-free.

## Confirmed findings

### 1. WebUI and messaging goal handlers cannot construct a run — fixed in code, runtime validation pending

`src/system/cli_webui.py:63`, `:78`, `:151`, `:296`, `:306`; `src/system/main.py:665`.

`run_webui` was extracted from `main`, but its nested goal handler still refers to `_run_sandbox`, `_make_web_researcher`, `_make_governor`, `_goal_extra_tools`, `_network_available`, `make_deep_researcher`, `client`, and `model_router` without defining or receiving them. Its first access, `_run_sandbox`, raises `NameError` for a dispatched goal. The same handler is registered for WebUI, WhatsApp, and Slack. Importing the missing helpers alone is insufficient: the configured client and router must also be passed into the extracted function.

The configured model, router and runtime helpers are now passed into the extracted WebUI entry point. Validation still needs to exercise the actual CLI-created dispatcher with an isolated test home and cover all three registered sources and fast-model routing. Existing route tests using constructed servers do not establish this wiring works.

### 2. Outbox loses notifications enqueued during a drain — fixed in code, runtime validation pending

`src/capabilities/outbox.py:58`, `:66`, `:79`, `:115`.

`drain` loads a snapshot and later overwrites the whole file with its own remaining list. A notification queued while the sender runs is overwritten. Reproduced without threads: have the sender call `queue()` for a second notification and return `True`; after `drain()`, the outbox is empty. Concurrent queue/drain operations have the same lost-update risk. Writes are also non-atomic, and a crash after an external send but before saving can repeat delivery; exactly-once delivery cannot be guaranteed by this file alone.

The file outbox now serializes in-process operations by path, writes atomically, and merges drain results into the latest file so notifications queued by a sender are retained. Cross-process locking and exactly-once delivery after a crash between external send and local save remain unresolved; validate concurrent use and document the at-least-once boundary.

### 3. Inspecting due schedules consumes them without running them — fixed for inspection, execution recovery remains open

`src/system/main.py:788`, `src/interface/triggers.py:419`.

`aidon trigger schedule` calls `check_due`, which immediately persists `last_run`, and then only prints the jobs. A subsequent scheduler tick sees those occurrences as already processed. The same early acknowledgement also means a crash or failure after `check_due` and before dispatch can lose a scheduled occurrence.

The CLI inspection path now uses a read-only `due_jobs` query. `check_due` still records an occurrence before dispatch, so a crash after that point can lose the occurrence; a durable claim/acknowledgement design remains open. Validate inspection followed by tick.

### 4. Zoned cron schedules can be rejected or stop firing at a month boundary — fixed in code, runtime validation pending

`src/interface/triggers.py:196`, `:204`, `:333`.

When skipping a nonmatching month, `_next_cron` creates a naive `datetime` even when the input is timezone-aware. The next loop compares naive and aware values, raising `TypeError`; `_next_due_uncapped` catches it and returns `None`. Reproduced with `SchedulerJob('x', '0 9 1 12 *', 'x', tz='America/Vancouver').next_due(datetime(2026, 10, 31))`: it returns `None`, while the unzoned schedule returns December 1.

Month advancement now preserves the timezone. Validate transitions across months, years and daylight-saving changes.

### 5. Standalone scheduler service is broken — fixed in code, runtime validation pending

`src/system/main.py:880`, `:928`, `:1017`.

The scheduler serve branch refers to undefined `server`, `msg`, and sender variables. The source and roadmap already document this. It cannot be used as the standalone service in its present state.

The daemon now creates its messaging configuration and senders locally, uses a confined practice directory name, and uses the nonblocking scheduled gate for practice requiring approval. It no longer refers to WebUI-only locals or waits on an unavailable browser approval card. Run a bounded startup/dispatch/shutdown smoke test and practice-delivery checks.

### 6. Slack and WhatsApp file tools always fail — fixed in code, runtime validation pending

`src/system/goal_tool_research.py:47`, `:144`, `:158`.

The registration function receives `confined` but both file tools call undefined `_confined`. Reproduced by invoking each registered function on a harmless temporary file: each returns a `NameError` failure before reaching the transport.

Both tools now use the injected path-confinement function. Validate successful calls with mocked transports as well as traversal rejection.

### 7. Valid leap-day cron schedules are rejected — fixed in code, runtime validation pending

`src/interface/triggers.py:197`, `src/interface/triggers.py:411`.

Cron searching stops after 366 days, so `0 0 29 2 *` after March 1, 2025 returns `None` even though it next fires in 2028. Job creation interprets that as a schedule that never fires.

Cron search now spans eight years, which covers the longest gap between valid leap-day occurrences without scanning a full Gregorian cycle. Validate leap years and truly impossible dates.

## Dead code, duplication, and improvement candidates

- `src/capabilities/skill_import.py`: the unused `_now` helper and unresolved return annotation were removed. The pyflakes undefined-name warnings for these symbols should be rechecked.
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

Remaining priority: add durable scheduler claim/acknowledgement and cross-process outbox locking if multiple Aidon processes share one home; then add CI coverage and review cleanup candidates. Runtime behavior for all code fixes remains unverified in this environment.
