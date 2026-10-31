# Spec Sheet

How to use this sheet: fill in the three required sections (Goal, Done when, Constraints). Everything else is optional. Blank means "use Aidon's configured defaults", and blank never triggers a question.
Save it and attach it in the chat box. Aidon reads it, verifies it, and skips the interview. You can also edit a frozen spec on the sheet itself in chat, save it, and proceed.

Prefill: Aidon may prefill non-critical fields from your past tasks and memory. Prefilled values are proposals, not facts: review them, edit them, or delete them. Aidon never prefills Budget, Recipients, or anything that sends, spends, or publishes.

Permissions rule: unspecified permissions follow Aidon's enforced policy; a blank field never grants additional authority. Actions that need approval stay gated even after the plan is approved. To restrict further, write it as a Constraint.

Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo.
Example: examples/SPEC-SHEET-EXAMPLE.md in the repo.
Docs: README, Spec sheet section.

## Goal

Write the goal in plain language, in one sentence, saying what you want Aidon to do and for whom, not how it will do it. This section is required.

## Done when

Write checkable acceptance criteria, one per bullet. Aidon verifies each one before reporting done. Prefer things it can test (a command, a count, a total, a source URL) over opinions. This section is required.

## Constraints

List every hard limit Aidon must not break, one per bullet, such as a date range, a format, or a source file. Write None if there are no hard limits. This section is required.
Write scope, priorities, and permission restrictions here too, one per bullet, for example "Out of scope: ...", "Priority: accuracy over speed", or "Never: ...".

## Deliverables

List each file or concrete outcome Aidon must produce, one per bullet, with the file name if it is a file, and the properties it must have.

## Inputs and context

List every input file, link, repo, or dataset Aidon should read first, one per bullet, with what each is for. Say who the audience is and what tone to use if it matters.

## Budget

Write the maximum total amount Aidon may spend, in dollars and cents, or write None if there is no budget cap. Do not write a budget here unless it is a real limit. Write other limits (a time cap, a max step count) as Constraints.

## Deadline

Write the date and time the work must be finished by, or write No deadline if there is none.

## Recipients

List who the work is for or who should receive the output, with their contact details if Aidon must send it to them, or write None if there is no recipient.

## Files

List every output file Aidon should create, one per bullet, with the file name and path. Input files go under Inputs and context.

## Notifications

Write how you want to be told when the work is done or if it fails, or write None if no notification is needed. A checkpoint (pause for review after a phase) goes here as "Checkpoint: ...".

## Permissions

Aidon enforces its own policy; this table is the default, not a grant. Override only by writing a Constraint.

| Action | Level |
|---|---|
| Read files, browse, research | auto |
| Write or edit files in the workspace | auto |
| Run code, scripts, tests in the sandbox | auto |
| Install packages or tools | ask |
| Create and register new tools, skills, or MCP servers | ask |
| Send email or messages to other people | ask (draft first) |
| Spend real money or make purchases | never |
| Post or publish anything publicly | never |

## If something goes wrong

Aidon preserves successful outputs, diagnoses the failure, attempts a bounded repair, re-runs the affected checks and any necessary regression checks, records the failure and repair outcome, and stops and reports if the retry limit is reached or safe progress is no longer possible. To change this, write a bullet under Assumptions starting with "Recovery:".

## Assumptions

List anything Aidon may assume without asking, one per bullet, or write None if there are no assumptions. Aidon adds its own assumptions to the plan for you to confirm.

## Open questions

List any questions that are still open, one per bullet, or write None if the spec is complete. A spec with open questions is not verified, and Aidon asks only about the ones that block the work.

## What Aidon does with this sheet

1. Validate: checks that the required sections are filled and the criteria are testable.
2. Fill gaps: infers defaults and states each assumption instead of interviewing. It asks only when a missing answer would change the work or cause something irreversible.
3. Plan: writes a short plan covering steps, tools, assumptions, and how each "Done when" item will be checked.
4. Execute: works within permissions and limits, and keeps a run log of actions, spend, and decisions.
5. Verify: checks every "Done when" item and reports pass or fail with evidence.
6. Report: delivers the outputs and a short summary with the evidence and outcome for each acceptance check, estimates, unverified claims, limitations, and failed checks, the files created or modified and any new capabilities, and any outstanding approvals or actions not performed. Final status: Success, Partial success, or Failed.
