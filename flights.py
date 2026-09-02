"""Data fetching and merge logic for tracking Caribbean Airlines (BWA) flights.

Two data sources are combined, matched by ICAO callsign:
  - A community ADS-B aggregator (adsb.lol, falling back to adsb.fi): live
    position, on-ground status, and which BWA flight numbers are currently
    airborne (no schedule data). OpenSky Network was tried first but its
    anonymous API blocks/times-out traffic from major cloud-hosting IP
    ranges (confirmed by testing) — including Streamlit Community Cloud,
    which runs on GCP — so it's unusable once actually deployed there.
    adsb.lol/adsb.fi are community projects built for exactly this kind of
    open, bulk, no-registration consumption and don't have that problem.
  - Caribbean Airlines' own internal flight-status endpoint (undocumented,
    used by caribbean-airlines.com's own "Flight Status" page): scheduled
    times, actual/estimated departure & arrival, and flight status, looked
    up per flight number.

This app only queries CAL's endpoint for flight numbers already confirmed
airborne. That endpoint is rate-limited/WAF-protected (found by testing:
bulk/rapid querying returns HTTP 429), so it is NOT safe to scan the full
route network to build a complete schedule — only a small, paced,
per-flight lookup is used. Consequence: a flight that hasn't departed yet
(not visible on ADS-B) won't appear here until wheels-up.
"""

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

CAL_STATUS_URL = "https://www.caribbean-airlines.com/get/Flight/Status"

# Point+radius queries against community ADS-B aggregators (readsb/tar1090
# API family). Centered on Piarco (POS), Caribbean Airlines' main hub, with
# a radius wide enough to cover the whole route network including the
# farthest destination (Paris ORY, ~3,670 nm away). Tried in order; falls
# back to the next source if one is unreachable or errors.
ADSB_SOURCES = [
    "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}",
    "https://opendata.adsb.fi/api/v2/point/{lat}/{lon}/{radius}",
]
ADSB_QUERY_LAT = 10.5954
ADSB_QUERY_LON = -61.3372
ADSB_QUERY_RADIUS_NM = 4000

CALLSIGN_PREFIX = "BWA"  # Caribbean Airlines ICAO code
OVERDUE_THRESHOLD_MINUTES = 30
CAL_REQUEST_DELAY_SECONDS = 1.5  # pacing between per-flight lookups, to stay well under CAL's rate limit

CAL_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://www.caribbean-airlines.com/",
}

CAL_STATUS_MAP = {
    "airborne": "active",
    "completed": "landed",
    "landed": "landed",
    "cancelled": "cancelled",
    "diverted": "diverted",
    "delayed": "delayed",
    "scheduled": "scheduled",
}


def _present(value) -> bool:
    """True if value is a real value, not None/NaN/NaT (which pandas produces
    after a merge for missing cells, and which are NOT falsy the way you'd expect)."""
    return value is not None and pd.notna(value)


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
def fetch_live_positions() -> pd.DataFrame:
    """Live position/on-ground status for aircraft currently squawking a BWA
    callsign, via community ADS-B aggregators (adsb.lol and adsb.fi).

    Queries every source and takes the union of what each finds, rather
    than stopping at the first success — these are independent volunteer
    feeder networks with real, non-identical coverage gaps (confirmed by
    testing: adsb.lol alone missed real live BWA flights that adsb.fi or
    OpenSky picked up), so combining them catches more real aircraft than
    either alone. Only raises if every source fails.
    """
    seen = {}
    any_success = False
    last_exc = None
    for template in ADSB_SOURCES:
        url = template.format(lat=ADSB_QUERY_LAT, lon=ADSB_QUERY_LON, radius=ADSB_QUERY_RADIUS_NM)
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            last_exc = exc
            continue

        any_success = True
        for ac in payload.get("ac") or []:
            callsign = (ac.get("flight") or "").strip()
            if not callsign.startswith(CALLSIGN_PREFIX) or callsign in seen:
                continue
            seen[callsign] = {
                "icao24": ac.get("hex"),
                "callsign": callsign,
                "on_ground": ac.get("alt_baro") == "ground",
            }

    if not any_success:
        raise last_exc
    return pd.DataFrame(list(seen.values()), columns=["icao24", "callsign", "on_ground"])


def airborne_flight_numbers(positions_df: pd.DataFrame) -> tuple:
    if positions_df.empty:
        return tuple()
    numbers = positions_df["callsign"].str[len(CALLSIGN_PREFIX):].str.strip()
    numbers = numbers[numbers.str.len() > 0]
    return tuple(sorted(numbers.unique()))


def _cal_request(flight_number: str, dept_date: str) -> list[dict]:
    resp = requests.post(
        CAL_STATUS_URL,
        headers=CAL_HEADERS,
        json={"to": "", "from": "", "dept_date": dept_date, "flight_number": flight_number},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    body = resp.json()
    data = body.get("data") or {}
    if data.get("status") != "Success":
        return []
    result = data.get("result") or []
    return [leg for group in result for leg in group]


def _pick_current_leg(legs: list[dict], now: datetime) -> dict | None:
    if not legs:
        return None

    def dep_time(leg):
        return _parse_iso(leg.get("std_utc"))

    airborne = [leg for leg in legs if (leg.get("flight_status") or "").lower() == "airborne"]
    if airborne:
        return airborne[0]

    in_window = []
    for leg in legs:
        dep, arr = dep_time(leg), _parse_iso(leg.get("sta_utc"))
        if dep and arr and dep <= now <= arr + timedelta(hours=2):
            in_window.append(leg)
    if in_window:
        return in_window[0]

    past = [leg for leg in legs if dep_time(leg) and dep_time(leg) <= now]
    if past:
        return max(past, key=dep_time)

    future = [leg for leg in legs if dep_time(leg) and dep_time(leg) > now]
    if future:
        return min(future, key=dep_time)

    return legs[0]


@st.cache_data(ttl=300, show_spinner=False)
def fetch_caribbean_status(flight_numbers: tuple) -> tuple:
    """Look up schedule/status for each given flight number via CAL's own
    flight-status endpoint. Returns (DataFrame, rate_limited: bool).

    Paced with a delay between requests; stops early (rate_limited=True)
    the moment a request fails, rather than retrying into a block.
    """
    if not flight_numbers:
        return pd.DataFrame(), False

    now = _now_utc()
    dates_to_try = [now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")]

    rows = []
    rate_limited = False
    for i, flight_number in enumerate(flight_numbers):
        if i > 0:
            time.sleep(CAL_REQUEST_DELAY_SECONDS)
        try:
            legs = []
            for dept_date in dates_to_try:
                legs = _cal_request(flight_number, dept_date)
                if legs:
                    break
        except Exception:
            rate_limited = True
            break

        leg = _pick_current_leg(legs, now)
        if leg is None:
            continue

        dep_actual = _parse_iso((leg.get("mvt_times") or {}).get("actual_block_off"))
        arr_actual = _parse_iso((leg.get("mvt_times") or {}).get("actual_block_on")) \
            or _parse_iso((leg.get("mvt_times") or {}).get("actual_touch_down"))
        arr_estimated = _parse_iso((leg.get("mvt_times") or {}).get("estimated_touch_down")) \
            or _parse_iso((leg.get("mvt_times") or {}).get("estimated_block_on"))

        rows.append({
            "callsign": f"{CALLSIGN_PREFIX}{flight_number}",
            "flight_iata": f"BW{flight_number}",
            "flight_status": CAL_STATUS_MAP.get((leg.get("flight_status") or "").lower(), leg.get("flight_status")),
            "dep_airport": leg.get("dept_city"),
            "dep_iata": leg.get("dept_code"),
            "dep_scheduled": _parse_iso(leg.get("std_utc")),
            "dep_estimated": None,
            "dep_actual": dep_actual,
            "arr_airport": leg.get("arr_city"),
            "arr_iata": leg.get("arr_code"),
            "arr_scheduled": _parse_iso(leg.get("sta_utc")),
            "arr_estimated": arr_estimated,
            "arr_actual": arr_actual,
            "aircraft_type": leg.get("aircraft_type"),
            "tailnumber": leg.get("tailnumber"),
        })

    return pd.DataFrame(rows), rate_limited


def _classify_status(row) -> str:
    raw = row.get("flight_status")
    raw_status = raw.lower() if _present(raw) and isinstance(raw, str) else ""
    if raw_status == "cancelled":
        return "Cancelled"
    if raw_status == "diverted":
        return "Diverted"
    if raw_status == "delayed":
        return "Delayed"
    if raw_status == "landed":
        return "Landed"

    dep_scheduled = row.get("dep_scheduled")
    dep_actual = row.get("dep_actual")
    if _present(dep_scheduled) and _present(dep_actual):
        delay = (dep_actual - dep_scheduled).total_seconds() / 60
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
    raw = row.get("flight_status")
    raw_status = raw.lower() if _present(raw) and isinstance(raw, str) else ""
    if raw_status in {"cancelled", "landed"}:
        return False
    eta = row.get("arr_scheduled")
    if not _present(eta):
        return False
    return (_now_utc() - eta).total_seconds() / 60 > OVERDUE_THRESHOLD_MINUTES


def build_flight_table(positions_df: pd.DataFrame, cal_df: pd.DataFrame) -> pd.DataFrame:
    """Merge live position data with CAL schedule/status data by callsign."""
    if cal_df.empty:
        merged = positions_df.copy()
        for col in ["flight_iata", "flight_status", "dep_airport", "dep_iata",
                     "dep_scheduled", "dep_estimated", "dep_actual",
                     "arr_airport", "arr_iata", "arr_scheduled", "arr_estimated",
                     "arr_actual", "aircraft_type", "tailnumber"]:
            merged[col] = None
    else:
        merged = pd.merge(cal_df, positions_df, on="callsign", how="outer")

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
