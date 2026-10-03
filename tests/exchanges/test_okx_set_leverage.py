"""A-4 — ``set_leverage(symbol, lever, mgn_mode)`` 를 SDK 가 제공한다.

없어서 하네스가 ``POST /api/v5/account/set-leverage`` 를 raw 로 직접 불렀다
(``w11-okx-live/okxlive/client.py:267-275``). 어댑터가 raw HTTP 를 만들 이유를
없애는 것이 줄기 A 의 목적이다.

**정책은 여기 없다.** 레버리지 상한·마진모드 강제는 호출자(P-3 주문 예산
게이트)의 몫이고, SDK 는 거래소가 받지 않을 값만 막는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json as json_lib

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import ExchangeError, InvalidOrderError, NotSupportedError
from pycex.exchanges.okx import OKX

_ACK = {"code": "0", "msg": "", "data": [{"instId": "BTC-USDT-SWAP", "lever": "3", "mgnMode": "isolated"}]}


def _swap() -> OKX:
    return OKX(api_key="k", secret="s", passphrase="p", market_type="linear")


def _assert_valid_signature(request: httpx.Request) -> None:
    ts = request.headers["OK-ACCESS-TIMESTAMP"]
    message = ts + request.method + request.url.raw_path.decode() + (request.content.decode() or "")
    expected = base64.b64encode(hmac.new(b"s", message.encode(), hashlib.sha256).digest()).decode()
    assert request.headers["OK-ACCESS-SIGN"] == expected


async def test_set_leverage_sends_native_instrument_and_mode(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    row = await ex.set_leverage("BTC/USDT:USDT", 3, "isolated")
    req = httpx_mock.get_requests()[-1]
    assert req.url.path == "/api/v5/account/set-leverage"
    body = json_lib.loads(req.content.decode())
    assert body == {"instId": "BTC-USDT-SWAP", "lever": "3", "mgnMode": "isolated"}
    assert row["lever"] == "3"
    _assert_valid_signature(req)
    await ex.close()


async def test_set_leverage_defaults_to_cross(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    await ex.set_leverage("BTC/USDT:USDT", 5)
    body = json_lib.loads(httpx_mock.get_requests()[-1].content.decode())
    assert body["mgnMode"] == "cross"
    await ex.close()


async def test_integral_leverage_is_not_sent_as_a_float(httpx_mock: HTTPXMock) -> None:
    """표기를 한 모양으로 정규화한다 — 3 은 "3", 1.5 는 "1.5".

    (실측 정정: 09-10 원장을 보면 OKX 는 "3.0" 도 받아준다. 거부를 피하는 게
    아니라 파이썬 float 표기가 와이어 모양을 정하지 못하게 하는 것이다.)
    """
    httpx_mock.add_response(json=_ACK, is_reusable=True)
    ex = _swap()
    await ex.set_leverage("BTC/USDT:USDT", 3.0, "isolated")
    assert json_lib.loads(httpx_mock.get_requests()[-1].content.decode())["lever"] == "3"
    await ex.set_leverage("BTC/USDT:USDT", 1.5, "isolated")
    assert json_lib.loads(httpx_mock.get_requests()[-1].content.decode())["lever"] == "1.5"
    await ex.close()


async def test_set_leverage_rejection_is_not_swallowed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"code": "51000", "msg": "Parameter lever error", "data": []})
    ex = _swap()
    with pytest.raises(ExchangeError):
        await ex.set_leverage("BTC/USDT:USDT", 3, "isolated")
    await ex.close()


def test_set_leverage_has_a_sync_twin(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://www.okx.com/api/v5/account/config",
                            json={"code": "0", "data": [{"acctLv": "2", "posMode": "net_mode"}]})
    httpx_mock.add_response(url="https://www.okx.com/api/v5/account/leverage-info?instId=BTC-USDT-SWAP&mgnMode=isolated",
                            json={"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "lever": "3",
                                                         "mgnMode": "isolated", "posSide": "net"}]})
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    assert ex.fetch_account_config_sync() == {"acctLv": "2", "posMode": "net_mode"}
    assert ex.fetch_leverage_sync("BTC/USDT:USDT", "isolated") == [
        {"instId": "BTC-USDT-SWAP", "lever": "3", "mgnMode": "isolated", "posSide": "net"}]
    requests = httpx_mock.get_requests()
    assert [request.method for request in requests] == ["GET", "GET"]
    assert dict(requests[1].url.params) == {"instId": "BTC-USDT-SWAP", "mgnMode": "isolated"}
    for request in requests:
        _assert_valid_signature(request)
    assert ex.set_leverage_sync("BTC/USDT:USDT", 3, "isolated")["lever"] == "3"


# ── 우회 시도 ──


async def test_spot_adapter_cannot_set_leverage(httpx_mock: HTTPXMock) -> None:
    """우회 1 — 현물 어댑터로 레버리지를 거는 것은 요청조차 나가지 않는다."""
    ex = OKX(api_key="k", secret="s", passphrase="p")
    with pytest.raises(NotSupportedError):
        await ex.set_leverage("BTC/USDT", 3, "isolated")
    assert httpx_mock.get_requests() == []
    await ex.close()


@pytest.mark.parametrize("mode", ["cash", "", "ISOLATED", "portfolio"])
async def test_unknown_margin_mode_is_rejected_before_the_network(httpx_mock: HTTPXMock, mode: str) -> None:
    """우회 2 — 마진모드를 «보내보고» 거래소 답으로 확인하지 않는다."""
    ex = _swap()
    with pytest.raises(InvalidOrderError):
        await ex.set_leverage("BTC/USDT:USDT", 3, mode)
    assert httpx_mock.get_requests() == []
    await ex.close()


@pytest.mark.parametrize("lever", [0, -3, float("nan"), float("inf")])
async def test_non_positive_leverage_is_rejected_before_the_network(httpx_mock: HTTPXMock, lever: float) -> None:
    """우회 3 — 0·음수·NaN·무한대는 여기서 죽는다."""
    ex = _swap()
    with pytest.raises(InvalidOrderError):
        await ex.set_leverage("BTC/USDT:USDT", lever, "isolated")
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_sdk_does_not_impose_a_leverage_cap(httpx_mock: HTTPXMock) -> None:
    """우회 4(반대 방향) — SDK 가 몰래 상한을 걸지 않는다. 캡은 호출자 몫이다.

    SDK 안에 조용한 상한이 있으면 호출자의 캡이 «지켜지고 있다»는 착각을 만든다.
    """
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    await ex.set_leverage("BTC/USDT:USDT", 125, "cross")
    assert json_lib.loads(httpx_mock.get_requests()[-1].content.decode())["lever"] == "125"
    await ex.close()
