"""A-6 — 포지션의 **방향은 ``side``**, **마진모드는 ``margin_mode``** 로 읽는다.
``amount`` 는 언제나 **절대값**이다.

🚨 09-10 회귀(S5): ``amount`` 의 부호로 방향을 판정했더니 숏 청산이 **추가
매도**로 나가 포지션이 닫히지 않았다. ``amount`` 에는 부호가 없다 — 부호로
판정할 수 있다는 착각 자체를 없앤다.

🚨 같은 날 두 번째: 청산을 하드캡의 마진모드(isolated)로 보냈더니, 하네스 밖에서
크로스로 열린 잔존 포지션이 tdMode 불일치로 거절됐다. 마진모드는 **그 포지션이
실제로 열려 있는 모드**를 읽어야 한다 — 그래서 SDK 가 추측하지 않고 노출한다.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from pycex.exchanges.okx import OKX, _parse_position
from pycex.exceptions import ExchangeError


def _row(**kw: object) -> dict:
    row = {"instId": "ETH-USDT-SWAP", "pos": "1", "posSide": "net", "mgnMode": "isolated"}
    row.update(kw)  # type: ignore[arg-type]
    return row


@pytest.mark.parametrize(("pos", "side"), [("3", "long"), ("-3", "short")])
def test_net_mode_side_comes_from_the_sign_but_amount_does_not(pos: str, side: str) -> None:
    p = _parse_position("ETH/USDT:USDT", _row(pos=pos))
    assert p.side == side
    assert p.amount == 3.0, "amount 는 절대값이다"
    assert p.signed_amount == float(pos)


@pytest.mark.parametrize("pos_side", ["long", "short"])
def test_hedge_mode_side_comes_from_pos_side(pos_side: str) -> None:
    """헤지 모드에서는 pos 가 양수로 오고 방향은 posSide 에만 있다."""
    p = _parse_position("ETH/USDT:USDT", _row(pos="2", posSide=pos_side))
    assert p.side == pos_side
    assert p.amount == 2.0
    assert p.signed_amount == (2.0 if pos_side == "long" else -2.0)


def test_zero_position_is_flat_not_a_guessed_direction() -> None:
    p = _parse_position("ETH/USDT:USDT", _row(pos="0"))
    assert p.side == "flat"
    assert p.amount == 0.0
    assert p.signed_amount == 0.0


def test_margin_mode_is_exposed_as_a_field_and_in_raw() -> None:
    p = _parse_position("ETH/USDT:USDT", _row(mgnMode="cross"))
    assert p.margin_mode == "cross"
    assert p.raw["mgnMode"] == "cross"


def test_missing_margin_mode_is_none_not_a_guess() -> None:
    """우회 1 — 거래소가 안 알려준 마진모드를 SDK 가 지어내지 않는다.

    지어낸 모드로 청산을 보내면 tdMode 불일치로 거절된다(09-10 실측).
    ``None`` 이면 호출자가 «모른다»는 사실을 보고 멈출 수 있다.
    """
    row = _row()
    del row["mgnMode"]
    assert _parse_position("ETH/USDT:USDT", row).margin_mode is None


@pytest.mark.parametrize("pos", ["-0.04", "-9999", "-1e-8"])
def test_amount_is_never_negative(pos: str) -> None:
    """우회 2 — 어떤 입력에도 amount 는 음수가 되지 않는다."""
    assert _parse_position("ETH/USDT:USDT", _row(pos=pos)).amount >= 0


def test_amount_docstring_states_it_is_absolute() -> None:
    """우회 3 — 계약을 문서에서 지운 채 코드만 남기지 않는다."""
    from pycex.models.position import Position

    doc = (Position.__doc__ or "") + " " + str(Position.model_fields["amount"].description or "")
    assert "absolute" in doc.lower()


async def test_fetch_positions_short_cross_row_end_to_end(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instId": "ETH-USDT-SWAP",
                    "pos": "-0.04",
                    "posSide": "net",
                    "mgnMode": "cross",
                    "avgPx": "2442.79",
                    "lever": "3",
                }
            ],
        }
    )
    ex = OKX(api_key="k", secret="s", passphrase="p", market_type="linear")
    (p,) = await ex.fetch_positions(["ETH/USDT:USDT"], include_flat=True)
    assert (p.side, p.amount, p.margin_mode) == ("short", 0.04, "cross")
    assert p.raw["instId"] == "ETH-USDT-SWAP"
    httpx_mock.add_response(json={"code": "0", "data": [{"instId": "ETH-USDT-SWAP", "posSide": "net"}]})
    with pytest.raises(ExchangeError, match="incomplete position snapshot"):
        await ex.fetch_positions(["ETH/USDT:USDT"], include_flat=True)
    httpx_mock.add_response(json={"code": "0", "data": [_row(pos="0")]})
    (flat,) = await ex.fetch_positions(["ETH/USDT:USDT"], include_flat=True)
    assert flat.raw["instId"] == "ETH-USDT-SWAP" and flat.raw["pos"] == "0"
    assert (flat.side, flat.amount) == ("flat", 0.0)
    await ex.close()
