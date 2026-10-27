import pytest

from src.memory.ingest import ingest_folder, remove_folder
from src.memory.memory import InMemoryStore, Memory


def _memory(tmp_path):
    return Memory(tmp_path, store=InMemoryStore())


def _folder(tmp_path, name="notes"):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "notes.md").write_text(
        "# Notes\n\nSome short notes about the project.\n")
    (folder / "code.py").write_text(
        "def hello():\n    return 'hi'\n")
    (folder / "binary.bin").write_bytes(bytes(range(256)) * 4)
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("[core]\n")
    return folder


def _knowledge_points(memory):
    return list(memory.iter_points("knowledge"))


def test_ingest_folder(tmp_path):
    memory = _memory(tmp_path)
    folder = _folder(tmp_path)
    result = ingest_folder(tmp_path, folder, memory=memory)
    assert result["files"] == 2
    assert result["chunks"] >= 2
    assert result["skipped"] == ["binary.bin"]
    assert result["dry_run"] is False
    points = _knowledge_points(memory)
    assert len(points) == result["chunks"]
    doc_ids = {p.payload["doc_id"] for p in points}
    assert any("notes.md" in d for d in doc_ids)
    assert any("code.py" in d for d in doc_ids)
    assert all(p.payload["kind"] == "folder" for p in points)
    assert all(p.payload["source_dir"] == str(folder.resolve()) for p in points)
    assert not any(".git" in p.payload["title"] for p in points)


def test_reingest_shrinks_and_removes_stale(tmp_path):
    memory = _memory(tmp_path)
    folder = tmp_path / "docs"
    folder.mkdir()
    long_text = ("word " * 400 + "\n\n") * 3
    (folder / "long.md").write_text(long_text)
    first = ingest_folder(tmp_path, folder, memory=memory)
    assert first["chunks"] == 6
    (folder / "long.md").write_text("short now\n")
    second = ingest_folder(tmp_path, folder, memory=memory)
    assert second["chunks"] == 1
    assert second["removed_stale"] == 5
    assert second["stale"] == []
    points = _knowledge_points(memory)
    assert len(points) == 1
    assert points[0].payload["doc_id"].endswith(":::0")


def test_reingest_unchanged_is_idempotent(tmp_path):
    memory = _memory(tmp_path)
    folder = _folder(tmp_path)
    first = ingest_folder(tmp_path, folder, memory=memory)
    second = ingest_folder(tmp_path, folder, memory=memory)
    assert second["removed_stale"] == 0
    assert len(_knowledge_points(memory)) == first["chunks"]


def test_dry_run_writes_nothing(tmp_path):
    memory = _memory(tmp_path)
    folder = _folder(tmp_path)
    result = ingest_folder(tmp_path, folder, memory=memory, dry_run=True)
    assert result["files"] == 2
    assert result["chunks"] >= 2
    assert result["dry_run"] is True
    assert result["removed_stale"] == 0
    assert _knowledge_points(memory) == []


def test_missing_folder_raises(tmp_path):
    memory = _memory(tmp_path)
    with pytest.raises(FileNotFoundError):
        ingest_folder(tmp_path, tmp_path / "nope", memory=memory)


def test_remove_folder_leaves_other_folders(tmp_path):
    memory = _memory(tmp_path)
    folder_a = _folder(tmp_path, "a")
    folder_b = _folder(tmp_path, "b")
    ingest_folder(tmp_path, folder_a, memory=memory)
    ingest_folder(tmp_path, folder_b, memory=memory)
    total = len(_knowledge_points(memory))
    removed = remove_folder(tmp_path, folder_a, memory=memory)
    assert removed > 0
    remaining = _knowledge_points(memory)
    assert len(remaining) == total - removed
    assert all(p.payload["source_dir"] == str(folder_b.resolve()) for p in remaining)
    assert remove_folder(tmp_path, folder_a, memory=memory) == 0
