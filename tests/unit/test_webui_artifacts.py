from src.interface.webui import WebUIServer
from src.memory.library import Library
from src.system.cli_webui import _archive_generated_images


def test_generated_images_are_copied_to_library(tmp_path):
    home = tmp_path / "home"
    workdir = home / "workspace" / "run-1"
    workdir.mkdir(parents=True)
    image = workdir / "edited.png"
    image.write_bytes(b"image bytes")
    note = workdir / "notes.txt"
    note.write_text("not an image")

    errors = _archive_generated_images(
        home, workdir, ["edited.png", "notes.txt"], "chat-1", "run-1",
        "Edit the attached photo")

    items = Library(home).list_items()
    assert errors == []
    assert len(items) == 1
    assert items[0]["name"] == "edited.png"
    assert items[0]["kind"] == "image"
    assert items[0]["conversation_id"] == "chat-1"
    assert (home / items[0]["stored_path"]).read_bytes() == b"image bytes"


def test_generated_image_archiver_refuses_files_outside_run(tmp_path):
    home = tmp_path / "home"
    workdir = home / "workspace" / "run-1"
    workdir.mkdir(parents=True)
    outside = home / "other.png"
    outside.write_bytes(b"private")

    _archive_generated_images(home, workdir, [str(outside)], "chat-1",
                              "run-1", "goal")

    assert Library(home).list_items() == []


def test_artifacts_list_exposes_uploaded_images_and_serves_them(tmp_path):
    home = tmp_path / "home"
    uploads = home / "library" / "uploads"
    uploads.mkdir(parents=True)
    image = uploads / "0123456789ab-photo.png"
    image.write_bytes(b"png bytes")
    server = WebUIServer(home, memory=None)

    data = server.handle_artifacts_list()

    assert len(data["files"]) == 1
    item = data["files"][0]
    assert item["name"] == "photo.png"
    assert item["kind"] == "image"
    assert item["url"] == "/api/files/library/uploads/0123456789ab-photo.png"
    status, mime, body = server.handle_file(item["path"])
    assert (status, mime, body) == (200, "image/png", b"png bytes")
