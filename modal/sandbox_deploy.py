import re
import modal

# ---------- Base image: built once, cached by Modal ----------
BASE_IMAGE = (
    modal.Image.from_registry("ubuntu:24.04", add_python="3.12")
    .run_commands(
        "apt-get update",
        "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y",
    )
    .apt_install("git", "curl", "build-essential", "tree")
    .run_commands("python -m pip install --upgrade pip")
    .pip_install("requests", "numpy")
)

# ---------- What Aidon is allowed to request at install time ----------
APPROVED_PIP = {"requests", "numpy", "pandas", "scipy", "matplotlib", "beautifulsoup4"}
APPROVED_APT = {"git", "curl", "ffmpeg", "tree", "jq"}
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")


def _run(sb, *cmd, timeout=60, max_chars=4000):
    p = sb.exec(*cmd, timeout=timeout)
    out, err = p.stdout.read(), p.stderr.read()
    p.wait()
    return {"stdout": out[-max_chars:], "stderr": err[-max_chars:], "returncode": p.returncode}


def build_image_with(app, base_image, pip=(), apt=()):
    """Phase 1: networked sandbox installs only approved packages, then snapshots."""
    for name in list(pip) + list(apt):
        if not SAFE_NAME.match(name):
            raise ValueError(f"Invalid package name: {name!r}")
    bad_pip = set(pip) - APPROVED_PIP
    bad_apt = set(apt) - APPROVED_APT
    if bad_pip or bad_apt:
        raise ValueError(f"Not on the approved list: {sorted(bad_pip | bad_apt)}")

    setup = modal.Sandbox.create(
        app=app, image=base_image, timeout=900, block_network=False
    )
    try:
        if apt:
            _run(setup, "apt-get", "update", timeout=300)
            r = _run(setup, "env", "DEBIAN_FRONTEND=noninteractive",
                     "apt-get", "install", "-y", *apt, timeout=600)
            if r["returncode"] != 0:
                raise RuntimeError(r["stderr"])
        if pip:
            r = _run(setup, "python", "-m", "pip", "install", *pip, timeout=600)
            if r["returncode"] != 0:
                raise RuntimeError(r["stderr"])
        return setup.snapshot_filesystem()   # returns a modal.Image
    finally:
        setup.terminate()


class AidonSandbox:
    """Phase 2: the sandbox Aidon actually runs code in."""

    def __init__(self, app, image=BASE_IMAGE, timeout=600, block_network=True,
                 volume_name="aidon-data"):
        volume = modal.Volume.from_name(volume_name, create_if_missing=True)
        self.sb = modal.Sandbox.create(
            app=app,
            image=image,
            timeout=timeout,
            block_network=block_network,
            volumes={"/data": volume},      # persistent files survive sandbox restarts
        )

    def run(self, *cmd, timeout=60, max_chars=4000):
        return _run(self.sb, *cmd, timeout=timeout, max_chars=max_chars)

    def write_file(self, path, content):
        data = content.encode() if isinstance(content, str) else content
        p = self.sb.exec("sh", "-c",
                         'mkdir -p "$(dirname "$1")" && cat > "$1"', "_", path)
        p.stdin.write(data)
        p.stdin.write_eof()
        p.stdin.drain()
        p.wait()
        if p.returncode != 0:
            raise RuntimeError(p.stderr.read())

    def read_file(self, path, max_chars=20000):
        r = self.run("cat", path, max_chars=max_chars)
        if r["returncode"] != 0:
            raise FileNotFoundError(r["stderr"])
        return r["stdout"]

    def flush(self):
        """Force pending Volume writes to be persisted."""
        return self.run("sync", "/data/")

    def close(self):
        try:
            self.flush()
        except Exception:
            pass                      # sandbox may already be dead; still terminate
        finally:
            self.sb.terminate()


if __name__ == "__main__":
    print("starting...", flush=True)
    app = modal.App.lookup("aidon-sandbox", create_if_missing=True)

    with modal.enable_output():
        image = build_image_with(app, BASE_IMAGE, pip=["pandas"], apt=["jq"])
        box = AidonSandbox(app, image=image, block_network=True)

    try:
        box.write_file("/tmp/hi.py", "print(2 + 2)")
        print(box.run("python", "/tmp/hi.py"))
        print(box.run("python", "-c", "import pandas; print(pandas.__version__)"))
        print(box.run("jq", "--version"))

        box.write_file("/data/notes.txt", "hello from aidon")
        print(box.read_file("/data/notes.txt"))

        print(box.run("curl", "-sS", "-m", "5", "https://example.com"))

        try:
            build_image_with(app, BASE_IMAGE, pip=["some-random-package"])
        except ValueError as e:
            print("Rejected as expected:", e)
    finally:
        box.close()
