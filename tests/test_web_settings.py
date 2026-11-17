"""Configuration editing uses synthetic local files, without a live host/API."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def web_module(monkeypatch):
    name = "_ba_stats_web_settings_contract"
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, name, package)
    module = importlib.import_module(name + ".web_settings")
    yield module
    for key in tuple(sys.modules):
        if key.startswith(name + "."):
            sys.modules.pop(key, None)


@pytest.fixture
def editor(web_module, tmp_path):
    class Config(dict):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        config_path = tmp_path / "config.json"
        fail = False

        def save_config(self, replace_config=None):
            if replace_config:
                self.update(replace_config)
            if self.fail:
                raise OSError("PRIVATE/path?token=SECRET")
            self.config_path.write_text(json.dumps(self, allow_nan=False), encoding="utf-8-sig")

    from diagnostics import DiagnosticLog
    config = Config({"access": {"api_token": "PRIVATE_TOKEN", "client_id": "PRIVATE_CLIENT"},
                     "legacy_token": "PRIVATE_ROOT"})
    config.save_config()
    log = DiagnosticLog(tmp_path / "logs")
    result = web_module.WebSettings(config, web_module.Settings.from_config(config), log)
    yield result
    log.close()


def save(editor, changes, revision=None):
    return editor.save({"revision": revision or editor.snapshot()["revision"], "changes": changes})


def test_snapshot_is_schema_based_and_never_discloses_legacy_credentials(editor):
    snapshot = editor.snapshot()
    public = json.dumps(snapshot)
    assert not any(text in public for text in ("PRIVATE_", "api_token", "client_id", "legacy_token"))
    fields = {field["path"]: field for field in snapshot["fields"]}
    assert fields["query.default_limit"]["value"] == 5
    assert fields["output.theme"]["options"] == ["dark", "light"]
    assert fields["output.image_send_mode"]["value"] == "base64"
    assert fields["output.image_send_mode"]["default"] == "base64"
    assert fields["output.image_send_mode"]["options"] == ["base64", "file", "url"]
    assert fields["output.image_send_mode"]["labels"] == [
        "Base64（默认）", "本地文件（NapCat 直读）", "图片链接（NapCat 下载）"]
    assert "diagnostics.show_options" not in fields
    assert not snapshot["reload_required"] and not snapshot["diagnostics"]["enabled"]


@pytest.mark.parametrize("image_send_mode", ["file", "url"])
def test_partial_save_retains_unmodified_values_and_applies_only_diagnostics(editor, image_send_mode):
    original_settings = editor._running.copy()
    result = save(editor, {"diagnostics.enabled": True, "query.default_limit": 6,
                           "output.image_send_mode": image_send_mode})
    assert editor.diagnostics.enabled and result["diagnostics_applied"]
    assert editor._running == original_settings
    assert editor._running["output.image_send_mode"] == "base64"
    assert result["pending_restart"] == ["query.default_limit", "output.image_send_mode"]
    assert "手动重载" in result["message"]
    assert editor.config["access"]["api_token"] == "PRIVATE_TOKEN"
    assert editor.config["legacy_token"] == "PRIVATE_ROOT"
    stored = json.loads(editor.config.config_path.read_text(encoding="utf-8-sig"))
    assert stored["query"]["default_limit"] == 6
    assert stored["diagnostics"]["enabled"] is True
    assert stored["output"]["image_send_mode"] == image_send_mode
    result = save(editor, {"query.default_limit": 5, "diagnostics.enabled": False,
                           "output.image_send_mode": "base64"})
    assert not result["reload_required"] and not editor.diagnostics.enabled


@pytest.mark.parametrize("changes", [
    {}, {"access.api_token": "new"}, {"access.client_id": "new"}, {"api_token": "new"},
    {"limits.global_daily_limit": 1000}, {"diagnostics.show_options": True},
    {"query.default_limit": True}, {"query.default_limit": 2.5}, {"query.default_limit": "5"},
    {"query.default_limit": 21}, {"query.cooldown_seconds": float("nan")},
    {"query.cooldown_seconds": float("inf")}, {"query.cooldown_seconds": 10 ** 1000},
    {"access.enabled": "true"}, {"diagnostics.enabled": 1}, {"output.theme": "auto"},
    {"output.image_send_mode": "auto"}, {"output.image_send_mode": None},
    {"output.timezone": "Not/AZone"}, {"maintenance.concurrency": 6},
    {"access.base_url": "http://example.invalid"},
    {"access.base_url": "https://user:secret@example.invalid"},
    {"access.base_url": "https://example.invalid/api?token=secret"},
])
def test_strict_changes_reject_unknown_types_ranges_and_urls_without_mutation(editor, web_module, changes):
    before = editor.config.config_path.read_bytes()
    with pytest.raises(web_module.SettingsError) as error:
        save(editor, changes)
    assert error.value.status_code == 400
    assert editor.config.config_path.read_bytes() == before
    assert not editor.diagnostics.enabled


def test_full_cross_field_validation_and_combined_valid_changes(editor, web_module):
    with pytest.raises(web_module.SettingsError):
        save(editor, {"query.max_limit": 3})  # default_limit remains five
    result = save(editor, {"query.default_limit": 3, "query.max_limit": 3})
    assert set(result["pending_restart"]) == {"query.default_limit", "query.max_limit"}


def test_revision_detects_changes_from_native_dashboard_and_retains_external_fields(editor, web_module):
    revision = editor.snapshot()["revision"]
    external = dict(editor.config)
    external.pop("legacy_token")
    external["output"] = {"theme": "light"}
    editor.config.config_path.write_text(json.dumps(external), encoding="utf-8")
    assert editor.snapshot()["revision"] != revision
    with pytest.raises(web_module.SettingsError) as error:
        save(editor, {"diagnostics.enabled": True}, revision)
    assert error.value.status_code == 409
    assert not editor.diagnostics.enabled
    result = save(editor, {"diagnostics.enabled": True})
    assert editor.config["output"]["theme"] == "light"
    assert "legacy_token" not in editor.config
    assert result["pending_restart"] == ["output.theme"]


def test_external_diagnostic_save_reports_actual_state_and_can_be_applied(editor):
    values = dict(editor.config)
    values["diagnostics"] = {"enabled": True}
    editor.config.config_path.write_text(json.dumps(values), encoding="utf-8")
    snapshot = editor.snapshot()
    assert not snapshot["diagnostics_applied"] and not snapshot["diagnostics"]["enabled"]
    result = save(editor, {"diagnostics.enabled": True})
    assert result["diagnostics_applied"] and result["diagnostics"]["enabled"]


def test_two_concurrent_saves_with_one_revision_cannot_overwrite_each_other(editor, web_module):
    revision = editor.snapshot()["revision"]

    def attempt(value):
        try:
            save(editor, {"query.default_limit": value}, revision)
            return 200
        except web_module.SettingsError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, [6, 7])) == [200, 409]


def test_failed_atomic_save_restores_in_memory_config_and_leaves_runtime_unchanged(editor, web_module):
    before = json.loads(json.dumps(editor.config))
    disk = editor.config.config_path.read_bytes()
    editor.config.fail = True
    with pytest.raises(web_module.SettingsError) as error:
        save(editor, {"diagnostics.enabled": True})
    assert error.value.status_code == 500 and "PRIVATE" not in str(error.value)
    assert dict(editor.config) == before
    assert editor.config.config_path.read_bytes() == disk
    assert not editor.diagnostics.enabled


def test_invalid_persisted_values_and_credential_url_are_not_returned(editor):
    editor.config["access"]["base_url"] = "https://user:PRIVATE_URL@example.invalid/?token=PRIVATE_QUERY"
    editor.config["query"] = {"default_limit": {"token": "PRIVATE_NESTED"}}
    editor.config.save_config()
    snapshot = editor.snapshot()
    assert "PRIVATE" not in json.dumps(snapshot)
    assert set(snapshot["invalid_fields"]) == {"access.base_url", "query.default_limit"}
    fields = {field["path"]: field["value"] for field in snapshot["fields"]}
    assert fields["access.base_url"] == "" and fields["query.default_limit"] == 5
    save(editor, {"access.base_url": "https://example.invalid", "query.default_limit": 5})
    assert editor.snapshot()["invalid_fields"] == []


@pytest.mark.parametrize("content", ["[]", "null", "{bad json", '{"query": NaN}'])
def test_unreadable_configuration_is_safe_and_not_overwritten(editor, web_module, content):
    editor.config.config_path.write_text(content, encoding="utf-8")
    with pytest.raises(web_module.SettingsError) as error:
        editor.snapshot()
    assert error.value.status_code == 503
    assert editor.config.config_path.read_text(encoding="utf-8") == content
