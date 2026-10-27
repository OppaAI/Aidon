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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects inside urllib: it would follow them
    unchecked, and the hop loop below would never see them. Returning
    None makes urlopen raise HTTPError for the redirect status, and
    the loop below reads Location from that error and re-validates
    the next hop before fetching it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _resolve_url(path_or_url: str, allowlist=None, grants=None,
                 allow_local: bool = False) -> tuple[Path | None, str]:
    """Download a URL to a temp file, stdlib only, 50MB cap, 5 hops.

    Returns (path, error): path is None on failure, and error is
    "" on success, a READ_BLOCKED message when a hop is blocked by
    the allowlist or the SSRF guard, or a READ_FAILED message
    otherwise. Every hop is re-validated before it is fetched, so
    an allowlisted page cannot 302 into a blocked host, loopback,
    LAN, or cloud metadata. Grants never bypass the SSRF guard.
    """
    from ..agentic.research_allow import guard_reason
    opener = urllib.request.build_opener(_NoRedirect)
    current = path_or_url
    for _hop in range(6):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return None, "READ_FAILED: not an http(s) URL"
        reason = guard_reason(current, allowlist, grants, allow_local)
        if reason:
            return None, f"READ_BLOCKED: {reason}"
        req = urllib.request.Request(
            current, headers={"User-Agent": "Aidon/1.0 document-reader"})
        try:
            with opener.open(req, timeout=30) as resp:
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
                            return None, "READ_FAILED: download over 50MB cap"
                        f.write(chunk)
                return tmp, ""
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("location") if exc.headers else None
                if not loc:
                    return None, (f"READ_FAILED: redirect without Location "
                                  f"from {current}")
                current = urllib.parse.urljoin(current, loc)
                continue
            return None, f"READ_FAILED: HTTP {exc.code}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, f"READ_FAILED: {type(exc).__name__}: {exc}"
    return None, "READ_FAILED: too many redirects"


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
                  grants=None, allow_local: bool = False) -> str:
    """Convert a document (local path or URL) to Markdown.

    allowlist + SSRF wiring: a URL outside the list (and outside
    any explicit grant) is READ_BLOCKED, never fetched, and every
    redirect hop is re-validated before it is fetched. Local paths
    are unaffected — the allowlist is a network rule, not a file rule.
    allow_local=True is the explicit local-server exception, for
    tests and local development only.
    """
    if not _MARKITDOWN_AVAILABLE:
        # Fall back to the KB extractors for common formats.
        from ..memory.kb import extract_text, parser_needed_for
        p = Path(path_or_url)
        if _is_url(path_or_url):
            local, err = _resolve_url(path_or_url, allowlist, grants, allow_local)
            if local is None:
                return err
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
        local, err = _resolve_url(path_or_url, allowlist, grants, allow_local)
        if local is None:
            return err
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
