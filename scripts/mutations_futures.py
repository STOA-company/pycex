"""Offline mutation checks for futures order safety; always restore each file."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    ("binance reduceOnly", "exchanges/binance.py", 'params["reduceOnly"] = "true"', "pass"),
    ("bitget reduceOnly", "exchanges/bitget.py", 'body["reduceOnly"] = "YES"', "pass"),
    ("okx reduceOnly", "exchanges/okx.py", 'body["reduceOnly"] = True', "pass"),
    ("binance client ID", "exchanges/binance.py", 'params["newClientOrderId"] = client_order_id', "pass"),
    ("bitget client ID", "exchanges/bitget.py", 'body["clientOid"] = client_order_id', "pass"),
    ("okx client ID", "exchanges/okx.py", 'body["clOrdId"] = _validated_client_order_id(client_order_id)', "pass"),
    (
        "binance lookup ID",
        "exchanges/binance.py",
        '{"origClientOrderId": client_order_id}',
        '{"orderId": client_order_id}',
    ),
    ("bitget lookup ID", "exchanges/bitget.py", '{"clientOid": client_order_id}', '{"orderId": client_order_id}'),
    (
        "okx lookup ID",
        "exchanges/okx.py",
        '{"clOrdId": _validated_client_order_id(client_order_id)}',
        '{"ordId": client_order_id}',
    ),
    ("binance hedge error", "exchanges/binance.py", 'if code == "-4061":', 'if code == "never":'),
    ("bitget hedge error", "exchanges/bitget.py", 'if code == "45109":', 'if code == "never":'),
    ("okx hedge error", "exchanges/okx.py", 'code == "51000" and "parameter posside error" in msg.lower()', "False"),
    ("bitget hedge preflight", "exchanges/bitget.py", 'if mode == "hedge_mode":', "if False:"),
    ("bitget unknown mode", "exchanges/bitget.py", 'if mode != "one_way_mode":', "if False:"),
    (
        "contract multiplier",
        "exchanges/okx.py",
        'float(Decimal(d["ctVal"]) * Decimal(d["ctMult"]))',
        'float(d["ctVal"])',
    ),
    ("minimum quantity", "models/market.py", "result < minimum or result % step != 0", "result % step != 0"),
    ("quantity step", "models/market.py", "result < minimum or result % step != 0", "result < minimum"),
    ("no decimal rounding", "models/market.py", "context.traps[Inexact] = True", "context.traps[Inexact] = False"),
    ("market minimum", "models/market.py", "if self.market_min_amount is not None and", "if False and"),
    ("market step", "models/market.py", "if self.market_amount_step and", "if False and"),
    ("binance average", "exchanges/binance.py", 'float(d["avgPrice"]) or None', "None"),
    ("bitget average", "exchanges/bitget.py", 'float(d["priceAvg"]) or None', "None"),
    ("okx average", "exchanges/okx.py", 'float(d["avgPx"]) or None', "None"),
    ("partial state", "models/order.py", '"partially_filled": "partially_filled"', '"partially_filled": "filled"'),
]
MUTATIONS += [
    ("binance empty ACK", "exchanges/binance.py", 'not order.status and data.get("orderId")', "not order.status"),
    ("bitget empty ACK", "exchanges/bitget.py", 'and (first.get("orderId") or first.get("clientOid"))', ""),
    ("okx empty ACK", "exchanges/okx.py", 'and r.get("ordId")', ""),
]


def main() -> int:
    failures = 0
    print("| Mutation | Result |", flush=True)
    print("|---|---|", flush=True)
    for label, relative, old, new in MUTATIONS:
        path = ROOT / "src/pycex" / relative
        original = path.read_text()
        if original.count(old) != 1:
            raise RuntimeError(f"Mutation anchor is not unique: {label}")
        try:
            path.write_text(original.replace(old, new))
            # Separate bytecode cache per subprocess avoids same-second stale .pyc hits.
            result = subprocess.run(
                [sys.executable, "-B", "-m", "pytest", "-q", "tests/test_futures_order_safety.py"],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONPYCACHEPREFIX": str(ROOT / ".mutation-cache" / label.replace(" ", "_"))},
            )
            killed = result.returncode == 1 and "failed" in result.stdout and "ERROR collecting" not in result.stdout
            print(f"| {label} | {'KILLED' if killed else 'SURVIVED/ERROR'} |", flush=True)
            if not killed:
                print(result.stdout, flush=True)
                failures += 1
        finally:
            path.write_text(original)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
