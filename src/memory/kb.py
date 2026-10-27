"""Knowledge base (KB) for RAG, Qdrant knowledge collection.

The user has a knowledge/ path under the Aidon home. They can
dump files there directly, or import files in the WebUI. WebUI
import duplicates the attached file into knowledge/ first, so
the file on disk and the file in the KB are the same file, not
a temporary upload that disappears. Then the file is auto-
ingested into the Qdrant knowledge collection for RAG.

Ingestion, in order:
1. Copy the file into knowledge/ if it is not already there.
2. Chunk it: text and markdown by paragraphs, max 1000 chars
   per chunk, 100 chars overlap, so a chunk is a readable unit
   for retrieval, not a whole file and not a single line.
   Images are one point, not chunks: image vector + file
   name + knowledge path, so text recall can refer to them.
3. Embed each chunk and upsert it to the knowledge collection,
   with payload: file name, chunk index, file hash, and text.
4. Track file hashes in a manifest, so a later auto-ingest
   only ingests new or changed files, and deletes chunks for
   files that were removed from knowledge/.

Auto-ingest is triggered on import and on scan: ``scan()``
walks knowledge/, ingests new or changed files, and prunes
chunks for deleted files. It does not re-ingest unchanged
files, and it never deletes the user's files, only their KB
chunks.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import re

from .library import is_image
from .memory import Memory

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100

# Parser needs, for future auto-install, not preinstalled.
# When Aidon detects a file it cannot read, it records the
# need here, in the KB point and the manifest, instead of
# crashing or inventing text. In the future, Aidon climbs
# to that parser itself: research, draft, sandbox-test, and
# adopt it with the owner's approval, like any capability
# tool. CAD, LaTeX, and 3D formats are examples, not a
# closed list. Blank memory, see README: no import from
# previous projects, collections start empty.
PARSER_NEEDED = {
    ".dwg": "cad-parser", ".dxf": "cad-parser", ".step": "cad-parser",
    ".stp": "cad-parser", ".stl": "3d-parser", ".obj": "3d-parser",
    ".tex": "latex-parser", ".bib": "latex-parser",
    ".psd": "image-parser", ".ai": "vector-parser",
    ".xls": "spreadsheet-parser", ".xlsx": "spreadsheet-parser",
    ".ppt": "presentation-parser", ".pptx": "presentation-parser",
}


def parser_needed_for(path: Path) -> str | None:
    """Parser name needed to read this file, or None if readable now."""
    suffix = Path(path).suffix.lower()
    if suffix in (".txt", ".md", ".markdown", ".html", ".htm",
                  ".pdf", ".docx", ".json", ".csv"):
        return None
    if is_image(Path(path)):
        return None
    return PARSER_NEEDED.get(suffix, "unknown-parser")


def strip_html(html: str) -> str:
    """Shared HTML-to-text: one contract for the KB and research paths.

    - script and style elements are removed, never kept as text;
    - JSON-LD script content (type application/ld+json) is lifted
      into the text instead of dropped, so structured facts survive;
    - table rows survive as separate lines, one row per line.
    BeautifulSoup is used when installed; the regex fallback keeps
    the same three rules when it is not. Returns lines joined by
    newlines, with blank lines removed.
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:
        BeautifulSoup = None
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            script_type = (script.get("type") or "").lower()
            if "ld+json" in script_type:
                # Lift structured data into the text, in place.
                # ``.string`` can be None when the parser has represented
                # the contents as more than one text node; get_text keeps
                # the structured facts in both cases.
                script.replace_with(script.get_text())
            else:
                script.decompose()
        for style in soup.find_all("style"):
            style.decompose()
        for row in soup.find_all("tr"):
            cells = [cell.get_text(" ", strip=True)
                     for cell in row.find_all(["td", "th"])]
            row.replace_with("\n" + " ".join(c for c in cells if c) + "\n")
        lines = []
        for line in soup.get_text("\n").splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)
    # Regex fallback, same three rules, no dependency.
    ld_json = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</tr\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    lines.extend(line.strip() for line in ld_json if line.strip())
    return "\n".join(lines)


def extract_text(path: Path) -> str:
    """Extract readable text from a file, by suffix.

    Parsers are arena plumbing, not capability tools, so
    they may be used here: pypdf for PDF, python-docx for
    docx, BeautifulSoup for HTML, plain UTF-8 for text and
    markdown. If a parser is not installed, fall back to
    UTF-8 with errors ignored, never crash an ingest, and
    never invent text. A PDF without pypdf will therefore
    ingest as little or no text, which is honest, and the
    file itself is still in knowledge/ for later re-ingest
    once the parser is installed.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return "\n\n".join((page.extract_text() or "")
                                for page in reader.pages)
        except Exception:
            return Path(path).read_text(encoding="utf-8", errors="ignore")
    if suffix == ".docx":
        try:
            import docx
            document = docx.Document(str(path))
            return "\n\n".join(p.text for p in document.paragraphs)
        except Exception:
            return Path(path).read_text(encoding="utf-8", errors="ignore")
    if suffix in (".html", ".htm"):
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
        return strip_html(raw)
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def chunk_text(text: str, size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into readable chunks, paragraph-aware."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= size:
            current = (current + "\n\n" + para).strip() if current else para
            continue
        if current:
            chunks.append(current)
        # A single paragraph longer than size is cut, with overlap.
        while len(para) > size:
            chunks.append(para[:size])
            para = para[size - overlap:]
        current = para
    if current:
        chunks.append(current)
    return chunks


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class KnowledgeBase:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.path = Path(memory.home) / "knowledge"
        self.path.mkdir(parents=True, exist_ok=True)
        self.manifest_path = Path(memory.home) / "knowledge_manifest.json"

    def _manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(json.dumps(manifest, indent=2))

    def import_file(self, source: Path) -> Path:
        """Duplicate a file into knowledge/, then ingest it."""
        source = Path(source)
        target = self.path / source.name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        self.ingest_file(target)
        return target

    def ingest_file(self, path: Path) -> int:
        """Chunk and ingest one file. Returns chunk count.

        Images are not chunked as text: one knowledge point,
        with the image vector (440M vision encoder, or a
        description vector with the hash fallback), the file
        name as description, and the knowledge path in the
        payload, so a text recall can refer to the image file
        under knowledge/. The file itself stays on disk.
        """
        path = Path(path)
        needed = parser_needed_for(path)
        if needed is not None:
            # Future auto-install, recorded now: file stays
            # in knowledge/, a point says what parser is
            # needed, no text is invented, no crash.
            digest = file_hash(path)
            self.memory.record(
                "knowledge", f"File {path.name} needs parser {needed} to read",
                payload={"file": path.name, "file_hash": digest,
                         "kind": "kb", "parser_needed": needed,
                         "status": "needs_parser"})
            manifest = self._manifest()
            manifest[path.name] = digest
            self._save_manifest(manifest)
            return 1
        if is_image(path):
            self._delete_chunks_for_file(path.name)
            digest = file_hash(path)
            self.memory.record_image(
                "knowledge", path, f"Image file {path.name}",
                payload={"file": path.name, "file_hash": digest,
                         "kind": "kb", "media": "image",
                         "knowledge_path": f"knowledge/{path.name}"})
            manifest = self._manifest()
            manifest[path.name] = digest
            self._save_manifest(manifest)
            return 1
        try:
            text = extract_text(path)
        except OSError:
            return 0
        # Remove old chunks for this file first, so a re-ingest
        # does not duplicate chunks.
        self._delete_chunks_for_file(path.name)
        chunks = chunk_text(text)
        digest = file_hash(path)
        for index, chunk in enumerate(chunks):
            self.memory.record("knowledge", chunk, {
                "file": path.name, "chunk_index": index,
                "file_hash": digest, "kind": "kb"})
        manifest = self._manifest()
        manifest[path.name] = digest
        self._save_manifest(manifest)
        return len(chunks)

    def _delete_chunks_for_file(self, file_name: str) -> None:
        # One filtered delete, every chunk, not a 1000-point scan and
        # a delete call per point.
        if hasattr(self.memory.store, "delete_where"):
            self.memory.store.delete_where("knowledge", {"file": file_name})
            return
        for point in self.memory.list_points("knowledge", limit=1000):
            if point.payload.get("file") == file_name:
                self.memory.forget("knowledge", point.id)

    def scan(self) -> dict:
        """Auto-ingest: new/changed files in, deleted files out."""
        manifest = self._manifest()
        ingested = 0
        for path in sorted(self.path.iterdir()):
            if not path.is_file():
                continue
            digest = file_hash(path)
            if manifest.get(path.name) != digest:
                ingested += self.ingest_file(path)
        # Prune chunks for files that are gone from knowledge/.
        present = {p.name for p in self.path.iterdir() if p.is_file()}
        for file_name in list(manifest.keys()):
            if file_name not in present:
                self._delete_chunks_for_file(file_name)
                manifest.pop(file_name, None)
        self._save_manifest(manifest)
        return {"ingested_chunks": ingested, "files": len(present)}

    def recall(self, query: str, limit: int = 5):
        """RAG recall: retrieve KB chunks for a query."""
        return self.memory.recall("knowledge", query, limit=limit)
