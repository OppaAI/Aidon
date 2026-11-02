"""Provider separation and the actual image HTTP request contract."""
import base64
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from src.capabilities.image_gen import image_api_config, edit_image, edit_image_files
from src.system.config import Config


def test_baseten_chat_does_not_supply_meta_image_credentials(tmp_path):
    config = Config(home=tmp_path, api_base="https://inference.baseten.co/v1", api_key="chat-key")
    with pytest.raises(ValueError, match="AIDON_IMAGE_API_KEY"):
        image_api_config(config)
    config.image_api_key = "meta-key"
    assert image_api_config(config) == ("https://api.meta.ai/v1", "meta-key")


def test_meta_chat_can_reuse_its_key_but_cross_origin_override_cannot(tmp_path):
    config = Config(home=tmp_path, api_base="https://api.meta.ai/v1", api_key="meta-key")
    assert image_api_config(config) == (config.api_base, "meta-key")
    config.image_api_endpoint = "https://another-provider.example/v1"
    with pytest.raises(ValueError, match="AIDON_IMAGE_API_KEY"):
        image_api_config(config)


def test_legacy_active_meta_env_is_recognized(tmp_path, monkeypatch):
    monkeypatch.setattr("src.system.config.load_dotenv", lambda: None)
    monkeypatch.setenv("AIDON_IMAGE_API_KEY", "")
    monkeypatch.setenv("AIDON_IMAGE_API_ENDPOINT", "")
    monkeypatch.setenv("AIDON_META_API_KEY", "legacy-meta-key")
    monkeypatch.setenv("AIDON_META_ENDPOINT", "https://api.meta.ai/v1")
    config = Config.load(tmp_path)
    assert image_api_config(config) == ("https://api.meta.ai/v1", "legacy-meta-key")


def test_cli_and_workflow_edits_encode_local_files_with_real_mime(tmp_path, monkeypatch):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg-content")
    requests = []
    response = json.dumps({"data": [{"b64_json": base64.b64encode(b"edited-image").decode()}]}).encode()
    def urlopen(request, timeout):
        requests.append(request)
        return io.BytesIO(response)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert edit_image("cafe", "image-key", "https://api.meta.ai/v1", [str(photo)]) == b"edited-image"
    assert edit_image_files("cafe", "image-key", "https://api.meta.ai/v1", [str(photo)]) == b"edited-image"
    for request in requests:
        payload = json.loads(request.data)
        assert request.full_url == "https://api.meta.ai/v1/images/edits"
        assert payload["images"] == [{"image_url": "data:image/jpeg;base64," + base64.b64encode(photo.read_bytes()).decode()}]
        assert payload["response_format"] == "b64_json"
        assert str(photo) not in request.data.decode()


def test_provider_error_preserves_detail_and_redacts_key(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
            io.BytesIO(json.dumps({"error": "Invalid key sensitive-key"}).encode()))
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(RuntimeError) as caught:
        edit_image("cafe", "sensitive-key", "https://api.meta.ai/v1", [b"image-bytes"])
    assert "HTTP 401 at api.meta.ai/v1/images/edits" in str(caught.value)
    assert "Invalid key [redacted]" in str(caught.value)
    assert "sensitive-key" not in str(caught.value)
