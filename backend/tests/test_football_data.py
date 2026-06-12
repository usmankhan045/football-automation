"""Unit tests for the football-data.org ingestion layer (offline / mocked HTTP).

Verify match selection, the transform into ``match_stats``, error handling for
restricted resources, and that Node A degrades to mock data on failure.
"""

from __future__ import annotations

import httpx
import pytest

from app import football_data, graph
from app.football_data import FootballDataError, _build_match_stats, _readable_stage


def _match(home, away, hg, ag, hh=None, ha=None, utc="2026-07-19T19:00:00Z",
           status="FINISHED", stage="FINAL", group=None):
    return {
        "id": 1,
        "utcDate": utc,
        "status": status,
        "stage": stage,
        "group": group,
        "matchday": None,
        "homeTeam": {"name": home, "tla": home[:3].upper()},
        "awayTeam": {"name": away, "tla": away[:3].upper()},
        "score": {
            "winner": "HOME_TEAM" if hg > ag else "AWAY_TEAM" if ag > hg else "DRAW",
            "duration": "REGULAR",
            "fullTime": {"home": hg, "away": ag},
            "halfTime": {"home": hh, "away": ha},
        },
    }


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return lambda token, timeout: httpx.Client(
        base_url=football_data.BASE_URL, headers={"X-Auth-Token": token},
        transport=transport,
    )


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def test_readable_stage():
    assert _readable_stage("GROUP_STAGE", "GROUP_A") == "Group Stage · Group A"
    assert _readable_stage("QUARTER_FINALS", None) == "Quarter Finals"


def test_build_match_stats_shapes_payload():
    stats = _build_match_stats(_match("Argentina", "France", 3, 3, 2, 0), 2026)
    assert stats["home_team"] == "Argentina"
    assert stats["final_score"] == "3-3"
    assert stats["halftime_score"] == "2-0"
    assert stats["data_source"] == "football-data.org"
    assert stats["season"] == 2026


def test_anomaly_detects_comeback():
    # Trailed 0-2 at half, won 3-2 -> comeback hook.
    stats = _build_match_stats(_match("Spain", "Brazil", 3, 2, 0, 2), 2026)
    assert "flipped the script" in stats["biggest_anomaly"]


# ---------------------------------------------------------------------------
# Fetch + selection (mocked transport)
# ---------------------------------------------------------------------------
def test_fetch_picks_latest_finished(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"matches": [
            _match("A", "B", 1, 0, utc="2026-06-12T16:00:00Z"),
            _match("C", "D", 2, 1, utc="2026-07-19T19:00:00Z"),  # latest
            _match("E", "F", 0, 0, utc="2026-06-20T16:00:00Z", status="SCHEDULED"),
        ]})

    monkeypatch.setattr(football_data, "_client", _mock_client(handler))
    stats = football_data.fetch_latest_worldcup_match("TOKEN", season=2026)
    assert stats["home_team"] == "C" and stats["final_score"] == "2-1"


def test_fetch_raises_on_restricted(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "message": "The resource you are looking for is restricted.",
            "errorCode": 403,
        })

    monkeypatch.setattr(football_data, "_client", _mock_client(handler))
    with pytest.raises(FootballDataError, match="restricted"):
        football_data.fetch_latest_worldcup_match("TOKEN")


def test_fetch_raises_when_no_finished_matches(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"matches": [
            _match("A", "B", 0, 0, status="SCHEDULED"),
        ]})

    monkeypatch.setattr(football_data, "_client", _mock_client(handler))
    with pytest.raises(FootballDataError, match="No finished"):
        football_data.fetch_latest_worldcup_match("TOKEN")


def test_fetch_requires_token():
    with pytest.raises(FootballDataError, match="not set"):
        football_data.fetch_latest_worldcup_match("")


# ---------------------------------------------------------------------------
# Node A integration: graceful fallback to mock
# ---------------------------------------------------------------------------
def test_node_a_falls_back_to_mock_without_token(monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_TOKEN", raising=False)
    out = graph.scrape_match_data({"match_id": "x"})
    assert out["status"] == "SCRAPED"
    assert out["match_stats"]["data_source"] == "mock"


def test_node_a_falls_back_to_mock_on_api_error(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_TOKEN", "TOKEN")

    def boom(*a, **k):
        raise FootballDataError("restricted")

    monkeypatch.setattr(football_data, "fetch_latest_worldcup_match", boom)
    out = graph.scrape_match_data({"match_id": "x"})
    assert out["match_stats"]["data_source"] == "mock"
