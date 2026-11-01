"""Verifier: evidence, not assertion.

A plan is verified by code against files in the workdir:
- every deliverable exists and is non-empty,
- every criterion is checked, not just listed,
- a ledger file's stated total is recomputed from its
  lines, in cents, and a mismatch is a failure, even if
  the file exists.

Criteria, simple and checkable, in plain words:
- "X exists" -> file X exists,
- "X is non-empty" -> file X exists and is non-empty,
- "X contains Y" -> file X text contains Y,
- "ledger X total is N" -> recomputed total for X equals N,
  where N is in cents for a JSON ledger, or in the file's
  own units for a text ledger.
- "currency items in X total at most N" -> sum of bullet-list
  prices in X is at most N dollars.
- "currency items in X match stated total" -> computed item
  prices match the dollar amount on its Estimated Total line.
Anything else is not a checkable criterion: it is
reported as unverified, not passed, so a model cannot
pass itself by writing a vague criterion.

Workspace boundary: every model-supplied name resolves
strictly inside the workdir (resolve_in_workdir). A name
with "..", an absolute path, or a symlink escaping the
workdir resolves to nothing, so a criterion can never
read a file outside the run's folder — not the home
directory, not another run, not the host. The plan
validator already rejects escapes in deliverables; this
is the same rule for criteria, which the validator never
sees.

Binary deliverables: a file that is not UTF-8 text is
not unreadable by default. PDFs and office documents get
a stdlib-only text extraction (PDF text streams, plain
or FlateDecode; docx/xlsx/pptx/epub XML parts), so
"contains" checks and the model verifier can judge
their content. Anything else binary — images, archives,
executables, encrypted files — is opaque: judged on
properties (exists, non-empty, size, format) only, never
failed for being unreadable. An encrypted file is opaque
by design: the verifier must never be handed its key,
and its plaintext must never reach a model.
"""

from __future__ import annotations

import json
import re
import zipfile
import zlib
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Extraction is best-effort and bounded: a huge binary must
# not be pulled into memory just to judge it. Above this,
# a binary deliverable is opaque, full stop.
_EXTRACT_MAX_BYTES = 20 * 1024 * 1024



def _relative_name(workdir: Path, name: str) -> str:
    """Normalize model-facing ``workdir/foo`` paths to ``foo``.

    The planner sees the sandbox label ``workdir`` in prompts, while the
    verifier already receives that directory as its root. Joining both
    would incorrectly look for ``workdir/workdir/foo``. The user's
    "workspace folder" is the same folder, so its prefix strips too.
    """
    value = str(name).strip().replace("\\", "/")
    root_name = workdir.name.strip()
    if root_name and value == root_name:
        return ""
    if root_name and value.startswith(root_name + "/"):
        return value[len(root_name) + 1:]
    if value.startswith("workdir/"):
        return value[len("workdir/"):]
    if value.startswith("workspace folder/"):
        return value[len("workspace folder/"):]
    return value


def resolve_in_workdir(workdir: Path, name: str) -> Path | None:
    """Resolve a model-supplied name strictly inside ``workdir``.

    Normalizes the planner's workdir/workspace-folder prefixes,
    then resolves symlinks and rejects anything that is not
    strictly below the workdir root: ".." escapes, absolute
    paths outside the root, and symlinks pointing out. Returns
    None for every rejected name, so callers treat it as a
    missing file — fail-closed, never an outside read.
    """
    relative = _relative_name(workdir, name)
    if not relative:
        return None
    root = Path(workdir).resolve()
    candidate = (Path(workdir) / relative).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        return None
    return candidate


def _pdf_literal_strings(content: str) -> list[str]:
    """Literal and hex strings of PDF text-showing operators, in order.

    Scans BT...ET blocks and collects (...) and <...> operands,
    which is where Tj/TJ text lives in ordinary PDFs (reportlab,
    fpdf2, and Aidon's own document writer). Nested literal
    parens and backslash escapes are handled; kerning numbers
    and operators are ignored. This is best-effort: a PDF with
    custom font encodings yields glyph IDs, not text, and the
    caller treats thin or empty extraction as opaque.
    """
    texts: list[str] = []
    for block in re.findall(r"BT(.*?)ET", content, re.DOTALL):
        index = 0
        while index < len(block):
            char = block[index]
            if char == "(":
                depth = 1
                index += 1
                buf: list[str] = []
                while index < len(block) and depth:
                    c = block[index]
                    if c == "\\" and index + 1 < len(block):
                        nxt = block[index + 1]
                        if nxt in "nrtbf":
                            buf.append({"n": "\n", "r": "\r", "t": "\t",
                                        "b": "\b", "f": "\f"}[nxt])
                            index += 2
                        elif nxt.isdigit():
                            octal = block[index + 1:index + 4]
                            digits = ""
                            for digit in octal:
                                if digit.isdigit():
                                    digits += digit
                                else:
                                    break
                            buf.append(chr(int(digits, 8)) if digits else "")
                            index += 1 + len(digits)
                        else:
                            buf.append(nxt)
                            index += 2
                    elif c == "(":
                        depth += 1
                        buf.append(c)
                        index += 1
                    elif c == ")":
                        depth -= 1
                        if depth:
                            buf.append(c)
                        index += 1
                    else:
                        buf.append(c)
                        index += 1
                texts.append("".join(buf))
            elif char == "<" and index + 1 < len(block) \
                    and block[index + 1] != "<":
                match = re.match(r"<([0-9A-Fa-f\s]+)>", block[index:])
                if match:
                    raw = bytes.fromhex(re.sub(r"\s+", "", match.group(1)))
                    if raw.startswith(b"\xfe\xff"):
                        texts.append(raw[2:].decode("utf-16-be", "replace"))
                    else:
                        texts.append(raw.decode("latin-1"))
                    index += match.end()
                else:
                    index += 1
            else:
                index += 1
    return texts


def _extract_pdf_text(data: bytes) -> str:
    """Text of a PDF's content streams, plain or FlateDecode."""
    parts: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        raw = match.group(1)
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        text = raw.decode("latin-1")
        parts.append("".join(_pdf_literal_strings(text)))
    joined = "\n".join(part for part in parts if part)
    printable = "".join(c for c in joined if c.isprintable() or c in "\n\t")
    return printable


def _extract_office_text(path: Path) -> str:
    """Text of docx/xlsx/pptx/epub XML parts, via stdlib zipfile."""
    suffix = path.suffix.lower()
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return ""
    try:
        names = archive.namelist()
        if suffix == ".docx":
            wanted = ["word/document.xml"]
            pattern = r"<w:t[^>]*>([^<]*)</w:t>"
        elif suffix == ".xlsx":
            wanted = ["xl/sharedStrings.xml"]
            pattern = r"<t[^>]*>([^<]*)</t>"
        elif suffix == ".pptx":
            wanted = [n for n in names
                      if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
            pattern = r"<a:t>([^<]*)</a:t>"
        elif suffix == ".epub":
            wanted = [n for n in names
                      if n.endswith((".xhtml", ".html", ".htm"))]
            pattern = None
        else:
            return ""
        chunks: list[str] = []
        for name in wanted:
            if name not in names:
                continue
            try:
                xml = archive.read(name).decode("utf-8", "replace")
            except (KeyError, OSError):
                continue
            if pattern is None:
                xml = re.sub(r"<[^>]+>", " ", xml)
                chunks.append(xml)
            else:
                chunks.append(" ".join(re.findall(pattern, xml)))
        return "\n".join(c for c in chunks if c.strip())
    except (zipfile.BadZipFile, OSError):
        return ""
    finally:
        archive.close()


def _extract_binary_text(path: Path) -> str:
    """Best-effort text for a binary deliverable, "" when opaque."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            return _extract_pdf_text(path.read_bytes())
        except OSError:
            return ""
    if suffix in (".docx", ".xlsx", ".pptx", ".epub"):
        return _extract_office_text(path)
    return ""


def read_deliverable(path: Path, head_chars: int = 2000) -> dict:
    """Head of a deliverable for verification, with its kind.

    kind is "text" for UTF-8 text, "extracted" for a binary
    document whose text was recovered, and "opaque" for
    everything else binary — images, archives, executables,
    encrypted files. Opaque deliverables carry no shown
    content: a verifier judges them on properties only and
    must never fail them for being unreadable.
    """
    size = path.stat().st_size
    try:
        with path.open("r", encoding="utf-8") as handle:
            head = handle.read(head_chars + 1)
    except UnicodeDecodeError:
        pass
    except OSError:
        return {"kind": "opaque", "shown": "", "size": size}
    else:
        shown = head[:head_chars]
        if len(head) > head_chars:
            shown += f"\n[content truncated to {head_chars} chars]"
        return {"kind": "text", "shown": shown, "size": size}
    if size > _EXTRACT_MAX_BYTES:
        return {"kind": "opaque", "shown": "", "size": size}
    text = _extract_binary_text(path)
    if not text.strip():
        return {"kind": "opaque", "shown": "", "size": size}
    shown = text[:head_chars]
    if len(text) > head_chars:
        shown += f"\n[content truncated to {head_chars} chars]"
    return {"kind": "extracted", "shown": shown, "size": size}


def _ledger_total(workdir: Path, name: str) -> int | None:
    target = resolve_in_workdir(workdir, name)
    if target is None or not target.is_file():
        return None
    raw = target.read_text(encoding="utf-8", errors="ignore")
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "lines" in data:
            return sum(int(line.get("amount_cents", 0))
                       for line in data["lines"])
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        pass
    try:
        return sum(int(line.strip()) for line in raw.splitlines()
                   if line.strip())
    except ValueError:
        return None


_CURRENCY_AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"


def _currency_cents(amount: str) -> int | None:
    try:
        value = Decimal(amount.replace(",", ""))
        return int(value * 100)
    except (InvalidOperation, ValueError):
        return None


def _currency_list(workdir: Path, name: str) -> tuple[int | None, int | None]:
    """Return itemized and stated totals in cents for a Markdown price list."""
    path = resolve_in_workdir(workdir, name)
    if path is None or not path.is_file():
        return None, None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None

    item_total = 0
    item_count = 0
    item_pattern = re.compile(
        rf"^\s*[-*+]\s+(.+?)\s+(?:[-–—:]\s*)?\$\s*({_CURRENCY_AMOUNT})\s*$"
    )
    total_pattern = re.compile(
        rf"^\s*(?:estimated|grand|shopping)?\s*total\s*[:=]\s*"
        rf"\$\s*({_CURRENCY_AMOUNT})\b",
        re.IGNORECASE,
    )
    stated_totals: list[int] = []
    for line in content.splitlines():
        total_match = total_pattern.match(line)
        if total_match:
            cents = _currency_cents(total_match.group(1))
            if cents is not None:
                stated_totals.append(cents)
            continue
        item_match = item_pattern.match(line)
        if item_match and not re.search(r"\btotal\b", item_match.group(1), re.IGNORECASE):
            cents = _currency_cents(item_match.group(2))
            if cents is None:
                return None, None
            item_total += cents
            item_count += 1
    if item_count == 0 or len(stated_totals) != 1:
        return None, stated_totals[0] if len(stated_totals) == 1 else None
    return item_total, stated_totals[0]


def budget_currency_criteria(goal: str, deliverables: list[str],
                             workdir: Path) -> list[str]:
    """Add deterministic price checks for explicitly budgeted food plans.

    The planner is asked to include these criteria, but model-generated
    criteria are not authoritative: omitting one must not turn a budget
    claim into a pass. Require itemized prices and one recomputable total
    for budgeted grocery, shopping, or meal-plan deliverables.
    """
    goal_text = str(goal or "")
    if not re.search(r"\b(?:grocery|groceries|shopping list|meal plan|"
                     r"meal planning|food plan|menu)\b",
                     goal_text, re.IGNORECASE):
        return []
    budget_match = re.search(
        rf"\b(?:under|below|up to|at most|no more than|less than|"
        rf"budget(?:\s+(?:of|is|up to|under)|\s*[:=])?|"
        rf"maximum(?:\s+of)?)\s*"
        rf"(?:(?:CAD|USD|EUR|GBP|AUD|NZD)\s*)?\$?\s*"
        rf"({_CURRENCY_AMOUNT})\b",
        goal_text, re.IGNORECASE)
    if not budget_match:
        return []
    amount = budget_match.group(1)
    criteria: list[str] = []
    for raw_name in deliverables or []:
        name = _relative_name(workdir, raw_name)
        path = resolve_in_workdir(workdir, raw_name)
        if path is None or path.suffix.lower() not in {".md", ".txt", ".csv"}:
            continue
        criteria.extend((
            f"currency items in {name} total at most ${amount}",
            f"currency items in {name} match stated total",
        ))
    return criteria


def requested_shopping_item_criteria(goal: str, deliverables: list[str],
                                     workdir: Path) -> list[str]:
    """Require an explicitly requested item in the shopping-list section."""
    goal_text = str(goal or "")
    food_context = re.search(
        r"\b(?:shopping list|grocery list|produce section|produce list|"
        r"grocery|groceries|meal plan|food plan|menu)\b",
        goal_text, re.IGNORECASE)
    if not food_context:
        food_context = any(re.search(
            r"grocery|shopping|meal|food|menu",
            Path(str(name)).name, re.IGNORECASE)
            for name in (deliverables or []))
    if not food_context:
        return []
    match = re.search(
        r"\badd\s+(?!up\b)(?:the\s+)?([A-Za-z][A-Za-z -]*?)"
        r"(?=\s+(?:to\b|with\b|and\b)|[,.;!?]|$)",
        goal_text, re.IGNORECASE)
    if not match:
        return []
    item = re.sub(r"\s+", " ", match.group(1)).strip(" -")
    if not item:
        return []
    candidates = [name for name in (deliverables or [])
                  if Path(str(name)).suffix.lower() in {".md", ".txt", ".csv"}]
    if len(candidates) > 1:
        named = [name for name in candidates
                 if re.search(r"grocery|shopping|meal|food|menu",
                              Path(str(name)).name, re.IGNORECASE)]
        candidates = named or candidates
    return [f"shopping list in {_relative_name(workdir, name)} contains {item}"
            for name in candidates]


def check_criterion(workdir: Path, criterion: str) -> tuple[bool, str]:
    text = str(criterion).strip()
    # Models hedge with trailing parentheticals ("... (or ... if ...)").
    # The aside is not part of the assertion, so strip trailing
    # parenthetical groups and match the checkable core. Only
    # trailing groups strip: a parenthetical mid-criterion may
    # carry the filename or the needle. A criterion with no
    # checkable core still fails, as before — this narrows
    # nothing, it just stops hedges from voiding real checks.
    while True:
        stripped = re.sub(r"\s*\([^()]*\)\s*$", "", text)
        if stripped == text:
            break
        text = stripped
    match = re.match(
        r"^shopping list in\s+(.+?)\s+contains\s+(.+)$",
        text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        path = resolve_in_workdir(workdir, match.group(1))
        item = match.group(2).strip()
        if path is None or not path.is_file():
            return False, f"{name} missing"
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False, f"{name} not readable as text"
        lines = content.splitlines()
        section: list[str] = []
        in_section = False
        for line in lines:
            heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                level = len(heading.group(1))
                title = heading.group(2).strip().lower()
                is_list = bool(re.search(
                    r"\b(?:shopping|grocery)\s+list\b", title))
                if in_section and level <= 2:
                    break
                if is_list:
                    in_section = True
                    continue
            if in_section:
                section.append(line)
        item_re = re.compile(
            rf"^\s*[-*+]\s+.*\b{re.escape(item)}\b.*"
            rf"\$\s*{_CURRENCY_AMOUNT}\s*$", re.IGNORECASE)
        ok = any(item_re.search(line) for line in section)
        return ok, (f"{name} shopping list includes priced {item}"
                    if ok else f"{name} shopping list missing priced {item}")
    # Models often describe the evidence instead of naming the generated
    # file. The file-producing tool is the source of truth in this case.
    if re.search(r"(?:pdf|document|file)\s+(?:file\s+)?(?:is\s+)?(?:created|exists)\s+inside\s+(?:the\s+)?(?:workdir|workspace folder)(?:\b|$)",
                 text, re.IGNORECASE):
        files = [path for path in workdir.rglob("*")
                 if path.is_file() and (path.suffix.lower() == ".pdf"
                                        if "pdf" in text.lower() else True)]
        ok = bool(files)
        return ok, ("generated file exists inside workdir" if ok
                    else "generated file missing inside workdir")
    if re.search(r"(?:output|final)\s+message\s+provides?\s+(?:a\s+)?link\s+to\s+(?:the\s+)?(?:file|document|pdf)",
                 text, re.IGNORECASE):
        files = [path for path in workdir.rglob("*") if path.is_file()]
        ok = bool(files)
        return ok, ("output link target available" if ok
                    else "output link target missing")
    if re.search(r"(?:pdf|document)\s+link\s+(?:can\s+be\s+)?returned\s+in\s+output\s+message",
                 text, re.IGNORECASE):
        files = [path for path in workdir.rglob("*")
                 if path.is_file() and path.suffix.lower() == ".pdf"]
        ok = bool(files)
        return ok, ("output link target available" if ok
                    else "output link target missing")
    match = re.match(r"^(.+?)\s+exists$", text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        path = resolve_in_workdir(workdir, match.group(1))
        ok = path is not None and path.is_file()
        return ok, f"{name} exists" if ok else f"{name} missing"
    match = re.match(r"^.+?\s+exists\s+at\s+(.+)$", text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        path = resolve_in_workdir(workdir, match.group(1))
        ok = path is not None and path.is_file()
        return ok, f"{name} exists" if ok else f"{name} missing"
    # The user calls the workdir the "workspace folder": same folder,
    # same check. A leading "PDF file"/"document"/"file" descriptor
    # is model phrasing, not part of the filename.
    match = re.match(r"^(.+?)\s+exists\s+in\s+(?:the\s+)?workspace folder$",
                     text, re.IGNORECASE)
    if match:
        name = re.sub(r"^(?:pdf\s+file|document|file)\s+", "",
                      match.group(1).strip(), flags=re.IGNORECASE)
        name = _relative_name(workdir, name)
        path = resolve_in_workdir(workdir, name)
        ok = path is not None and path.is_file()
        return ok, f"{name} exists" if ok else f"{name} missing"
    match = re.match(r"^(.+?)\s+is non-empty$", text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        path = resolve_in_workdir(workdir, match.group(1))
        ok = path is not None and path.is_file() and path.stat().st_size > 0
        return ok, f"{name} non-empty" if ok else f"{name} empty or missing"
    match = re.match(r"^(.+?)\s+does not contain\s+(.+)$", text, re.IGNORECASE)
    if match:
        # Absence check, for allergy and dietary criteria ("menu.md does
        # not contain milk"): the file must NOT contain the needle.
        # Matching is case-insensitive because allergen names vary in
        # case on labels and in prose (Milk, MILK, milk). This branch
        # must stay BEFORE the plain "contains" branch below: the
        # contains regex would otherwise match with the name
        # "menu.md does not" and check the wrong assertion.
        name, needle = _relative_name(workdir, match.group(1)), match.group(2).strip()
        path = resolve_in_workdir(workdir, match.group(1))
        if path is None or not path.is_file():
            return False, f"{name} missing"
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            if path.stat().st_size > _EXTRACT_MAX_BYTES:
                return False, f"{name} not searchable (binary, too large)"
            content = _extract_binary_text(path)
            if not content.strip():
                return False, f"{name} not searchable (binary, text not extractable)"
        ok = needle.lower() not in content.lower()
        return ok, f"{name} lacks {needle}" if ok else f"{name} contains {needle}"
    match = re.match(r"^(.+?)\s+contains\s+(.+)$", text, re.IGNORECASE)
    if match:
        name, needle = _relative_name(workdir, match.group(1)), match.group(2).strip()
        path = resolve_in_workdir(workdir, match.group(1))
        if path is None or not path.is_file():
            return False, f"{name} missing"
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary deliverable: search extracted text where
            # extraction works, fail closed where it does not.
            # A compressed PDF that extracts is checkable; an
            # encrypted or unextractable file is not, and the
            # failure says so instead of claiming the needle
            # is absent.
            if path.stat().st_size > _EXTRACT_MAX_BYTES:
                return False, f"{name} not searchable (binary, too large)"
            content = _extract_binary_text(path)
            if not content.strip():
                return False, f"{name} not searchable (binary, text not extractable)"
        ok = needle in content
        return ok, f"{name} contains {needle}" if ok else f"{name} lacks {needle}"
    match = re.match(r"^ledger\s+(.+?)\s+total is\s+(-?\d+)$", text, re.IGNORECASE)
    if match:
        name, expected = _relative_name(workdir, match.group(1)), int(match.group(2))
        total = _ledger_total(workdir, name)
        if total is None:
            return False, f"ledger {name} total not computable"
        return total == expected, \
            f"ledger {name} total {total}" if total == expected else \
            f"ledger {name} total {total} != {expected}"
    match = re.match(
        rf"^currency items in\s+(.+?)\s+total at most\s+\$?({_CURRENCY_AMOUNT})$",
        text,
        re.IGNORECASE,
    )
    if match:
        name = _relative_name(workdir, match.group(1))
        item_total, _stated_total = _currency_list(workdir, name)
        expected = _currency_cents(match.group(2))
        if item_total is None or expected is None:
            return False, f"currency items in {name} total not computable"
        ok = item_total <= expected
        if ok:
            return True, f"currency items in {name} total ${item_total / 100:.2f} within budget"
        return False, f"currency items in {name} total ${item_total / 100:.2f} exceeds ${expected / 100:.2f}"
    match = re.match(
        r"^currency items in\s+(.+?)\s+match stated total$",
        text,
        re.IGNORECASE,
    )
    if match:
        name = _relative_name(workdir, match.group(1))
        item_total, stated_total = _currency_list(workdir, name)
        if item_total is None or stated_total is None:
            return False, f"currency items in {name} or stated total not computable"
        ok = item_total == stated_total
        if ok:
            return True, f"currency items in {name} match stated total ${item_total / 100:.2f}"
        return False, f"currency items in {name} total ${item_total / 100:.2f} != stated ${stated_total / 100:.2f}"
    match = re.match(
        r"^(?:the )?(?:final )?output message provides link to (?:the )?(.+)$",
        text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        target = resolve_in_workdir(workdir, match.group(1))
        ok = target is not None and target.is_file()
        if not ok and name.lower() in {"pdf", "document", "file"}:
            ok = any(path.is_file() and path.suffix.lower() == ".pdf"
                     for path in workdir.rglob("*")) if name.lower() == "pdf" else False
        return ok, (f"output link target {name} available" if ok else
                    f"output link target {name} missing")
    return False, f"unverified criterion: {criterion}"


def verify_plan(workdir: Path, deliverables: list[str],
                criteria: list[str]) -> tuple[bool, list[str], list[str]]:
    """Returns (ok, failures, verified_files)."""
    failures: list[str] = []
    verified: list[str] = []
    pdf_files = [path for path in workdir.rglob("*")
                 if path.is_file() and path.suffix.lower() == ".pdf"]
    for name in deliverables:
        relative = _relative_name(workdir, name)
        path = resolve_in_workdir(workdir, name)
        if path is None or not path.is_file():
            # If a document tool produced exactly one PDF, its structured
            # result may have been lost by an older adapter or an in-flight
            # repair. Recover the real artifact rather than asking a model
            # to verify a prose label or replacing the plan with ``list``.
            if (len(pdf_files) == 1 and
                    re.search(r"pdf|document", str(name), re.IGNORECASE)):
                verified.append(str(pdf_files[0].relative_to(workdir)))
            else:
                failures.append(f"missing deliverable: {name}")
        elif path.stat().st_size == 0:
            failures.append(f"empty deliverable: {name}")
        else:
            verified.append(name)
    for criterion in criteria:
        ok, message = check_criterion(workdir, criterion)
        if not ok:
            failures.append(message)
    return (not failures, failures, verified)
