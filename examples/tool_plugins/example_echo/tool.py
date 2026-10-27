"""Read-only example tool for the manifest in this directory."""


def example_echo(args: dict) -> str:
    return f"echo: {str(args.get('text', ''))}"
