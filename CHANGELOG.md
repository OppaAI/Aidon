# Changelog

User-facing changes are recorded here. Before tagging a release, move its
entries from Unreleased into a versioned section. The release workflow uses
that section as the GitHub Release notes.

## [Unreleased]

### Added

- Docker image and Compose deployment for the WebUI, with persistent state in
  a host-mounted `aidon-data` directory and a localhost-only bind.
- GitHub release workflow that publishes a GitHub Release and a GHCR image
  when a version tag is pushed.
- Explicit WebUI bind address option; localhost remains the default.

### Fixed

- Slack replies use the bot token ahead of legacy webhooks. Durable inbox and
  reply queues keep polling responsive during approvals and preserve retries.
- Prevent old approval replies and concurrent progress updates from changing
  unrelated decisions or losing cancellation; reject spoofed localhost names.
- Validate malformed plans/checkpoints safely, retain messaging conversation
  context, and render chat source/artifact/lab links without control characters.
- Package the operating manual, load owner profiles from persistent storage,
  and lock the Modal runtime dependencies.
- Preserve corrupt outboxes and failed notifications, share optimized fallback
  memory ranking, and propagate JavaScript syntax failures through CI.

## [0.4.0] - 2026-10-31

- Initial changelog for the existing 0.4.0 project version.
