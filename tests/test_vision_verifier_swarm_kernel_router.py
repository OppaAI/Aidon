"""Vision, Spark verifier, swarm, kernel, semantic router.

Vision is tested by payload shape, not by a live Spark
call: image parts are data URLs, text parts are text.
Spark 1.3 vision itself is a model property, verified
by web sources in chat, not by a key here.
Model verifier is second opinion only: code passes,
model unknown does not fail, model no triggers repair.
Swarm is decided by the model: SINGLE means no swarm,
two or more subgoals means parallel, in order.
Kernel blocks non-seed tools, semantic router classifies
by embedding, with no LLM call, low confidence to agentic.
"""

from src.kernel import kernel_allowed, kernel_filter_tools
from src.loop import GoalRunner
from src.model_client import FakeClient, Message, image_message
from src.router import RoutedClient
from src.semantic_router import SemanticRouter
from src.swarm import decide_subgoals, run_swarm
from src.verifier_model import ModelVerifier
from src.approval import ApprovalGate
from src.sandbox import Sandbox


def test_image_message_payload():
    msg = image_message("What is in this image?", [(b"\x89PNG", "image/png")])
    assert msg.content[0]["type"] == "text"
    assert msg.content[1]["type"] == "image_url"
    assert msg.content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert image_message("hi", []).content == "hi"


def test_model_verifier_yes_no_unknown():
    assert ModelVerifier(FakeClient("YES, complete")).verify("g", ["a.md"], "/tmp")["verdict"] == "yes"
    assert ModelVerifier(FakeClient("NO, missing file")).verify("g", ["a.md"], "/tmp")["verdict"] == "no"
    assert ModelVerifier(FakeClient("")).verify("g", ["a.md"], "/tmp")["verdict"] == "unknown"


def test_swarm_decided_by_model():
    assert decide_subgoals("Do one thing", FakeClient("SINGLE")) == []
    subgoals = decide_subgoals("Do two things", FakeClient("1. Research A\n2. Research B"))
    assert subgoals == ["Research A", "Research B"]
    result = run_swarm("Do two things", FakeClient("1. Research A\n2. Research B"),
                       lambda subgoal: f"Done {subgoal}")
    assert result["swarmed"] is True
    assert result["results"] == ["Done Research A", "Done Research B"]
    single = run_swarm("Do one thing", FakeClient("SINGLE"), lambda goal: "single done")
    assert single["swarmed"] is False and single["merged"] == "single done"


def test_kernel_blocks_non_seed():
    assert kernel_allowed("write", True) is True
    assert kernel_allowed("calculate", True) is False
    assert kernel_allowed("calculate", False) is True
    assert kernel_filter_tools(["write", "calculate", "shell"], True) == ["write", "shell"]


class FakeEmbedder:
    def embed_dense(self, texts):
        # chat example "hi" -> [1,0], agentic "run ls" -> [0,1], else [0.5,0.5]
        out = []
        for text in texts:
            if text in ("hi", "hello"):
                out.append([1.0, 0.0])
            elif "run ls" in text or "research" in text:
                out.append([0.0, 1.0])
            else:
                out.append([0.5, 0.5])
        return out


def test_semantic_router_chat_vs_agentic():
    router = SemanticRouter(FakeEmbedder(), example_vectors={
        "chat": [[1.0, 0.0]], "webchat": [[0.7, 0.7]],
        "agentic": [[0.0, 1.0]], "artifact": [[0.2, 0.8]]})
    assert router.classify("hi")[0] == "chat"
    assert router.classify("run ls in my home directory")[0] == "agentic"


def test_phase_router_tier():
    fast = FakeClient("fast")
    strong = FakeClient("strong")
    routed = RoutedClient(fast, strong, fast_phases={"interpret", "learn", "respond"})
    assert routed.chat_phase("interpret", [Message("user", "hi")]) == "fast"
    assert routed.chat_phase("plan", [Message("user", "hi")]) == "strong"
    assert routed.stats() == {"fast_calls": 1, "strong_calls": 1}


def test_loop_model_verifier_no_triggers_repair(tmp_path):
    # Code verifier passes, model verifier says NO, so the
    # run should repair, not approve, and with a FakeClient
    # plan it will fail after attempts, not go live.
    from src.approval import Decision
    client = FakeClient('{"steps": [{"tool": "write", "args": {"file": "a.md", "text": "hello"}, "deliverable": "a.md"}], "deliverables": ["a.md"], "criteria": []}')
    # FakeClient always returns the plan, so model verifier
    # needs a separate client that says NO.
    verifier = ModelVerifier(FakeClient("NO, a.md is incomplete"))
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    sandbox = Sandbox(tmp_path / "work", timeout_seconds=10)
    runner = GoalRunner(client, sandbox, gate, tmp_path / "home", model_verifier=verifier)
    report = runner.run("Write a.md")
    # Repair attempts exhausted, or model verifier kept failing,
    # either way the run must not be done with a NO verifier.
    assert report.status != "done" or report.verified is False
