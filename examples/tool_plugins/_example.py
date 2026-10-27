"""Example plugin for Aidon's current manifest-based plugin system.

Place this module beside a ``plugin.json`` manifest in a plugin directory.
The directory can then be scanned, reviewed, and adopted through Aidon's
plugin approval flow.
"""


def example_echo(args: dict) -> str:
    """Return the supplied text without side effects."""
    return f"echo: {str(args.get('text', ''))}"
