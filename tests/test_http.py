import pytest
from pytest_httpx import HTTPXMock

from pycex.exceptions import ExchangeError, InsufficientBalanceError, RateLimitError
from pycex.http import HTTPClient


async def test_429_carries_retry_after(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "3"}, json={})
    c = HTTPClient("https://x.test", rate=1000)
    with pytest.raises(RateLimitError) as e:
        await c.get("/p")
    assert e.value.retry_after == 3.0
    await c.close()


async def test_delete_goes_through_handler(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="DELETE", status_code=400, json={"code": -2011, "msg": "Unknown order"})
    c = HTTPClient("https://x.test", rate=1000)
    with pytest.raises(ExchangeError) as e:
        await c.delete("/o", params={"id": "1"})
    assert e.value.code == -2011
    await c.close()


async def test_error_mapper_hook(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400, json={"code": -2010, "msg": "insufficient"})

    def mapper(status: int, data: dict) -> ExchangeError | None:
        return InsufficientBalanceError(data["msg"], code=data["code"]) if data.get("code") == -2010 else None

    c = HTTPClient("https://x.test", rate=1000, error_mapper=mapper)
    with pytest.raises(InsufficientBalanceError):
        await c.get("/p")
    await c.close()


async def test_429_keeps_binance_ip_limit_code_and_ban_text(httpx_mock: HTTPXMock) -> None:
    """A Binance IP request-weight 429 keeps -1003 and the ban deadline (callers wait per IP)."""
    from pycex.exchanges.binance import _error_mapper

    httpx_mock.add_response(
        status_code=429,
        headers={"Retry-After": "120"},
        json={"code": -1003, "msg": "Way too much request weight used; IP banned until 1700000000000."},
    )
    c = HTTPClient("https://x.test", rate=1000, error_mapper=_error_mapper)
    with pytest.raises(RateLimitError) as e:
        await c.get("/p")
    assert e.value.code == -1003
    assert e.value.retry_after == 120.0
    assert e.value.status_code == 429
    assert "banned until 1700000000000" in str(e.value)
    await c.close()


async def test_429_with_account_body_stays_account_code(httpx_mock: HTTPXMock) -> None:
    """Another venue's account 429 is not turned into an IP code."""
    httpx_mock.add_response(status_code=429, json={"code": "429", "msg": "Too Many Requests"})
    c = HTTPClient("https://x.test", rate=1000)
    with pytest.raises(RateLimitError) as e:
        await c.get("/p")
    assert e.value.code == "429" and e.value.status_code == 429
    await c.close()


async def test_429_non_rate_mapping_still_rate_limit(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=429, text="slow down")

    def mapper(status: int, data: dict) -> ExchangeError | None:
        return InsufficientBalanceError("no", code=1)

    c = HTTPClient("https://x.test", rate=1000, error_mapper=mapper)
    with pytest.raises(RateLimitError) as e:
        await c.get("/p")
    assert e.value.code is None and e.value.status_code == 429
    await c.close()
