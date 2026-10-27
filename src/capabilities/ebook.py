"""Build EPUB ebooks from markdown sources.

build_epub(sources, output, title, author="Aidon"): markdown files
(paths) or raw markdown strings ("text:...") -> one EPUB file.

Requires `ebooklib` and `markdown`. If either is missing the error
names the exact install command, instead of failing cryptically.
"""

from __future__ import annotations

from pathlib import Path

_MISSING = ("ebooklib/markdown not installed. In Aidon: "
            "install_python_package('ebooklib, markdown') "
            "(isolated mission venv), then retry.")


def _load() -> tuple:
    try:
        import ebooklib  # noqa: F401
        from ebooklib import epub
        import markdown
        return epub, markdown
    except ImportError:
        raise RuntimeError(_MISSING)


def build_epub(sources: list[str], output: str | Path,
               title: str, author: str = "Aidon") -> dict:
    """Build an EPUB. Returns {"ok": True, "path": ...} or
    {"ok": False, "error": ...}. Never raises."""
    try:
        epub, markdown = _load()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if not sources:
        return {"ok": False, "error": "no sources given"}
    if not title.strip():
        return {"ok": False, "error": "title is required"}

    book = epub.EpubBook()
    book.set_identifier(f"aidon-{abs(hash(title)) % 10**8}")
    book.set_title(title)
    book.add_author(author)

    chapters = []
    for i, src in enumerate(sources):
        if src.startswith("text:"):
            md_text, name = src[len("text:"):], f"part-{i + 1}"
        else:
            p = Path(src)
            if not p.is_file():
                return {"ok": False, "error": f"no such file: {src}"}
            md_text, name = p.read_text(encoding="utf-8"), p.stem
        html = markdown.markdown(md_text,
                                 extensions=["fenced_code", "tables"])
        ch = epub.EpubHtml(title=name.replace("-", " ").title(),
                           file_name=f"ch{i + 1}.xhtml")
        ch.content = f"<h1>{ch.title}</h1>\n{html}"
        book.add_item(ch)
        chapters.append(ch)

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters

    out = Path(output)
    if out.suffix.lower() != ".epub":
        out = out.with_suffix(".epub")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        epub.write_epub(str(out), book)
    except Exception as e:
        return {"ok": False, "error": f"write failed: {type(e).__name__}: {e}"}
    return {"ok": True, "path": str(out)}
