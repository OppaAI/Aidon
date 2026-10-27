"""Document generation, store, and ebook, old Aidon docgen/docstore/documents/ebook concepts, rebuilt.

docgen: build a Markdown document from a title and sections, code only, no model call.
docstore: save, list, and read documents under <home>/documents/, with safe filenames.
ebook: combine a list of chapter documents into one Markdown ebook file, in chapter order,
with a table of contents, so a multi-chapter document can be delivered as one file.
No PDF generation here: Markdown is the deliverable, PDF is a separate climb, as in the demo.
"""

from __future__ import annotations

import json
import re
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
