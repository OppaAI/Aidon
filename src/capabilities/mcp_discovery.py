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
    # Prefer the published hosted server when Aidon can connect to it.
    packages = list(server.get("packages", []))
    for remote in server.get("remotes", []):
        url = str(remote.get("url", ""))
        if (remote.get("type") == "streamable-http" and url.startswith("https://")
                and "{" not in url and not remote.get("headers")):
            return {"transport": "http", "url": url}
    packages.sort(key=lambda package: not any(
        variable.get("isSecret") or variable.get("isRequired")
        for variable in package.get("environmentVariables", [])))
    for package in packages:
        config = _package_connection(package)
        if config:
            return config
    return None


def _auth_hints(server, connection):
    """Report credential names and OAuth uncertainty from published metadata."""
    names = set()
    if connection and connection.get("transport") == "stdio":
        names.update(key for key, value in (connection.get("env") or {}).items()
                     if str(value).startswith("$"))
    has_http = bool(connection and connection.get("transport") == "http")
    auth_headers = sorted({str(key) for remote in server.get("remotes", [])
                           if connection is None or (has_http and remote.get("url") == connection.get("url"))
                           for key in (remote.get("headers") or {})})
    return sorted(names), auth_headers, bool(has_http and not names and not auth_headers)


def _package_connection(package):
    name = str(package.get("identifier", ""))
    version = str(package.get("version", ""))
    if (package.get("registryType") not in ("npm", "pypi")
            or (package.get("transport") or {}).get("type", "stdio") != "stdio"
            or not re.fullmatch(r"[@A-Za-z0-9_./-]+", name)
            or not re.fullmatch(r"[A-Za-z0-9_.+-]+", version)
            or version in ("latest", "main")
            or package.get("runtimeArguments")):
        return None
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
        return None
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
    if not complete:
        return None
    if package["registryType"] == "npm":
        return {"transport": "stdio", "command": "npx", "args": ["-y", f"{name}@{version}", *args], "env": env}
    return {"transport": "stdio", "command": "uvx", "args": [f"{name}=={version}", *args], "env": env}


def _publisher_matches_website(server, repo, get):
    """Verify publisher control using its registry namespace or GitHub site."""
    website = str(server.get("websiteUrl", ""))
    site_host = (urllib.parse.urlsplit(website).hostname or "").lower().removeprefix("www.")
    # The official MCP Registry authenticates reverse-DNS namespaces against
    # their publisher's domain (for example, net.todoist -> todoist.net).
    # When the published website is on that domain, this remains verifiable
    # even if GitHub's API is rate-limited and stars cannot be fetched.
    namespace = str(server.get("name", "")).split("/", 1)[0].lower()
    labels = namespace.split(".")
    if len(labels) >= 2 and all(labels):
        namespace_domain = ".".join(reversed(labels))
        remote_hosts = [(urllib.parse.urlsplit(str(remote.get("url", ""))).hostname or "").lower()
                        for remote in server.get("remotes", [])
                        if str(remote.get("url", "")).startswith("https://")
                        and "{" not in str(remote.get("url", ""))]
        if (site_host == namespace_domain
                or site_host.endswith("." + namespace_domain)
                or any(host == namespace_domain or host.endswith("." + namespace_domain)
                       for host in remote_hosts)):
            return True
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/[A-Za-z0-9_.-]+(?:\.git)?/?", repo)
    if not match or not site_host:
        return False
    try:
        owner = get("https://api.github.com/users/" + match[1])
        profile_url = str(owner.get("blog", ""))
        owner_host = (urllib.parse.urlsplit(profile_url if "://" in profile_url else "https://" + profile_url).hostname or "").lower().removeprefix("www.")
        return bool(site_host and owner_host and
                    (site_host == owner_host or site_host.endswith("." + owner_host)
                     or owner_host.endswith("." + site_host)))
    except Exception:
        return False


def discover_mcp(queries, get_json=None, limit=8):
    get = get_json or _get_json
    if isinstance(queries, str):
        queries = [queries]
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
            connection = _connection(server)
            auth_env_names, auth_headers, may_use_oauth = _auth_hints(server, connection)
            option = {"id": hashlib.sha256(name.encode()).hexdigest()[:20],
                      "name": name, "title": str(server.get("title") or name),
                      "description": str(server.get("description", ""))[:2000],
                      "author": name.split("/", 1)[0], "version": str(server.get("version", "")),
                      "repository": repo, "website": str(server.get("websiteUrl", "")),
                      "source": REGISTRY + "/" + urllib.parse.quote(name, safe="") + "/versions/latest",
                      "connection": connection, "stars": None,
                      "stars_source": "", "license": "Unknown", "official": False}
            repo_match = re.fullmatch(
                r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",
                repo)
            if repo_match:
                owner, repository = repo_match.groups()
                option["readme_source"] = (
                    f"https://github.com/{owner}/{repository}/blob/HEAD/README.md")
                option["setup_source"] = (
                    f"https://github.com/{owner}/{repository}/blob/HEAD/docs/mcp-server.md")
            option.update({"auth_env_names": auth_env_names,
                           "auth_headers": auth_headers,
                           "may_use_oauth": may_use_oauth})
            match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", repo)
            if match:
                try:
                    github = get("https://api.github.com/repos/" + match[1])
                    option["stars"] = github.get("stargazers_count")
                    option["stars_source"] = repo
                    option["license"] = (github.get("license") or {}).get("spdx_id") or "Unknown"
                    option["author"] = (github.get("owner") or {}).get("login") or option["author"]
                except Exception:
                    pass
            option["official"] = _publisher_matches_website(server, repo, get)
            results.append(option)
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
    # Keep the registry's relevance order within an equal trust/star tier.
    results.sort(key=lambda item: (not item.get("official", False),
                                   -(item.get("stars") or 0)))
    return results, errors


def read_mcp_setup_docs(option, read_document=None, max_chars=18000):
    """Read a selected server's GitHub README and conventional setup guide.

    Only repository URLs already supplied by the registry are accepted. The
    document reader retains its HTTPS/SSRF and redirect protections. Remote
    documentation is untrusted reference text; callers must not treat it as
    executable instructions or let it override approval boundaries.
    """
    repo = str(option.get("repository", ""))
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",
        repo)
    if not match:
        return {"readme_url": "", "setup_url": "", "readme": "",
                "setup": "", "error": "No verified GitHub repository was published."}
    owner, repository = match.groups()
    if read_document is None:
        from .documents import read_document as read_document
    result = {}
    remaining = max(1000, int(max_chars))
    for key, url_key in (("readme", "readme_url"), ("setup", "setup_url")):
        if remaining <= 0:
            result[key] = ""
            continue
        relative = "README.md" if key == "readme" else "docs/mcp-server.md"
        content, selected_url = "", ""
        for branch in ("main", "master"):
            candidate = f"https://raw.githubusercontent.com/{owner}/{repository}/{branch}/{relative}"
            try:
                candidate_content = str(read_document(candidate))
            except Exception:
                continue
            if not candidate_content.startswith(("READ_BLOCKED:", "READ_FAILED:")):
                content, selected_url = candidate_content, candidate
                break
        content = content[:remaining]
        result[key] = content
        result[url_key] = selected_url or (
            f"https://github.com/{owner}/{repository}/blob/main/{relative}")
        remaining -= len(content)
    return result
