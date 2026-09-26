"""A venue refusing a duplicate ``client_order_id`` is ``DuplicateOrderError``, not a definite rejection.

The bodies below are the documented error shapes (not live captures — no live call was made), fed
through each adapter's own ``_error_mapper``, the function ``HTTPClient`` runs on a non-2xx/error body.
Sources:

- OKX 51016 "Client order ID already exists." — https://www.okx.com/docs-v5/en/#error-code-rest-api-trade
- Binance -2010 "Duplicate order sent." — https://developers.binance.com/docs/binance-spot-api-docs/errors
- Bitget 40786 "Duplicate clientOid" — https://bitgetlimited.github.io/apidoc/en/spot/ (Error Code table)
- Upbit ``duplicated_identifier`` (400) — https://docs.upbit.com/kr/reference/rest-api-guide
- Korbit ``DUPLICATE_CLIENT_ORDER_ID`` — https://docs.digitalx.miraeasset.com/llms/en/rest_api/trading.md

Bithumb documents no duplicate ``client_order_id`` error, so it stays unmapped (last test).
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import (
    DuplicateOrderError,
    ExchangeError,
    InsufficientBalanceError,
    InvalidOrderError,
    OrderNotFoundError,
    PyCexError,
    SettlementPendingError,
)
from pycex.exchanges import binance, bitget, bithumb, korbit, okx, upbit

_DUPLICATE_BODIES: list[tuple[Any, int, dict[str, Any], str]] = [
    (okx._error_mapper, 200, {"code": "51016", "msg": "Client order ID already exists.", "data": []}, "51016"),
    (
        binance._error_mapper,
        400,
        {"code": -2010, "msg": "Duplicate order sent."},
        "-2010",
    ),
    (bitget._error_mapper, 400, {"code": "40786", "msg": "Duplicate clientOid", "requestTime": 1627293504611}, "40786"),
    (
        upbit._map_error,
        400,
        {"error": {"name": "duplicated_identifier", "message": "이미 등록된 identifier입니다"}},
        "duplicated_identifier",
    ),
    (
        korbit._map_error,
        400,
        {"error": {"code": 400, "message": "DUPLICATE_CLIENT_ORDER_ID"}},
        "DUPLICATE_CLIENT_ORDER_ID",
    ),
]


@pytest.mark.parametrize(("mapper", "status", "body", "code"), _DUPLICATE_BODIES)
def test_duplicate_client_order_id_is_duplicate_order_error(
    mapper: Any, status: int, body: dict[str, Any], code: str
) -> None:
    err = mapper(status, body)
    assert isinstance(err, DuplicateOrderError)
    assert str(err.code) == code
    # The whole point: trader's «definite reject» list is InvalidOrderError, and this must not be on it.
    assert not isinstance(err, InvalidOrderError)
    assert isinstance(err, ExchangeError)


def test_duplicate_order_error_sits_beside_invalid_order_error() -> None:
    assert issubclass(DuplicateOrderError, ExchangeError)
    assert not issubclass(DuplicateOrderError, InvalidOrderError)
    assert not issubclass(InvalidOrderError, DuplicateOrderError)


async def test_okx_create_order_scode_51016_raises_duplicate_order_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "code": "1",
            "msg": "All operations failed",
            "data": [
                {"ordId": "", "clOrdId": "cid1", "tag": "", "sCode": "51016", "sMsg": "Client order ID already exists."}
            ],
        }
    )
    ex = okx.OKX(api_key="k", secret="s", passphrase="p")
    ex._acct_level = "1"
    with pytest.raises(DuplicateOrderError) as info:
        await ex.create_order("BTC/USDT", "buy", "limit", 1, price=1.0, client_order_id="cid1")
    assert not isinstance(info.value, InvalidOrderError)
    await ex.close()


# ── other codes unchanged ──


@pytest.mark.parametrize(
    ("mapper", "status", "body", "expected"),
    [
        (okx._error_mapper, 200, {"code": "51008", "msg": "x", "data": []}, SettlementPendingError),
        (okx._error_mapper, 200, {"code": "51603", "msg": "x", "data": []}, OrderNotFoundError),
        (okx._error_mapper, 200, {"code": "51015", "msg": "x", "data": []}, ExchangeError),
        # Binance -2010 without the duplicate message is still insufficient balance.
        (
            binance._error_mapper,
            400,
            {"code": -2010, "msg": "Account has insufficient balance for requested action."},
            InsufficientBalanceError,
        ),
        (bitget._error_mapper, 400, {"code": "40711", "msg": "x"}, InsufficientBalanceError),
        (bitget._error_mapper, 400, {"code": "40787", "msg": "x"}, ExchangeError),
        (
            upbit._map_error,
            400,
            {"error": {"name": "insufficient_funds_bid", "message": "x"}},
            InsufficientBalanceError,
        ),
        (korbit._map_error, 400, {"error": {"code": 400, "message": "NO_BALANCE"}}, InsufficientBalanceError),
    ],
)
def test_other_codes_keep_their_mapping(
    mapper: Any, status: int, body: dict[str, Any], expected: type[PyCexError]
) -> None:
    err = mapper(status, body)
    assert type(err) is expected


def test_bithumb_has_no_documented_duplicate_error_so_stays_generic() -> None:
    err = bithumb._map_error(400, {"error": {"name": "duplicated_identifier", "message": "x"}})
    assert type(err) is ExchangeError
