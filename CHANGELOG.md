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

## [0.4.0] - 2026-10-31

- Initial changelog for the existing 0.4.0 project version.
