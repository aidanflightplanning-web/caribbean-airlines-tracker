"""Data fetching and merge logic for tracking Caribbean Airlines (BWA) flights.

Three data sources are combined, matched by ICAO callsign/flight number:
  - A community ADS-B aggregator (adsb.lol and adsb.fi, unioned): live
    position, on-ground status, and which BWA flight numbers are currently
    airborne (no schedule data). OpenSky Network was tried first but its
    anonymous API blocks/times-out traffic from major cloud-hosting IP
    ranges (confirmed by testing) — including Streamlit Community Cloud,
    which runs on GCP — so it's unusable once actually deployed there.
    adsb.lol/adsb.fi are community projects built for exactly this kind of
    open, bulk, no-registration consumption and don't have that problem,
    but their crowdsourced coverage still has real gaps (also confirmed by
    testing: cross-checking against OpenSky, and even against CAL's own
    live-status feed, both adsb.lol and adsb.fi individually and combined
    have missed real, currently-airborne BWA flights).
  - Caribbean Airlines' own internal flight-status endpoint (undocumented,
    used by caribbean-airlines.com's own "Flight Status" page): scheduled
    times, actual/estimated departure & arrival, and flight status, looked
    up per flight number. Rate-limited/WAF-protected (found by testing:
    bulk/rapid querying returns HTTP 429), so it's NOT safe to scan the
    full route network on this endpoint — only a small, paced, per-flight
    lookup is used.
  - Caribbean Airlines' own published timetable endpoint (also
    undocumented, used by their "Flight Schedule" page) — a *different*
    endpoint from the live-status one above, and NOT similarly rate-
    limited (confirmed by testing: querying it for all 25 origin airports
    back-to-back succeeded without issue). This is the fix for ADS-B's
    coverage gaps: rather than relying on ADS-B to discover which flight
    numbers exist, this fetches CAL's actual published schedule (cached
    for hours, since schedules don't change intraday) and computes which
    flight numbers are plausibly operating right now from their day-of-
    week/time-of-day pattern, then checks those directly regardless of
    what ADS-B does or doesn't detect.

Consequence of what's left after all three: a flight that's genuinely not
in CAL's published schedule at all (e.g. a one-off charter) still won't
appear until ADS-B spots it or it's added to the watchlist.
"""

import random
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

CAL_STATUS_URL = "https://www.caribbean-airlines.com/get/Flight/Status"
CAL_SCHEDULE_URL_TEMPLATE = "https://www.caribbean-airlines.com/api/schedule/{origin}/NONE/{month_year}"

# Every origin airport in CAL's network (from api/Flight/Getroutesflights).
# Needed because return legs (e.g. BW483 MIA->POS) originate from the spoke
# city, not the hub, and querying only POS/KIN's schedule would miss them.
CAL_SCHEDULE_ORIGINS = [
    "ANU", "BGI", "CCS", "CUR", "FDF", "FLL", "GEO", "GND", "HAV", "JFK",
    "KIN", "MCO", "MIA", "NAS", "OGL", "ORY", "PBM", "POS", "PTP", "SFG",
    "SLU", "SVD", "SXM", "TAB", "YYZ",
]
CAL_SCHEDULE_TTL_SECONDS = 6 * 60 * 60  # schedules don't change intraday
CAL_SCHEDULE_REQUEST_DELAY_SECONDS = 1.0

# Rough UTC offsets (hours) for the airports above, only used to estimate
# whether a scheduled local time is plausibly "now" — not shown to users,
# so DST edge cases costing an hour of accuracy here don't matter.
AIRPORT_UTC_OFFSET_HOURS = {
    "POS": -4, "BGI": -4, "GND": -4, "SLU": -4, "SVD": -4, "ANU": -4,
    "PTP": -4, "FDF": -4, "SXM": -4, "SFG": -4, "TAB": -4, "CUR": -4,
    "GEO": -4, "OGL": -4, "CCS": -4, "HAV": -4, "NAS": -4,
    "MIA": -4, "FLL": -4, "MCO": -4, "JFK": -4, "YYZ": -4,
    "PBM": -3, "KIN": -5, "ORY": 2,
}
SCHEDULE_PRE_DEPARTURE_BUFFER_MIN = 30
SCHEDULE_POST_ARRIVAL_BUFFER_MIN = 30
_WEEKDAY_KEYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

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
CAL_REQUEST_DELAY_SECONDS = 1.5  # pacing between new (non-cached) per-flight lookups
# Deliberately longer than the 10-min refresh cadence: with the schedule
# roster now feeding in ~15-20 candidates at once, a TTL equal to the
# refresh interval makes every cycle's cache entries expire in lockstep,
# forcing a full ~20-request burst every single cycle -- which is exactly
# what tripped CAL's rate limiter in testing. A longer TTL staggers
# expiry so only a fraction need a fresh fetch on any given cycle.
CAL_CACHE_TTL_SECONDS = 20 * 60
STICKY_GRACE_SECONDS = 20 * 60  # keep a confirmed-active flight in the candidate set this long after its last confirmation, riding out ADS-B/CAL blind spots
TERMINAL_STATUSES = {"cancelled", "landed"}

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


def get_secret(key: str):
    """st.secrets raises if no secrets.toml exists at all, even via .get()."""
    try:
        return st.secrets.get(key)
    except Exception:
        return None


def watchlist_flight_numbers() -> tuple:
    """Flight numbers to always look up via CAL, regardless of whether any
    ADS-B source currently sees them airborne. No ADS-B feed's coverage is
    complete, so a flight you specifically care about can otherwise miss a
    refresh cycle. Configured via the optional WATCHLIST_FLIGHT_NUMBERS
    secret (comma- or newline-separated, e.g. "526,217,415"). Keep this
    short — each entry adds a paced ~1.5s CAL request every refresh cycle,
    on top of whatever's airborne.
    """
    raw = get_secret("WATCHLIST_FLIGHT_NUMBERS") or ""
    numbers = [n.strip() for n in raw.replace("\n", ",").split(",")]
    return tuple(sorted({n for n in numbers if n}))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value):
    if not value:
        return None
    try:
        return pd.to_datetime(value, utc=True).to_pydatetime()
    except (ValueError, TypeError):
        return None


def _parse_date_only(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_local_clock_time_to_utc(date_obj, time_str: str, utc_offset_hours: float):
    """Combine a date with a 'HH:MM AM/PM' local-time string and a UTC
    offset into a UTC datetime, or None if time_str can't be parsed."""
    try:
        t = datetime.strptime((time_str or "").strip(), "%I:%M %p")
    except ValueError:
        return None
    local_dt = datetime(date_obj.year, date_obj.month, date_obj.day, t.hour, t.minute, tzinfo=timezone.utc)
    return local_dt - timedelta(hours=utc_offset_hours)


@st.cache_data(ttl=CAL_SCHEDULE_TTL_SECONDS, show_spinner=False)
def fetch_schedule_roster() -> list:
    """Caribbean Airlines' own published timetable, fetched once per origin
    airport (25 requests) and cached for hours since schedules don't change
    intraday. A single origin failing doesn't block the others — this just
    ends up with a slightly less complete roster until the next refetch.
    """
    now = _now_utc()
    month_year = now.strftime("%b %Y")
    entries = []
    for i, origin in enumerate(CAL_SCHEDULE_ORIGINS):
        if i > 0:
            time.sleep(CAL_SCHEDULE_REQUEST_DELAY_SECONDS)
        try:
            resp = requests.get(
                CAL_SCHEDULE_URL_TEMPLATE.format(origin=origin, month_year=month_year.replace(" ", "%20")),
                headers=CAL_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            continue
        entries.extend(payload.get("schedule") or [])
    return entries


def active_scheduled_flight_numbers(roster: list) -> tuple:
    """Flight numbers from the schedule roster whose scheduled window
    (departure minus a pre-departure buffer, through arrival plus a
    post-arrival buffer) plausibly includes right now, accounting for each
    entry's day-of-week pattern and validity date range."""
    now = _now_utc()
    active = set()

    for entry in roster:
        flight_num = (entry.get("flight_num") or "").strip().upper()
        if not flight_num.startswith("BW"):
            continue
        number = flight_num[2:].strip()
        if not number:
            continue

        start_date = _parse_date_only(entry.get("start_date"))
        end_date = _parse_date_only(entry.get("end_date"))
        frequency = entry.get("frequency") or {}
        offset = AIRPORT_UTC_OFFSET_HOURS.get(entry.get("from_code"), -4)

        # Check today, yesterday, and tomorrow at the origin's local date,
        # since the entry's day-of-week is local and a window can straddle
        # a UTC/local date boundary.
        local_today = (now + timedelta(hours=offset)).date()
        for day_delta in (0, -1, 1):
            candidate_date = local_today + timedelta(days=day_delta)
            if start_date and candidate_date < start_date:
                continue
            if end_date and candidate_date > end_date:
                continue
            if not frequency.get(_WEEKDAY_KEYS[candidate_date.weekday()]):
                continue

            dep_utc = _parse_local_clock_time_to_utc(candidate_date, entry.get("departure_time"), offset)
            arr_utc = _parse_local_clock_time_to_utc(candidate_date, entry.get("arrival_time"), offset)
            if dep_utc is None or arr_utc is None:
                continue
            if arr_utc < dep_utc:
                arr_utc += timedelta(days=1)  # overnight flight

            window_start = dep_utc - timedelta(minutes=SCHEDULE_PRE_DEPARTURE_BUFFER_MIN)
            window_end = arr_utc + timedelta(minutes=SCHEDULE_POST_ARRIVAL_BUFFER_MIN)
            if window_start <= now <= window_end:
                active.add(number)
                break

    return tuple(sorted(active))


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


@st.cache_resource
def _active_registry() -> dict:
    """Persists in server memory across reruns and page reloads (unlike
    st.session_state, which resets on the meta-refresh's full page reload):
    {flight_number: epoch of last confirmed-active sighting}."""
    return {}


def sticky_candidates(detected: tuple, watchlist: tuple) -> tuple:
    """Union of this cycle's ADS-B-detected + watchlisted flight numbers
    with recently-active ones still inside their grace period. Without
    this, a flight flickers off the table the moment one refresh cycle's
    ADS-B query happens to miss it, even though it's still genuinely
    flying — confirmed by testing that no combination of ADS-B sources has
    fully reliable per-cycle coverage.
    """
    registry = _active_registry()
    now = time.time()
    for fn in [fn for fn, ts in registry.items() if now - ts > STICKY_GRACE_SECONDS]:
        del registry[fn]
    combined = set(detected) | set(watchlist) | set(registry.keys())
    return tuple(sorted(combined))


def update_active_registry(table: pd.DataFrame) -> None:
    """Call once per refresh with the final built table: keeps confirmed
    non-terminal flights alive in the sticky registry, and drops ones CAL
    confirms are Landed/Cancelled so they don't linger past that."""
    registry = _active_registry()
    now = time.time()
    for _, row in table.iterrows():
        callsign = row.get("callsign")
        if not isinstance(callsign, str) or not callsign.startswith(CALLSIGN_PREFIX):
            continue
        flight_number = callsign[len(CALLSIGN_PREFIX):]
        status = row.get("status")
        status_lower = status.lower() if isinstance(status, str) else ""
        if status_lower in TERMINAL_STATUSES:
            registry.pop(flight_number, None)
        else:
            registry[flight_number] = now


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


STALE_LEG_CUTOFF_HOURS = 6  # don't surface a leg further than this from "now" as if it were current


_LEG_TERMINAL_STATUSES = {"completed", "landed", "cancelled"}


def _pick_current_leg(legs: list[dict], now: datetime) -> dict | None:
    """Same flight number can have multiple legs the same day (a through-
    flight, e.g. BW526 POS->GEO then continuing GEO->JFK). Once the first
    leg completes, the continuation is what's actually relevant -- so a
    non-terminal leg (still pending/delayed/scheduled) is always preferred
    over an already-completed one, regardless of which departs earlier.
    Only when every leg is terminal do we fall back to showing the most
    recent one, i.e. the flight number's rotation for the day is done.
    """
    if not legs:
        return None

    def dep_time(leg):
        return _parse_iso(leg.get("std_utc"))

    def is_terminal(leg):
        return (leg.get("flight_status") or "").lower() in _LEG_TERMINAL_STATUSES

    airborne = [leg for leg in legs if (leg.get("flight_status") or "").lower() == "airborne"]
    if airborne:
        return airborne[0]

    cutoff = timedelta(hours=STALE_LEG_CUTOFF_HOURS)
    non_terminal = [leg for leg in legs if not is_terminal(leg) and dep_time(leg)]

    if non_terminal:
        in_window = []
        for leg in non_terminal:
            dep, arr = dep_time(leg), _parse_iso(leg.get("sta_utc"))
            if dep and arr and dep <= now <= arr + timedelta(hours=2):
                in_window.append(leg)
        if in_window:
            return in_window[0]

        overdue = [leg for leg in non_terminal if dep_time(leg) <= now]
        if overdue:
            most_overdue = max(overdue, key=dep_time)
            if now - dep_time(most_overdue) <= cutoff:
                return most_overdue

        upcoming = [leg for leg in non_terminal if dep_time(leg) > now]
        if upcoming:
            soonest = min(upcoming, key=dep_time)
            if dep_time(soonest) - now <= cutoff:
                return soonest

    terminal_past = [leg for leg in legs if is_terminal(leg) and dep_time(leg) and dep_time(leg) <= now]
    if terminal_past:
        most_recent = max(terminal_past, key=dep_time)
        if now - dep_time(most_recent) <= cutoff:
            return most_recent

    return None


@st.cache_data(ttl=CAL_CACHE_TTL_SECONDS, show_spinner=False)
def _fetch_single_flight_legs(flight_number: str) -> list[dict]:
    now = _now_utc()
    dates_to_try = [now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")]
    for dept_date in dates_to_try:
        legs = _cal_request(flight_number, dept_date)
        if legs:
            return legs
    return []


@st.cache_resource
def _cal_fetch_timestamps() -> dict:
    """Tracks when each flight number was last actually fetched over the
    network (as opposed to served from _fetch_single_flight_legs's own
    cache), so fetch_caribbean_status knows which lookups are free
    (cached, no pacing needed) vs. need a real, paced network call."""
    return {}


def fetch_caribbean_status(flight_numbers: tuple) -> tuple:
    """Look up schedule/status for each given flight number via CAL's own
    flight-status endpoint. Returns (DataFrame, rate_limited: bool).

    Each flight number is cached independently (ttl=CAL_CACHE_TTL_SECONDS),
    rather than caching the whole requested batch as one unit. That matters
    because the exact set of flight numbers asked for changes cycle to
    cycle (as ADS-B detection shifts) — with a single whole-batch cache,
    that alone would force a full refetch of everything, including flights
    already known-good, and a rate-limit hit partway through would drop
    every flight after it in that batch. Per-flight caching means only
    genuinely new/expired lookups trigger a paced network call, and a
    rate-limit hit only affects new lookups, not already-cached ones.
    """
    if not flight_numbers:
        return pd.DataFrame(), False

    now = _now_utc()
    now_epoch = time.time()
    fetch_times = _cal_fetch_timestamps()

    def is_cached(fn):
        last_fetch = fetch_times.get(fn)
        return last_fetch is not None and (now_epoch - last_fetch) < CAL_CACHE_TTL_SECONDS

    # Cached lookups are free (no network call), so process those first in
    # any order. New lookups are the ones that pace/risk rate-limiting, so
    # shuffle their order each cycle -- otherwise a rate-limit cutoff would
    # deterministically always hit the same (sort-order-last) flights.
    already_cached = [fn for fn in flight_numbers if is_cached(fn)]
    needs_fetch = [fn for fn in flight_numbers if fn not in already_cached]
    random.shuffle(needs_fetch)
    ordered_flight_numbers = already_cached + needs_fetch

    rows = []
    rate_limited = False
    network_fetching_disabled = False
    made_first_real_call = False

    for flight_number in ordered_flight_numbers:
        cached = is_cached(flight_number)

        if not cached and network_fetching_disabled:
            continue

        try:
            if not cached:
                if made_first_real_call:
                    time.sleep(CAL_REQUEST_DELAY_SECONDS)
                legs = _fetch_single_flight_legs(flight_number)
                made_first_real_call = True
                fetch_times[flight_number] = now_epoch
            else:
                legs = _fetch_single_flight_legs(flight_number)
        except Exception:
            rate_limited = True
            network_fetching_disabled = True
            continue

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
    if raw_status == "landed":
        return "Landed"
    if raw_status == "active":
        return "En Route"  # once airborne, always En Route -- a late departure doesn't change that

    # Not airborne (or CAL's status is unclear) -- check whether it's overdue
    # departing, using "now" rather than an actual departure time it doesn't
    # have yet.
    dep_scheduled = row.get("dep_scheduled")
    if _present(dep_scheduled) and not _present(row.get("dep_actual")):
        overdue_departure = (_now_utc() - dep_scheduled).total_seconds() / 60
        if overdue_departure >= 15:
            return "Delayed"

    if raw_status == "delayed":
        return "Delayed"
    if raw_status == "scheduled":
        return "Scheduled"
    return raw_status.title() if raw_status else "Unknown"


def _minutes_since_due_departure(row) -> float | None:
    if row.get("status") == "Cancelled":
        return None  # "still waiting to depart" is meaningless for a cancelled flight
    if _present(row.get("dep_actual")):
        return None
    scheduled = row.get("dep_scheduled")
    if not _present(scheduled):
        return None
    delta = (_now_utc() - scheduled).total_seconds() / 60
    return round(delta, 1) if delta > 0 else None


def _departure_delay_minutes(row) -> float | None:
    """How late a flight actually pushed back, once it's departed -- distinct
    from minutes_since_due_departure, which only applies pre-departure."""
    dep_scheduled = row.get("dep_scheduled")
    dep_actual = row.get("dep_actual")
    if not _present(dep_scheduled) or not _present(dep_actual):
        return None
    delta = (dep_actual - dep_scheduled).total_seconds() / 60
    return round(delta, 1) if delta > 0 else 0.0


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
    merged["departure_delay_minutes"] = merged.apply(_departure_delay_minutes, axis=1)
    merged["eta"] = merged.apply(_eta, axis=1)
    merged["is_overdue"] = merged.apply(_is_overdue, axis=1)
    merged["is_diverting"] = merged["status"] == "Diverted"

    merged = merged.sort_values(
        by="dep_scheduled", na_position="last"
    ).reset_index(drop=True)
    return merged
