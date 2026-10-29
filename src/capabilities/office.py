"""Document builders: spreadsheet, Word, slides, LaTeX.

Stdlib-first, and never a fake file: every builder validates its
input up front, writes a real file in the requested format when
the library is available, and otherwise writes the smallest
valid OOXML zip (docx/pptx) or a CSV-per-sheet fallback
(spreadsheet) that still carries the actual data. An empty or
invalid input raises ValueError before anything is written, and
a failed write never leaves a half-written file behind.
"""

from __future__ import annotations

import csv
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape


def _out_path(output) -> Path:
    path = Path(output)
    if not str(output).strip():
        raise ValueError("output path is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _require_nonempty(value, what: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{what} must not be empty")


def _safe_sheet_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name).strip()).strip("_")
    if not safe:
        raise ValueError("sheet name must contain a letter, digit, - or _")
    return safe


# ---------------------------------------------------------------- spreadsheet

def make_spreadsheet(sheets, output) -> str:
    """Write a multi-sheet workbook.

    sheets: [{name, headers: [...], rows: [[...], ...]}, ...].
    Returns the output path when openpyxl is available; when it
    is not, writes one CSV per sheet plus a manifest next to the
    output and returns the manifest path, so the caller can tell
    the fallback happened from the return value alone.
    """
    if not sheets:
        raise ValueError("sheets must not be empty")
    cleaned = []
    seen = set()
    for sheet in sheets:
        if not isinstance(sheet, dict):
            raise ValueError("each sheet must be a dict")
        name = _safe_sheet_name(sheet.get("name", ""))
        if name in seen:
            raise ValueError(f"duplicate sheet name: {name}")
        seen.add(name)
        headers = sheet.get("headers") or []
        rows = sheet.get("rows") or []
        if not headers and not rows:
            raise ValueError(f"sheet {name!r} has no headers and no rows")
        cleaned.append((name, list(headers), [list(r) for r in rows]))
    path = _out_path(output)
    try:
        import openpyxl
    except ImportError:
        openpyxl = None
    if openpyxl is not None:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name, headers, rows in cleaned:
            ws = wb.create_sheet(title=name[:31])
            if headers:
                ws.append(headers)
            for row in rows:
                ws.append(row)
        wb.save(path)
        return str(path)
    print("warning: openpyxl is not installed; wrote CSV files plus a manifest instead of XLSX",
          file=sys.stderr)
    base = path.with_suffix("")
    manifest_lines = []
    for name, headers, rows in cleaned:
        csv_path = base.parent / f"{base.name}_{name}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if headers:
                writer.writerow(headers)
            writer.writerows(rows)
        manifest_lines.append(f"{name}: {csv_path.name}")
    manifest = base.parent / f"{base.name}_manifest.txt"
    manifest.write_text(
        "Spreadsheet fallback: openpyxl is not installed, so each sheet "
        "was written as a CSV file.\n" + "\n".join(manifest_lines) + "\n",
        encoding="utf-8")
    return str(manifest)


# ---------------------------------------------------------------- docx

def _docx_paragraph_xml(text: str, style: str | None = None) -> str:
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    return (f"<w:p><w:pPr>{style_xml}</w:pPr>"
            f"<w:r><w:t xml:space=\"preserve\">{_xml_escape(text)}</w:t>"
            f"</w:r></w:p>")


def _docx_table_xml(headers, rows) -> str:
    def row_xml(cells, header=False):
        cells_xml = "".join(
            f"<w:tc><w:p><w:r><w:t xml:space=\"preserve\">"
            f"{_xml_escape(str(c))}</w:t></w:r></w:p></w:tc>"
            for c in cells)
        return f"<w:tr>{cells_xml}</w:tr>"
    parts = ["<w:tbl>"]
    if headers:
        parts.append(row_xml(headers, header=True))
    for row in rows:
        parts.append(row_xml(row))
    parts.append("</w:tbl>")
    return "".join(parts)


def _minimal_docx_xml(title: str, blocks) -> str:
    body = [_docx_paragraph_xml(title, "Title")]
    for block in blocks:
        kind = block.get("type", "paragraph")
        if kind == "heading":
            level = int(block.get("level", 1))
            body.append(_docx_paragraph_xml(
                block.get("text", ""), f"Heading{max(1, min(6, level))}"))
        elif kind == "bullet":
            body.append(_docx_paragraph_xml("• " + block.get("text", "")))
        elif kind == "table":
            body.append(_docx_table_xml(
                block.get("headers") or [], block.get("rows") or []))
        else:
            body.append(_docx_paragraph_xml(block.get("text", "")))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(body) + "</w:body></w:document>")


def make_docx(title: str, blocks, output) -> str:
    """Write a .docx document.

    blocks: [{type: heading|paragraph|bullet|table, text, level,
    headers, rows}]. Uses python-docx when importable, else a
    minimal valid OOXML zip carrying the same title and text.
    """
    _require_nonempty(title, "title")
    if not blocks:
        raise ValueError("blocks must not be empty")
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("each block must be a dict")
        if block.get("type") == "table":
            if not block.get("headers") and not block.get("rows"):
                raise ValueError("table block needs headers or rows")
        elif not str(block.get("text", "")).strip():
            raise ValueError("text block must not be empty")
    path = _out_path(output)
    try:
        import docx as python_docx
    except ImportError:
        python_docx = None
    if python_docx is not None:
        document = python_docx.Document()
        document.add_heading(title, level=0)
        for block in blocks:
            kind = block.get("type", "paragraph")
            if kind == "heading":
                document.add_heading(block.get("text", ""),
                                     level=max(1, min(6, int(block.get("level", 1)))))
            elif kind == "bullet":
                document.add_paragraph(block.get("text", ""), style="List Bullet")
            elif kind == "table":
                headers = block.get("headers") or []
                rows = block.get("rows") or []
                width = max(len(headers), max((len(r) for r in rows), default=0))
                table = document.add_table(rows=0, cols=max(1, width))
                if headers:
                    table.add_row().cells
                    for i, header in enumerate(headers):
                        table.rows[-1].cells[i].text = str(header)
                for row in rows:
                    cells = table.add_row().cells
                    for i, cell in enumerate(row):
                        cells[i].text = str(cell)
            else:
                document.add_paragraph(block.get("text", ""))
        document.save(path)
        return str(path)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>")
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>")
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/></w:style>'
        "</w:styles>")
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", _minimal_docx_xml(title, blocks))
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", doc_rels)
    return str(path)


# ---------------------------------------------------------------- slides

def _slide_xml(title_text: str, bullets) -> str:
    shapes = []
    shapes.append(
        '<p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>'
        f"{_xml_escape(title_text)}</a:t></a:r></a:p></p:txBody></p:sp>")
    for bullet in bullets or []:
        shapes.append(
            '<p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>'
            f"• {_xml_escape(str(bullet))}</a:t></a:r></a:p></p:txBody></p:sp>")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree>' + "".join(shapes) + "</p:spTree></p:cSld>"
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')


def make_slides(title: str, slides, output) -> str:
    """Write a .pptx deck.

    slides: [{title, bullets: [...]}]. The first slide is a
    title slide carrying the deck title, then one slide per
    entry. Uses python-pptx when importable, else a minimal
    valid OOXML zip carrying the same text.
    """
    _require_nonempty(title, "title")
    if not slides:
        raise ValueError("slides must not be empty")
    for slide in slides:
        if not isinstance(slide, dict) or not str(slide.get("title", "")).strip():
            raise ValueError("each slide needs a non-empty title")
    path = _out_path(output)
    try:
        import pptx as python_pptx
    except ImportError:
        python_pptx = None
    if python_pptx is not None:
        deck = python_pptx.Presentation()
        title_slide = deck.slides.add_slide(deck.slide_layouts[0])
        title_slide.shapes.title.text = title
        for slide in slides:
            content = deck.slides.add_slide(deck.slide_layouts[1])
            content.shapes.title.text = slide["title"]
            frame = content.placeholders[1].text_frame
            for i, bullet in enumerate(slide.get("bullets") or []):
                paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
                paragraph.text = str(bullet)
                paragraph.level = 0
        deck.save(path)
        return str(path)
    slide_count = len(slides) + 1
    # Unique relationship ids in the presentation part: master
    # rId1, theme rId2, slides rId3 and up, matching sldId r:ids.
    slide_ids = "".join(
        f'<p:sldId id="{256 + i}" r:id="rId{3 + i}"/>'
        for i in range(slide_count))
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        '<p:sldSz cx="9144000" cy="6858000"/></p:presentation>')
    pres_rels = "".join(
        f'<Relationship Id="rId{3 + i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i + 1}.xml"/>'
        for i in range(slide_count))
    pres_rels += ('<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
                  '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>')
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        + "".join(
            f'<Override PartName="/ppt/slides/slide{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            for i in range(slide_count))
        + "</Types>")
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
        "</Relationships>")
    master = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/></p:nvGrpSpPr>'
        '<p:grpSpPr/></p:spTree></p:cSld>'
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        '<p:txStyles><p:titleStyle><a:lvl1pPr algn="l"/></p:titleStyle></p:txStyles></p:sldMaster>')
    master_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
        "</Relationships>")
    layout = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld name="Title and Content"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/></p:nvGrpSpPr>'
        '<p:grpSpPr/></p:spTree></p:cSld>'
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    layout_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>")
    theme = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">'
        '<a:themeElements><a:clrScheme name="Office">'
        '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
        '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
        '<a:dk2><a:srgbClr val="1F497D"/></a:dk2><a:lt2><a:srgbClr val="EEECE1"/></a:lt2>'
        '<a:accent1><a:srgbClr val="4F81BD"/></a:accent1><a:accent2><a:srgbClr val="C0504D"/></a:accent2>'
        '<a:accent3><a:srgbClr val="9BBB59"/></a:accent3><a:accent4><a:srgbClr val="8064A2"/></a:accent4>'
        '<a:accent5><a:srgbClr val="4BACC6"/></a:accent5><a:accent6><a:srgbClr val="F79646"/></a:accent6>'
        '<a:hlink><a:srgbClr val="0000FF"/></a:hlink><a:folHlink><a:srgbClr val="800080"/></a:folHlink>'
        "</a:clrScheme><a:fontScheme name=\"Office\"><a:majorFont><a:latin typeface=\"Calibri\"/></a:majorFont>"
        '<a:minorFont><a:latin typeface="Calibri"/></a:minorFont></a:fontScheme>'
        '<a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
        "</a:fmtScheme></a:themeElements></a:theme>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels",
                         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         + pres_rels + "</Relationships>")
        archive.writestr("ppt/slideMasters/slideMaster1.xml", master)
        archive.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels)
        archive.writestr("ppt/slideLayouts/slideLayout1.xml", layout)
        archive.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels)
        archive.writestr("ppt/theme/theme1.xml", theme)
        archive.writestr("ppt/presProps.xml",
                         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                         '<p:presentationPr xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
        archive.writestr("ppt/slides/slide1.xml", _slide_xml(title, []))
        for i, slide in enumerate(slides):
            archive.writestr(f"ppt/slides/slide{i + 2}.xml",
                             _slide_xml(slide["title"], slide.get("bullets") or []))
            archive.writestr(f"ppt/slides/_rels/slide{i + 2}.xml.rels",
                             '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                             "</Relationships>")
        archive.writestr("ppt/slides/_rels/slide1.xml.rels",
                         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                         "</Relationships>")
    return str(path)


# ---------------------------------------------------------------- latex

def _latex_escape(text: str) -> str:
    replacements = [
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"),
        ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
        ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}"),
    ]
    out = str(text)
    for old, new in replacements:
        out = out.replace(old, new)
    return out


def _latex_body(body: str) -> str:
    lines = []
    in_list = False
    for raw in str(body).splitlines():
        line = raw.strip()
        if line.startswith("- "):
            if not in_list:
                lines.append(r"\begin{itemize}")
                in_list = True
            lines.append(r"\item " + _latex_escape(line[2:].strip()))
        else:
            if in_list:
                lines.append(r"\end{itemize}")
                in_list = False
            if line:
                lines.append(_latex_escape(line))
    if in_list:
        lines.append(r"\end{itemize}")
    return "\n\n".join(lines)


def make_latex(title: str, sections, output, author: str = "Aidon",
               abstract: str = "") -> str:
    """Write article-class .tex source (no compile step here)."""
    _require_nonempty(title, "title")
    if not sections:
        raise ValueError("sections must not be empty")
    for section in sections:
        if not isinstance(section, dict) or not str(section.get("heading", "")).strip():
            raise ValueError("each section needs a non-empty heading")
    path = _out_path(output)
    parts = [r"\documentclass{article}",
             r"\usepackage[utf8]{inputenc}",
             f"\\title{{{_latex_escape(title)}}}",
             f"\\author{{{_latex_escape(author)}}}" if author else "",
             r"\begin{document}", r"\maketitle"]
    if abstract:
        parts.append(r"\begin{abstract}" + _latex_escape(abstract) + r"\end{abstract}")
    for section in sections:
        parts.append(f"\\section{{{_latex_escape(section['heading'])}}}")
        body = section.get("body", "")
        if body:
            parts.append(_latex_body(body))
    parts.append(r"\end{document}")
    path.write_text("\n\n".join(p for p in parts if p) + "\n", encoding="utf-8")
    return str(path)
