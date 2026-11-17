"""Small, schema-based editor for the host's existing AstrBotConfig.

v4.29.4: save_config() writes an atomic snapshot; PluginManager.reload()
terminates the old instance first, so ordinary settings await a manual reload.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import threading
from .settings import Settings, valid_https_origin


# Explicit public surface: legacy credentials/client IDs never become fields,
# even when they remain in an old configuration or schema.
_ATTRS = {
    "access.enabled": "enabled", "access.base_url": "base_url",
    "query.default_limit": "default_limit", "query.max_limit": "max_limit",
    "query.cooldown_seconds": "cooldown_seconds", "query.chinese_aliases": "chinese_aliases",
    "output.image_enabled": "image_enabled", "output.theme": "theme", "output.timezone": "timezone",
    "limits.user_daily_limit": "user_daily_limit", "limits.group_daily_limit": "group_daily_limit",
    **{f"maintenance.{key}": key for key in (
        "request_interval", "concurrency", "queue_size", "request_timeout", "wait_timeout",
        "retries", "cache_max_entries", "stale_hours",
    )},
    "diagnostics.enabled": "diagnostics_enabled",
}
_BOUNDS = {
    "query.default_limit": (1, 20), "query.max_limit": (1, 20),
    "query.cooldown_seconds": (0, 60), "limits.user_daily_limit": (0, 100),
    "limits.group_daily_limit": (0, 100), "maintenance.request_interval": (.1, 60),
    "maintenance.concurrency": (1, 5), "maintenance.queue_size": (1, 100),
    "maintenance.request_timeout": (1, 120), "maintenance.wait_timeout": (1, 180),
    "maintenance.retries": (0, 2), "maintenance.cache_max_entries": (10, 10000),
    "maintenance.stale_hours": (0, 168),
}


class SettingsError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class WebSettings:
    def __init__(self, config, runtime_settings, diagnostics):
        self.config, self.diagnostics = config, diagnostics
        schema = getattr(config, "schema", None)
        self.schema = schema if isinstance(schema, dict) else json.loads(
            Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8"))
        self._running = {path: getattr(runtime_settings, attr) for path, attr in _ATTRS.items()}
        self._lock = threading.RLock()
        self._specs = {}
        for path in _ATTRS:
            group, key = path.split(".")
            spec = self.schema.get(group, {}).get("items", {}).get(key)
            if isinstance(spec, dict):
                self._specs[path] = spec

    def _read(self):
        # Dashboard saves can replace the on-disk config without updating this
        # plugin instance. Read that same file for both GET and revision checks.
        config_path = getattr(self.config, "config_path", None)
        if not config_path:
            raise SettingsError("配置文件暂不可用，请重载插件后重试。", 503)
        try:
            values = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
            if not isinstance(values, dict):
                raise ValueError
            revision = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=True,
                                                 allow_nan=False).encode()).hexdigest()
        except (OSError, ValueError, TypeError):
            raise SettingsError("无法读取配置文件，请先在 AstrBot 中检查配置。", 503) from None
        return values, revision

    @staticmethod
    def _value(values, path, spec):
        group, key = path.split(".")
        node = values.get(group, {})
        if not isinstance(node, dict):
            raise SettingsError("配置分组格式异常，请先在 AstrBot 中检查配置。")
        return node.get(key, deepcopy(spec["default"]))

    def _validate(self, path, value):
        spec = self._specs[path]
        kind, label = spec["type"], spec.get("description", path)
        valid = ((kind == "bool" and type(value) is bool)
                 or (kind == "int" and type(value) is int)
                 or (kind == "float" and (type(value) is int or type(value) is float and math.isfinite(value)))
                 or (kind == "string" and isinstance(value, str) and len(value) <= 2048))
        if not valid:
            raise SettingsError(f"「{label}」的类型或格式不正确。")
        if spec.get("options") and value not in spec["options"]:
            raise SettingsError(f"请为「{label}」选择有效选项。")
        if path in _BOUNDS and not _BOUNDS[path][0] <= value <= _BOUNDS[path][1]:
            lower, upper = _BOUNDS[path]
            raise SettingsError(f"「{label}」必须在 {lower}–{upper} 之间。")
        if path == "access.base_url" and not valid_https_origin(value):
            raise SettingsError("API 地址必须是不含路径、参数和账号信息的 HTTPS 地址。")

    def _describe(self, values, revision):
        fields, pending, invalid = [], [], []
        for path, spec in self._specs.items():
            value = self._value(values, path, spec)
            # Invalid legacy values may include credential-bearing URLs or
            # arbitrary objects. Never expose those through public fields.
            try:
                self._validate(path, value)
            except SettingsError:
                value = "" if path == "access.base_url" else deepcopy(spec["default"])
                invalid.append(path)
            group = path.split(".")[0]
            fields.append({"path": path, "group": group, "label": spec.get("description", path),
                           "type": spec["type"], "value": value, "default": spec["default"],
                           "hint": spec.get("hint", ""), "options": spec.get("options", []),
                           "labels": spec.get("labels", []), "bounds": _BOUNDS.get(path)})
            if path != "diagnostics.enabled" and value != self._running[path]:
                pending.append(path)
        return {"revision": revision, "fields": fields,
                "groups": {key: {"label": spec.get("description", key), "hint": spec.get("hint", "")}
                           for key, spec in self.schema.items() if key in {p.split(".")[0] for p in self._specs}},
                "reload_required": bool(pending), "pending_restart": pending,
                "invalid_fields": invalid, "diagnostics": self.diagnostics.stats(),
                "diagnostics_applied": self.diagnostics.enabled == next(
                    (f["value"] for f in fields if f["path"] == "diagnostics.enabled"), False)}

    def snapshot(self):
        with self._lock:
            return self._describe(*self._read())

    def save(self, payload):
        with self._lock:
            if not isinstance(payload, dict) or set(payload) != {"revision", "changes"}:
                raise SettingsError("请求必须仅包含 revision 和 changes。")
            if (not isinstance(payload["revision"], str) or len(payload["revision"]) != 64
                    or any(char not in "0123456789abcdef" for char in payload["revision"])):
                raise SettingsError("revision 格式无效，请重新读取设置。")
            changes = payload["changes"]
            if not isinstance(changes, dict) or not changes or any(path not in self._specs for path in changes):
                raise SettingsError("没有有效更改，或包含未知配置项；旧令牌及客户端标识不可编辑。")
            current, revision = self._read()
            if payload["revision"] != revision:
                raise SettingsError("配置已在其他页面更新。请重新读取后核对并再次保存，当前草稿仍保留。", 409)
            updated = deepcopy(current)
            for path, value in changes.items():
                self._validate(path, value)
                group, key = path.split(".")
                if not isinstance(updated.get(group, {}), dict):
                    raise SettingsError("配置分组格式异常，请先在 AstrBot 中检查配置。")
                updated.setdefault(group, {})[key] = value
            # from_config casts some values permissively: enforce all schema
            # types first, then its complete cross-field/range/timezone checks.
            for path, spec in self._specs.items():
                self._validate(path, self._value(updated, path, spec))
            try:
                settings = Settings.from_config(updated)
            except (ValueError, TypeError, OverflowError):
                raise SettingsError("配置校验未通过：请检查场数范围、时区及请求限制。") from None
            before = deepcopy(dict(self.config))
            try:
                # v4.29.4 save_config(replace_config) merges then atomically
                # replaces its file. No await between revision check and save.
                self.config.clear()
                self.config.save_config(updated)
            except Exception:
                self.config.clear()
                self.config.update(before)
                raise SettingsError("配置保存失败，当前运行设置未变；请检查配置目录后重试。", 500) from None
            self.diagnostics.set_enabled(settings.diagnostics_enabled)
            snapshot = self._describe(*self._read())
            snapshot["message"] = ("已保存；诊断开关已即时应用，其他更改需在 AstrBot 插件页手动重载后生效。"
                                   if snapshot["reload_required"] else "已保存并应用诊断开关，无需重载。")
            return snapshot
