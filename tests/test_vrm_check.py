import json
import struct

import pytest

from src.capabilities.vrm_check import (
    check_vrm,
    used_morph_names,
    used_names_from_audio,
    vrm_target_names,
)

_JSON_CHUNK_TYPE = 0x4E4F534A  # "JSON"
_NAMES = ["Fcl_MTH_Small", "Fcl_MTH_Large", "Fcl_EYE_Close", "Fcl_ALL_Joy"]


def _glb_bytes(names, extra_meshes=None):
    document = {
        "meshes": [
            {"primitives": [{"extras": {"targetNames": names}}]},
            *(extra_meshes or []),
        ]
    }
    json_bytes = json.dumps(document).encode("utf-8")
    chunk = struct.pack("<II", len(json_bytes), _JSON_CHUNK_TYPE) + json_bytes
    total = 12 + len(chunk)
    return b"glTF" + struct.pack("<II", 2, total) + chunk


def test_vrm_target_names_returns_exact_names(tmp_path):
    path = tmp_path / "avatar.vrm"
    path.write_bytes(_glb_bytes(_NAMES))
    assert vrm_target_names(path) == set(_NAMES)


def test_vrm_target_names_collects_across_primitives(tmp_path):
    path = tmp_path / "avatar.vrm"
    path.write_bytes(_glb_bytes(
        ["Fcl_MTH_Small"],
        extra_meshes=[{"primitives": [{"extras": {"targetNames": ["Fcl_EYE_Close"]}}]}]))
    assert vrm_target_names(path) == {"Fcl_MTH_Small", "Fcl_EYE_Close"}


def test_check_vrm_passes_when_all_used_names_present(tmp_path):
    path = tmp_path / "avatar.vrm"
    path.write_bytes(_glb_bytes(_NAMES))
    js = "vrm.expressionManager.setValue('Fcl_MTH_Small', 1); setValue('Fcl_EYE_Close', 0);"
    result = check_vrm(path, js)
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["used"] == ["Fcl_EYE_Close", "Fcl_MTH_Small"]
    assert result["available"] == sorted(_NAMES)


def test_check_vrm_fails_listing_missing_name(tmp_path):
    path = tmp_path / "avatar.vrm"
    path.write_bytes(_glb_bytes(["Fcl_MTH_Small"]))
    result = check_vrm(path, "setValue('Fcl_MTH_Small', 1); setValue('Fcl_EYE_Close', 1);")
    assert result["ok"] is False
    assert result["missing"] == ["Fcl_EYE_Close"]


def test_check_vrm_empty_used_is_ok_with_note(tmp_path):
    path = tmp_path / "avatar.vrm"
    path.write_bytes(_glb_bytes(_NAMES))
    result = check_vrm(path, "console.log('no morphs here');")
    assert result["ok"] is True
    assert result["used"] == []
    assert result["note"]


def test_non_glb_raises_value_error(tmp_path):
    path = tmp_path / "not-a-vrm.txt"
    path.write_text("hello, not a GLB")
    with pytest.raises(ValueError):
        vrm_target_names(path)


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        vrm_target_names(tmp_path / "does-not-exist.vrm")


def test_used_morph_names_extracts_from_js_snippet():
    js = "setValue('Fcl_MTH_A', 1); setValue('Fcl_EYE_Close', 0); const x = 'Fcl_ALL_Joy';"
    assert used_morph_names(js) == {"Fcl_MTH_A", "Fcl_EYE_Close", "Fcl_ALL_Joy"}


def test_used_names_from_audio():
    names = used_names_from_audio()
    assert names, "the lip-sync JS should drive at least one morph"
    # The repo's lip-sync JS drives mouth morphs and the eye-close
    # morph: RMS volume to Fcl_MTH_*, a blink timer to Fcl_EYE_Close.
    assert "Fcl_EYE_Close" in names
    assert any(name.startswith("Fcl_MTH_") for name in names)
