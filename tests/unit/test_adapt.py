"""Adapt word mapping: only changed words map, case is preserved."""

from src.agentic.adapt import (
    _replace_word,
    _word_map,
    instantiate_template,
)


def test_word_map_ignores_shared_words_on_order_shift():
    # Old [red, snake], new [worm, red]: red is shared, so only
    # snake -> worm maps. The old full-list zip mapped red -> worm.
    assert _word_map("red snake game", "worm red levels") == {"snake": "worm"}


def test_word_map_single_changed_word():
    assert _word_map("Write a Snake game", "Write a Worm game") == {"snake": "worm"}


def test_replace_word_preserves_capitalization():
    assert _replace_word(
        "Snake and snake and SNAKE", {"snake": "worm"}
    ) == "Worm and worm and WORM"


def test_replace_word_respects_word_boundaries():
    assert _replace_word("snakeoil and snake", {"snake": "worm"}) == "snakeoil and worm"


def test_instantiate_template_uses_first_distinctive_word():
    # Pinned behavior: template slots are single-word, so a multi-word
    # goal fills every slot with its first distinctive word. A
    # multi-word placeholder scheme is a template-format change, not
    # part of this fix.
    template = [{"tool": "write",
                 "args": {"file": "{task}.py", "text": "{Task} game"},
                 "deliverable": "{task}.py"}]
    adapted = instantiate_template(template, "Write a Worm game with levels")
    assert adapted[0]["args"]["file"] == "worm.py"
    assert adapted[0]["args"]["text"] == "Worm game"
