import json
from types import SimpleNamespace

import pytest

from src.capabilities.plugin_acceptance import test_candidate as verify_candidate


def candidate(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "plugin.json").write_text(json.dumps({"name": "sample", "tools": [{"name": "upper", "module": "tool.py", "function": "upper"}], "acceptance_tests": [["python3", "test_tool.py"]]}))
    (root / "test_tool.py").write_text(
        "from tool import upper\nassert upper({'text':'aidon'}) == 'AIDON'\n")
    (root / "tool.py").write_text("def upper(args):\n    return str(args.get('text', '')).upper()\n")
    return root


class Backend:
    name = "modal"
    allow_network = False
    test_double = True
    def run(self, command, workspace, timeout_seconds):
        assert (workspace / "test_tool.py").is_file()
        return SimpleNamespace(isolated=True, ok=True, returncode=0)


def test_evidence_binds_candidate_hash(tmp_path):
    evidence = verify_candidate(candidate(tmp_path), Backend())
    assert len(evidence["source_hash"]) == 64
    assert evidence["tests"][0]["command"] == ["python3", "test_tool.py"]


def test_acceptance_must_detect_each_tool_mutation(tmp_path):
    class MutationAwareBackend(Backend):
        test_double = False

        def run(self, command, workspace, timeout_seconds):
            if "acceptance mutation" in (workspace / "tool.py").read_text():
                return SimpleNamespace(isolated=True, ok=False, returncode=1)
            return SimpleNamespace(isolated=True, ok=True, returncode=0)

    evidence = verify_candidate(candidate(tmp_path), MutationAwareBackend())
    assert evidence["mutation_checks"] == [{"tool": "upper", "detected": True}]


def test_acceptance_that_does_not_reference_tool_is_refused(tmp_path):
    root = candidate(tmp_path)
    (root / "test_tool.py").write_text(
        "from pathlib import Path\nassert Path('plugin.json').is_file()\n")
    with pytest.raises(ValueError, match="every declared tool"):
        verify_candidate(root, Backend())


def test_isolated_network_disabled_bubblewrap_is_accepted(tmp_path):
    class Bubblewrap(Backend):
        name = "bubblewrap"

    evidence = verify_candidate(candidate(tmp_path), Bubblewrap())
    assert evidence["backend"] == "bubblewrap"


def test_bubblewrap_with_network_enabled_is_rejected(tmp_path):
    class NetworkedBubblewrap(Backend):
        name = "bubblewrap"
        allow_network = True

    with pytest.raises(ValueError, match="network-disabled"):
        verify_candidate(candidate(tmp_path), NetworkedBubblewrap())


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


@pytest.mark.parametrize("command", [
    ["true"], ["echo", "it works"], ["python3", "--version"],
    ["python3", "-c", "assert True"],
    ["python3", "-c", "assert 1 + 1 == 2"],
    ["python3", "-c", "print('test passed')"],
])
def test_rejects_obvious_noop_acceptance_commands(tmp_path, command):
    root = candidate(tmp_path)
    manifest = json.loads((root / "plugin.json").read_text())
    manifest["acceptance_tests"] = [command]
    (root / "plugin.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="no-op|behavioral assertion"):
        verify_candidate(root, Backend())


def test_rejects_missing_python_test_script(tmp_path):
    root = candidate(tmp_path)
    manifest = json.loads((root / "plugin.json").read_text())
    manifest["acceptance_tests"] = [["python3", "missing_test.py"]]
    (root / "plugin.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="missing or outside"):
        verify_candidate(root, Backend())
