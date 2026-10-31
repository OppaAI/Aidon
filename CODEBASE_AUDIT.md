# Repository-wide audit — 2026-10-31

Reviewed repair commit `88f91af`. Verdict: **not production-ready for unrestricted autonomous host access**.

## Status after subsequent repairs

The findings below describe the audited baseline; the status in this section reflects later fixes on `dev`.

- **Code mitigations implemented:** MCP child credential inheritance and blocking I/O, WebUI file/body bounds, local sandbox output/process-tree bounds, X11 wheel scrolling, staged plugin replacement, and filtered/validated remote archive extraction.
- **Validation improvements implemented:** plugin acceptance rejects known no-op tests and missing/out-of-candidate Python scripts, requires each declared tool in an asserted result, and mutation-checks tool functions; unsupported recommendation URLs are removed in the capability interview.
- **Additional hardening implemented:** MCP connection approvals now bind a SHA-256 fingerprint to the exact config and optional functional test; secrets are not printed or stored in test evidence. Lab transcripts are durable per conversation, with migration from the previous global-per-kind table.
- **Code-level acquisition fixes implemented:** durable interview/approval replay, explicit owner recovery after restart, exact-byte plugin retry idempotency, existing skill trust checks, MCP registry deduplication, and plugin acceptance coverage/mutation checks. Automatic unattended replay remains intentionally disabled so a recovered goal cannot repeat side effects without owner intent.
- **Runtime validation pending:** Home-PC browser refresh/switching, Modal, real MCP servers, and X11/desktop behavior require the checks in `PRODUCTION_READINESS.md`.
- **Residual test assurance limit:** authored test code and capability disclosures still require owner review; neither static analysis nor mutation checks can prove all behavior or safety.
- **Current full suite:** 1,417 passed, 7 skipped on 2026-10-31; see `PRODUCTION_READINESS.md` for the home-PC procedure.
- **Skill lifecycle refinement:** missing skills are treated as post-success optimization, while missing executable capabilities use Tool/MCP acquisition. Successful WebUI runs can open Skill Lab with conversation-bound, server-validated workflow evidence. Skill adoption is available only to that validated follow-up. Offline tests and live browser flow should be rerun for this revision.
- **Workflow DAG editor:** the editor persists dependency edges and node positions, validates cycles and metadata, and exposes fit-to-view and node-contract controls. Its scope is user workflow editing/execution; the internal agent loop remains responsible for stateful control flow, so a separate LangGraph-compatible runtime is not duplicated.
- **Module decomposition:** WebUI workflow APIs have been extracted into `src/interface/webui_workflows.py`; memory storage adapters and retention policy are separate from `Memory`; CLI runtime construction is in `src/system/runtime.py`; and plan/report value types plus capability-interview persistence are in `src/agentic/run_types.py` and `src/agentic/capability_interview.py`. Existing imports remain available through their prior modules. Plan execution has since moved into `src/agentic/plan_execution.py` (1,103 lines), reducing `loop.py` from 3,386 to 2,309 lines. CLI parser construction moved into `src/system/cli_parser.py` (245 lines), reducing `main.py` from 2,923 to 2,688 lines. `goal_tools.py` has since been decomposed into research/messaging (`goal_tool_research.py`), content/office creation (`goal_tool_creation.py`), and computer-use (`goal_tool_computer.py`) registrars, reducing it from 1,491 to 860 lines while keeping `_goal_extra_tools` as the public factory. A shared theme controller now applies and synchronizes the preference across static pages, studios, settings, and open tabs. WebUI page templates moved to `src/interface/webui_pages.py` (397 lines), while WhatsApp/Slack handlers moved to `src/interface/webui_messaging.py` (524 lines), reducing `webui.py` from 2,283 to 1,386 lines. The WebUI server command moved to `src/system/cli_webui.py` (688 lines), reducing `main.py` from 2,688 to 2,013 lines. Plan construction and research replanning moved to `src/agentic/planning.py` (510 lines), and tool dispatch moved to `src/agentic/tool_dispatch.py` (269 lines), and run finalization moved to `src/agentic/run_finalization.py` (276 lines), reducing `loop.py` from 3,386 to 1,285 lines. Plugin static inspection and loading moved into `src/capabilities/plugin_scan.py` (409 lines), reducing `plugins.py` from 1,022 to 640 lines while leaving preflight, adoption, and registry behavior in the original module. `main.py` (2,013 lines) and `loop.py` (1,285 lines) remain the largest orchestration modules for continued review.

## Coverage and limits

- Executed the full repository pytest suite: **1,348 passed, 7 skipped**. The skipped tests require working bubblewrap namespaces, unavailable on this runner.
- Ran Bandit recursively over all Python under `src`: **38,904 lines**, no scanner errors. Raw results: 383 low, 33 medium, 2 high findings. These are scanner classifications, not confirmed vulnerabilities.
- Ran targeted manual review of chat rendering/restoration, interview persistence, plugin adoption, sandbox/SSH/Modal boundaries, MCP transports, WebUI authentication/file serving/request parsing, and skill validation.
- Checked edited JavaScript syntax. No live browser recovery, real model/Modal, PC SSH/X11, load/chaos, dependency vulnerability or penetration test was completed.
- This is repository-wide automated coverage with targeted manual review, not a claim that every line, dependency, external integration or production configuration has been independently audited.

## Findings from the audited baseline

The following findings were confirmed at the baseline commit but have since received code fixes on `dev`. They are retained as audit history; verify the listed runtime behavior before closing deployment readiness.

### ~~P1 — MCP subprocesses inherit all host credentials~~ Fixed in code; runtime validation pending

`src/interface/mcp_client.py`, `McpClient.start`: the baseline forwarded unrelated credentials to approved stdio servers. The current implementation uses a minimal process environment with explicitly selected bindings. Runtime check: use a harmless test server, set a dummy unrelated environment variable, and confirm the child cannot read it while an explicitly selected binding is available. This limits inherited secrets but does not isolate the child filesystem or host account.

### ~~P1 — MCP calls can block a workflow indefinitely~~ Fixed in code; runtime validation pending

`src/interface/mcp_client.py`, `_call`: the baseline had unbounded reads. The current implementation applies request deadlines and response-ID checks. Runtime check: use a disposable server that stays silent, then one that sends a notification and a wrong-ID response before the correct response; confirm bounded timeout/correlation and child cleanup.

### ~~P1 — File serving includes sensitive Aidon-home files~~ Fixed in code; runtime validation pending

`src/interface/webui.py`, `handle_file`: the baseline allowed sensitive files within Aidon's home. The current handler limits serving to workspace, library files and images and excludes private filenames and database/key extensions. Runtime check: while authenticated, verify a generated artifact and library attachment open, while config/database/key paths, encoded traversal and symlinks outside allowed roots are denied.

### ~~P2 — Request bodies are not globally bounded~~ Fixed in code; runtime validation pending

`src/interface/webui.py`, handler `_read_body`: the baseline accepted unbounded request bodies. The current handler rejects invalid framing, applies an 80 MiB cap, and sets a read deadline. Runtime check: on a disposable instance, submit malformed, oversized, and stalled bodies to harmless POST/PUT and webhook routes; confirm bounded errors and that the server remains responsive.

### ~~P2 — Subprocess timeout/output limits are incomplete~~ Fixed for local runner; other backends need runtime validation

`src/capabilities/sandbox.py`, `run`: the local runner now caps each output stream at 1 MiB and cleans up its process group on timeout/end. Runtime check: run noisy and child-spawning commands on the deployed local runner and confirm truncation and cleanup. Exercise Modal and SSH independently; local process-group handling does not guarantee their limits.

### ~~P2 — Acquisition completion is not enforced end to end~~ Fixed with durable, owner-initiated recovery; runtime validation pending

The baseline lacked complete acceptance gating. Current plugin and skill paths require isolated test evidence, plugin evidence binds to candidate bytes, and MCP approval binds the exact config and optional functional probe. Interview approval is durable and the owner can resume the original goal after restart; plugin adoption retries are idempotent for identical approved bytes, skill retries verify stored bytes and trust, and MCP registry entries do not duplicate. Automatic unattended replay is deliberately disabled to prevent unapproved side effects. Plugin tests must call each declared tool in a result comparison and survive a per-tool mutation check. Owner review is still required. MCP list-tools alone remains connectivity evidence, not proof of every remote action. Never test external actions speculatively.

### ~~P2 — Recovery still requires explicit retry~~ Fixed as explicit owner recovery; unattended replay intentionally disabled

Interview steps persist and replay for the same goal/conversation, and the reopened conversation presents a recovery action. The owner can resume the approved original goal without repeating saved answers or consent. Standalone lab rows use conversation-scoped server storage, and plugin replacement has startup reconciliation. Browser refresh/switching and restart recovery still need the procedures above. Aidon does not automatically replay goals at startup because side effects require fresh owner intent.

### ~~P2 — Desktop scrolling uses an unsupported xdotool command~~ Fixed in code; runtime validation pending

`src/capabilities/x11_driver.py`, `X11Driver.__call__`: the baseline emitted an unsupported scroll command; it now sends bounded signed mouse-wheel button events. Runtime check: on the intended X11 display, verify actual scrolling and other desktop actions on an innocuous window. Merely finding DISPLAY and xdotool does not demonstrate desktop control works.

## Scanner findings triaged

- `registry_db.py` dynamic UPDATE assignments use a fixed allowed-column set and parameterized values; the scanner's SQL-injection warning is not a demonstrated injection.
- `voice.py` SHA-1 is required by the WebSocket handshake protocol, not a password hash.
- `acquisition_governor.py` SHA-1 is a mission identifier. Replace truncated SHA-1 with a modern digest as hardening, but the warning alone is not proof of an authorization bypass.
- URL-opening and XML warnings need per-destination/data-size review; do not mass-suppress them or disable TLS verification.

## Release decision

Green offline tests do not satisfy the remaining live-integration checks. Keep unrestricted autonomous host access disabled until target runtime checks pass and owners review authored tests and capability disclosures. Automatic unattended recovery remains disabled by design. Required PC access, accounts and credentials must be supplied through secure configuration; never put passwords in chat, commits or audit reports.

## Subsequent repair progress

### Fixed in code; target-runtime validation still required

Use the matching detailed procedure in `PRODUCTION_READINESS.md` and record date, commit, environment and outcome in its checklist.

- **MCP environment and protocol deadlines:** run the harmless local MCP smoke test, verify unrelated process environment secrets are absent, and test a silent server plus a notification before a matching response. Confirm the request times out and the child is cleaned up.
- **File serving and request bounds:** while signed in, confirm workspace/library artifacts still open and config/database/key files and symlinks outside allowed roots are denied. Send malformed and oversized bodies to harmless test routes; verify bounded HTTP errors and no server hang. Use a disposable test instance.
- **Local subprocess bounds:** on the target OS, run a noisy child and a child that spawns a subprocess; verify output truncation and process-tree cleanup at timeout. Test each deployed execution backend separately because local process-group handling does not prove Modal/SSH behavior.
- **X11 scrolling:** confirm actual wheel scrolling, screenshot, click, typing and focus on the intended display and Xauthority with a harmless window.
- **Plugin acceptance:** confirm trivial no-op checks are refused; use a harmless plugin with a meaningful assertion, verify per-tool mutation checks and hash-bound approval, then owner-review test source for coverage and safety.
- **MCP approval, Tool Lab persistence, research sources, browser restoration, Modal, SSH and live model:** follow their itemized procedures in `PRODUCTION_READINESS.md`; these need target services, browser, credentials or host access unavailable to this audit runner.
- **Post-success Skill Lab and DAG editor:** use the revised procedure in `PRODUCTION_READINESS.md` to validate successful-run handoff, rejection of failed/cross-conversation source IDs, and edit/save/reopen behavior in a live browser.

### Intentional safety and review limits

- Automatic unattended goal replay after process restart is intentionally not implemented; the owner-visible recovery action is the safe resume mechanism.
- Authored acceptance tests and capability disclosures still require owner review. Coverage/mutation checks improve evidence but cannot prove complete behavior or test safety.

- MCP child environments now contain only runtime variables and explicitly configured bindings. Stdio reads and nonblocking writes share request deadlines, with response-ID checks. This is not filesystem isolation; approved MCP children still have the host account's filesystem access.
- File serving is limited to workspace, library files and images, with private names and database/key extensions excluded. Existing links to files elsewhere in the Aidon home must be migrated to artifact locations.
- POST and PUT body parsing rejects invalid framing and payloads above 80 MiB and uses a socket read deadline.
- Local sandbox output is capped at 1 MiB per stream and its process group is killed at the end of a run or on timeout. Process groups are not a substitute for OS isolation: escaped sessions and other backends need separate verification.
- X11 scrolling emits wheel button events; verify it on the target desktop using the procedure above.
- Plugin registry, goal-tool, and low-level commit paths now fail closed without successful isolated, network-disabled acceptance evidence bound to candidate bytes. Bubblewrap, Docker, and Modal are accepted only when the test run reports isolation and networking is disabled. Approval identifies the backend, test count and hash without exposing arbitrary test arguments or output. Test quality still requires review.
- Durable interview recovery and idempotent exact-byte plugin retry are implemented; unattended automatic goal replay remains intentionally disabled. Verify owner-initiated recovery after restart with the home-PC procedure.
- MCP connection approval now shows the exact target, selected environment-variable names, a config/test-call fingerprint, and any optional one-shot functional test's tool and redacted arguments. The test runs only after that approval; result text is not stored in evidence.
- Standalone Tool, Skill, and MCP lab sessions are now keyed by conversation ID, migrate legacy records, and share a stable ID with their associated chat conversation. Browser-level restore and multi-browser conflict behavior still need a home-PC check.
