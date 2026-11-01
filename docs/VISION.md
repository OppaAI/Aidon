# Aidon's vision and mission

## Why Aidon exists

AI development often rewards models that are larger, more capable, and more expensive to run. Open-weight models make important work available to more people, but running them can still require costly hardware. At the same time, getting useful results from an AI assistant can ask users to write detailed prompts, explain the same preferences repeatedly, or design the workflow themselves.

Aidon starts from a different question: how much useful work can we help people get from the models and computers they already have?

## Vision

AI should help ordinary people accomplish practical goals, regardless of whether they have access to the largest models, powerful hardware, or specialist knowledge about prompting.

Aidon is not trying to win a race to build the biggest model. It is exploring how an agent harness can help a range of models—from roughly 30–35B parameters down through 14B, 8B, 4B, or 3B where the task allows—do useful work reliably. These sizes are an aspiration for exploration, not a guarantee that every model can handle every task.

## Mission

Aidon aims to turn everyday requests into completed, checked work. A person should be able to explain what they want in ordinary language without first becoming a prompt engineer or writing formal skills and specifications.

To support that, Aidon should:

- understand the goal and ask only for information it truly needs;
- remember useful preferences and working patterns, while letting the user inspect and manage that memory;
- prepare workflows for recurring needs and adapt them when circumstances change;
- use the model, tools, and compute available for the task rather than assuming the largest option;
- carry out work with clear approval boundaries and check whether the result meets the goal;
- learn from what worked so future tasks take less effort.

The relationship should improve over time: the user gets an assistant that better understands how they work, and Aidon gets better at helping that user accomplish their goals.

## How to judge progress

Progress should be measured by useful outcomes and the effort required to get them—not only by model benchmarks or the number of features in the repository. Useful questions include:

- Can someone describe a real task briefly and reach a good result without learning prompt engineering?
- Can a modest model complete more of the task when Aidon supplies planning, memory, tools, and verification?
- How much compute, user time, clarification, and correction did the task require?
- Does Aidon remember preferences accurately and apply them when they are relevant?
- Does a workflow become easier to reuse, adapt, and trust after it succeeds?
- Can the user see what Aidon is doing, approve consequential actions, and recover from mistakes?

These questions should guide which capabilities Aidon deepens, simplifies, makes optional, or defers. A feature is valuable when it makes practical work more accessible, reliable, or less burdensome for the user.

## Current status

This document describes Aidon's direction. The repository is experimental, and its current capabilities do not yet prove that every model size or workflow will meet this vision. The roadmap and production-readiness notes describe what is implemented and what still needs validation.
