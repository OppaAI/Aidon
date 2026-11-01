"""Exercise the active chat renderer, including its artifact/lab links."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def render(text):
    source = (Path(__file__).parents[2] / "src/interface/static/app.js").read_text()
    helpers = source[source.index("  function esc(s)"):source.index("  function addMsg(")]
    result = subprocess.run(
        ["node", "-e", helpers + "\nprocess.stdout.write(JSON.stringify(md(" +
         json.dumps(text) + ")));"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_chat_links_and_artifact_paths_render_without_control_characters():
    html = render("[source](https://example.com/page)\n\n"
                  "[lab](/labs?lab=skill&source_run=abc)\n\n"
                  "[file](file://workspace/run/a%20b.md)")
    assert 'href="https://example.com/page"' in html
    assert 'href="/labs?lab=skill&amp;source_run=abc"' in html
    assert 'href="/api/files/workspace/run/a%20b.md"' in html
    assert not any(ord(char) < 32 and char not in "\n\r\t" for char in html)


def test_chat_renderer_escapes_html_and_refuses_unsafe_link_schemes():
    html = render('<script>alert(1)</script> [bad](javascript:alert) [bad](//evil.test)')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<a " not in html


def test_chat_code_and_tables_stay_separate():
    html = render('```python\nprint("<tag>")\n```\n\n| A | B |\n|---|---|\n| x | y |')
    assert '<pre><code>print(&quot;&lt;tag&gt;&quot;)</code></pre>' in html
    assert '<table class="md-table">' in html
