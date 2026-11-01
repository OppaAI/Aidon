from src.system.cli_parser import build_parser


def test_webui_bind_defaults_to_localhost():
    args = build_parser().parse_args(["webui", "serve"])
    assert args.host == "127.0.0.1"


def test_webui_bind_can_be_set_for_container_network():
    args = build_parser().parse_args(
        ["webui", "serve", "--host", "0.0.0.0", "--port", "8765"])
    assert args.host == "0.0.0.0"
    assert args.port == 8765
