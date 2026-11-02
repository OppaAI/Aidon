"""Discovery grounds selectable transports in registry records."""
from src.capabilities.mcp_discovery import discover_mcp


def test_discovery_records_sources_stars_and_exact_pinned_command():
    def get(url):
        if url.startswith("https://api.github.com/repos/"):
            return {"stargazers_count": 31, "license": {"spdx_id": "MIT"}, "owner": {"login": "publisher"}}
        if url.startswith("https://api.github.com/users/"):
            return {"blog": "https://publisher.example"}
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
    assert "pricing" not in option and "reviews" not in option
    assert option["stars_source"] == "https://github.com/publisher/notes"


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


def test_secret_package_is_preferred_over_oauth_remote_and_publisher_match_ranks_first():
    records = [{"server": {
        "name": "net.todoist/mcp", "title": "Todoist", "websiteUrl": "https://developer.todoist.com",
        "repository": {"url": "https://github.com/Doist/todoist-mcp"},
        "remotes": [{"type": "streamable-http", "url": "https://ai.todoist.net/mcp"}],
        "packages": [{"registryType": "npm", "identifier": "@doist/todoist-mcp",
                      "version": "13.4.0", "environmentVariables": [
                          {"name": "TODOIST_API_KEY", "isSecret": True}]}]}},
        {"server": {"name": "community/todoist", "repository": {"url": ""}}}]

    def get(url):
        if url.startswith("https://api.github.com/repos/Doist/"):
            return {"stargazers_count": 5, "license": {"spdx_id": "MIT"}, "owner": {"login": "Doist"}}
        if url == "https://api.github.com/users/Doist":
            return {"blog": "https://www.todoist.com"}
        return {"servers": records}

    options, _ = discover_mcp(["todoist"], get)
    todoist = next(row for row in options if row["name"] == "net.todoist/mcp")
    assert todoist["connection"]["command"] == "npx"
    assert todoist["connection"]["env"] == {"TODOIST_API_KEY": "$TODOIST_API_KEY"}
    assert todoist["official"] is True
    assert options[0] is todoist
