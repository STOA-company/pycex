# 주문 종결·금액 단위 팩 (fix1)
1. 범위: DigitalX, Korbit, Upbit, Bithumb의 주문 반환.
2. 입력 create_order amount 의미는 기존과 같다.
3. 시장가 매수 입력은 quote 금액이다.
4. DigitalX best 매수도 quote 금액이다.
5. 지정가 입력 amount는 base 수량이다.
6. 시장가 매도 입력 amount는 base 수량이다.
7. 출력 Order.amount는 base 수량만 담는다.
8. Order.amount_exact는 알려진 요청 base 수량의 Decimal이다.
9. 금액 매수의 요청 base 수량은 알 수 없다.
10. 이때 amount=0은 호환 sentinel이고 amount_exact=None이다.
11. Order.amount_quote는 요청 quote 예산 Decimal이다.
12. Order.cost는 체결 quote 금액 Decimal이다.
13. cost에는 수수료를 더하지 않는다.
14. Order.filled/filled_exact는 실제 체결 base 수량이다.
15. Order.remaining/remaining_exact는 살아 있는 base 잔량이다.
16. 금액 매수의 열린 base 잔량은 응답에 없으면 None이다.
17. 종결 주문의 살아 있는 잔량은 0이다.
18. 취소된 잔량을 체결 수량으로 더하지 않는다.
19. DigitalX/Korbit는 같은 응답 파서를 쓴다.
20. 거래소 filled는 IOC/시장가/가격보호 잔량 반환도 포함한다.
21. 수량 주문은 qty와 filledQty를 비교한다.
22. 금액 주문은 amt와 filledAmt를 비교한다.
23. filled이며 실제 집행량이 목표보다 작으면 취소 종결이다.
24. 체결이 있으면 기존 Bybit 상태 partiallyFilledCanceled를 쓴다.
25. 체결이 없으면 canceled를 쓴다.
26. 전량이면 filled를 유지한다.
27. 비교 필드 누락 시 소진 여부를 추측하지 않는다.
28. filledAmt 누락이어도 원 filled는 종결이므로 remaining=0이다.
29. Upbit/Bithumb의 done/cancel 상태는 보존하고 remaining=0이다.
30. Upbit/Bithumb의 price형 주문 price는 amount_quote로 옮긴다.
31. 이들 cost는 executed_funds 또는 보고된 trades.funds 합이다.
32. 원 응답은 raw에 보존한다.
33. trader는 amount-filled 대신 명시적 remaining_exact를 소비해야 한다.
34. trader는 금액 매수에 intent.qty를 그대로 보내면 안 된다.
35. 숫자 단위 변환 및 exact 소비는 D1 작업이다; 여기서는 trader 무변경.
36. DigitalX factory는 auth_type/account_seq를 통과시킨다.
37. 깨진 error.code는 ValueError 대신 ExchangeError(unknown)다.
38. 전송 예외 서명 누출은 GET/POST/DELETE 시험으로 보호한다.
39. 시각 보정·trader 사유표·USDT 호가 규칙은 이번 변경에 없다.
