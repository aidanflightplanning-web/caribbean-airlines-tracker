# Caribbean Airlines Flight Tracker

A Streamlit dashboard for dispatchers tracking Caribbean Airlines flights (ICAO callsign `BWA`). Shows flight status, scheduled departure time, minutes elapsed since a delayed flight was due to depart, and ETA — with warning banners for diverting or overdue aircraft (not landed 30+ minutes after scheduled arrival).

**No API key or account required.**

## Data sources

| Source | Provides |
|---|---|
| [OpenSky Network](https://opensky-network.org/) | Live ADS-B position, on-ground status, and *which* BWA flight numbers are currently airborne |
| Caribbean Airlines' own internal flight-status feed | Scheduled times, actual/estimated departure & arrival (MVT data), flight status, tail number — looked up per flight number |

Neither source alone is enough, so they're merged by callsign/flight number.

### Important limitation: airborne flights only

Caribbean Airlines' flight-status lookup (`caribbean-airlines.com`'s own "Flight Status" search) is **not a public/documented API** — it's the same internal endpoint their website's flight-status page calls, discovered by inspecting network traffic. It works well for a single flight number, but testing showed it's protected by rate-limiting/WAF: querying it rapidly across many routes to build a full day's schedule got this session's IP blocked with HTTP 429 within seconds.

To stay well under that limit, this app **only looks up flight numbers that OpenSky has already confirmed are airborne** — a small, naturally bounded set (Caribbean Airlines' whole fleet), queried one at a time with a ~1.5s pause between requests. The consequence:

- A flight that hasn't pushed back yet is invisible to OpenSky, and therefore to this app, until it's actually in the air. **"Minutes since due to depart" only becomes visible once a flight is airborne** (at which point it's usually near-zero) — it won't warn you about a flight still sitting at the gate.
- If you need pre-departure delay tracking too, the practical options are: (a) give me a short watchlist of specific flight numbers to also poll proactively (same endpoint, same pacing, just queried unconditionally instead of only-when-airborne), or (b) use a real schedule API (e.g. a free [AviationStack](https://aviationstack.com/) key) instead of/alongside this.
- Because this hits an undocumented, unofficial endpoint, it can break or get blocked entirely if Caribbean Airlines changes their site — there's no SLA or ToS coverage here. Treat it as best-effort, not a guaranteed feed, and keep OpenSky (which is a legitimate public API) as the fallback for at least basic live position.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(Optional) copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and add OpenSky credentials if you want a higher live-position rate limit — the app runs fine with none set.

```bash
streamlit run app.py
```

## Deploying (GitHub + Streamlit Community Cloud)

1. Push this folder to a new GitHub repository.
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with GitHub, and click **New app**.
3. Point it at your repo, branch `main`, main file `app.py`.
4. (Optional) In the app's **Settings → Secrets**, paste OpenSky credentials if you have them.
5. Deploy. The app auto-redeploys on every push to `main`.

## Notes / known limitations

- Times are shown in UTC ("Z"), the standard aviation/dispatch convention.
- "Overdue" triggers 30 minutes past scheduled arrival if the flight hasn't landed; "Diverting" triggers on Caribbean Airlines' own "Diverted" status if/when it appears in their feed.
- OpenSky's `states/all` endpoint has no server-side callsign filter, so the app fetches all current global states and filters client-side for anything starting with `BWA`.
- The CAL status lookup is cached for 5 minutes and paced at ~1.5s/request; if it still gets rate-limited mid-refresh, the app shows a warning and whatever partial results it collected, and retries on the next cycle.
