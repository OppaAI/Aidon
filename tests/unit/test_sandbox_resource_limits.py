import sys
import time

from src.capabilities.sandbox import Sandbox


def test_output_is_bounded(tmp_path):
    sandbox = Sandbox(tmp_path, backend="disabled", require_isolation=False)
    result = sandbox.run([sys.executable, "-c", "import sys; sys.stdout.write('x'*3000000)"])
    assert result.ok
    assert len(result.stdout) < 1100000
    assert result.stdout.endswith("[output truncated]")


def test_timeout_kills_child_process(tmp_path):
    sandbox = Sandbox(tmp_path, backend="disabled", require_isolation=False, timeout_seconds=0.2)
    command = ("import subprocess,sys,time; "
               "subprocess.Popen([sys.executable,'-c',\"import time; time.sleep(1); open('escaped','w').write('x')\"]); "
               "time.sleep(10)")
    result = sandbox.run([sys.executable, "-c", command])
    assert result.returncode == 124
    time.sleep(1.2)
    assert not (tmp_path / "escaped").exists()
