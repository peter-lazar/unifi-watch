#!/usr/bin/env python3
"""UniFi watch: search hardverapro.hu and jofogas.hu for UniFi access points and
switches, keep a list of the current listings, and send a push notification
(ntfy.sh) for new ones."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "listings.json"
MARKDOWN = ROOT / "LISTINGS.md"
PAGE = ROOT / "docs" / "index.html"
TZ = ZoneInfo("Europe/Budapest")

JOFOGAS_QUERIES = ["unifi"]
# Ads that drop out of HardverApró's newest-100 category window are kept this long
# (we cannot tell whether they were sold). Jófogás ads are removed as soon as they disappear.
HVA_KEEP_DAYS = 21
USER_AGENT = "unifi-watch/1.0 (personal listing alert; +https://github.com)"
TIMEOUT = 30

# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
AP_PATTERNS = [
    r"access ?point", r"\bap\b", r"\buap\b", r"\buap-", r"\bu[67]\b", r"\bu[67][ -+]",
    r"\bu6\+", r"\bu[67]-?(pro|lr|lite|mesh|plus|enterprise|iw|in-wall|outdoor)",
    r"\be7\b", r"\bac[ -]?(lite|lr|pro|mesh|hd|shd|iw)\b", r"nano ?hd", r"flex ?hd",
    r"\buwb-?xg\b", r"\bbeacon ?hd\b", r"in-?wall", r"\bwi-?fi ?[67]?\b.*\b(ap|pont)\b",
    r"\bmesh\b",
]
SWITCH_PATTERNS = [
    r"switch", r"\busw\b", r"\busw-", r"\bus-?\d", r"\bus ?\d{1,2}\b", r"flex ?mini",
    r"\blite ?(8|16)\b", r"\baggregation\b", r"\bport\b.*\b(poe|gigabit)\b",
    r"\bkapcsol",
]
EXCLUDE_PATTERNS = [
    r"\bcover\b", r"\bburkolat", r"\bkamera\b", r"\bcamera\b", r"\bg[345] ?(dome|bullet|flex|pro|instant)",
    r"\bprotect\b", r"\bkeres(ek|ünk)\b", r"\bvenn[eé]k\b",
]


def classify(title: str) -> str | None:
    """Return 'Access point', 'Switch' or None (= not interesting)."""
    t = title.lower()
    if "unifi" not in t and "ubiquiti" not in t and "ubnt" not in t:
        return None
    if any(re.search(p, t) for p in EXCLUDE_PATTERNS):
        return None
    is_sw = any(re.search(p, t) for p in SWITCH_PATTERNS)
    is_ap = any(re.search(p, t) for p in AP_PATTERNS)
    if is_sw and is_ap:
        return "Access point + Switch"
    if is_sw:
        return "Switch"
    if is_ap:
        return "Access point"
    return None


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
def get(session: requests.Session, url: str) -> requests.Response:
    """GET with polite retries on 429/5xx (honours Retry-After)."""
    for attempt in range(4):
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
            r.raise_for_status()
            return r
        wait = r.headers.get("Retry-After", "")
        time.sleep(min(int(wait), 300) if wait.isdigit() else 30 * (attempt + 1))
    raise AssertionError("unreachable")


# HardverApró's keyword search is not served to automated clients, so we read the
# newest ads of the network-product categories (robots.txt allows these; it only
# disallows paging with offset=) and filter them for UniFi locally.
HVA_CATEGORIES = [
    "https://hardverapro.hu/aprok/hardver/halozati_termekek/index.html",
    "https://hardverapro.hu/aprok/hardver/halozati_termekek/router_switch_repeater/index.html",
    "https://hardverapro.hu/aprok/hardver/halozati_termekek/szerver_halozati_eszkozok/index.html",
]


def fetch_hardverapro(session: requests.Session) -> list[dict]:
    items = []
    for url in HVA_CATEGORIES:
        r = get(session, url)
        if "data-uadid" not in r.text:
            raise RuntimeError(f"no listings found on {url} (page layout changed or access blocked)")
        soup = BeautifulSoup(r.text, "html.parser")
        for li in soup.select("li.media[data-uadid]"):
            a = li.select_one(".uad-col-title h1 a")
            if not a:
                continue
            price = li.select_one(".uad-col-price .uad-price")
            city = li.select_one(".uad-cities")
            when = li.select_one(".uad-col-info .uad-time time")
            price_txt = price.get_text(" ", strip=True) if price else ""
            items.append({
                "id": f"hva-{li['data-uadid']}",
                "source": "HardverApró",
                "title": a.get_text(" ", strip=True),
                "url": urljoin(url, a["href"]),
                "price": price_txt.replace(" jegelve", ""),
                "on_hold": "jegelve" in price_txt,
                "location": city.get_text(" ", strip=True) if city else "",
                "posted": when.get_text(" ", strip=True) if when else "",
            })
        time.sleep(2)
    return items


def fetch_jofogas(session: requests.Session) -> list[dict]:
    items = []
    for query in JOFOGAS_QUERIES:
        items += _jofogas_query(session, query)
    return items


def _jofogas_query(session: requests.Session, query: str) -> list[dict]:
    items = []
    page = 1
    while page <= 5:
        url = f"https://www.jofogas.hu/magyarorszag?q={quote_plus(query)}"
        if page > 1:
            url += f"&o={page}"
        r = get(session, url)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise RuntimeError("jofogas: page data (__NEXT_DATA__) not found")
        ad_list = json.loads(m.group(1))["props"]["pageProps"].get("adList") or {}
        for ad in ad_list.get("ads", []):
            if (ad.get("type") or {}).get("value") not in (None, "s"):
                continue  # skip "wanted to buy" ads
            ts = (ad.get("list_time") or {}).get("value")
            posted = (
                datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M")
                if ts else (ad.get("list_time") or {}).get("label", "")
            )
            items.append({
                "id": f"jof-{ad['list_id']}",
                "source": "Jófogás",
                "title": ad.get("subject", "").strip(),
                "url": ad.get("url", ""),
                "price": (ad.get("price") or {}).get("label", ""),
                "on_hold": False,
                "location": (ad.get("region") or {}).get("label", ""),
                "posted": posted,
            })
        pager = ad_list.get("pager") or {}
        if page >= int(pager.get("last_page") or 1):
            break
        page += 1
        time.sleep(2)
    return items


SOURCES = [("HardverApró", fetch_hardverapro), ("Jófogás", fetch_jofogas)]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def write_markdown(listings: list[dict], updated: str, errors: list[str]) -> None:
    lines = [
        "# UniFi access points & switches",
        "",
        f"Last update: **{updated}** (Budapest time) · {len(listings)} listings · 🆕 = new in the last run",
        "",
    ]
    for e in errors:
        lines.append(f"> ⚠️ {e}")
    if errors:
        lines.append("")
    for cat in ("Access point", "Switch", "Access point + Switch"):
        group = [x for x in listings if x["category"] == cat]
        if not group:
            continue
        lines += [f"## {cat} ({len(group)})", "", "| | Item | Price | Location | Site | Posted |", "|---|---|---|---|---|---|"]
        for x in group:
            title = x["title"].replace("|", "/")
            hold = " (on hold)" if x.get("on_hold") else ""
            lines.append(
                f"| {'🆕' if x.get('is_new') else ''} | [{title}]({x['url']}) | {x['price']}{hold} "
                f"| {x['location']} | {x['source']} | {x['posted']} |"
            )
        lines.append("")
    MARKDOWN.write_text("\n".join(lines), encoding="utf-8")


def write_html(listings: list[dict], updated: str, errors: list[str]) -> None:
    e = html.escape
    rows = []
    for x in listings:
        rows.append(
            f'<tr class="{"new" if x.get("is_new") else ""}" data-cat="{e(x["category"])}">'
            f'<td>{"<span class=badge>ÚJ</span>" if x.get("is_new") else ""}</td>'
            f'<td><a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x["title"])}</a>'
            f'<div class=meta>{e(x["category"])} · {e(x["source"])}</div></td>'
            f'<td class=price>{e(x["price"])}{" <small>(jegelve)</small>" if x.get("on_hold") else ""}</td>'
            f'<td>{e(x["location"])}</td><td>{e(x["posted"])}</td></tr>'
        )
    errs = "".join(f'<p class="err">⚠️ {e(m)}</p>' for m in errors)
    PAGE.write_text(f"""<!doctype html>
<html lang="hu"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UniFi Watch</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--accent:#0064e0;--new:#eaf3ff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#f2f2f7;--muted:#9a9aa0;--line:#2c2c2e;--accent:#5aa2ff;--new:#13243b}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}}
main{{max-width:1000px;margin:auto}} h1{{margin:0 0 4px}} .sub{{color:var(--muted);margin-bottom:12px}}
.filters button{{border:1px solid var(--line);background:none;color:var(--fg);padding:6px 12px;border-radius:999px;margin:0 4px 8px 0;cursor:pointer}}
.filters button.on{{background:var(--accent);border-color:var(--accent);color:#fff}}
table{{width:100%;border-collapse:collapse}} td{{padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}}
tr.new{{background:var(--new)}} a{{color:var(--accent);text-decoration:none;font-weight:500}}
.meta{{color:var(--muted);font-size:13px}} .price{{white-space:nowrap;font-weight:600}}
.badge{{background:var(--accent);color:#fff;border-radius:4px;padding:1px 5px;font-size:11px}}
.err{{color:#c0392b}}
@media (max-width:640px){{td:nth-child(4),td:nth-child(5){{display:none}}}}
</style></head><body><main>
<h1>UniFi access pointok és switchek</h1>
<div class="sub">Frissítve: {e(updated)} · {len(listings)} hirdetés · HardverApró + Jófogás</div>
{errs}
<div class="filters">
<button class="on" data-f="">Mind</button><button data-f="Access point">Access point</button><button data-f="Switch">Switch</button><button data-f="new">Csak új</button>
</div>
<table><tbody>{''.join(rows)}</tbody></table>
</main>
<script>
document.querySelectorAll('.filters button').forEach(b=>b.onclick=()=>{{
 document.querySelectorAll('.filters button').forEach(x=>x.classList.toggle('on',x===b));
 const f=b.dataset.f;
 document.querySelectorAll('tbody tr').forEach(r=>{{
  r.hidden = f==='new' ? !r.classList.contains('new') : (f && !r.dataset.cat.startsWith(f) && !(f==='Switch'&&r.dataset.cat.endsWith('Switch')));
 }});
}});
</script></body></html>
""", encoding="utf-8")


# --------------------------------------------------------------------------
# Push notification (ntfy.sh)
# --------------------------------------------------------------------------
def notify(new_items: list[dict], title: str | None = None) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not new_items:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    page_url = os.environ.get("PAGE_URL", "").strip()
    lines = [f"• {x['title']} – {x['price']} ({x['source']})" for x in new_items[:15]]
    if len(new_items) > 15:
        lines.append(f"…és még {len(new_items) - 15}")
    actions = [{"action": "view", "label": f"{x['source']}: {x['title'][:28]}", "url": x["url"]}
               for x in new_items[:2]]
    if page_url:
        actions = actions[:2] + [{"action": "view", "label": "Teljes lista", "url": page_url}]
    payload = {
        "topic": topic,
        "title": title or f"{len(new_items)} új UniFi hirdetés",
        "message": "\n".join(lines),
        "tags": ["satellite_antenna"],
        "click": new_items[0]["url"] if len(new_items) == 1 or not page_url else page_url,
        "actions": actions,
    }
    headers = {}
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.post(server, json=payload, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    print(f"Push sent ({len(new_items)} new)")


# --------------------------------------------------------------------------
def main() -> int:
    previous = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {"listings": []}
    prev_by_id = {x["id"]: x for x in previous.get("listings", [])}
    first_run = not prev_by_id

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept-Language"] = "hu-HU,hu;q=0.9,en;q=0.8"

    now_dt = datetime.now(TZ)
    now = now_dt.strftime("%Y-%m-%d %H:%M")
    found: dict[str, dict] = {}
    errors: list[str] = []
    ok_sources: set[str] = set()
    for name, fetch in SOURCES:
        try:
            for item in fetch(session):
                cat = classify(item["title"])
                if cat:
                    item["category"] = cat
                    item["last_seen"] = now
                    found[item["id"]] = item
            ok_sources.add(name)
        except Exception as exc:  # keep going with the other site
            errors.append(f"{name}: {exc}")
            print(f"ERROR {name}: {exc}", file=sys.stderr)

    # Keep previous listings when their site failed this run, and keep HardverApró ads
    # for a while after they leave the newest-ads window.
    for pid, p in prev_by_id.items():
        if pid in found:
            continue
        last_seen = datetime.strptime(p.get("last_seen", p["first_seen"]), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        recent = (now_dt - last_seen).days < HVA_KEEP_DAYS
        if p["source"] not in ok_sources or (p["source"] == "HardverApró" and recent):
            found[pid] = dict(p)

    new_items = []
    for item in found.values():
        old = prev_by_id.get(item["id"])
        item["first_seen"] = old["first_seen"] if old else now
        item["is_new"] = old is None and not first_run
        if item["is_new"]:
            new_items.append(item)

    # newest first: by first-seen time, then by posting order on the site
    listings = sorted(found.values(), key=lambda x: x["first_seen"], reverse=True)

    DATA.parent.mkdir(parents=True, exist_ok=True)
    PAGE.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps({"updated": now, "listings": listings}, ensure_ascii=False, indent=1), encoding="utf-8")
    write_markdown(listings, now, errors)
    write_html(listings, now, errors)

    print(f"{len(listings)} listings, {len(new_items)} new, errors: {errors or 'none'}")
    for x in new_items:
        print(f"  NEW {x['source']}: {x['title']} {x['price']} {x['url']}")
    try:
        if first_run and listings:
            # First run: nothing is "new" yet; send one push so you can see notifications work.
            notify(listings, title=f"UniFi figyelő elindult – {len(listings)} hirdetés a listán")
        else:
            notify(new_items)
    except Exception as exc:
        print(f"ERROR push: {exc}", file=sys.stderr)
    return 1 if len(errors) == len(SOURCES) else 0


if __name__ == "__main__":
    sys.exit(main())
