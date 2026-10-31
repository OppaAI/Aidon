"""Robot SVG avatar as default, with optional explicit VRM viewing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.interface.webui import CHAT_HTML, VRM_HTML


def test_robot_avatar_default():
    assert 'id="robot-avatar"' in CHAT_HTML
    assert 'id="avatar" aria-label="Aidon robot avatar, default; VRM is available separately"' in CHAT_HTML
    assert "robot-blink" in CHAT_HTML
    assert "robot-bob" in CHAT_HTML
    assert "robot-head-sway" in CHAT_HTML
    assert "robot-arm-sway" in CHAT_HTML
    assert 'class="robot-eye"' in CHAT_HTML


def test_mouth_smile_waveform_and_karaoke_sync():
    assert 'id="mouth-smile"' in CHAT_HTML
    assert 'id="mouth-open"' in CHAT_HTML
    assert "setMouthPose" in CHAT_HTML
    assert "robot-wave" in CHAT_HTML
    assert "wave-bars" in CHAT_HTML
    assert "typewriterKaraoke" in CHAT_HTML
    assert "karaoke-word" in CHAT_HTML
    assert "mouthForChar" in CHAT_HTML
    # Approximate vowel groups, not full phoneme recognition.
    assert "/[aáàâäãå]/i" in CHAT_HTML


def test_vrm_is_explicit_and_robot_remains_default():
    assert "Open VRM avatar" in CHAT_HTML
    assert "VRM avatar (optional)" in VRM_HTML
    assert "robot avatar is always the default in chat" in VRM_HTML
