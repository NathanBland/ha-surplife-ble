from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "surplife_ble"


def test_manifest_is_local_polling_with_bluetooth_discovery() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "surplife_ble"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_polling"
    assert "bluetooth" in manifest
    assert isinstance(manifest["bluetooth"], list)
    assert len(manifest["bluetooth"]) >= 1
    assert "version" in manifest
    assert "codeowners" in manifest
    assert "issue_tracker" in manifest
    assert "bleak-retry-connector" in " ".join(manifest["requirements"])


def test_home_assistant_modules_parse_without_importing_home_assistant() -> None:
    for path in INTEGRATION.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_translations_match_strings() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text(encoding="utf-8"))
    assert english == strings


def test_services_yaml_matches_service_strings() -> None:
    services_yaml = (INTEGRATION / "services.yaml").read_text(encoding="utf-8")
    strings = json.loads((INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    services = strings["services"]
    for name, service in services.items():
        assert f"\n{name}:" in f"\n{services_yaml}"
        for field in service["fields"]:
            assert f"    {field}:" in services_yaml
