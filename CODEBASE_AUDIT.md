# Repository-wide audit — 2026-10-31

Reviewed repair commit `88f91af`. Verdict: **not production-ready for unrestricted autonomous host access**.

## Coverage and limits

- Executed the full repository pytest suite: **1,348 passed, 7 skipped**. The skipped tests require working bubblewrap namespaces, unavailable on this runner.
- Ran Bandit recursively over all Python under `src`: **38,904 lines**, no scanner errors. Raw results: 383 low, 33 medium, 2 high findings. These are scanner classifications, not confirmed vulnerabilities.
- Ran targeted manual review of chat rendering/restoration, interview persistence, plugin adoption, sandbox/SSH/Modal boundaries, MCP transports, WebUI authentication/file serving/request parsing, and skill validation.
- Checked edited JavaScript syntax. No live browser recovery, real model/Modal, PC SSH/X11, load/chaos, dependency vulnerability or penetration test was completed.
- This is repository-wide automated coverage with targeted manual review, not a claim that every line, dependency, external integration or production configuration has been independently audited.

## Confirmed findings to address

### P1 — MCP subprocesses inherit all host credentials

`src/interface/mcp_client.py`, `McpClient.start`: `dict(os.environ)` forwards unrelated credentials to any approved third-party stdio server. The connection approval does not enumerate all inherited secrets. Build a minimal process environment and add only explicitly selected bindings; test that unrelated secrets never reach the child. Treat existing MCP servers as having host-account privileges until process isolation is implemented.

### P1 — MCP calls can block a workflow indefinitely

`src/interface/mcp_client.py`, `_call`: blocking `stdout.readline()` has no deadline, does not match response IDs, and assumes the next line is a response rather than a notification. Add bounded protocol reads, response correlation, notification handling, cancellation and child-process cleanup. Test silent servers, interleaved notifications and wrong IDs.

### P1 — File serving includes sensitive Aidon-home files

`src/interface/webui.py`, `handle_file`: resolved-path confinement prevents escape from home, but allows every file inside it, including configuration and databases. Authentication is present; this is excessive exposure within the authenticated artifact-serving boundary. Restrict serving to registered artifacts/library attachments and deny configuration, credentials and internal databases. Test symlinks, encoded paths and generated links.

### P2 — Request bodies are not globally bounded

`src/interface/webui.py`, handler `_read_body`: trusts Content-Length and reads it into memory without a general maximum. Specific upload limits do not protect every route or the initial read. Reject invalid/negative/oversized lengths before reading, return suitable HTTP errors, and configure read deadlines. Verify webhook routes too.

### P2 — Subprocess timeout/output limits are incomplete

`src/capabilities/sandbox.py`, `run`: capture_output buffers all output. Timeout handling does not explicitly terminate a whole process group. RLIMIT_FSIZE does not bound pipe output. Implement bounded collection and process-tree cleanup across supported backends, with noisy and child-spawning tests.

### P2 — Acquisition completion is not enforced end to end

The approved interview brief becomes planner context. Plugin static preflight and successful registration do not prove functional acceptance tests passed. MCP list-tools validates connectivity, not every remote action. Introduce durable acquisition states and recorded evidence tied to exact plugin bytes/server configuration, then resume the original goal only after successful tests and required approvals. Never test external actions speculatively.

### P2 — Recovery still requires explicit retry

Interview steps persist and replay for the same goal/conversation; automatic waiter/workflow restoration is not implemented. Standalone lab pages still rely on browser storage. Plugin replacement rolls back ordinary failures, but process termination between renames needs startup reconciliation. Complete browser refresh, switching and process-restart tests before claiming outage-safe operation.

### P2 — Desktop scrolling uses an unsupported xdotool command

`src/capabilities/x11_driver.py`, `X11Driver.__call__`: scroll emits `xdotool scroll ...`; implement signed mouse-wheel button events with a bounded count and verify on the target X11 session. Merely finding DISPLAY and xdotool does not demonstrate desktop control works.

## Scanner findings triaged

- `registry_db.py` dynamic UPDATE assignments use a fixed allowed-column set and parameterized values; the scanner's SQL-injection warning is not a demonstrated injection.
- `voice.py` SHA-1 is required by the WebSocket handshake protocol, not a password hash.
- `acquisition_governor.py` SHA-1 is a mission identifier. Replace truncated SHA-1 with a modern digest as hardening, but the warning alone is not proof of an authorization bypass.
- URL-opening and XML warnings need per-destination/data-size review; do not mass-suppress them or disable TLS verification.

## Release decision

Green offline tests do not satisfy the host-access, crash-recovery, functional-acquisition or real integration requirements. Keep deployments restricted while the P1 findings and workflow blockers remain open. Required PC access, accounts and credentials must be supplied through secure configuration; never put passwords in chat, commits or audit reports.

## Subsequent repair progress

- MCP child environments now contain only runtime variables and explicitly configured bindings. Stdio reads and nonblocking writes share request deadlines, with response-ID checks. This is not filesystem isolation; approved MCP children still have the host account's filesystem access.
- File serving is limited to workspace, library files and images, with private names and database/key extensions excluded. Existing links to files elsewhere in the Aidon home must be migrated to artifact locations.
- POST and PUT body parsing rejects invalid framing and payloads above 80 MiB and uses a socket read deadline.
- Local sandbox output is capped at 1 MiB per stream and its process group is killed at the end of a run or on timeout. Process groups are not a substitute for OS isolation: escaped sessions and other backends need separate verification.
- X11 scrolling emits wheel button events; verification on a real desktop is still outstanding.
- Durable acquisition states, verified acceptance-test evidence, and automatic interruption recovery remain unresolved.
