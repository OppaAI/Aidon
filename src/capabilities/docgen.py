"""Document generation, store, and ebook, Aidon docgen/docstore/documents/ebook concepts, rebuilt.

docgen: build a Markdown document from a title and sections, code only, no model call.
docstore: save, list, and read documents under <home>/documents/, with safe filenames.
ebook: combine a list of chapter documents into one Markdown ebook file, in chapter order,
with a table of contents, so a multi-chapter document can be delivered as one file.
No PDF generation here: Markdown is the deliverable, PDF is a separate climb, as in the demo.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path


def _safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name or "document")[:80]
    return safe or "document"


def generate_document(title: str, sections: list[dict]) -> str:
    lines = [f"# {title}", ""]
    for section in sections:
        lines.append(f"## {section.get('heading', '')}")
        lines.append("")
        lines.append(section.get("body", ""))
        lines.append("")
    return "\n".join(lines)


def write_document_file(title: str, sections: list[dict], output) -> str:
    """Write generated document content to Markdown or a small valid PDF."""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() != ".pdf":
        path.write_text(generate_document(title, sections), encoding="utf-8")
        return str(path)

    lines = [title, ""]
    for section in sections:
        lines.extend([str(section.get("heading", "")), ""])
        for paragraph in str(section.get("body", "")).splitlines() or [""]:
            lines.extend(textwrap.wrap(paragraph, width=92) or [""])
        lines.append("")

    # Build simple text-only PDF pages without requiring a heavy PDF writer.
    pages = [lines[i:i + 40] for i in range(0, len(lines), 40)] or [[""]]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,  # filled with page references below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_refs = []
    for page_lines in pages:
        stream_lines = ["BT", "/F1 16 Tf", "54 770 Td"]
        for index, line in enumerate(page_lines):
            if index == 1:
                stream_lines.append("/F1 11 Tf")
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream_lines.append(f"({escaped}) Tj")
            stream_lines.append("0 -18 Td")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", "replace")
        content_index = len(objects) + 1
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        page_index = len(objects) + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_index} 0 R >>".encode())
        page_refs.append(f"{page_index} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>".encode()

    output_bytes = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output_bytes))
        output_bytes.extend(f"{number} 0 obj\n".encode())
        output_bytes.extend(obj)
        output_bytes.extend(b"\nendobj\n")
    xref = len(output_bytes)
    output_bytes.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output_bytes.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output_bytes.extend(f"{offset:010d} 00000 n \n".encode())
    output_bytes.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(output_bytes)
    return str(path)


class DocStore:
    def __init__(self, home):
        self.dir = Path(home) / "documents"
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, content: str) -> Path:
        path = self.dir / f"{_safe_name(name)}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def list(self) -> list:
        return sorted(p.name for p in self.dir.glob("*.md"))

    def read(self, name: str) -> str | None:
        path = self.dir / f"{_safe_name(name)}.md"
        return path.read_text(encoding="utf-8") if path.is_file() else None


def build_ebook(title: str, chapters: list[dict]) -> str:
    lines = [f"# {title}", "", "## Contents", ""]
    for index, chapter in enumerate(chapters, start=1):
        lines.append(f"{index}. {chapter.get('title', '')}")
    lines.append("")
    for index, chapter in enumerate(chapters, start=1):
        lines.append(f"## Chapter {index}: {chapter.get('title', '')}")
        lines.append("")
        lines.append(chapter.get("body", ""))
        lines.append("")
    return "\n".join(lines)
