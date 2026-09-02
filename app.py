"""Caribbean Airlines (BWA) flight tracker for dispatchers.

Combines live ADS-B data (OpenSky Network) with Caribbean Airlines' own
flight-status data (looked up per airborne flight number) into a single
dispatch-facing table. See flights.py for why this only covers flights
that have already departed (OpenSky-discoverable), not the full day's
schedule.
"""

import time

import pandas as pd
import streamlit as st

from flights import (
    airborne_flight_numbers,
    build_flight_table,
    fetch_caribbean_status,
    fetch_opensky_states,
)

st.set_page_config(page_title="Caribbean Airlines Flight Tracker", page_icon="✈️", layout="wide")

STATUS_COLORS = {
    "On Time": "#1e7e34",
    "Scheduled": "#1e7e34",
    "En Route": "#0b6ea8",
    "Landed": "#5a5a5a",
    "Delayed": "#b8860b",
    "Cancelled": "#a61b1b",
    "Diverted": "#a61b1b",
    "Incident": "#a61b1b",
    "Unknown": "#7a7a7a",
}


def style_status(value: str) -> str:
    color = STATUS_COLORS.get(value, "#7a7a7a")
    return f"background-color: {color}; color: white; font-weight: 600; text-align: center; border-radius: 4px;"


def fmt_time(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return value.strftime("%Y-%m-%d %H:%M") + "Z"


def fmt_minutes(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{int(value)} min"


def main():
    st.title("✈️ Caribbean Airlines Flight Tracker")
    st.caption("Callsign BWA · Live position via OpenSky Network · Schedule/status via Caribbean Airlines")

    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Refresh now", use_container_width=True):
            fetch_opensky_states.clear()
            fetch_caribbean_status.clear()
            st.rerun()

        st.caption(
            "Shows flights OpenSky has already spotted airborne — a flight "
            "still on the ground pre-departure won't appear until wheels-up. "
            "Schedule/status lookups are paced and cached for 5 minutes to "
            "avoid tripping Caribbean Airlines' own rate limiting; use "
            "Refresh now sparingly."
        )

    try:
        opensky_df = fetch_opensky_states()
    except Exception as exc:
        st.error(f"Failed to fetch OpenSky data: {exc}")
        opensky_df = pd.DataFrame()

    flight_numbers = airborne_flight_numbers(opensky_df)

    cal_df = pd.DataFrame()
    rate_limited = False
    try:
        cal_df, rate_limited = fetch_caribbean_status(flight_numbers)
    except Exception as exc:
        st.error(f"Failed to fetch Caribbean Airlines status data: {exc}")

    if rate_limited:
        st.warning(
            "Caribbean Airlines' status endpoint rate-limited this request "
            "partway through — showing partial results. It will retry on "
            "the next refresh."
        )

    table = build_flight_table(opensky_df, cal_df)

    if table.empty:
        st.info("No BWA flights currently airborne.")
        return

    overdue = table[table["is_overdue"]]
    diverting = table[table["is_diverting"]]

    for _, row in diverting.iterrows():
        st.error(
            f"⚠️ **DIVERTING** — {row.get('flight_iata') or row.get('callsign')} "
            f"({row.get('dep_iata') or '?'} → {row.get('arr_iata') or '?'})"
        )
    for _, row in overdue.iterrows():
        st.warning(
            f"⏰ **OVERDUE** — {row.get('flight_iata') or row.get('callsign')} "
            f"has not landed more than 30 min after its scheduled arrival "
            f"({fmt_time(row.get('arr_scheduled'))})"
        )

    display = pd.DataFrame({
        "Flight": table["flight_iata"].fillna(table["callsign"]),
        "Route": table["dep_iata"].fillna("?") + " → " + table["arr_iata"].fillna("?"),
        "Status": table["status"],
        "Scheduled Dep.": table["dep_scheduled"].apply(fmt_time),
        "Min. Since Due": table["minutes_since_due_departure"].apply(fmt_minutes),
        "ETA": table["eta"].apply(fmt_time),
        "Tail #": table["tailnumber"].fillna("—"),
        "On Ground": table["on_ground"].map({True: "Yes", False: "No"}).fillna("—"),
    })

    styled = display.style.map(lambda v: style_status(v), subset=["Status"])

    st.dataframe(styled, use_container_width=True, hide_index=True, height=min(600, 60 + 35 * len(display)))

    st.caption(f"Last updated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} · {len(display)} flight(s) shown")


if __name__ == "__main__":
    main()
