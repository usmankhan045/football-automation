"""Real-time match data ingestion via football-data.org (API v4).

Node A (``scrape_match_data``) calls :func:`fetch_latest_worldcup_match` to pull
the most recent FINISHED FIFA World Cup fixture and reshape it into the
``match_stats`` dict the Llama 4 script generator consumes. Any failure (missing
token, restricted resource, network error, no matches) raises
:class:`FootballDataError` so the caller can fall back to mock data.

Endpoint:  GET https://api.football-data.org/v4/competitions/WC/matches
Auth:      header ``X-Auth-Token: <token>``

Note: the matches endpoint provides score / stage / status only — it does NOT
expose possession, shots or xG. The script is therefore built from the result,
stage and half-time -> full-time swing rather than granular tactical metrics.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("football_data")

BASE_URL = "https://api.football-data.org/v4"
COMPETITION_CODE = "WC"  # FIFA World Cup
DEFAULT_SEASON = 2026


class FootballDataError(RuntimeError):
    """Raised on any API failure or when no usable match is available."""


def _client(token: str, timeout: float) -> httpx.Client:
    headers = {"X-Auth-Token": token} if token else {}
    return httpx.Client(
        base_url=os.getenv("FOOTBALL_DATA_HOST", BASE_URL),
        headers=headers,
        timeout=timeout,
    )


def _readable_stage(stage: Optional[str], group: Optional[str]) -> str:
    """GROUP_STAGE + GROUP_A -> 'Group Stage · Group A'."""
    label = (stage or "Group Stage").replace("_", " ").title()
    if group:
        label += f" · {group.replace('_', ' ').title()}"
    return label


def _anomaly(home: str, away: str, hg: Optional[int], ag: Optional[int],
             ht: dict[str, Any]) -> str:
    if hg is None or ag is None:
        return f"{home} vs {away}: result still pending."

    hh, ha = ht.get("home"), ht.get("away")
    # Half-time -> full-time swing (a comeback) is the juiciest hook available.
    if hh is not None and ha is not None:
        ht_lead = "home" if hh > ha else "away" if ha > hh else None
        ft_win = "home" if hg > ag else "away" if ag > hg else None
        if ht_lead and ft_win and ht_lead != ft_win:
            team = home if ft_win == "home" else away
            return (
                f"{team} flipped the script — trailing {hh}-{ha} at the break "
                f"before turning it around to win {max(hg, ag)}-{min(hg, ag)}."
            )

    if hg == ag:
        return f"{home} and {away} couldn't be separated, finishing {hg}-{ag}."

    winner, loser = (home, away) if hg > ag else (away, home)
    return f"{winner} saw off {loser} {max(hg, ag)}-{min(hg, ag)} on the world's biggest stage."


def _build_match_stats(match: dict, season: int) -> dict[str, Any]:
    home = match.get("homeTeam", {}) or {}
    away = match.get("awayTeam", {}) or {}
    home_name = home.get("name") or home.get("shortName") or "Home"
    away_name = away.get("name") or away.get("shortName") or "Away"

    score = match.get("score", {}) or {}
    ft = score.get("fullTime", {}) or {}
    ht = score.get("halfTime", {}) or {}
    hg, ag = ft.get("home"), ft.get("away")

    final_score = f"{hg}-{ag}" if hg is not None and ag is not None else "TBD"
    halftime = (
        f"{ht.get('home')}-{ht.get('away')}"
        if ht.get("home") is not None and ht.get("away") is not None
        else None
    )

    stats: dict[str, Any] = {
        "data_source": "football-data.org",
        "competition": "FIFA World Cup",
        "season": season,
        "stage": _readable_stage(match.get("stage"), match.get("group")),
        "matchday": match.get("matchday"),
        "match_state": match.get("status"),
        "home_team": home_name,
        "away_team": away_name,
        "home_tla": home.get("tla"),
        "away_tla": away.get("tla"),
        "final_score": final_score,
        "halftime_score": halftime,
        "result": score.get("winner"),
        "extra_time": score.get("duration") not in (None, "REGULAR"),
        "kickoff": match.get("utcDate"),
        "biggest_anomaly": _anomaly(home_name, away_name, hg, ag, ht),
    }
    return {k: v for k, v in stats.items() if v is not None}


def fetch_latest_worldcup_match(
    token: str,
    *,
    season: Optional[int] = None,
    competition: str = COMPETITION_CODE,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch the latest FINISHED World Cup match as a ``match_stats`` dict.

    Raises :class:`FootballDataError` on any failure so Node A can fall back to
    deterministic mock data.
    """
    if not token:
        raise FootballDataError("FOOTBALL_DATA_TOKEN is not set.")

    season = season or int(os.getenv("FOOTBALL_DATA_SEASON", DEFAULT_SEASON))

    with _client(token, timeout) as client:
        try:
            resp = client.get(
                f"/competitions/{competition}/matches",
                params={"status": "FINISHED", "season": season},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # football-data.org returns {"message": ..., "errorCode": ...}.
            detail = exc.response.text[:200]
            try:
                detail = exc.response.json().get("message", detail)
            except Exception:
                pass
            raise FootballDataError(
                f"HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise FootballDataError(f"request failed: {exc}") from exc

        payload = resp.json()

    matches = [m for m in payload.get("matches", []) if m.get("status") == "FINISHED"]
    if not matches:
        raise FootballDataError(
            f"No finished World Cup matches for season {season}."
        )

    # "Latest completed" = the most recent kickoff among finished matches.
    latest = max(matches, key=lambda m: m.get("utcDate", ""))
    stats = _build_match_stats(latest, season)
    logger.info(
        "Fetched World Cup %s match: %s %s %s [%s]",
        season, stats.get("home_team"), stats.get("final_score"),
        stats.get("away_team"), stats.get("stage"),
    )
    return stats
