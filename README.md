# Caribbean Airlines Flight Tracker

A large-format Streamlit dashboard for a dispatch office monitor, tracking Caribbean Airlines flights (ICAO callsign `BWA`). Shows flight status, scheduled departure time, minutes elapsed since a delayed flight was due to depart, and ETA — with warning banners for diverting or overdue aircraft (not landed 30+ minutes after scheduled arrival). Auto-refreshes every 10 minutes; no interaction needed once it's up on the monitor.

**No API key or account required.**

## Data sources

| Source | Provides |
|---|---|
| [adsb.lol](https://adsb.lol) **and** [adsb.fi](https://adsb.fi) (unioned, not failover) | Live ADS-B position, on-ground status, and *which* BWA flight numbers are currently airborne |
| Caribbean Airlines' own internal flight-status feed | Scheduled times, actual/estimated departure & arrival (MVT data), flight status, tail number — looked up per flight number |

Neither source alone is enough, so they're merged by callsign/flight number.

### Why not OpenSky?

The first version of this app used [OpenSky Network](https://opensky-network.org/) for live position. That works fine locally, but **fails once deployed to Streamlit Community Cloud** with a connection timeout — OpenSky is known to block/throttle traffic from major cloud-hosting IP ranges (AWS, GCP, etc.) to fight bot abuse, and Community Cloud runs on GCP. (OpenSky has also asked not to be petitioned for "AI dashboard" whitelisting, so that's not a path forward either.)

[adsb.lol](https://adsb.lol) and [adsb.fi](https://adsb.fi) are community-run alternatives built specifically for open, bulk, no-registration consumption, and don't have this problem. They use the same underlying data format (the readsb/tar1090 API family also used by airplanes.live and others), so the swap was a like-for-like replacement.

**Neither has OpenSky's coverage, though.** Verified by cross-checking live: adsb.lol alone missed real BWA flights that OpenSky picked up, and even querying both adsb.lol *and* adsb.fi together (unioned, currently what the app does) still occasionally misses one that OpenSky has. This is a genuine, unresolved gap in community-run crowdsourced ADS-B coverage versus OpenSky's larger, more established volunteer network — not a bug to "fix," just a real tradeoff of not being able to use OpenSky from this hosting environment. The watchlist below is the direct workaround for any specific flight this matters for, and the sticky-tracking below softens the symptom for everything else.

### Why flights don't flicker on and off anymore

Early versions of this app re-derived "which flights to show" from scratch every refresh, purely from that cycle's ADS-B query. Since ADS-B coverage has real gaps (above), a genuinely-still-flying aircraft could vanish from the table for a cycle and reappear later — and worse, because the Caribbean Airlines lookup was cached as one batch keyed by the *exact set* of flight numbers being asked about, that flicker at the ADS-B layer forced a full batch re-fetch every time, and a single rate-limit hit partway through could drop *every* flight after it in that batch, even ones already known-good.

Two fixes address this:
- **Per-flight caching**: each flight number's Caribbean Airlines status is now cached independently (10 minutes, matching the refresh cadence), so a change in which flights are airborne doesn't force-refetch everything, and a rate-limit hit on one new lookup can't drop flights already fetched.
- **Sticky tracking**: once a flight is confirmed active (via ADS-B or CAL), it stays in the "keep checking this" set for 20 minutes after its last confirmation, even if a later cycle's ADS-B query misses it — so a brief coverage gap doesn't drop it off the display. It's removed promptly once CAL itself confirms the flight has landed or been cancelled, not just after the grace period expires.

### Important limitation: airborne (or watchlisted) flights only

Caribbean Airlines' flight-status lookup (`caribbean-airlines.com`'s own "Flight Status" search) is **not a public/documented API** — it's the same internal endpoint their website's flight-status page calls, discovered by inspecting network traffic. It works well for a single flight number, but testing showed it's protected by rate-limiting/WAF: querying it rapidly across many routes to build a full day's schedule got this session's IP blocked with HTTP 429 within seconds.

To stay well under that limit, this app only looks up flight numbers that are **confirmed airborne via ADS-B, on your watchlist, or still within their sticky grace period** (see below) — queried one at a time with a ~1.5s pause between requests, and only for numbers that don't already have fresh cached data. The consequence for anything not on the watchlist:

- A flight that hasn't pushed back yet is invisible to ADS-B, and therefore to this app, until it's actually in the air. **"Minutes since due to depart" only becomes visible once a flight is airborne** (at which point it's usually near-zero) — it won't warn you about a flight still sitting at the gate.
- Because this hits an undocumented, unofficial endpoint, it can break or get blocked entirely if Caribbean Airlines changes their site — there's no SLA or ToS coverage here. Treat it as best-effort, not a guaranteed feed.

### Watchlist: always check specific flights

Set the `WATCHLIST_FLIGHT_NUMBERS` secret (comma-separated, e.g. `"526,217,415"`, no "BW" prefix) to a short list of flight numbers that should always be checked directly against Caribbean Airlines' status feed — regardless of whether any ADS-B source currently shows them airborne. This is the fix for a specific flight intermittently not showing up: it bypasses ADS-B detection entirely for anything on the list, and as a side benefit also surfaces pre-departure delays for those flights (the one thing airborne-only detection structurally can't do). Keep the list short — each entry is one more paced request every refresh cycle, on top of whatever's airborne.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

No secrets needed. (`.streamlit/secrets.toml.example` is kept as a placeholder in case you add a secret later.)

## Deploying (GitHub + Streamlit Community Cloud)

1. Push this folder to a GitHub repository.
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with GitHub, and click **New app**.
3. Point it at your repo, branch `main`, main file `app.py`.
4. Deploy. The app auto-redeploys on every push to `main`.
5. Open the deployed URL full-screen on the office monitor — it refreshes itself every 10 minutes, no interaction needed.

## Notes / known limitations

- Times are shown in UTC ("Z"), the standard aviation/dispatch convention.
- "Overdue" triggers 30 minutes past scheduled arrival if the flight hasn't landed; "Diverting" triggers on Caribbean Airlines' own "Diverted" status if/when it appears in their feed.
- The ADS-B query is a single point+radius search (4,000 nm from Piarco/POS) covering CAL's entire route network in one request, cached for 60s.
- The CAL status lookup is cached per flight number for 10 minutes and paced at ~1.5s/request (only for numbers not already cached); if it still gets rate-limited mid-refresh, the app shows a warning and whatever results it already has, and retries the missing ones on the next cycle.
