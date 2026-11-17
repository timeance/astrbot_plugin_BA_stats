"""Local synthetic metadata only; no framework, browser or live HTTP needed."""

from concurrent.futures import ThreadPoolExecutor
import ast
from io import BytesIO
import json
from pathlib import Path
from threading import Barrier
from zipfile import ZipFile

import pytest

from diagnostics import DiagnosticLog, MAX_TOTAL_BYTES


def unpack(data):
    with ZipFile(BytesIO(data)) as archive:
        assert archive.testzip() is None
        contents = {name: archive.read(name) for name in archive.namelist()}
    records = []
    for name, content in contents.items():
        assert name.startswith("diagnostics-") and name.endswith(".jsonl")
        assert "/" not in name and "\\" not in name
        assert not content or content.endswith(b"\n")
        records.extend(json.loads(line) for line in content.splitlines())
    return contents, records


def test_default_disabled_switch_and_permanent_close(tmp_path):
    directory = tmp_path / "diagnostics"
    log = DiagnosticLog(directory)
    assert not log.enabled
    assert not log.record("command", command="stats")
    assert unpack(log.export_zip()) == ({}, [])
    assert not directory.exists()
    log.set_enabled(True)
    assert log.record("command", command="stats", count=1)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    log.set_enabled(False)
    assert not log.record("command", count=2)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert unpack(log.export_zip())[0] == before
    log.set_enabled(True)
    assert log.record("command", count=3)
    log.close()
    closed = unpack(log.export_zip())[1]
    log.set_enabled(True)
    assert not log.enabled and not log.record("command", count=4)
    assert unpack(log.export_zip())[1] == closed


def test_strict_allowlist_discards_secrets_even_inside_allowed_fields(tmp_path):
    log = DiagnosticLog(tmp_path, enabled=True)
    secret = "SYNTHETIC-secret-token-not-for-diagnostics"
    encoded = "ZGF0YTppbWFnZS9wbmc7YmFzZTY0LFNFQ1JFVA=="

    class MustNotStringify:
        def __str__(self):
            raise AssertionError("untrusted objects must never be stringified")

        __repr__ = __str__

    assert log.record("request", operation="player", outcome="error",
                      status_code=401, duration_ms=1.25, error_code="authentication",
                      token=secret, api_token=secret, authorization="Bearer " + secret,
                      base64=encoded, response_body={"token": secret},
                      message=secret, exception=MustNotStringify(),
                      headers={"Authorization": secret}, url="https://invalid/?token=" + secret)
    assert log.record("command", command=secret, phase=encoded, error_code=secret,
                      count=secret, duration_ms=secret, force=secret,
                      operation={"payload": encoded}, outcome=MustNotStringify(),
                      timestamp=secret)
    assert not log.record(secret, token=secret)
    assert not log.record(["request"], token=secret)
    contents, records = unpack(log.export_zip())
    raw = b"".join(contents.values()).decode()
    assert secret not in raw and encoded not in raw
    assert records[0]["error_code"] == "authentication"
    assert records[0]["status_code"] == 401
    assert records[0]["duration_ms"] == 1.25
    assert set(records[1]) == {"timestamp", "event"}


def test_main_client_event_contract_and_ui_stats(tmp_path):
    """Catch event-name drift at actual integration sites without loading AstrBot."""
    log = DiagnosticLog(tmp_path, True)
    assert log.stats() == {"enabled": True, "bytes": 0, "limit_bytes": MAX_TOTAL_BYTES, "files": 0}
    events = set()
    for filename in ("main.py", "client.py"):
        tree = ast.parse((Path(__file__).resolve().parents[1] / filename).read_text(encoding="utf-8-sig"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not call.args or not isinstance(call.func, ast.Attribute):
                continue
            function = call.func
            is_record = (function.attr == "record" and isinstance(function.value, ast.Attribute)
                         and function.value.attr == "diagnostics")
            if is_record or function.attr == "diagnostics":
                if isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                    events.add(call.args[0].value)
    assert {"command_start", "image_send_error", "http_response"} <= events
    for event in sorted(events):
        assert log.record(event), f"actual integration event was rejected: {event}"
    contents, records = unpack(log.export_zip())
    assert {record["event"] for record in records} == events
    stats = log.stats()
    assert stats["bytes"] == sum(map(len, contents.values()))
    assert stats["files"] == len(contents)
    log.close()
    assert log.stats() == {**stats, "enabled": False}


def test_actual_image_failure_fields_keep_safe_error_summary_and_basename(tmp_path):
    log = DiagnosticLog(tmp_path, enabled=True)
    assert log.record("image_send_error", error="ActionFailed", retcode=1200,
                      message="Timeout: sendMsg token=BARE_SYNTHETIC_SECRET "
                              "base64://ZGF0YQ== response-body-SECRET https://secret.invalid/?token=secret",
                      path=r"C:\Users\private-user\secret-dir\report.jpg",
                      image_type="JPEG", image_bytes=12345, duration_ms=10)
    assert log.record("http_response", route="/api/players/search?q=private-query&token=SECRET#secret",
                      status=200, attempt=2, requests=3, cache_hits=4, stale_hits=5, rows=6)
    assert log.record("query_complete", action="stats", kind="overview", page=1)
    contents, records = unpack(log.export_zip())
    assert records[0] == {
        "timestamp": records[0]["timestamp"], "event": "image_send_error", "error": "ActionFailed",
        "retcode": 1200, "message": "Timeout: sendMsg", "path": "report.jpg",
        "image_type": "JPEG", "image_bytes": 12345, "duration_ms": 10,
    }
    assert records[1]["route"] == "/api/players/search"
    assert records[1]["status"] == 200 and records[1]["attempt"] == 2
    assert records[1]["rows"] == 6
    assert records[2]["action"] == "stats" and records[2]["kind"] == "overview" and records[2]["page"] == 1
    raw = b"".join(contents.values()).decode()
    assert not any(secret in raw for secret in (
        "SECRET", "secret-dir", "private-user", "ZGF0YQ==", "secret.invalid", "private-query", "response-body",
    ))


def test_sensitive_values_cannot_hide_in_new_allowlist_fields(tmp_path):
    log = DiagnosticLog(tmp_path, enabled=True)
    fields = dict.fromkeys(("action", "kind", "page", "error", "retcode", "message", "path",
                            "image_type", "image_bytes", "duration_ms", "status", "attempt", "route",
                            "requests", "cache_hits", "stale_hits", "rows"), "BARE_PRIVATE_TOKEN")
    assert log.record("image_send_error", **fields)
    contents, records = unpack(log.export_zip())
    assert "BARE_PRIVATE_TOKEN" not in b"".join(contents.values()).decode()
    assert records[0] == {"timestamp": records[0]["timestamp"], "event": "image_send_error",
                          "message": "details_omitted"}


@pytest.mark.parametrize("fields", [
    {"duration_ms": float("nan"), "count": True},
    {"duration_ms": float("inf"), "count": -1},
    {"duration_ms": -1, "count": 10**100, "status_code": 1000},
    {"configured": 1, "status_code": True},
])
def test_numeric_and_boolean_fields_have_strict_bounds(tmp_path, fields):
    log = DiagnosticLog(tmp_path, enabled=True)
    assert log.record("request", **fields)
    assert set(unpack(log.export_zip())[1][0]) == {"timestamp", "event"}


def test_rotation_checks_total_disk_bytes_before_each_append_and_restart(tmp_path):
    budget = 1100
    log = DiagnosticLog(tmp_path, enabled=True, max_total_bytes=budget, max_file_bytes=300)
    for index in range(150):
        assert log.record("request", count=index, status_code=200)
        files = list(tmp_path.glob("diagnostics-*.jsonl"))
        total = sum(path.stat().st_size for path in files)
        assert total == log.total_bytes <= budget
        assert all(path.stat().st_size <= 300 for path in files)
    contents, records = unpack(log.export_zip())
    assert len(contents) > 1
    assert len(records) < 150 and records[0]["count"] > 0
    assert records[-1]["count"] == 149
    assert sum(map(len, contents.values())) == log.total_bytes
    log.close()
    restarted = DiagnosticLog(tmp_path, enabled=True, max_total_bytes=budget, max_file_bytes=300)
    assert restarted.record("request", count=150)
    assert unpack(restarted.export_zip())[1][-1]["count"] == 150
    assert sum(p.stat().st_size for p in tmp_path.glob("*.jsonl")) <= budget
    restarted.close()
    smaller = DiagnosticLog(tmp_path, max_total_bytes=250, max_file_bytes=250)
    assert smaller.total_bytes <= 250


def test_real_30_mib_hard_limit_prunes_preexisting_managed_files(tmp_path):
    # Sparse synthetic segments let this exercise the production byte ceiling
    # without generating hundreds of thousands of individual events.
    for index in range(4):
        path = tmp_path / f"diagnostics-{index:016d}.jsonl"
        with path.open("wb") as stream:
            stream.truncate(10 * 1024 * 1024)
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("SYNTHETIC unrelated file", encoding="utf-8")
    log = DiagnosticLog(tmp_path)
    assert log.total_bytes == MAX_TOTAL_BYTES
    assert not (tmp_path / "diagnostics-0000000000000000.jsonl").exists()
    log.set_enabled(True)
    assert log.record("lifecycle", phase="initialize")
    assert sum(p.stat().st_size for p in tmp_path.glob("*.jsonl")) == log.total_bytes <= MAX_TOTAL_BYTES
    assert unrelated.read_text(encoding="utf-8") == "SYNTHETIC unrelated file"


def test_oversized_record_is_dropped_without_eviction(tmp_path):
    log = DiagnosticLog(tmp_path, enabled=True, max_total_bytes=120, max_file_bytes=120)
    assert log.record("cache")
    previous = unpack(log.export_zip())[0]
    assert not log.record("request", error_code="identity_mismatch", operation="player", phase="error")
    assert unpack(log.export_zip())[0] == previous


def test_concurrent_writers_and_exports_are_complete_consistent_snapshots(tmp_path):
    log = DiagnosticLog(tmp_path, enabled=True, max_total_bytes=5000, max_file_bytes=450)
    barrier = Barrier(5)

    def writer(offset):
        barrier.wait(timeout=10)
        for index in range(100):
            assert log.record("request", count=offset + index, status_code=200)

    def exporter():
        barrier.wait(timeout=10)
        for _ in range(50):
            contents, records = unpack(log.export_zip())
            assert sum(map(len, contents.values())) <= 5000
            assert all(record["event"] == "request" for record in records)
            assert len({record["count"] for record in records}) == len(records)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(writer, index * 100) for index in range(4)]
        futures.append(pool.submit(exporter))
        for future in futures:
            future.result(timeout=20)
    assert 0 < log.total_bytes <= 5000
    assert sum(p.stat().st_size for p in tmp_path.glob("*.jsonl")) == log.total_bytes
    assert unpack(log.export_zip())[1]


def test_write_failure_does_not_break_caller_or_poison_future_writes(tmp_path, monkeypatch):
    from pathlib import Path

    log = DiagnosticLog(tmp_path, enabled=True)
    original = Path.open

    def failing_open(path, mode="r", *args, **kwargs):
        if mode in {"ab", "xb"}:
            raise OSError("SYNTHETIC disk failure; secret must not be logged")
        return original(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", failing_open)
        assert not log.record("request", count=1)
    assert log.record("request", count=2)
    assert [record["count"] for record in unpack(log.export_zip())[1]] == [2]


def test_partial_write_rolls_back_to_complete_jsonl_and_keeps_capacity(tmp_path, monkeypatch):
    log = DiagnosticLog(tmp_path, enabled=True)
    assert log.record("command_start", action="stats")
    before = unpack(log.export_zip())[0]
    original = Path.open

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, line):
            self.stream.write(line[:10])
            raise OSError("SYNTHETIC disk full")

    def partial_open(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        return PartialWriter(stream) if mode in {"ab", "xb"} else stream

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", partial_open)
        assert not log.record("query_complete", kind="overview")
    assert unpack(log.export_zip())[0] == before
    assert log.total_bytes == sum(map(len, before.values()))
    assert log.record("command_complete", action="stats")
    assert len(unpack(log.export_zip())[1]) == 2


@pytest.mark.parametrize("options", [
    {"max_total_bytes": MAX_TOTAL_BYTES + 1}, {"max_total_bytes": 0},
    {"max_total_bytes": True}, {"max_file_bytes": 0}, {"enabled": "false"},
])
def test_capacity_cannot_exceed_production_limit(tmp_path, options):
    with pytest.raises(ValueError):
        DiagnosticLog(tmp_path, **options)
