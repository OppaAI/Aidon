"""Discovery grounds selectable transports in registry records."""
from src.capabilities.mcp_discovery import discover_mcp


def test_discovery_records_sources_stars_and_exact_pinned_command():
    def get(url):
        if url.startswith("https://api.github.com/"):
            return {"stargazers_count": 31, "license": {"spdx_id": "MIT"}, "owner": {"login": "publisher"}}
        return {"servers": [{"server": {
            "name": "io.github.publisher/notes", "version": "1.2.3", "description": "Organize notes",
            "repository": {"url": "https://github.com/publisher/notes"},
            "packages": [{"registryType": "npm", "identifier": "@publisher/notes", "version": "1.2.3",
                          "environmentVariables": [{"name": "NOTES_TOKEN", "isSecret": True}]}]
        }}]}
    options, warnings = discover_mcp(["notes"], get)
    assert not warnings and len(options) == 1
    option = options[0]
    assert option["connection"] == {"transport": "stdio", "command": "npx", "args": ["-y", "@publisher/notes@1.2.3"], "env": {"NOTES_TOKEN": "$NOTES_TOKEN"}}
    assert option["stars"] == 31 and option["license"] == "MIT"
    assert option["source"].endswith("io.github.publisher%2Fnotes/versions/latest")
    assert option["pricing"].startswith("Not verified")


def test_discovery_does_not_invent_arguments_or_resolve_url_templates():
    records = [{"server": {"name": "com.example/tenant", "remotes": [{"type": "streamable-http", "url": "https://{tenant}.example.com/mcp"}]}},
               {"server": {"name": "com.example/package", "packages": [{"registryType": "npm", "identifier": "example", "version": "latest"}]}},
               {"server": {"name": "com.example/http", "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}]}}]
    options, _ = discover_mcp(["example"], lambda url: {"servers": records})
    assert options[0]["connection"] is None
    assert options[1]["connection"] is None
    assert options[2]["connection"] == {"transport": "http", "url": "https://example.com/mcp"}


def test_registry_failure_is_reported_without_fake_options():
    def get(url): raise OSError("offline")
    options, warnings = discover_mcp(["notes"], get)
    assert options == [] and warnings
