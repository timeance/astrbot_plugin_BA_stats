"""Daily upstream request quotas, independent of credentials and caches."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


GLOBAL_DAILY_LIMIT = 100
_BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class QuotaActor:
    platform: str
    user: str
    group: str = ""

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.platform, self.user)):
            raise ValueError("配额的平台实例和用户 ID 必须为非空字符串。")
        if not isinstance(self.group, str) or (self.group and not self.group.strip()):
            raise ValueError("群 ID 必须为非空字符串；私聊请使用空字符串。")


class QuotaExceededError(ValueError):
    code = "daily_quota"

    def __init__(self, scope: str, limit: int, used: int) -> None:
        self.scope = scope
        self.limit = limit
        self.used = used
        label = {"global": "全插件", "user": "本用户", "group": "本群"}[scope]
        super().__init__(f"""{label}今日请求额度已用完（已用 {used}/{limit} 次），将于北京时间 0 点重置。""")


class AdmissionDeferred(Exception):
    """The send gate changed before quota commit; retry without spending."""


def quota_day(timestamp: float) -> str:
    """Return the UTC+08:00 calendar date, independent of the host timezone."""
    return datetime.fromtimestamp(timestamp, _BEIJING).date().isoformat()
