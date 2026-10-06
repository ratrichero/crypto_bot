#!/usr/bin/env python3
"""Shared Binance USDⓈ-M public REST client.

The bot has one session/connection pool and one request governor.  In
particular, HTTP 429, HTTP 418, and API code -1003 are *not* retried: they
open a persisted circuit and the caller must stop the bot.  This is required
because Binance applies request-weight limits to the public IP, not only to
an API key.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional

import requests
from requests.adapters import HTTPAdapter

import binance_safety

BASE = "https://fapi.binance.com"
TIMEOUT = 12

# One pool is shared by all public REST calls in this process.  urllib3 retry
# is deliberately disabled: a generic retry adapter would retry 429/418.
SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "crypto-bot/binance-usdm-safe-client/1.0",
})
SESSION.mount(
    "https://",
    HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0),
)
SESSION.mount(
    "http://",
    HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0),
)

_LOGGER: Optional[Callable[[str], None]] = None


def configure(
    logger: Optional[Callable[[str], None]] = None,
    *,
    state_path: Optional[str] = None,
    ip_scope: Optional[str] = None,
) -> None:
    """Attach bot logging and configure the shared persisted circuit."""
    global _LOGGER
    if logger is not None:
        _LOGGER = logger
    binance_safety.configure(
        logger=logger,
        state_path=state_path,
        ip_scope=ip_scope,
    )


def _log(message: str) -> None:
    if _LOGGER:
        try:
            _LOGGER(message)
        except Exception:
            pass
    else:
        binance_safety.log(message)


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except (ValueError, TypeError):
        return response.text


def _request_error(
    *,
    request_id: str,
    method: str,
    endpoint: str,
    response: requests.Response,
    body: Any,
) -> binance_safety.BinanceAPIError:
    api_code = binance_safety.parse_api_code(body, str(body))
    retry_after = binance_safety.parse_retry_after(response.headers)
    return binance_safety.BinanceAPIError(
        "Binance HTTP error request_id=%s method=%s endpoint=%s status=%s "
        "api_code=%s retry_after=%s body=%s"
        % (request_id, method, endpoint, response.status_code, api_code,
           retry_after, binance_safety.redact_body(body)),
        request_id=request_id,
        method=method,
        endpoint=endpoint,
        status_code=response.status_code,
        api_code=api_code,
        headers=response.headers,
        body=body,
        retry_after=retry_after,
    )


def _get(path: str, params: Optional[Mapping[str, Any]] = None, tries: int = 3):
    """GET JSON with bounded retries for network/5xx errors only.

    A rate-limit response immediately opens the circuit.  It is never retried
    here and the resulting ``BinanceRateLimitError`` is intentionally fatal to
    the bot's request loop.
    """
    tries = max(1, int(tries))
    last: Optional[BaseException] = None
    method = "GET"
    for attempt in range(tries):
        request_id = binance_safety.new_request_id()
        binance_safety.ensure_allowed()
        # Weight estimates are conservative for the endpoints used here.
        weight = 40 if path == "/fapi/v1/ticker/24hr" and not params else 2
        if path == "/fapi/v2/ticker/price" and not params:
            weight = 2
        binance_safety.acquire(path, weight=weight)
        started = time.monotonic()
        try:
            response = SESSION.get(
                BASE + path,
                params=dict(params or {}),
                timeout=TIMEOUT,
            )
            body = _json_or_text(response)
            binance_safety.observe_headers(response.headers)
            used_weight = binance_safety.GOVERNOR.observed_weight
            _log(
                "BINANCE HTTP request_id=%s method=%s endpoint=%s status=%s "
                "api_code=%s latency_ms=%.1f used_weight=%s retry_after=%s"
                % (
                    request_id,
                    method,
                    path,
                    response.status_code,
                    binance_safety.parse_api_code(body, str(body)),
                    (time.monotonic() - started) * 1000,
                    used_weight,
                    binance_safety.parse_retry_after(response.headers),
                )
            )
            if response.status_code in (429, 418):
                error = _request_error(
                    request_id=request_id,
                    method=method,
                    endpoint=path,
                    response=response,
                    body=body,
                )
                fatal = binance_safety.trip_for_exception(
                    error, endpoint=path, request_id=request_id
                )
                # trip_for_exception always returns for 429/418.  Keep the
                # fallback for defensive completeness.
                if fatal:
                    raise fatal from error
                raise error
            if response.status_code >= 500:
                error = _request_error(
                    request_id=request_id,
                    method=method,
                    endpoint=path,
                    response=response,
                    body=body,
                )
                if attempt + 1 < tries:
                    last = error
                    time.sleep(min(2.0, 0.5 * (2 ** attempt)))
                    continue
                raise error
            if response.status_code >= 400:
                error = _request_error(
                    request_id=request_id,
                    method=method,
                    endpoint=path,
                    response=response,
                    body=body,
                )
                # Binance can communicate the weight ban as API code -1003;
                # classify it even if a proxy returned a non-standard status.
                fatal = binance_safety.trip_for_exception(
                    error, endpoint=path, request_id=request_id
                )
                if fatal:
                    raise fatal from error
                raise error
            if isinstance(body, Mapping) and body.get("code") == -1003:
                error = _request_error(
                    request_id=request_id,
                    method=method,
                    endpoint=path,
                    response=response,
                    body=body,
                )
                fatal = binance_safety.trip_for_exception(
                    error, endpoint=path, request_id=request_id
                )
                if fatal:
                    raise fatal from error
                raise error
            return body
        except binance_safety.BinanceSafetyStop:
            # Never turn a rate-limit/circuit signal into a retry.
            raise
        except requests.RequestException as exc:
            last = exc
            _log(
                "BINANCE HTTP request_id=%s method=%s endpoint=%s "
                "network_error=%s latency_ms=%.1f attempt=%d/%d"
                % (request_id, method, path, binance_safety.redact_body(exc),
                   (time.monotonic() - started) * 1000, attempt + 1, tries)
            )
            if attempt + 1 < tries:
                # Network errors are not evidence of a Binance rate limit, but
                # still use exponential backoff so a network outage cannot
                # become a tight retry loop.
                time.sleep(min(2.0, 0.5 * (2 ** attempt)))
        except binance_safety.BinanceAPIError:
            # A normal 4xx/5xx is returned to the caller; do not blindly retry
            # malformed requests.  5xx can be retried only if represented by
            # requests' HTTP path below; this branch intentionally remains
            # conservative for trading safety.
            raise
        except Exception as exc:
            last = exc
            _log(
                "BINANCE HTTP request_id=%s method=%s endpoint=%s "
                "unexpected_error=%s attempt=%d/%d"
                % (request_id, method, path, binance_safety.redact_body(exc),
                   attempt + 1, tries)
            )
            if attempt + 1 < tries:
                time.sleep(min(2.0, 0.5 * (2 ** attempt)))
    if last is None:
        last = RuntimeError("Binance request failed without an exception")
    raise last


def get_klines(symbol: str, interval: str = "5m", limit: int = 100):
    """Return oldest-first candles: [{ts,o,h,l,c}]."""
    rows = _get(
        "/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    out = []
    for x in rows:  # Binance returns oldest-first rows.
        out.append({
            "ts": int(x[0]),
            "o": float(x[1]),
            "h": float(x[2]),
            "l": float(x[3]),
            "c": float(x[4]),
        })
    return out


def get_ticker_24h():
    """All USDT-M futures 24h tickers (universe builder only).

    Binance charges weight 40 when ``symbol`` is omitted.  This endpoint is
    intentionally not used by the live price fallback.
    """
    return _get("/fapi/v1/ticker/24hr")


def get_symbol_prices():
    """Return latest prices for all symbols using the low-weight endpoint."""
    return _get("/fapi/v2/ticker/price")


def get_exchange_info():
    """Full exchange info (symbols, filters). Cached by callers."""
    return _get("/fapi/v1/exchangeInfo")


def close() -> None:
    """Close the shared session when the bot shuts down."""
    SESSION.close()


__all__ = [
    "BASE",
    "SESSION",
    "TIMEOUT",
    "close",
    "configure",
    "get_exchange_info",
    "get_klines",
    "get_symbol_prices",
    "get_ticker_24h",
]
