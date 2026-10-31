""""does not contain" criterion form, for allergy and dietary criteria.

The absence form exists because a planner that can only assert
presence cannot express an allergen ban ("menu.md does not contain
milk"), and the old fallback was a soft criterion that failed
verification on every attempt. Matching is case-insensitive because
allergen names vary in case on labels and in prose.
"""

from src.agentic.verify import check_criterion, verify_plan


def _workdir(tmp_path):
    wd = tmp_path / "workdir"
    wd.mkdir()
    (wd / "menu.md").write_text(
        "Flour, sugar, butter, Whey powder, oat milk\n", encoding="utf-8")
    return wd


def test_does_not_contain_absent_passes(tmp_path):
    wd = _workdir(tmp_path)
    ok, message = check_criterion(wd, "menu.md does not contain egg")
    assert ok is True
    assert "lacks" in message


def test_does_not_contain_present_fails_naming_file(tmp_path):
    wd = _workdir(tmp_path)
    ok, message = check_criterion(wd, "menu.md does not contain butter")
    assert ok is False
    assert "menu.md" in message
    assert "contains butter" in message


def test_does_not_contain_is_case_insensitive(tmp_path):
    wd = _workdir(tmp_path)
    # The file says "Whey", the criterion says "whey": a label-case
    # difference must not let a hidden milk ingredient through.
    ok, _ = check_criterion(wd, "menu.md does not contain whey")
    assert ok is False
    ok, _ = check_criterion(wd, "menu.md does not contain WHEY")
    assert ok is False


def test_does_not_contain_missing_file(tmp_path):
    wd = _workdir(tmp_path)
    ok, message = check_criterion(wd, "nope.md does not contain milk")
    assert ok is False
    assert "missing" in message


def test_allergy_menu_verifies(tmp_path):
    # A milk/egg/gluten-free menu's Done-when criteria, in the forms
    # the planner now emits: one absence check per allergen and its
    # hidden names, plus a label-checks section the user confirms
    # on the package for claims no text can prove.
    wd = tmp_path / "workdir"
    wd.mkdir()
    (wd / "menu.md").write_text(
        "Oatcakes, roast beef, carrots\nLabel checks: confirm the "
        "certified gluten-free logo on the oatcake package.\n",
        encoding="utf-8")
    ok, failures, _ = verify_plan(
        wd, ["menu.md"],
        ["menu.md does not contain milk",
         "menu.md does not contain whey",
         "menu.md does not contain casein",
         "menu.md does not contain egg",
         "menu.md does not contain albumin",
         "menu.md does not contain wheat",
         "menu.md contains Label checks"])
    assert ok is True
    assert failures == []


def test_allergy_menu_fails_on_hidden_allergen(tmp_path):
    wd = tmp_path / "workdir"
    wd.mkdir()
    (wd / "menu.md").write_text(
        "Oatcakes, whey protein, carrots\n", encoding="utf-8")
    ok, failures, _ = verify_plan(
        wd, ["menu.md"], ["menu.md does not contain whey"])
    assert ok is False
    assert any("menu.md" in f and "contains whey" in f for f in failures)
