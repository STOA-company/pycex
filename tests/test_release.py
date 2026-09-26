"""A-10 — 릴리스 노트가 버전 표기와 **한 벌**로 유지된다.

🚨 공개본과 사설 포크가 갈리는 사고는 «버전만 올리고 문서를 안 쓰는» 한 걸음에서
시작한다. 버전 표기 세 곳(pyproject·__init__·CHANGELOG)이 어긋나면 빨강이다.
0.3.0 에서 더한 공개 API 는 그 절에 남아 있다.

이 스위트에는 제품 전용 분기가 없다 — 09-10 강건화(A-1~A-9)는 전부 공개본에
그대로 들어간다.
"""

from __future__ import annotations

import pathlib
import re

import pycex

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = "0.4.4"


def test_package_version() -> None:
    assert pycex.__version__ == VERSION


def test_pyproject_version_matches_the_package() -> None:
    text = (ROOT / "pyproject.toml").read_text()
    assert re.search(rf'^version = "{re.escape(VERSION)}"$', text, re.M)


def test_changelog_documents_this_version() -> None:
    text = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{VERSION}]" in text, "버전을 올렸으면 CHANGELOG 에 적는다"


def test_changelog_section_is_not_empty() -> None:
    """우회 1 — 제목만 있는 빈 릴리스 노트를 «적었다»고 하지 않는다."""
    text = (ROOT / "CHANGELOG.md").read_text()
    section = text.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
    assert len(section.strip().splitlines()) > 10
    assert any(h in section for h in ("### Added", "### Changed", "### Fixed", "### Removed"))
    released = text.split("## [0.3.0]", 1)[1].split("\n## [", 1)[0]
    assert "### Added" in released and "### Fixed" in released


def test_the_new_public_surface_is_documented() -> None:
    """우회 2 — 0.3.0 에서 더한 공개 API 가 릴리스 노트에 빠지지 않는다."""
    section = (ROOT / "CHANGELOG.md").read_text().split("## [0.3.0]", 1)[1].split("\n## [", 1)[0]
    for name in (
        "client_order_id",
        "set_leverage",
        "fetch_available_balance",
        "fetch_account_config",
        "SettlementPendingError",
        "margin_mode",
        "tgt_ccy",
        "reduce_only",
    ):
        assert name in section, f"CHANGELOG 에 {name} 이 없다"


def test_settlement_pending_error_is_exported() -> None:
    """우회 3 — 새 예외를 내부에만 두고 공개본에서 못 쓰게 하지 않는다."""
    assert "SettlementPendingError" in pycex.__all__
    assert pycex.SettlementPendingError.retryable is True


def test_no_product_specific_branch_in_the_adapter() -> None:
    """우회 4 — 공개 SDK 안에 «우리 제품일 때만» 이 들어오는 것을 막는다."""
    source = (ROOT / "src" / "pycex" / "exchanges" / "okx.py").read_text().lower()
    for needle in ("quantus", "vibe", "stoa", "internal_only"):
        assert needle not in source, f"공개본에 제품 전용 표식이 있다: {needle}"
