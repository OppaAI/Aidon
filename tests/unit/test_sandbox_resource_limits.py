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


def test_bubblewrap_uses_private_root_and_read_only_workdir_parents(tmp_path):
    workdir = tmp_path / "home" / "work"
    sandbox = Sandbox(workdir, backend="bubblewrap")
    argv = sandbox._bwrap_argv(["true"])

    tmpfs_args = [argv[index:index + 2]
                  for index, value in enumerate(argv)
                  if value == "--tmpfs"]
    assert ["--tmpfs", "/"] in tmpfs_args
    assert ["--tmpfs", "/tmp"] in tmpfs_args
    assert ["--bind", str(workdir), str(workdir)] in [
        argv[index:index + 3] for index in range(len(argv) - 2)]
    assert ["--chmod", "0555", str(tmp_path)] in [
        argv[index:index + 3] for index in range(len(argv) - 2)]
