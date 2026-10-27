"""Universal document reader: one tool for (almost) all formats.

Built on Microsoft's markitdown when available
(`pip install markitdown[all]`): PDF, DOCX, PPTX, XLSX/CSV, HTML,
EPUB, images, audio, and more — everything comes back as clean
Markdown for the model. Falls back to the KB extractors (pypdf,
python-docx, BeautifulSoup) when markitdown is not installed, so
the tool still works offline for the common formats.

Contract: read_document(path_or_url, sandbox=None) -> markdown text
(or a clear READ_FAILED / READ_BLOCKED message). Never raises, never
crashes the loop.

Local reads are confined to the sandbox workdir, exactly like the
file tools: relative paths land in the workdir, everything else is
an escape. URLs are fetched with urllib, stdlib only, with a 50MB
cap and no redirect chain beyond 5 hops. No credentials, cookies,
or API keys are ever sent.
"""

from __future__ import annotations

import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from markitdown import MarkItDown
    _MARKITDOWN_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dep
    _MARKITDOWN_AVAILABLE = False

from .tools import _confined

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_CHARS = 60_000


def _is_url(s: str) -> bool:
    return urllib.parse.urlparse(s).scheme in ("http", "https")


def _resolve_local(path_or_url: str, sandbox) -> Path | None:
    """Resolve a local path, confined to the sandbox workdir."""
    try:
        if sandbox is not None and hasattr(sandbox, "workdir"):
            target = _confined(Path(sandbox.workdir), path_or_url)
        else:
            target = Path(path_or_url)
    except ValueError:
        return None
    if not target.exists():
        return None
    if not target.is_file():
        return None
    return target


def _resolve_url(path_or_url: str) -> Path | None:
    """Download a URL to a temp file, stdlib only, 50MB cap, 5 hops."""
    current = path_or_url
    for _hop in range(6):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return None
        req = urllib.request.Request(
            current, headers={"User-Agent": "Aidon/1.0 document-reader"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        return None
                    current = urllib.parse.urljoin(current, loc)
                    continue
                suffix = Path(urllib.parse.urlparse(current).path).suffix or ".bin"
                tmp = Path(tempfile.mkdtemp(prefix="aidon-doc-")) / f"doc{suffix}"
                size = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            tmp.unlink(missing_ok=True)
                            tmp.parent.rmdir(missing_ok=True)
                            return None
                        f.write(chunk)
                return tmp
        except (urllib.error.URLError, OSError, ValueError):
            return None
    return None


def _ocr_image(path: Path) -> str:
    """Best-effort OCR for images when markitdown returns nothing useful."""
    import shutil
    if shutil.which("tesseract") is None:
        return ""
    try:
        from PIL import Image
        import pytesseract  # type: ignore
        return pytesseract.image_to_string(Image.open(path)).strip()
    except Exception:
        return ""


def read_document(path_or_url: str, sandbox=None,
                  max_chars: int = MAX_CHARS, allowlist=None,
                  grants=None) -> str:
    """Convert a document (local path or URL) to Markdown.

    allowlist wiring: a URL outside the list (and outside any
    explicit grant) is READ_BLOCKED, never fetched. Local paths
    are unaffected — the allowlist is a network rule, not a file rule.
    """
    if _is_url(path_or_url) and allowlist:
        from ..agentic.research_allow import is_fetchable
        if not is_fetchable(path_or_url, allowlist, grants):
            return "READ_BLOCKED: URL host not on research allowlist"
    if not _MARKITDOWN_AVAILABLE:
        # Fall back to the KB extractors for common formats.
        from ..memory.kb import extract_text, parser_needed_for
        p = Path(path_or_url)
        if _is_url(path_or_url):
            local = _resolve_url(path_or_url)
            if local is None:
                return "READ_FAILED: could not download URL"
            p = local
        elif sandbox is not None:
            local = _resolve_local(path_or_url, sandbox)
            if local is None:
                return "READ_BLOCKED: path outside sandbox or not found"
            p = local
        needed = parser_needed_for(p)
        if needed is not None:
            return f"READ_FAILED: {p.name} needs parser {needed} to read (install pypdf, python-docx, or beautifulsoup4)"
        try:
            text = extract_text(p)
        except Exception as e:
            return f"READ_FAILED: {type(e).__name__}: {e}"
        finally:
            if _is_url(path_or_url):
                try:
                    p.unlink(missing_ok=True)
                    p.parent.rmdir(missing_ok=True)
                except Exception:
                    pass
        if not text.strip():
            return f"READ_FAILED: no text content extracted from {path_or_url}"
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n…(truncated, {len(text)} chars total)"
        return text

    local, err_path = None, None
    if _is_url(path_or_url):
        local = _resolve_url(path_or_url)
        if local is None:
            return "READ_FAILED: could not download URL"
    elif sandbox is not None:
        local = _resolve_local(path_or_url, sandbox)
        if local is None:
            return "READ_BLOCKED: path outside sandbox or not found"
        err_path = local
    else:
        err_path = Path(path_or_url)
        if not err_path.exists():
            return f"READ_FAILED: file not found: {path_or_url}"
        if not err_path.is_file():
            return f"READ_FAILED: not a file: {path_or_url}"
    try:
        converter = MarkItDown()
        result = converter.convert(str(local))
        text = (result.text_content or "").strip()
        if not text and local.suffix.lower() in (
                ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"):
            ocr = _ocr_image(local)
            if ocr:
                text = f"[OCR via tesseract]\n\n{ocr}"
            else:
                text = ("[image: no text extracted — markitdown returns "
                        "metadata only without a vision LLM; install tesseract "
                        "for OCR]")
        if not text:
            return f"READ_FAILED: no text content extracted from {path_or_url}"
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n…(truncated, {len(text)} chars total)"
        return text
    except Exception as e:
        return f"READ_FAILED: {type(e).__name__}: {e}"
    finally:
        # clean up URL downloads; never delete user files
        if _is_url(path_or_url) and local is not None:
            try:
                local.unlink(missing_ok=True)
                local.parent.rmdir(missing_ok=True)
            except Exception:
                pass
