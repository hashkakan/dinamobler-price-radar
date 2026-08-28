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
RE_INITIAL_PAYLOAD = re.compile(r'<script[^>]*id="initial_payload"[^>]*>(.*?)</script>', re.S | re.I)


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
    # Token skickas som header, aldrig som query-parameter: URL:er hamnar i
    # proxy- och serverloggar, headers gör det inte.
    headers = {"X-VS-Token": VS_TOKEN}
    while True:
        params = {"limit": page_size, "offset": offset}
        if only_ean:
            params["only_ean"] = 1
        r = session.get(API_PRODUCTS, params=params, headers=headers, timeout=30)
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
    """Sök primärt på EAN, sekundärt på namn.

    Returnerar (url, matched_by) eller None. Namnträffar märks som "name":
    PriceRunner ger ingen EAN på produktsidan, så en namnsökning kan landa på
    fel vara. Den osäkerheten ska synas i datan, inte döljas.
    """
    for query, how in ((ean, "ean"), (name, "name")):
        if not query:
            continue
        try:
            r = session.get(PR_RESULTS, headers=PR_HEADERS, params={"q": query}, timeout=25)
            if r.status_code != 200:
                continue
            m = RE_PRODUCT_LINK.search(r.text)
            if m:
                return PR_BASE + m.group(0), how
        except Exception as e:
            log(f"  PR-sök fel ({query}): {e}")
    return None


def _offers_from_payload(html: str):
    """Läs erbjudandena ur den inbäddade app-staten.

    PriceRunner renderar sedan 2026 priserna klientsida. Produktsidans enda
    JSON-LD-block är numera en BreadcrumbList utan AggregateOffer, vilket är
    varför den gamla skrapan slutade hitta träffar. Priserna ligger i stället
    i <script id="initial_payload"> under queryn "product-detail-offers".
    """
    m = RE_INITIAL_PAYLOAD.search(html)
    if not m:
        return None
    try:
        payload = json.loads(m.group(1).strip())
    except Exception:
        return None

    # En sida kan innehålla FLERA "product-detail-offers"-queries, där den
    # första ibland är tom och en senare bär de riktiga erbjudandena. Samla
    # därför in från allihop innan vi väljer – att returnera på den första
    # missar annars produkter som faktiskt har ett pris.
    priced, merchants, count = [], {}, 0
    for q in (payload.get("__DEHYDRATED_QUERY_STATE__") or {}).get("queries") or []:
        key = q.get("queryKey")
        if not (isinstance(key, list) and key and key[0] == "product-detail-offers"):
            continue

        data = (q.get("state") or {}).get("data") or {}
        merchants.update(data.get("merchants") or {})
        count = max(count, int(((data.get("offersSummary") or {})
                                .get("nationalOffer") or {}).get("count") or 0))

        for o in data.get("offers") or []:
            amount = _to_float((o.get("price") or {}).get("amount"))
            if amount and not any(o.get("id") == prev.get("id") for _, prev in priced):
                priced.append((amount, o))

    if not priced:
        return None

    priced.sort(key=lambda t: t[0])
    # Billigaste priset en kund faktiskt kan handla till väger tyngst. Finns
    # inget i lager rapporterar vi ändå det lägsta, men med sann lagerstatus –
    # prismotorn sållar själv bort OUT_OF_STOCK innan den sätter priser.
    in_stock = [t for t in priced if t[1].get("stockStatus") == "IN_STOCK"]
    low, best = (in_stock or priced)[0]

    status = str(best.get("stockStatus") or "")
    return {
        "low": low,
        "high": priced[-1][0],
        "count": count or len(priced),
        "stock": status if status in ("IN_STOCK", "OUT_OF_STOCK") else "UNKNOWN",
        "merchant": str((merchants.get(str(best.get("merchantId"))) or {}).get("name") or ""),
    }


def _offers_from_ldjson(html: str):
    """Reserv: gamla JSON-LD-vägen, om PriceRunner skulle återinföra den."""
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
                "stock": "IN_STOCK" if "InStock" in str(offers.get("availability") or "") else "UNKNOWN",
                "merchant": "",
            }
    return None


def pr_lowest_price(session, url: str):
    """Billigaste konkurrentpriset på en PriceRunner-produktsida, eller None."""
    try:
        r = session.get(url, headers=PR_HEADERS, timeout=25)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception as e:
        log(f"  PR-produkt fel: {e}")
        return None

    info = _offers_from_payload(html) or _offers_from_ldjson(html)
    if info:
        info["url"] = url
    return info


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

    buffer, stats = [], {"produkter": 0, "med_traff": 0, "via_namn": 0,
                         "observationer": 0, "sparade": 0}

    for p in fetch_catalog(session, only_ean=True, limit=args.limit):
        stats["produkter"] += 1
        ean, name = str(p["ean"]), p.get("name", "")
        found = pr_find_product_url(session, ean, name)
        time.sleep(args.sleep + random.uniform(0, 0.6))   # artig mot PriceRunner
        if not found:
            continue
        url, matched_by = found
        info = pr_lowest_price(session, url)
        time.sleep(args.sleep + random.uniform(0, 0.6))
        if not info or not info["low"]:
            continue
        stats["med_traff"] += 1
        stock = info["stock"]
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
            "matched_by": matched_by,
            "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })
        stats["observationer"] += 1
        stats["via_namn"] += 1 if matched_by == "name" else 0
        butik = f" hos {info['merchant']}" if info.get("merchant") else ""
        flagga = "" if matched_by == "ean" else "  [OSÄKER: namnmatchad]"
        log(f"  {name[:38]:38} EAN {ean}: lägst {info['low']:.0f} kr "
            f"({info['count']} butiker{butik}){flagga}")

        if not args.dry_run and len(buffer) >= args.batch:
            stats["sparade"] += post_observations(session, buffer)
            buffer = []

    if not args.dry_run and buffer:
        stats["sparade"] += post_observations(session, buffer)

    log("KLART. " + " | ".join(f"{k}={v}" for k, v in stats.items()))


if __name__ == "__main__":
    main()
