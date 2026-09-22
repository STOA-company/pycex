"""F5 — 0.4.0 notes and the candle docs name the new surface, in Korean and English."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("\n## [", 1)[0]


def test_changelog_0_4_0_lists_closed_bars_and_listing_time() -> None:
    section = _section((ROOT / "CHANGELOG.md").read_text(), "## [0.4.0]")
    for name in ("closed_only", "listed_at", "fetch_candles_history", "CANDLE_VENUES"):
        assert name in section
    assert "## [0.4.0]" in (ROOT / "docs/changelog.md").read_text()
    docs_section = _section((ROOT / "docs/changelog.md").read_text(), "## [0.4.0]")
    assert "closed_only" in docs_section
    assert "fetch_candles_history" in docs_section


def test_readme_candles_section_is_korean_and_english() -> None:
    text = (ROOT / "README.md").read_text()
    assert "closed_only=True" in text
    assert "fetch_candles_history" in text
    assert "닫힌 봉" in text
    assert "Closed bars" in text
    assert "listed_at" in text


def test_api_docs_describe_listed_at_and_history() -> None:
    models = (ROOT / "docs/api/models.md").read_text()
    exchanges = (ROOT / "docs/api/exchanges.md").read_text()
    assert "listed_at" in models
    assert "closed_only" in exchanges
    assert "fetch_candles_history" in exchanges
