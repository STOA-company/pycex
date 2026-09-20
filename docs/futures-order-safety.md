# USDT perpetual order safety

Binance, Bitget and OKX accept the same optional order arguments:

```python
from pycex import OKX

exchange = OKX(api_key="...", secret="...", passphrase="...", market_type="linear")
markets = await exchange.fetch_markets()
market = next(m for m in markets if m.symbol == "BTC/USDT:USDT")
amount = market.amount_from_base("0.001")
order = await exchange.create_order(
    market.symbol, "buy", "market", float(amount), client_order_id="CustomerOrder001"
)
# After an uncertain response, LOOK UP the existing order; do not blindly resend.
order = await exchange.fetch_order(None, market.symbol, client_order_id="CustomerOrder001")
print(order.normalized_status, order.filled, order.average)
await exchange.close()
```

This example is illustrative and was tested only with synthetic transports. It
is not a live-order verification. Credential placeholders are not real keys.

## Reading a result

- `status` preserves the original venue value for existing integrations.
- `normalized_status` is `accepted`, `open`, `partially_filled`, `filled`,
  `canceled`, `expired`, `rejected`, or `unknown`. An ACK is only `accepted`;
  it does not prove execution. Query the order to obtain its cumulative fills.
- `filled` is cumulative executed **order units**; `average` is the execution
  average price, or `None` if not reported. `price` remains the submitted limit
  price. A canceled order can still have `filled > 0` and an `average`.
- `Market.amount_unit` is `base` for Binance/Bitget and `contract` for OKX.
  `contract_size` is base units per order contract (1 for base-unit markets).
  `min_amount` and `amount_step` use order units. Binance additionally exposes
  `market_min_amount`/`market_amount_step` from `MARKET_LOT_SIZE`.
- `amount_from_base` returns a Decimal, rejects below-minimum/off-step quantities
  and never rounds upward. It checks the additional market-order filters where
  supplied. It does not convert quote budgets, check notional/max size, or
  automatically run inside `create_order`. Callers retain their sizing policy.

## Reducing and unsupported account modes

Use `reduce_only=True` for a position reduction. On these adapters' spot
instances it raises `NotSupportedError` before order submission. OKX also retains
compatibility with callers catching `InvalidOrderError` for that rejection.

Reduce-only prevents opening the opposite position; it does **not** promise that
an oversized reduction always rejects instead of clipping/canceling other
reduce-only orders. Bitget can return no order ID for a reduce-only ACK; preserve
`client_order_id` and query by it.

`HedgeModeNotSupportedError` is non-retryable. Binance `-4061` and OKX's specific
`51000 Parameter posSide error` are mapped to it. Bitget checks `posMode` before
every linear order (one extra signed GET; no cache), rejects `hedge_mode` before
POST, and fails closed if mode is missing. Bitget `45109` also maps to the typed
exception. Other errors retain their existing classification. A preflight does
not lock the account against concurrent external mode changes.

## Client IDs are reconciliation keys, not exactly-once execution

`client_order_id` is passed verbatim as Binance `newClientOrderId`, Bitget
`clientOid`, or OKX `clOrdId`. ID lookup uses `origClientOrderId`, `clientOid`, or
`clOrdId`, respectively. Pass exactly one ID: `fetch_order(exchange_id, symbol)`
or `fetch_order(None, symbol, client_order_id=...)`.

Binance only requires ID uniqueness among open orders. OKX permits reuse after
orders finish and returns the latest match on lookup. **A filled market order can
therefore be executed again when its ID is reused.** The library does not retry,
persist deduplication state, or promise exactly-once execution. The execution
server must own durable order intent and reconciliation. OKX keeps its existing
1–32 alphanumeric validation; Binance/Bitget validation is left to the venue.

## Official references (checked 2026-09-20)

- [Binance futures order creation/query](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)
- [Binance quantity filters](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/common-definition)
- [Binance errors](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/error-code)
- [Bitget order creation](https://www.bitget.com/docs/catalog/classic-contract-trade/classic-contract-trade)
- [Bitget order query](https://www.bitget.com/api-doc/classic/contract/trade/Get-Order-Details)
- [Bitget contract metadata](https://www.bitget.com/api-doc/classic/contract/market/Get-All-Symbols-Contracts)
- [Bitget account mode](https://www.bitget.com/api-doc/classic/contract/account/Get-Single-Account)
- [Bitget errors](https://www.bitget.com/docs/classic/error-code/restapi)
- [OKX trade and instruments](https://www.okx.com/docs-v5/en/)
- [OKX position mode FAQ](https://www.okx.com/help/api-faq)
- [OKX contract value calculation](https://www.okx.com/en-us/help/how-can-i-do-derivatives-trading-with-the-jupyter-notebook)
