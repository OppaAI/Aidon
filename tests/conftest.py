import shutil
import subprocess

import pytest


@pytest.fixture
def require_bubblewrap():
    """Real-isolation tests require functioning namespaces, not just a binary."""
    binary = shutil.which("bwrap")
    if not binary:
        pytest.skip("bubblewrap is not installed")
    probe = subprocess.run(
        [binary, "--unshare-net", "--unshare-pid", "--ro-bind", "/", "/", "true"],
        capture_output=True, timeout=10)
    if probe.returncode:
        pytest.skip("runner does not permit bubblewrap namespaces")
