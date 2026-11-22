"""OKX OAuth (Bearer) option + per-instance ``broker_code`` coexistence.

What this pins down:

1. **API-key adapters are untouched.** Same host, same HMAC headers, no
   ``Authorization`` header.
2. **OAuth adapters send only the Bearer header** to the OAuth host from OKX's
   broker docs (or a validated custom ``https://`` base URL), with zero
   ``OK-ACCESS-*`` signing headers.
3. **The token is read on every request**, so a rotated token (OKX refresh
   invalidates the old access token at once) is picked up without rebuilding —
   even between a spot ``acctLv`` config probe and the order that follows.
4. **Fail closed**: non-callable token, key+token mix, demo/sandbox, invalid
   ``oauth_base_url`` (non-https, missing host, userinfo, query, fragment),
   empty/whitespace/control-char/non-ASCII token.
5. **``broker_code`` coexistence**: OAuth adapters keep ``broker-id`` header
   and order body ``tag`` on both spot (post-``acctLv`` probe) and linear
   ``create_order``, omit ``tag`` on ``cancel_order``, and coexist in the same
   process alongside API-key adapters with distinct broker codes without
   cross-contamination.

Responses reuse the real recorded OKX V5 wire fixtures in
``tests/fixtures/okx/live_20260910.json`` and ``markets_spot.json``; tokens
and broker codes below are stand-ins, not real credentials.
"""

from __future__ import annotations

import json as json_lib
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.constants import OKX_BASE, OKX_OAUTH_BASE
from pycex.exceptions import AuthenticationError, SettlementPendingError
from pycex.exchanges.okx import OKX
from tests.conftest import load_fixture

_LIVE_PAIRS: dict[str, Any] = load_fixture("okx", "live_20260910")["pairs"]
_MARKETS_SPOT_OK: dict[str, Any] = load_fixture("okx", "markets_spot")

_ACCT_CONFIG_LV3: dict[str, Any] = _LIVE_PAIRS["account_config_acctlv3"]["response"]
_SPOT_LIMIT_OK: dict[str, Any] = _LIVE_PAIRS["spot_limit_ok"]["response"]
_SWAP_ENTRY_OK: dict[str, Any] = _LIVE_PAIRS["swap_entry_ok"]["response"]
_CANCEL_OK: dict[str, Any] = _LIVE_PAIRS["cancel_ok"]["response"]
_SPOT_SELL_51008: dict[str, Any] = _LIVE_PAIRS["settlement_pending_51008_spot_sell"]["response"]

_BALANCE_OK: dict[str, Any] = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "adjEq": "",
            "details": [
                {
                    "availBal": "100.5",
                    "cashBal": "100.5",
                    "ccy": "USDT",
                    "eq": "100.5",
                    "frozenBal": "0",
                }
            ],
            "totalEq": "100.5",
            "uTime": "1788998855547",
        }
    ],
}

_OKX_API_KEY_HEADERS = (
    "OK-ACCESS-KEY",
    "OK-ACCESS-SIGN",
    "OK-ACCESS-TIMESTAMP",
    "OK-ACCESS-PASSPHRASE",
)


def _assert_no_api_key_headers(req: httpx.Request) -> None:
    for name in (*_OKX_API_KEY_HEADERS, "x-simulated-trading"):
        assert name not in req.headers


async def _balance(ex: OKX, httpx_mock: HTTPXMock) -> httpx.Request:
    httpx_mock.add_response(json=_BALANCE_OK)
    try:
        await ex.fetch_balance()
    finally:
        await ex.close()
    return httpx_mock.get_requests()[-1]


# ── API-key baseline & read-only auth_mode ──


async def test_api_key_adapter_unchanged(httpx_mock: HTTPXMock) -> None:
    ex = OKX(api_key="k", secret="s", passphrase="p")
    assert ex.auth_mode == "api_key"
    req = await _balance(ex, httpx_mock)
    assert str(req.url).startswith(OKX_BASE + "/api/v5/account/balance")
    assert req.headers["OK-ACCESS-KEY"] == "k"
    assert "authorization" not in req.headers


def test_auth_mode_is_read_only() -> None:
    api_ex = OKX(api_key="k", secret="s", passphrase="p")
    oauth_ex = OKX(oauth_token=lambda: "tok-1")
    assert api_ex.auth_mode == "api_key"
    assert oauth_ex.auth_mode == "oauth"
    with pytest.raises(AttributeError):
        api_ex.auth_mode = "oauth"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        oauth_ex.auth_mode = "api_key"  # type: ignore[misc]


# ── Constructor fail-closed validations ──


@pytest.mark.parametrize("bad", ["tok", 123, ["tok"], object()])
def test_non_callable_token_rejected(bad: Any) -> None:
    with pytest.raises(TypeError):
        OKX(oauth_token=bad)


@pytest.mark.parametrize(
    "creds",
    [
        {"api_key": "k"},
        {"secret": "s"},
        {"passphrase": "p"},
        {"api_key": "k", "secret": "s", "passphrase": "p"},
    ],
)
def test_key_and_token_together_rejected(creds: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        OKX(oauth_token=lambda: "t", **creds)


def test_oauth_with_sandbox_rejected() -> None:
    with pytest.raises(ValueError):
        OKX(oauth_token=lambda: "t", sandbox=True)


@pytest.mark.parametrize("kw", [{"demo": True}, {"sandbox": True, "demo": False}])
def test_oauth_with_demo_rejected(kw: dict[str, Any]) -> None:
    with pytest.deprecated_call(), pytest.raises(ValueError):
        OKX(oauth_token=lambda: "t", **kw)


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://openapi.okx.com",
        "ftp://openapi.okx.com",
        "https://",
        "https:///api/v5",
        "https://:443",
        "https://user:pass@openapi.okx.com",
        "https://user@openapi.okx.com",
        "https://@openapi.okx.com",
        "https://:pass@openapi.okx.com",
        "https://openapi.okx.com?foo=bar",
        "https://openapi.okx.com?",
        "https://openapi.okx.com#section",
        "https://openapi.okx.com#",
        "https://openapi.okx.com:notaport",
        "https://openapi.okx.com\r\n",
        " https://openapi.okx.com",
        "https://openapi.okx.com\\@evil.example",
        "",
        123,
    ],
)
def test_invalid_oauth_base_url_rejected(bad_url: Any) -> None:
    with pytest.raises(ValueError):
        OKX(oauth_token=lambda: "t", oauth_base_url=bad_url)


# ── OAuth headers, host, and public endpoints ──


async def test_oauth_adapter_sends_bearer_only(httpx_mock: HTTPXMock) -> None:
    ex = OKX(oauth_token=lambda: "tok-1")
    assert ex.auth_mode == "oauth"
    req = await _balance(ex, httpx_mock)
    assert str(req.url) == f"{OKX_OAUTH_BASE}/api/v5/account/balance"
    assert req.headers["authorization"] == "Bearer tok-1"
    _assert_no_api_key_headers(req)


async def test_oauth_custom_https_base_url(httpx_mock: HTTPXMock) -> None:
    ex = OKX(oauth_token=lambda: "tok-custom", oauth_base_url="https://proxy.example.com/okx/")
    req = await _balance(ex, httpx_mock)
    assert str(req.url) == "https://proxy.example.com/okx/api/v5/account/balance"
    assert req.headers["authorization"] == "Bearer tok-custom"
    _assert_no_api_key_headers(req)


async def test_public_calls_on_oauth_adapter_carry_no_token(httpx_mock: HTTPXMock) -> None:
    calls = 0

    def provider() -> str:
        nonlocal calls
        calls += 1
        return "tok-unused"

    httpx_mock.add_response(json=_MARKETS_SPOT_OK)
    ex = OKX(oauth_token=provider)
    try:
        markets = await ex.fetch_markets()
    finally:
        await ex.close()
    assert len(markets) >= 1
    req = httpx_mock.get_requests()[0]
    assert str(req.url).startswith(f"{OKX_OAUTH_BASE}/api/v5/public/instruments")
    assert "authorization" not in req.headers
    _assert_no_api_key_headers(req)
    assert calls == 0


# ── Per-request token rotation & malformed token rejection ──


async def test_oauth_token_is_read_per_request(httpx_mock: HTTPXMock) -> None:
    tokens = iter(["tok-1", "tok-2"])
    ex = OKX(oauth_token=lambda: next(tokens))
    httpx_mock.add_response(json=_BALANCE_OK)
    httpx_mock.add_response(json=_BALANCE_OK)
    try:
        await ex.fetch_balance()
        await ex.fetch_balance()
    finally:
        await ex.close()
    sent = [r.headers["authorization"] for r in httpx_mock.get_requests()]
    assert sent == ["Bearer tok-1", "Bearer tok-2"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "\t",
        None,
        123,
        b"tok-bytes",
        " tok-leading",
        "tok-trailing ",
        "tok with space",
        "tok\n",
        "tok\r\nX-Injected: 1",
        "tok\x00null",
        "tok\x1fctrl",
        "tok\x7fdel",
        "tok한글",
    ],
)
async def test_bad_token_raises_before_sending(httpx_mock: HTTPXMock, bad: Any) -> None:
    ex = OKX(oauth_token=lambda: bad)
    try:
        with pytest.raises(AuthenticationError):
            await ex.fetch_balance()
    finally:
        await ex.close()
    assert httpx_mock.get_requests() == []


# ── Point-specific failure injection & rotation inside multi-step spot order flow ──


async def test_oauth_token_rotates_between_acct_lv_probe_and_spot_order(httpx_mock: HTTPXMock) -> None:
    """Spot create_order makes two authenticated requests; rotation applies between them."""
    tokens = iter(["tok-1", "tok-2"])
    httpx_mock.add_response(json=_ACCT_CONFIG_LV3)
    httpx_mock.add_response(json=_SPOT_LIMIT_OK)
    ex = OKX(oauth_token=lambda: next(tokens), broker_code="oauthCode1")
    try:
        order = await ex.create_order("BTC/USDT", "buy", "limit", 0.00012834, 77912.1)
    finally:
        await ex.close()
    assert order.id == "3908989482144419840"
    cfg_req, ord_req = httpx_mock.get_requests()
    assert cfg_req.url.path == "/api/v5/account/config"
    assert ord_req.url.path == "/api/v5/trade/order"
    assert cfg_req.headers["authorization"] == "Bearer tok-1"
    assert ord_req.headers["authorization"] == "Bearer tok-2"


@pytest.mark.parametrize("second_token", ["", "tok-2\r\nX-Evil: 1", None])
async def test_point_failure_bad_token_after_acct_lv_probe_blocks_spot_order(
    httpx_mock: HTTPXMock, second_token: Any
) -> None:
    """Failure injected specifically at step 2 (after acctLv probe succeeds): 0 order POSTs."""
    tokens = iter(["tok-1", second_token])
    httpx_mock.add_response(json=_ACCT_CONFIG_LV3)
    ex = OKX(oauth_token=lambda: next(tokens), broker_code="oauthCode1")
    try:
        with pytest.raises(AuthenticationError):
            await ex.create_order("BTC/USDT", "buy", "limit", 0.00012834, 77912.1)
    finally:
        await ex.close()
    assert ex.account_level == "3"
    sent = httpx_mock.get_requests()
    assert [r.url.path for r in sent] == ["/api/v5/account/config"]


async def test_point_failure_acct_config_auth_error_blocks_spot_order(httpx_mock: HTTPXMock) -> None:
    """Failure injected at step 1 (acctLv probe returns OKX auth error): 0 order POSTs."""
    httpx_mock.add_response(status_code=401, json={"code": "50111", "msg": "Invalid authorization", "data": []})
    ex = OKX(oauth_token=lambda: "tok-expired", broker_code="oauthCode1")
    try:
        with pytest.raises(AuthenticationError):
            await ex.create_order("BTC/USDT", "buy", "limit", 0.00012834, 77912.1)
    finally:
        await ex.close()
    assert ex.account_level is None
    assert [r.url.path for r in httpx_mock.get_requests()] == ["/api/v5/account/config"]


async def test_oauth_order_rejection_preserves_recorded_error_mapping(httpx_mock: HTTPXMock) -> None:
    """Real recorded 51008 settlement-pending rejection maps identically on the OAuth path."""
    httpx_mock.add_response(json=_ACCT_CONFIG_LV3)
    httpx_mock.add_response(json=_SPOT_SELL_51008)
    ex = OKX(oauth_token=lambda: "tok-1", broker_code="oauthCode1")
    try:
        with pytest.raises(SettlementPendingError) as exc_info:
            await ex.create_order("BTC/USDT", "sell", "market", 0.00012771)
    finally:
        await ex.close()
    assert exc_info.value.code == "51008"
    assert exc_info.value.retryable is True


# ── broker_code coexistence (core) ──


async def test_oauth_spot_create_order_carries_bearer_broker_header_and_tag(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACCT_CONFIG_LV3)
    httpx_mock.add_response(json=_SPOT_LIMIT_OK)
    ex = OKX(oauth_token=lambda: "tok-spot", broker_code="oauthCode1")
    try:
        order = await ex.create_order("BTC/USDT", "buy", "limit", 0.00012834, 77912.1)
    finally:
        await ex.close()
    assert order.id == "3908989482144419840"
    assert ex.account_level == "3"

    cfg_req, ord_req = httpx_mock.get_requests()
    assert str(cfg_req.url) == f"{OKX_OAUTH_BASE}/api/v5/account/config"
    assert cfg_req.headers["authorization"] == "Bearer tok-spot"
    assert cfg_req.headers["broker-id"] == "oauthCode1"
    _assert_no_api_key_headers(cfg_req)

    assert str(ord_req.url) == f"{OKX_OAUTH_BASE}/api/v5/trade/order"
    assert ord_req.headers["authorization"] == "Bearer tok-spot"
    assert ord_req.headers["broker-id"] == "oauthCode1"
    _assert_no_api_key_headers(ord_req)
    body = json_lib.loads(ord_req.content.decode())
    assert body["tdMode"] == "cross"
    assert body["tag"] == "oauthCode1"


async def test_oauth_linear_create_order_carries_bearer_broker_header_and_tag(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_SWAP_ENTRY_OK)
    ex = OKX(
        oauth_token=lambda: "tok-swap",
        market_type="linear",
        td_mode="isolated",
        broker_code="oauthCode1",
    )
    try:
        order = await ex.create_order("ETH/USDT:USDT", "buy", "market", 0.04)
    finally:
        await ex.close()
    assert order.id == "3908972901020520448"
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 1
    req = reqs[0]
    assert str(req.url) == f"{OKX_OAUTH_BASE}/api/v5/trade/order"
    assert req.headers["authorization"] == "Bearer tok-swap"
    assert req.headers["broker-id"] == "oauthCode1"
    _assert_no_api_key_headers(req)
    body = json_lib.loads(req.content.decode())
    assert body["instId"] == "ETH-USDT-SWAP"
    assert body["tdMode"] == "isolated"
    assert body["tag"] == "oauthCode1"


async def test_oauth_cancel_order_carries_bearer_and_omits_tag_in_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_CANCEL_OK)
    ex = OKX(oauth_token=lambda: "tok-cancel", market_type="linear", broker_code="oauthCode1")
    try:
        order = await ex.cancel_order("3908975091051188224", "ETH/USDT:USDT")
    finally:
        await ex.close()
    assert order.id == "3908975091051188224"
    req = httpx_mock.get_requests()[0]
    assert str(req.url) == f"{OKX_OAUTH_BASE}/api/v5/trade/cancel-order"
    assert req.headers["authorization"] == "Bearer tok-cancel"
    assert req.headers["broker-id"] == "oauthCode1"
    _assert_no_api_key_headers(req)
    body = json_lib.loads(req.content.decode())
    assert body == {"instId": "ETH-USDT-SWAP", "ordId": "3908975091051188224"}
    assert "tag" not in body


async def test_api_key_and_oauth_instances_coexist_with_distinct_broker_codes(httpx_mock: HTTPXMock) -> None:
    """API-key and OAuth adapters in the same process never leak auth mode or broker_code."""
    httpx_mock.add_response(json=_SWAP_ENTRY_OK)
    httpx_mock.add_response(json=_SWAP_ENTRY_OK)
    httpx_mock.add_response(json=_ACCT_CONFIG_LV3)
    httpx_mock.add_response(json=_SPOT_LIMIT_OK)
    httpx_mock.add_response(json=_ACCT_CONFIG_LV3)
    httpx_mock.add_response(json=_SPOT_LIMIT_OK)

    api_linear = OKX(
        api_key="k",
        secret="s",
        passphrase="p",
        market_type="linear",
        td_mode="isolated",
        broker_code="apiCode1",
    )
    oauth_linear = OKX(
        oauth_token=lambda: "tok-oauth-1",
        market_type="linear",
        td_mode="isolated",
        broker_code="oauthCode1",
    )
    api_spot = OKX(api_key="k", secret="s", passphrase="p", broker_code="apiCode1")
    oauth_spot = OKX(oauth_token=lambda: "tok-oauth-2", broker_code="oauthCode1")

    try:
        assert api_linear.auth_mode == "api_key"
        assert oauth_linear.auth_mode == "oauth"
        await api_linear.create_order("ETH/USDT:USDT", "buy", "market", 0.04)
        await oauth_linear.create_order("ETH/USDT:USDT", "buy", "market", 0.04)
        await api_spot.create_order("BTC/USDT", "buy", "limit", 0.00012834, 77912.1)
        await oauth_spot.create_order("BTC/USDT", "buy", "limit", 0.00012834, 77912.1)
    finally:
        await api_linear.close()
        await oauth_linear.close()
        await api_spot.close()
        await oauth_spot.close()

    api_lin_req, oauth_lin_req, api_cfg_req, api_spot_req, oauth_cfg_req, oauth_spot_req = httpx_mock.get_requests()

    # API-key requests: www.okx.com, OK-ACCESS-* present, no Authorization, apiCode1
    for req in (api_lin_req, api_cfg_req, api_spot_req):
        assert str(req.url).startswith(f"{OKX_BASE}/")
        for hdr in _OKX_API_KEY_HEADERS:
            assert hdr in req.headers
        assert "authorization" not in req.headers
        assert req.headers["broker-id"] == "apiCode1"
    assert json_lib.loads(api_lin_req.content.decode())["tag"] == "apiCode1"
    assert json_lib.loads(api_spot_req.content.decode())["tag"] == "apiCode1"

    # OAuth requests: openapi.okx.com, Authorization Bearer, zero OK-ACCESS-*, oauthCode1
    for req, expected_tok in (
        (oauth_lin_req, "Bearer tok-oauth-1"),
        (oauth_cfg_req, "Bearer tok-oauth-2"),
        (oauth_spot_req, "Bearer tok-oauth-2"),
    ):
        assert str(req.url).startswith(f"{OKX_OAUTH_BASE}/")
        assert req.headers["authorization"] == expected_tok
        assert req.headers["broker-id"] == "oauthCode1"
        _assert_no_api_key_headers(req)
    assert json_lib.loads(oauth_lin_req.content.decode())["tag"] == "oauthCode1"
    assert json_lib.loads(oauth_spot_req.content.decode())["tag"] == "oauthCode1"


async def test_oauth_order_body_matches_api_key_order_body(httpx_mock: HTTPXMock) -> None:
    """Only the identity headers and host differ; the serialized order bytes match."""
    httpx_mock.add_response(json=_SWAP_ENTRY_OK)
    httpx_mock.add_response(json=_SWAP_ENTRY_OK)
    a = OKX(api_key="k", secret="s", passphrase="p", market_type="linear", td_mode="isolated", broker_code="sameCode1")
    b = OKX(oauth_token=lambda: "tok", market_type="linear", td_mode="isolated", broker_code="sameCode1")
    try:
        await a.create_order("ETH/USDT:USDT", "buy", "market", 0.04)
        await b.create_order("ETH/USDT:USDT", "buy", "market", 0.04)
    finally:
        await a.close()
        await b.close()
    key_req, oauth_req = httpx_mock.get_requests()
    assert key_req.content == oauth_req.content
    assert key_req.url.path == oauth_req.url.path == "/api/v5/trade/order"
    assert oauth_req.headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("bad_code", ["", "   ", "has space", "X" * 17, "a-b"])
async def test_oauth_malformed_broker_code_sends_no_tag_or_header(httpx_mock: HTTPXMock, bad_code: str) -> None:
    httpx_mock.add_response(json=_SWAP_ENTRY_OK)
    ex = OKX(oauth_token=lambda: "tok-1", market_type="linear", td_mode="isolated", broker_code=bad_code)
    try:
        await ex.create_order("ETH/USDT:USDT", "buy", "market", 0.04)
    finally:
        await ex.close()
    req = httpx_mock.get_requests()[0]
    assert req.headers["authorization"] == "Bearer tok-1"
    assert "broker-id" not in req.headers
    assert "tag" not in json_lib.loads(req.content.decode())
