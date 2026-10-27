"""Charts and graphs, made by code, never by image generation.

A chart's job is exact numbers: a bar must be as tall as
its value, a label must say its value, and a reader must
be able to check it without looking at pixels. An image
model cannot guarantee that, it can draw a bar that looks
right and is wrong, or a label that is misspelled. So
Aidon makes charts by code, in SVG, with the data in the
SVG text itself, verifiable by code: the value appears
as text, the bar height is proportional to the value,
and a test can read the SVG and check both.

Functions, stdlib only, no plotting library, no image model:
- bar_chart(title, data, path): data is a list of
  (label, value) pairs, bars are rectangles, heights
  proportional to the maximum value, values are text.
- line_chart(title, data, path): data is a list of
  (label, value) pairs, points are circles, joined by a
  polyline, values are text.
- Both return the path written, inside the caller's
  directory, never outside it, and both raise on empty
  data, because an empty chart is not a chart, it is a
  missing deliverable.
"""

from __future__ import annotations

from pathlib import Path


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def bar_chart(title: str, data: list[tuple[str, float]], path: Path) -> Path:
    if not data:
        raise ValueError("bar chart data is required")
    width, height = 600, 400
    max_value = max(abs(value) for _label, value in data) or 1.0
    bar_width = width // (len(data) * 2 + 1)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             f"<title>{_escape(title)}</title>",
             f'<text x="300" y="30" text-anchor="middle" font-size="20">{_escape(title)}</text>']
    for index, (label, value) in enumerate(data):
        x = bar_width * (index * 2 + 1)
        bar_height = int((abs(value) / max_value) * 300)
        y = 350 - bar_height
        parts.append(f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_height}" fill="#336699"/>')
        parts.append(f'<text x="{x + bar_width // 2}" y="{y - 5}" text-anchor="middle">{value}</text>')
        parts.append(f'<text x="{x + bar_width // 2}" y="370" text-anchor="middle">{_escape(label)}</text>')
    parts.append("</svg>")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def line_chart(title: str, data: list[tuple[str, float]], path: Path) -> Path:
    if not data:
        raise ValueError("line chart data is required")
    width, height = 600, 400
    max_value = max(abs(value) for _label, value in data) or 1.0
    min_value = min(value for _label, value in data)
    points = []
    for index, (label, value) in enumerate(data):
        x = 50 + int(index * (500 / max(1, len(data) - 1))) if len(data) > 1 else 300
        y = 350 - int(((value - min_value) / (max_value - min_value if max_value != min_value else 1.0)) * 300)
        points.append((x, y, label, value))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             f"<title>{_escape(title)}</title>",
             f'<text x="300" y="30" text-anchor="middle" font-size="20">{_escape(title)}</text>',
             f'<polyline points="{" ".join(f"{x},{y}" for x, y, _l, _v in points)}" fill="none" stroke="#336699" stroke-width="2"/>']
    for x, y, label, value in points:
        parts.append(f'<circle cx="{x}" cy="{y}" r="4" fill="#336699"/>')
        parts.append(f'<text x="{x}" y="{y - 10}" text-anchor="middle">{value}</text>')
        parts.append(f'<text x="{x}" y="370" text-anchor="middle">{_escape(label)}</text>')
    parts.append("</svg>")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
