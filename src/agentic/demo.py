"""Learning demo: the before/after ablation, with a declared verdict gate.

Ported from old Aidon demo.py, same contract, adapted to the new layout
(no EvalHarness dependency, so the module is testable on its own):

1. Clean workspace, no task-specific skills.
2. Task A needs an unfamiliar capability -> Aidon researches, experiments,
   and distills a candidate skill.
3. Task B is related but different. It runs WITHOUT the skill (control)
   and WITH the skill, both arms starting from the SAME post-task-A state.
   The control arm removes ONLY the skill under measurement. Anything
   weaker measures memory in general, not the skill.
4. The comparison is printed. Success = Aidon demonstrably improves on
   task B because of a capability it created itself.

The verdict gate is declared BEFORE looking at the numbers, so the demo
cannot grade its own homework:
  0. attribution - the skilled arm must actually USE the skill
     (a SKILL_REUSED event, skill_used=True) in a successful trial;
     improvement without use is not the skill's doing;
  1. correctness - the skilled arm must succeed (majority of trials);
  2. failure -> success counts as demonstrated (the strongest signal);
  3. otherwise the skill must cut the primary metric (tokens, median of
     trials) by at least min_improvement AND must not regress the
     secondary metric (tool calls).

run_task(task, skills) -> RunResult. A task is any object with a .name,
or a plain string; a skill is any object with .name/.version/.state,
or a plain string. Tests pass stubs, the CLI wires the real GoalRunner.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable


def _name(obj) -> str:
    if isinstance(obj, str):
        return obj
    return getattr(obj, "name", str(obj))


@dataclass
class RunResult:
    success: bool
    tokens: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    detail: str = ""
    # Set by runners that can observe it: the run actually USED the
    # skill under measurement (a SKILL_REUSED event fired for it).
    # Attribution evidence for ablation verdicts.
    skill_used: bool = False

    def to_dict(self) -> dict:
        return {"success": self.success, "tokens": self.tokens,
                "tool_calls": self.tool_calls,
                "seconds": round(self.seconds, 2),
                "detail": self.detail, "skill_used": self.skill_used}


@dataclass
class BenchmarkTask:
    name: str
    goal: str = ""
    check: Callable[[dict], bool] = lambda r: bool(r.get("success"))


@dataclass
class DemoScenario:
    task_a: object  # unfamiliar capability -> creates the skill
    task_b: object  # related task -> benefits from the skill
    make_skill: Callable[[], object | None]  # run task A, distill the skill


def _aggregate(results: list) -> RunResult:
    """Majority success, median metrics, any-trial skill use."""
    n = len(results)
    successes = sum(1 for r in results if r.success)
    return RunResult(
        success=successes * 2 > n,
        tokens=int(statistics.median([r.tokens for r in results])),
        tool_calls=int(statistics.median([r.tool_calls for r in results])),
        seconds=round(statistics.median([r.seconds for r in results]), 2),
        skill_used=any(r.skill_used for r in results),
        detail=f"{successes}/{n} trials succeeded")


def run_learning_demo(scenario: DemoScenario,
                      run_task: Callable[[object, list], RunResult],
                      out=print,
                      min_improvement: float = 0.20,
                      trials: int = 3) -> dict:
    """Run the ablation. Returns the full report dict."""
    out("=== Aidon learning demo: before/after ===")
    out(f"Task A (learn): {_name(scenario.task_a)}")
    out(f"Task B (measure): {_name(scenario.task_b)}\n")

    skill = scenario.make_skill()
    if skill is None:
        out("Task A produced no skill — nothing to measure. Demo inconclusive.")
        return {"verdict": "inconclusive", "reason": "no skill created"}
    skill_name = _name(skill)
    skill_version = getattr(skill, "version", "")
    skill_state = getattr(skill, "state", "")
    out(f"Skill created by Aidon: {skill_name}"
        f"{f' (v{skill_version}' if skill_version else ''}"
        f"{f', {skill_state}' if skill_state else ''}"
        f"{')' if skill_version or skill_state else ''}\n")

    def arm(label: str, skills: list) -> tuple[RunResult, list]:
        out(f"Running task B {label} ({trials} trials)...")
        results = []
        for _ in range(trials):
            try:
                result = run_task(scenario.task_b, skills)
            except Exception as exc:  # a crashing leg is a failed leg
                result = RunResult(success=False,
                                   detail=f"{type(exc).__name__}: {exc}")
            results.append(result)
        agg = _aggregate(results)
        for i, r in enumerate(results, 1):
            out(f"  trial {i}: success={r.success} tokens={r.tokens} "
                f"tool_calls={r.tool_calls} skill_used={r.skill_used}")
        out(f"  -> aggregate: success={agg.success} tokens={agg.tokens} "
            f"tool_calls={agg.tool_calls}\n")
        return agg, results

    control, control_trials = arm("WITHOUT the skill (control)", [])
    skilled, skilled_trials = arm("WITH the skill", [skill])

    rule = (f"gate: skilled arm must use the skill and succeed "
            f"(majority of {trials} trials); then failure->success, or "
            f"median token cut >= {min_improvement:.0%} with no tool-call "
            f"regression")
    out(f"Verdict rule: {rule}")
    if not skilled.success:
        verdict = "not-demonstrated"
        reason = "skilled arm failed: the correctness gate was not met"
    elif not skilled.skill_used:
        verdict = "not-demonstrated"
        reason = ("the skill was never used in the skilled arm — any "
                  "improvement is not attributable to it")
    elif not control.success:
        verdict = "demonstrated"
        reason = "the skill turned a control failure into a success"
    else:
        cut = ((control.tokens - skilled.tokens) / control.tokens
               if control.tokens else 0.0)
        no_regression = skilled.tool_calls <= control.tool_calls
        if cut >= min_improvement and no_regression:
            verdict = "demonstrated"
            reason = (f"median tokens cut {cut:.0%} "
                      f"(>= {min_improvement:.0%} bar), tool calls "
                      f"{control.tool_calls} -> {skilled.tool_calls} "
                      f"(no regression)")
        else:
            verdict = "not-demonstrated"
            bits = []
            if cut < min_improvement:
                bits.append(f"median token cut {cut:.0%} below the "
                            f"{min_improvement:.0%} bar")
            if not no_regression:
                bits.append(f"tool calls regressed "
                            f"{control.tool_calls} -> {skilled.tool_calls}")
            reason = "; ".join(bits)
    out(f"=== verdict: {'LEARNING DEMONSTRATED' if verdict == 'demonstrated' else 'no improvement measured'} ===")
    out(f"reason: {reason}")
    out(f"caveat: medians of {trials} trials per arm — model variance "
        f"is reduced, not eliminated")
    if verdict == "demonstrated" and not control.success:
        out("The skill turned a failure into a success.")

    return {
        "verdict": verdict,
        "reason": reason,
        "rule": rule,
        "skill": skill_name,
        "control": control.to_dict(),
        "skilled": skilled.to_dict(),
        "control_trials": [r.to_dict() for r in control_trials],
        "skilled_trials": [r.to_dict() for r in skilled_trials],
    }


@dataclass
class InventToolScenario:
    task_a: object  # novel capability -> Aidon invents a tool
    task_b: object  # same shape, new data -> reuses the tool


def run_invent_tool_demo(scenario: InventToolScenario,
                         run_task_a: Callable[[object], dict],
                         run_task_b: Callable[[object, list], dict],
                         out=print) -> dict:
    """The tool-invention story, end to end.

    run_task_a(task) -> {"success": bool, "adopted": [tool names]}.
    run_task_b(task, adopted) -> {"success": bool, "reused": [tool names]}.

    Gate, declared before the numbers:
      1. task A must succeed AND adopt at least one tool;
      2. task B must succeed AND call at least one adopted tool.
    """
    out("=== Aidon invent-tool demo: research -> invent -> reuse ===")
    out(f"Task A (novel): {_name(scenario.task_a)}")
    out(f"Task B (reuse): {_name(scenario.task_b)}\n")

    out("Task A: no existing tool fits — Aidon drafts a plugin, tests it in")
    out("the sandbox, and proposes adoption...")
    a = run_task_a(scenario.task_a)
    out(f"  -> success={a['success']} adopted={a['adopted']}\n")

    rule = ("gate: task A must succeed AND adopt >= 1 tool; task B must "
            "succeed AND call >= 1 adopted tool")
    out(f"Verdict rule: {rule}")
    if not a["success"]:
        verdict, reason = "not-demonstrated", "task A failed"
        b = None
    elif not a["adopted"]:
        verdict, reason = ("not-demonstrated",
                           "task A solved but no tool was adopted")
        b = None
    else:
        out("Task B: the same shape with new data — the adopted tool should")
        out("now be callable by name, with no re-research...")
        b = run_task_b(scenario.task_b, a["adopted"])
        out(f"  -> success={b['success']} reused={b['reused']}\n")
        if not b["success"]:
            verdict, reason = "not-demonstrated", "task B failed"
        elif not b["reused"]:
            verdict, reason = ("not-demonstrated",
                               "task B solved without calling the adopted tool")
        else:
            verdict, reason = "demonstrated", "task B reused an adopted tool"
    out(f"=== verdict: {'DEMONSTRATED' if verdict == 'demonstrated' else 'no improvement measured'} ===")
    out(f"reason: {reason}")
    return {"verdict": verdict, "reason": reason, "rule": rule,
            "task_a": a, "task_b": b}
