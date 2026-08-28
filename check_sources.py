#!/usr/bin/env python3
"""Granska om en konkurrentsajt duger som priskälla – innan vi bygger mot den.

Kör från GitHub Actions, eftersom det är därifrån skrapan ska köra. En sajt som
svarar fint från en hemdator kan blockera datacenter-IP:n; PriceRunner gör
precis det (HTTP 202 på varje anrop från en runner, HTTP 200 hemifrån).

För varje domän kontrolleras fyra saker, i tur och ordning:
  1. Går den att nå härifrån?
  2. Tillåter robots.txt att vi läser produktsidor?
  3. Finns en sitemap att hitta produkterna via?
  4. Bär produktsidorna pris i strukturerad form (Product-JSON-LD)?

Faller något av stegen är sajten inte en kandidat, och då ska vi inte bygga
mot den. Verktyget svarar på frågan – det hämtar ingen prisdata.

    python check_sources.py trademax.se,mio.se,em.se
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
}

PAUS = 1.5           # sekunder mellan anrop – vi är gäster på deras servrar
MAX_PRODUKTFORSOK = 6

RE_LDJSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
RE_LOC = re.compile(r'<loc>([^<]+)</loc>', re.I)


# Utvinning av pris/EAN och robots-tolkning delas med radar.py – logiken
# kostade sex buggfixar att få rätt och ska inte finnas i två kopior.
from extract import (  # noqa: E402
    to_float as _to_float,
    giltig_ean,
    hitta_ean,
    produkt_ur_sida as produkt_ur_jsonld,
    las_robots,
    tillaten,
    ASSET_SUFFIX,
)


def hamta(session, url, timeout=25):
    try:
        return session.get(url, headers=HEADERS, timeout=timeout), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"


def interna_lankar(html: str, bas_url: str) -> list:
    """Länkar på sidan som kan vara produktsidor."""
    bas = urllib.parse.urlparse(bas_url)
    # Produkten ligger ofta på SAMMA djup som kategorin – sista segmentet byts
    # bara ut. Kräv därför inte "djupare", bara "minst lika djupt".
    djup = bas.path.rstrip("/").count("/")
    ut, sedda = [], set()
    for href in re.findall(r'href="([^"#?]+)"', html):
        full = urllib.parse.urljoin(bas_url, href)
        d = urllib.parse.urlparse(full)
        if d.netloc != bas.netloc or full in sedda or d.path == bas.path:
            continue
        # Utan assetfiltret går försöken åt till JS-chunkar och bilder i
        # stället för produktsidor – vilket får sajten att se prislös ut.
        if ASSET_SUFFIX.search(d.path):
            continue
        if d.path.rstrip("/").count("/") < djup:
            continue
        sedda.add(full)
        ut.append(urllib.parse.unquote(full))
    ut.sort(key=lambda u: 0 if re.search(r"-p\d{4,}|/p/|\d{6,}", u) else 1)
    return ut


def granska(session, doman: str) -> dict:
    rå = doman.strip().lower().removeprefix("http://").removeprefix("https://").strip("/")
    # Listan kan innehålla sökväg (bolia.com/sv-se) – behåll bara värdnamnet.
    doman = rå.split("/")[0]
    if not doman:
        return {}
    res = {"doman": rå, "verdikt": "?", "not": ""}

    # 1. Nåbarhet. Alla sajter använder inte www, så prova båda innan vi
    # dömer ut den – annars blir "ingen www-post" felaktigt "EJ NÅBAR".
    varianter = ([f"https://{doman}"] if doman.startswith("www.")
                 else [f"https://www.{doman}", f"https://{doman}"])
    bas, r, fel = None, None, None
    for kandidat in varianter:
        r, fel = hamta(session, kandidat + "/")
        time.sleep(PAUS)
        if r is not None and r.status_code == 200 and len(r.content) >= 20000:
            bas = kandidat
            break
    if bas is None:
        if r is None:
            res.update(verdikt="EJ NÅBAR", **{"not": fel or "inget svar"})
        else:
            res.update(verdikt="BLOCKERAD", http=r.status_code, storlek=len(r.content),
                       **{"not": f"HTTP {r.status_code}, {len(r.content)} b – ser ut som bot-utmaning"})
        return res

    res["http"] = r.status_code
    res["storlek"] = len(r.content)
    res["bas"] = bas

    # 2. robots.txt
    rr, _ = hamta(session, bas + "/robots.txt")
    time.sleep(PAUS)
    disallow, sitemaps = las_robots(rr.text) if rr is not None and rr.status_code == 200 else ([], [])
    res["disallow_antal"] = len(disallow)
    res["sitemaps"] = len(sitemaps)

    # 3. Sitemap – från robots, annars gissa standardplatsen
    # Standardplatserna som reserv – ett trasigt sitemap-direktiv ska inte
    # ensamt döma ut en sajt som saknar sitemap.
    kandidater = [s for s in sitemaps if s.strip()]
    kandidater += [bas + "/sitemap.xml", bas + "/sitemap_index.xml",
                   bas + "/media/sitemap.xml"]
    url_lista = []
    for sm in kandidater[:4]:
        sr, _ = hamta(session, sm, timeout=40)
        time.sleep(PAUS)
        if sr is None or sr.status_code != 200:
            continue
        loc = [urllib.parse.unquote(x) for x in RE_LOC.findall(sr.text)]
        # Sitemapindex? Följ första underliggande sitemap.
        if loc and loc[0].endswith(".xml"):
            sr2, _ = hamta(session, loc[0], timeout=40)
            time.sleep(PAUS)
            if sr2 is not None and sr2.status_code == 200:
                loc = [urllib.parse.unquote(x) for x in RE_LOC.findall(sr2.text)]
        url_lista = [u for u in loc if not u.endswith(".xml")]
        if url_lista:
            break

    res["sitemap_urler"] = len(url_lista)
    if not url_lista:
        res.update(verdikt="INGEN SITEMAP",
                   **{"not": "hittade inga produkt-URL:er att utgå från"})
        return res

    # 4. Produktsidor. Djupet avslöjar inte var produkterna bor: hos Trademax
    # ligger de djupt och kategorierna grunt, hos Magento-sajter tvärtom
    # (/produktnamn.html i roten). Prova därför både de djupaste och ett
    # spritt urval ur mitten – annars underkänns halva urvalet av sajter.
    djupast = sorted(url_lista, key=lambda u: u.rstrip("/").count("/"), reverse=True)[:3]
    mitten = url_lista[len(url_lista) // 2:][:200]
    spritt = mitten[:: max(1, len(mitten) // 5)][:5]
    kandidat_urler = list(dict.fromkeys(djupast + spritt))

    provade = 0
    for u in kandidat_urler[:MAX_PRODUKTFORSOK + 2]:
        sokvag = urllib.parse.urlparse(u).path
        if not tillaten(sokvag, disallow):
            continue
        pr, _ = hamta(session, u)
        time.sleep(PAUS)
        provade += 1
        if pr is None or pr.status_code != 200:
            continue
        träff = produkt_ur_jsonld(pr.text)
        if träff:
            res.update(verdikt="DUGER", exempel=träff, provad_url=u)
            return res

        # Sitemappen kan lista kategorier i stället för produkter (Trademax gör
        # det). Följ då en länk vidare in, precis som en besökare hade gjort.
        for lank in interna_lankar(pr.text, u)[:4]:
            if not tillaten(urllib.parse.urlparse(lank).path, disallow):
                continue
            lr, _ = hamta(session, lank)
            time.sleep(PAUS)
            provade += 1
            if lr is None or lr.status_code != 200:
                continue
            träff = produkt_ur_jsonld(lr.text)
            if träff:
                res.update(verdikt="DUGER", exempel=träff, provad_url=lank)
                return res

    res.update(verdikt="INGET PRIS HITTAT",
               **{"not": f"provade {provade} sidor utan pris i strukturerad form"})
    return res


def main():
    if len(sys.argv) < 2:
        print("Ange domäner: python check_sources.py trademax.se,mio.se")
        sys.exit(2)

    domaner = [d for d in re.split(r"[,\s]+", sys.argv[1]) if d]
    session = requests.Session()
    resultat = []

    for d in domaner:
        print(f"\n─── {d} " + "─" * max(0, 50 - len(d)))
        r = granska(session, d)
        resultat.append(r)
        print(f"  verdikt      : {r.get('verdikt')}")
        for nyckel in ("http", "storlek", "disallow_antal", "sitemaps", "sitemap_urler"):
            if nyckel in r:
                print(f"  {nyckel:13}: {r[nyckel]}")
        if r.get("exempel"):
            e = r["exempel"]
            print(f"  exempel      : {e['namn']!r} – {e['pris']} {e['valuta']}"
                  f"  EAN: {e['ean'] or 'saknas'}  SKU: {e['sku'] or '-'}")
        if r.get("not"):
            print(f"  not          : {r['not']}")

    print("\n" + "=" * 62)
    print("SAMMANFATTNING")
    print("=" * 62)
    for r in resultat:
        ean = ""
        if r.get("exempel"):
            ean = "  EAN finns" if r["exempel"].get("ean") else "  utan EAN (matcha på namn)"
        print(f"  {r.get('doman',''):28} {r.get('verdikt',''):22}{ean}")

    dugliga = [r for r in resultat if r.get("verdikt") == "DUGER"]
    print(f"\n{len(dugliga)} av {len(resultat)} duger som priskälla.")


if __name__ == "__main__":
    main()
