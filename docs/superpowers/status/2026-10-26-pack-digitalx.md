# DigitalX adapter pack — 2026-10-26
범위: pycex Digital X Open API v2 로컬 계약 구현.
기준 커밋: d0c502d804b8.
정본: tests/fixtures/digitalx/CONTRACT-2026-10-26.md.
구현: src/pycex/exchanges/digitalx.py.
factory: digitalx → DigitalX.
공식 문서: https://docs.digitalx.miraeasset.com/llms.txt.
개발자센터 llms.txt는 SPA HTML 응답.
공식 introduction: 이전 이름 Korbit, 같은 서비스.
공개 시세는 Korbit 구현 재사용.
private REST는 DigitalX 자체 서명·오류분류.
HMAC-SHA256 hex 지원.
ED25519 PEM/Base64 지원, cryptography optional extra.
실키·실주문·운영 접속 없음.
공식 sandbox는 별도 upstream 경로에 다운로드.
공식 sandbox 소스는 재배포/커밋하지 않음.
Node24 필요, loopback 9999만 사용.
Node outbound 네트워크는 preload로 금지.
pytest는 기존 소켓 네트워크 가드 사용.
공식 샌드박스 5쌍 × 18 주문 = 90 조합.
문서 기반 4쌍 × 18 주문 = 72 상태대사 걸음.
USDT quote 마켓은 합성 시험이며 실제 상장 미확인.
지정가: gtc/ioc/fok/po.
시장가: ioc만.
best: gtc/ioc/fok/po + bestNth1..5.
IOC/FOK order_type 별칭은 지정가로 변환.
금액 매수는 amt, 수량 주문은 qty.
Decimal 입력은 float를 거치지 않고 문자열 전송.
출력 exact 필드 추가, 기존 float 필드는 유지.
주문가능금액 exact 메서드 제공.
동일 인스턴스 같은 client_order_id 재전송0.
재시작 멱등 원장은 호출자 소유.
주문/취소 자동 재시도 없음.
5xx/미등록코드/유실은 unknown 계열.
취소 승인 뒤 GET으로 실제 상태 조회.
IOC filled는 전량 체결을 뜻하지 않을 수 있음.
계약 시험: tests/exchanges/test_digitalx.py.
공식 시험: tests/exchanges/test_digitalx_sandbox.py.
부수 수정: 기존 기준판 lint 포맷과 TYPE_CHECKING 주석만.
트레이더 핀·능력행·원장 연결·운영 검증은 D1/메인 소유.
