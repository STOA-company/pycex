# 원화 현물 공식 계약 — 2026-10-04

검토 범위는 공개 공식 문서 및 무인증 마켓 규칙 GET뿐이다. 인증 API·계좌·주문·취소·운영 VM·DB는 호출하지 않았다. 문서의 계약 확인과 실계좌 사용 가능 여부는 별개다.

## 적용 결론

| 거래소 | 주문·조회·취소 계약 | 가격·최소금액 | 지정가 수량 단위 | 이번 Trader 행 |
| --- | --- | --- | --- | --- |
| 업비트 | 확인(U1–U3) | 확인(U4, P1) | **미확인**: 공식 지정가 입력 자릿수 없음 | preview/simulated 유지 |
| 빗썸 | 확인(B1–B3) | 확인(B5) | **0.00000001**, 8자리(B5) | 기존 live 게이트를 모두 통과할 때만 실주문 가능 |
| 코빗 | 공개 문서 확인(K1); 문서 리다이렉트 아래 참조 | BTC 확인(K2, P2–P3) | **미확인**: currencyPairs·주문 문서에 qty step 없음 | preview/simulated 유지 |

업비트 FAQ의 시장가 체결 8자리 예시는 지정가 요청 수량 규칙으로 확대하지 않는다. 코빗 docs.korbit.co.kr은 접근 시 docs.digitalx.miraeasset.com으로 리다이렉트되며 현재 예시 서버는 api.digitalx.miraeasset.com이다. 기존 api.korbit.co.kr의 공개 규칙은 정상 응답했지만 **인증 API 서버의 호환성은 미확인**이다. 이번 작업에서 호스트·키·권한을 추정해 변경하지 않았다.

## 요청·응답 계약 표

| 항목 | 업비트 | 빗썸 | 코빗 |
| --- | --- | --- | --- |
| 지정가 매수·매도 | POST /v1/orders, JSON: market=KRW-BTC, side=bid/ask, ord_type=limit, volume=BTC 수량 문자열, price=KRW 가격 문자열(U1) | POST /v2/orders, JSON: market=KRW-BTC, side=bid/ask, order_type=limit, volume=BTC 수량 문자열, price=KRW 가격 문자열(B1) | POST /v2/orders, 서명 파라미터: symbol=btc_krw, side=buy/sell, orderType=limit, qty=BTC 수량 문자열, price=KRW 가격 문자열(K1 POST 절) |
| client id | identifier 선택; 계정 전체 고유, 64자 이하, 과거 사용값 재사용 불가. 기존 SDK의 url-safe 제한은 더 좁은 입력 검증이지 공식 제한이라고 주장하지 않음(U1 identifier 절) | client_order_id 선택; 1–36자 영문·숫자·_·-, 고유값(B1 Request) | clientOrderId 선택; [0-9a-zA-Z.:_-]{1,36}; expired/canceled는 종료 약 3일 후 조회 불가·재사용 가능(K1 POST 절). Trader의 키 재사용/재주문 금지는 완화하지 않음 |
| 접수 응답 | uuid 및 주문 상세(U1 Response) | order_id, market, side, order_type, created_at. 상태·체결수량 없음(B1 Response) | success=true, data.orderId(K1 POST Response) |
| 주문번호 조회 | GET /v1/order?uuid=…(U2) | GET /v1/order?uuid=…(B2) | GET /v2/orders?symbol=btc_krw&orderId=… + 서명(K1 GET 절) |
| client id 조회 | GET /v1/order?identifier=…; 두 키 동시 요청 시 uuid 우선(U2). SDK는 정확히 한 키만 허용 | GET /v1/order?client_order_id=…; 두 키 동시 요청 시 uuid 우선(B2). SDK는 정확히 한 키만 허용 | GET /v2/orders?symbol=btc_krw&clientOrderId=… + 서명(K1). SDK는 정확히 한 키만 허용 |
| 상태 | wait=체결 대기, watch=예약 주문 대기, done=체결 완료, cancel=취소(U2 Response schema) | wait=체결 대기, watch=주문 대기, done=주문 처리 완료, cancel=취소. done 문구만으로 전량체결을 추정하지 않음(B2 schema/조건부 주문 주의) | pending=접수 대기, open=전량 미체결, partiallyFilled=부분체결, filled=실행 종료(IOC/가격보호로 잔량 반환 시 부분 실행도 가능), canceled=취소, partiallyFilledCanceled=부분체결 후 잔량 취소, expired=접수 실패(K1 GET Response) |
| 부분체결 판정 | executed_volume>0, remaining_volume>0인 wait. 상태와 수량 함께 사용 | executed_volume>0, remaining_volume>0인 wait. 상태와 수량 함께 사용 | partiallyFilled 및 filledQty/qty; filled도 반드시 실제 수량 확인 |
| 체결 수량 | executed_volume=기초자산, remaining_volume=잔량, prevented_volume=SMP 방지량(U2) | executed_volume=기초자산, remaining_volume=잔량(B2) | filledQty=기초자산, filledAmt=견적통화, qty=주문 기초자산(K1) |
| 평균 체결가 | 전용 평균 칸 없음. 완전한 trades[].funds 합계 / trades[].volume 합계(합계가 executed_volume과 같을 때); limit price를 평균으로 쓰지 않음(U2 trades) | executed_funds / executed_volume; 둘 다 공식 누적 칸. 미체결은 평균 없음(B2) | avgPrice(선택), filledAmt/filledQty가 실행 단위(K1). 기존 raw 추출 경로 유지 |
| 취소 | DELETE /v1/order?uuid=… 또는 identifier=…; 두 키면 uuid 우선(U3) | DELETE /v2/order?order_id=… 또는 client_order_id=…(B3) | DELETE /v2/orders?symbol=…&orderId=… 또는 clientOrderId=… + 서명(K1 DELETE 절) |
| 취소 완료 판단 | 취소 응답/후속 조회의 공식 상태·실제 체결량으로 대사. ACK만으로 잔량 체결·취소 완료를 만들지 않음 | sparse 응답에는 상태·수량 없음: unknown, 후속 GET /v1/order로 대사 | success=true ACK는 최종 상태 아님. 후속 주문 조회로 대사 |
| 최소 금액 | 5,000 KRW(U4 최소 주문 가능 금액) | 5,000 KRW(B5 거래 정책); 지정가 최대 5,000,000,000 KRW | BTC 5,000 KRW(P2 minOrderValue); 다른 종목은 currencyPairs 값을 읽음 |
| 수량 자릿수 | **미확인**(U1 volume=NumberString, U5는 시장가 예시만) | 최소 주문수량 단위 0.00000001(B5) | **미확인**(K1 qty=string, P2 수량단위 칸 없음) |
| IP·API 키 권한 | 공개 IP allowlist 등록 필요(U6). 주문조회=주문조회 권한(U2), 생성·취소=주문하기 권한(U1/U3) | 접근허용 IP 등록, API 활성 항목 자산조회·주문조회·주문하기(B4). 실제 키의 활성 상태는 **미확인** | 현재 공식 포털은 권한·IP allowlist 설정(K3), 조회=readOrders, 생성·취소=writeOrders(K1). 기존 코빗 키의 새 호스트 호환성은 **미확인** |

## 가격 호가 단위

구간은 하한 이상, 다음 하한 미만이다. 단위는 KRW다. 업비트 U4, 빗썸 B5, 코빗 BTC는 P3 응답을 직접 대조했다. 코빗은 정적 공통표로 보장하지 않고 종목별 tickSizePolicy를 읽는다.

| 하한 | 업비트 | 빗썸 | 코빗 BTC(관측) |
| ---: | ---: | ---: | ---: |
| 0 | 0.00000001 | 0.0001 | 0.0001 |
| 0.00001 | 0.0000001 | 위와 동일 | 위와 동일 |
| 0.0001 | 0.000001 | 위와 동일 | 위와 동일 |
| 0.001 | 0.00001 | 위와 동일 | 위와 동일 |
| 0.01 | 0.0001 | 위와 동일 | 위와 동일 |
| 0.1 | 0.001 | 위와 동일 | 위와 동일 |
| 1 | 0.01 | 0.001 | 0.001 |
| 10 | 0.1 | 0.01 | 0.01 |
| 100 | 1 | 1 | 0.1 |
| 1,000 | 1 | 1 | 1 |
| 5,000 | 5 | 5 | 5 |
| 10,000 | 10 | 10 | 10 |
| 50,000 | 50 | 50 | 50 |
| 100,000 | 100 | 100 | 100 |
| 500,000 | 500 | 500 | 500 |
| 1,000,000 | 1,000 | 1,000 | 1,000 |
| 2,000,000 | 1,000 | 위와 동일 | 위와 동일 |

1만원은 주문 명목금액 한도다. 기존 Decimal 수량 산정에서 가격을 해당 호가로 내리고 수량을 1e-8로 내려 계산한 price×qty가 5,000원 이상·10,000원 이하임을 확인했다. 수수료 정책이나 계좌 잔액의 실측 증거는 없으며 이번 작업은 이를 변경하지 않는다.

## 출처·접근 UTC·해당 절

원본 전문 및 접근 기록은 발주 폴더 sources/와 sources/access.jsonl에 저장했다. 아래 링크는 실제로 읽은 공식 URL이다. 문서 403 응답은 성공 근거에 포함하지 않으며 B5는 공식 help-center JSON API의 article.body를 읽었다.

| ID | 공식 출처 | 접근 UTC(2026-10-04) | 읽은 절 |
| --- | --- | --- | --- |
| U1 | https://docs.upbit.com/kr/reference/new-order.md | 02:21:46.362744 | 지정가·identifier·API Key Permission·Request/Response schema |
| U2 | https://docs.upbit.com/kr/reference/get-order.md | 02:21:45.821674 | uuid/identifier·API Key Permission·Response(state/volume/trades) |
| U3 | https://docs.upbit.com/kr/reference/cancel-order.md | 02:21:47.035193 | uuid/identifier·API Key Permission·Response |
| U4 | https://docs.upbit.com/kr/docs/krw-market-info.md | 02:21:47.388175 | 호가 표시 단위·최소 주문 가능 금액 |
| U5 | https://docs.upbit.com/kr/docs/faq-order.md | 02:21:47.890954 | 시장가 체결 잔량·8자리 예시 |
| U6 | https://docs.upbit.com/kr/docs/api-key.md | 02:21:48.263576 | 권한·허용 IP 등록·IP FAQ |
| U7 | https://docs.upbit.com/kr/docs/limit-bid-order-creation.md | 02:21:48.732633 | 지정가 매수 예시; 지정가 수량 step 없음 |
| B1 | https://apidocs.bithumb.com/reference/주문-요청.md | 02:21:49.706182 | /v2/orders Request/Response schema |
| B2 | https://apidocs.bithumb.com/reference/개별-주문-조회.md | 02:21:50.043637 | /v1/order query·Response schema·조건부 주문 주의 |
| B3 | https://apidocs.bithumb.com/reference/주문-취소-접수.md | 02:21:50.423365 | /v2/order query·sparse Response |
| B4 | https://apidocs.bithumb.com/docs/빠른-시작-가이드.md | 02:21:50.681258 | Open API 키 생성·활성 항목·허용 IP |
| B5 | https://support.bithumb.com/api/v2/help_center/ko/articles/51036972377241.json | 02:22:41.135922 | article.body 원화 마켓 거래 정책: 금액·수량·가격 구간표; updated_at=2026-08-11T08:37:06Z |
| K1 | https://docs.korbit.co.kr/llms/en/rest_api/trading.md → https://docs.digitalx.miraeasset.com/llms/en/rest_api/trading.md | 02:20:01.315015 | GET/POST/DELETE /v2/orders Request/Response·Required Permissions |
| K2 | https://docs.korbit.co.kr/llms/en/rest_api.md → https://docs.digitalx.miraeasset.com/llms/en/rest_api.md | 02:21:51.014949 | currencyPairs·tickSizePolicy |
| K3 | https://docs.korbit.co.kr/llms/en/introduction.md → https://docs.digitalx.miraeasset.com/llms/en/introduction.md | 02:21:50.911028 | API Key permissions/IP allowlists·현재 API portal |
| P1 | https://api.upbit.com/v1/orderbook/instruments?markets=KRW-BTC | 02:32:03.216233 | HTTP200 KRW-BTC tick_size=1000, 무인증 GET |
| P2 | https://api.korbit.co.kr/v2/currencyPairs | 02:32:03.260990 | HTTP200 btc_krw minOrderValue=5000, 무인증 GET |
| P3 | https://api.korbit.co.kr/v2/tickSizePolicy?symbol=btc_krw | 02:32:03.302932 | HTTP200 btc_krw tickSizePolicy, 무인증 GET |

## 구현 계약·봉인

pycex의 세 거래소 create/cancel/fetch_order/rules/ticker 구현은 이미 존재했다. 공식 표와 맞는 서명·요청·호가 코드는 재사용했다. 이번 변경은 공식 체결 평균·응답 종목 검증·미등록 상태 unknown·빗썸 sparse 취소 ACK·코빗 조회 client id 검증뿐이다. Trader는 실제 조회의 주문번호·종목·필수 수량·상태가 불명확하면 raw 체결량을 버리고 unknown으로 유지한다. 재조회와 재주문을 혼동하지 않으며 timeout 뒤 새 주문도 prior_unconfirmed로 차단한다.

원화 현물 시장가 금액 주문은 기존 D9 krw_coin_limit_only를 유지하고 adapter에서도 transport 전에 막는다. KRW는 Q_INTENT_MAX_PER_ORDER_KRW·Q_INTENT_MAX_DAILY_KRW·LIVE_MAX_NOTIONAL_KRW_PER_ORDER·LIVE_MAX_NOTIONAL_KRW_PER_DAY를 기존 이름 그대로 사용한다. USDT 캡을 KRW에 대입하거나 FX로 바꾸지 않는다. 계좌·symbol·시간·limit·slippage·dual unseal·reserve-before-place·seal은 기존 첫 OKX 현물 경로를 유지한다. 거래소 row 활성화는 게이트의 자동 해제가 아니다.

실제 고객의 주문·체결·잔량·취소 장면은 **안 걸어 봄**. 오프라인 테스트는 공식 응답 fixture에 의한 SDK/Trader 연결 증거이며 실계좌 증거가 아니다. 독립 리뷰·공유 dev 반영·실거래는 메인이 대표 승인 뒤 수행한다.
