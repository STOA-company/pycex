"""Bybit v5 linear perp: isolated margin, leverage set/read, positions, reduce_only (contract mock, no network).

Request shapes follow the official docs:
- POST /v5/position/set-leverage (buyLeverage/sellLeverage; retCode 110043 "leverage not modified")
  https://bybit-exchange.github.io/docs/v5/position/leverage
- POST /v5/account/set-margin-mode (unified account, account-wide; result.reasons on refusal)
  https://bybit-exchange.github.io/docs/v5/account/set-margin-mode
- GET /v5/position/list, GET /v5/account/info, POST /v5/order/create reduceOnly + orderLinkId
- retCode 10006 rate limit, 10010 IP not allowed, 110072 duplicate orderLinkId
"""

from __future__ import annotations

import json as json_lib
from urllib.parse import parse_qs

import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import (
    DuplicateOrderError,
    ExchangeError,
    InvalidOrderError,
    NotSupportedError,
    RateLimitError,
)
from pycex.exchanges.bybit import Bybit

SYM = "BTC/USDT:USDT"


def _ex() -> Bybit:
    return Bybit(api_key="k", secret="s", market_type="linear")


def _q(req) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}


def _ok(result: dict) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result}


def _info(mode: str = "ISOLATED_MARGIN", status: int = 3) -> dict:
    return _ok({"marginMode": mode, "unifiedMarginStatus": status})


def _pos_row(
    idx: int = 0, size: str = "0", side: str = "", lever: str = "3", trade: int = 0, symbol: str = "BTCUSDT"
) -> dict:
    return {
        "symbol": symbol,
        "positionIdx": idx,
        "side": side,
        "size": size,
        "avgPrice": "0" if size == "0" else "60000",
        "leverage": lever,
        "tradeMode": trade,
        "liqPrice": "",
        "unrealisedPnl": "0",
        "updatedTime": "1700000000000",
    }


def _pos(rows: list[dict], cursor: str = "") -> dict:
    return _ok({"category": "linear", "list": rows, "nextPageCursor": cursor})


async def test_set_leverage_posts_both_sides_as_string(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ok({}))
    ex = _ex()
    await ex.set_leverage(SYM, 3, "isolated")
    (req,) = httpx_mock.get_requests()
    body = json_lib.loads(req.content)
    assert req.url.path == "/v5/position/set-leverage"
    assert body == {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "3", "sellLeverage": "3"}
    assert "X-BAPI-SIGN" in req.headers
    await ex.close()


async def test_set_leverage_110043_already_that_value_is_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 110043, "retMsg": "Set leverage has not been modified", "result": {}})
    ex = _ex()
    assert await ex.set_leverage(SYM, 3, "isolated") == {}
    await ex.close()


async def test_set_leverage_other_errors_raise(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 110013, "retMsg": "Parameter error: leverage exceeds limit", "result": {}})
    ex = _ex()
    with pytest.raises(ExchangeError):
        await ex.set_leverage(SYM, 500, "isolated")
    await ex.close()


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True])
async def test_set_leverage_rejects_bad_values_before_any_request(bad, httpx_mock: HTTPXMock) -> None:  # type: ignore[no-untyped-def]
    ex = _ex()
    with pytest.raises(InvalidOrderError):
        await ex.set_leverage(SYM, bad, "isolated")
    assert not httpx_mock.get_requests()
    await ex.close()


async def test_spot_adapter_refuses_perp_settings() -> None:
    ex = Bybit(api_key="k", secret="s")
    for call in (
        ex.set_leverage(SYM, 3, "isolated"),
        ex.fetch_leverage(SYM),
        ex.set_margin_mode(SYM),
    ):
        with pytest.raises(NotSupportedError):
            await call
    await ex.close()


async def test_set_leverage_ensure_margin_mode_switches_unified_account(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info("REGULAR_MARGIN"))  # fetch_leverage: account info
    httpx_mock.add_response(json=_pos([_pos_row()]))  # fetch_leverage: position list
    httpx_mock.add_response(json=_info("REGULAR_MARGIN"))  # set_margin_mode: account info
    httpx_mock.add_response(json=_ok({"reasons": []}))  # set-margin-mode
    httpx_mock.add_response(json=_ok({}))  # set-leverage
    ex = _ex()
    await ex.set_leverage(SYM, 3, "isolated", ensure_margin_mode=True)
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert paths == [
        "/v5/account/info",
        "/v5/position/list",
        "/v5/account/info",
        "/v5/account/set-margin-mode",
        "/v5/position/set-leverage",
    ]
    assert json_lib.loads(httpx_mock.get_requests()[3].content) == {"setMarginMode": "ISOLATED_MARGIN"}
    await ex.close()


async def test_set_leverage_ensure_margin_mode_already_isolated_sends_no_mode_change(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info("ISOLATED_MARGIN"))
    httpx_mock.add_response(json=_pos([_pos_row()]))
    httpx_mock.add_response(json=_ok({}))
    ex = _ex()
    await ex.set_leverage(SYM, 3, "isolated", ensure_margin_mode=True)
    assert [r.url.path for r in httpx_mock.get_requests()][-1] == "/v5/position/set-leverage"
    assert "/v5/account/set-margin-mode" not in [r.url.path for r in httpx_mock.get_requests()]
    await ex.close()


async def test_margin_mode_refusal_reasons_raise(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info("REGULAR_MARGIN"))
    httpx_mock.add_response(
        json=_ok({"reasons": [{"reasonCode": "3400045", "reasonMsg": "Open positions or orders exist"}]})
    )
    ex = _ex()
    with pytest.raises(ExchangeError):
        await ex.set_margin_mode(SYM, "isolated")
    await ex.close()


async def test_margin_mode_portfolio_account_switch_is_account_wide_request(httpx_mock: HTTPXMock) -> None:
    """PORTFOLIO_MARGIN -> ISOLATED_MARGIN is a valid set-margin-mode value; the caller checks the account first."""
    httpx_mock.add_response(json=_info("PORTFOLIO_MARGIN", 4))
    httpx_mock.add_response(json=_ok({"reasons": []}))
    ex = _ex()
    await ex.set_margin_mode(SYM, "isolated")
    req = httpx_mock.get_requests()[-1]
    assert req.url.path == "/v5/account/set-margin-mode"
    assert json_lib.loads(req.content) == {"setMarginMode": "ISOLATED_MARGIN"}
    await ex.close()


async def test_set_margin_mode_does_not_require_a_flat_account(httpx_mock: HTTPXMock) -> None:
    """Bybit documents no 'no open perpetual position' condition; set_margin_mode sends the request regardless of
    other symbols' positions (it never reads them)."""
    httpx_mock.add_response(json=_info("REGULAR_MARGIN"))
    httpx_mock.add_response(json=_ok({"reasons": []}))
    ex = _ex()
    await ex.set_margin_mode(SYM, "isolated")
    assert [r.url.path for r in httpx_mock.get_requests()] == ["/v5/account/info", "/v5/account/set-margin-mode"]
    await ex.close()


async def test_classic_account_is_refused_not_guessed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info("REGULAR_MARGIN", 1))
    ex = _ex()
    with pytest.raises(NotSupportedError):
        await ex.set_margin_mode(SYM, "isolated", lever=3)
    assert [r.url.path for r in httpx_mock.get_requests()] == ["/v5/account/info"]
    await ex.close()


async def test_fetch_open_orders_without_symbol_reads_all_usdt_pages(httpx_mock: HTTPXMock) -> None:
    row = {"orderId": "1", "symbol": "ETHUSDT", "side": "Buy", "orderType": "Limit", "qty": "1", "price": "1",
           "cumExecQty": "0", "orderStatus": "New", "createdTime": "1"}
    httpx_mock.add_response(json=_ok({"list": [row], "nextPageCursor": "c2"}))
    httpx_mock.add_response(json=_ok({"list": [dict(row, orderId="2")], "nextPageCursor": ""}))
    ex = _ex()
    orders = await ex.fetch_open_orders()
    assert [o.id for o in orders] == ["1", "2"]
    reqs = httpx_mock.get_requests()
    assert "settleCoin=USDT" in str(reqs[0].url) and "cursor=c2" in str(reqs[1].url)
    await ex.close()


async def test_fetch_open_orders_read_failure_never_means_no_orders(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 10010, "retMsg": "Unmatched IP", "result": {}})
    ex = _ex()
    with pytest.raises(ExchangeError):
        await ex.fetch_open_orders()
    await ex.close()


async def test_fetch_leverage_one_way_unified_reads_account_mode_and_row(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info("ISOLATED_MARGIN"))
    httpx_mock.add_response(json=_pos([_pos_row(lever="3")]))
    ex = _ex()
    rows = await ex.fetch_leverage(SYM, "isolated")
    req = httpx_mock.get_requests()[-1]
    assert req.url.path == "/v5/position/list" and _q(req)["symbol"] == "BTCUSDT" and _q(req)["category"] == "linear"
    assert rows[0]["mgnMode"] == "isolated" and rows[0]["lever"] == "3" and rows[0]["posMode"] == "one_way_mode"
    assert rows[0]["symbol"] == "BTCUSDT"
    await ex.close()


async def test_fetch_leverage_hedge_mode_is_labelled_hedge(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info())
    httpx_mock.add_response(json=_pos([_pos_row(1), _pos_row(2)]))
    ex = _ex()
    assert (await ex.fetch_leverage(SYM))[0]["posMode"] == "hedge_mode"
    await ex.close()


async def test_fetch_leverage_empty_or_odd_rows_are_error_not_default(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info())
    httpx_mock.add_response(json=_pos([]))
    httpx_mock.add_response(json=_info())
    httpx_mock.add_response(json=_pos([_pos_row(1)]))
    ex = _ex()
    for _ in range(2):
        with pytest.raises(ExchangeError):
            await ex.fetch_leverage(SYM)
    await ex.close()


async def test_fetch_positions_pages_and_skips_flat(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info("ISOLATED_MARGIN"))
    httpx_mock.add_response(json=_pos([_pos_row(size="0.1", side="Buy", lever="3"), _pos_row(symbol="ETHUSDT")], "c2"))
    httpx_mock.add_response(json=_pos([_pos_row(symbol="SOLUSDT", size="2", side="Sell", lever="5")]))
    ex = _ex()
    got = await ex.fetch_positions()
    reqs = httpx_mock.get_requests()
    assert _q(reqs[1])["settleCoin"] == "USDT" and _q(reqs[2])["cursor"] == "c2"
    assert [(p.symbol, p.side, p.amount, p.margin_mode, p.leverage) for p in got] == [
        ("BTC/USDT:USDT", "long", 0.1, "isolated", 3.0),
        ("SOL/USDT:USDT", "short", 2.0, "isolated", 5.0),
    ]
    assert got[0].raw["symbol"] == "BTCUSDT"
    await ex.close()


async def test_fetch_positions_read_failure_never_means_flat(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info())
    httpx_mock.add_response(
        json={"retCode": 10010, "retMsg": "Unmatched IP, please check your API key's bound IP addresses.", "result": {}}
    )
    ex = _ex()
    with pytest.raises(ExchangeError) as e:
        await ex.fetch_positions()
    assert str(e.value.code) == "10010"
    await ex.close()


async def test_fetch_positions_unknown_direction_is_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_info())
    httpx_mock.add_response(json=_pos([_pos_row(size="1", side="")]))
    ex = _ex()
    with pytest.raises(ExchangeError):
        await ex.fetch_positions()
    await ex.close()


async def test_reduce_only_order_and_order_link_id(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ok({"orderId": "1", "orderLinkId": "q-abc"}))
    ex = _ex()
    o = await ex.create_order(SYM, "sell", "limit", 0.1, 60000, client_order_id="q-abc", reduce_only=True)
    body = json_lib.loads(httpx_mock.get_requests()[0].content)
    assert body["reduceOnly"] is True and body["orderLinkId"] == "q-abc" and body["category"] == "linear"
    assert o.client_order_id == "q-abc"
    await ex.close()


async def test_default_order_has_no_reduce_only_and_spot_refuses_it(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_ok({"orderId": "1"}))
    ex = _ex()
    await ex.create_order(SYM, "buy", "limit", 0.1, 60000)
    assert "reduceOnly" not in json_lib.loads(httpx_mock.get_requests()[0].content)
    await ex.close()
    spot = Bybit(api_key="k", secret="s")
    with pytest.raises(InvalidOrderError):
        await spot.create_order("BTC/USDT", "sell", "limit", 0.1, 60000, reduce_only=True)
    await spot.close()


async def test_duplicate_order_link_id_and_rate_limit_errors(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"retCode": 110072, "retMsg": "OrderLinkedID is duplicate", "result": {}})
    httpx_mock.add_response(
        json={"retCode": 10006, "retMsg": "Too many visits. Exceeded the API Rate Limit.", "result": {}}
    )
    ex = _ex()
    with pytest.raises(DuplicateOrderError):
        await ex.create_order(SYM, "buy", "limit", 0.1, 60000, client_order_id="q-abc")
    with pytest.raises(RateLimitError):
        await ex.create_order(SYM, "buy", "limit", 0.1, 60000, client_order_id="q-def")
    await ex.close()
