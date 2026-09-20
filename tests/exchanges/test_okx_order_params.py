"""A-5 — ``create_order`` 가 ``reduce_only`` · ``tp_px``/``sl_px``(attachAlgoOrds)
· ``tgt_ccy`` 를 받는다.

근거: 현물 **시장가** 주문의 ``sz`` 단위는 ``tgtCcy`` 가 정한다. 명시하지 않으면
거래소 기본값(매수=quote_ccy)에 의존하게 되는데, 그 기본값이 바뀌면 «10 USDT
어치»가 «10 BTC»가 된다. 의존하지 않는다 — 현물 시장가는 항상 명시해서 보낸다.

TP/SL 부착과 reduceOnly 도 하네스가 raw 로 직접 만들던 것들이다
(``w11-okx-live/okxlive/client.py:300-315``).
"""

from __future__ import annotations

import json as json_lib

import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import InvalidOrderError
from pycex.exchanges.okx import OKX

_ACK = {"code": "0", "msg": "", "data": [{"ordId": "1", "sCode": "0"}]}


def _spot() -> OKX:
    ex = OKX(api_key="k", secret="s", passphrase="p")
    ex._acct_level = "1"  # A-3 은 별도 파일에서 다룬다
    return ex


def _swap() -> OKX:
    return OKX(api_key="k", secret="s", passphrase="p", market_type="linear")


def _body(httpx_mock: HTTPXMock) -> dict:
    return json_lib.loads(httpx_mock.get_requests()[-1].content.decode())


# ── tgtCcy: 현물 시장가의 수량 단위 ──


@pytest.mark.parametrize(("side", "expected"), [("buy", "quote_ccy"), ("sell", "base_ccy")])
async def test_spot_market_order_always_states_tgt_ccy(httpx_mock: HTTPXMock, side: str, expected: str) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _spot()
    await ex.create_order("BTC/USDT", side, "market", 10)
    assert _body(httpx_mock)["tgtCcy"] == expected
    await ex.close()


async def test_explicit_tgt_ccy_overrides_the_default(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _spot()
    await ex.create_order("BTC/USDT", "buy", "market", 0.001, tgt_ccy="base_ccy")
    assert _body(httpx_mock)["tgtCcy"] == "base_ccy"
    await ex.close()


async def test_spot_limit_order_carries_no_tgt_ccy(httpx_mock: HTTPXMock) -> None:
    """지정가의 sz 는 언제나 base 수량이다 — tgtCcy 를 붙이면 뜻이 흐려진다."""
    httpx_mock.add_response(json=_ACK)
    ex = _spot()
    await ex.create_order("BTC/USDT", "buy", "limit", 0.001, 50000.0)
    assert "tgtCcy" not in _body(httpx_mock)
    await ex.close()


# ── reduceOnly ──


async def test_reduce_only_is_sent_for_swap(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    await ex.create_order("BTC/USDT:USDT", "sell", "market", 1, reduce_only=True)
    assert _body(httpx_mock)["reduceOnly"] is True
    await ex.close()


async def test_reduce_only_false_sends_no_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    await ex.create_order("BTC/USDT:USDT", "sell", "market", 1)
    assert "reduceOnly" not in _body(httpx_mock)
    await ex.close()


# ── TP/SL 부착 ──


async def test_attached_tp_and_sl(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    await ex.create_order("ETH/USDT:USDT", "buy", "limit", 1, 2442.79, tp_px=2491.64, sl_px=2393.93)
    algo = _body(httpx_mock)["attachAlgoOrds"]
    assert algo == [{"tpTriggerPx": "2491.64", "tpOrdPx": "-1", "slTriggerPx": "2393.93", "slOrdPx": "-1"}]
    await ex.close()


async def test_attached_tp_only(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    await ex.create_order("ETH/USDT:USDT", "buy", "limit", 1, 2442.79, tp_px=2491.64)
    assert _body(httpx_mock)["attachAlgoOrds"] == [{"tpTriggerPx": "2491.64", "tpOrdPx": "-1"}]
    await ex.close()


async def test_no_tp_sl_sends_no_algo_block(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ACK)
    ex = _swap()
    await ex.create_order("ETH/USDT:USDT", "buy", "limit", 1, 2442.79)
    assert "attachAlgoOrds" not in _body(httpx_mock)
    await ex.close()


# ── 우회 시도 ──


async def test_reduce_only_on_spot_is_rejected(httpx_mock: HTTPXMock) -> None:
    """우회 1 — 현물엔 줄일 포지션이 없다. 조용히 무시하면 «청산했다»는 거짓말이 된다."""
    ex = _spot()
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/USDT", "sell", "market", 1, reduce_only=True)
    assert httpx_mock.get_requests() == []
    await ex.close()


@pytest.mark.parametrize("bad", ["quote", "BASE_CCY", "", "usdt"])
async def test_unknown_tgt_ccy_is_rejected_before_the_network(httpx_mock: HTTPXMock, bad: str) -> None:
    """우회 2 — 모르는 단위를 보내고 거래소 답으로 확인하지 않는다."""
    ex = _spot()
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/USDT", "buy", "market", 10, tgt_ccy=bad)
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_tgt_ccy_on_limit_order_is_rejected(httpx_mock: HTTPXMock) -> None:
    """우회 3 — 지정가에 tgtCcy 를 붙이면 OKX 는 조용히 무시한다. 조용한 무시를 막는다."""
    ex = _spot()
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/USDT", "buy", "limit", 0.001, 50000.0, tgt_ccy="quote_ccy")
    assert httpx_mock.get_requests() == []
    await ex.close()


async def test_tgt_ccy_on_swap_is_rejected(httpx_mock: HTTPXMock) -> None:
    """우회 4 — 무기한의 sz 는 계약수다. tgtCcy 가 끼어들 자리가 없다."""
    ex = _swap()
    with pytest.raises(InvalidOrderError):
        await ex.create_order("BTC/USDT:USDT", "buy", "market", 1, tgt_ccy="quote_ccy")
    assert httpx_mock.get_requests() == []
    await ex.close()


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
async def test_non_positive_trigger_prices_are_rejected(httpx_mock: HTTPXMock, bad: float) -> None:
    """우회 5 — 0·음수·NaN 트리거는 «손절 걸어뒀다»는 착각만 만든다."""
    ex = _swap()
    with pytest.raises(InvalidOrderError):
        await ex.create_order("ETH/USDT:USDT", "buy", "limit", 1, 2442.79, sl_px=bad)
    assert httpx_mock.get_requests() == []
    await ex.close()
