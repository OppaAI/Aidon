"""Read-only MCP discovery from published registry records, never guessed commands."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request

REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"


def _get_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Aidon-MCP-Discovery"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def _connection(server):
    """Offer only fully specified transports supported by Aidon's MCP client."""
    for remote in server.get("remotes", []):
        url = str(remote.get("url", ""))
        if (remote.get("type") == "streamable-http" and url.startswith("https://")
                and "{" not in url and not remote.get("headers")):
            return {"transport": "http", "url": url}
    for package in server.get("packages", []):
        name = str(package.get("identifier", ""))
        version = str(package.get("version", ""))
        if (package.get("registryType") not in ("npm", "pypi")
                or not re.fullmatch(r"[@A-Za-z0-9_./-]+", name)
                or not re.fullmatch(r"[A-Za-z0-9_.+-]+", version)
                or version in ("latest", "main")
                or package.get("runtimeArguments")):
            continue
        args = []
        complete = True
        for argument in package.get("packageArguments", []):
            value = argument.get("value") or argument.get("default")
            if value is None and argument.get("isRequired"):
                complete = False
            if value is not None:
                if "{" in str(value):
                    complete = False
                if argument.get("type") == "named":
                    args.append(str(argument.get("name", "")))
                args.append(str(value))
        if not complete:
            continue
        env = {}
        for variable in package.get("environmentVariables", []):
            key = str(variable.get("name", ""))
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                complete = False
                break
            if variable.get("isSecret") or variable.get("isRequired"):
                env[key] = "$" + key
            elif variable.get("default") is not None:
                env[key] = str(variable["default"])
        if complete:
            if package["registryType"] == "npm":
                return {"transport": "stdio", "command": "npx", "args": ["-y", f"{name}@{version}", *args], "env": env}
            return {"transport": "stdio", "command": "uvx", "args": [f"{name}=={version}", *args], "env": env}
    return None


def discover_mcp(queries, get_json=None, limit=8):
    get = get_json or _get_json
    results, errors, seen = [], [], set()
    for query in list(queries)[:3]:
        query = str(query).strip()[:80]
        if len(query) < 3:
            continue
        source = REGISTRY + "?" + urllib.parse.urlencode({"search": query, "version": "latest", "limit": limit})
        try:
            response = get(source)
        except Exception as exc:
            errors.append(f"Registry search unavailable ({type(exc).__name__})")
            continue
        for item in response.get("servers", []):
            server = item.get("server", {})
            name = str(server.get("name", ""))
            meta = item.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})
            if not name or name in seen or meta.get("status", "active") != "active":
                continue
            seen.add(name)
            repo = str((server.get("repository") or {}).get("url", ""))
            option = {"id": hashlib.sha256(name.encode()).hexdigest()[:20],
                      "name": name, "title": str(server.get("title") or name),
                      "description": str(server.get("description", ""))[:2000],
                      "author": name.split("/", 1)[0], "version": str(server.get("version", "")),
                      "repository": repo, "website": str(server.get("websiteUrl", "")),
                      "source": REGISTRY + "/" + urllib.parse.quote(name, safe="") + "/versions/latest",
                      "connection": _connection(server), "stars": None, "license": "Unknown",
                      "pricing": "Not verified; check publisher pricing", "reviews": "No verified review rating available"}
            match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", repo)
            if match:
                try:
                    github = get("https://api.github.com/repos/" + match[1])
                    option["stars"] = github.get("stargazers_count")
                    option["license"] = (github.get("license") or {}).get("spdx_id") or "Unknown"
                    option["author"] = (github.get("owner") or {}).get("login") or option["author"]
                except Exception:
                    pass
            results.append(option)
            if len(results) >= limit:
                return results, errors
    return results, errors
