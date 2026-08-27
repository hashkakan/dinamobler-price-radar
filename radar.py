#!/usr/bin/env python3
"""
Price Radar – konkurrentpris-skrapa för Dina Möbler.

Flöde:
  1. Hämtar katalogen (produkter med EAN + COGS) från WordPress REST-intaget.
  2. Slår upp varje EAN på PriceRunner och läser ut BILLIGASTE konkurrentpris
     (JSON-LD AggregateOffer.lowPrice på produktsidan – det priset vi ska slå).
  3. POST:ar observationerna tillbaka till /observations, där prismotorn tar vid.

Körs EXTERNT (GitHub Actions / valfri maskin) – aldrig från webbservern, eftersom
PriceRunner blockerar server-IP:n. Auth mot sajten sker med den hemliga VS-token.

Miljövariabler:
  SITE_URL   – t.ex. https://dinamobler.se   (default nedan)
  VS_TOKEN   – hemlig token (venture_price_token i WP)   [obligatorisk]

Exempel:
  VS_TOKEN=xxxx python radar.py --limit 25 --dry-run     # testkörning, skriver inget
  VS_TOKEN=xxxx python radar.py                          # full körning
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
SITE_URL = os.environ.get("SITE_URL", "https://dinamobler.se").rstrip("/")
VS_TOKEN = os.environ.get("VS_TOKEN", "").strip()

API_PRODUCTS = f"{SITE_URL}/wp-json/venture-price/v1/products"
API_OBSERVATIONS = f"{SITE_URL}/wp-json/venture-price/v1/observations"

PR_BASE = "https://www.pricerunner.se"
PR_RESULTS = PR_BASE + "/results"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PR_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Referer": PR_BASE + "/",
}

# Produktlänk på sökträffsidan: /pl/343-{ID}/Kategori/Namn-priser
RE_PRODUCT_LINK = re.compile(r'/pl/\d+-\d+/[^"\'\s<>]*?-priser')
RE_LDJSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _to_float(v):
    try:
        if isinstance(v, str):
            v = v.replace("\xa0", "").replace(" ", "").replace(",", ".")
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 1. Hämta katalogen från sajten
# --------------------------------------------------------------------------- #
def fetch_catalog(session, only_ean: bool = True, limit=None):
    """Generator: produkter {id, name, sku, ean, price, cost, ...} sida för sida."""
    offset, page_size, yielded = 0, 100, 0
    while True:
        params = {"token": VS_TOKEN, "limit": page_size, "offset": offset}
        if only_ean:
            params["only_ean"] = 1
        r = session.get(API_PRODUCTS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows = data.get("products", [])
        if not rows:
            break
        for p in rows:
            if not p.get("ean"):
                continue
            yield p
            yielded += 1
            if limit and yielded >= limit:
                return
        offset += page_size
        if offset >= int(data.get("total", 0)):
            break


# --------------------------------------------------------------------------- #
# 2. PriceRunner: hitta produktsidan + läs billigaste pris
# --------------------------------------------------------------------------- #
def pr_find_product_url(session, ean: str, name: str):
    """Sök primärt på EAN, sekundärt på namn. Returnerar /pl/-URL eller None."""
    for query in (ean, name):
        if not query:
            continue
        try:
            r = session.get(PR_RESULTS, headers=PR_HEADERS, params={"q": query}, timeout=25)
            if r.status_code != 200:
                continue
            m = RE_PRODUCT_LINK.search(r.text)
            if m:
                return PR_BASE + m.group(0)
        except Exception as e:
            log(f"  PR-sök fel ({query}): {e}")
    return None


def pr_lowest_price(session, url: str):
    """Läs JSON-LD AggregateOffer på produktsidan. Returnerar dict eller None."""
    try:
        r = session.get(url, headers=PR_HEADERS, timeout=25)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception as e:
        log(f"  PR-produkt fel: {e}")
        return None
    for block in RE_LDJSON.findall(html):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            offers = obj.get("offers") if isinstance(obj, dict) else None
            if not isinstance(offers, dict):
                continue
            low = _to_float(offers.get("lowPrice") or offers.get("price"))
            if not low:
                continue
            return {
                "low": low,
                "high": _to_float(offers.get("highPrice")),
                "count": int(offers.get("offerCount") or 1),
                "avail": str(offers.get("availability") or ""),
                "url": url,
            }
    return None


# --------------------------------------------------------------------------- #
# 3. POST observationer till sajten
# --------------------------------------------------------------------------- #
def post_observations(session, observations: list) -> int:
    if not observations:
        return 0
    headers = {"X-VS-Token": VS_TOKEN, "Content-Type": "application/json"}
    r = session.post(API_OBSERVATIONS, headers=headers,
                     json={"observations": observations}, timeout=60)
    r.raise_for_status()
    try:
        return int(r.json().get("saved", len(observations)))
    except Exception:
        return len(observations)


# --------------------------------------------------------------------------- #
# Huvudflöde
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Price Radar – PriceRunner-skrapa")
    ap.add_argument("--limit", type=int, default=None, help="Max antal produkter (för test)")
    ap.add_argument("--dry-run", action="store_true", help="Skriv inte till sajten")
    ap.add_argument("--sleep", type=float, default=1.5, help="Sekunder mellan PriceRunner-anrop")
    ap.add_argument("--batch", type=int, default=200, help="Observationer per POST")
    args = ap.parse_args()

    if not VS_TOKEN:
        log("FEL: VS_TOKEN saknas (sätt miljövariabeln).")
        sys.exit(2)

    session = requests.Session()
    log(f"Startar Price Radar mot {SITE_URL} (dry_run={args.dry_run})")

    buffer, stats = [], {"produkter": 0, "med_traff": 0, "observationer": 0, "sparade": 0}

    for p in fetch_catalog(session, only_ean=True, limit=args.limit):
        stats["produkter"] += 1
        ean, name = str(p["ean"]), p.get("name", "")
        url = pr_find_product_url(session, ean, name)
        time.sleep(args.sleep + random.uniform(0, 0.6))   # artig mot PriceRunner
        if not url:
            continue
        info = pr_lowest_price(session, url)
        time.sleep(args.sleep + random.uniform(0, 0.6))
        if not info or not info["low"]:
            continue
        stats["med_traff"] += 1
        stock = "IN_STOCK" if "InStock" in info["avail"] else "UNKNOWN"
        buffer.append({
            "product_id": int(p["id"]),
            "sku": p.get("sku", ""),
            "ean": ean,
            "source": "pricerunner",
            "source_product": info["url"].split("/pl/")[-1].split("/")[0],
            "source_url": info["url"][:1000],
            "competitor": "PriceRunner (lägst)",
            "price": info["low"],
            "currency": "SEK",
            "stock_status": stock,
            "matched_by": "ean",
            "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })
        stats["observationer"] += 1
        log(f"  {name[:38]:38} EAN {ean}: lägst {info['low']:.0f} kr ({info['count']} butiker)")

        if not args.dry_run and len(buffer) >= args.batch:
            stats["sparade"] += post_observations(session, buffer)
            buffer = []

    if not args.dry_run and buffer:
        stats["sparade"] += post_observations(session, buffer)

    log("KLART. " + " | ".join(f"{k}={v}" for k, v in stats.items()))


if __name__ == "__main__":
    main()
