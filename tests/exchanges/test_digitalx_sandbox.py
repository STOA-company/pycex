"""Opt-in upstream artifact: DIGITALX_SANDBOX_FILE points outside this repository.

Never downloads from within pytest. Official sandbox cannot be redistributed.
Both Node and pytest deny outbound connections. All keys are ephemeral and read
in-process from the newly initialized local database; never printed or logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from decimal import Decimal
from pathlib import Path

import pytest

from pycex import DigitalX
from pycex.exceptions import AuthenticationError, DuplicateOrderError


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    artifact = os.environ.get("DIGITALX_SANDBOX_FILE")
    if not artifact:
        pytest.skip("Set DIGITALX_SANDBOX_FILE to separately downloaded official artifact")
    node = shutil.which("node")
    assert node and Path(artifact).is_file(), "Node 24+ and upstream sandbox artifact required"
    guard = Path(__file__).parents[1] / "fixtures/digitalx/deny-outbound.cjs"
    path = tmp_path_factory.mktemp("digitalx-official") / "sandbox.db"
    command = [node, "--require", str(guard), artifact]
    with socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", 9999)) != 0, "Port 9999 already owned; do not reuse it"
    result = subprocess.run(
        [*command, "init-db", "--db", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    assert result.returncode == 0, "Official sandbox init failed (output suppressed to protect generated keys)"
    with sqlite3.connect(path) as db:
        keys = db.execute("SELECT api_key, secret, type FROM api_keys").fetchall()
        # Official sandbox permits local balance shaping; fund each fixture asset.
        currencies = db.execute("SELECT base, counter FROM currency_pairs").fetchall()
        for currency in {c for pair in currencies for c in pair}:
            db.execute(
                "INSERT INTO balances (user_id, account_seq, currency, available) VALUES (1, 1, ?, '1000000000') "
                "ON CONFLICT(user_id, account_seq, currency) DO UPDATE SET available='1000000000'",
                (currency,),
            )
    proc = subprocess.Popen(
        [*command, "run", "--db", str(path), "--port", "9999"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert proc.poll() is None, "Owned sandbox process exited"
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", 9999)) == 0:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("Owned sandbox did not listen")
        yield keys
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.parametrize("auth", ["hmac-sha256", "ed25519"])
async def test_official_auth_and_cancel(sandbox, auth):
    if auth == "ed25519":
        pytest.importorskip("cryptography")
    key, secret, _ = next(k for k in sandbox if k[2] == auth)
    ex = DigitalX(key, secret, sandbox=True, auth_type="hmac" if auth == "hmac-sha256" else "ed25519")
    try:
        markets = await ex.fetch_markets(symbols=["BTC/KRW"])
        assert any(m.symbol == "BTC/KRW" for m in markets)
        balance = await ex.fetch_balance()
        assert balance.get("KRW").free_exact > 0
        ticker = await ex.fetch_ticker("BTC/KRW")
        # Far from matching book, valid KRW price tick and above minimum value.
        price = Decimal(int(ticker.last // 2 // 1000) * 1000)
        oid = await ex.create_order(
            "BTC/KRW", "buy", "limit", Decimal("0.001"), price, client_order_id="official-" + auth
        )
        assert oid.id and oid.amount_exact == Decimal("0.001")
        with pytest.raises(DuplicateOrderError):
            await ex.create_order(
                "BTC/KRW", "buy", "limit", Decimal("0.001"), price, client_order_id="official-" + auth
            )
        for _ in range(30):
            current = await ex.fetch_order(oid.id, "BTC/KRW")
            if current.status == "open":
                break
            await asyncio.sleep(0.1)
        assert current.status == "open"
        assert oid.id in {o.id for o in await ex.fetch_open_orders("BTC/KRW")}
        await ex.cancel_order(oid.id, "BTC/KRW")
        for _ in range(30):
            current = await ex.fetch_order(None, "BTC/KRW", client_order_id="official-" + auth)
            if current.status == "canceled":
                break
            await asyncio.sleep(0.1)
        assert current.status == "canceled"
        assert await ex.fetch_my_trades("BTC/KRW") == []
        ex._api_key = "invalid-ephemeral-key"
        with pytest.raises(AuthenticationError):
            await ex.fetch_balance()
    finally:
        await ex.close()


async def test_official_all_order_variants(sandbox):
    key, secret, _ = next(k for k in sandbox if k[2] == "hmac-sha256")
    ex = DigitalX(key, secret, sandbox=True)
    try:
        # Walk every pair the sandbox actually publishes. No live listing claim.
        markets = await ex.fetch_markets()
        coverage = []
        for market in markets:
            if not market.active:
                continue
            ticker = await ex.fetch_ticker(market.symbol)
            for kind, tifs in ex.order_kinds.items():
                for tif in tifs:
                    for side in ("buy", "sell"):
                        # Use native minOrderValue as quote spend; sell sizes follow base decimals.
                        price = Decimal(str(ticker.last))
                        qty = (Decimal("100000") / price).quantize(Decimal("0.0001"))
                        amount = Decimal("100000") if side == "buy" and kind != "limit" else qty
                        kw = {"time_in_force": tif}
                        if kind == "best":
                            kw["best_nth"] = 1
                        cid = f"{market.native}-{kind}-{tif}-{side}"
                        order = await ex.create_order(
                            market.symbol,
                            side,
                            kind,
                            amount,
                            price if kind == "limit" else None,
                            client_order_id=cid,
                            **kw,
                        )
                        assert order.id
                        coverage.append([market.symbol, kind, tif, side])
                        for _ in range(30):
                            state = await ex.fetch_order(order.id, market.symbol)
                            if state.status != "pending":
                                break
                            await asyncio.sleep(0.05)
                        assert state.status in {
                            "open",
                            "partiallyFilled",
                            "filled",
                            "expired",
                            "canceled",
                            "partiallyFilledCanceled",
                        }
                        if state.status in {"open", "partiallyFilled"}:
                            await ex.cancel_order(order.id, market.symbol)
        assert (await ex.fetch_balance()).assets
        print("OFFICIAL_COVERAGE=" + json.dumps(coverage))
    finally:
        await ex.close()
