"""Parse arguments after the framework's registered ``ba`` command.

Adapters replace actual At components with ``mention_token(index)`` and pass
their IDs in ``mentions`` in component order. NUL is reserved for these tokens:
adapters must reject NUL in Plain input. Typed ``@user:`` is always literal text.
Names which look like reserved words or numeric IDs can use ``name:``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex


@dataclass(frozen=True)
class Target:
    kind: str  # me | mention | stb | steam | name
    value: str


@dataclass(frozen=True)
class Request:
    action: str
    target: Target | None = None
    limit: int = 5
    code: str = ""  # match ID for match; request code for select/cancel
    index: int = 0  # one-based candidate index


_ALIASES = {
    "帮助": "help", "我": "me", "自己": "me", "战绩": "stats",
    "近期": "recent", "最近": "recent", "上一场": "last",
    "单局": "match", "搜索": "search", "绑定": "bind",
    "当前绑定": "binding", "解绑": "unbind", "选择": "select",
    "取消": "cancel", "状态": "status", "缓存": "cache", "清理": "clear", "额度": "quota",
}
_NUMBER = re.compile(r"[0-9]+\Z")


def mention_token(index: int) -> str:
    """Encode a trusted component position, not a user-typed account marker."""
    return f"\x00at:{index}\x00"


def _target(parts: list[str], mentions: dict[str, str], aliases: bool) -> Target:
    if not parts:
        raise ValueError("请指定玩家：me、@群友、stb:玩家ID、steam:SteamID64 或 name:昵称。")
    value = " ".join(parts)
    if not value.strip():
        raise ValueError("玩家名称或 ID 不能为空。")
    if "\x00" in value:
        if len(parts) != 1 or value not in mentions:
            raise ValueError("请单独使用聊天软件的 @ 提及功能选择群友；昵称请使用文字输入。")
        return Target("mention", mentions[value])
    prefix, separator, suffix = value.partition(":")
    if separator and prefix.lower() in {"name", "stb", "steam"}:
        kind = prefix.lower()
        if not suffix.strip():
            raise ValueError("玩家名称或 ID 不能为空。")
        if kind != "name" and any(char.isspace() for char in suffix):
            raise ValueError("玩家 ID 不能包含空格。")
        if kind == "steam" and not _NUMBER.fullmatch(suffix):
            raise ValueError("steam: 后请填写纯数字的 SteamID64。")
        return Target(kind, suffix)
    if value.lower() == "me" or (aliases and value in {"我", "自己"}):
        return Target("me", "")
    if any(part.startswith("@user:") for part in parts):
        raise ValueError("请通过聊天软件的 @ 提及功能选择群友，不要手动输入 @user: 文本。")
    if _NUMBER.fullmatch(value):
        raise ValueError("纯数字可能有歧义，请补充前缀：stb:（STB ID）、steam:（SteamID64）或 name:（纯数字昵称）。")
    if re.search(r"https?://|www\.|steamcommunity\.com/", value, re.IGNORECASE):
        raise ValueError("暂不支持直接输入主页链接，请改用 stb:、steam: 或 name:昵称。")
    if value.startswith("@"):
        raise ValueError("请通过聊天软件的 @ 提及功能选择群友；如果玩家昵称本身以 @ 开头，请使用 name:昵称。")
    return Target("name", value)


def _count(value: str, maximum: int) -> int:
    try:
        count = int(value) if _NUMBER.fullmatch(value) else 0
    except ValueError:
        count = 0
    if not 1 <= count <= maximum:
        raise ValueError(f"对局数量须为 1 到 {maximum} 的整数。")
    return count


def parse_command(
    text: str,
    mentions: list[str] | None = None,
    default_limit: int = 5,
    max_limit: int = 20,
    chinese_aliases: bool = True,
) -> Request:
    """Return one canonical request; all syntax errors are Chinese ValueErrors.

    Actions: help/stats/recent/last/match/search/bind/binding/unbind/select/
    cancel/status/quota/cache_clear. Match IDs are in ``code``; search queries in
    ``target=Target('name', query)``. Empty arguments mean help.
    """
    if (type(default_limit) is not int or type(max_limit) is not int
            or not 1 <= default_limit <= max_limit):
        raise ValueError("默认数量和最大数量须为有效正整数，且默认数量不能超过上限。")
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise ValueError("引号未配对，包含空格的昵称请用成对的英文双引号（\"\"）括起来。") from exc
    if not parts:
        return Request("help", limit=default_limit)

    def keyword(word: str) -> str:
        return _ALIASES.get(word, word.lower()) if chinese_aliases else word.lower()

    action = keyword(parts.pop(0))
    if action not in {"stats", "bind", "recent", "last"} and any("\x00" in part for part in parts):
        raise ValueError("此命令请使用文字参数；@ 提及仅用于指定查询或绑定的群友。")
    compact = re.fullmatch(r"recent([0-9]+)", action)
    if compact:
        parts.insert(0, compact[1])
        action = "recent"
    if action == "me":
        if not parts:
            return Request("stats", Target("me", ""), default_limit)
        subcommand = keyword(parts.pop(0))
        compact = re.fullmatch(r"recent([0-9]+)", subcommand)
        if compact:
            parts.insert(0, compact[1])
            subcommand = "recent"
        if subcommand == "last" and not parts:
            return Request("last", Target("me", ""), default_limit)
        if subcommand != "recent" or len(parts) > 1:
            raise ValueError("用法：me、me recent [数量] 或 me last。")
        limit = _count(parts[0], max_limit) if parts else default_limit
        return Request("recent", Target("me", ""), limit)
    if action in {"help", "binding", "unbind", "status", "quota"}:
        if parts:
            raise ValueError(f"""{action} 命令不需要额外参数，请直接发送。""")
        return Request(action, limit=default_limit)
    if action == "cache":
        if len(parts) != 1 or keyword(parts[0]) != "clear":
            raise ValueError("用法：cache clear。")
        return Request("cache_clear", limit=default_limit)
    if action in {"match", "cancel", "select"}:
        expected = 2 if action == "select" else 1
        if len(parts) != expected or not parts[0] or any(c.isspace() for c in parts[0]):
            raise ValueError({"match": "用法：match <对局ID>。", "cancel": "用法：cancel <请求码>。",
                              "select": "用法：select <请求码> <序号>。"}[action])
        index = 0
        if action == "select":
            try:
                index = int(parts[1]) if _NUMBER.fullmatch(parts[1]) else 0
            except ValueError:
                index = 0
            if index < 1:
                raise ValueError("候选序号须为从 1 开始的整数。")
        return Request(action, limit=default_limit, code=parts[0], index=index)
    if action == "search":
        if not parts or not " ".join(parts).strip():
            raise ValueError("用法：search <昵称>。")
        return Request("search", Target("name", " ".join(parts)), default_limit)
    trusted_mentions = {mention_token(index): user for index, user in enumerate(mentions or ())}
    if action == "recent":
        limit = default_limit
        if "--limit" in parts:
            position = parts.index("--limit")
            if position == 0 or position != len(parts) - 2:
                raise ValueError("用法：recent <玩家> --limit <数量>。")
            limit = _count(parts[-1], max_limit)
            parts = parts[:position]
        elif len(parts) == 1 and _NUMBER.fullmatch(parts[0]):
            return Request("recent", Target("me", ""), _count(parts[0], max_limit))
        if any(part.startswith("--") for part in parts):
            raise ValueError("近期查询仅支持末尾的 --limit <数量> 参数。")
        target = _target(parts, trusted_mentions, chinese_aliases) if parts else Target("me", "")
        return Request("recent", target, limit)
    if action in {"stats", "bind", "last"}:
        if action == "last" and not parts:
            return Request("last", Target("me", ""), default_limit)
        return Request(action, _target(parts, trusted_mentions, chinese_aliases), default_limit)
    raise ValueError("未知命令，请使用 ba help 查看帮助。")
