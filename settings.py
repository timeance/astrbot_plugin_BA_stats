"""单一配置来源：AstrBotConfig；启动时校验并快照。"""
from dataclasses import dataclass
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def valid_https_origin(value):
    """Shared API origin contract for runtime and both settings interfaces."""
    if not isinstance(value, str) or not value:
        return False
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        return False
    try:
        url = urlsplit(value)
        return bool(url.scheme == "https" and url.hostname
                    and url.username is None and url.password is None
                    and url.path in {"", "/"} and not url.query and not url.fragment
                    and (url.port is None or 1 <= url.port <= 65535))
    except ValueError:
        return False


def bounded(data, key, default, lower, upper, cast=float):
    try:
        value = cast(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"配置 {key} 必须为数值。") from None
    if not lower <= value <= upper:
        raise ValueError(f"配置 {key} 必须在 {lower}–{upper} 之间。")
    return value


def daily_limit(data, key, default):
    value = data.get(key, default)
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError(f"配置 {key} 必须是 0–100 的整数；0 表示不单独限制。")
    return value


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    base_url: str = "https://bot.batrace.top"
    default_limit: int = 5
    max_limit: int = 20
    cooldown_seconds: float = 3
    chinese_aliases: bool = True
    image_enabled: bool = True
    theme: str = "dark"
    timezone: str = "Asia/Shanghai"
    request_interval: float = 1
    concurrency: int = 2
    queue_size: int = 20
    request_timeout: float = 20
    wait_timeout: float = 45
    retries: int = 1
    cache_max_entries: int = 1000
    stale_hours: float = 24
    user_daily_limit: int = 20
    group_daily_limit: int = 100
    diagnostics_enabled: bool = False

    @classmethod
    def from_config(cls, config):
        access = config.get("access", {})
        query = config.get("query", {})
        output = config.get("output", {})
        maintenance = config.get("maintenance", {})
        limits = config.get("limits", {})
        diagnostics = config.get("diagnostics", {})
        maximum = bounded(query, "max_limit", 20, 1, 20, int)
        timezone = str(output.get("timezone", "Asia/Shanghai"))
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("时区无效，请填写 Asia/Shanghai 等 IANA 时区名。") from None
        theme = output.get("theme", "dark")
        if theme not in {"dark", "light"}:
            raise ValueError("主题必须为 dark 或 light。")
        return cls(
            enabled=bool(access.get("enabled", False)),
            base_url=str(access.get("base_url", cls.base_url)).strip(),
            default_limit=bounded(query, "default_limit", 5, 1, maximum, int),
            max_limit=maximum,
            cooldown_seconds=bounded(query, "cooldown_seconds", 3, 0, 60),
            chinese_aliases=bool(query.get("chinese_aliases", True)),
            image_enabled=bool(output.get("image_enabled", True)),
            theme=theme, timezone=timezone,
            request_interval=bounded(maintenance, "request_interval", 1, 0.1, 60),
            concurrency=bounded(maintenance, "concurrency", 2, 1, 5, int),
            queue_size=bounded(maintenance, "queue_size", 20, 1, 100, int),
            request_timeout=bounded(maintenance, "request_timeout", 20, 1, 120),
            wait_timeout=bounded(maintenance, "wait_timeout", 45, 1, 180),
            retries=bounded(maintenance, "retries", 1, 0, 2, int),
            cache_max_entries=bounded(maintenance, "cache_max_entries", 1000, 10, 10000, int),
            stale_hours=bounded(maintenance, "stale_hours", 24, 0, 168),
            user_daily_limit=daily_limit(limits, "user_daily_limit", 20),
            group_daily_limit=daily_limit(limits, "group_daily_limit", 100),
            diagnostics_enabled=bool(diagnostics.get("enabled", False)),
        )
