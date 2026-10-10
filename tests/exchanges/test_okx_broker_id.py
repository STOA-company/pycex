"""OKX broker code: one configuration source, and nothing on the wire without it.

What this pins down:

1. **Unconfigured is byte-identical.** With no broker code set, the signed
   order body is the exact bytes it was before the hook existed and no
   ``broker-id`` header is sent. Money path: an attribution feature must not
   change a single character of an order that is already working.
2. **Configured adds one key, nothing else.** ``tag`` appears; instrument,
   side, size, price, order type and client id are untouched.
3. **The signature still covers the verbatim body**, recomputed independently —
   otherwise every live order would be rejected the moment the code is set.
4. **One source.** The value comes from the environment variable named by
   ``OKX_BROKER_ID_ENV``; nothing is hard-coded at the call sites.

The value below is a stand-in, not a real broker code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json as json_lib

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.constants import OKX_BROKER_ID_ENV, okx_broker_id
from pycex.exchanges.okx import OKX

SECRET = "s"
FAKE_BROKER_ID = "TESTTAG1"

#: What a linear market order must look like on the wire with no broker code.
UNCONFIGURED_BODY = '{"instId": "BTC-USDT-SWAP", "tdMode": "isolated", "side": "buy", "ordType": "market", "sz": "1"}'


@pytest.fixture(autouse=True)
def _unset_broker_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OKX_BROKER_ID_ENV, raising=False)


def _assert_valid_signature(request: httpx.Request, secret: str = SECRET) -> None:
    ts = request.headers["OK-ACCESS-TIMESTAMP"]
    message = (
        ts + request.method + request.url.raw_path.decode() + (request.content.decode() if request.content else "")
    )
    expected = base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()
    assert request.headers["OK-ACCESS-SIGN"] == expected


async def _place(httpx_mock: HTTPXMock) -> httpx.Request:
    httpx_mock.add_response(json={"code": "0", "msg": "", "data": [{"ordId": "1", "sCode": "0"}]})
    ex = OKX(api_key="k", secret=SECRET, passphrase="p", market_type="linear", td_mode="isolated")
    try:
        await ex.create_order("BTC/USDT:USDT", "buy", "market", 1)
    finally:
        await ex.close()
    return httpx_mock.get_request()


# ── 1. Unconfigured: nothing changes ──


async def test_unconfigured_order_body_is_byte_identical(httpx_mock: HTTPXMock) -> None:
    req = await _place(httpx_mock)
    assert req.content.decode() == UNCONFIGURED_BODY
    assert "tag" not in json_lib.loads(req.content.decode())
    _assert_valid_signature(req)


async def test_unconfigured_sends_no_broker_header(httpx_mock: HTTPXMock) -> None:
    req = await _place(httpx_mock)
    assert "broker-id" not in req.headers


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_blank_config_is_the_same_as_unset(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv(OKX_BROKER_ID_ENV, blank)
    req = await _place(httpx_mock)
    assert req.content.decode() == UNCONFIGURED_BODY
    assert "broker-id" not in req.headers


# ── 2. Configured: one key more, nothing else ──


async def test_configured_adds_tag_and_leaves_every_other_field_alone(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OKX_BROKER_ID_ENV, FAKE_BROKER_ID)
    req = await _place(httpx_mock)
    body = json_lib.loads(req.content.decode())
    assert body["tag"] == FAKE_BROKER_ID
    stripped = {k: v for k, v in body.items() if k != "tag"}
    assert json_lib.dumps(stripped) == UNCONFIGURED_BODY


async def test_configured_order_signature_still_covers_the_verbatim_body(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OKX_BROKER_ID_ENV, FAKE_BROKER_ID)
    req = await _place(httpx_mock)
    _assert_valid_signature(req)


async def test_configured_sends_broker_header(httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OKX_BROKER_ID_ENV, FAKE_BROKER_ID)
    req = await _place(httpx_mock)
    assert req.headers["broker-id"] == FAKE_BROKER_ID


async def test_exactly_one_request_goes_out(httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OKX_BROKER_ID_ENV, FAKE_BROKER_ID)
    await _place(httpx_mock)
    assert len(httpx_mock.get_requests()) == 1


# ── 3. One source ──


def test_broker_id_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert okx_broker_id() == ""
    monkeypatch.setenv(OKX_BROKER_ID_ENV, f"  {FAKE_BROKER_ID}  ")
    assert okx_broker_id() == FAKE_BROKER_ID


def test_environment_name_is_stable() -> None:
    """Embedders (quantus-trader) read this same name — keep it put."""
    assert OKX_BROKER_ID_ENV == "OKX_BROKER_ID"
