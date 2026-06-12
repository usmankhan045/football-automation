"""Deep match data ingestion via Highlightly (sports.highlightly.net).

Node A (``scrape_match_data``) calls :func:`fetch_match_data` with a Highlightly
match id to pull core match info plus deep statistics — possession, Expected
Goals (xG), shots — and reshape them into the ``match_stats`` dict the Llama 4
script generator consumes. Authentication is a single ``x-api-key`` header; the
free tier serves these stats without SofaScore's datacenter-IP 403 blocking.

Endpoint:  GET https://sports.highlightly.net/football/matches/{matchId}

Parsing is intentionally tolerant: Highlightly's statistics payload may be
embedded in the match object or under a sibling endpoint, and stat blocks come
either as per-team lists ({team, statistics:[{name,value}]}) or as rows with
home/away columns. Both shapes are handled.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlencode
from typing import Any, Optional

import httpx

logger = logging.getLogger("highlightly")

BASE_URL = "https://sports.highlightly.net/football"


class HighlightlyError(RuntimeError):
    """Raised on any API failure or when the match cannot be parsed."""


def is_quota_error(exc: BaseException) -> bool:
    """Best-effort detection for exhausted/burned provider credentials."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "http 401",
            "http 403",
            "http 429",
            "quota",
            "limit",
            "too many requests",
            "rate limit",
            "exceeded",
            "subscription",
            "unauthorized",
            "forbidden",
        )
    )


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------
def _num(value: Any) -> Optional[float]:
    """Coerce '60%', '1.84', 12, '8/10 (80%)' -> number, else None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    paren_pct = re.search(r"\((\d+(?:\.\d+)?)%\)", text)
    if paren_pct:
        return float(paren_pct.group(1))
    text = text.rstrip("%")
    lead = re.match(r"-?\d+(?:\.\d+)?", text)
    if lead:
        token = lead.group(0)
        return float(token) if "." in token else int(token)
    return None


def _to_int(value: Any) -> Optional[int]:
    num = _num(value)
    return int(num) if num is not None else None


# ---------------------------------------------------------------------------
# Payload shape helpers
# ---------------------------------------------------------------------------
def _first_match(payload: Any) -> dict[str, Any]:
    """Unwrap a single match object from list / {data:...} / direct shapes."""
    if isinstance(payload, list):
        return payload[0] if payload else {}
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return payload
    return {}


def _team_name(block: dict, key: str) -> Optional[str]:
    team = block.get(key) or {}
    return team.get("name") if isinstance(team, dict) else None


def _flatten_statistics(stat_blocks: Any, home: str, away: str) -> dict[str, dict[str, Any]]:
    """Flatten Highlightly statistics into {lower_name: {home, away}}.

    Handles per-team blocks ([{team, statistics:[{name,value}]}]) and column
    rows ([{name|type, home, away}]).
    """
    flat: dict[str, dict[str, Any]] = {}
    if not isinstance(stat_blocks, list) or not stat_blocks:
        return flat

    first = stat_blocks[0]
    per_team = isinstance(first, dict) and "statistics" in first

    if per_team:
        for idx, block in enumerate(stat_blocks):
            tname = _team_name(block, "team")
            side = "home" if tname == home else "away" if tname == away else (
                "home" if idx == 0 else "away"
            )
            for item in block.get("statistics", []) or []:
                name = str(
                    item.get("displayName") or item.get("name") or item.get("type") or ""
                ).strip().lower()
                if not name:
                    continue
                flat.setdefault(name, {})[side] = _num(item.get("value"))
    else:
        for item in stat_blocks:
            name = str(
                item.get("displayName") or item.get("name") or item.get("type") or ""
            ).strip().lower()
            if not name:
                continue
            flat[name] = {"home": _num(item.get("home")), "away": _num(item.get("away"))}
    return flat


def _as_pct(pair: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Highlightly returns possession as a 0-1 fraction -> normalise to percent."""
    if not pair:
        return None
    out = {}
    for team, val in pair.items():
        if isinstance(val, (int, float)) and val is not None and val <= 1:
            val = round(val * 100)
        out[team] = val
    return out


def _sum_sides(flat: dict, keys: list[str], home: str, away: str) -> Optional[dict]:
    """Sum several stat rows into one {home_team, away_team} total (e.g. shots)."""
    present = [k for k in keys if k in flat]
    if not present:
        return None
    h = sum((flat[k].get("home") or 0) for k in present)
    a = sum((flat[k].get("away") or 0) for k in present)
    return {home: h, away: a}


def _find(flat: dict, *needles: str, exclude: tuple[str, ...] = ()) -> Optional[dict]:
    for name, vals in flat.items():
        if any(n in name for n in needles) and not any(x in name for x in exclude):
            if vals.get("home") is not None or vals.get("away") is not None:
                return vals
    return None


def _pair(vals: Optional[dict], home: str, away: str) -> Optional[dict[str, Any]]:
    if not vals:
        return None
    return {home: vals.get("home"), away: vals.get("away")}


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------
def _parse_score(match: dict) -> tuple[Optional[int], Optional[int]]:
    state = match.get("state") or {}
    score = state.get("score") or match.get("score") or {}
    if isinstance(score, dict):
        current = score.get("current")
        if isinstance(current, str) and "-" in current:
            parts = [p.strip() for p in current.split("-")]
            if len(parts) == 2:
                return _to_int(parts[0]), _to_int(parts[1])
        hg, ag = score.get("home"), score.get("away")
        if hg is not None or ag is not None:
            return _to_int(hg), _to_int(ag)
    goals = match.get("goals") or {}
    return (
        _to_int(match.get("homeScore") or goals.get("home")),
        _to_int(match.get("awayScore") or goals.get("away")),
    )


def _anomaly(home: str, away: str, hg: Optional[int], ag: Optional[int],
             xg: Optional[dict]) -> str:
    if hg is None or ag is None:
        return f"{home} vs {away}: in progress — no settled story yet."
    if xg:
        hx, ax = xg.get(home), xg.get(away)
        if hx is not None and ax is not None:
            if hg < ag and hx > ax:
                return (f"{home} out-xG'd {away} ({hx} vs {ax}) but still lost "
                        f"{hg}-{ag} — clinical finishing beat the underlying numbers.")
            if ag < hg and ax > hx:
                return (f"{away} out-xG'd {home} ({ax} vs {hx}) yet lost "
                        f"{ag}-{hg} — ruthless conversion flipped the script.")
    if hg == ag:
        return f"{home} and {away} finished level {hg}-{ag} in a tight tactical duel."
    w, l = (home, away) if hg > ag else (away, home)
    return f"{w} edged {l} {max(hg, ag)}-{min(hg, ag)} on the data and the scoreboard."


def _build_match_stats(match: dict, statistics: Any) -> dict[str, Any]:
    if not match:
        raise HighlightlyError("Empty match payload.")

    home_team = _team_name(match, "homeTeam") or _team_name(match, "home") or "Home"
    away_team = _team_name(match, "awayTeam") or _team_name(match, "away") or "Away"
    hg, ag = _parse_score(match)
    league = match.get("league") or {}
    state = match.get("state") or {}

    flat = _flatten_statistics(statistics, home_team, away_team)
    possession = _as_pct(_pair(_find(flat, "possession"), home_team, away_team))
    xg = _pair(_find(flat, "expected goals", "xg", exclude=("assist",)),
               home_team, away_team)
    shots_on = _pair(_find(flat, "shots on target", "on goal"), home_team, away_team)
    # Highlightly has no single "total shots" — derive it from the components.
    total_shots = _sum_sides(
        flat, ["shots on target", "shots off target", "blocked shots"],
        home_team, away_team,
    ) or _pair(_find(flat, "total shots"), home_team, away_team)

    final_score = f"{hg}-{ag}" if hg is not None and ag is not None else "TBD"

    stats: dict[str, Any] = {
        "data_source": "highlightly",
        "match_id": match.get("id"),
        "competition": league.get("name"),
        "season": league.get("season"),
        "stage": match.get("round") or league.get("round"),
        "match_state": state.get("description") or match.get("status"),
        "home_team": home_team,
        "away_team": away_team,
        "final_score": final_score,
        "possession_pct": possession,
        "xg": xg,
        "shots": total_shots,
        "shots_on_target": shots_on,
        "all_statistics": flat or None,
        "biggest_anomaly": _anomaly(home_team, away_team, hg, ag, xg),
    }
    return {k: v for k, v in stats.items() if v is not None}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _client(headers: dict[str, str], timeout: float) -> httpx.Client:
    return httpx.Client(headers=headers, timeout=timeout)


def _get(client: httpx.Client, url: str) -> Any:
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200]
        try:
            body = exc.response.json()
            detail = body.get("message") or body.get("error") or detail
        except Exception:
            pass
        raise HighlightlyError(f"HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise HighlightlyError(f"request failed for {url}: {exc}") from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise HighlightlyError(f"non-JSON response for {url}") from exc


def _unwrap_matches(payload: Any) -> list[dict[str, Any]]:
    """Return the first list of match objects from common API envelopes."""
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if isinstance(payload, dict):
        for key in ("data", "matches", "fixtures", "events", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
            if isinstance(value, dict):
                nested = _unwrap_matches(value)
                if nested:
                    return nested
    return []


def _match_time(match: dict[str, Any]) -> str | None:
    for key in ("date", "kickoff", "kickoffTime", "startTime", "timestamp", "utcDate"):
        value = match.get(key)
        if value is not None:
            return str(value)
    return None


def _available_match(match: dict[str, Any]) -> dict[str, Any] | None:
    match_id = match.get("id") or match.get("matchId") or match.get("eventId")
    if match_id is None:
        return None

    home = _team_name(match, "homeTeam") or _team_name(match, "home") or "Home"
    away = _team_name(match, "awayTeam") or _team_name(match, "away") or "Away"
    hg, ag = _parse_score(match)
    league = match.get("league") or match.get("competition") or {}
    state = match.get("state") or {}
    status = (
        state.get("description")
        if isinstance(state, dict)
        else None
    ) or match.get("status")

    return {
        "id": str(match_id),
        "home_team": home,
        "away_team": away,
        "competition": league.get("name") if isinstance(league, dict) else None,
        "season": _to_int(
            (league.get("season") if isinstance(league, dict) else None)
            or match.get("season")
        ),
        "stage": match.get("round") or (
            league.get("round") if isinstance(league, dict) else None
        ),
        "kickoff": _match_time(match),
        "status": str(status) if status is not None else None,
        "final_score": f"{hg}-{ag}" if hg is not None and ag is not None else None,
        "data_source": "highlightly",
    }


def _is_world_cup_2026(row: dict[str, Any]) -> bool:
    competition = str(row.get("competition") or "").lower()
    if "world cup" not in competition:
        return False
    if any(marker in competition for marker in ("qualification", "qualifier", "women")):
        return False

    if row.get("season") == 2026:
        return True
    if row.get("season") is not None:
        return False

    kickoff = str(row.get("kickoff") or "")
    return kickoff.startswith("2026")


def _is_completed_match(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").lower()
    final_score = str(row.get("final_score") or "").strip()

    if any(
        marker in status
        for marker in (
            "not started",
            "scheduled",
            "postponed",
            "cancelled",
            "canceled",
            "delayed",
            "upcoming",
        )
    ):
        return False

    if final_score and final_score != "TBD":
        return True

    return any(
        marker in status
        for marker in (
            "finished",
            "full time",
            "ft",
            "ended",
            "complete",
            "closed",
            "after extra",
            "aet",
            "pen",
        )
    )


def fetch_available_matches(
    api_key: str,
    *,
    date: str | None = None,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Fetch match rows for the dashboard picker.

    Highlightly deployments vary slightly in their query parameter naming, so
    the endpoint can be tuned with ``HIGHLIGHTLY_MATCHES_QUERY_PARAM``. The
    default uses the common ``date=YYYY-MM-DD`` shape.
    """
    if not api_key:
        raise HighlightlyError("HIGHLIGHTLY_API_KEY is not set.")

    base = os.getenv("HIGHLIGHTLY_HOST", BASE_URL)
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": os.getenv(
            "HIGHLIGHTLY_API_HOST", "sport-highlights-api.p.rapidapi.com"
        ),
        "Accept": "application/json",
    }
    params: dict[str, str] = {}
    if date:
        params[os.getenv("HIGHLIGHTLY_MATCHES_QUERY_PARAM", "date")] = date
    url = f"{base}/matches"
    if params:
        url = f"{url}?{urlencode(params)}"

    with _client(headers, timeout) as client:
        payload = _get(client, url)

    matches = [
        row for match in _unwrap_matches(payload) if (row := _available_match(match))
    ]
    matches = [
        row for row in matches if _is_world_cup_2026(row) and _is_completed_match(row)
    ]
    matches.sort(key=lambda m: (m.get("kickoff") or "", m.get("competition") or ""))
    return matches


def fetch_match_data(
    match_id: Any, api_key: str, *, timeout: float = 15.0
) -> dict[str, Any]:
    """Fetch deep Highlightly match data as a ``match_stats`` dict.

    Raises :class:`HighlightlyError` on any failure so Node A can fall back.
    """
    if not api_key:
        raise HighlightlyError("HIGHLIGHTLY_API_KEY is not set.")
    if match_id is None or str(match_id).strip() == "":
        raise HighlightlyError("No Highlightly match id provided.")

    base = os.getenv("HIGHLIGHTLY_HOST", BASE_URL)
    # The sports.highlightly.net host authenticates via RapidAPI-style headers
    # (x-api-key alone returns 403 "Missing mandatory HTTP Headers").
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": os.getenv(
            "HIGHLIGHTLY_API_HOST", "sport-highlights-api.p.rapidapi.com"
        ),
        "Accept": "application/json",
    }

    with _client(headers, timeout) as client:
        match = _first_match(_get(client, f"{base}/matches/{match_id}"))

        # Statistics are usually embedded; if not, try the sibling endpoint.
        statistics = match.get("statistics")
        if not statistics:
            try:
                statistics = _first_match_statistics(
                    _get(client, f"{base}/statistics/{match_id}")
                )
            except HighlightlyError as exc:
                logger.info("No sibling statistics for %s: %s", match_id, exc)
                statistics = None

    stats = _build_match_stats(match, statistics)
    logger.info(
        "Highlightly match %s: %s %s %s [%s]",
        match_id, stats.get("home_team"), stats.get("final_score"),
        stats.get("away_team"), stats.get("match_state"),
    )
    return stats


def _first_match_statistics(payload: Any) -> Any:
    """Pull the statistics array from a /statistics response (tolerant)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("statistics", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return payload
