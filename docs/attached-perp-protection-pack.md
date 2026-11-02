# Attached perpetual protection contract (2026-11-02)
1. Scope: one-way USDT perpetual entries on Binance, Bitget Classic and Bybit.
2. Existing OKX attachAlgoOrds remains unchanged.
3. create_order adds keyword-only tp_px/sl_px, matching OKX's entry function shape.
4. Spot attachments are rejected before sending an entry.
5. reduce_only entries with attachments are rejected before entry.
6. Trigger prices must be positive and finite.
7. No exchange/demo calls are used to verify this contract.
8. Binance create_order stores pending_fill metadata after the parent receipt.
9. Bitget Classic create_order does the same, deliberately avoiding unowned presets.
10. The trader must persist the parent and a child-write claim before reconciliation.
11. ensure_attached_exits accepts symbol, side and parent_order_id.
12. It also accepts client_order_id, filled_qty, tp_px, sl_px and existing state.
13. Only confirmed executed quantities may be passed as filled_qty.
14. Each new fill delta adds one quantity-specific leg per requested trigger.
15. Old legs remain while later fill deltas are protected.
16. Binance uses POST /fapi/v1/algoOrder, algoType=CONDITIONAL.
17. Binance SL uses STOP_MARKET; TP uses TAKE_PROFIT_MARKET.
18. Binance sets opposite side, reduceOnly=true and workingType=CONTRACT_PRICE.
19. Quantity and closePosition are never mixed.
20. Bitget uses POST /api/v2/mix/order/place-tpsl-order.
21. loss_plan/profit_plan are closing plans attached to the held side.
22. Bitget one-way holdSide is the original entry side, buy or sell.
23. Bitget sets triggerType=fill_price and executePrice=0.
24. Bitget child clientOid ownership is persisted; prices never establish ownership.
25. Bybit attaches takeProfit/stopLoss to POST /v5/order/create.
26. Bybit sets tpslMode=Partial and Market TP/SL with LastPrice trigger sources.
27. Native Bybit Partial protection follows actual fills; trader confirms the snapshot.
28. Bybit parentOrderLinkId explicitly identifies attached futures children.
29. attached_algo_orders returns normalized IDs, trigger Decimals and quantity Decimals.
30. Bitget and Bybit snapshots walk their documented pagination cursors.
31. Snapshot parsing failures are errors, never evidence that no protection exists.
32. cancel_algo_orders cancels only the IDs requested, never all symbol orders.
33. Bitget cancellation inspects successList and failureList per requested ID.
34. A child POST acknowledgement returns pending_confirmation and attached=false.
35. Only a later exchange snapshot may establish active attachment in the trader.
36. A failed child records status/reason while retaining previously acknowledged legs.
37. Network/duplicate/unknown writes return unknown and are never blindly retried.
38. Deterministic child IDs use parent identity, leg kind and cumulative filled quantity.
39. Bitget UTA isolated-margin rejection remains; new UTA attachments reject before entry.
40. Offline tests: tests/exchanges/test_attached_perp_exits.py; evidence: .codex-out/.

Official contracts:
- Binance: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade
- Bitget Classic: https://www.bitget.com/docs/catalog/classic-contract-plan/classic-contract-plan
- Bitget UTA: https://www.bitget.com/legacy-docs/uta/strategy/Place-Strategy-Order
- Bybit create: https://bybit-exchange.github.io/docs/v5/order/create-order
- Bybit read: https://bybit-exchange.github.io/docs/v5/order/open-order
