"""Packaging declarations: optional office deps and the bundled
security rules must actually ship, or installed users silently lose
features (CSV instead of XLSX, semgrep auto config instead of the
bundled rules)."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pyproject():
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def test_office_optional_group_declares_builders():
    data = _pyproject()
    office = data["project"]["optional-dependencies"]["office"]
    assert "openpyxl" in office
    assert "python-pptx" in office
    assert "pytesseract" in office


def test_security_rules_shipped_as_package_data():
    data = _pyproject()
    package_data = data["tool"]["setuptools"]["package-data"]
    assert "security_rules/*.yml" in package_data.get("src.capabilities", [])
    assert (ROOT / "src" / "capabilities" / "security_rules" / "semgrep.yml").exists()
