"""Modal backend: workspace transfer over a fake Modal sandbox,
failure mapping, and factory wiring."""

import base64
import io
import shutil
import subprocess
import tarfile

import pytest

from src.capabilities.execution import (
    ModalBackend, make_backend)


class _Stream:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _Stdin:
    def __init__(self, box):
        self.box = box

    def write(self, data):
        self.box.append(data)

    def write_eof(self):
        pass

    def drain(self):
        pass


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0, stdin_box=None):
        self.stdout = _Stream(stdout)
        self.stderr = _Stream(stderr)
        self.returncode = returncode
        self.stdin = _Stdin(stdin_box if stdin_box is not None else [])

    def wait(self):
        return self.returncode


class _RemoteDir:
    """A fake Modal sandbox: a real directory behind exec calls."""

    def __init__(self, root):
        self.root = root
        self.terminated = False
        self.fail_sync_up = False
        self.filesystem = _RemoteFilesystem(self)

    def exec(self, *cmd, timeout=None):
        if cmd[:1] == ("mkdir",):
            return _Proc(returncode=0)
        joined = " ".join(cmd)
        if "base64 -d | tar" in joined:
            if self.fail_sync_up:
                return _Proc(stderr="bad tar", returncode=1,
                             stdin_box=[])
            box = []
            proc = _Proc(returncode=0, stdin_box=box)
            proc._box = box
            # The backend writes stdin after exec returns, so the
            # extraction must happen at wait() time, not here.
            orig_wait = proc.wait

            def _wait():
                data = base64.b64decode(b"".join(box))
                with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                    tf.extractall(path=str(self.root), filter="data")
                return orig_wait()

            proc.wait = _wait
            return proc
        if "tar -cf" in joined and "base64" in joined:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                tf.add(str(self.root), arcname=".")
            return _Proc(stdout=base64.b64encode(buf.getvalue()).decode(),
                         returncode=0)
        if cmd[:2] == ("sh", "-c"):
            script = cmd[2]
            # The backend prefixes cd /work; the fake remote IS the
            # workdir, so strip that prefix before running locally.
            if script.startswith("cd /work && "):
                script = script[len("cd /work && "):]
            proc = subprocess.run(script, shell=True, cwd=str(self.root),
                                  capture_output=True, text=True)
            return _Proc(stdout=proc.stdout, stderr=proc.stderr,
                         returncode=proc.returncode)
        return _Proc(stderr="unknown fake command", returncode=1)

    def terminate(self):
        self.terminated = True


class _RemoteFilesystem:
    def __init__(self, remote):
        self.remote = remote

    def list_files(self, remote_path):
        base = self.remote.root / remote_path.removeprefix("/work").lstrip("/")
        return [_RemoteFileInfo(path) for path in base.iterdir()]

    def copy_to_local(self, remote_path, local_path):
        source = self.remote.root / remote_path.removeprefix("/work").lstrip("/")
        shutil.copyfile(source, local_path)


class _RemoteFileInfo:
    def __init__(self, path):
        self.name = path.name
        self._path = path

    def is_dir(self):
        return self._path.is_dir()

    def is_file(self):
        return self._path.is_file()


def _factory(remote):
    def _make(app_name, allow_network, image=None):
        assert app_name == "aidon-sandbox"
        assert allow_network is False
        assert image is None, "default backend uses the base image"
        return remote
    return _make


def test_round_trip_writes_back_new_files(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("hello")
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    backend = ModalBackend(sandbox_factory=_factory(_RemoteDir(remote_dir)))
    result = backend.run(
        ["sh", "-c", "mkdir -p nested && printf world > nested/b.txt"],
        work)
    assert result.returncode == 0
    assert result.isolated is True
    assert result.backend == "modal"
    assert (work / "a.txt").read_text() == "hello"
    assert (work / "nested" / "b.txt").read_text() == "world"


def test_command_failure_still_syncs_back(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("hello")
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    backend = ModalBackend(sandbox_factory=_factory(_RemoteDir(remote_dir)))
    result = backend.run(["sh", "-c", "echo partial > c.txt; exit 3"], work)
    assert result.returncode == 3
    assert (work / "c.txt").read_text().strip() == "partial"


def test_sync_up_failure_is_a_failed_result_not_a_crash(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    remote = _RemoteDir(tmp_path / "remote")
    remote.fail_sync_up = True
    backend = ModalBackend(sandbox_factory=_factory(remote))
    result = backend.run(["echo", "hi"], work)
    assert result.returncode == 1
    assert "sync to Modal" in result.stderr


def test_missing_modal_package_raises_fail_closed(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "modal", None)
    # A None entry makes `import modal` raise ImportError.
    backend = ModalBackend()
    with pytest.raises(RuntimeError, match="modal package"):
        backend.run(["echo", "hi"], tmp_path / "work")


def test_cleanup_terminates_sandbox(tmp_path):
    remote = _RemoteDir(tmp_path / "remote")
    backend = ModalBackend(sandbox_factory=_factory(remote))
    backend.run(["echo", "hi"], tmp_path / "work")
    backend.cleanup()
    assert remote.terminated is True


def test_make_backend_modal_branch(tmp_path):
    backend = make_backend("modal", None)
    assert isinstance(backend, ModalBackend)
    assert backend.app_name == "aidon-sandbox"
    backend2 = make_backend("modal", None, modal_app="custom-app")
    assert backend2.app_name == "custom-app"


def _make_tar(spec):
    """spec: list of (name, data-or-None, kind, linkname-or-None)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data, kind, linkname in spec:
            info = tarfile.TarInfo(name)
            if kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
                tf.addfile(info)
            elif kind == "lnk":
                info.type = tarfile.LNKTYPE
                info.linkname = linkname
                tf.addfile(info)
            else:
                raw = data or b""
                info.size = len(raw)
                tf.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def test_untar_rejects_escaping_members(tmp_path):
    backend = ModalBackend()
    cases = [
        [("/tmp/evil.txt", b"x", "file", None)],
        [("../evil.txt", b"x", "file", None)],
        [("sub/../../evil.txt", b"x", "file", None)],
        [("link", None, "sym", "/tmp/evil")],
        [("link", None, "sym", "../evil")],
        [("link", None, "lnk", "/tmp/evil")],
    ]
    for spec in cases:
        with pytest.raises((ValueError, tarfile.TarError)):
            backend._untar_bytes(_make_tar(spec), tmp_path)


def test_untar_allows_links_inside_workspace(tmp_path):
    backend = ModalBackend()
    data = _make_tar([("real.txt", b"hello", "file", None),
                      ("link", None, "sym", "real.txt")])
    backend._untar_bytes(data, tmp_path)
    assert (tmp_path / "real.txt").read_text() == "hello"
    assert (tmp_path / "link").read_text() == "hello"


def test_backend_passes_custom_image_to_factory(tmp_path):
    seen = {}
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()

    def _make(app_name, allow_network, image=None):
        seen["image"] = image
        return _RemoteDir(remote_dir)

    backend = ModalBackend(sandbox_factory=_make, image="custom-image")
    backend.run(["echo", "hi"], tmp_path / "work")
    assert seen["image"] == "custom-image"
