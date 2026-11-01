from src.system.cli_parser import build_parser


def test_webui_bind_defaults_to_localhost():
    args = build_parser().parse_args(["webui", "serve"])
    assert args.host == "127.0.0.1"


def test_webui_bind_can_be_set_for_container_network():
    args = build_parser().parse_args(
        ["webui", "serve", "--host", "0.0.0.0", "--port", "8765"])
    assert args.host == "0.0.0.0"
    assert args.port == 8765


def test_cli_webui_registers_one_goal_handler_for_all_chat_sources(
        tmp_path, monkeypatch):
    """Exercise the real CLI WebUI factory, not a hand-built server."""
    from types import SimpleNamespace

    from src.interface.messaging import MessagingConfig
    from src.system.config import Config
    from src.system import cli_webui

    captured = {}

    class FakeServer:
        def __init__(self, home, *, dispatcher, **_kwargs):
            captured["home"] = home
            captured["dispatcher"] = dispatcher
            self.voice_announce = False

        def serve(self, **_kwargs):
            return None

    class FakeReplySender:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("src.interface.webui.WebUIServer", FakeServer)
    monkeypatch.setattr("src.interface.reply_delivery.ReplySender", FakeReplySender)
    monkeypatch.setattr(MessagingConfig, "from_env", classmethod(lambda _cls: MessagingConfig()))
    monkeypatch.setattr(cli_webui, "_startup_style", lambda *_args, **_kwargs: None)

    config = Config(home=tmp_path)
    args = SimpleNamespace(webhook_only=False, host="127.0.0.1", port=8765)
    assert cli_webui.run_webui(args, config, SimpleNamespace(embedder=None),
                               SimpleNamespace(), model_router=None) == 0

    handlers = captured["dispatcher"].handlers
    assert {"webui", "whatsapp", "slack"}.issubset(handlers)
    assert handlers["webui"] is handlers["whatsapp"] is handlers["slack"]
