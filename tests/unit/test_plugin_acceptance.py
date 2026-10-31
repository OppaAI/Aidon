import json
from types import SimpleNamespace

import pytest

from src.capabilities.plugin_acceptance import test_candidate as verify_candidate


def candidate(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "plugin.json").write_text(json.dumps({"name": "sample", "acceptance_tests": [["python3", "test_tool.py"]]}))
    (root / "test_tool.py").write_text("assert 1 + 1 == 2\n")
    return root


class Backend:
    name = "modal"
    allow_network = False
    def run(self, command, workspace, timeout_seconds):
        assert (workspace / "test_tool.py").is_file()
        return SimpleNamespace(isolated=True, ok=True, returncode=0)


def test_evidence_binds_candidate_hash(tmp_path):
    evidence = verify_candidate(candidate(tmp_path), Backend())
    assert len(evidence["source_hash"]) == 64
    assert evidence["tests"][0]["command"] == ["python3", "test_tool.py"]


def test_rejects_failed_unisolated_and_modified_tests(tmp_path):
    root = candidate(tmp_path)
    class Bad(Backend):
        def run(self, command, workspace, timeout_seconds):
            return SimpleNamespace(isolated=False, ok=True, returncode=0)
    with pytest.raises(ValueError, match="isolation"):
        verify_candidate(root, Bad())
    class Changing(Backend):
        def run(self, command, workspace, timeout_seconds):
            (workspace / "test_tool.py").write_text("changed")
            return SimpleNamespace(isolated=True, ok=True, returncode=0)
    with pytest.raises(ValueError, match="modified"):
        verify_candidate(root, Changing())
