"""OKX OAuth (Bearer) option: an alternative identity, never a change to API-key signing.

What this pins down:

1. **API-key adapters are untouched.** Same host, same HMAC headers, no
   ``Authorization`` header.
2. **OAuth adapters send only the Bearer header** to the OAuth host from OKX's
   broker docs, with no ``OK-ACCESS-*`` signing headers.
3. **The token is read on every request**, so a rotated token (OKX refresh
   invalidates the old access token at once) is picked up without rebuilding.
4. **Fail closed**: no empty ``Bearer``, no key+token mix, no demo header, no
   plain-http host.

Tokens below are stand-ins, not real credentials.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from pycex.constants import OKX_BASE, OKX_OAUTH_BASE
from pycex.exceptions import AuthenticationError
from pycex.exchanges.okx import OKX

BALANCE_OK = {"code": "0", "msg": "", "data": [{"details": [], "totalEq": "0"}]}
ORDER_OK = {"code": "0", "msg": "", "data": [{"ordId": "1", "clOrdId": "", "sCode": "0", "sMsg": ""}]}


async def _balance(ex: OKX, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=BALANCE_OK)
    try:
        await ex.fetch_balance()
    finally:
        await ex.close()
    return httpx_mock.get_requests()[-1]


async def test_api_key_adapter_unchanged(httpx_mock: HTTPXMock) -> None:
    ex = OKX(api_key="k", secret="s", passphrase="p")
    assert ex.auth_mode == "api_key"
    req = await _balance(ex, httpx_mock)
    assert str(req.url).startswith(OKX_BASE + "/")
    assert req.headers["OK-ACCESS-KEY"] == "k"
    assert "authorization" not in req.headers


async def test_oauth_adapter_sends_bearer_only(httpx_mock: HTTPXMock) -> None:
    ex = OKX(oauth_token=lambda: "tok-1")
    assert ex.auth_mode == "oauth"
    req = await _balance(ex, httpx_mock)
    assert str(req.url).startswith(OKX_OAUTH_BASE + "/api/v5/account/balance")
    assert req.headers["authorization"] == "Bearer tok-1"
    for name in (
        "OK-ACCESS-KEY",
        "OK-ACCESS-SIGN",
        "OK-ACCESS-TIMESTAMP",
        "OK-ACCESS-PASSPHRASE",
        "x-simulated-trading",
    ):
        assert name not in req.headers


async def test_oauth_token_is_read_per_request(httpx_mock: HTTPXMock) -> None:
    tokens = iter(["tok-old", "tok-new"])
    ex = OKX(oauth_token=lambda: next(tokens))
    httpx_mock.add_response(json=BALANCE_OK)
    httpx_mock.add_response(json=BALANCE_OK)
    try:
        await ex.fetch_balance()
        await ex.fetch_balance()
    finally:
        await ex.close()
    sent = [r.headers["authorization"] for r in httpx_mock.get_requests()]
    assert sent == ["Bearer tok-old", "Bearer tok-new"]


async def test_oauth_order_body_matches_api_key_order_body(httpx_mock: HTTPXMock) -> None:
    """Only the identity headers differ; the order bytes are the same."""
    httpx_mock.add_response(json=ORDER_OK)
    httpx_mock.add_response(json=ORDER_OK)
    a = OKX(api_key="k", secret="s", passphrase="p", market_type="linear", td_mode="isolated")
    b = OKX(oauth_token=lambda: "tok", market_type="linear", td_mode="isolated")
    try:
        await a.create_order("BTC/USDT:USDT", "buy", "market", 1)
        await b.create_order("BTC/USDT:USDT", "buy", "market", 1)
    finally:
        await a.close()
        await b.close()
    key_req, oauth_req = httpx_mock.get_requests()
    assert key_req.content == oauth_req.content
    assert key_req.url.path == oauth_req.url.path == "/api/v5/trade/order"
    assert oauth_req.headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
async def test_empty_token_raises_before_sending(httpx_mock: HTTPXMock, bad) -> None:
    ex = OKX(oauth_token=lambda: bad)
    try:
        with pytest.raises(AuthenticationError):
            await ex.fetch_balance()
    finally:
        await ex.close()
    assert httpx_mock.get_requests() == []


def test_key_and_token_together_rejected() -> None:
    with pytest.raises(ValueError):
        OKX(api_key="k", secret="s", passphrase="p", oauth_token=lambda: "t")


def test_non_callable_token_rejected() -> None:
    with pytest.raises(TypeError):
        OKX(oauth_token="tok")  # type: ignore[arg-type]


@pytest.mark.parametrize("kw", [{"sandbox": True}, {"demo": True}])
def test_oauth_with_demo_rejected(kw) -> None:
    with pytest.raises(ValueError):
        OKX(oauth_token=lambda: "t", **kw)


def test_plain_http_oauth_host_rejected() -> None:
    with pytest.raises(ValueError):
        OKX(oauth_token=lambda: "t", oauth_base_url="http://openapi.okx.com")


async def test_public_calls_on_oauth_adapter_carry_no_token(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [{"instId": "BTC-USDT", "last": "1", "bidPx": "1", "askPx": "1", "ts": "1"}],
        }
    )
    ex = OKX(oauth_token=lambda: "tok")
    try:
        await ex.fetch_ticker("BTC/USDT")
    finally:
        await ex.close()
    assert "authorization" not in httpx_mock.get_requests()[0].headers
