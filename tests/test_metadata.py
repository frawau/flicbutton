"""Metadata tests for the flicbutton integration."""

import json
from pathlib import Path

from custom_components.flicbutton.const import legacy_button_name

ROOT = Path(__file__).parent.parent
COMPONENT = ROOT / "custom_components" / "flicbutton"


def test_manifest_required_keys() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == "flicbutton"
    assert manifest["name"] == "Flic button (flicd)"
    assert manifest["version"] == "0.1.0"
    assert manifest["documentation"] == "https://github.com/frawau/flicbutton"
    assert manifest["issue_tracker"] == "https://github.com/frawau/flicbutton/issues"
    assert manifest["codeowners"] == ["@frawau"]
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_push"
    assert manifest["integration_type"] == "hub"
    assert manifest["requirements"] == []
    assert manifest["dependencies"] == []


def test_hacs_json() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs == {"name": "Flic button (flicd)", "homeassistant": "2026.11.0"}


def test_legacy_button_name() -> None:
    assert legacy_button_name("80:E4:DA:71:2B:3C") == "flic_80e4da712b3c"
