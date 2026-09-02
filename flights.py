"""Data fetching and merge logic for tracking Caribbean Airlines (BWA) flights.

Two data sources are combined, matched by ICAO callsign:
  - OpenSky Network: live position / on-ground status (no schedule data)
  - AviationStack:   scheduled/estimated times and flight status (no reliable live position on the free tier)
"""

from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
AVIATIONSTACK_URL = "http://api.aviationstack.com/v1/flights"

CALLSIGN_PREFIX = "BWA"  # Caribbean Airlines ICAO code
AIRLINE_IATA = "BW"
OVERDUE_THRESHOLD_MINUTES = 30

OPENSKY_STATE_COLUMNS = [
    "icao24", "callsign", "origin_country", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
    "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk",
    "spi", "position_source",
]


def _present(value) -> bool:
    """True if value is a real value, not None/NaN/NaT (which pandas produces
    after a merge for missing cells, and which are NOT falsy the way you'd expect)."""
    return value is not None and pd.notna(value)


def get_secret(key: str):
    """st.secrets raises if no secrets.toml exists at all, even via .get()."""
    try:
        return st.secrets.get(key)
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value):
    if not value:
        return None
    try:
        return pd.to_datetime(value, utc=True).to_pydatetime()
    except (ValueError, TypeError):
        return None


@st.cache_data(ttl=60, show_spinner=False)
def fetch_opensky_states() -> pd.DataFrame:
    """Live ADS-B state vectors for aircraft currently squawking a BWA callsign."""
    auth = None
    username = get_secret("OPENSKY_USERNAME")
    password = get_secret("OPENSKY_PASSWORD")
    if username and password:
        auth = (username, password)

    resp = requests.get(OPENSKY_STATES_URL, auth=auth, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    states = payload.get("states") or []

    df = pd.DataFrame(states, columns=OPENSKY_STATE_COLUMNS)
    if df.empty:
        return df

    df["callsign"] = df["callsign"].fillna("").str.strip()
    return df[df["callsign"].str.startswith(CALLSIGN_PREFIX)].reset_index(drop=True)


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_aviationstack_flights(_cache_bust: int = 0) -> pd.DataFrame:
    """Scheduled/estimated times and flight status for Caribbean Airlines flights.

    Cached for 30 minutes by default to conserve AviationStack's free-tier
    quota (100 requests/month). `_cache_bust` lets the sidebar "Refresh now"
    button force a real fetch without changing the normal TTL behavior.
    """
    api_key = get_secret("AVIATIONSTACK_API_KEY")
    if not api_key:
        return pd.DataFrame()

    resp = requests.get(
        AVIATIONSTACK_URL,
        params={"access_key": api_key, "airline_iata": AIRLINE_IATA, "limit": 100},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()

    if "error" in payload:
        raise RuntimeError(payload["error"].get("message", "AviationStack API error"))

    rows = []
    for entry in payload.get("data", []):
        flight = entry.get("flight") or {}
        departure = entry.get("departure") or {}
        arrival = entry.get("arrival") or {}
        rows.append({
            "callsign": (flight.get("icao") or "").strip(),
            "flight_iata": flight.get("iata"),
            "flight_status": entry.get("flight_status"),
            "dep_airport": departure.get("airport"),
            "dep_iata": departure.get("iata"),
            "dep_scheduled": _parse_iso(departure.get("scheduled")),
            "dep_estimated": _parse_iso(departure.get("estimated")),
            "dep_actual": _parse_iso(departure.get("actual")),
            "dep_delay_min": departure.get("delay"),
            "arr_airport": arrival.get("airport"),
            "arr_iata": arrival.get("iata"),
            "arr_scheduled": _parse_iso(arrival.get("scheduled")),
            "arr_estimated": _parse_iso(arrival.get("estimated")),
            "arr_actual": _parse_iso(arrival.get("actual")),
            "arr_delay_min": arrival.get("delay"),
        })

    return pd.DataFrame(rows)


def _classify_status(row) -> str:
    raw_status = (row.get("flight_status") or "").lower()
    if raw_status == "cancelled":
        return "Cancelled"
    if raw_status == "diverted":
        return "Diverted"
    if raw_status == "incident":
        return "Incident"
    if raw_status == "landed":
        return "Landed"

    dep_delay = row.get("dep_delay_min")
    arr_delay = row.get("arr_delay_min")
    delay = dep_delay if _present(dep_delay) else (arr_delay if _present(arr_delay) else 0)
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        delay = 0

    if delay >= 15:
        return "Delayed"
    if raw_status == "active":
        return "En Route"
    if raw_status == "scheduled":
        return "Scheduled"
    return raw_status.title() if raw_status else "Unknown"


def _minutes_since_due_departure(row) -> float | None:
    if _present(row.get("dep_actual")):
        return None
    scheduled = row.get("dep_scheduled")
    if not _present(scheduled):
        return None
    delta = (_now_utc() - scheduled).total_seconds() / 60
    return round(delta, 1) if delta > 0 else None


def _eta(row):
    estimated = row.get("arr_estimated")
    return estimated if _present(estimated) else row.get("arr_scheduled")


def _is_overdue(row) -> bool:
    if _present(row.get("arr_actual")):
        return False
    if (row.get("flight_status") or "").lower() in {"cancelled", "landed"}:
        return False
    eta = row.get("arr_scheduled")
    if not _present(eta):
        return False
    return (_now_utc() - eta).total_seconds() / 60 > OVERDUE_THRESHOLD_MINUTES


def build_flight_table(opensky_df: pd.DataFrame, aviationstack_df: pd.DataFrame) -> pd.DataFrame:
    """Merge live position data with schedule/status data by callsign."""
    if aviationstack_df.empty:
        merged = opensky_df.copy()
        for col in ["flight_iata", "flight_status", "dep_airport", "dep_iata",
                     "dep_scheduled", "dep_estimated", "dep_actual", "dep_delay_min",
                     "arr_airport", "arr_iata", "arr_scheduled", "arr_estimated",
                     "arr_actual", "arr_delay_min"]:
            merged[col] = None
    else:
        merged = pd.merge(aviationstack_df, opensky_df, on="callsign", how="outer")

    if merged.empty:
        return merged

    merged["status"] = merged.apply(_classify_status, axis=1)
    merged["minutes_since_due_departure"] = merged.apply(_minutes_since_due_departure, axis=1)
    merged["eta"] = merged.apply(_eta, axis=1)
    merged["is_overdue"] = merged.apply(_is_overdue, axis=1)
    merged["is_diverting"] = merged["status"] == "Diverted"

    merged = merged.sort_values(
        by="dep_scheduled", na_position="last"
    ).reset_index(drop=True)
    return merged
