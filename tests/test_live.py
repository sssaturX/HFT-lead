from hftv2.config import load_config
from hftv2.live.mexc_web import MexcWebClient, MexcWebError, dumps, json_num, mexc_sign, tpsl_prices
from hftv2.types import LiveConfig


def test_sign_matches_md5_scheme() -> None:
    token = "WEB_TEST"
    now = "1700000000000"
    body = {
        "symbol": "BTC_USDT",
        "price": 50000,
        "vol": 0.001,
        "side": 1,
        "type": 5,
        "openType": 1,
        "leverage": 10,
    }
    sign = mexc_sign(token, body, now)
    assert sign == "3f1f7728619556263e28bb7b535fd34d"
    assert " " not in dumps(body)


def test_json_num_matches_js_stringify() -> None:
    assert dumps({"vol": json_num(1.0)}) == '{"vol":1}'
    assert dumps({"vol": json_num(0.001)}) == '{"vol":0.001}'


def test_tpsl_long_short_ticks() -> None:
    long_lv = tpsl_prices(100.0, "long", 3.0, 100.0, 0.01, 2)
    assert long_lv["takeProfitPrice"] == 100.03
    assert long_lv["stopLossPrice"] == 99
    short_lv = tpsl_prices(100.0, "short", 3.0, 100.0, 0.01, 2)
    assert short_lv["takeProfitPrice"] == 99.97
    assert short_lv["stopLossPrice"] == 101
    assert tpsl_prices(100.0, "close_long", 3.0, 100.0, 0.01, 2) == {}


def test_dry_run_attaches_tpsl() -> None:
    live = LiveConfig(enabled=False, dry_run=True, tp_bps=3.0, sl_bps=100.0)
    client = MexcWebClient(live, token="WEB_FAKE")
    spec = {"priceUnit": 0.01, "priceScale": 2, "volUnit": 1, "minVol": 1, "contractSize": 0.001}
    out = client.submit_order(
        symbol="SNDKSTOCK_USDT",
        side="long",
        vol=1,
        price=100,
        market=True,
        confirm=True,
        contract=spec,
    )
    assert out["sent"] is False
    assert out["body"]["takeProfitPrice"] == 100.03
    assert out["body"]["stopLossPrice"] == 99
    assert out["notional_usd"] == 0.1


def test_close_skips_tpsl() -> None:
    live = LiveConfig(enabled=False, dry_run=True)
    client = MexcWebClient(live, token="WEB_FAKE")
    out = client.submit_order(
        symbol="SNDKSTOCK_USDT",
        side="close_long",
        vol=1,
        price=100,
        contract={"priceUnit": 0.01, "priceScale": 2, "volUnit": 1, "minVol": 1},
    )
    assert "takeProfitPrice" not in out["body"]
    assert out["body"]["side"] == 4


def test_enabled_but_dry_run_still_blocks() -> None:
    live = LiveConfig(enabled=True, dry_run=True)
    client = MexcWebClient(live, token="WEB_FAKE")
    out = client.submit_order(
        symbol="SNDKSTOCK_USDT", side="short", vol=1, price=100, confirm=True
    )
    assert out["sent"] is False
    assert out["reason"] == "dry_run"


def test_live_without_confirm_raises() -> None:
    live = LiveConfig(enabled=True, dry_run=False)
    client = MexcWebClient(live, token="WEB_FAKE")

    def boom(*_a, **_k):
        raise AssertionError("must not HTTP")

    client._open = boom  # type: ignore[method-assign]
    try:
        client.submit_order(
            symbol="SNDKSTOCK_USDT", side="long", vol=1, price=100, confirm=False
        )
        raise AssertionError("expected MexcWebError")
    except MexcWebError as exc:
        assert "confirm" in str(exc)


def test_rejects_non_web_token() -> None:
    try:
        MexcWebClient(LiveConfig(), token="not-a-web-token")
        raise AssertionError("expected MexcWebError")
    except MexcWebError as exc:
        assert "WEB" in str(exc)


def test_config_live_defaults_off() -> None:
    cfg = load_config("config.yaml")
    assert cfg.live.enabled is False
    assert cfg.live.dry_run is True
    assert cfg.live.tp_bps == 3.0
    assert cfg.live.sl_bps == 100.0
    assert cfg.live.attach_tpsl is True


class _FakeResp:
    def __init__(self, payload: bytes = b'{"success":true,"data":1}') -> None:
        self.status = 200
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _FakeConn:
    def __init__(self, fail_send: bool = False, fail_resp: bool = False) -> None:
        self.fail_send = fail_send
        self.fail_resp = fail_resp

    def request(self, *args, **kwargs) -> None:
        if self.fail_send:
            raise ConnectionResetError("send boom")

    def getresponse(self) -> _FakeResp:
        if self.fail_resp:
            raise ConnectionResetError("resp boom")
        return _FakeResp()

    def close(self) -> None:
        pass


def _patched_client(conns: list[_FakeConn]) -> tuple[MexcWebClient, list[int]]:
    client = MexcWebClient(LiveConfig(), token="WEB_FAKE")
    connects: list[int] = []

    def fake_connect():
        connects.append(1)
        return conns.pop(0)

    client._connect = fake_connect  # type: ignore[method-assign]
    return client, connects


def test_send_failure_retries_once() -> None:
    client, connects = _patched_client([_FakeConn(fail_send=True), _FakeConn()])
    out = client._open("POST", "/x", {}, b"{}")
    assert out["success"] is True
    assert len(connects) == 2


def test_post_response_failure_never_retries() -> None:
    client, connects = _patched_client([_FakeConn(fail_resp=True), _FakeConn()])
    try:
        client._open("POST", "/x", {}, b"{}")
        raise AssertionError("expected MexcWebError")
    except MexcWebError as exc:
        assert "response" in str(exc)
    assert len(connects) == 1


def test_get_response_failure_retries() -> None:
    client, connects = _patched_client([_FakeConn(fail_resp=True), _FakeConn()])
    out = client._open("GET", "/x", {}, None)
    assert out["success"] is True
    assert len(connects) == 2


def test_close_position_resolves_side_and_id() -> None:
    client = MexcWebClient(LiveConfig(enabled=False, dry_run=True), token="WEB_FAKE")

    def fake_open_positions(symbol=None):
        return {"data": [{"symbol": "SNDKSTOCK_USDT", "positionType": 1,
                          "holdVol": 3, "positionId": 42}]}

    def fake_ticker(symbol):
        return {"data": {"lastPrice": 100.0}}

    client.open_positions = fake_open_positions  # type: ignore[method-assign]
    client.ticker = fake_ticker  # type: ignore[method-assign]
    client.contract_detail = lambda s: {"data": {"symbol": s, "priceUnit": 0.01, "priceScale": 2, "volUnit": 1, "minVol": 1}}  # type: ignore[method-assign]
    out = client.close_position("SNDKSTOCK_USDT", confirm=True)
    assert out["sent"] is False  # dry-run
    assert out["body"]["side"] == 4  # close long
    assert out["body"]["vol"] == 3  # full hold
    assert out["body"]["positionId"] == 42
    assert "takeProfitPrice" not in out["body"]


def test_close_short_and_partial() -> None:
    client = MexcWebClient(LiveConfig(enabled=False, dry_run=True), token="WEB_FAKE")
    client.open_positions = lambda symbol=None: {  # type: ignore[method-assign]
        "data": [{"symbol": "SUI_USDT", "positionType": 2, "holdVol": 10, "positionId": 7}]
    }
    client.ticker = lambda symbol: {"data": {"lastPrice": 1.0}}  # type: ignore[method-assign]
    client.contract_detail = lambda s: {"data": {"symbol": s, "priceUnit": 0.0001, "priceScale": 4, "volUnit": 1, "minVol": 1}}  # type: ignore[method-assign]
    out = client.close_position("SUI_USDT", vol=4, confirm=True)
    assert out["body"]["side"] == 2  # close short
    assert out["body"]["vol"] == 4  # partial


def test_close_no_position_raises() -> None:
    client = MexcWebClient(LiveConfig(), token="WEB_FAKE")
    client.open_positions = lambda symbol=None: {"data": []}  # type: ignore[method-assign]
    try:
        client.close_position("SNDKSTOCK_USDT", confirm=True)
        raise AssertionError("expected MexcWebError")
    except MexcWebError as exc:
        assert "no open position" in str(exc)


def test_contract_spec_cached() -> None:
    client = MexcWebClient(LiveConfig(), token="WEB_FAKE")
    calls = {"n": 0}

    def fake_detail(symbol: str) -> dict:
        calls["n"] += 1
        return {"data": {"symbol": symbol, "priceUnit": 0.01, "priceScale": 2}}

    client.contract_detail = fake_detail  # type: ignore[method-assign]
    first = client.contract_spec("SNDKSTOCK_USDT")
    second = client.contract_spec("SNDKSTOCK_USDT")
    assert first == second
    assert calls["n"] == 1
