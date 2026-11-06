# UniFi Watch

Searches **hardverapro.hu** and **jofogas.hu** for used **UniFi access points and switches**
three times a day (morning, noon, evening), keeps a list with links, and sends a **push
notification to your phone** when a new ad shows up.

- 📋 List: [`LISTINGS.md`](LISTINGS.md), or the web page in `docs/` (GitHub Pages)
- 🔔 Push: [ntfy](https://ntfy.sh) (free app for Android / iOS, no account needed)
- ⏰ Schedule: 07:00, 12:00 and 19:00 Budapest time (08:00 / 13:00 / 20:00 in summer), via GitHub Actions

## Setup (5 minutes)

1. **Phone app** – install *ntfy* ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) /
   [iOS](https://apps.apple.com/app/ntfy/id1625396347)), tap **+** and subscribe to a topic with a long,
   hard-to-guess name, e.g. `unifi-watch-7f3k9q2x`. (Anyone who knows the topic name can read it, so
   treat it like a password.)
2. **Secret** – in this repo: *Settings → Secrets and variables → Actions → New repository secret*,
   name `NTFY_TOPIC`, value = your topic name.
3. **Web page (optional)** – *Settings → Pages → Source: Deploy from a branch → `main` / `/docs`*.
   Then add the page address as an Actions **variable** `PAGE_URL`
   (e.g. `https://<user>.github.io/unifi-watch/`) so the notification opens the full list.
   (Pages on private repos needs a paid GitHub plan. `LISTINGS.md` works either way.)
4. **First run** – *Actions → UniFi search → Run workflow*. The first run builds the list and sends one
   "UniFi figyelő elindult" push so you can see it works. After that you only get a push when there are new ads.

## How it works

| Site | What is read |
|---|---|
| Jófogás | The keyword search `unifi` (all of Hungary, up to 5 result pages) |
| HardverApró | The newest ads in *Hálózati termékek*, *Router, switch, repeater* and *Szerver hálózati eszközök*. HardverApró's keyword search is not served to automated clients, so new ads are caught as they appear (3× a day is plenty). Ads that have dropped out of that window stay on the list for 21 days. |

Each title is classified as **Access point** or **Switch** (`classify()` in `scraper.py`). Gateways,
Cloud Keys, cameras, SFP modules, covers and "wanted" ads are left out. Edit the pattern lists at the top of
`scraper.py` to change what counts.

After each run the workflow commits `data/listings.json`, `LISTINGS.md` and `docs/index.html`, so the
repo history is also a price history.

If a site is down or rate-limits a run, the error shows at the top of the list, and that site's
previous results are kept until the next successful run.

## Options

| Name | Kind | Purpose |
|---|---|---|
| `NTFY_TOPIC` | secret | ntfy topic to push to (required for notifications) |
| `NTFY_TOKEN` | secret | access token if you use a protected topic / own server |
| `NTFY_SERVER` | variable | your own ntfy server, default `https://ntfy.sh` |
| `PAGE_URL` | variable | link opened when tapping a notification with several new ads |

Change the run times in `.github/workflows/search.yml` (`cron`, in UTC).

## Run locally

```bash
pip install -r requirements.txt
NTFY_TOPIC=your-topic python scraper.py
```
