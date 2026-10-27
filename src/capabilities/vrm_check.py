"""VRM morph-target validation, no VRM library required.

A VRM avatar file is a GLB container: a 12-byte header (magic
``glTF``, version, total length), then chunks, each with its own
length and type. The JSON chunk carries the glTF document, and a
mesh primitive's ``extras.targetNames`` lists the morph targets
that primitive actually ships. The lip-sync JavaScript drives
morphs by name (``Fcl_...``); if a name it drives is not in the
model, the expression manager silently does nothing, so a model
swap can kill the lip sync without any error. This module checks
the driven names against the names in the file, before load.

Functions:
- vrm_target_names(vrm_path): set of target names in the file.
- used_morph_names(js_source): Fcl_ names appearing in JS source.
- check_vrm(vrm_path, js_source): {ok, used, available, missing, note}.
- used_names_from_audio(): names driven by the repo's own lip-sync JS.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

_GLTF_MAGIC = b"glTF"
_JSON_CHUNK_TYPE = 0x4E4F534A  # "JSON" in little-endian ASCII
_MORPH_RE = re.compile(r"Fcl_[A-Za-z0-9_]+")


def vrm_target_names(vrm_path) -> set[str]:
    """Return the mesh primitive extras targetNames in a GLB/VRM file.

    Raises FileNotFoundError when the file is missing and
    ValueError when the file is not a GLB, is truncated, or has
    no JSON chunk.
    """
    path = Path(vrm_path)
    if not path.is_file():
        raise FileNotFoundError(f"no VRM file: {path}")
    data = path.read_bytes()
    if len(data) < 12 or data[0:4] != _GLTF_MAGIC:
        raise ValueError(f"not a GLB file: {path}")
    total_length = struct.unpack_from("<I", data, 8)[0]
    if total_length > len(data):
        raise ValueError(f"truncated GLB file: {path}")
    offset = 12
    names: set[str] = set()
    saw_json = False
    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_length
        if chunk_end > len(data):
            raise ValueError(f"truncated chunk in GLB file: {path}")
        if chunk_type == _JSON_CHUNK_TYPE:
            saw_json = True
            try:
                document = json.loads(data[chunk_start:chunk_end].decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(f"invalid JSON chunk in GLB file: {path}") from exc
            for mesh in document.get("meshes", []) or []:
                for primitive in mesh.get("primitives", []) or []:
                    extras = primitive.get("extras") or {}
                    for name in extras.get("targetNames", []) or []:
                        names.add(str(name))
        offset = chunk_end
    if not saw_json:
        raise ValueError(f"no JSON chunk in GLB file: {path}")
    return names


def used_morph_names(js_source: str) -> set[str]:
    """Return every Fcl_ morph name appearing in a JS source string."""
    if not js_source:
        return set()
    return set(_MORPH_RE.findall(str(js_source)))


def check_vrm(vrm_path, js_source: str) -> dict:
    """Check driven morph names against the names in a VRM file.

    ok is True only when every used name exists in the model.
    An empty used set is not a failure: ok is True, and the note
    warns the caller that the JS drives no morphs at all.
    """
    available = vrm_target_names(vrm_path)
    used = used_morph_names(js_source)
    missing = used - available
    note = ""
    if not used:
        note = "warning: no morph names driven by this JS source"
    return {
        "ok": not missing,
        "used": sorted(used),
        "available": sorted(available),
        "missing": sorted(missing),
        "note": note,
    }


def used_names_from_audio() -> set[str]:
    """Morph names driven by the repo's own VRM lip-sync JS.

    The JS is imported and read at runtime, never copied here,
    so a change to the lip-sync script is reflected here too.
    """
    try:
        from .audio import vrm_lip_sync_js
    except ImportError:  # direct module load outside the package
        from src.capabilities.audio import vrm_lip_sync_js  # type: ignore[no-redef]
    return used_morph_names(vrm_lip_sync_js())
