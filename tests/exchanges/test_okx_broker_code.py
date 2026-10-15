"""Per-instance OKX broker code: order ``tag`` + ``broker-id`` header (fake codes only)."""

from __future__ import annotations

import json as json_lib

import pytest
from pytest_httpx import HTTPXMock

from pycex.exchanges.okx import OKX

_OK = {"code": "0", "msg": "", "data": [{"ordId": "1", "sCode": "0"}]}
_FAKE_API = "FAKEAPI01"
_FAKE_OAUTH = "FAKEOAUTH02"


def _linear(**kw) -> OKX:
    return OKX(api_key="k", secret="s", passphrase="p", market_type="linear", td_mode="isolated", **kw)


@pytest.mark.parametrize("code", [_FAKE_API, _FAKE_OAUTH])
async def test_instance_code_is_the_order_tag_and_header(httpx_mock: HTTPXMock, code: str) -> None:
    httpx_mock.add_response(json=_OK)
    ex = _linear(broker_code=code)
    await ex.create_order("BTC/USDT:USDT", "buy", "limit", 1, 50000.0)
    req = httpx_mock.get_request()
    assert json_lib.loads(req.content.decode())["tag"] == code
    assert req.headers["broker-id"] == code
    await ex.close()


async def test_two_adapters_in_one_process_keep_their_own_code(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_OK)
    httpx_mock.add_response(json=_OK)
    a, b = _linear(broker_code=_FAKE_API), _linear(broker_code=_FAKE_OAUTH)
    await a.create_order("BTC/USDT:USDT", "buy", "limit", 1, 50000.0)
    await b.create_order("BTC/USDT:USDT", "buy", "limit", 1, 50000.0)
    tags = [json_lib.loads(r.content.decode())["tag"] for r in httpx_mock.get_requests()]
    assert tags == [_FAKE_API, _FAKE_OAUTH]
    await a.close()
    await b.close()


@pytest.mark.parametrize("bad", ["", "   ", "has space", "한글코드", "X" * 17, "a-b"])
async def test_empty_or_malformed_instance_code_sends_no_tag(httpx_mock: HTTPXMock, bad: str) -> None:
    httpx_mock.add_response(json=_OK)
    ex = _linear(broker_code=bad)
    await ex.create_order("BTC/USDT:USDT", "buy", "limit", 1, 50000.0)
    req = httpx_mock.get_request()
    assert "tag" not in json_lib.loads(req.content.decode())
    assert "broker-id" not in req.headers
    await ex.close()


async def test_omitted_code_keeps_legacy_constant(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_OK)
    ex = _linear()
    await ex.create_order("BTC/USDT:USDT", "buy", "limit", 1, 50000.0)
    assert "tag" not in json_lib.loads(httpx_mock.get_request().content.decode())  # constant is empty
    await ex.close()
