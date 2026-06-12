"""Unit tests for the Highlightly ingestion layer (offline / mocked HTTP).

Verify deep-stat extraction across both payload shapes (per-team blocks and
home/away rows), score parsing, the xG anomaly hook, error handling, and Node
A's source routing + mock fallback. Live calls need an API key, so the HTTP
transport is mocked.
"""

from __future__ import annotations

import httpx
import pytest

from app import graph, highlightly
from app.highlightly import HighlightlyError, _build_match_stats, _num, _parse_score


# --- A Highlightly-style match payload with embedded per-team statistics -----
MATCH = {
    "id": 99001,
    "round": "Group Stage",
    "league": {"name": "World Cup", "season": 2026},
    "homeTeam": {"id": 1, "name": "Germany"},
    "awayTeam": {"id": 2, "name": "Japan"},
    "state": {"description": "Finished", "score": {"current": "1 - 2"}},
    # Real Highlightly shape: per-team blocks, `displayName` labels, possession
    # as a 0-1 fraction, and no single "total shots" (sum the components).
    "statistics": [
        {"team": {"id": 1, "name": "Germany"}, "statistics": [
            {"displayName": "Possession", "value": 0.74},
            {"displayName": "Expected Goals", "value": 2.20},
            {"displayName": "Shots on target", "value": 9},
            {"displayName": "Shots off target", "value": 5},
            {"displayName": "Blocked shots", "value": 3}]},
        {"team": {"id": 2, "name": "Japan"}, "statistics": [
            {"displayName": "Possession", "value": 0.26},
            {"displayName": "Expected Goals", "value": 1.40},
            {"displayName": "Shots on target", "value": 5},
            {"displayName": "Shots off target", "value": 4},
            {"displayName": "Blocked shots", "value": 3}]},
    ],
}


def _mock_client(payload, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return lambda headers, timeout: httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("74%", 74), ("2.20", 2.2), ("8/10 (80%)", 80.0), (5, 5), ("", None),
])
def test_num(raw, expected):
    assert _num(raw) == expected


def test_parse_score_from_current_string():
    assert _parse_score(MATCH) == (1, 2)


def test_build_extracts_deep_stats():
    stats = _build_match_stats(MATCH, MATCH["statistics"])
    assert stats["home_team"] == "Germany" and stats["away_team"] == "Japan"
    assert stats["final_score"] == "1-2"
    assert stats["possession_pct"] == {"Germany": 74, "Japan": 26}
    assert stats["xg"] == {"Germany": 2.2, "Japan": 1.4}
    assert stats["shots"] == {"Germany": 17, "Japan": 12}  # 9+5+3, 5+4+3
    assert stats["shots_on_target"] == {"Germany": 9, "Japan": 5}
    assert stats["data_source"] == "highlightly"


def test_anomaly_uses_xg_when_favourite_loses():
    # Germany out-xG'd Japan (2.2 vs 1.4) but lost 1-2.
    stats = _build_match_stats(MATCH, MATCH["statistics"])
    assert "out-xG" in stats["biggest_anomaly"]


def test_build_handles_home_away_row_shape():
    rows = [
        {"name": "Ball possession", "home": "60%", "away": "40%"},
        {"name": "Expected goals", "home": "1.5", "away": "0.9"},
    ]
    stats = _build_match_stats(MATCH, rows)
    assert stats["possession_pct"] == {"Germany": 60, "Japan": 40}
    assert stats["xg"] == {"Germany": 1.5, "Japan": 0.9}


# ---------------------------------------------------------------------------
# Fetch (mocked transport)
# ---------------------------------------------------------------------------
def test_fetch_match_data_ok(monkeypatch):
    monkeypatch.setattr(highlightly, "_client", _mock_client(MATCH))
    stats = highlightly.fetch_match_data(99001, "KEY")
    assert stats["xg"]["Germany"] == 2.2
    assert stats["final_score"] == "1-2"


def test_fetch_unwraps_data_envelope(monkeypatch):
    monkeypatch.setattr(highlightly, "_client", _mock_client({"data": MATCH}))
    stats = highlightly.fetch_match_data(99001, "KEY")
    assert stats["home_team"] == "Germany"


def test_fetch_requires_key():
    with pytest.raises(HighlightlyError, match="not set"):
        highlightly.fetch_match_data(1, "")


def test_fetch_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        highlightly, "_client",
        _mock_client({"message": "Unauthorized"}, status=401),
    )
    with pytest.raises(HighlightlyError, match="401"):
        highlightly.fetch_match_data(99001, "BADKEY")


# ---------------------------------------------------------------------------
# Node A routing
# ---------------------------------------------------------------------------
def test_node_a_uses_highlightly_for_numeric_id(monkeypatch):
    monkeypatch.setenv("HIGHLIGHTLY_API_KEY", "KEY")
    monkeypatch.setattr(
        highlightly, "fetch_match_data",
        lambda mid, key, **k: {"data_source": "highlightly", "home_team": "G"},
    )
    out = graph.scrape_match_data({"match_id": "99001"})
    assert out["match_stats"]["data_source"] == "highlightly"
    assert out["status"] == "SCRAPED"


def test_node_a_falls_back_to_mock_on_error(monkeypatch):
    monkeypatch.setenv("HIGHLIGHTLY_API_KEY", "KEY")
    monkeypatch.delenv("FOOTBALL_DATA_TOKEN", raising=False)

    def boom(*a, **k):
        raise HighlightlyError("HTTP 500")

    monkeypatch.setattr(highlightly, "fetch_match_data", boom)
    out = graph.scrape_match_data({"match_id": "99001"})
    assert out["match_stats"]["data_source"] == "mock"
