"""Opt-in, metadata-only JSONL diagnostics; no AstrBot dependency.

Create ONE DiagnosticLog per dedicated data directory. The directory must not
be shared with another writer/process. Managed JSONL files together never
exceed MAX_TOTAL_BYTES; ZIP exports are in memory and do not add disk files.
Unknown events/fields and invalid values are discarded, never stringified.
No free-form text, identifiers, URLs, exceptions, headers or payloads are kept.

Integration: DiagnosticLog(data_dir / "diagnostics", enabled=settings_flag),
record("command", command=request.action, phase="finish", outcome="success",
duration_ms=elapsed_ms), then close() on plugin termination. Run export_zip()
in asyncio.to_thread from a host-authenticated download handler. Disabling
stops writes immediately but keeps previous logs available for export.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import math
from pathlib import Path
import re
from threading import RLock
from zipfile import ZIP_DEFLATED, ZipFile


MAX_TOTAL_BYTES = 30 * 1024 * 1024
DEFAULT_FILE_BYTES = 1024 * 1024
_FILENAME = re.compile(r"diagnostics-([0-9]{16,})\.jsonl\Z")
_EVENTS = frozenset({
    "lifecycle", "command", "query", "request", "cache", "render", "send", "fallback", "quota", "export",
    "plugin_start", "plugin_stop", "command_start", "command_complete", "command_error",
    "query_complete", "image_cache", "image_render_start", "image_render_complete", "image_render_error",
    "image_send_start", "image_send_complete", "image_send_error", "image_validated", "image_cache_invalid", "text_report",
    "text_send_start", "text_send_complete", "text_send_error", "http_start", "http_response", "http_error",
})
_ACTIONS = frozenset({"help", "stats", "recent", "last", "match", "search", "bind", "binding", "unbind", "select", "cancel", "status", "quota", "cache_clear", "unknown"})
_ERROR_CODES = frozenset({"client_error", "not_configured", "authentication", "rate_limited", "timeout", "connection", "upstream", "no_data", "schema", "identity_mismatch", "queue_full", "closed", "daily_quota", "invalid_command", "invalid_input", "permission_denied", "render_error", "storage_error", "action_failed", "unknown"})
_ERROR_TYPES = frozenset({
    "ActionFailed", "TimeoutError", "CancelledError", "RuntimeError", "ValueError", "TypeError", "KeyError",
    "OSError", "PermissionError", "FileNotFoundError", "ConnectionError", "ConnectError", "ConnectTimeout",
    "ReadTimeout", "WriteTimeout", "PoolTimeout", "HTTPError", "HTTPStatusError", "RequestError",
    "ClientError", "NotConfiguredError", "AuthenticationError", "RateLimitError", "RequestTimeoutError",
    "UpstreamError", "NoDataError", "SchemaError", "IdentityMismatchError", "QueueFullError", "ClientClosedError",
    "QuotaExceededError", "ImageTransportError", "UnidentifiedImageError", "DecompressionBombError",
    "DecompressionBombWarning", "ClientConnectorError", "ClientPayloadError", "ClientResponseError", "ServerTimeoutError",
})
_ROUTES = frozenset({"/api/players/search", "/api/analysis/player", "/api/players/matches", "/api/match"})
_IMAGE_NAME = re.compile(r"(?:[0-9a-f]{64}|report|card|image)(?:-[0-9]{1,4})?\.(?:jpg|jpeg|png|webp|gif)\Z", re.IGNORECASE)
_MESSAGE_MARKERS = (
    ("Timeout", re.compile(r"\btimeout\b|\btimed?\s*out\b|超时", re.IGNORECASE)),
    ("sendMsg", re.compile(r"\bsend(?:group|private)?msg\b", re.IGNORECASE)),
    ("image", re.compile(r"\bimage\b|图片", re.IGNORECASE)),
    ("upload", re.compile(r"\bupload\w*\b|上传", re.IGNORECASE)),
    ("decode", re.compile(r"\bdecod\w*\b|解码", re.IGNORECASE)),
    ("permission_denied", re.compile(r"permission|forbidden|权限", re.IGNORECASE)),
    ("rate_limited", re.compile(r"rate.limit|限流", re.IGNORECASE)),
    ("connection", re.compile(r"\bconnect\w*\b|连接", re.IGNORECASE)),
    ("failed", re.compile(r"\bfail\w*\b|失败", re.IGNORECASE)),
)
_ENUMS = {
    "mode": frozenset({"base64", "file", "url"}),
    "stage": frozenset({"generate", "download", "validate", "cache", "send"}),
    "reason": frozenset({"generation_failed", "download_failed", "validation_failed", "cache_failed", "send_failed",
                         "timeout", "http_status", "invalid_content_type", "invalid_image_size", "invalid_image",
                         "unsupported_image", "url_unavailable", "local_path_unavailable", "unsupported_mode",
                         "onebot_required", "invalid_target", "onebot_rejected"}),
    "content_type": frozenset({"image/jpeg", "image/png", "image/gif", "image/webp", "application/octet-stream",
                               "text/plain", "text/html", "application/json"}),
    "phase": frozenset({"start", "finish", "error", "cancel", "initialize", "terminate", "enable", "disable"}),
    "command": _ACTIONS,
    "action": _ACTIONS,
    "kind": frozenset({"text", "image", "overview", "recent", "match", "search", "binding", "help", "status", "quota", "error", "unknown"}),
    "operation": frozenset({"search", "player", "recent", "match", "render", "export", "initialize", "terminate"}),
    "outcome": frozenset({"success", "error", "cancelled", "rejected", "hit", "miss", "stale"}),
    "error_code": _ERROR_CODES,
    "error": _ERROR_CODES | _ERROR_TYPES,
    "image_type": frozenset({"JPEG", "JPG", "PNG", "GIF", "WEBP", "UNKNOWN", "jpeg", "jpg", "png", "gif", "webp", "unknown", "未知"}),
    "scope": frozenset({"global", "user", "group"}),
}
_COUNTERS = frozenset({"count", "inflight", "requests", "cache_hits", "stale_hits", "bytes", "retry", "limit", "used", "remaining", "page", "image_bytes", "attempt", "rows", "width", "height"})
_BOOLEANS = frozenset({"configured", "closed", "force", "cached", "stale"})


def _safe_fields(fields: dict) -> dict:
    result = {}
    for key, choices in _ENUMS.items():
        value = fields.get(key)
        if type(value) is str and value in choices:
            result[key] = value
    for key in _COUNTERS:
        value = fields.get(key)
        if type(value) is int and 0 <= value <= 1_000_000_000:
            result[key] = value
    for key in _BOOLEANS:
        value = fields.get(key)
        if type(value) is bool:
            result[key] = value
    value = fields.get("status_code")
    if type(value) is int and 100 <= value <= 599:
        result["status_code"] = value
    value = fields.get("status")
    if type(value) is int and 100 <= value <= 599:
        result["status"] = value
    elif type(value) is str and value in _ERROR_CODES | {"ok", "success", "error", "failed", "fallback", "0.1.2", "0.1.3"}:
        result["status"] = value
    value = fields.get("retcode")
    if type(value) is int and -1_000_000_000 <= value <= 1_000_000_000:
        result["retcode"] = value
    value = fields.get("route")
    if type(value) is str:
        route = value.split("?", 1)[0].split("#", 1)[0]
        if route in _ROUTES:
            result["route"] = route
    value = fields.get("path")
    if type(value) is str:
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        if _IMAGE_NAME.fullmatch(name):
            result["path"] = name
    value = fields.get("message")
    if type(value) is str:
        # Extract fixed vocabulary only. Regex redaction of arbitrary exception
        # strings cannot guarantee removal of bare tokens or response bodies.
        markers = [label for label, pattern in _MESSAGE_MARKERS if pattern.search(value[:4096])]
        result["message"] = ": ".join(markers) if markers else "details_omitted"
    value = fields.get("duration_ms")
    if type(value) in (int, float) and 0 <= value <= 1_000_000_000 and math.isfinite(value):
        result["duration_ms"] = value
    return result


def image_error_metadata(exc, mode, stage):
    """Share the same allowlist between always-on console errors and diagnostics."""
    result = getattr(exc, "result", None)
    result = result if isinstance(result, dict) else {}
    cause = exc.__cause__ or exc
    defaults = {"generate": "generation_failed", "download": "download_failed", "validate": "validation_failed",
                "cache": "cache_failed", "send": "send_failed"}
    return _safe_fields({
        "mode": mode, "stage": stage if stage == "cache" else getattr(exc, "stage", stage),
        "error": type(cause).__name__, "retcode": result.get("retcode"),
        "reason": getattr(exc, "reason", "timeout" if isinstance(exc, TimeoutError) else defaults.get(stage)),
        "message": result.get("message") or result.get("wording"),
        "status_code": getattr(exc, "status_code", None), "content_type": getattr(exc, "content_type", None),
        "image_bytes": getattr(exc, "image_bytes", None),
    })


class DiagnosticLog:
    """Thread-safe bounded recorder with atomic ZIP snapshots.

    A smaller byte budget is accepted for tests/deployments, never a larger one.
    record() returns False when disabled, closed, invalid, too large, or an OS
    write fails. Export errors propagate to the download handler. close() is
    permanent; set_enabled() is the reversible configuration switch.
    """

    def __init__(self, directory: str | Path, enabled: bool = False, *,
                 max_total_bytes: int = MAX_TOTAL_BYTES,
                 max_file_bytes: int = DEFAULT_FILE_BYTES):
        if type(max_total_bytes) is not int or not 1 <= max_total_bytes <= MAX_TOTAL_BYTES:
            raise ValueError("diagnostic total must be between 1 byte and 30 MiB")
        if type(max_file_bytes) is not int or max_file_bytes < 1:
            raise ValueError("diagnostic file size must be positive")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self._directory = Path(directory)
        self._max_total = max_total_bytes
        self._max_file = min(max_file_bytes, max_total_bytes)
        self._lock = RLock()
        self._enabled = enabled
        self._closed = False
        self._files: list[tuple[Path, int]] = []
        self._total = 0
        self._next_id = 0
        # Read and enforce an existing ring after restart, even when disabled.
        # A new disabled recorder does not create a directory or any file.
        try:
            self._scan()
            self._trim()
        except OSError:
            self._enabled = False

    def _scan(self) -> None:
        files = []
        for path in self._directory.glob("diagnostics-*.jsonl"):
            match = _FILENAME.fullmatch(path.name)
            if match and not path.is_symlink() and path.is_file():
                serial = int(match[1])
                files.append((serial, path, path.stat().st_size))
        files.sort(key=lambda item: item[0])
        self._files = [(path, size) for _, path, size in files]
        self._total = sum(size for _, size in self._files)
        if files:
            self._next_id = max(self._next_id, files[-1][0] + 1)

    def _trim(self, reserve: int = 0) -> None:
        while self._files and self._total + reserve > self._max_total:
            path, size = self._files[0]
            path.unlink(missing_ok=True)
            self._files.pop(0)
            self._total -= size

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled and not self._closed

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total

    def stats(self) -> dict:
        """Safe UI metadata, without file paths or arbitrary configuration."""
        with self._lock:
            return {"enabled": self._enabled and not self._closed,
                    "bytes": self._total, "limit_bytes": self._max_total,
                    "files": len(self._files)}

    def set_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            self._enabled = enabled and not self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._enabled = False

    def record(self, event: str, **fields) -> bool:
        with self._lock:
            if not self._enabled or self._closed or type(event) is not str or event not in _EVENTS:
                return False
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "event": event,
                **_safe_fields(fields),
            }
            line = (json.dumps(entry, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(line) > self._max_file:
                return False
            path, size, opened = None, 0, False
            try:
                # Evict BEFORE append, including the active segment if needed.
                self._trim(len(line))
                append = bool(self._files and self._files[-1][1] + len(line) <= self._max_file)
                if append:
                    path, size = self._files[-1]
                else:
                    self._directory.mkdir(parents=True, exist_ok=True)
                    path = self._directory / f"diagnostics-{self._next_id:016d}.jsonl"
                    self._next_id += 1
                    size = 0
                with path.open("ab" if append else "xb") as stream:
                    opened = True
                    if stream.write(line) != len(line):
                        raise OSError("incomplete diagnostic write")
                if append:
                    self._files[-1] = (path, size + len(line))
                else:
                    self._files.append((path, len(line)))
                self._total += len(line)
                return True
            except OSError:
                # Partial writes are still bounded by the pre-reserved bytes.
                # Reconcile sizes so future attempts cannot overrun the budget.
                try:
                    if opened and path is not None and path.exists():
                        # Roll back a partial JSONL line after disk-full/flush
                        # errors; never append the next event onto broken JSON.
                        if size:
                            with path.open("r+b") as stream:
                                stream.truncate(size)
                        else:
                            path.unlink()
                    self._scan()
                    self._trim()
                except OSError:
                    self._enabled = False
                return False

    def export_zip(self) -> bytes:
        """Snapshot all managed segments without rotating, clearing or logging.

        File reads share the writer lock; compression occurs after releasing it.
        ZIP member names contain no absolute/user paths. Closed/disabled logs
        remain downloadable. An empty recorder produces a valid empty ZIP.
        The caller must authenticate the request; this module has no HTTP API.
        """
        with self._lock:
            snapshot = [(path.name, path.read_bytes()) for path, _ in self._files]
        result = BytesIO()
        with ZipFile(result, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in snapshot:
                archive.writestr(name, data)
        return result.getvalue()
