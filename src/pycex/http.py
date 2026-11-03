"""HTTP client with rate limiting and error handling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from pycex.exceptions import ExchangeError, NetworkError, PyCexError, RateLimitError

logger = logging.getLogger("pycex")


class RateLimiter:
    """Simple token bucket rate limiter.

    The bucket belongs to the caller's *whole* session, not to one event loop.
    Only the ``asyncio.Lock`` is loop-bound, so that — and nothing else — is
    rebuilt when the running loop changes; ``_tokens``/``_last`` carry over.
    Rebuilding the limiter instead hands every ``*_sync`` call a full bucket,
    which is the same as having no rate limit at all (A2-2).
    """

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _loop_lock(self) -> asyncio.Lock:
        """The lock for the loop running right now. ``asyncio.Lock`` binds to a
        loop the first time it is contended and refuses another one, so a new
        loop gets a new lock — the token budget above it does not move."""
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def acquire(self) -> None:
        async with self._loop_lock():
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0
                self._last = time.monotonic()
            else:
                self._tokens -= 1


class HTTPClient:
    """Async HTTP client for exchange API calls.

    The underlying ``httpx.AsyncClient`` owns a connection pool, and that pool
    belongs to the event loop it was first used in. The blocking ``*_sync``
    twins drive each call through its own ``asyncio.run`` (see
    ``pycex.base._make_sync``), so a client built once in ``__init__`` is dead
    from the second call onwards — ``RuntimeError: Event loop is closed``.
    This client therefore tracks which loop its ``httpx.AsyncClient`` is bound
    to and builds a fresh one whenever the running loop changes or the current
    one has been closed. See ``tests/test_sync_twins.py`` (A-1).
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        rate: float = 10.0,
        default_headers: dict[str, str] | None = None,
        error_mapper: Callable[[int, dict[str, Any]], PyCexError | None] | None = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_headers = default_headers or {}
        self._timeout = timeout
        self._rate = rate
        self._transport_factory = transport_factory
        self._client_obj: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._limiter = RateLimiter(rate)
        self._error_mapper = error_mapper

    def _new_client(self) -> httpx.AsyncClient:
        """Build a client — and, with it, a **new transport/pool** for the loop
        that is about to use it. Reusing the previous transport is exactly the
        bug this class exists to avoid, so a ``transport_factory`` is called
        again for every rebuild."""
        transport = None
        if self._transport_factory is not None:
            transport = self._transport_factory()
            if transport is None:
                # httpx would quietly substitute a real ``AsyncHTTPTransport``
                # here, so a caller who injected a factory to stay offline would
                # go out to the live venue without ever being told.
                raise TypeError("transport_factory returned None; it must return an httpx transport")
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=self._default_headers,
            transport=transport,
        )

    def set_transport_factory(self, factory: Callable[[], httpx.AsyncBaseTransport]) -> None:
        """The **only** supported way to inject a transport (recorded fixtures,
        offline tests, proxies).

        Injecting a finished ``httpx.AsyncClient`` used to be possible and was a
        money-path hazard: ``*_sync`` twins close the client after every call and
        the rebuild in :meth:`_bind` only knows about the factory — so the second
        call silently built a real client and went out to the live venue. A
        factory survives every rebuild, so what was injected stays injected.
        """
        self._transport_factory = factory
        self._client_obj = None
        self._client_loop = None

    @property
    def _client(self) -> httpx.AsyncClient:
        """The current client (read-only — inject with :meth:`set_transport_factory`).

        Building one here (outside a running loop) is allowed so callers can
        install ``event_hooks`` before the first request; the loop binding is
        decided later, in :meth:`_bind`."""
        if self._client_obj is None:
            self._client_obj = self._new_client()
        return self._client_obj

    async def _bind(self) -> httpx.AsyncClient:
        """Return a client owned by the loop that is running **right now**.

        Rebuilds when the loop changed under us or the current client was
        closed. The rate limiter is **not** rebuilt with it — the token budget
        belongs to this ``HTTPClient``, not to one loop (A2-2).
        """
        loop = asyncio.get_running_loop()
        client = self._client_obj
        if client is None or client.is_closed or (self._client_loop is not None and self._client_loop is not loop):
            hooks = client.event_hooks if client is not None else None
            client = self._new_client()
            if hooks:
                client.event_hooks = hooks
            self._client_obj = client
        self._client_loop = loop
        return client

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        client = await self._bind()
        await self._limiter.acquire()
        try:
            resp = await client.get(path, params=params, headers=headers)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            raise NetworkError(str(e)) from e

    async def post(
        self,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        client = await self._bind()
        await self._limiter.acquire()
        try:
            resp = await client.post(path, json=data, headers=headers, params=params)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            raise NetworkError(str(e)) from e

    async def delete(
        self, path: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        client = await self._bind()
        await self._limiter.acquire()
        try:
            resp = await client.delete(path, params=params, headers=headers)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            raise NetworkError(str(e)) from e

    async def post_raw(self, path: str, *, body: str, headers: dict[str, str] | None = None) -> Any:
        """POST a pre-serialized JSON body verbatim (async only — no sync twin).

        Sends ``body`` exactly as given via ``content=`` rather than handing a
        dict to httpx's own ``json=`` encoding (which re-serializes with its
        own separators, e.g. compact vs. ``json.dumps``'s default ``", "``/
        ``": "``). Callers whose signature covers the literal byte string being
        sent — e.g. OKX, whose prehash is ``ts + method + path + body`` — need
        the signed string and the wire bytes to be byte-for-byte identical;
        going through ``post()``'s ``json=`` would re-serialize and silently
        break every such signature.
        """
        client = await self._bind()
        await self._limiter.acquire()
        merged_headers = {"Content-Type": "application/json", **(headers or {})}
        try:
            resp = await client.post(path, content=body.encode(), headers=merged_headers)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            raise NetworkError(str(e)) from e

    async def post_form(
        self, path: str, *, data: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        """POST an ``application/x-www-form-urlencoded`` body (async only — no sync twin).

        Encodes ``data`` with :func:`urllib.parse.urlencode` ourselves (rather than
        handing the dict to httpx's own ``data=`` form encoding) so that callers who
        need to sign the exact bytes being sent — e.g. Korbit, whose signature covers
        the literal encoded body — get a byte-for-byte match between what they signed
        and what goes over the wire.
        """
        client = await self._bind()
        await self._limiter.acquire()
        body = urlencode(data or {}, doseq=True)
        merged_headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        try:
            resp = await client.post(path, content=body, headers=merged_headers)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            raise NetworkError(str(e)) from e

    def _handle_response(self, resp: httpx.Response) -> Any:
        """Return the parsed JSON body. Shape (dict or list) depends on the endpoint."""
        if resp.status_code == 429:
            raise self._rate_limit_error(resp)
        try:
            data: Any = resp.json()
        except ValueError:
            data = {"raw_text": resp.text}
        if resp.status_code >= 400:
            err_dict: dict[str, Any] = data if isinstance(data, dict) else {}
            if self._error_mapper is not None:
                mapped = self._error_mapper(resp.status_code, err_dict)
                if mapped is not None:
                    raise mapped
            msg = err_dict.get("msg") or err_dict.get("message") or str(data)
            code = err_dict.get("code") or err_dict.get("ret_code") or resp.status_code
            raise ExchangeError(msg, code=code)
        return data

    def _rate_limit_error(self, resp: httpx.Response) -> RateLimitError:
        """HTTP 429 stays a RateLimitError, but keeps what the body says.

        The venue code (Binance ``-1003`` = IP request-weight limit / IP ban) and
        the message ("... banned until <ms>") tell the caller whether to wait per
        account or per IP; dropping them made every 429 look like an account
        limit. Only the venue's own error mapper may name the code: a 429 whose
        body maps to a non-rate-limit error is still reported as a rate limit.
        """
        try:
            data: Any = resp.json()
        except ValueError:
            data = None
        body: dict[str, Any] = data if isinstance(data, dict) else {}
        err: RateLimitError | None = None
        if self._error_mapper is not None and body:
            mapped = self._error_mapper(resp.status_code, body)
            if isinstance(mapped, RateLimitError):
                err = mapped
        if err is None:
            msg = body.get("msg") or body.get("message") or "Rate limit exceeded"
            code = body.get("code") if body.get("code") not in (None, "") else body.get("ret_code")
            err = RateLimitError(str(msg), code=code, exchange="")
        ra = resp.headers.get("Retry-After")
        err.retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
        err.status_code = 429
        return err

    async def close(self) -> None:
        """Close the current client. The object is kept so that a later call can
        see it is closed and build a replacement (``*_sync`` twins close after
        every call — see ``pycex.base._make_sync``)."""
        client = self._client_obj
        if client is not None and not client.is_closed:
            await client.aclose()
