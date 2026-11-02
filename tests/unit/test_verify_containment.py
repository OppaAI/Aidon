"""Path containment and binary extraction in the code verifier.

A model-supplied name must never read outside the workdir:
".." escapes, absolute paths outside the root, and symlinks
pointing out all resolve to a missing file. Binary
deliverables get stdlib text extraction (PDF streams, office
XML parts) so "contains" works on them; anything else binary
is opaque and fails closed with an honest message, never a
claim that the needle is absent.
"""

import zipfile
import zlib
from pathlib import Path

from src.agentic.verify import (
    check_criterion,
    read_deliverable,
    resolve_in_workdir,
    requested_exact_content_criteria,
    verify_plan,
)


def _workdir(tmp_path):
    wd = tmp_path / "workdir"
    wd.mkdir()
    (wd / "a.txt").write_text("hello world", encoding="utf-8")
    (wd / "sub").mkdir()
    (wd / "sub" / "b.txt").write_text("bee", encoding="utf-8")
    return wd


def test_traversal_cannot_read_outside(tmp_path):
    wd = _workdir(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("topsecret", encoding="utf-8")
    assert resolve_in_workdir(wd, "../secret.txt") is None
    assert check_criterion(wd, "../secret.txt contains topsecret")[0] is False
    assert check_criterion(wd, "../secret.txt exists")[0] is False
    assert check_criterion(wd, "sub/../../secret.txt exists")[0] is False


def test_absolute_path_outside_is_missing(tmp_path):
    wd = _workdir(tmp_path)
    assert check_criterion(wd, "/etc/passwd exists")[0] is False
    assert resolve_in_workdir(wd, "/etc/passwd") is None


def test_absolute_path_inside_still_works(tmp_path):
    wd = _workdir(tmp_path)
    assert check_criterion(wd, f"{wd}/a.txt exists")[0] is True


def test_symlink_escape_is_missing(tmp_path):
    wd = _workdir(tmp_path)
    (wd / "evil").symlink_to("/etc")
    assert check_criterion(wd, "evil/passwd exists")[0] is False
    assert resolve_in_workdir(wd, "evil/passwd") is None


def test_symlink_inside_is_allowed(tmp_path):
    wd = _workdir(tmp_path)
    (wd / "link.txt").symlink_to(wd / "a.txt")
    assert check_criterion(wd, "link.txt contains hello")[0] is True


def test_workspace_folder_descriptor_still_strips(tmp_path):
    wd = _workdir(tmp_path)
    assert check_criterion(wd, "PDF file a.txt exists in workspace folder")[0] is True


def test_plain_pdf_reads_as_text_and_contains_works(tmp_path):
    wd = _workdir(tmp_path)
    (wd / "doc.pdf").write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Length 44 >>\nstream\n"
        b"BT /F1 12 Tf 100 700 Td (Roast beef dinner) Tj ET\nendstream\nendobj\n")
    assert check_criterion(wd, "doc.pdf contains Roast beef")[0] is True
    assert check_criterion(wd, "doc.pdf contains turkey")[0] is False


def test_flate_pdf_extracts(tmp_path):
    wd = _workdir(tmp_path)
    content = b"BT /F1 12 Tf 100 700 Td (Flated salmon menu) Tj ET"
    comp = zlib.compress(content)
    (wd / "doc.pdf").write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(comp)).encode()
        + b" /Filter /FlateDecode >>\nstream\n" + comp + b"\nendstream\nendobj\n")
    info = read_deliverable(wd / "doc.pdf")
    assert info["kind"] == "extracted"
    assert "Flated salmon menu" in info["shown"]
    assert check_criterion(wd, "doc.pdf contains salmon")[0] is True


def test_docx_extracts(tmp_path):
    wd = _workdir(tmp_path)
    with zipfile.ZipFile(wd / "doc.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="urn:x"><w:body>'
            "<w:p><w:r><w:t>Chicken stew</w:t></w:r></w:p>"
            "</w:body></w:document>")
    info = read_deliverable(wd / "doc.docx")
    assert info["kind"] == "extracted"
    assert "Chicken stew" in info["shown"]


def test_opaque_binary_is_not_searchable(tmp_path):
    wd = _workdir(tmp_path)
    (wd / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (wd / "secret.age").write_bytes(bytes([0x9F, 0x3A]) * 64)
    assert read_deliverable(wd / "blob.bin")["kind"] == "opaque"
    assert read_deliverable(wd / "secret.age")["kind"] == "opaque"
    ok, message = check_criterion(wd, "secret.age contains password")
    assert ok is False
    assert "not searchable" in message


def test_verify_plan_rejects_escaping_deliverable(tmp_path):
    wd = _workdir(tmp_path)
    ok, failures, _ = verify_plan(wd, ["../secret.txt"], [])
    assert ok is False
    assert any("missing deliverable" in f for f in failures)


def test_explicit_exact_content_goal_creates_code_criterion():
    goal = ("Create scheduler-recovery.txt containing exactly recovered-ok, "
            "then verify its contents. Do not use external services.")
    assert requested_exact_content_criteria(
        goal, ["scheduler-recovery.txt"]
    ) == ["scheduler-recovery.txt contains exactly recovered-ok"]


def test_exact_content_goal_enforces_filename_omitted_by_planner(tmp_path):
    goal = ("Create exact-content-check.txt in your workspace containing "
            "exactly cedar-742. Then verify its contents.")
    criteria = requested_exact_content_criteria(goal, ["exact_test.py"])
    assert criteria == ["exact-content-check.txt contains exactly cedar-742"]

    wd = _workdir(tmp_path)
    (wd / "exact_test.py").write_text("some script", encoding="utf-8")
    ok, failures, _ = verify_plan(wd, ["exact_test.py"], criteria)
    assert ok is False
    assert any("exact-content-check.txt missing" in failure
               for failure in failures)


def test_exact_content_criterion_rejects_wrong_nonempty_file(tmp_path):
    wd = _workdir(tmp_path)
    (wd / "scheduler-recovery.txt").write_text("scheduler-ok", encoding="utf-8")
    criterion = "scheduler-recovery.txt contains exactly recovered-ok"
    ok, message = check_criterion(wd, criterion)
    assert ok is False
    assert "content mismatch" in message
    ok, failures, _ = verify_plan(wd, ["scheduler-recovery.txt"], [criterion])
    assert ok is False
    assert any("content mismatch" in failure for failure in failures)


def test_exact_content_criterion_accepts_requested_text_with_newline(tmp_path):
    wd = _workdir(tmp_path)
    (wd / "scheduler-recovery.txt").write_text("recovered-ok\n", encoding="utf-8")
    ok, _ = check_criterion(
        wd, "scheduler-recovery.txt contains exactly recovered-ok")
    assert ok is True


def test_exact_content_quoted_literal_is_supported():
    goal = 'Write result.txt whose contents are exactly "hello, world!".'
    assert requested_exact_content_criteria(goal, ["result.txt"]) == [
        "result.txt contains exactly hello, world!"
    ]
