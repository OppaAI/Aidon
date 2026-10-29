"""Binary and opaque deliverables in the model verifier.

Unreadability must never be a failure: extracted documents
show their text, images go to the model as vision, and
anything else binary is judged on properties only. When
every deliverable is opaque the model is not called at
all — code verification decides alone. Escaping names are
never read.
"""

from pathlib import Path

from src.agentic.verifier_model import ModelVerifier
from src.system.model_client import FakeClient


def _workdir(tmp_path):
    wd = tmp_path / "workdir"
    wd.mkdir()
    (wd / "letter.txt").write_text("Dear Oppa, roast beef dinner.",
                                   encoding="utf-8")
    (wd / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (wd / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    (wd / "secret.age").write_bytes(bytes([0x9F, 0x3A]) * 64)
    return wd


def test_all_opaque_skips_the_model_call(tmp_path):
    wd = _workdir(tmp_path)
    client = FakeClient("YES fine")
    verdict = ModelVerifier(client).verify(
        "goal", ["blob.bin", "secret.age"], wd, ["blob exists"])
    assert verdict["verdict"] == "unknown"
    assert client.calls == []


def test_mixed_prompt_carries_opaque_rule_not_unreadable(tmp_path):
    wd = _workdir(tmp_path)
    client = FakeClient("YES all good")
    verdict = ModelVerifier(client).verify(
        "goal", ["letter.txt", "blob.bin"], wd, ["letter contains roast"])
    assert verdict["verdict"] == "yes"
    prompt = client.calls[0][1].content
    assert "(unreadable)" not in prompt
    assert "Never answer NO solely because an opaque" in prompt
    assert "blob.bin (1024 bytes" in prompt
    assert "roast beef dinner" in prompt


def test_image_attaches_as_vision_part(tmp_path):
    wd = _workdir(tmp_path)
    client = FakeClient("YES seen")
    verdict = ModelVerifier(client).verify(
        "goal", ["pic.png"], wd, ["pic exists"])
    assert verdict["verdict"] == "yes"
    content = client.calls[0][1].content
    assert isinstance(content, list)
    assert [part.get("type") for part in content] == ["text", "image_url"]


def test_escaping_name_is_never_read(tmp_path):
    wd = _workdir(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("topsecret", encoding="utf-8")
    client = FakeClient("YES")
    verdict = ModelVerifier(client).verify(
        "goal", ["../secret.txt"], wd, [])
    # Absence is a genuine failure, so it still goes to the
    # model with its marker — but the outside file is never
    # read: its content must not appear in the prompt.
    assert verdict["verdict"] == "yes"
    assert len(client.calls) == 1
    prompt = client.calls[0][1].content
    assert "(outside workdir, not read)" in prompt
    assert "topsecret" not in prompt


def test_extracted_pdf_shows_text_not_unreadable(tmp_path):
    wd = _workdir(tmp_path)
    content = b"BT /F1 12 Tf 100 700 Td (Flated salmon menu) Tj ET"
    import zlib
    comp = zlib.compress(content)
    (wd / "menu.pdf").write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(comp)).encode()
        + b" /Filter /FlateDecode >>\nstream\n" + comp + b"\nendstream\nendobj\n")
    client = FakeClient("YES tasty")
    verdict = ModelVerifier(client).verify(
        "goal", ["menu.pdf"], wd, ["menu contains salmon"])
    assert verdict["verdict"] == "yes"
    prompt = client.calls[0][1].content
    assert "Flated salmon menu" in prompt
    assert "(unreadable)" not in prompt
