# Production readiness review

Reviewed against the requested chat → capability inventory → Tool Crafting Lab → tested acquisition → original goal workflow. This is a code review and local test record, not certification for deployment.

## Implemented and locally validated

- Missing-capability assessment has a model fallback when Clef is absent on interactive runs.
- Explicit proceed/reject decisions precede a bounded requirements interview and brief approval.
- Interview questions and answers have separate lab events and render inside one Tool Crafting Lab card using the existing composer.
- The server identifies lab answers so polling timing cannot duplicate them as ordinary chat bubbles.
- Cancellation during the interview stops without preparing another brief.
- Sandbox execution requires isolation by default; Modal keeps its independent network policy.

## Code-level work still open

These items need implementation before the release can be called complete; live checks below do not close them.

1. **Durable end-to-end acquisition state:** the interview is persisted and approved skills trigger a bounded original-goal replan, but there is no durable state machine that automatically resumes acquisition, registration, and replanning after a process restart. The current recovery action is owner initiated.
2. **Acquisition retry semantics:** test interrupted adoption at each side-effect boundary and make retries explicitly idempotent across plugins, skills, and MCP. Current skill adoption rejects changed bytes and recognizes an already-trusted identical skill; plugin replacement reconciles staged filesystem changes, but that is not a complete workflow transaction.
3. **Acceptance-test quality:** obvious plugin no-ops are rejected and approval warns that static checks cannot prove coverage or safety. A stronger review/evaluation of authored tests is still needed; passing isolated commands alone is not proof of useful behavior.

## Implemented in code; validate live before release

The code changes and offline tests are complete for these items. This cloud workspace lacks the user's live browser, credentials, home PC, and configured services, so run the procedures below on the target setup and record evidence in the checklist.

1. **Tool Lab acquisition and recovery UI:** code persists interview turns and lab state per conversation, renders questions in the lab card, and offers explicit recovery. Follow home-PC procedure 3 and 4 below; also open two conversations, save an answer in each, switch between them, and verify neither transcript overwrites the other. Force-close/restart Aidon mid-interview and verify the saved answer is offered by the recovery action, then continue without answering it twice.
2. **Plugin test gate and approval:** using a disposable plugin, try `true` or `python -c 'assert True'` as its acceptance test and confirm adoption is refused. Replace it with a real assertion against a harmless plugin output, confirm the approval shows isolated backend, test count and candidate hash plus the quality caveat, then approve and verify the tool is registered. Do not use a plugin with external side effects.
3. **MCP approval and functional probe:** configure a harmless local/test MCP server. Connect and confirm approval identifies the exact target and selected environment-variable names without values. Request an optional read-only test call; verify the approval shows its tool and redacted arguments before invocation, and that evidence stores only result type/hash. Confirm connect/list by itself is reported only as connectivity validation.
4. **Research citation handling:** run a capability interview with configured search/research and compare the brief with returned search URLs. Confirm unsupported URLs are removed, returned URLs appear as references, and no-source research is labeled unverified. Manually verify each remaining URL supports the nearby claim; automated checks establish URL provenance only.
5. **Browser persistence and rendering:** use procedure 2 below. Refresh while a conversation has Markdown, a code block, and a generated artifact; confirm the same conversation and content return. Repeat with the Tool Lab open, then switch conversations and return.
6. **Actual sandbox backend:** use procedure 5 below with the backend you will deploy. For Modal, run from the configured Modal account and confirm the run card shows Modal, successful isolation, and the expected network policy. A local Bubblewrap/Docker run does not validate Modal.
7. **SSH/PC isolation and cleanup (only if SSH is enabled):** SSH is intentionally unsandboxed. Configure a disposable reachable target using secure configuration (never chat or this Markdown), run a harmless command, confirm unattended shell approval is not bypassed, confirm the workspace is a unique child of the configured root, then confirm cleanup removes only that child and leaves sibling/parent files intact. If Aidon and the desktop are on the same PC, use the local X11 procedure instead of SSH.
8. **Local X11/computer use:** use procedure 5 below, with a harmless test window. Verify screenshot, focus, click, typing and bounded wheel scroll against the intended DISPLAY/Xauthority. Confirm behavior on the actual desktop session; detecting `DISPLAY` or `xdotool` alone is insufficient.
9. **Live model and deployment configuration:** submit a harmless acquisition goal through the production-configured model/provider and confirm it returns a valid plan, handles a missing capability interview, and fails clearly when the provider is unavailable. Never use a consequential goal for smoke testing.

## Release blockers

- Regression failures are **closed for this revision**: 1,410 passed, 7 environment-dependent skips on 2026-10-31. Re-run after later code changes and in CI.
- The code-level items above remain open. Runtime-only checks above remain pending until run on the target deployment. Green offline tests do not certify production readiness.

## Additional hardening — code fixes implemented

- Plugin replacement stages and hashes candidate bytes, rolls back on copy/load failures, and reconciles interrupted renames at startup.
- Remote archive sync-back rejects traversal and escaping links before extraction and applies Python's data filter.
- Standalone lab transcripts have conversation-scoped durable identities and no longer overwrite another conversation's tool, skill or MCP lab state.
- Plugin candidate hashes and MCP target/config fingerprints bind approval to the artifact or exact connection; an explicitly requested MCP functional probe is displayed before execution.

## Required end-to-end scenarios

Covered goal; reject lab; uncertain user compares options; approve and revise brief; failed tests prevent registration; successful acquisition resumes original goal; cancellation; reload/restart; conversation switching; unavailable model/Modal/MCP; repeated submission; changed source after approval; missing credentials; PC target disconnected.

## Repair validation update

- Trusted skill adoption now signals the goal runner to replan the original goal against the refreshed trusted-skill registry. It performs at most one skill-triggered replan per attempt, validates the new plan against the current tools and skills, keeps completed reversible draft files, and refuses to continue after failed or irreversible earlier work. Focused tests cover successful replan and fail-closed handling. This closes the “current plan never uses newly trusted skill” subfinding; durable end-to-end acquisition/resume state remains open.
- A DAG plan containing `adopt_skill` is forced to sequential execution, so work planned against the old capability inventory cannot race the adoption/replan boundary.
- Planner instructions now match runtime behavior: the newly trusted procedure enters refreshed context only after trust, and Aidon is told to rely on the bounded original-goal replan rather than repeat acquisition or ask for a manual replan.
- Plugin acceptance rejects obvious no-ops (`true`, `echo`, version/help commands, constant-only Python assertions) and missing/out-of-candidate Python test scripts. Owner approval now states that isolated success does not certify coverage or test safety; stronger behavioral review remains required.
- Capability interview recommendations remove URLs absent from retrieved evidence, show retrieved URLs as references, and label recommendations unverified when search returns no source URLs. This prevents fabricated links from appearing as citations; it does not establish that the linked sources support the claims.

- Baseline regression run: 1,346 passed, 7 skipped. Namespace-dependent tests now explicitly probe the runner and skip when isolation is unavailable; execution still fails closed by default. Subsequent focused checks cover durable replay and plugin replacement rollback.
- Interview steps (including model output, original goal, questions and answers) persist in SQLite with FULL synchronization. Retrying the same goal in the same conversation replays committed steps and refuses changed prompts. Reopened chats show a recovery action; automatic process restart orchestration remains open.
- Research is available during uncertain-choice interviews through the configured researcher. Source URLs are retained for citations; unavailable research is labeled unverified. The current test verifies URL retention, not live-model citation accuracy.
- Plugin replacement stages and hashes new bytes, preserves the previous version until installation, and rolls back on copy/load failures. Startup reconciliation restores an approved backup when a process stops between renames, and removes a stale backup only after validating the installed target; regression tests cover both cases.
- Remote archive sync-back pre-scans for traversal and escaping links, then extracts with Python's data filter; hostile archive regression tests cover traversal rejection.
- MCP approval now binds a canonical SHA-256 fingerprint of the exact connection config and optional test call, names environment-variable bindings without revealing values, and shows any requested test tool and redacted arguments before it can run. Functional test results record only type and output hash, not returned text. Connect/list alone is clearly recorded as connectivity evidence, not per-tool behavior proof.
- Standalone lab state is keyed by kind and conversation ID, with a migration for previous global rows. API saves require the matching ID, and the lab page uses the same stable ID for saved lab state and its associated chat conversation.
- Full regression suite on 2026-10-31 after this repair set: 1,410 passed, 7 skipped. JavaScript syntax check passed for `labs.js`; browser-level restore/switching remains a home-PC check.
- The WebUI, CLI, goal tool, registry and low-level commit path refuse plugin adoption without successful isolated acceptance evidence bound to the candidate hash. Bubblewrap, Docker, and Modal are accepted only when the run reports isolation and networking is disabled. The approval card reports the backend and test count, while suppressing arbitrary test arguments and output that could contain secrets. Test quality still needs review.
- Skills cannot be trusted until isolated, offline tests pass for the current metadata and Markdown bytes. Changed skill files leave planner context until retested and separately approved; legacy trusted skills without evidence need revalidation.
- SSH execution is marked unsandboxed, so practice and scheduled approval paths no longer treat it as a disposable container. Remote workspaces are unique child directories; configured parent paths, `..`, and `~` are not removed or accepted as cleanup targets.
- SSH sync-back now uses Python's data-filtered archive extraction, matching Modal's extraction policy.
- Workflow fixtures now supply executable model plans. Empty/invalid real model plans remain rejected. Updated assertions reflect the current lab and tool contracts.
- Actual model, Modal and PC integration checks remain unrun without the required configured endpoints, authentication and reachable PC. These cannot be established by offline tests.

Only the code-level items under “Code-level work still open” are unimplemented. Items under “Implemented in code; validate live before release” have offline coverage but remain unverified against the target runtime until their procedures are completed.

## Fast home-PC validation (after pulling `dev`)

This is the short manual acceptance pass for the remaining runtime-only items. Use a test Aidon home/workspace and a harmless goal; do not use a real email, purchase, account or host-changing goal.

1. Update and run the offline regression suite from the repository root:

   ```sh
   git switch dev
   git pull --ff-only
   python3 -m pytest tests/ -q
   ```

   Expected: all tests pass; environment-dependent skips are reported. If pytest is missing, activate the project environment or install the test dependency there before proceeding.

2. Start the WebUI with the same local environment/configuration you normally use:

   ```sh
   ./launch.sh
   ```

   Open `http://127.0.0.1:8765/`. Confirm the home page loads, a new chat accepts a goal, and a browser refresh restores the same conversation and formatted Markdown/artifacts.

3. Validate acquisition flow with a harmless test skill (for example, a short checklist for drafting a local project plan). Ask for an output that specifically benefits from that procedure. Expected sequence: Aidon explains the missing capability; the Tool Crafting Lab card asks to proceed; the discussion and interview stay inside the card; rejecting ends the task without registering anything. In a second run, proceed, answer the interview, inspect/edit the skill draft, run its isolated test, and separately approve trust. Confirm the original goal is replanned and completed using the skill without repeating the interview. Confirm the skill appears in the skill library only after its tests and trust approval pass.

4. Verify failure boundaries with a disposable invalid skill draft: make its isolated check fail and confirm it is not trusted or visible to the planner. Cancel another interview once and confirm it stops cleanly. Refresh the browser during an interview and verify the latest saved answer and current lab state return.

5. If configured, run one harmless sandbox smoke test for the actual execution backend (`bubblewrap`, Docker, or Modal) and confirm the run card reports the expected backend and network state. Do not infer Modal readiness from local bubblewrap success. For X11, use an innocuous window and verify screenshot, focus, click, typing and wheel scroll on the configured display; record display/Xauthority errors. Aidon on the same PC does not need SSH to control that PC. SSH is only a separate remote backend and needs an independently reachable target.

Record date, commit, backend, browser, outcome, and any error under the checklist below. Never paste credentials, API keys, cookies, or SSH passwords into this document.

| Runtime check | Status | Evidence / notes |
| --- | --- | --- |
| Home-PC full test suite | Pending | |
| WebUI chat + refresh persistence/Markdown | Pending | |
| Tool Lab proceed/reject and in-card interview | Pending | |
| Skill isolated test, trust approval, original-goal replan | Pending | |
| Failed test and cancellation boundaries | Pending | |
| Actual configured sandbox backend (Modal if used) | Pending | |
| Local X11 / computer-use smoke test (if enabled) | Pending | |
| Plugin no-op rejection and real isolated acceptance test | Pending | |
| MCP target approval and optional functional probe (if used) | Pending | |
| Research URL provenance and manual source/claim review | Pending | |
| Restart during acquisition and resume behavior | Pending | |
| SSH target/workspace/cleanup checks (only if SSH is enabled) | Pending | |
| Live model/provider smoke test | Pending | |
