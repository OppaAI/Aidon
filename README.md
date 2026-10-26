# Aidon

An autonomous agent that works from a plain-language goal.

The user gives a goal in chat. Aidon researches, plans, tests the
plan in a sandbox, verifies the result against evidence, asks
the human for approval before any live action, executes only
what was approved, and then records what it learned so the next
goal can reuse it.

## What is new here

The idea was validated with an earlier prototype, and all code
in this repository was written during the Meta Global AI
Developer Hackathon, Oct 26 - Nov 4, 2026. Nothing in this
repository was copied from that prototype: the design was read,
and the code was written fresh, part by part, and tested as it
was written.

The demo runs on Meta models through the Meta Model API. That
is a real part of what Aidon runs, not just a build-time choice.

## Run it

```sh
cp .env.example .env
# Fill in your own endpoint, key, and model name in .env
python -m aidon.main goal "Plan a dinner for two"
```

Configuration is just an endpoint, a key, and a model name:

- `AIDON_API_BASE` — endpoint URL for the model API
- `AIDON_API_KEY` — API key for that endpoint
- `AIDON_MODEL` — model name at that endpoint
- `AIDON_HOME` — where Aidon keeps its state (default `~/.aidon`)

## Models

- **Agent, submission and demo:** Muse Spark 1.3, standard.
  Built for long-horizon, multi-step agentic work, with native
  tool calling and MCP support.
- **Agent, tests only:** Muse Spark 1.3 Contributor, same
  capabilities, much cheaper. Switch by changing `AIDON_MODEL`
  only, no code change. Contributor inputs and outputs are used
  to train models, so tests must use non-sensitive test data
  only, never a real private goal, and the demo and submission
  run on the standard model, not Contributor.
- **Speech, when voice is on:** Muse Voice Transcribe, for
  speech-to-text only. It transcribes what the user said, then
  the agent model does the work.
- **Images, optional, only if a workflow requires one:** Muse
  Image 1.0, as an image-generation tool, not as the agent.
  No image tool is used by default, and no core workflow
  requires an image to succeed.

Layout: the package lives under `src/aidon/`, standard src
layout, so `pip install -e .` installs it and
`python -m aidon.main goal "..."` runs it.

Aidon is self-hosted by design: clone it to your own computer
or server, run it there, and use the WebUI or call the API.
There is no central Aidon cloud, and your data is not sent to
one. Your memory, workdirs, and keys stay on your machine,
under your Aidon home, outside this repository.

## Core, in order

1. **Interpret** — the goal, in chat. Interview questions are
   chat turns, not cards, and they have no clock.
2. **Plan** — a plan with steps, deliverables, and criteria.
3. **Sandbox** — the plan is tested in an isolated workdir.
   Bubblewrap, when installed, is the isolated backend. A
   subprocess fallback is labeled as not isolated, never as
   isolated.
4. **Verify** — a result is verified by evidence, not by
   assertion. A deliverable must exist and be non-empty before
   any approval is asked.
5. **Approve** — decisions are cards, and they fail closed on
   timeout. No answer means rejected, never approved by default.
   An external agent can submit and read, but it cannot approve
   on the owner's behalf.
6. **Execute** — only what was approved, and only after it.
7. **Learn** — what worked is recorded, so the next goal can
   reuse it.

## Future — post-hackathon (NOT built, NOT demoed)

The items below are ideas only, for direction. None of them is
in this repository, none is in the hackathon demo, and none was
built during Oct 26 - Nov 4.

1. **Procedural 3D game-asset generation.** A game maker asking
   for a crate, chair, or sword gets no usable mesh today.
2. **Dedicated live toolset.** Live tools with real network and
   credentials, distinct from sandbox tools.
3. **Stronger sandbox backend (spike).** A Tier-2 backend behind
   the isolation seam, with bubblewrap staying default until the
   probes say otherwise.
4. **Deterministic plan validation.** A canonical plan format
   plus a cross-file validator, so plan, ledger, and
   deliverables must agree by code before approval.
