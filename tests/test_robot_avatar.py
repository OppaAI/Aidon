"""Robot SVG avatar as default, VRM as fallback, with blink, idle, speaking mouth, karaoke typewriter."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.webui import CHAT_HTML, VRM_HTML


def test_robot_avatar_default():
    assert 'id="robot-avatar"' in CHAT_HTML
    assert 'id="avatar" aria-label="Aidon avatar, robot, default, VRM is the fallback"' in CHAT_HTML
    assert "robot-blink" in CHAT_HTML
    assert "robot-bob" in CHAT_HTML
    assert "robot-head-sway" in CHAT_HTML
    assert "robot-arm-sway" in CHAT_HTML
    assert 'class="robot-eye"' in CHAT_HTML


def test_mouth_smile_waveform_and_karaoke_sync():
    assert 'id="mouth-smile"' in CHAT_HTML
    assert 'id="mouth-open"' in CHAT_HTML
    assert "robot-mouth-wave" in CHAT_HTML
    assert "robot-wave" in CHAT_HTML
    assert "wave-bars" in CHAT_HTML
    assert "typewriterKaraoke" in CHAT_HTML
    assert "karaoke-word" in CHAT_HTML
    assert "mouthForChar" in CHAT_HTML
    # Vowel-driven mouth, not true phonemes, documented in code
    assert "aeiou" in CHAT_HTML


def test_vrm_is_fallback_not_default():
    assert "VRM avatar, fallback" in CHAT_HTML
    assert "VRM avatar, fallback" in VRM_HTML
    assert "robot SVG" in VRM_HTML
