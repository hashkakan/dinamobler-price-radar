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


def _to_float(v):
    """Svenska priser skrivs "1 234,50" – normalisera innan tolkning."""
    try:
        if isinstance(v, str):
            v = v.replace("\xa0", "").replace(" ", "").replace(",", ".")
        return float(v)
    except (TypeError, ValueError):
        return None


def hamta(session, url, timeout=25):
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        return r, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"


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
            # Vissa sajter (Nordiska Rum) bryter direktivet över två rader:
            # "Sitemap:" på en rad och URL:en på nästa. Ett tomt värde här är
            # inte "ingen sitemap" – titta på raden efter.
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


def giltig_ean(kod) -> bool:
    """Äkta EAN-8/12/13/14 med korrekt kontrollsiffra.

    Prefix 20–29 förkastas: GS1 reserverar dem för butiksintern numrering
    (Svenska Hem använder t.ex. 2900001947671). Två butiker kan ha samma
    sådana siffror på helt olika varor, så de får aldrig matcha produkter.
    """
    s = re.sub(r"\D", "", str(kod or ""))
    if len(s) not in (8, 12, 13, 14):
        return False
    if s.startswith(("2", "02")) and len(s) == 13:
        return False
    if len(set(s)) == 1:
        return False
    siffror = [int(c) for c in s]
    kontroll = siffror.pop()
    summa = 0
    for i, d in enumerate(reversed(siffror)):
        summa += d * (3 if i % 2 == 0 else 1)
    return (10 - summa % 10) % 10 == kontroll


def hitta_ean(html: str):
    """Letar EAN/GTIN överallt sajter brukar lägga det – inte bara i JSON-LD."""
    monster = [
        r'itemprop="(?:gtin13|gtin14|gtin12|gtin8|gtin)"[^>]*content="([^"]+)"',
        r'"(?:gtin13|gtin14|gtin12|gtin8|gtin|ean|EAN|barcode)"\s*:\s*"?(\d{8,14})"?',
        r'\b(?:gtin|ean|barcode)"?\s*:\s*"(\d{8,14})"',
        r'data-(?:ean|gtin|barcode)="(\d{8,14})"',
        r'<meta[^>]+name="(?:ean|gtin)"[^>]+content="(\d{8,14})"',
        r'(?:EAN|GTIN|Streckkod|Artikelnummer)[\s:</a-zA-Z>-]{0,40}?(\d{12,14})\b',
    ]
    for p in monster:
        for kod in re.findall(p, html, re.I):
            if giltig_ean(kod):
                return re.sub(r"\D", "", kod)
    return None


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
                    "_html": None,
                    "sku": nod.get("sku") or nod.get("mpn"),
                }

    for v in nod.values():
        if isinstance(v, (dict, list)):
            träff = _produkt_i_trad(v)
            if träff:
                return träff
    return None


def _ur_microdata(html: str):
    """Schema.org som HTML-attribut i stället för JSON-LD (vanligt i Magento)."""
    m = (re.search(r'itemprop="price"[^>]*content="([^"]+)"', html)
         or re.search(r'content="([^"]+)"[^>]*itemprop="price"', html)
         or re.search(r'property="product:price:amount"[^>]*content="([^"]+)"', html)
         or re.search(r'itemprop="price"[^>]*>\s*([\d\s.,]+)\s*<', html))
    if not m:
        return None
    pris = _to_float(m.group(1))
    if pris is None:
        return None

    def attr(namn):
        t = re.search(rf'itemprop="{namn}"[^>]*content="([^"]+)"', html)
        return t.group(1) if t else None

    valuta = attr("priceCurrency") or ""
    ean = hitta_ean(html)
    namn = attr("name") or ""
    if not namn:
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        namn = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""
    return {"namn": namn[:50], "pris": pris, "valuta": valuta,
            "ean": ean, "sku": attr("sku") or attr("mpn"), "kalla": "microdata"}


def produkt_ur_jsonld(html: str):
    """Namn/pris/EAN ur sidans strukturerade data, oavsett vilken form den har.

    Att bara läsa JSON-LD ger falska negativ: Nordiska Rum har noll JSON-LD-block
    men bär både pris och äkta EAN i microdata respektive en inbäddad datablob.
    """
    for block in RE_LDJSON.findall(html):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        träff = _produkt_i_trad(data)
        if träff:
            träff.pop("_html", None)
            träff.setdefault("kalla", "json-ld")
            # JSON-LD saknar ofta gtin även när sidan bär EAN någon annanstans.
            if not träff.get("ean") or not giltig_ean(träff["ean"]):
                träff["ean"] = hitta_ean(html)
            return träff
    return _ur_microdata(html)


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
