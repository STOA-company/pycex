"""A2-4 — CHANGELOG 가 코드에 대해 하는 말이 사실인지 검사한다.

공개 레포다. CHANGELOG 한 줄이 틀리면 그 줄을 믿고 붙인 사람의 돈이 나간다.
0.3.0 은 «rebuilds client, transport and rate limiter» 라고 적었지만
리미터를 재빌드하는 것이 바로 A2-2 가 고친 결함이었다.
"""

from __future__ import annotations

import inspect
import pathlib

from pycex.http import HTTPClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()


# ── A2-4: CHANGELOG 정정 ──


def test_changelog_no_longer_claims_the_rate_limiter_is_rebuilt() -> None:
    """거짓 문장 제거 — 리미터는 재빌드되지 않는다(그게 A2-2 의 수정이다)."""
    assert "rebuilds client, transport and rate limiter" not in CHANGELOG


def test_changelog_records_the_injected_transport_fix() -> None:
    """A2-1 은 공개본 사용자에게 «주입 방식이 바뀌었다»는 뜻이므로 반드시 적힌다."""
    assert "set_transport_factory" in CHANGELOG


def test_changelog_records_the_rate_limiter_lifetime_fix() -> None:
    """A2-2 도 마찬가지 — 실측 수치와 함께 남긴다."""
    assert "rate limiter is no longer reset" in CHANGELOG.lower()


def test_unreleased_records_conservative_constant_removal() -> None:
    """The shared 5/s names are a breaking removal, and they are gone from the package.

    Written while the note still sat under Unreleased (PR #11); the 0.4.1 cut moved it
    into a dated section, so this checks the CHANGELOG as a whole rather than one heading.
    """
    assert "Removed: `CONSERVATIVE_RATE_LIMIT`, `CONSERVATIVE_MAX_INFLIGHT` (breaking)" in CHANGELOG
    package = "\n".join(path.read_text() for path in (ROOT / "src").rglob("*.py"))
    assert "CONSERVATIVE_RATE_LIMIT" not in package
    assert "CONSERVATIVE_MAX_INFLIGHT" not in package


# ── A2-4 우회 거부 ──


def test_bypass_1_no_doc_anywhere_repeats_the_false_claim() -> None:
    """우회 ① — CHANGELOG 에서만 지우고 README·docs 에 남겨두는 것을 막는다."""
    offenders = []
    for path in [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]:
        if "rebuilds client, transport and rate limiter" in path.read_text():
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_bypass_2_documented_injection_path_actually_exists() -> None:
    """우회 ② — 문서에만 있고 코드에 없는 API 를 적어두는 것을 막는다."""
    assert callable(HTTPClient.set_transport_factory)
    assert not isinstance(inspect.getattr_static(HTTPClient, "_client"), staticmethod)
    prop = inspect.getattr_static(HTTPClient, "_client")
    assert isinstance(prop, property)
    assert prop.fset is None, "문서는 «읽기 전용»이라고 말한다 — 세터가 있으면 거짓이다"
