# Caribbean Airlines Flight Tracker

A Streamlit dashboard for dispatchers tracking Caribbean Airlines flights (ICAO callsign `BWA`). Shows flight status, scheduled departure time, minutes elapsed since a delayed flight was due to depart, and ETA — with warning banners for diverting or overdue aircraft (not landed 30+ minutes after scheduled arrival).

## Data sources

| Source | Provides | Limits |
|---|---|---|
| [OpenSky Network](https://opensky-network.org/) | Live ADS-B position & on-ground status | Free; anonymous access works but a free registered account raises the rate limit |
| [AviationStack](https://aviationstack.com/) | Scheduled/estimated times, delay minutes, flight status (scheduled/active/landed/cancelled/diverted) | **Free tier is 100 requests/month** — the app caches this data for 30 minutes and does not auto-poll it aggressively |

Neither source alone is enough: OpenSky has no schedule, AviationStack's free tier has no live position. The app matches records from both by ICAO callsign (e.g. `BWA526`).

If you outgrow AviationStack's free tier, this is the piece to swap for a paid feed (AviationStack paid plan, FlightAware AeroAPI, Cirium, etc.) — only `flights.py::fetch_aviationstack_flights` needs to change.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Edit `.streamlit/secrets.toml` and add your AviationStack API key (get one free at [aviationstack.com](https://aviationstack.com/)). OpenSky credentials are optional.

```bash
streamlit run app.py
```

## Deploying (GitHub + Streamlit Community Cloud)

1. Push this folder to a new GitHub repository.
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with GitHub, and click **New app**.
3. Point it at your repo, branch `main`, main file `app.py`.
4. In the app's **Settings → Secrets**, paste the same keys from `secrets.toml.example` with your real values. Do **not** commit `.streamlit/secrets.toml` — it's gitignored.
5. Deploy. The app auto-redeploys on every push to `main`.

## Notes / known limitations

- **AviationStack free tier (100 req/month)** is the binding constraint on how "live" the schedule/status columns can be. The sidebar "Refresh now" button clears the cache immediately — use it sparingly, or upgrade the AviationStack plan for production dispatch use.
- Times are shown in UTC ("Z"), the standard aviation/dispatch convention.
- "Minutes since due" only shows for flights that haven't actually departed yet per AviationStack's `departure.actual` field.
- "Overdue" triggers 30 minutes past `arrival.scheduled` if the flight hasn't landed; "Diverting" triggers on AviationStack's `diverted` status.
- OpenSky's `states/all` endpoint has no server-side callsign filter, so the app fetches all current states and filters client-side for anything starting with `BWA`.
