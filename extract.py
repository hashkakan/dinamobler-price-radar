#!/usr/bin/env python3
"""Gemensam utvinning av pris/EAN ur en produktsida.

Delas av check_sources.py (granskar om en sajt duger) och radar.py (skrapar
skarpt). Logiken bor på ett ställe eftersom den kostade sex buggfixar att få
rätt – varje dubblett hade behövt samma sex fixar igen.

Data kan ligga på fyra ställen, och sajter väljer olika:
  1. JSON-LD, ofta nedgrävd i en @graph-array (Yoast/WooCommerce)
  2. microdata som HTML-attribut (Magento)
  3. og-/product-metataggar
  4. en inbäddad JS-datablob (Nordiska Rum bär både pris och EAN så)
"""
from __future__ import annotations

import json
import re

RE_LDJSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
RE_LOC = re.compile(r"<loc>([^<]+)</loc>", re.I)

ASSET_SUFFIX = re.compile(
    r"\.(js|mjs|css|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|eot"
    r"|xml|json|pdf|zip|mp4|webm)$", re.I)


def to_float(v):
    """Svenska priser skrivs "1 234,50" – normalisera innan tolkning."""
    try:
        if isinstance(v, str):
            v = v.replace("\xa0", "").replace(" ", "").replace(",", ".")
        return float(v)
    except (TypeError, ValueError):
        return None


def giltig_ean(kod) -> bool:
    """Äkta EAN-8/12/13/14 med korrekt kontrollsiffra.

    Prefix 20–29 förkastas: GS1 reserverar dem för butiksintern numrering.
    Svenska Hems 2900001947671 passerar checksumman men är inte global – två
    butiker kan ha samma siffror på helt olika varor, så den får aldrig matcha.

    Utan checksummekontrollen plockas dessutom slumpmässiga 13-siffriga tal upp
    som streckkoder; Trademax bäddar in CDN-hashar som 6909317344891 i sina
    bildfilnamn.
    """
    s = re.sub(r"\D", "", str(kod or ""))
    if len(s) not in (8, 12, 13, 14):
        return False
    if len(s) == 13 and s.startswith("2"):
        return False
    if len(set(s)) == 1:
        return False
    siffror = [int(c) for c in s]
    kontroll = siffror.pop()
    summa = sum(d * (3 if i % 2 == 0 else 1)
                for i, d in enumerate(reversed(siffror)))
    return (10 - summa % 10) % 10 == kontroll


_EAN_MONSTER = [
    r'itemprop="(?:gtin13|gtin14|gtin12|gtin8|gtin)"[^>]*content="([^"]+)"',
    r'"(?:gtin13|gtin14|gtin12|gtin8|gtin|ean|barcode)"\s*:\s*"?(\d{8,14})"?',
    r'\b(?:gtin|ean|barcode)"?\s*:\s*"(\d{8,14})"',
    r'data-(?:ean|gtin|barcode)="(\d{8,14})"',
    r'<meta[^>]+name="(?:ean|gtin)"[^>]+content="(\d{8,14})"',
    r'(?:EAN|GTIN|Streckkod)[\s:</a-zA-Z>-]{0,40}?(\d{12,14})\b',
]


def hitta_ean(html: str):
    """Letar EAN/GTIN överallt sajter brukar lägga det, med checksummekontroll."""
    for p in _EAN_MONSTER:
        for kod in re.findall(p, html, re.I):
            if giltig_ean(kod):
                return re.sub(r"\D", "", kod)
    return None


def _ar_produkt(obj) -> bool:
    t = obj.get("@type") if isinstance(obj, dict) else None
    typer = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.split("/")[-1] == "Product" for x in typer)


def _produkt_i_trad(nod):
    """Rekursiv sökning – produkten ligger ofta i en @graph-array, inte överst."""
    if isinstance(nod, list):
        for x in nod:
            if (träff := _produkt_i_trad(x)):
                return träff
        return None
    if not isinstance(nod, dict):
        return None

    if _ar_produkt(nod):
        erbjudanden = nod.get("offers")
        if isinstance(erbjudanden, dict):
            erbjudanden = [erbjudanden]
        for e in erbjudanden or []:
            if not isinstance(e, dict):
                continue
            pris = e.get("price")
            if pris is None and isinstance(e.get("priceSpecification"), dict):
                pris = e["priceSpecification"].get("price")
            if pris is None:
                pris = e.get("lowPrice")
            if pris is None:
                continue
            lager = str(e.get("availability") or "")
            return {
                "namn": str(nod.get("name") or "").strip(),
                "pris": to_float(pris),
                "valuta": e.get("priceCurrency") or "SEK",
                "ean": nod.get("gtin13") or nod.get("gtin") or nod.get("gtin14")
                       or nod.get("gtin12") or nod.get("gtin8"),
                "sku": nod.get("sku") or nod.get("mpn"),
                "lager": "IN_STOCK" if "InStock" in lager
                         else ("OUT_OF_STOCK" if "OutOfStock" in lager else "UNKNOWN"),
                "kalla": "json-ld",
            }

    for v in nod.values():
        if isinstance(v, (dict, list)):
            if (träff := _produkt_i_trad(v)):
                return träff
    return None


def _ur_microdata(html: str):
    """Schema.org som HTML-attribut, og-taggar eller JS-blob."""
    m = (re.search(r'itemprop="price"[^>]*content="([^"]+)"', html)
         or re.search(r'content="([^"]+)"[^>]*itemprop="price"', html)
         or re.search(r'property="product:price:amount"[^>]*content="([^"]+)"', html)
         or re.search(r'itemprop="price"[^>]*>\s*([\d\s.,]+)\s*<', html))
    if not m:
        return None
    pris = to_float(m.group(1))
    if pris is None:
        return None

    def attr(namn):
        t = re.search(rf'itemprop="{namn}"[^>]*content="([^"]+)"', html)
        return t.group(1) if t else None

    namn = attr("name") or ""
    if not namn:
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        namn = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""

    lager = "UNKNOWN"
    if re.search(r"(InStock|i lager)", html, re.I):
        lager = "IN_STOCK"
    if re.search(r"(OutOfStock|sluts[åa]ld|tillf[äa]lligt slut)", html, re.I):
        lager = "OUT_OF_STOCK"

    return {"namn": namn, "pris": pris, "valuta": attr("priceCurrency") or "SEK",
            "ean": None, "sku": attr("sku") or attr("mpn"),
            "lager": lager, "kalla": "microdata"}


def produkt_ur_sida(html: str):
    """Namn/pris/EAN/lager ur sidans strukturerade data, oavsett form."""
    träff = None
    for block in RE_LDJSON.findall(html):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        if (träff := _produkt_i_trad(data)):
            break
    if träff is None:
        träff = _ur_microdata(html)
    if träff is None:
        return None

    # JSON-LD saknar ofta gtin även när sidan bär EAN någon annanstans.
    if not träff.get("ean") or not giltig_ean(träff["ean"]):
        träff["ean"] = hitta_ean(html)
    elif träff["ean"]:
        träff["ean"] = re.sub(r"\D", "", str(träff["ean"]))
    return träff


def las_robots(text: str):
    """Returnerar (disallow-regler för *, sitemap-URL:er)."""
    disallow, sitemaps, aktuell = [], [], None
    rader = text.splitlines()
    for i, rad in enumerate(rader):
        s = rad.strip()
        low = s.lower()
        if low.startswith("user-agent:"):
            aktuell = s.split(":", 1)[1].strip()
        elif low.startswith("sitemap:"):
            varde = s.split(":", 1)[1].strip()
            # Nordiska Rum bryter direktivet över två rader.
            if not varde:
                for nasta in rader[i + 1:i + 3]:
                    n = nasta.strip()
                    if n.lower().startswith("http"):
                        varde = n
                        break
            if varde:
                sitemaps.append(varde)
        elif low.startswith("disallow:") and aktuell == "*":
            disallow.append(s.split(":", 1)[1].strip())
    return disallow, sitemaps


def tillaten(sokvag: str, disallow: list) -> bool:
    """Robots-matchning: prefix, med * som jokertecken."""
    for regel in disallow:
        if not regel:
            continue
        m = re.escape(regel).replace(r"\*", ".*")
        if re.match(m, sokvag):
            return False
    return True
