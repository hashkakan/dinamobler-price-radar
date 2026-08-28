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


def hamta(session, url, timeout=25):
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        return r, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"


def las_robots(text: str):
    """Returnerar (disallow-regler för *, sitemap-URL:er)."""
    disallow, sitemaps, aktuell = [], [], None
    for rad in text.splitlines():
        s = rad.strip()
        low = s.lower()
        if low.startswith("user-agent:"):
            aktuell = s.split(":", 1)[1].strip()
        elif low.startswith("sitemap:"):
            sitemaps.append(s.split(":", 1)[1].strip())
        elif low.startswith("disallow:") and aktuell == "*":
            disallow.append(s.split(":", 1)[1].strip())
    return disallow, sitemaps


def tillaten(sokvag: str, disallow: list) -> bool:
    """Enkel robots-matchning: prefix, med * som jokertecken."""
    for regel in disallow:
        if not regel:
            continue
        m = re.escape(regel).replace(r"\*", ".*")
        if re.match(m, sokvag):
            return False
    return True


def interna_lankar(html: str, bas_url: str) -> list:
    """Länkar på sidan som pekar djupare in på samma domän – produktkandidater."""
    bas = urllib.parse.urlparse(bas_url)
    # Produkten ligger ofta på SAMMA djup som kategorin – sista segmentet byts
    # bara ut. Kräv därför inte "djupare", bara "minst lika djupt och inte
    # sidan vi redan står på".
    djup = bas.path.rstrip("/").count("/")
    ut, sedda = [], set()
    for href in re.findall(r'href="([^"#?]+)"', html):
        full = urllib.parse.urljoin(bas_url, href)
        d = urllib.parse.urlparse(full)
        if d.netloc != bas.netloc or full in sedda or d.path == bas.path:
            continue
        # Utan det här filtret går försöken åt till JS-chunkar och bilder i
        # stället för produktsidor – vilket får sajten att se prislös ut.
        if re.search(r"\.(js|mjs|css|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|eot"
                     r"|xml|json|pdf|zip|mp4|webm)$", d.path, re.I):
            continue
        if d.path.rstrip("/").count("/") < djup:
            continue
        sedda.add(full)
        ut.append(urllib.parse.unquote(full))
    # Länkar som ser ut som produkter först (artikelnummer i sluget).
    ut.sort(key=lambda u: 0 if re.search(r"-p\d{4,}|/p/|\d{6,}", u) else 1)
    return ut


def _ar_produkt(obj) -> bool:
    """@type kan vara "Product", en lista, eller en URL-form av samma sak."""
    t = obj.get("@type") if isinstance(obj, dict) else None
    typer = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.split("/")[-1] == "Product" for x in typer)


def _produkt_i_trad(nod):
    """Letar rekursivt efter ett Product-objekt med pris.

    Yoast och de flesta WooCommerce-sajter lägger produkten i en `@graph`-array
    i stället för på toppnivån. En parser som bara tittar överst missar dem och
    rapporterar felaktigt att sajten saknar pris.
    """
    if isinstance(nod, list):
        for x in nod:
            träff = _produkt_i_trad(x)
            if träff:
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
            if pris is not None:
                return {
                    "namn": str(nod.get("name") or "")[:50],
                    "pris": pris,
                    "valuta": e.get("priceCurrency") or "",
                    "ean": nod.get("gtin13") or nod.get("gtin") or nod.get("gtin14")
                           or nod.get("gtin12") or nod.get("gtin8"),
                    "sku": nod.get("sku") or nod.get("mpn"),
                }

    for v in nod.values():
        if isinstance(v, (dict, list)):
            träff = _produkt_i_trad(v)
            if träff:
                return träff
    return None


def produkt_ur_jsonld(html: str):
    """Plockar ut namn/pris/EAN ur ett Product-block, var det än ligger."""
    for block in RE_LDJSON.findall(html):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        träff = _produkt_i_trad(data)
        if träff:
            return träff
    return None


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
    kandidater = list(sitemaps) or [bas + "/sitemap.xml"]
    url_lista = []
    for sm in kandidater[:2]:
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

    # 4. Produktsidor – djupast liggande URL:er först
    url_lista.sort(key=lambda u: u.rstrip("/").count("/"), reverse=True)
    provade = 0
    for u in url_lista[:MAX_PRODUKTFORSOK]:
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

    res.update(verdikt="INGET PRIS I JSON-LD",
               **{"not": f"provade {provade} sidor utan Product-JSON-LD"})
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
