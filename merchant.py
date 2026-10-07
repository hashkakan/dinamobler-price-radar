#!/usr/bin/env python3
"""Price Radar – prisintag från Google Merchant Center.

Varför Merchant Center i stället för att skrapa
-----------------------------------------------
Skrapan (radar.py) går igenom konkurrenternas egna sajter, sida för sida, och
får ihop ett par hundra matchningar per natt. Merchant Center har redan
jämfört 6 340 av våra artiklar mot vad resten av marknaden tar för samma vara
– Google matchar på GTIN och titel över hela Shopping-indexet, inte mot en
handplockad lista konkurrenter. Det är samma fråga, besvarad av den som har
hela underlaget.

Google kallar siffran *benchmark price*: den klickviktade snittprisnivån för
samma produkt hos alla annonsörer. Den läggs in i price radar som en vanlig
observation med källa `google` och "konkurrent" `Google marknadspris`, så
HELA den befintliga kedjan fungerar oförändrad:

    /wp-json/venture-price/v1/products       – katalogen (pris, COGS, EAN)
    /wp-json/venture-price/v1/observations   – intaget (upsert per produkt+källa)
    wp-admin → Price Radar                   – diff mot marknaden, godkänn-flöde

Inget nytt API, ingen ny tabell. Unika nyckeln (product_id, source, competitor)
gör att raden skrivs över vid varje körning och att prev_price/price_changed_at
fylls i automatiskt när marknaden rör sig.

Två vägar in med data
---------------------
1. `--api`  Merchant API (reports.search) med ett tjänstekonto som lagts till
            som användare i Merchant Center. Det är den nattliga vägen; kräver
            GOOGLE_APPLICATION_CREDENTIALS + MERCHANT_ID.
2. `--rows fil.txt`  rader `titel|vårt pris|benchmark` (en per rad), eller en
            CSV exporterad ur Merchant Centers prisrapport. Används för en
            körning innan tjänstekontot finns på plats.

Spärrar (viktigare här än för skrapan)
--------------------------------------
Googles benchmark matchar ibland FEL vara – en matgrupp jämförs mot en ensam
stol och ser då ut att vara 170 % för dyr. En sådan rad får aldrig bli ett
prisförslag. Därför filtreras rader bort när
  * benchmarken ligger under vårt inköpspris (COGS) + marginalkrav,
  * gapet är orimligt stort (--max-gap, standard 60 %),
  * priset i Merchant Center skiljer sig påtagligt från priset i katalogen
    (då är flödet gammalt och jämförelsen gäller ett pris vi inte har).

Exempel:
    VS_TOKEN=xxx python merchant.py --rows mc-rows.txt --dry-run
    VS_TOKEN=xxx python merchant.py --api            # nattlig körning
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

SITE_URL = os.environ.get("SITE_URL", "https://dinamobler.se").rstrip("/")
VS_TOKEN = os.environ.get("VS_TOKEN", "").strip()

KALLA = "google"
KONKURRENT = "Google marknadspris"

API_PRODUCTS = f"{SITE_URL}/wp-json/venture-price/v1/products"
API_OBSERVATIONS = f"{SITE_URL}/wp-json/venture-price/v1/observations"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- normalisering
_SKRAP = re.compile(r"[^\w\s]", re.UNICODE)
_LUFT = re.compile(r"\s+")


def nyckel(titel: str) -> str:
    """Titel → jämförbar nyckel.

    Flödet till Google och WooCommerce-titeln är samma sträng i grunden, men
    skiljetecken och dubbla mellanslag följer inte alltid med. Siffror och
    bokstäver behålls (måtten skiljer varianterna åt: 180x90 är inte 120x80),
    allt annat faller bort.
    """
    t = unicodedata.normalize("NFKC", titel or "").casefold()
    t = t.replace("×", "x").replace("ø", "o")
    t = _SKRAP.sub(" ", t)
    return _LUFT.sub(" ", t).strip()


# ---------------------------------------------------------------- katalog
def hamta_katalog(session: requests.Session) -> list[dict]:
    """Hela sortimentet från WordPress. Token som header, inte i URL:en."""
    headers = {"X-VS-Token": VS_TOKEN}
    offset, sida, ut = 0, 500, []
    while True:
        r = session.get(API_PRODUCTS, params={"limit": sida, "offset": offset},
                        headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        rader = data.get("products", [])
        ut.extend(rader)
        total = int(data.get("total", 0))
        offset += len(rader)
        if not rader or offset >= total:
            break
    return ut


def bygg_index(katalog: list[dict]) -> dict[str, list]:
    """Nyckel → alla produkter med den titeln.

    1 123 titlar bärs av mer än en produkt (samma vara inlagd via två
    leverantörsflöden). Att kasta dem vore att tappa var tionde match, så
    listan behålls och valet görs vid matchningen: den kandidat vars pris
    ligger närmast priset Google rapporterar är den annonsen gällde.
    """
    index: dict[str, list] = {}
    for p in katalog:
        k = nyckel(p.get("name", ""))
        if k:
            index.setdefault(k, []).append(p)
    flera = sum(1 for v in index.values() if len(v) > 1)
    if flera:
        log(f"  {flera} titlar finns på flera produkter – väljer på pris")
    return index


def valj_kandidat(kandidater: list, google_pris) -> dict | None:
    """Rätt produkt bland flera med samma titel: den vars pris ligger närmast
    Googles. Utan pris från Google går det inte att avgöra – då hoppas raden
    över hellre än att priset sätts på fel vara."""
    if len(kandidater) == 1:
        return kandidater[0]
    if not google_pris:
        return None
    med_pris = [k for k in kandidater if k.get("price")]
    if not med_pris:
        return None
    return min(med_pris, key=lambda k: abs(float(k["price"]) - google_pris))


# ---------------------------------------------------------------- källor
def las_rader(sokvag: Path) -> list[dict]:
    """`titel|pris|benchmark` per rad, eller CSV ur Merchant Centers export."""
    text = sokvag.read_text(encoding="utf-8")
    ut: list[dict] = []
    if sokvag.suffix.lower() == ".csv":
        for rad in csv.DictReader(text.splitlines()):
            titel = _kolumn(rad, ("title", "titel", "produkt", "product"))
            pris = _tal(_kolumn(rad, ("your price", "ditt pris", "price", "pris")))
            bench = _tal(_kolumn(rad, ("benchmark", "benchmark price", "riktpris")))
            if titel and pris and bench:
                ut.append({"titel": titel, "pris": pris, "benchmark": bench,
                           "gtin": _kolumn(rad, ("gtin", "ean")) or "",
                           "offer_id": _kolumn(rad, ("offer id", "offer_id", "artikel-id", "id")) or ""})
        return ut
    for rad in text.splitlines():
        rad = rad.strip()
        if not rad or rad.startswith("#"):
            continue
        delar = rad.split("|")
        if len(delar) < 3:
            continue
        pris, bench = _tal(delar[-2]), _tal(delar[-1])
        titel = "|".join(delar[:-2]).strip()
        if titel and pris and bench:
            ut.append({"titel": titel, "pris": pris, "benchmark": bench, "gtin": "", "offer_id": ""})
    return ut


def _kolumn(rad: dict, namn: tuple[str, ...]) -> str:
    for k, v in rad.items():
        if k and k.strip().lower() in namn:
            return (v or "").strip()
    return ""


def _tal(varde) -> float | None:
    if varde in (None, ""):
        return None
    ren = re.sub(r"[^0-9,.\-]", "", str(varde))
    if ren in ("", "-"):
        return None
    if "," in ren and "." not in ren:
        ren = ren.replace(",", ".")
    else:
        ren = ren.replace(",", "")
    try:
        return round(float(ren), 2)
    except ValueError:
        return None


def hamta_api(merchant_id: str) -> list[dict]:
    """Merchant API: price_competitiveness_product_view för hela kontot.

    Kräver ett tjänstekonto som är tillagt som användare i Merchant Center
    (Inställningar → Personer och åtkomst) och GOOGLE_APPLICATION_CREDENTIALS
    som pekar på nyckelfilen. Sidstorleken är Googles, inte vår – rapporten
    paginerar med nextPageToken tills den tar slut.
    """
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        sys.exit("Saknar google-auth: pip install google-auth google-auth-httplib2")

    cred, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/content"])
    sess = AuthorizedSession(cred)
    url = (f"https://merchantapi.googleapis.com/reports/v1/"
           f"accounts/{merchant_id}/reports:search")
    query = (
        "SELECT id, offer_id, title, brand, price, benchmark_price, "
        "report_country_code "
        "FROM price_competitiveness_product_view "
        "WHERE report_country_code = 'SE'"
    )
    ut, token = [], None
    while True:
        body = {"query": query, "pageSize": 1000}
        if token:
            body["pageToken"] = token
        r = sess.post(url, json=body, timeout=120)
        if r.status_code != 200:
            sys.exit(f"Merchant API svarade {r.status_code}: {r.text[:300]}")
        data = r.json()
        for rad in data.get("results", []):
            v = rad.get("priceCompetitivenessProductView", {})
            pris = _micros(v.get("price", {}))
            bench = _micros(v.get("benchmarkPrice", {}))
            if pris and bench:
                ut.append({"titel": v.get("title", ""), "pris": pris, "benchmark": bench,
                           "gtin": "", "offer_id": v.get("offerId", "")})
        token = data.get("nextPageToken")
        if not token:
            break
    return ut


def _micros(pris: dict) -> float | None:
    mikro = pris.get("amountMicros") or pris.get("amount_micros")
    return round(int(mikro) / 1_000_000, 2) if mikro else None


# ---------------------------------------------------------------- matchning
def matcha(rader: list[dict], index: dict[str, list], args) -> tuple[list[dict], dict]:
    obs, stat = [], {
        "rader": len(rader), "matchade": 0, "omatchade": 0,
        "under_cogs": 0, "orimligt_gap": 0, "gammalt_pris": 0, "for_litet_gap": 0,
    }
    omatchade = []

    for rad in rader:
        kandidater = index.get(nyckel(rad["titel"]))
        p = valj_kandidat(kandidater, rad.get("pris")) if kandidater else None
        if not p:
            stat["omatchade"] += 1
            omatchade.append(rad["titel"])
            continue

        vart_pris = p.get("price") or 0
        bench = rad["benchmark"]

        # Flödet kan ligga efter. Jämför Googles uppfattning om vårt pris med
        # katalogens – skiljer de sig mycket gäller benchmarken ett annat pris
        # än det vi faktiskt tar, och diffen blir meningslös.
        if vart_pris and rad["pris"] and abs(rad["pris"] - vart_pris) / vart_pris > 0.25:
            stat["gammalt_pris"] += 1
            continue

        if not vart_pris or bench <= 0:
            stat["omatchade"] += 1
            continue

        gap = (vart_pris - bench) / bench
        if gap < args.min_gap:
            stat["for_litet_gap"] += 1
            continue
        if gap > args.max_gap:
            # Nästan alltid en felmatchning hos Google (matgrupp mot ensam stol).
            stat["orimligt_gap"] += 1
            continue

        kostnad = p.get("cost")
        if kostnad and bench < kostnad * (1 + args.min_marginal):
            # Marknadspriset ligger under vad varan kostar oss – jämförelsen
            # gäller en annan vara, eller så är varan inte värd att priskriga om.
            stat["under_cogs"] += 1
            continue

        stat["matchade"] += 1
        obs.append({
            "product_id": p["id"],
            "sku": p.get("sku", ""),
            "ean": p.get("ean", ""),
            "source": KALLA,
            "source_product": rad.get("offer_id", ""),
            "competitor": KONKURRENT,
            "price": bench,
            "currency": "SEK",
            "stock_status": "IN_STOCK",
            "matched_by": "gtin" if rad.get("gtin") else "name",
            "_prisavstand": abs((rad.get("pris") or 0) - vart_pris),
        })

    # Flera Merchant Center-rader kan peka på SAMMA produkt: Google har en rad
    # per variant medan vi har en produkt. Unika nyckeln i watch-tabellen är
    # (product_id, source, competitor), så utan den här hopslagningen vore det
    # sista varianten i listan som råkade vinna – 3 363 rader blev 1 981 utan
    # att något sa ifrån. Behåll i stället den rad vars pris hos Google ligger
    # närmast katalogens: det är den varianten jämförelsen faktiskt gällde.
    per_produkt: dict[int, dict] = {}
    for o in obs:
        pid = o["product_id"]
        tidigare = per_produkt.get(pid)
        if tidigare is None or o["_prisavstand"] < tidigare["_prisavstand"]:
            per_produkt[pid] = o
    stat["slogs_ihop"] = len(obs) - len(per_produkt)
    obs = list(per_produkt.values())
    for o in obs:
        o.pop("_prisavstand", None)

    stat["_omatchade"] = omatchade
    return obs, stat


def skicka(session: requests.Session, obs: list[dict], batch: int) -> int:
    headers = {"X-VS-Token": VS_TOKEN, "Content-Type": "application/json"}
    sparade = 0
    for i in range(0, len(obs), batch):
        del_ = obs[i:i + batch]
        r = session.post(API_OBSERVATIONS, headers=headers,
                         data=json.dumps({"observations": del_}), timeout=120)
        if r.status_code != 200:
            log(f"  FEL {r.status_code}: {r.text[:200]}")
            continue
        svar = r.json()
        sparade += int(svar.get("saved", 0))
        log(f"  {i + len(del_)}/{len(obs)} – sparade {svar.get('saved')}, "
            f"hoppade {svar.get('skipped')}")
    return sparade


def main() -> int:
    global SITE_URL, VS_TOKEN, API_PRODUCTS, API_OBSERVATIONS

    ap = argparse.ArgumentParser(description="Merchant Center → Price Radar")
    ap.add_argument("--rows", type=Path, help="fil med titel|pris|benchmark, eller CSV")
    ap.add_argument("--api", action="store_true", help="hämta via Merchant API")
    ap.add_argument("--merchant-id", default=os.environ.get("MERCHANT_ID", "117899063"))
    ap.add_argument("--base", default=SITE_URL)
    ap.add_argument("--token", default=VS_TOKEN)
    ap.add_argument("--min-gap", type=float, default=0.05, help="minsta övervärde (0.05 = 5 %%)")
    ap.add_argument("--max-gap", type=float, default=0.60, help="över detta: trolig felmatchning")
    ap.add_argument("--min-marginal", type=float, default=0.10, help="benchmark måste ligga så här över COGS")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--save-unmatched", type=Path)
    args = ap.parse_args()

    SITE_URL = args.base.rstrip("/")
    VS_TOKEN = args.token or VS_TOKEN
    API_PRODUCTS = f"{SITE_URL}/wp-json/venture-price/v1/products"
    API_OBSERVATIONS = f"{SITE_URL}/wp-json/venture-price/v1/observations"

    if not VS_TOKEN:
        sys.exit("VS_TOKEN saknas (sätt miljövariabeln eller --token).")
    if not args.rows and not args.api:
        sys.exit("Ange --rows FIL eller --api.")

    session = requests.Session()
    session.headers["User-Agent"] = "VenturePriceRadar/2.0 (Merchant Center)"

    log("Hämtar Merchant Center-data …")
    rader = hamta_api(args.merchant_id) if args.api else las_rader(args.rows)
    log(f"  {len(rader)} rader med benchmark")

    log("Hämtar katalogen …")
    katalog = hamta_katalog(session)
    index = bygg_index(katalog)
    med_cogs = sum(1 for p in katalog if p.get("cost"))
    log(f"  {len(katalog)} produkter, {len(index)} unika titlar, {med_cogs} med inköpspris")

    obs, stat = matcha(rader, index, args)
    log(f"Matchning: {stat['matchade']} klara, {stat['omatchade']} utan produkt, "
        f"{stat['for_litet_gap']} under {args.min_gap:.0%}, "
        f"{stat['orimligt_gap']} över {args.max_gap:.0%} (felmatchning), "
        f"{stat['under_cogs']} under inköpspris, {stat['gammalt_pris']} med gammalt flödespris, "
        f"{stat.get('slogs_ihop', 0)} varianter slogs ihop till samma produkt")

    if args.save_unmatched and stat["_omatchade"]:
        args.save_unmatched.write_text("\n".join(stat["_omatchade"]), encoding="utf-8")
        log(f"  omatchade titlar skrivna till {args.save_unmatched}")

    if not obs:
        log("Inget att skicka.")
        return 0

    if args.dry_run:
        log(f"DRY-RUN: skulle skicka {len(obs)} observationer. Exempel:")
        for o in obs[:5]:
            log(f"  #{o['product_id']} {o['price']} kr  ({o['competitor']})")
        return 0

    log(f"Skickar {len(obs)} observationer …")
    sparade = skicka(session, obs, args.batch)
    log(f"Klart – {sparade} rader i price radar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
