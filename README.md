# Caribbean Airlines Flight Tracker

A Streamlit dashboard for a dispatch office monitor, tracking Caribbean Airlines flights (ICAO callsign `BWA`). Shows flight status, scheduled departure time, minutes elapsed since a delayed flight was due to depart, how many minutes late a flight actually pushed back, and ETA — with a warning banner for diverting aircraft. Auto-refreshes every 5 minutes; no interaction needed once it's up on the monitor. Sized in viewport-relative units so the whole board fits on one screen without scrolling, regardless of the monitor's actual resolution.

**No API key or account required.**

## Data sources

| Source | Provides |
|---|---|
| Caribbean Airlines' own published timetable | The actual flight-number roster — which BW numbers exist, their day-of-week pattern, and scheduled local times. Used to compute which flight numbers are plausibly operating *right now*, independent of ADS-B |
| [adsb.lol](https://adsb.lol) **and** [adsb.fi](https://adsb.fi) (unioned, not failover) | Live position and on-ground status, plus a supplementary way to catch any flight number the schedule-based check might miss |
| Caribbean Airlines' own internal flight-status feed | Scheduled times, actual/estimated departure & arrival (MVT data), flight status, tail number — looked up per flight number |

All three are merged by callsign/flight number. The schedule timetable is the primary discovery mechanism (see below) — ADS-B is now a supplement to it, not the only way a flight gets found.

### Why not OpenSky?

The first version of this app used [OpenSky Network](https://opensky-network.org/) for live position. That works fine locally, but **fails once deployed to Streamlit Community Cloud** with a connection timeout — OpenSky is known to block/throttle traffic from major cloud-hosting IP ranges (AWS, GCP, etc.) to fight bot abuse, and Community Cloud runs on GCP. (OpenSky has also asked not to be petitioned for "AI dashboard" whitelisting, so that's not a path forward either.)

[adsb.lol](https://adsb.lol) and [adsb.fi](https://adsb.fi) are community-run alternatives built specifically for open, bulk, no-registration consumption, and don't have this problem. They use the same underlying data format (the readsb/tar1090 API family also used by airplanes.live and others), so the swap was a like-for-like replacement.

**Neither has OpenSky's coverage, though.** Verified by cross-checking live: adsb.lol alone missed real BWA flights that OpenSky picked up, and even querying both adsb.lol *and* adsb.fi together (unioned) still occasionally misses one that OpenSky has — in one test, even OpenSky itself only saw 1 of 4 genuinely-airborne flights at that moment. This is a real, unresolved gap in community-run crowdsourced ADS-B coverage — not a bug to "fix," just a fact of not being able to use OpenSky from this hosting environment.

### The real fix for missing flights: schedule-based discovery

Relying on ADS-B to *discover* which flight numbers exist turned out to be the wrong approach — no ADS-B source, individually or combined, reliably detects every airborne CAL flight. The actual fix: caribbean-airlines.com has a **"Flight Schedule" page** distinct from the live-status one, backed by `/api/schedule/{origin}/NONE/{month}` — a real published timetable (flight number, route, local departure/arrival times, day-of-week pattern, validity dates). Unlike the live-status endpoint, this one is not aggressively rate-limited (confirmed by testing: querying it for all 25 origin airports in CAL's network back-to-back succeeded without issue).

The app fetches this timetable once per origin airport (cached for 6 hours — schedules don't change intraday), then computes which flight numbers are plausibly operating *right now* from their day-of-week pattern and scheduled local time (using each origin airport's approximate UTC offset, with a 1-hour pre-departure / 30-min post-arrival buffer window). Those flight numbers get checked directly against CAL's live-status feed, regardless of what ADS-B does or doesn't detect. This is what actually resolved specific flights (e.g. BW483, BW485, BW217) not showing up — they were genuinely airborne but consistently missed by every ADS-B source tried.

A landed flight drops off the board roughly **30 minutes after its scheduled arrival time** (that post-arrival buffer). One edge case worth knowing: this is measured from the *scheduled* time, not actual landing — a flight still genuinely delayed and airborne well past that point stays visible only because ADS-B (a separate, independent discovery path) is still detecting it. If ADS-B also happens to miss it at that exact moment, it could briefly drop out of discovery before landing. In practice this is rare, since a flight that's airborne is usually ADS-B-visible.

Two consequences worth knowing:
- Return legs (e.g. BW483 flying MIA→POS) originate from the spoke city, not POS/KIN, so the schedule fetch covers **all 25** of CAL's origin airports, not just the hubs — otherwise half the return flights would be invisible to this layer.
- A flight not in the published schedule at all (a one-off charter, an irregular extra section) still relies on ADS-B or the watchlist to be found — this layer only knows about *regularly scheduled* flight numbers.

The watchlist below remains useful for guaranteeing a specific flight regardless of any of this, and the sticky-tracking further down is a safety net under all three sources.

### Why flights don't flicker on and off anymore

Early versions of this app re-derived "which flights to show" from scratch every refresh, purely from that cycle's ADS-B query. Since ADS-B coverage has real gaps, a genuinely-still-flying aircraft could vanish from the table for a cycle and reappear later — and worse, because the Caribbean Airlines lookup was cached as one batch keyed by the *exact set* of flight numbers being asked about, that flicker forced a full batch re-fetch every time, and a single rate-limit hit partway through could drop *every* flight after it in that batch, even ones already known-good.

Several fixes address this:
- **Schedule-based discovery** (above) is the main one — it makes the candidate set mostly stable cycle to cycle, since it's driven by a published timetable rather than moment-to-moment detection.
- **Per-flight caching**: each flight number's Caribbean Airlines status is cached independently, for 20 minutes — deliberately *longer* than the 5-minute refresh interval, so cache entries expire on a rolling basis rather than all at once forcing a full-batch re-fetch (and, with ~15-20 candidates typical once schedule-based discovery is active, a full-batch re-fetch every cycle is exactly what tripped CAL's rate limiter in testing).
- **Shuffled fetch order**: only lookups that need a real network call (not already cached) get shuffled before pacing through them, so if the rate limiter does trip partway through, it doesn't deterministically cut off the same flights every cycle (sorted order would otherwise always sacrifice the same ones).
- **A staleness cutoff**: if the only Caribbean Airlines record found for a flight number is from more than 6 hours away from now (past or future), it's treated as no current data rather than displayed as if relevant — this prevents a long-cancelled or long-past flight from cluttering the board with misleading figures like "978 min since due."
- **Sticky tracking**: once a flight is confirmed active, it stays in the "keep checking this" set for 20 minutes after its last confirmation, even if a later cycle's discovery misses it — so a brief gap doesn't drop it off the display. It's removed promptly once CAL confirms the flight has landed or been cancelled, not just after the grace period expires.

### Important limitation: airborne (or watchlisted) flights only

Caribbean Airlines' flight-status lookup (`caribbean-airlines.com`'s own "Flight Status" search) is **not a public/documented API** — it's the same internal endpoint their website's flight-status page calls, discovered by inspecting network traffic. It works well for a single flight number, but testing showed it's protected by rate-limiting/WAF: querying it rapidly across many routes to build a full day's schedule got this session's IP blocked with HTTP 429 within seconds.

To stay well under that limit, this app only looks up flight numbers that are **due to be operating soon per the published schedule, confirmed airborne via ADS-B, on your watchlist, or still within their sticky grace period** — queried one at a time with a ~1.5s pause between requests, and only for numbers that don't already have fresh cached data. Because the schedule layer now includes a 1-hour pre-departure window, "minutes since due to depart" can show up *before* wheels-up for scheduled flights — no longer strictly airborne-only. The remaining real limitation:

- Because this hits an undocumented, unofficial endpoint, it can break or get blocked entirely if Caribbean Airlines changes their site — there's no SLA or ToS coverage here. Treat it as best-effort, not a guaranteed feed.
- A flight with no published schedule entry (a one-off charter, an irregular extra section) is still airborne-only: invisible until ADS-B spots it or it's on the watchlist.

### Watchlist: always check specific flights

Set the `WATCHLIST_FLIGHT_NUMBERS` secret (comma-separated, e.g. `"526,217,415"`, no "BW" prefix) to a short list of flight numbers that should always be checked directly against Caribbean Airlines' status feed, unconditionally. With schedule-based discovery in place this matters less than it used to, but it's still the guaranteed override for a flight that's irregular, not in the published timetable, or otherwise falling through every other layer. Keep the list short — each entry is one more paced request every refresh cycle.

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
5. Open the deployed URL full-screen on the office monitor — it refreshes itself every 5 minutes, no interaction needed.

## Notes / known limitations

- Times are shown in UTC ("Z"), the standard aviation/dispatch convention.
- "Diverting" triggers on Caribbean Airlines' own "Diverted" status if/when it appears in their feed. There's no "overdue" warning — that was removed.
- Status is "En Route" for any airborne flight regardless of how late it departed — a delayed departure doesn't keep showing "Delayed" once wheels-up. "Min. Since Due" (a live-updating countdown) only applies before departure; "Dep. Delay" (a fixed figure — how many minutes late it actually pushed back) only applies after.
- The schedule timetable is fetched once per origin airport (25 requests, ~1s apart) and cached for 6 hours.
- The ADS-B query is a single point+radius search (4,000 nm from Piarco/POS) covering CAL's entire route network in one request, cached for 60s.
- The CAL live-status lookup is cached per flight number for 20 minutes and paced at ~1.5s/request (only for numbers not already cached, in shuffled order); if it still gets rate-limited mid-refresh, the app shows a warning and whatever results it already has, and retries the missing ones on the next cycle.
