#!/usr/bin/env python3
"""Price Radar – konkurrentprisbevakning för Dina Möbler.

Flöde:
  1. Hämtar katalogen (produkter med EAN + COGS) från WordPress REST-intaget.
  2. Går igenom konkurrenternas EGNA sajter (sources.json): sitemap → de
     produktsidor som liknar vårt sortiment → pris/EAN ur strukturerad data.
  3. POST:ar observationerna till /observations, där prismotorn tar vid.

Varför inte PriceRunner längre (bytet gjordes 2026-08-28)
---------------------------------------------------------
Tre oberoende skäl, alla verifierade:
  * Deras robots.txt spärrar `/results` – sökvägen skrapan alltid använt.
  * De blockerar datacenter-IP: HTTP 202 + 2383 b på varje anrop från en
    GitHub-runner, medan samma anrop ger 200 + ~1 MB från en hem-IP.
  * De har slutat lägga priset i JSON-LD; produktsidans enda block är numera
    en BreadcrumbList.
Konkurrenternas egna sajter gör tvärtom: sitemaps åt crawlers, pris i
strukturerad form, ingen spärr mot molntrafik.

Körs EXTERNT (GitHub Actions) – aldrig från webbservern.

Miljövariabler:
  SITE_URL   – t.ex. https://dinamobler.se
  VS_TOKEN   – hemlig token (venture_price_token i WP)   [obligatorisk]

Exempel:
  VS_TOKEN=xxx python radar.py --limit 200 --dry-run
  VS_TOKEN=xxx python radar.py --sources royaldesign.se,rum21.se
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import competitors
import state

SITE_URL = os.environ.get("SITE_URL", "https://dinamobler.se").rstrip("/")
VS_TOKEN = os.environ.get("VS_TOKEN", "").strip()

API_PRODUCTS = f"{SITE_URL}/wp-json/venture-price/v1/products"
API_OBSERVATIONS = f"{SITE_URL}/wp-json/venture-price/v1/observations"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_catalog(session, only_ean: bool = False, limit=None) -> list:
    """Vårt eget sortiment. Token som header – URL:er hamnar i serverloggar."""
    headers = {"X-VS-Token": VS_TOKEN}
    offset, page_size, ut = 0, 100, []
    while True:
        params = {"limit": page_size, "offset": offset}
        if only_ean:
            params["only_ean"] = 1
        r = session.get(API_PRODUCTS, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            # HTTP 200 med icke-JSON betyder oftast en cachad HTML-sida eller
            # ett WP-fel som renderats som sida. Visa vad vi faktiskt fick –
            # ett rått JSONDecodeError säger ingenting om orsaken.
            log(f"FEL: svar utan JSON från katalogen (offset={offset}).")
            log(f"     HTTP {r.status_code}, {len(r.content)} b, "
                f"content-type={r.headers.get('content-type','?')}")
            log(f"     början: {r.text[:200]!r}")
            raise SystemExit(1)
        rader = data.get("products", [])
        if not rader:
            break
        ut.extend(rader)
        if limit and len(ut) >= limit:
            return ut[:limit]
        offset += page_size
        if offset >= int(data.get("total", 0)):
            break
    return ut


def post_observations(session, observations: list) -> int:
    if not observations:
        return 0
    # Interna fält (understreck) är till för loggen, inte för API:et.
    rena = [{k: v for k, v in o.items() if not k.startswith("_")}
            for o in observations]
    headers = {"X-VS-Token": VS_TOKEN, "Content-Type": "application/json"}
    r = session.post(API_OBSERVATIONS, headers=headers,
                     json={"observations": rena}, timeout=90)
    r.raise_for_status()
    try:
        return int(r.json().get("saved", len(rena)))
    except Exception:
        return len(rena)


def rensa_kollisioner(observationer: list, log) -> tuple[list, list]:
    """Tar bort matchningar som inte går att lita på. Returnerar (behållna, slängda).

    Två kollisioner uppstår i praktiken, och båda kostar pengar om de får passera:

    A) Flera av VÅRA produkter matchas mot SAMMA konkurrentsida. Selma Soffa
       finns hos oss i tre utföranden (9 171 / 10 620 / 13 401 kr) men matchades
       alla mot ett konkurrentpris på 7 190 kr. Sänks alla tre dit tappar vi
       6 355 kr på den dyraste utan att ha mött någon faktisk konkurrent.

    B) EN av våra produkter matchas mot FLERA sidor hos samma konkurrent.
       Redmond sänggavel gav sex priser mellan 18 990 och 36 990 kr – olika
       storlekar hos dem. Databasens uniknyckel är (produkt, källa, konkurrent),
       så raderna skrev över varandra och den SISTA vann, godtyckligt vald.

    Regeln är att hellre tappa en observation än att sätta fel pris. En sänkning
    mot fel variant syns inte i statistiken – den syns i marginalen.
    """
    SPRIDNING_TAK = 1.10   # >10 % mellan billigaste och dyrast = olika varor

    # A) En konkurrentsida får bara peka på en av våra produkter: den bäst matchade.
    #    EAN-träffar undantas – en produktsida bär ofta relaterade produkter med
    #    egna streckkoder, och olika EAN ÄR olika varor. Regeln finns för
    #    namnmatchningar, där samma sida annars fångar upp flera av våra varianter.
    per_url = {}
    for o in observationer:
        nyckel = ("ean", o["product_id"]) if o["matched_by"] == "ean" else o["source_url"]
        per_url.setdefault(nyckel, []).append(o)
    behall_a, slang = [], []
    for url, grupp in per_url.items():
        if len(grupp) == 1:
            behall_a.append(grupp[0])
            continue
        grupp.sort(key=lambda o: (o["matched_by"] == "ean", o.get("_sakerhet", 0)),
                   reverse=True)
        behall_a.append(grupp[0])
        for o in grupp[1:]:
            o["_skal"] = f"samma konkurrentsida matchade {len(grupp)} av våra produkter"
            slang.append(o)

    # B) Per (vår produkt, konkurrent): flera sidor med spridda priser = varianter.
    per_par = {}
    for o in behall_a:
        per_par.setdefault((o["product_id"], o["source"]), []).append(o)

    behallna = []
    for (_pid, _src), grupp in per_par.items():
        if len(grupp) == 1:
            behallna.append(grupp[0])
            continue
        priser = [o["price"] for o in grupp]
        spridning = max(priser) / max(0.01, min(priser))
        # EAN är exakt: samma streckkod = samma vara, då är billigaste rätt svar.
        if all(o["matched_by"] == "ean" for o in grupp) or spridning <= SPRIDNING_TAK:
            grupp.sort(key=lambda o: o["price"])
            behallna.append(grupp[0])
            for o in grupp[1:]:
                o["_skal"] = "dubblett, billigaste behölls"
                slang.append(o)
        else:
            for o in grupp:
                o["_skal"] = (f"{len(grupp)} sidor hos samma konkurrent, "
                              f"{min(priser):.0f}–{max(priser):.0f} kr "
                              f"(spridning {spridning:.2f}) – vilken variant?")
                slang.append(o)

    if slang:
        log(f"Kollisionsrensning: {len(slang)} osäkra matchningar slängda, "
            f"{len(behallna)} behållna")
    return behallna, slang


def fordela_budget(kallor: list, total: int, log) -> dict:
    """Sidbudget per källa, styrd av var träffarna faktiskt finns.

    Med lika budget åt alla gick 2 000 hämtningar och femtio minuter per natt
    till fem källor som gav noll träffar, medan de som gav 120 respektive 88
    fick exakt lika mycket. Utbytet skiljer sig med flera tiopotenser mellan
    källor, så en jämn fördelning är i praktiken att kasta bort halva natten.

    Varje källa får ett golv så att nya och tillfälligt tomma källor fortsätter
    provas – en källa som får noll i budget kan aldrig visa att den blivit bra.
    Resten fördelas efter träffkvot.
    """
    GOLV, TAK = 60, 900

    kvoter, obeprovade = {}, []
    for k in kallor:
        sidor, traffar = state.utbyte(k["doman"])
        if sidor < 50:
            obeprovade.append(k["doman"])
        else:
            kvoter[k["doman"]] = traffar / sidor

    budget = {k["doman"]: GOLV for k in kallor}
    kvar = total - GOLV * len(kallor)
    if kvar <= 0:
        return budget

    # Obeprövade källor får en normal andel tills de visat vad de går för.
    medel = (sum(kvoter.values()) / len(kvoter)) if kvoter else 0.02
    for d in obeprovade:
        kvoter[d] = max(medel, 0.01)

    summa = sum(kvoter.values()) or 1.0
    for doman, kvot in kvoter.items():
        budget[doman] = min(TAK, GOLV + int(kvar * kvot / summa))

    rader = sorted(budget.items(), key=lambda kv: kv[1], reverse=True)
    log("Sidbudget efter utbyte: " + ", ".join(f"{d.split('.')[0]} {n}" for d, n in rader[:8])
        + f" … (totalt {sum(budget.values())})")
    return budget


def las_kallor(vald: str | None) -> list:
    fil = Path(__file__).with_name("sources.json")
    kallor = json.loads(fil.read_text(encoding="utf-8"))["kallor"]
    kallor = [k for k in kallor if k.get("aktiv", True)]
    if vald:
        önskade = {d.strip().lower() for d in vald.split(",") if d.strip()}
        kallor = [k for k in kallor if k["doman"].lower() in önskade]
    return kallor


def main():
    ap = argparse.ArgumentParser(description="Price Radar – konkurrentpriser")
    ap.add_argument("--limit", type=int, default=None,
                    help="Max antal EGNA produkter att bevaka (för test)")
    ap.add_argument("--sources", default=None,
                    help="Bara dessa domäner, kommaseparerat")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="Fast sidbudget per konkurrent (0 = fördela efter utbyte)")
    ap.add_argument("--budget", type=int, default=6000,
                    help="Total sidbudget för hela körningen")
    ap.add_argument("--dry-run", action="store_true", help="Skriv inte till sajten")
    ap.add_argument("--refresh-catalog", action="store_true",
                    help="Tvinga omhämtning av katalogen (annars cache i 7 dygn)")
    ap.add_argument("--sleep", type=float, default=1.5,
                    help="Sekunder mellan anrop – vi är gäster på deras servrar")
    args = ap.parse_args()

    if not VS_TOKEN:
        log("FEL: VS_TOKEN saknas (sätt miljövariabeln).")
        sys.exit(2)

    session = requests.Session()
    log(f"Startar Price Radar mot {SITE_URL} (dry_run={args.dry_run})")

    # Katalogen läses ur cache när den är färsk nog. Att hämta om den varje
    # körning kostar ~129 anrop mot butiken innan skrapningen ens börjat, och
    # det var den lasten som fick Imunify360 att spärra runnerns IP 2026-08-29.
    # Sortimentet ändras inte över natten; en vecka gammal katalog duger.
    katalog, alder = state.las_katalog()
    if katalog and alder < state.KATALOG_FARSK_DYGN and not args.refresh_catalog:
        log(f"Katalog: {len(katalog)} produkter ur cache ({alder:.1f} dygn gammal)")
    else:
        skal = "tvingad uppdatering" if args.refresh_catalog else f"cache {alder:.0f} dygn gammal"
        log(f"Hämtar katalogen från sajten ({skal})…")
        try:
            katalog = fetch_catalog(session, only_ean=False, limit=args.limit)
            state.spara_katalog(katalog)
            log(f"Katalog: {len(katalog)} produkter (sparad till cache)")
        except SystemExit:
            if not katalog:
                raise
            log(f"Kunde inte hämta katalogen – kör vidare på cachen "
                f"({len(katalog)} produkter, {alder:.0f} dygn gammal)")

    if not katalog:
        log("Tom katalog – avbryter.")
        sys.exit(1)
    if args.limit:
        katalog = katalog[:args.limit]

    index = competitors.Katalogindex(katalog)
    saljare = sum(1 for p in katalog if int(p.get("total_sales") or 0) > 0)
    log(f"Varav {saljare} som nagon gang salt – de rankas hogst nar skrapan "
        f"valjer sidor (aldrig salda utesluts inte, bara senarelaggs)")
    med_ean = len(index.per_ean)
    log(f"Varav {med_ean} med giltig EAN ({med_ean * 100 // max(1, len(katalog))} %) "
        f"– de kan matchas exakt")

    kallor = las_kallor(args.sources)
    log(f"Källor: {len(kallor)} st")
    budgetar = ({k["doman"]: args.max_pages for k in kallor} if args.max_pages
                else fordela_budget(kallor, args.budget, log))
    log("")

    alla, per_kalla = [], {}
    for kalla in kallor:
        try:
            obs = competitors.skanna_kalla(session, kalla, index, log,
                                           budgetar.get(kalla["doman"], 60), args.sleep)
        except Exception as e:
            log(f"    FEL på {kalla['doman']}: {type(e).__name__}: {e}")
            continue
        # Leverantörer märks ut så de aldrig förväxlas med konkurrentpriser.
        if kalla.get("roll") == "leverantor":
            for o in obs:
                o["competitor"] = f"{kalla['namn']} (leverantör)"
        per_kalla[kalla["doman"]] = len(obs)
        alla.extend(obs)

    log("")
    alla, slangda = rensa_kollisioner(alla, log)
    if slangda:
        rapport = Path(__file__).with_name("osakra-matchningar.txt")
        rader = [f"{o['competitor']}\t{o['price']:.0f} kr\tprodukt {o['product_id']}"
                 f"\t{o.get('_skal','')}\t{o['source_url']}" for o in slangda]
        rapport.write_text("\n".join(rader), encoding="utf-8")
        log(f"   (skälen listade i {rapport.name})")
        for o in slangda[:5]:
            log(f"     slängd: produkt {o['product_id']} hos {o['competitor']} "
                f"– {o.get('_skal','')}")

    ean_traffar = sum(1 for o in alla if o["matched_by"] == "ean")
    per_kalla = {}
    for o in alla:
        per_kalla[o["source"]] = per_kalla.get(o["source"], 0) + 1
    log("")
    log("── Sammanfattning " + "─" * 42)
    for d, n in sorted(per_kalla.items(), key=lambda kv: kv[1], reverse=True):
        log(f"   {d:24} {n:>5} observationer")
    log(f"   {'TOTALT':24} {len(alla):>5}  "
        f"({ean_traffar} via EAN, {len(alla) - ean_traffar} via namn)")

    if args.dry_run:
        log("Dry-run – inget sparat.")
        return

    sparade = 0
    for i in range(0, len(alla), 200):
        sparade += post_observations(session, alla[i:i + 200])
        time.sleep(0.5)
    log(f"Sparade {sparade} observationer i databasen.")


if __name__ == "__main__":
    main()
