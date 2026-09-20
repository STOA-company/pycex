"""A-9 — 2026-09-10 OKX **실계좌 실행 6회**의 기록 픽스처로 도는 계약 테스트.

`tests/fixtures/okx/live_20260910.json` 은 하네스 원장
(`mwork2:~/coding/ws/w11-okx-live/runs/*/live/ledger.jsonl`)에서 뽑은 **실제
요청/응답 쌍**이다. 실키는 쓰지 않는다 — 인증 헤더는 원장에 남지 않으며,
여기서는 네트워크 없이 그대로 재생만 한다.

이 파일이 지키는 것은 두 가지다.
1. 오늘의 어댑터가 만드는 요청이 **그때 실제로 통했던 요청과 같다.**
2. 그때 실제로 돌아온 거절이 **같은 결론**(막힘/재시도 가능/재시도 불가)에 닿는다.

여기서 재생하는 것은 A-2·A-3·A-5·A-7·A-8 이다. **A-6(포지션 방향·마진모드)은
여기 없다** — 하네스 원장이 포지션은 이미 파싱된 객체로만 남겨서
(`preflight.positions`: `{"amount": 0.01, "mode": "cross"}`) 거래소 원본 행이
기록되지 않았기 때문이다. 없는 원본을 지어내지 않는다. A-6 의 회귀는
`tests/exchanges/test_okx_position_side.py` 가 지킨다. 다만 그 한 줄은
**«amount 는 절대값»이 실제 라이브 기록에서도 그랬다**는 근거로 남는다.
"""

from __future__ import annotations

import json as json_lib
from typing import Any

import httpx
import pytest

from pycex.exceptions import ExchangeError, SettlementPendingError
from pycex.exchanges.okx import OKX
from tests.conftest import load_fixture

LIVE = load_fixture("okx", "live_20260910")
PAIRS: dict[str, Any] = LIVE["pairs"]


def pair(label: str) -> dict[str, Any]:
    return PAIRS[label]


def _adapter(labels: list[str], *, market_type: str = "spot", td_mode: str = "cross") -> OKX:
    """기록된 응답을 «요청 순서대로» 돌려주는 어댑터. 네트워크 0."""
    queue = [pair(label)["response"] for label in labels]
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=queue.pop(0))

    ex = OKX(
        api_key="test",
        secret="test",
        passphrase="test",
        market_type=market_type,  # type: ignore[arg-type]
        td_mode=td_mode,  # type: ignore[arg-type]
    )
    # A2-1: 클라이언트 «객체»를 꽂으면 sync 트윈 2회차의 재빌드에서 벗겨져 실 OKX 로 나간다.
    # 팩토리로 꽂으면 재빌드마다 다시 불려 목이 유지된다 — 이 파일의 «네트워크 0» 은 그 위에 선다.
    ex._http.set_transport_factory(lambda: httpx.MockTransport(handler))
    ex.sent = sent  # type: ignore[attr-defined]
    return ex


def _body(ex: OKX, index: int = -1) -> dict[str, Any]:
    return json_lib.loads(ex.sent[index].content.decode())  # type: ignore[attr-defined]


# ── 1. 오늘 만드는 요청 == 그때 통했던 요청 ──


async def test_spot_market_buy_body_matches_the_live_one_that_worked() -> None:
    recorded = pair("spot_market_buy_ok")["request"]["body"]
    ex = _adapter(["account_config_acctlv3", "spot_market_buy_ok"])
    order = await ex.create_order("BTC/USDT", "buy", "market", 10.0, client_order_id=recorded["clOrdId"])
    assert _body(ex) == recorded
    assert order.id == "3908989407217373184"
    assert order.client_order_id == recorded["clOrdId"]
    await ex.close()


async def test_spot_market_sell_body_matches_the_live_one() -> None:
    recorded = pair("spot_market_sell_ok")["request"]["body"]
    ex = _adapter(["account_config_acctlv3", "spot_market_sell_ok"])
    await ex.create_order("ETH/USDT", "sell", "market", 0.000567, client_order_id=recorded["clOrdId"])
    assert _body(ex) == recorded
    await ex.close()


async def test_spot_limit_body_matches_the_live_one() -> None:
    recorded = pair("spot_limit_ok")["request"]["body"]
    ex = _adapter(["account_config_acctlv3", "spot_limit_ok"])
    await ex.create_order("BTC/USDT", "buy", "limit", 0.00012834, 77912.1, client_order_id=recorded["clOrdId"])
    assert _body(ex) == recorded
    await ex.close()


async def test_swap_entry_body_matches_the_live_one() -> None:
    recorded = pair("swap_entry_ok")["request"]["body"]
    ex = _adapter(["swap_entry_ok"], market_type="linear", td_mode="isolated")
    await ex.create_order("ETH/USDT:USDT", "buy", "market", 0.04, client_order_id=recorded["clOrdId"])
    assert _body(ex) == recorded
    await ex.close()


async def test_swap_reduce_only_close_body_matches_the_live_one() -> None:
    """S10 정리에서 실제로 통했던 크로스마진 청산 주문 그대로."""
    recorded = pair("swap_reduce_only_close_ok")["request"]["body"]
    ex = _adapter(["swap_reduce_only_close_ok"], market_type="linear", td_mode="cross")
    await ex.create_order("ETH/USDT:USDT", "buy", "market", 0.01, client_order_id=recorded["clOrdId"], reduce_only=True)
    # Preserve the recording; current official API declares a JSON boolean.
    assert _body(ex) == {**recorded, "reduceOnly": True}
    await ex.close()


async def test_swap_tp_sl_body_matches_the_live_one() -> None:
    recorded = pair("swap_limit_with_tp_sl_ok")["request"]["body"]
    algo = recorded["attachAlgoOrds"][0]
    ex = _adapter(["swap_limit_with_tp_sl_ok"], market_type="linear", td_mode="isolated")
    await ex.create_order(
        "ETH/USDT:USDT",
        "buy",
        "limit",
        0.01,
        float(recorded["px"]),
        client_order_id=recorded["clOrdId"],
        tp_px=float(algo["tpTriggerPx"]),
        sl_px=float(algo["slTriggerPx"]),
    )
    assert _body(ex) == recorded
    await ex.close()


async def test_cancel_body_matches_the_live_one() -> None:
    recorded = pair("cancel_ok")["request"]["body"]
    ex = _adapter(["cancel_ok"], market_type="linear")
    order = await ex.cancel_order(recorded["ordId"], "ETH/USDT:USDT")
    assert _body(ex) == recorded
    assert order.client_order_id == "w11a21c83b106eb4"
    await ex.close()


async def test_set_leverage_body_matches_the_live_one() -> None:
    recorded = pair("set_leverage_ok")["request"]["body"]
    ex = _adapter(["set_leverage_ok"], market_type="linear")
    await ex.set_leverage("ETH/USDT:USDT", float(recorded["lever"]), recorded["mgnMode"])
    sent = _body(ex)
    assert sent["instId"] == recorded["instId"]
    assert sent["mgnMode"] == recorded["mgnMode"]
    # 하네스는 "3.0" 을 보냈고 OKX 는 받아줬다(실측). 우리는 "3" 으로 정규화해
    # 보내되, 값이 달라지지는 않는다.
    assert float(sent["lever"]) == float(recorded["lever"])
    await ex.close()


async def test_account_config_is_the_recorded_acct_lv_3() -> None:
    ex = _adapter(["account_config_acctlv3"])
    row = await ex.fetch_account_config()
    assert row["acctLv"] == "3"
    assert row["perm"] == "read_only,trade"
    assert ex.account_level == "3"
    await ex.close()


# ── 2. 그때 돌아온 거절 -> 같은 결론 ──


async def test_the_51000_td_mode_rejection_is_permanent_and_would_not_recur() -> None:
    """🚨 이 거절이 S1·S2·S3·S9 를 전멸시켰다.

    (a) 재생하면 재시도 불가로 분류되고, (b) 오늘의 어댑터는 그때의 `cash` 대신
    `cross` 를 보내므로 애초에 이 요청을 만들지 않는다.
    """
    recorded_request = pair("spot_cash_rejected_51000_tdmode")["request"]["body"]
    assert recorded_request["tdMode"] == "cash"

    ex = _adapter(["account_config_acctlv3", "spot_cash_rejected_51000_tdmode"])
    with pytest.raises(ExchangeError) as e:
        await ex.create_order("ETH/USDT", "sell", "market", 0.00105, client_order_id=recorded_request["clOrdId"])
    assert e.value.code == "51000"
    assert e.value.retryable is False
    assert _body(ex)["tdMode"] == "cross", "acctLv=3 계좌에 cash 를 다시 보내면 안 된다"
    await ex.close()


async def test_the_51008_rejection_is_retryable_and_sent_once() -> None:
    recorded = pair("settlement_pending_51008_spot_sell")["request"]["body"]
    ex = _adapter(["account_config_acctlv3", "settlement_pending_51008_spot_sell"])
    with pytest.raises(SettlementPendingError) as e:
        await ex.create_order("BTC/USDT", "sell", "market", float(recorded["sz"]), client_order_id=recorded["clOrdId"])
    assert e.value.retryable is True
    orders = [r for r in ex.sent if r.url.path == "/api/v5/trade/order"]  # type: ignore[attr-defined]
    assert len(orders) == 1, "SDK 가 스스로 다시 보내면 안 된다"
    await ex.close()


async def test_the_min_size_rejection_is_not_retryable() -> None:
    """`Parameter sz error` — 최소수량 미달은 기다린다고 풀리지 않는다."""
    recorded = pair("min_size_rejected_51000_sz")["request"]["body"]
    ex = _adapter(["account_config_acctlv3", "min_size_rejected_51000_sz"])
    with pytest.raises(ExchangeError) as e:
        await ex.create_order(
            "BTC/USDT", "buy", "limit", 1e-07, float(recorded["px"]), client_order_id=recorded["clOrdId"]
        )
    assert (e.value.code, e.value.retryable) == ("51000", False)
    await ex.close()


# ── 3. 실키 0 ──


def test_fixture_carries_no_credentials() -> None:
    """우회 1 — 픽스처에 비밀값이 섞여 들어오는 것을 막는다."""
    raw = json_lib.dumps(LIVE, ensure_ascii=False).lower()
    for needle in ("secret", "passphrase", "ok-access", "apikey", "api_key", "authorization"):
        assert needle not in raw, f"픽스처에 {needle} 이 들어 있다"


def test_replay_never_reads_credentials_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """우회 2 — 환경변수에 실키가 있어도 이 테스트들은 쓰지 않는다."""
    monkeypatch.setenv("OKX_API_KEY", "should-not-be-read")
    monkeypatch.setenv("OKX_SECRET", "should-not-be-read")
    ex = _adapter(["account_config_acctlv3"])
    assert ex._api_key == "test"
    assert "should-not-be-read" not in (ex._api_key + ex._secret + ex._passphrase)


def test_every_recorded_pair_is_covered_by_a_test() -> None:
    """우회 3 — 픽스처만 늘리고 검사는 안 하는 상태를 막는다."""
    import pathlib

    source = pathlib.Path(__file__).read_text()
    uncovered = [label for label in PAIRS if f'"{label}"' not in source.replace("PAIRS[label]", "")]
    assert uncovered == [], f"검사하지 않는 기록 쌍: {uncovered}"
