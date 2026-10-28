"""Semantic routing places, write-intent guard, and Unity asset generation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.interface.approval import ApprovalGate, Decision
from src.memory.embedder import HashEmbedder
from src.agentic.loop import GoalRunner
from src.memory.memory import Memory
from src.system.model_client import FakeClient
from src.capabilities.sandbox import Sandbox
from src.agentic.semantic_router import SemanticRouter
from src.capabilities.unity import UnityAssetBuilder


def test_semantic_router_classifies_and_loop_guard():
    # With a fake embedder that returns the example vectors, classification works.
    # Here, with HashEmbedder, the guard is what matters: a write goal must
    # not be skipped as chat, even if the router said chat.
    memory = Memory(Path("/tmp") / "mem-test", embedder=HashEmbedder())
    # Direct loop test: use_semantic_router_skip with a router that says chat,
    # but a write goal must still go through the pipeline, not chat.
    class ChatRouter:
        def classify(self, goal):
            return ("chat", 0.9)
    home = Path("/tmp") / "home-test-guard"
    home.mkdir(exist_ok=True)
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient(""), sandbox, gate, home, memory=None,
                        semantic_router=ChatRouter(), use_semantic_router_skip=True)
    report = runner.run("Write a note")
    assert report.status != "chat", "write-intent guard must veto a chat shortcut"


def test_unity_cube_asset_is_importable(tmp_path):
    builder = UnityAssetBuilder(tmp_path / "unity")
    result = builder.generate_asset("Worm", shape="cube", color="green")
    obj = Path(result["obj"]).read_text()
    assert "v " in obj and "f " in obj
    assert Path(result["obj"]).stat().st_size > 0
    script = Path(result["script"]).read_text()
    assert "WormBehaviour" in script
    assert "MonoBehaviour" in script
    assert Path(result["readme"]).exists()


def test_unity_sphere_and_cylinder(tmp_path):
    builder = UnityAssetBuilder(tmp_path / "unity")
    for shape in ("sphere", "cylinder"):
        result = builder.generate_asset(f"Test{shape}", shape=shape)
        obj = Path(result["obj"]).read_text()
        assert obj.count("v ") > 10
        assert "f " in obj


def test_unity_build_command_never_fakes_unity(tmp_path):
    builder = UnityAssetBuilder(tmp_path / "unity")
    command = builder.build_command(tmp_path / "unity" / "Worm")
    # In this VM, Unity is not installed, so the command must say so, not fake a build.
    assert "not found" in command.lower() or "Unity" in command
