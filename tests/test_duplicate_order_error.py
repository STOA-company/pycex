"""A venue refusing a duplicate ``client_order_id`` is ``DuplicateOrderError``, not a definite rejection.

The bodies below are the documented error shapes (not live captures — no live call was made), fed
through each adapter's own ``_error_mapper``, the function ``HTTPClient`` runs on a non-2xx/error body.
Sources:

- OKX 51016 "Client order ID already exists." — https://www.okx.com/docs-v5/en/#error-code-rest-api-trade
- Binance -2010 "Duplicate order sent." — https://developers.binance.com/docs/binance-spot-api-docs/errors
- Bitget 40786 "Duplicate clientOid" — https://bitgetlimited.github.io/apidoc/en/spot/ (Error Code table)
- Bitget, same table: 40708 "client_oid duplicate", 43118 "clientOrderId duplicate" (also the spot place-order
  "Duplicate clientOrderId Response" example), 45034 "clientOid duplicate", 50060 "Duplicated clientOid"
- Binance USDT-M futures -4116 DUPLICATED_CLIENT_ORDER_ID "clientOrderId is duplicated" —
  https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code (the spot errors page has no
  -4xxx codes, so the code is unambiguous)
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
        bitget._error_mapper,
        400,
        {"code": "40708", "msg": "client_oid duplicate", "requestTime": 1627293504611},
        "40708",
    ),
    (bitget._error_mapper, 400, {"code": "43118", "msg": "clientOrderId duplicate"}, "43118"),
    (bitget._error_mapper, 400, {"code": "45034", "msg": "clientOid duplicate", "requestTime": 1627293504611}, "45034"),
    (
        bitget._error_mapper,
        400,
        {"code": "50060", "msg": "Duplicated clientOid", "requestTime": 1627293504611},
        "50060",
    ),
    (binance._error_mapper, 400, {"code": -4116, "msg": "clientOrderId is duplicated."}, "-4116"),
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
        # Neighbours of the newly mapped codes stay generic (-4115 is a transfer id, not an order id).
        (bitget._error_mapper, 400, {"code": "43119", "msg": "Trading is not open"}, ExchangeError),
        (bitget._error_mapper, 400, {"code": "40709", "msg": "x"}, ExchangeError),
        (binance._error_mapper, 400, {"code": -4115, "msg": "clientTranId is duplicated"}, ExchangeError),
        (binance._error_mapper, 400, {"code": -4117, "msg": "x"}, ExchangeError),
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


def test_okx_51784_is_not_mapped() -> None:
    # Documented only for finance/flexible-loan borrow and repay ("Client order ID is being processed"),
    # never for POST /api/v5/trade/order, so it stays the generic ExchangeError.
    err = okx._error_mapper(200, {"code": "51784", "msg": "Client order ID is being processed", "data": []})
    assert type(err) is ExchangeError


def test_bithumb_has_no_documented_duplicate_error_so_stays_generic() -> None:
    err = bithumb._map_error(400, {"error": {"name": "duplicated_identifier", "message": "x"}})
    assert type(err) is ExchangeError
