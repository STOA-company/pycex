"""A2-7 — 공개 레포에 비밀값이 섞여 들어가지 않았는지 훑는다.

`Jaeminyx-Stoa/pycex` 는 PUBLIC 이다. 기록 픽스처는 **실계좌 6회 실행**에서
뽑았으므로, 원장에 인증 헤더가 남지 않았다는 말만 믿고 넘어가지 않는다.

    python scripts/secret_scan.py        # 0 = 깨끗함, 1 = 걸린 것 있음

일부러 심은 가짜 비밀값을 잡는지까지 테스트가 검사한다
(`tests/test_public_repo_secrets.py`) — 판정기도 틀린다.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 훑는 곳. 코드가 아니라 «값이 실려 나가는» 표면이다.
TARGETS = [
    ROOT / "tests" / "fixtures",
    ROOT / "CHANGELOG.md",
    ROOT / "README.md",
    ROOT / "README.ko.md",
    ROOT / "docs",
]

#: 문서의 자리표시자. 값이 아니라 «여기에 네 것을 넣어라»는 안내다.
PLACEHOLDERS = {
    "key", "secret", "pass", "your_key", "your_secret", "your_passphrase",
    "okx_passphrase", "test", "changeme", "xxx", "...", "none", "null",
    "your_api_key", "your-api-key", "api_key", "api-key", "passphrase",
}

#: 비밀값이 실릴 만한 필드 이름.
CREDENTIAL_FIELDS = re.compile(
    r"""["']?(?:
        ok[-_]access[-_](?:key|sign|passphrase)
      | x[-_]mbx[-_]apikey
      | api[-_]?(?:key|secret)
      | access[-_]?(?:key|token)
      | secret[-_]?key
      | passphrase
      | private[-_]?key
      | authorization
      | acct_?id | account_?id | \buid\b | sub_?acct
    )["']?[ \t]*[:=][ \t]*["']?([^"'\s,;)}\]`]{1,200})["']?""",
    re.IGNORECASE | re.VERBOSE,
)

#: 짧은 값은 자리표시자이기 쉽다. 진짜 키는 길고 무작위다.
HIGH_ENTROPY = re.compile(r"[A-Za-z0-9+/=]{28,}")


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("<>{}[]").lower()
    return v in PLACEHOLDERS or v.replace("_", "").replace("-", "").isalpha() and len(v) <= 12


def _files() -> list[Path]:
    out: list[Path] = []
    for t in TARGETS:
        if t.is_dir():
            out.extend(p for p in t.rglob("*") if p.is_file() and p.suffix in {".json", ".md", ".txt"})
        elif t.is_file():
            out.append(t)
    return sorted(out)


def scan_text(text: str, *, label: str = "<text>") -> list[str]:
    """걸린 것들을 사람이 읽을 수 있는 줄로 돌려준다. **값은 찍지 않는다.**"""
    hits: list[str] = []
    for m in CREDENTIAL_FIELDS.finditer(text):
        value = m.group(1)
        if _is_placeholder(value):
            continue
        line = text[: m.start()].count("\n") + 1
        hits.append(f"{label}:{line} 자격증명형 필드에 실제 값처럼 보이는 것 (길이 {len(value)})")
    for m in HIGH_ENTROPY.finditer(text):
        token = m.group(0)
        if "/" in token and token.count("/") >= 2:  # URL 경로
            continue
        if text[m.end() : m.end() + 2] == '":':  # JSON 키 이름(긴 camelCase 필드)
            continue
        if not (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
            continue  # 숫자만·글자만 = 식별자나 수치, 무작위 키가 아니다
        line = text[: m.start()].count("\n") + 1
        hits.append(f"{label}:{line} 무작위로 보이는 긴 문자열 (길이 {len(token)})")
    return hits


def scan_repo() -> list[str]:
    hits: list[str] = []
    for path in _files():
        hits.extend(scan_text(path.read_text(errors="replace"), label=str(path.relative_to(ROOT))))
    return hits


def main() -> int:
    files = _files()
    hits = scan_repo()
    print(f"훑은 파일 {len(files)}개 (tests/fixtures · CHANGELOG.md · README.md · docs/)")
    if not hits:
        print("걸린 것 0건 — 키·시크릿·계좌 식별자 없음")
        return 0
    for h in hits:
        print("  " + h)
    print(f"걸린 것 {len(hits)}건")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
