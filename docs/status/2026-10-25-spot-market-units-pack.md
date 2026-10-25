# 현물 시장가 주문 단위 분리 (Bitget / Bybit / OKX)
1. 기준: #43 머지 뒤 main d34657a, 기존 Order 계약을 재사용한다.
2. 범위: 세 거래소 현물 create_order/fetch_order/fetch_open_orders.
3. Binance·GRVT 변경 없음; 무기한 요청·반환 계약은 유지한다.
4. 고객 장면: USDT 금액을 보내고 실제 매수 코인 수량을 확인한다.
5. 고객 장면: 일부 체결 뒤 취소되면 체결 수량만 보유량에 반영한다.
6. 고객 장면: Bybit/OKX에서는 코인 수량을 지정해 시장가 매수한다.
7. 고객 장면: 열린 금액 주문의 미정 base 잔량을 0으로 오해하지 않는다.
8. amount/amount_exact는 base 수량 전용이다.
9. 금액 주문은 amount=0(호환 sentinel), amount_exact=None이다.
10. amount_quote는 요청 quote 금액 Decimal이다.
11. filled_exact는 누적 base 체결 수량 Decimal이다.
12. cost는 수수료를 제외한 집행 quote 금액이다.
13. 열린 금액 주문의 remaining_exact는 None이다.
14. 종결 remaining_exact는 0이며 취소 수량을 체결에 더하지 않는다.
15. 열린 base 주문 잔량은 요청 base−체결 base다.
16. 숫자 크기로 filled 상태를 추론하지 않는다. 원문 상태를 보존한다.
17. Bitget size는 시장가 buy에만 quote, 나머지는 base다.
18. Bitget baseVolume→filled_exact, quoteVolume→cost.
19. Bitget v2의 base 단위 시장가 매수 옵션은 확인하지 못해 추가하지 않는다.
20. Bitget filled가 예산 미소진 부분종결인지 미확인; 상태를 창작하지 않는다.
21. Bybit 현물 시장가에 marketUnit을 항상 명시한다.
22. Bybit 기본 buy=quoteCoin, sell=baseCoin으로 기존 입력 의미를 유지한다.
23. Bybit market_unit="baseCoin"으로 수량 매수를 선택한다.
24. Bybit market_unit은 현물 시장가 및 공식 두 값에만 허용한다.
25. Bybit qty는 marketUnit에 따라 분리하고 cumExecQty는 base다.
26. Bybit cumExecValue→cost, PartiallyFilledCanceled를 그대로 반환한다.
27. OKX의 기존 tgt_ccy 및 tgtCcy 명시 로직을 유지한다.
28. OKX sz는 tgtCcy에 따라 분리하고 accFillSz는 base다.
29. OKX cost는 Decimal(accFillSz)×Decimal(avgPx) 파생값이다.
30. OKX avgPx 누락이면 cost=None; 체결별 quote 합계의 정밀도까지 보장하지 않는다.
31. Bybit/OKX 조회의 빈 단위는 공식 기본 buy quote/sell base로 해석한다.
32. create 응답은 접수 응답이므로 cost·체결 exact·잔량은 미확인이다.
33. trader는 amount-filled 계산 대신 remaining_exact를 우선 소비해야 한다.
34. trader는 amount=0을 주문 실패로 판정하지 말고 amount_quote/cost를 소비해야 한다.
35. trader의 수량 의도는 명시적 base 옵션 또는 기존 IOC 지정가 계약을 써야 한다.
36. trader 변경·핀 갱신·운영 경로 검증은 이 작업에 없다.
37. 회귀는 tests/exchanges/test_spot_market_units.py; 외부 소켓 차단, HTTP mock.
38. Bitget 공식: https://www.bitget.com/docs/catalog/classic-spot-trade/classic-spot-trade (size/baseVolume/quoteVolume).
39. Bybit 공식: https://bybit-exchange.github.io/docs/v5/order/create-order 및 open-order (marketUnit/qty/cumExecQty/cumExecValue).
40. OKX 공식: https://app.okx.com/docs-v5/en/ (tgtCcy/sz/accFillSz/avgPx/state).
