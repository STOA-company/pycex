"""``fetch_markets(symbols=)``: public filter argument, same request count on every venue but Korbit."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from pycex import Binance, Bithumb, Upbit
from tests.conftest import load_fixture

CASES = [
    (Upbit, "upbit", "markets"),
    (Bithumb, "bithumb", "markets"),
    (Binance, "binance", "markets_spot"),
]


@pytest.mark.parametrize(("cls", "venue", "fixture"), CASES)
async def test_symbols_only_filters_the_result_and_adds_no_request(
    httpx_mock: HTTPXMock, cls: type, venue: str, fixture: str
) -> None:
    httpx_mock.add_response(json=load_fixture(venue, fixture), is_reusable=True)
    async with cls() as ex:
        everything = await ex.fetch_markets()
        picked = everything[-1]
        subset = await ex.fetch_markets(symbols=[picked.symbol, "NOPE/NONE"])
        assert [m.symbol for m in subset] == [picked.symbol]
        assert subset[0].public_rules == picked.public_rules
        assert ex._markets.keys() == {m.native for m in everything}  # cache stays complete
        assert await ex.fetch_markets(symbols=[]) == []
        assert [m.symbol for m in await ex.fetch_markets(symbols=None)] == [m.symbol for m in everything]
    assert len(httpx_mock.get_requests()) == 4  # one catalogue call per fetch_markets, filtered or not


@pytest.mark.parametrize(("cls", "venue", "fixture"), CASES)
async def test_a_bare_string_is_rejected_not_iterated(
    httpx_mock: HTTPXMock, cls: type, venue: str, fixture: str
) -> None:
    httpx_mock.add_response(json=load_fixture(venue, fixture))
    async with cls() as ex:
        with pytest.raises(TypeError):
            await ex.fetch_markets(symbols="BTC/KRW")  # type: ignore[arg-type]


def test_sync_twin_forwards_symbols(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=load_fixture("upbit", "markets"), is_reusable=True)
    ex = Upbit()
    every = ex.fetch_markets_sync()
    only = ex.fetch_markets_sync(symbols=[every[0].symbol])
    assert [m.symbol for m in only] == [every[0].symbol]
