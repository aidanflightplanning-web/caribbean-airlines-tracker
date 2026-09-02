"""Caribbean Airlines (BWA) flight tracker — dedicated dispatch-office display.

Combines live ADS-B data (community aggregator: adsb.lol/adsb.fi) with
Caribbean Airlines' own flight-status data (looked up per airborne flight
number) into a single large-format table meant for an unattended wall/
office monitor. Auto-refreshes the page every 60 seconds. See flights.py
for why this only covers flights that have already departed (ADS-B
discoverable), not the full day's schedule.
"""

import html
import time

import pandas as pd
import streamlit as st

from flights import (
    airborne_flight_numbers,
    build_flight_table,
    fetch_caribbean_status,
    fetch_live_positions,
)

REFRESH_SECONDS = 60

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

KIOSK_CSS = """
<meta http-equiv="refresh" content="%d">
<style>
    #MainMenu, header, footer { visibility: hidden; }
    .block-container { padding-top: 2rem; max-width: 100%%; }

    h1 { font-size: 3.2rem !important; }
    .subtitle { font-size: 1.3rem; color: #888; margin-top: -0.8rem; margin-bottom: 1.5rem; }
    .footer-note { font-size: 1.05rem; color: #888; margin-top: 1.2rem; }

    div[data-testid="stAlert"] { font-size: 1.4rem; padding: 1rem 1.2rem; }

    table.flight-board { width: 100%%; border-collapse: collapse; font-size: 1.5rem; }
    table.flight-board th {
        text-align: left; padding: 16px 20px; border-bottom: 3px solid #ccc;
        font-size: 1.05rem; text-transform: uppercase; letter-spacing: 0.04em;
        color: #888; font-weight: 600;
    }
    table.flight-board td {
        padding: 18px 20px; border-bottom: 1px solid #eee; white-space: nowrap;
    }
    table.flight-board tr:nth-child(even) { background: rgba(127, 127, 127, 0.06); }

    .status-badge {
        display: inline-block; padding: 8px 20px; border-radius: 8px;
        color: white; font-weight: 700; font-size: 1.3rem;
    }
</style>
""" % REFRESH_SECONDS


def status_badge(value: str) -> str:
    color = STATUS_COLORS.get(value, "#7a7a7a")
    return f'<span class="status-badge" style="background-color:{color}">{html.escape(value)}</span>'


def fmt_time(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return value.strftime("%Y-%m-%d %H:%M") + "Z"


def fmt_minutes(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{int(value)} min"


def render_table(display: pd.DataFrame) -> str:
    header_cells = "".join(f"<th>{html.escape(col)}</th>" for col in display.columns)
    rows_html = []
    for _, row in display.iterrows():
        cells = []
        for col, value in row.items():
            cell = status_badge(value) if col == "Status" else html.escape(str(value))
            cells.append(f"<td>{cell}</td>")
        rows_html.append(f"<tr>{''.join(cells)}</tr>")
    return (
        '<table class="flight-board">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        "</table>"
    )


def main():
    st.markdown(KIOSK_CSS, unsafe_allow_html=True)

    st.title("✈️ Caribbean Airlines Flight Tracker")
    st.markdown(
        '<div class="subtitle">Callsign BWA · Live position via community ADS-B feed · '
        "Schedule/status via Caribbean Airlines</div>",
        unsafe_allow_html=True,
    )

    try:
        positions_df = fetch_live_positions()
    except Exception as exc:
        st.error(f"Failed to fetch live position data: {exc}")
        positions_df = pd.DataFrame()

    flight_numbers = airborne_flight_numbers(positions_df)

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
            "the next automatic refresh."
        )

    table = build_flight_table(positions_df, cal_df)

    if table.empty:
        st.info("No BWA flights currently airborne.")
        st.markdown(
            f'<div class="footer-note">Auto-refreshing every {REFRESH_SECONDS}s · '
            f'Last checked {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}</div>',
            unsafe_allow_html=True,
        )
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

    st.markdown(render_table(display), unsafe_allow_html=True)

    st.markdown(
        f'<div class="footer-note">Auto-refreshing every {REFRESH_SECONDS}s · '
        f'Last updated {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())} · '
        f"{len(display)} flight(s) shown</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
