# 공개 요청 한도 (2026-09-22)

채택 기본값 = 확인된 공시 한도를 정수로 내린 80% 이하. `fetch_candles_history`의 429 백오프는 바꾸지 않았다. 빈 concurrency 는 쿼리 동시성 상한이 없다는 뜻이다. 확인된 레이트 한도는 동시 4.

OKX·Bitget 행은 공시값 미확인(벤더 문서 재확인 실패 — 2026-09-22). 그 행의 채택 숫자는 이전 기본값을 유지한 것이고, 재확인된 공시의 80%라고 보지 않는다. OKX 주문은 실계좌 venue라 이전 값 5/s 를 유지한다.

| exchange | market | bucket | limit | period_s | concurrency |
|---|---|---|---|---|---|
| binance | spot | total | 4800 | 60 | |
| binance | spot | order | 80 | 10 | |
| binance | linear | total | 1920 | 60 | |
| binance | linear | order | 240 | 10 | |
| binance | linear | order_minute | 960 | 60 | |
| bybit | spot | query | 480 | 5 | 4 |
| bybit | spot | order | 16 | 1 | 4 |
| bybit | linear | query | 480 | 5 | 4 |
| bybit | linear | order | 8 | 1 | 4 |
| bybit | spot | private | 40 | 1 | 4 |
| bybit | linear | private | 40 | 1 | 4 |
| okx | spot | query | 16 | 2 | 4 |
| okx | spot | order | 5 | 1 | 4 |
| bitget | spot | query | 16 | 1 | 4 |
| bitget | spot | order | 5 | 1 | 4 |
| upbit | spot | query | 24 | 1 | |
| upbit | spot | order | 9 | 1 | |
| bithumb | spot | query | 120 | 1 | 4 |
| bithumb | spot | order | 8 | 1 | 4 |
| korbit | spot | query | 40 | 1 | 4 |
| korbit | spot | order | 24 | 1 | 4 |

OKX 최근 캔들 가중치 0.5, 히스토리 캔들 가중치 1. query 버킷 16/2s 위에서 각각 32/2s, 16/2s. 이 가중치의 공시 근거도 위 OKX 행과 같이 미확인이다.

Upbit 시세 그룹(`market`, `candle`, `ticker`, `trade`, `orderbook`)은 그룹당 8/s 다. Exchange `default`(어댑터 그룹 `query30`)와 그룹 없는 query 는 24/s 다. 그룹이 있기만 하면 8/s 버킷을 붙이던 옛 매핑은 `default` 30/s 엔드포인트까지 8/s 로 묶었다. 시세 그룹을 24/s 로 올리면 공시 10/s 를 넘는다.

## 공시

`*_PUBLISHED_CAP` 는 아래 확인된 행과 같다. 채택 횟수 ≤ floor(0.8 × 공시 횟수), 주기는 같다.

| 거래소 | 버킷 | 공시 | 문서 | 채택 |
|---|---|---|---|---|
| Binance spot | weight | IP REQUEST_WEIGHT 6000/분 | https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md | 4800/60s |
| Binance spot | order | 주문 100/10s | https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md | 80/10s |
| Binance USD-M | weight | IP REQUEST_WEIGHT 2400/분 | https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info | 1920/60s |
| Binance USD-M | order | ORDERS 300/10s | https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info | 240/10s |
| Binance USD-M | order_minute | ORDERS 1200/분 | https://fapi.binance.com/fapi/v1/exchangeInfo | 960/60s |
| Bybit | IP | HTTP IP 600요청/5초 | https://bybit-exchange.github.io/docs/v5/rate-limit | 480/5s |
| Bybit | spot order | 주문 20/s | https://bybit-exchange.github.io/docs/v5/rate-limit | 16/s |
| Bybit | linear order | 주문 10/s | https://bybit-exchange.github.io/docs/v5/rate-limit | 8/s |
| Bybit | private | wallet·order realtime·execution 50/s | https://bybit-exchange.github.io/docs/v5/rate-limit | 40/s |
| OKX | query, order | 공시값 미확인(벤더 문서 재확인 실패 — 2026-09-22) | — | query 16/2s, 주문 5/s |
| Bitget | query, order | 공시값 미확인(벤더 문서 재확인 실패 — 2026-09-22) | — | query 16/s, 주문 5/s |
| Upbit | quotation | `market` `candle` `ticker` `trade` `orderbook` 각 10/s (IP) | https://docs.upbit.com/kr/reference/rate-limits | 그룹당 8/s |
| Upbit | query | Exchange `default` 30/s (포켓) | https://docs.upbit.com/kr/reference/rate-limits | 24/s |
| Upbit | order | 주문 생성 12/s (포켓) | https://docs.upbit.com/kr/reference/rate-limits | 9/s |
| Bithumb | query | Public API 150/s | https://apidocs.bithumb.com/docs/api-%EC%9A%94%EC%B2%AD-%EC%88%98-%EC%A0%9C%ED%95%9C-%EC%95%88%EB%82%B4 | 120/s |
| Bithumb | order | 10/s 초과 시 제한될 수 있음 | https://apidocs.bithumb.com/docs/api-%EC%9A%94%EC%B2%AD-%EC%88%98-%EC%A0%9C%ED%95%9C-%EC%95%88%EB%82%B4 | 8/s |
| Korbit | query | Public API 50/s (IP) | https://docs.korbit.co.kr/ | 40/s |
| Korbit | order | 주문 30/s | https://docs.korbit.co.kr/ | 24/s |
