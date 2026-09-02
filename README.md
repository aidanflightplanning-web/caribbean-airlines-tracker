# Caribbean Airlines Flight Tracker

A large-format Streamlit dashboard for a dispatch office monitor, tracking Caribbean Airlines flights (ICAO callsign `BWA`). Shows flight status, scheduled departure time, minutes elapsed since a delayed flight was due to depart, and ETA — with warning banners for diverting or overdue aircraft (not landed 30+ minutes after scheduled arrival). Auto-refreshes every 60 seconds; no interaction needed once it's up on the monitor.

**No API key or account required.**

## Data sources

| Source | Provides |
|---|---|
| [adsb.lol](https://adsb.lol) (falling back to [adsb.fi](https://adsb.fi)) | Live ADS-B position, on-ground status, and *which* BWA flight numbers are currently airborne |
| Caribbean Airlines' own internal flight-status feed | Scheduled times, actual/estimated departure & arrival (MVT data), flight status, tail number — looked up per flight number |

Neither source alone is enough, so they're merged by callsign/flight number.

### Why not OpenSky?

The first version of this app used [OpenSky Network](https://opensky-network.org/) for live position. That works fine locally, but **fails once deployed to Streamlit Community Cloud** with a connection timeout — OpenSky is known to block/throttle traffic from major cloud-hosting IP ranges (AWS, GCP, etc.) to fight bot abuse, and Community Cloud runs on GCP. (OpenSky has also asked not to be petitioned for "AI dashboard" whitelisting, so that's not a path forward either.)

[adsb.lol](https://adsb.lol) and [adsb.fi](https://adsb.fi) are community-run alternatives built specifically for open, bulk, no-registration consumption, and don't have this problem. They use the same underlying data format (the readsb/tar1090 API family also used by airplanes.live and others), so the swap was a like-for-like replacement. If one of these ever starts blocking cloud traffic too, the fix is the same shape: swap in another provider from that family, or self-host the app outside a major cloud provider's IP ranges.

### Important limitation: airborne flights only

Caribbean Airlines' flight-status lookup (`caribbean-airlines.com`'s own "Flight Status" search) is **not a public/documented API** — it's the same internal endpoint their website's flight-status page calls, discovered by inspecting network traffic. It works well for a single flight number, but testing showed it's protected by rate-limiting/WAF: querying it rapidly across many routes to build a full day's schedule got this session's IP blocked with HTTP 429 within seconds.

To stay well under that limit, this app **only looks up flight numbers that are already confirmed airborne** via the ADS-B feed — a small, naturally bounded set (Caribbean Airlines' whole fleet), queried one at a time with a ~1.5s pause between requests. The consequence:

- A flight that hasn't pushed back yet is invisible to ADS-B, and therefore to this app, until it's actually in the air. **"Minutes since due to depart" only becomes visible once a flight is airborne** (at which point it's usually near-zero) — it won't warn you about a flight still sitting at the gate.
- If you need pre-departure delay tracking too, the practical options are: (a) give me a short watchlist of specific flight numbers to also poll proactively (same endpoint, same pacing, just queried unconditionally instead of only-when-airborne), or (b) use a real schedule API (e.g. a free [AviationStack](https://aviationstack.com/) key) instead of/alongside this.
- Because this hits an undocumented, unofficial endpoint, it can break or get blocked entirely if Caribbean Airlines changes their site — there's no SLA or ToS coverage here. Treat it as best-effort, not a guaranteed feed.

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
5. Open the deployed URL full-screen on the office monitor — it refreshes itself every 60s, no interaction needed.

## Notes / known limitations

- Times are shown in UTC ("Z"), the standard aviation/dispatch convention.
- "Overdue" triggers 30 minutes past scheduled arrival if the flight hasn't landed; "Diverting" triggers on Caribbean Airlines' own "Diverted" status if/when it appears in their feed.
- The ADS-B query is a single point+radius search (4,000 nm from Piarco/POS) covering CAL's entire route network in one request, cached for 60s.
- The CAL status lookup is cached for 5 minutes and paced at ~1.5s/request; if it still gets rate-limited mid-refresh, the app shows a warning and whatever partial results it collected, and retries on the next cycle.
