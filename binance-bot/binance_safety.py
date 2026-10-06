#!/usr/bin/env python3
"""Shared request safety primitives for the Binance bot.

This module deliberately fails closed around Binance rate-limit responses.  A
429, 418, or API error -1003 opens a persisted circuit for the process/IP
scope and is never retried by this bot.  The caller must stop and investigate
before restarting it.

The in-process governor is keyed by ``ip_scope`` and endpoint name.  The bot
also takes a single-instance file lock, which prevents two copies on the same
host from bypassing the governor.  Binance still applies limits to the public
IP across every process/service/NAT user, so the operator must keep other
clients on the same IP under control too.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from datetime import timezone
from typing import Any, Callable, Mapping, Optional


# Keep a large safety margin below the 2,400 request-weight/minute USD-M
# default.  The server response headers remain the source of truth.
DEFAULT_WEIGHT_PER_MINUTE = 1_200
DEFAULT_429_COOLDOWN = 120.0
DEFAULT_418_COOLDOWN = 900.0
DEFAULT_API_1003_COOLDOWN = 900.0


class BinanceSafetyStop(RuntimeError):
    """Base class for errors that must stop Binance requests/trading."""


class BinanceCircuitOpen(BinanceSafetyStop):
    """Raised when the persisted per-IP circuit is open."""


class BinanceRateLimitError(BinanceSafetyStop):
    """A Binance 429/418/-1003 response; never retry automatically."""


class BinanceAPIError(RuntimeError):
    """Structured non-rate-limit HTTP/API error from a Binance endpoint."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        method: str,
        endpoint: str,
        status_code: Optional[int] = None,
        api_code: Optional[int] = None,
        headers: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        retry_after: Optional[float] = None,
    ) -> None:
        self.request_id = request_id
        self.method = method
        self.endpoint = endpoint
        self.status_code = status_code
        self.api_code = api_code
        self.headers = dict(headers or {})
        self.body = redact_body(body)
        self.retry_after = retry_after
        super().__init__(message)


class RequestGovernor:
    """Small sliding-window/per-endpoint limiter and circuit breaker.

    ``ip_scope`` is a label for the public-IP bucket.  It is intentionally
    configurable because a host's private address is not necessarily its NAT
    public address.  One process lock plus the bot instance lock protects the
    common single-host deployment; persistent circuit state protects restarts.
    """

    def __init__(
        self,
        *,
        ip_scope: str = "binance-public-ip",
        max_weight_per_minute: int = DEFAULT_WEIGHT_PER_MINUTE,
        endpoint_intervals: Optional[Mapping[str, float]] = None,
    ) -> None:
        self.ip_scope = ip_scope
        self.max_weight_per_minute = max_weight_per_minute
        self.endpoint_intervals = {
            "/fapi/v1/klines": 0.25,
            "/fapi/v1/ticker/24hr": 2.0,
            "/fapi/v2/ticker/price": 5.0,
            "/fapi/v1/exchangeInfo": 60.0,
            "/fapi/v1/time": 1.0,
            "websocket_connect": 15.0,
            "private:account": 1.0,
            "private:exchange_info": 1.0,
            "private:trade": 0.35,
            "private:order_status": 1.0,
            "private:user_stream": 60.0,
        }
        if endpoint_intervals:
            self.endpoint_intervals.update(endpoint_intervals)
        self._lock = threading.RLock()
        self._last_request: dict[str, float] = {}
        self._weighted_requests: list[tuple[float, int]] = []
        self._cooldown_until = 0.0
        self._cooldown_reason = ""
        self._cooldown_meta: dict[str, Any] = {}
        self._state_path: Optional[str] = None
        self._loaded_path: Optional[str] = None
        self._log: Optional[Callable[[str], None]] = None
        self._observed_weight: Optional[int] = None
        self._observed_order_count: Optional[int] = None

    def configure(
        self,
        *,
        ip_scope: Optional[str] = None,
        state_path: Optional[str] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        with self._lock:
            if ip_scope:
                self.ip_scope = ip_scope
            if logger is not None:
                self._log = logger
            if state_path and state_path != self._loaded_path:
                self._state_path = state_path
                self._loaded_path = state_path
                self._load_state_locked()

    def _load_state_locked(self) -> None:
        if not self._state_path:
            return
        try:
            with open(self._state_path) as f:
                data = json.load(f)
            # Do not import a circuit from another IP scope.
            if data.get("ip_scope") != self.ip_scope:
                return
            self._cooldown_until = float(data.get("open_until", 0) or 0)
            self._cooldown_reason = str(data.get("reason", ""))
            self._cooldown_meta = dict(data.get("meta") or {})
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return

    def _save_state_locked(self) -> None:
        if not self._state_path:
            return
        payload = {
            "ip_scope": self.ip_scope,
            "open_until": self._cooldown_until,
            "reason": self._cooldown_reason,
            "meta": self._cooldown_meta,
            "updated_at": time.time(),
        }
        tmp = self._state_path + ".tmp"
        try:
            parent = os.path.dirname(self._state_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(payload, f, sort_keys=True)
            os.replace(tmp, self._state_path)
        except OSError:
            # Failure to write diagnostics must not turn a trading error into
            # an unhandled exception.  The in-memory circuit remains active.
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _log_event(self, message: str) -> None:
        if self._log:
            try:
                self._log(message)
            except Exception:
                pass

    def ensure_allowed(self) -> None:
        with self._lock:
            now = time.time()
            if self._cooldown_until > now:
                remaining = self._cooldown_until - now
                raise BinanceCircuitOpen(
                    "Binance circuit OPEN for ip_scope=%s remaining=%.1fs "
                    "reason=%s meta=%s"
                    % (self.ip_scope, remaining, self._cooldown_reason,
                       self._cooldown_meta)
                )

    def acquire(self, endpoint: str, *, weight: int = 1) -> None:
        """Wait for the local budget, then reserve one request.

        This method may sleep for a normal pacing delay.  It never sleeps to
        hide a rate-limit response: once the circuit is open it raises.
        """
        weight = max(1, int(weight))
        while True:
            with self._lock:
                now = time.monotonic()
                wall_now = time.time()
                if self._cooldown_until > wall_now:
                    remaining = self._cooldown_until - wall_now
                    raise BinanceCircuitOpen(
                        "Binance circuit OPEN before %s, remaining=%.1fs "
                        "reason=%s" % (endpoint, remaining,
                                         self._cooldown_reason)
                    )

                self._weighted_requests = [
                    (when, w) for when, w in self._weighted_requests
                    if now - when < 60.0
                ]
                used = sum(w for _, w in self._weighted_requests)
                last = self._last_request.get(endpoint, 0.0)
                interval = float(self.endpoint_intervals.get(endpoint, 0.25))
                wait_endpoint = max(0.0, interval - (now - last))
                wait_weight = 0.0
                if used + weight > self.max_weight_per_minute:
                    if self._weighted_requests:
                        wait_weight = max(
                            0.01,
                            60.0 - (now - self._weighted_requests[0][0]),
                        )
                    else:
                        wait_weight = 1.0
                wait_for = max(wait_endpoint, wait_weight)
                if wait_for <= 0:
                    self._last_request[endpoint] = now
                    self._weighted_requests.append((now, weight))
                    return
            time.sleep(min(wait_for, 5.0))

    def observe_headers(self, headers: Optional[Mapping[str, Any]]) -> None:
        """Record Binance's used-weight header for diagnostics/headroom."""
        if not headers:
            return
        used = None
        order_count = None
        for name, value in headers.items():
            key = str(name).lower()
            if key.startswith("x-mbx-used-weight-"):
                try:
                    candidate = int(float(value))
                except (TypeError, ValueError):
                    continue
                used = max(used or 0, candidate)
            elif key.startswith("x-mbx-order-count-"):
                try:
                    candidate = int(float(value))
                except (TypeError, ValueError):
                    continue
                order_count = max(order_count or 0, candidate)
        if used is not None or order_count is not None:
            with self._lock:
                if used is not None:
                    self._observed_weight = used
                if order_count is not None:
                    self._observed_order_count = order_count

    def trip(
        self,
        *,
        reason: str,
        retry_after: Optional[float] = None,
        status_code: Optional[int] = None,
        api_code: Optional[int] = None,
        endpoint: str = "",
        request_id: str = "",
    ) -> float:
        """Open/persist the circuit and return its absolute expiry."""
        if retry_after is not None:
            try:
                delay = max(1.0, float(retry_after))
            except (TypeError, ValueError):
                delay = 0.0
        else:
            delay = 0.0
        if delay <= 0:
            if status_code == 418:
                delay = DEFAULT_418_COOLDOWN
            elif api_code == -1003:
                delay = DEFAULT_API_1003_COOLDOWN
            else:
                delay = DEFAULT_429_COOLDOWN
        meta = {
            "status_code": status_code,
            "api_code": api_code,
            "endpoint": endpoint,
            "request_id": request_id,
        }
        with self._lock:
            until = time.time() + delay
            # Never shorten an existing ban/cooldown.
            self._cooldown_until = max(self._cooldown_until, until)
            self._cooldown_reason = reason
            self._cooldown_meta = meta
            self._save_state_locked()
            self._log_event(
                "BINANCE CIRCUIT OPEN ip_scope=%s seconds=%.1f reason=%s "
                "status=%s api_code=%s endpoint=%s request_id=%s"
                % (self.ip_scope, self._cooldown_until - time.time(), reason,
                   status_code, api_code, endpoint, request_id)
            )
            return self._cooldown_until

    def reset_for_tests(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0
            self._cooldown_reason = ""
            self._cooldown_meta = {}
            self._weighted_requests.clear()
            self._last_request.clear()
            self._observed_weight = None
            self._observed_order_count = None
            if self._state_path:
                try:
                    os.remove(self._state_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    @property
    def observed_weight(self) -> Optional[int]:
        with self._lock:
            return self._observed_weight

    @property
    def observed_order_count(self) -> Optional[int]:
        with self._lock:
            return self._observed_order_count


GOVERNOR = RequestGovernor()
_LOGGER: Optional[Callable[[str], None]] = None


def configure(
    *,
    logger: Optional[Callable[[str], None]] = None,
    state_path: Optional[str] = None,
    ip_scope: Optional[str] = None,
) -> None:
    global _LOGGER
    if logger is not None:
        _LOGGER = logger
    GOVERNOR.configure(
        logger=logger,
        state_path=state_path,
        ip_scope=ip_scope or os.environ.get("BINANCE_IP_SCOPE"),
    )


def log(message: str) -> None:
    if _LOGGER:
        try:
            _LOGGER(message)
        except Exception:
            pass


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def redact_body(body: Any, limit: int = 900) -> str:
    """Return bounded error text without credentials/signatures."""
    if body is None:
        return ""
    try:
        if isinstance(body, (dict, list)):
            text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(body)
    except Exception:
        text = repr(body)
    text = re.sub(r"(?i)(api[-_]?key|secret|signature|listenKey)\s*[=:]\s*[^,&\s}]+",
                  r"\1=<redacted>", text)
    text = re.sub(r"(?i)(X-MBX-APIKEY)\s*[:=]\s*[^,\s]+",
                  r"\1=<redacted>", text)
    return text[:limit]


def _header(headers: Optional[Mapping[str, Any]], name: str) -> Any:
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return value
    return None


def parse_retry_after(headers: Optional[Mapping[str, Any]]) -> Optional[float]:
    value = _header(headers, "Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def parse_api_code(body: Any, text: str = "") -> Optional[int]:
    candidates = []
    if isinstance(body, Mapping):
        candidates.append(body.get("code"))
    candidates.extend([
        re.search(r"[\"']code[\"']\s*:\s*(-?\d+)", text or ""),
        re.search(r"\bcode\s*[=:]\s*(-?\d+)", text or "", re.I),
    ])
    for item in candidates:
        value = item.group(1) if hasattr(item, "group") else item
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def parse_status(exc: BaseException) -> Optional[int]:
    for obj in (exc, getattr(exc, "response", None)):
        if obj is None:
            continue
        for attr in ("status_code", "http_status_code", "status"):
            value = getattr(obj, attr, None)
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                pass
        if isinstance(obj, Mapping):
            for key in ("status_code", "http_status_code", "status"):
                try:
                    if obj.get(key) is not None:
                        return int(obj[key])
                except (TypeError, ValueError):
                    pass
    match = re.search(r"\b(418|429)\b", str(exc))
    return int(match.group(1)) if match else None


def exception_headers(exc: BaseException) -> dict[str, Any]:
    for obj in (exc, getattr(exc, "response", None)):
        headers = getattr(obj, "headers", None) if obj is not None else None
        if isinstance(headers, Mapping):
            return dict(headers)
        if isinstance(obj, Mapping) and isinstance(obj.get("headers"), Mapping):
            return dict(obj["headers"])
    return {}


def exception_body(exc: BaseException) -> Any:
    for attr in ("body", "response_text", "text", "message"):
        value = getattr(exc, attr, None)
        if value:
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "text", None)
        if value:
            return value
        if isinstance(response, Mapping):
            return response.get("body") or response
    return str(exc)


def classify_exception(exc: BaseException) -> tuple[Optional[int], Optional[int], dict[str, Any], str]:
    """Extract status/API code/body from requests, ccxt, or plain exceptions."""
    status = parse_status(exc)
    headers = exception_headers(exc)
    body = exception_body(exc)
    body_text = redact_body(body)
    api_code = parse_api_code(body, body_text + " " + str(exc))
    return status, api_code, headers, body_text


def is_rate_limit_failure(
    *,
    status_code: Optional[int],
    api_code: Optional[int],
    body: Any = "",
    exc: Optional[BaseException] = None,
) -> bool:
    text = (redact_body(body) + " " + str(exc or "")).lower()
    # Chi trip khi co dau hieu ban THAT (status/api code hoac message loi cu the).
    # Khong match "rate limit" chung chung vi dinh ca CCXT warning
    # (vd: "fetching open orders without symbol has stricter rate limits").
    return (
        status_code in (418, 429)
        or api_code == -1003
        or "too many requests" in text
        or "way too many requests" in text
        or "ip banned" in text
        or "banned until" in text
        or "ip ban" in text
        or "request weight" in text and "limit" in text
    )


def trip_for_exception(
    exc: BaseException,
    *,
    endpoint: str,
    request_id: str = "",
) -> Optional[BinanceRateLimitError]:
    status, api_code, headers, body = classify_exception(exc)
    if not is_rate_limit_failure(
        status_code=status, api_code=api_code, body=body, exc=exc
    ):
        return None
    retry_after = parse_retry_after(headers)
    GOVERNOR.trip(
        reason="rate_limit_response",
        retry_after=retry_after,
        status_code=status,
        api_code=api_code,
        endpoint=endpoint,
        request_id=request_id,
    )
    return BinanceRateLimitError(
        "Binance rate limit/ban: endpoint=%s status=%s api_code=%s "
        "retry_after=%s request_id=%s body=%s"
        % (endpoint, status, api_code, retry_after, request_id,
           redact_body(body)),
    )


def call_private(
    endpoint: str,
    fn: Callable[..., Any],
    *args: Any,
    exchange: Any = None,
    request_id: Optional[str] = None,
    weight: int = 1,
    **kwargs: Any,
) -> Any:
    """Rate-limit and classify one ccxt private/public call."""
    rid = request_id or new_request_id()
    GOVERNOR.acquire(endpoint, weight=weight)
    started = time.monotonic()
    try:
        result = fn(*args, **kwargs)
        headers = getattr(exchange, "last_response_headers", None)
        GOVERNOR.observe_headers(headers)
        log("BINANCE CCXT request_id=%s endpoint=%s status=success "
            "latency_ms=%.1f used_weight=%s order_count=%s"
            % (rid, endpoint, (time.monotonic() - started) * 1000,
               GOVERNOR.observed_weight, GOVERNOR.observed_order_count))
        return result
    except BinanceSafetyStop:
        raise
    except Exception as exc:
        status, api_code, headers, body = classify_exception(exc)
        if not headers and exchange is not None:
            candidate = getattr(exchange, "last_response_headers", None)
            if isinstance(candidate, Mapping):
                headers = dict(candidate)
        GOVERNOR.observe_headers(headers)
        log("BINANCE CCXT request_id=%s endpoint=%s status=%s api_code=%s "
            "latency_ms=%.1f retry_after=%s used_weight=%s order_count=%s "
            "body=%s"
            % (rid, endpoint, status, api_code,
               (time.monotonic() - started) * 1000,
               parse_retry_after(headers), GOVERNOR.observed_weight,
               GOVERNOR.observed_order_count, redact_body(body)))
        fatal = trip_for_exception(exc, endpoint=endpoint, request_id=rid)
        if fatal:
            raise fatal from exc
        raise


def acquire(endpoint: str, *, weight: int = 1) -> None:
    GOVERNOR.acquire(endpoint, weight=weight)


def observe_headers(headers: Optional[Mapping[str, Any]]) -> None:
    GOVERNOR.observe_headers(headers)


def ensure_allowed() -> None:
    GOVERNOR.ensure_allowed()


def reset_for_tests() -> None:
    GOVERNOR.reset_for_tests()
    global _LOGGER
    _LOGGER = None


__all__ = [
    "BinanceAPIError",
    "BinanceCircuitOpen",
    "BinanceRateLimitError",
    "BinanceSafetyStop",
    "GOVERNOR",
    "acquire",
    "call_private",
    "classify_exception",
    "configure",
    "ensure_allowed",
    "is_rate_limit_failure",
    "log",
    "new_request_id",
    "observe_headers",
    "parse_api_code",
    "parse_retry_after",
    "redact_body",
    "reset_for_tests",
    "trip_for_exception",
]
