"""Caribbean Airlines (BWA) flight tracker — dedicated dispatch-office display.

Combines live ADS-B data (community aggregator: adsb.lol/adsb.fi), CAL's
own published schedule (to find flight numbers ADS-B might miss), and
CAL's live flight-status data into a single large-format table meant for
an unattended wall/office monitor. Auto-refreshes every REFRESH_SECONDS.
See flights.py for the full picture of how these three sources combine.
"""

import html
import time

import pandas as pd
import streamlit as st

from flights import (
    active_scheduled_flight_numbers,
    airborne_flight_numbers,
    build_flight_table,
    fetch_caribbean_status,
    fetch_live_positions,
    fetch_schedule_roster,
    sticky_candidates,
    update_active_registry,
    watchlist_flight_numbers,
)

REFRESH_SECONDS = 600

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
    /* Sized in vh (viewport height) throughout, not rem/px, so the whole
       board scales to fill whatever the actual overhead monitor's
       resolution is and stays a single no-scroll screen rather than being
       tuned to one specific display. */
    #MainMenu, header, footer { visibility: hidden; }
    .block-container { padding-top: 1vh; padding-bottom: 1vh; max-width: 100%%; }
    div[data-testid="stVerticalBlock"] { gap: 0.4vh; }
    .element-container { margin-bottom: 0 !important; }

    h1 { font-size: 2.6vh !important; margin: 0 !important; line-height: 1.2 !important; }
    .subtitle { font-size: 1.3vh; color: #888; margin: 0 0 0.6vh 0; }
    .footer-note { font-size: 1.1vh; color: #888; margin-top: 0.6vh; }

    div[data-testid="stAlert"] { font-size: 1.5vh; padding: 0.5vh 0.8vh; }
    div[data-testid="stAlert"] p { font-size: 1.5vh; margin: 0; }

    table.flight-board { width: 100%%; border-collapse: collapse; font-size: 1.9vh; }
    table.flight-board th {
        text-align: left; padding: 0.5vh 0.8vh; border-bottom: 0.25vh solid #ccc;
        font-size: 1.2vh; text-transform: uppercase; letter-spacing: 0.04em;
        color: #888; font-weight: 600; white-space: nowrap;
    }
    table.flight-board td {
        padding: 0.5vh 0.8vh; border-bottom: 0.1vh solid #eee; white-space: nowrap;
    }
    table.flight-board tr:nth-child(even) { background: rgba(127, 127, 127, 0.06); }

    .status-badge {
        display: inline-block; padding: 0.3vh 1vh; border-radius: 0.6vh;
        color: white; font-weight: 700; font-size: 1.6vh;
    }
</style>
""" % REFRESH_SECONDS


def status_badge(value: str) -> str:
    color = STATUS_COLORS.get(value, "#7a7a7a")
    return f'<span class="status-badge" style="background-color:{color}">{html.escape(value)}</span>'


def fmt_refresh_interval(seconds: int) -> str:
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} min" if minutes != 1 else "1 min"
    return f"{seconds}s"


def fmt_time(value) -> str:
    """Time-of-day only (no date) to keep rows compact — this board only
    ever shows flights within roughly a day of "now" anyway (see flights.py's
    staleness cutoff), so the date rarely adds information worth the width."""
    if value is None or pd.isna(value):
        return "—"
    return value.strftime("%H:%M") + "Z"


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
        "Schedule &amp; status via Caribbean Airlines</div>",
        unsafe_allow_html=True,
    )

    try:
        positions_df = fetch_live_positions()
    except Exception as exc:
        st.error(f"Failed to fetch live position data: {exc}")
        positions_df = pd.DataFrame()

    try:
        roster = fetch_schedule_roster()
    except Exception as exc:
        st.error(f"Failed to fetch Caribbean Airlines schedule: {exc}")
        roster = []

    discovered = set(airborne_flight_numbers(positions_df)) | set(active_scheduled_flight_numbers(roster))
    flight_numbers = sticky_candidates(tuple(sorted(discovered)), watchlist_flight_numbers())

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
    update_active_registry(table)

    if table.empty:
        st.info("No BWA flights currently airborne.")
        st.markdown(
            f'<div class="footer-note">Auto-refreshing every {fmt_refresh_interval(REFRESH_SECONDS)} · '
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
        "Sched. Dep.": table["dep_scheduled"].apply(fmt_time),
        "Min. Since Due": table["minutes_since_due_departure"].apply(fmt_minutes),
        "Dep. Delay": table["departure_delay_minutes"].apply(fmt_minutes),
        "ETA": table["eta"].apply(fmt_time),
        "Tail #": table["tailnumber"].fillna("—"),
        "On Ground": table["on_ground"].map({True: "Yes", False: "No"}).fillna("—"),
    })

    st.markdown(render_table(display), unsafe_allow_html=True)

    st.markdown(
        f'<div class="footer-note">Auto-refreshing every {fmt_refresh_interval(REFRESH_SECONDS)} · '
        f'Last updated {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())} · '
        f"{len(display)} flight(s) shown</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
