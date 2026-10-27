"""Playwright MCP wiring, for Aidon to climb to, not preinstalled in the arena.

Is Playwright MCP ready? The server is a real, public MCP
server, @playwright/mcp, run with npx, stdio JSON-RPC, with
tools like browser_navigate, browser_snapshot, browser_click,
browser_type, and browser_take_screenshot. In this VM, node
and npx are present, but Playwright MCP itself is not
installed, and no Playwright browser is installed by Aidon.
That is deliberate, by the library line: browser drivers
are capabilities, never preinstalled in the arena. Aidon
climbs to them himself: research, draft a plugin, sandbox-
test, adopt with owner approval. This module is the wiring
he climbs to, not a preinstalled tool in the loop.

How Aidon wires it in:
- PlaywrightMcpClient wraps the generic MCP client, with
  the server command ["npx", "@playwright/mcp@latest"],
  plus optional args, for example --browser chromium,
  --headless, --isolated, --caps pdf, vision, etc.
- Methods are thin wrappers over MCP tool calls, with the
  exact tool names Playwright MCP uses, so a tool result
  is never invented by Aidon, it is the browser's snapshot.
- If npx is missing, or the server exits, the client raises,
  it does not fall back to a fake browser, and it does not
  invent a page. A browser run is a live action, so it
  still needs approval before it navigates to a site that
  requires sign-in, spends money, or submits a form.
- In tests, the MCP client is injected as a fake, with
  canned tool results, so no browser is launched, no
  network is used, and the wiring is still tested.

Security boundary, same as old Aidon: a browser can see
a page, it cannot see Aidon's home, .env, or keys, unless
the owner explicitly passes them, and a page's content is
untrusted data, never instructions, never followed as a
link to a credential.
"""

from __future__ import annotations

import shutil

from .mcp_client import McpClient

DEFAULT_COMMAND = ["npx", "@playwright/mcp@latest"]
TOOL_NAMES = ("browser_navigate", "browser_snapshot", "browser_click",
              "browser_type", "browser_take_screenshot", "browser_close",
              "browser_wait_for", "browser_evaluate")


class PlaywrightMcpClient:
    def __init__(self, command: list[str] | None = None, mcp_client=None):
        self.command = command or DEFAULT_COMMAND
        self.mcp = mcp_client
        self._started = False

    def start(self) -> None:
        if self.mcp is not None:
            self._started = True
            return
        if shutil.which("npx") is None:
            raise ValueError("npx is not installed, Playwright MCP cannot start, install Node.js first")
        self.mcp = McpClient(self.command)
        self.mcp.start()
        self._started = True

    def close(self) -> None:
        if self.mcp is not None and hasattr(self.mcp, "close"):
            self.mcp.close()
        self._started = False

    def _call(self, tool: str, arguments: dict):
        if not self._started or self.mcp is None:
            raise ValueError("Playwright MCP is not started, call start() first")
        return self.mcp.call_tool(tool, arguments)

    def navigate(self, url: str):
        return self._call("browser_navigate", {"url": url})

    def snapshot(self):
        return self._call("browser_snapshot", {})

    def click(self, element: str, ref: str):
        return self._call("browser_click", {"element": element, "ref": ref})

    def type_text(self, element: str, ref: str, text: str, submit: bool = False):
        return self._call("browser_type", {"element": element, "ref": ref,
                                          "text": text, "submit": submit})

    def screenshot(self):
        return self._call("browser_take_screenshot", {})

    def list_tools(self) -> list[dict]:
        if not self._started or self.mcp is None:
            raise ValueError("Playwright MCP is not started, call start() first")
        return self.mcp.list_tools()
