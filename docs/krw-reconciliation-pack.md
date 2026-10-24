# W6 KRW order reconciliation (2026-10-24)

- Baseline: trader pycex pin `10f9910cf8b16e77653ff789f1723f92f8f2902f`.
- Reuses the KRW parser/cancellation delta and regression cases from pycex PR #28 (`2dc67e3`); no unrelated venue code.
- Interface remains `fetch_order(None, symbol, client_order_id=key)`; exactly one lookup key is required.
- Create uses the same optional `client_order_id` as before; the SDK never invents or retries an order key.
- Upbit sends `identifier`; Bithumb sends `client_order_id`; Korbit sends `clientOrderId` plus native `symbol`.
- Korbit lookup applies the same documented `[0-9a-zA-Z.:_-]{1,36}` validation already used on creation.
- Lookups preserve response market identity; mismatches are visible to the caller rather than replaced by its requested symbol.
- Missing/invalid/null lookup market becomes an empty symbol. Sparse create/cancel ACKs retain caller context.
- Unrecognized, missing, empty or null states return `status="unknown"`; raw fields remain available.
- Bithumb and Korbit sparse cancel ACKs are `unknown`; callers must query the result before treating cancellation as final.
- Upbit cancel retains the actual returned state and filled quantity; it does not force cancellation.
- `Order.average_price: float | None = None` is optional and separate from submitted `price`.
- Upbit average uses complete `trades[].funds / volume` totals only when summed volume equals `executed_volume`.
- Bithumb average uses `executed_funds / executed_volume`, with complete trades as a fallback when funds are absent.
- Korbit average uses positive finite `avgPrice`, falling back to `filledAmt / filledQty` only when avgPrice is absent.
- No fills, incomplete/nonfinite/invalid execution amounts => no average; the limit price is never an execution average.
- Korbit `filled` may close with less than requested quantity (IOC/price protection); `filledQty` is preserved verbatim numerically.
- Not-found lookup remains `OrderNotFoundError`; it is not proof that resending an order is safe.

## Official public documentation checked 2026-10-24 UTC

- [Upbit lookup](https://docs.upbit.com/kr/reference/get-order): UUID/identifier selection; [official response schema](https://global-docs.upbit.com/reference/get-order): state, market, executed volume and trade funds/volume.
- [Upbit cancel](https://docs.upbit.com/kr/reference/cancel-order): cancel request and returned order object.
- [Bithumb lookup](https://apidocs.bithumb.com/reference/개별-주문-조회): uuid/client_order_id, actual market, state and executed_funds/executed_volume.
- [Bithumb cancel acceptance](https://apidocs.bithumb.com/reference/주문-취소-접수): sparse order_id/client_order_id/created_at response, no final status.
- [Korbit trading](https://docs.korbit.co.kr/llms/en/rest_api/trading.md) redirects to [current official document](https://docs.digitalx.miraeasset.com/llms/en/rest_api/trading.md): lookup key, regex, quantities, avgPrice, statuses and cancellation acceptance.
- Korbit canceled/expired orders become unqueryable after approximately three days. This does not authorize key reuse or order retry.
- No host migration is included; authenticated Korbit compatibility with its documentation redirect is unverified.
- HTML pages/search schemas were readable; direct Upbit/Bithumb `.md` retrieval returned 403 and was not treated as verification.

## Evidence boundary

- `tests/exchanges/test_krw_reconciliation.py` walks fake HTTP timeout → same-key lookup, unknown/not-found/mismatched markets, partial fills, sparse cancel → followup, and invalid averages.
- Existing KRW adapter tests and model tests cover signatures, key validation and the reused PR #28 examples.
- Pytest excludes `live`; `tests/conftest.py` blocks non-loopback sockets for all selected tests.
- Source-reverted independent worktree must fail the new regression cases; command/output/HEAD evidence is in the W6 run folder.
- Real account creation, fills, remainder and cancellation: **안 걸어 봄**. No keys, exchange API, dev/prod deploy, push or merge.
