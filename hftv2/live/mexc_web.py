from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import ssl
import time
import urllib.parse
from pathlib import Path
from typing import Any

from hftv2.types import LiveConfig

BASE_URL = "https://futures.mexc.com/api/v1"
SIDES = {"long": 1, "close_short": 2, "short": 3, "close_long": 4}
TOKEN_ENV = ("MEXC_WEB_TOKEN", "MEXC_AUTH_TOKEN")


def load_dotenv(path: str | Path | None = None) -> None:
    p = Path(path or ".env")
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

# Same defaults as https://github.com/oboshto/mexc-futures-sdk
_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://www.mexc.com",
    "referer": "https://www.mexc.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "x-language": "en-US",
}


class MexcWebError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.body = body


def json_num(x: float) -> int | float:
    xf = float(x)
    if xf.is_integer():
        return int(xf)
    return xf


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def unwrap_contract(resp: Any, symbol: str) -> dict[str, Any]:
    data = resp.get("data") if isinstance(resp, dict) else resp
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("symbol") == symbol:
                return row
        return data[0] if data and isinstance(data[0], dict) else {}
    return data if isinstance(data, dict) else {}


def round_to_unit(price: float, unit: float, scale: int, mode: str = "nearest") -> float:
    if unit <= 0:
        return json_num(price)
    n = price / unit
    if mode == "ceil":
        n = math.ceil(n - 1e-12)
    elif mode == "floor":
        n = math.floor(n + 1e-12)
    else:
        n = round(n)
    rounded = round(n * unit, int(scale) if scale >= 0 else 8)
    return json_num(rounded)


def tpsl_prices(
    entry: float,
    side: str,
    tp_bps: float,
    sl_bps: float,
    unit: float,
    scale: int,
) -> dict[str, float]:
    if entry <= 0 or side not in ("long", "short"):
        return {}
    tp_raw = entry * (tp_bps / 10_000.0)
    sl_raw = entry * (sl_bps / 10_000.0)
    tick = unit if unit > 0 else 10 ** (-max(scale, 0))
    if side == "long":
        tp = round_to_unit(entry + max(tp_raw, tick), unit, scale, "ceil")
        sl = round_to_unit(entry - max(sl_raw, tick), unit, scale, "floor")
        if tp <= entry:
            tp = round_to_unit(entry + tick, unit, scale, "ceil")
        if sl >= entry:
            sl = round_to_unit(entry - tick, unit, scale, "floor")
    else:
        tp = round_to_unit(entry - max(tp_raw, tick), unit, scale, "floor")
        sl = round_to_unit(entry + max(sl_raw, tick), unit, scale, "ceil")
        if tp >= entry:
            tp = round_to_unit(entry - tick, unit, scale, "floor")
        if sl <= entry:
            sl = round_to_unit(entry + tick, unit, scale, "ceil")
    return {"takeProfitPrice": tp, "stopLossPrice": sl}


def mexc_sign(token: str, body: Any, now_ms: str) -> str:
    """MD5 web-client signature used by MEXC futures.mexc.com (SDK-compatible)."""
    g = hashlib.md5((token + now_ms).encode("utf-8")).hexdigest()[7:]
    return hashlib.md5((now_ms + dumps(body) + g).encode("utf-8")).hexdigest()


def load_token() -> str:
    for key in TOKEN_ENV:
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    raise MexcWebError(
        "set MEXC_WEB_TOKEN (browser Authorization header starting with WEB)"
    )


def token_preview(token: str) -> str:
    if len(token) <= 10:
        return "***"
    return token[:6] + "..." + token[-4:]


class MexcWebClient:
    """Keep-alive client: one TLS session reused for every call, so an order
    is a single POST round-trip instead of connect+handshake+request."""

    def __init__(self, live: LiveConfig, token: str | None = None, now_ms: str | None = None) -> None:
        self.live = live
        self.token = token if token is not None else load_token()
        if not self.token.startswith("WEB"):
            raise MexcWebError("token must be the browser Authorization value starting with WEB")
        self._now_ms = now_ms  # tests only
        self.base = live.base_url.rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        self._host = parts.hostname or "futures.mexc.com"
        self._port = parts.port or 443
        self._prefix = parts.path.rstrip("/")
        self._ctx = ssl.create_default_context()
        self._conn: http.client.HTTPSConnection | None = None
        self._contracts: dict[str, dict[str, Any]] = {}
        self.last_rtt_ms: float | None = None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def contract_spec(self, symbol: str) -> dict[str, Any]:
        """Contract tick/vol spec, fetched once per symbol and cached."""
        spec = self._contracts.get(symbol)
        if spec is None:
            spec = unwrap_contract(self.contract_detail(symbol), symbol)
            if spec:
                self._contracts[symbol] = spec
        return spec

    def warmup(self, symbols: list[str] | None = None) -> float:
        """Open the TLS session and prefetch contract specs ahead of the first
        order. Returns elapsed ms."""
        t0 = time.perf_counter()
        if symbols:
            for symbol in symbols:
                self.contract_spec(symbol)
        else:
            self.contract_detail("BTC_USDT")
        return (time.perf_counter() - t0) * 1000.0

    def account_asset(self, currency: str = "USDT") -> dict[str, Any]:
        return self._get(f"/private/account/asset/{urllib.parse.quote(currency)}")

    def open_positions(self, symbol: str | None = None) -> dict[str, Any]:
        path = "/private/position/open_positions"
        if symbol:
            path += "?" + urllib.parse.urlencode({"symbol": symbol})
        return self._get(path)

    def ticker(self, symbol: str) -> dict[str, Any]:
        q = urllib.parse.urlencode({"symbol": symbol})
        return self._get(f"/contract/ticker?{q}", auth=False)

    def contract_detail(self, symbol: str) -> dict[str, Any]:
        q = urllib.parse.urlencode({"symbol": symbol})
        return self._get(f"/contract/detail?{q}", auth=False)

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        vol: float,
        price: float,
        leverage: float | None = None,
        market: bool = True,
        position_id: int | None = None,
        confirm: bool = False,
        attach_tpsl: bool | None = None,
        tp_bps: float | None = None,
        sl_bps: float | None = None,
        contract: dict[str, Any] | None = None,
        fetch_contract: bool = False,
    ) -> dict[str, Any]:
        if side not in SIDES:
            raise MexcWebError(f"side must be one of {sorted(SIDES)}")
        if not symbol or vol <= 0 or price < 0:
            raise MexcWebError("symbol, vol>0 and price>=0 are required")
        spec = contract
        if spec is None and fetch_contract:
            spec = self.contract_spec(symbol)
        spec = spec or {}
        unit = float(spec.get("priceUnit") or 0)
        scale = int(spec.get("priceScale") or 8)
        vol_unit = float(spec.get("volUnit") or 0)
        min_vol = float(spec.get("minVol") or 0)
        if unit > 0:
            price = float(round_to_unit(price, unit, scale, "nearest"))
        if vol_unit > 0:
            vol = float(round_to_unit(vol, vol_unit, int(spec.get("volScale") or 0), "nearest"))
        if min_vol > 0 and vol < min_vol:
            raise MexcWebError(f"vol {vol} < minVol {min_vol} for {symbol}")
        body: dict[str, Any] = {
            "symbol": symbol,
            "price": json_num(price),
            "vol": json_num(vol),
            "side": SIDES[side],
            "type": 5 if market else 1,
            "openType": int(self.live.open_type),
            "leverage": int(leverage if leverage is not None else self.live.leverage),
        }
        if position_id is not None:
            body["positionId"] = int(position_id)
        use_tpsl = self.live.attach_tpsl if attach_tpsl is None else attach_tpsl
        if use_tpsl and side in ("long", "short"):
            levels = tpsl_prices(
                price,
                side,
                float(self.live.tp_bps if tp_bps is None else tp_bps),
                float(self.live.sl_bps if sl_bps is None else sl_bps),
                unit,
                scale,
            )
            body.update({k: json_num(v) for k, v in levels.items()})
        if self.live.dry_run or not self.live.enabled:
            notional = None
            csz = spec.get("contractSize")
            if csz:
                notional = round(float(csz) * vol * price, 6)
            return {
                "success": True,
                "dry_run": True,
                "sent": False,
                "reason": "dry_run" if self.live.dry_run else "live.disabled",
                "body": body,
                "notional_usd": notional,
            }
        if not confirm:
            raise MexcWebError("refusing live order without confirm=True")
        return self._post("/private/order/submit", body)

    def cancel_order(self, order_ids: list[str], *, confirm: bool = False) -> dict[str, Any]:
        ids = [str(x) for x in order_ids]
        if not ids:
            raise MexcWebError("order_ids empty")
        if self.live.dry_run or not self.live.enabled:
            return {
                "success": True,
                "dry_run": True,
                "sent": False,
                "reason": "dry_run" if self.live.dry_run else "live.disabled",
                "body": ids,
            }
        if not confirm:
            raise MexcWebError("refusing live cancel without confirm=True")
        return self._post("/private/order/cancel", ids)

    def _get(self, path: str, auth: bool = True) -> dict[str, Any]:
        headers = dict(_HEADERS)
        if auth:
            headers["authorization"] = self.token
        return self._open("GET", path, headers, None)

    def _post(self, path: str, body: Any) -> dict[str, Any]:
        now_ms = self._now_ms or str(int(time.time() * 1000))
        headers = dict(_HEADERS)
        headers["authorization"] = self.token
        headers["x-mxc-nonce"] = now_ms
        headers["x-mxc-sign"] = mexc_sign(self.token, body, now_ms)
        return self._open("POST", path, headers, dumps(body).encode("utf-8"))

    def _connect(self) -> http.client.HTTPSConnection:
        conn = http.client.HTTPSConnection(
            self._host, self._port, timeout=self.live.timeout_sec, context=self._ctx
        )
        conn.connect()
        return conn

    def _req_path(self, path: str) -> str:
        if path.startswith("http"):
            parts = urllib.parse.urlsplit(path)
            return parts.path + (f"?{parts.query}" if parts.query else "")
        return self._prefix + path

    _RETRY_ERRORS = (
        http.client.NotConnected,
        http.client.CannotSendRequest,
        http.client.BadStatusLine,
        http.client.RemoteDisconnected,
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
        ssl.SSLError,
        OSError,
    )

    def _open(self, method: str, path: str, headers: dict[str, str], data: bytes | None) -> dict[str, Any]:
        req_path = self._req_path(path)
        t0 = time.perf_counter()
        raw = b""
        status = 0
        for attempt in (0, 1):
            conn = self._conn
            if conn is None:
                conn = self._connect()
                self._conn = conn
            # Send phase: a failure here means the server never got a complete
            # request, so retrying once on a fresh connection is safe (also for
            # orders). Timeouts are NOT retried: the request may have landed.
            try:
                conn.request(method, req_path, body=data, headers=headers)
            except TimeoutError as exc:
                self.close()
                raise MexcWebError(f"timeout after {self.live.timeout_sec}s (send)") from exc
            except self._RETRY_ERRORS as exc:
                self.close()
                if attempt == 1:
                    raise MexcWebError(f"network (send): {exc}") from exc
                continue
            # Response phase: the request was fully delivered. Never retry a
            # POST here or an order could be submitted twice.
            try:
                resp = conn.getresponse()
                status = resp.status
                raw = resp.read()
                break
            except TimeoutError as exc:
                self.close()
                raise MexcWebError(f"timeout after {self.live.timeout_sec}s (response)") from exc
            except self._RETRY_ERRORS as exc:
                self.close()
                if method != "GET" or attempt == 1:
                    raise MexcWebError(f"network (response): {exc}") from exc
                continue
        self.last_rtt_ms = (time.perf_counter() - t0) * 1000.0
        text = raw.decode("utf-8", errors="replace")
        if status >= 400:
            raise MexcWebError(f"HTTP {status}: {text[:400]}", code=status)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MexcWebError(f"non-json response: {text[:200]}") from exc
        if isinstance(parsed, dict) and parsed.get("success") is False:
            raise MexcWebError(
                str(parsed.get("message") or parsed.get("code") or parsed),
                code=parsed.get("code"),
                body=parsed,
            )
        return parsed if isinstance(parsed, dict) else {"data": parsed}
