"""Command contract tests; no framework, network, or real account is used."""

import pytest

from commands import Request, Target, mention_token, parse_command


@pytest.mark.parametrize(("text", "expected"), [
    ("", Request("help")),
    ("help", Request("help")),
    ("me", Request("stats", Target("me", ""))),
    ("stats me", Request("stats", Target("me", ""))),
    ("stats stb:0012345678901234567890", Request("stats", Target("stb", "0012345678901234567890"))),
    ("stats steam:76561198012345678", Request("stats", Target("steam", "76561198012345678"))),
    ("recent", Request("recent", Target("me", ""))),
    ("recent 10", Request("recent", Target("me", ""), 10)),
    ("recent10", Request("recent", Target("me", ""), 10)),
    ("me recent 10", Request("recent", Target("me", ""), 10)),
    ("me recent10", Request("recent", Target("me", ""), 10)),
    ("recent steam:76561198012345678 --limit 10",
     Request("recent", Target("steam", "76561198012345678"), 10)),
    ("last", Request("last", Target("me", ""))),
    ("me last", Request("last", Target("me", ""))),
    ("last stb:001", Request("last", Target("stb", "001"))),
    ("match 00098765432109876543210", Request("match", code="00098765432109876543210")),
    ("search Player 2", Request("search", Target("name", "Player 2"))),
    ("search 12345", Request("search", Target("name", "12345"))),
    ("search me", Request("search", Target("name", "me"))),
    ("bind name:Player", Request("bind", Target("name", "Player"))),
    ("binding", Request("binding")),
    ("unbind", Request("unbind")),
    ("select AbC123 2", Request("select", code="AbC123", index=2)),
    ("cancel AbC123", Request("cancel", code="AbC123")),
    ("status", Request("status")),
    ("quota", Request("quota")),
    ("cache clear", Request("cache_clear")),
])
def test_english_command_table(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize("action", ["stats", "bind", "recent", "last"])
@pytest.mark.parametrize(("argument", "name"), [
    ('"Player 2"', "Player 2"),
    ('name:"Player 2"', "Player 2"),
    ("name:Player 2", "Player 2"),
    ("name:me", "me"),
    ("name:recent", "recent"),
    ("name:12345", "12345"),
    ('name:" Player  2 "', " Player  2 "),
    ('name:"欧阳 O\'Brien [x]&<y>"', "欧阳 O'Brien [x]&<y>"),
    ("name:@user:123", "@user:123"),
    ('name:"Player --limit 2"', "Player --limit 2"),
])
def test_names_preserve_content(action, argument, name):
    request = parse_command(f"{action} {argument}")
    assert request.target == Target("name", name)


def test_trailing_number_belongs_to_target_unless_explicit_limit():
    assert parse_command("recent Player 2").target == Target("name", "Player 2")
    request = parse_command('recent name:"Player 2" --limit 10')
    assert request == Request("recent", Target("name", "Player 2"), 10)
    assert parse_command("recent name:10").target == Target("name", "10")


def test_case_insensitive_keywords_do_not_change_names_or_ids():
    assert parse_command("STATS NAME:MiXeD Player").target == Target("name", "MiXeD Player")
    assert parse_command("ME ReCeNt 10") == parse_command("recent 10")
    assert parse_command("STATS STB:AbC-001").target == Target("stb", "AbC-001")


@pytest.mark.parametrize(("chinese", "english"), [
    ("帮助", "help"), ("我", "me"), ("战绩 我", "stats me"),
    ("最近 10", "recent 10"), ("自己 近期 10", "me recent 10"),
    ("上一场", "last"), ("我 上一场", "me last"),
    ("单局 123", "match 123"), ("搜索 me", "search me"),
    ("绑定 name:me", "bind name:me"), ("当前绑定", "binding"),
    ("解绑", "unbind"), ("选择 ABC 1", "select ABC 1"),
    ("取消 ABC", "cancel ABC"), ("状态", "status"), ("缓存 清理", "cache clear"),
    ("额度", "quota"),
])
def test_optional_chinese_aliases_share_requests(chinese, english):
    assert parse_command(chinese) == parse_command(english)
    with pytest.raises(ValueError, match="未知命令"):
        parse_command(chinese, chinese_aliases=False)


def test_alias_switch_does_not_translate_explicit_names():
    assert parse_command("stats name:我").target == Target("name", "我")
    assert parse_command("stats 我", chinese_aliases=False).target == Target("name", "我")


def test_structured_mentions_require_trusted_component_position():
    assert parse_command(f"stats {mention_token(0)}", mentions=["000123"]).target == Target("mention", "000123")
    for text, mentions in [
        ("stats @user:123", []), ("stats @user:123", ["123"]),
        (f"stats @user:123 {mention_token(0)}", ["123"]),
        (f"stats {mention_token(1)}", ["123"]),
        (f"stats {mention_token(0)}", []),
        ("stats @user:123 @user:321", ["123", "321"]),
        ("stats @群名片", []), ("stats", ["123"]),
    ]:
        with pytest.raises(ValueError):
            parse_command(text, mentions=mentions)


@pytest.mark.parametrize("text", ["stats name:@user:123", "search @user:123"])
def test_literal_marker_names_never_borrow_trusted_account_ids(text):
    assert parse_command(text, mentions=["123"]).target == Target("name", "@user:123")


@pytest.mark.parametrize("text", ["search {}", "stats name:{}", "stats name:Player {}"])
def test_real_mentions_are_not_converted_into_nickname_queries(text):
    with pytest.raises(ValueError, match="提及"):
        parse_command(text.format(mention_token(0)), mentions=["123"])


@pytest.mark.parametrize("text", [
    "stats 12345", "bind 12345", "last 12345", "recent 123 --limit 5",
])
def test_bare_numeric_player_is_ambiguous(text):
    with pytest.raises(ValueError, match="stb:.*steam:.*name:"):
        parse_command(text)


@pytest.mark.parametrize("text", [
    "stats", "bind", 'stats ""', 'stats "   "', "stats name:", 'stats name:"   "', "stats stb:",
    "stats steam:abc", 'stats stb:"12 34"', 'stats "Player',
    "recent 0", "recent 21", "recent10 --limit 5", "me recent 0",
    "me recent player", "me recent 5 6", "me stats", "me last player",
    "recent Player --limit 0", "recent Player --limit", "recent --limit 5",
    "recent Player --limit 5 ignored", "recent Player --limit 5 --limit 10",
    "recent Player --unknown 10", "match", "match a b", "match 'a b'",
    "select code 0", "select code -1", "select code 1.5", "select code",
    "cancel", "cancel code extra", "cache", "cache clear extra", "cache delete",
    "search", 'search ""', "help extra", "binding extra", "unbind extra", "status extra", "quota extra",
    "stats https://steamcommunity.com/id/player", "stats steamcommunity.com/id/player",
    "unknown", "/ba help",
])
def test_invalid_syntax_has_chinese_feedback(text):
    with pytest.raises(ValueError) as error:
        parse_command(text)
    assert any("\u4e00" <= char <= "\u9fff" for char in str(error.value))


def test_configured_limits_apply_to_defaults_and_both_self_spellings():
    assert parse_command("recent", default_limit=3, max_limit=10).limit == 3
    assert parse_command("recent 10", max_limit=10) == parse_command("me recent 10", max_limit=10)
    with pytest.raises(ValueError):
        parse_command("recent 11", max_limit=10)
    for default, maximum in [(0, 20), (21, 20), (1, 0), (True, 20)]:
        with pytest.raises(ValueError):
            parse_command("help", default_limit=default, max_limit=maximum)


def test_excessively_long_integer_arguments_still_have_chinese_errors():
    for text in ["recent " + "9" * 5000, "select code " + "9" * 5000]:
        with pytest.raises(ValueError, match="整数"):
            parse_command(text)
