# Production readiness review

Reviewed against the requested chat → capability inventory → Tool Crafting Lab → tested acquisition → original goal workflow. This is a code review and local test record, not certification for deployment.

## Implemented and locally validated

- Missing-capability assessment has a model fallback when Clef is absent on interactive runs.
- Explicit proceed/reject decisions precede a bounded requirements interview and brief approval.
- Interview questions and answers have separate lab events and render inside one Tool Crafting Lab card using the existing composer.
- The server identifies lab answers so polling timing cannot duplicate them as ordinary chat bubbles.
- Cancellation during the interview stops without preparing another brief.
- Sandbox execution requires isolation by default; Modal keeps its independent network policy.

## Release blockers

1. **Acquisition orchestration:** an approved brief is currently planner context. There is no deterministic state machine proving acquisition, successful tests, registration and resumption of the original goal. Add explicit states and evidence before claiming completion.
2. **Functional validation:** plugin preflight scans and validates source; it does not enforce successful functional acceptance tests before adoption. MCP connection/list checks do not validate every tool's behavior. Require scoped acceptance evidence without speculative calls with side effects.
3. **Recovery:** question waiters and interview state live in process memory. Persist the original goal, interview, approved brief and stage, restore them after restart, and make acquisition retries idempotent. Browser refresh and conversation switching need browser tests.
4. **Recommendations:** the interview model currently has no research tool loop. It must not claim current prices, compatibility or MCP endpoints are verified. Add cited discovery before presenting researched options.
5. **Regression failures:** the full suite still fails. Several older tests depend on empty FakeClient replies becoming executable plans; preserve rejection of invalid plans and provide valid scripted plans in workflow tests.
6. **PC execution:** SSH is an alternative execution backend, not a separate scoped PC capability alongside Modal. Desktop actions run on Aidon's host. Verify target selection, permissions and credentials before enabling remote installations or desktop access.
7. **Deployment validation:** real model, Modal authentication/networking, browser interactions and PC/X11 control have not been validated end to end in this session.

## Additional hardening to review

- Plugin replacement removes the old directory before copying the new one; use a verified staged replacement with rollback.
- Audit archive extraction from remote execution and constrain destinations before accepting returned files.
- Give active labs stable identities per run/conversation and persist their transcript separately from ordinary conversation history.
- Retain explicit approval binding to exact plugin bytes and MCP command/URL; brief approval is not permission for unrelated host actions.

## Required end-to-end scenarios

Covered goal; reject lab; uncertain user compares options; approve and revise brief; failed tests prevent registration; successful acquisition resumes original goal; cancellation; reload/restart; conversation switching; unavailable model/Modal/MCP; repeated submission; changed source after approval; missing credentials; PC target disconnected.

## Repair validation update

- Baseline regression run: 1,346 passed, 7 skipped. Namespace-dependent tests now explicitly probe the runner and skip when isolation is unavailable; execution still fails closed by default. Subsequent focused checks cover durable replay and plugin replacement rollback.
- Interview steps (including model output, original goal, questions and answers) persist in SQLite with FULL synchronization. Retrying the same goal in the same conversation replays committed steps and refuses changed prompts. This is retry recovery, not automatic process restart orchestration.
- Research is available during uncertain-choice interviews through the configured researcher. Unavailable research is labeled unverified.
- Plugin replacement now stages and hashes new bytes, preserves the previous version until installation, and rolls back on copy/load failures. Sudden process termination during rename still needs startup reconciliation.
- SSH sync-back now uses Python's data-filtered archive extraction, matching Modal's extraction policy.
- Workflow fixtures now supply executable model plans. Empty/invalid real model plans remain rejected. Updated assertions reflect the current lab and tool contracts.
- Actual model, Modal and PC integration checks remain unrun without the required configured endpoints, authentication and reachable PC. These cannot be established by offline tests.

The numbered release blockers above remain a tracking list. Some have mitigations described here, but deterministic acceptance-test enforcement, guaranteed acquisition/resumption, full standalone-lab recovery and real deployment validation are still open. Do not interpret green offline tests as production readiness.
