"""Caribbean Airlines (BWA) flight tracker for dispatchers.

Combines live ADS-B data (OpenSky Network) with scheduled times and flight
status (AviationStack) into a single dispatch-facing table.
"""

import time

import pandas as pd
import streamlit as st

from flights import build_flight_table, fetch_aviationstack_flights, fetch_opensky_states, get_secret

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
    st.caption("Callsign BWA · Live position via OpenSky Network · Schedule/status via AviationStack")

    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Refresh now", use_container_width=True):
            fetch_opensky_states.clear()
            fetch_aviationstack_flights.clear()
            st.rerun()

        st.caption(
            "Live position refreshes automatically about every 60s. "
            "Schedule/status data is cached for 30 minutes to conserve "
            "AviationStack's free-tier quota (100 requests/month) — use "
            "Refresh now sparingly."
        )

        if not get_secret("AVIATIONSTACK_API_KEY"):
            st.warning(
                "No AVIATIONSTACK_API_KEY set. The table will show live "
                "position only, with no schedule, status, or ETA data. "
                "Add a key in .streamlit/secrets.toml (see README)."
            )

    try:
        opensky_df = fetch_opensky_states()
    except Exception as exc:
        st.error(f"Failed to fetch OpenSky data: {exc}")
        opensky_df = pd.DataFrame()

    try:
        aviationstack_df = fetch_aviationstack_flights()
    except Exception as exc:
        st.error(f"Failed to fetch AviationStack data: {exc}")
        aviationstack_df = pd.DataFrame()

    table = build_flight_table(opensky_df, aviationstack_df)

    if table.empty:
        st.info("No BWA flights currently found in either data source.")
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
            f"has not landed more than {int(30)} min after its scheduled arrival "
            f"({fmt_time(row.get('arr_scheduled'))})"
        )

    display = pd.DataFrame({
        "Flight": table["flight_iata"].fillna(table["callsign"]),
        "Route": table["dep_iata"].fillna("?") + " → " + table["arr_iata"].fillna("?"),
        "Status": table["status"],
        "Scheduled Dep.": table["dep_scheduled"].apply(fmt_time),
        "Min. Since Due": table["minutes_since_due_departure"].apply(fmt_minutes),
        "ETA": table["eta"].apply(fmt_time),
        "On Ground": table["on_ground"].map({True: "Yes", False: "No"}).fillna("—"),
    })

    styled = display.style.map(lambda v: style_status(v), subset=["Status"])

    st.dataframe(styled, use_container_width=True, hide_index=True, height=min(600, 60 + 35 * len(display)))

    st.caption(f"Last updated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} · {len(display)} flight(s) shown")


if __name__ == "__main__":
    main()
