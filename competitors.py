#!/usr/bin/env python3
"""Hämtar konkurrentpriser från butikernas egna sajter.

Varför inte jämförelsesajter: PriceRunner spärrar `/results` i robots.txt och
blockerar dessutom datacenter-IP (HTTP 202 på varje anrop från en runner).
Prisjakt spärrar `/search?*`. Båda tillåter produktsidor men förbjuder just
sökningen – och sökningen ÄR mekanismen som kopplar EAN till produkt.

Butikernas egna sajter gör tvärtom: de publicerar sitemaps åt crawlers och
lägger pris i strukturerad form. Det är den vägen den här modulen går.

Skalproblemet och lösningen
---------------------------
Konkurrenterna har tiotusentals produkter. Att hämta varenda produktsida vore
både ohyfsat och omöjligt inom en nattlig körning. Men sitemapen ger oss alla
URL:er billigt, och slugen bär produktnamnet. Vi matchar därför VÅRA namn mot
DERAS slugar först, och hämtar bara sidor som rimligen kan vara en träff.
Antalet hämtningar blir då proportionellt mot vårt sortiment, inte deras.
"""
from __future__ import annotations

import difflib
import re
import time
import unicodedata
import urllib.parse

import extract
import state

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
}

# Ord som inte skiljer produkter åt och därför inte får driva en matchning.
STOPPORD = {
    "och", "med", "till", "for", "med", "den", "det", "en", "ett", "av", "pa",
    "cm", "mm", "st", "pack", "set", "kpl", "inkl", "utan", "mobler", "mobel",
    "home", "design", "kop", "online", "se", "com", "produkt", "produkter",
    "www", "html", "rea", "nyhet", "ny",
}

NAMN_TROSKEL = 0.72     # difflib-likhet som krävs för en namnmatchning
MIN_POANG = 2.0         # slug-poäng som krävs för att sidan ska hämtas alls
ANDEL_SARSKILJANDE = 0.6   # delat ord måste väga minst så här mycket av produktens tyngsta


def _fold(s: str) -> str:
    """Vik bort diakriter så 'Fåtölj' och 'fatolj' i en slug möts."""
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def tokenisera(text: str) -> set:
    ord_ = re.split(r"[^a-z0-9]+", _fold(text))
    return {o for o in ord_ if len(o) >= 3 and o not in STOPPORD}


def saljvikt(p: dict) -> float:
    """Hur högt en produkt ska rankas när skrapan väljer vad som är värt ett anrop.

    93 % av sortimentet (11 912 av 12 796) har aldrig sålt en enda enhet.
    Behandlas alla lika drunknar de 303 som faktiskt säljer bland resten, och
    prisdata på en vara som inte rör sig ger inga pengar.

    Men aldrig sålda utesluts INTE – en produkt kan vara osåld just för att
    priset är fel, och den hypotesen går bara att pröva med prisdata. De får
    vikt 1 och hittas när de lätta poängen är tagna. Ordning, inte urval.
    """
    salt = int(p.get("total_sales") or 0)
    if salt >= 5:
        return 5.0
    if salt >= 1:
        return 2.5
    return 1.0


class Katalogindex:
    """Inverterat index över vårt eget sortiment: token → produkter.

    Gör att vi kan gå igenom konkurrentens URL-lista i minnet och avgöra vilka
    som är värda ett HTTP-anrop, i stället för att hämta allt.
    """

    def __init__(self, produkter: list):
        self.produkter = produkter
        self.per_ean = {}
        self.index = {}
        for p in produkter:
            ean = re.sub(r"\D", "", str(p.get("ean") or ""))
            if extract.giltig_ean(ean):
                self.per_ean[ean] = p
            p["_vikt"] = saljvikt(p)
            p["_tokens"] = tokenisera(p.get("name", ""))
            for t in p["_tokens"]:
                self.index.setdefault(t, []).append(p)
        # Sällsynta ord säger mer än vanliga: "bolmen" väger tyngre än "soffa".
        self.vikt = {t: 1.0 / (1 + len(v) ** 0.5) * 10 for t, v in self.index.items()}

    def kandidater(self, text: str, tak: int = 5):
        """Våra produkter som rimligen kan vara samma vara som `text`."""
        tokens = tokenisera(text)
        if not tokens:
            return []
        poang = {}
        for t in tokens:
            for p in self.index.get(t, ())[:400]:
                # Säljvikten avgör ordningen, inte om produkten får vara med.
                poang[p["id"]] = poang.get(p["id"], 0.0) + self.vikt.get(t, 0.0) * p["_vikt"]
        if not poang:
            return []
        basta = sorted(poang.items(), key=lambda kv: kv[1], reverse=True)[:tak]
        per_id = {p["id"]: p for p in self.produkter}
        return [(per_id[i], s) for i, s in basta if s >= MIN_POANG]


def namnlikhet(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _fold(a), _fold(b)).ratio()


def _hamta(session, url, timeout=25):
    try:
        return session.get(url, headers=HEADERS, timeout=timeout), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"


def _bas_url(session, doman: str):
    for kandidat in (f"https://www.{doman}", f"https://{doman}"):
        r, _ = _hamta(session, kandidat + "/")
        if r is not None and r.status_code == 200 and len(r.content) > 20000:
            return kandidat
    return None


def sitemap_urler(session, bas: str, disallow: list, log, tak: int = 60000) -> list:
    """Alla produkt-URL:er sajten själv pekar ut, inom rimliga gränser."""
    _, sitemaps = disallow_och_sitemaps(session, bas)
    kandidater = [s for s in sitemaps if s.strip()] or []
    kandidater += [bas + "/sitemap.xml", bas + "/sitemap_index.xml",
                   bas + "/media/sitemap.xml"]

    sedda, ut = set(), []
    ko = list(dict.fromkeys(kandidater))[:4]
    while ko and len(ut) < tak:
        sm = ko.pop(0)
        if sm in sedda:
            continue
        sedda.add(sm)
        r, _ = _hamta(session, sm, timeout=60)
        time.sleep(0.8)
        if r is None or r.status_code != 200:
            continue
        loc = [urllib.parse.unquote(x) for x in extract.RE_LOC.findall(r.text)]
        under = [x for x in loc if x.lower().endswith(".xml")]
        sidor = [x for x in loc if not x.lower().endswith(".xml")]
        # Sitemapindex: köa delsitemaps, men inte oändligt många.
        for u in under[:25]:
            if u not in sedda:
                ko.append(u)
        for u in sidor:
            p = urllib.parse.urlparse(u).path
            if extract.ASSET_SUFFIX.search(p):
                continue
            if not extract.tillaten(p, disallow):
                continue
            ut.append(u)
    log(f"    sitemap: {len(ut)} tillåtna URL:er")
    return ut


def disallow_och_sitemaps(session, bas: str):
    r, _ = _hamta(session, bas + "/robots.txt")
    if r is None or r.status_code != 200:
        return [], []
    return extract.las_robots(r.text)


def skanna_kalla(session, kalla: dict, index: Katalogindex, log,
                 max_sidor: int, paus: float) -> list:
    """Returnerar observationer för en konkurrentsajt."""
    doman = kalla["doman"]
    log(f"  {kalla['namn']} ({doman})")

    bas = _bas_url(session, doman)
    if not bas:
        log("    ej nåbar – hoppar över")
        return []

    disallow, _ = disallow_och_sitemaps(session, bas)
    time.sleep(paus)
    urler = sitemap_urler(session, bas, disallow, log)
    if not urler:
        log("    ingen sitemap – hoppar över")
        return []

    # Rangordna deras URL:er efter hur väl slugen liknar något vi själva säljer.
    rankade = []
    for u in urler:
        slug = urllib.parse.urlparse(u).path.rstrip("/").split("/")[-1]
        träffar = index.kandidater(slug.replace("-", " "), tak=3)
        if träffar:
            rankade.append((träffar[0][1], u, träffar))
    rankade.sort(key=lambda t: t[0], reverse=True)

    minne = state.las(doman)
    sidor = minne.setdefault("sidor", {})
    log(f"    {len(rankade)} URL:er liknar vårt sortiment  |  minne: {state.sammanfatta(minne)}")

    # Budgeten delas i två. Kända träffar måste ha aktuellt pris – ett gammalt
    # pris är sämre än inget. Resten går till att utforska nytt, så täckningen
    # växer natt för natt i stället för att stanna på samma 400 sidor.
    nu = time.time()
    att_uppdatera = [r for r in rankade
                     if sidor.get(r[1], {}).get("matchad_id")
                     and state.behover_hamtas(sidor.get(r[1]), nu)]
    att_utforska = [r for r in rankade
                    if not sidor.get(r[1], {}).get("matchad_id")
                    and state.behover_hamtas(sidor.get(r[1]), nu)]

    tak_uppdatera = min(len(att_uppdatera), int(max_sidor * 0.6))
    ko = att_uppdatera[:tak_uppdatera]
    ko += att_utforska[:max_sidor - len(ko)]
    log(f"    hämtar {len(ko)}: {len(att_uppdatera[:tak_uppdatera])} kända träffar "
        f"+ {len(ko) - len(att_uppdatera[:tak_uppdatera])} nya "
        f"({len(att_utforska)} outforskade kvar)")

    observationer, hamtade, träffar_tot = [], 0, 0
    for _, url, kandidater in ko:
        r, fel = _hamta(session, url)
        time.sleep(paus)
        hamtade += 1
        post = sidor.setdefault(url, {})
        post["sedd"] = int(nu)

        if r is None or r.status_code != 200:
            post["matchad_id"] = None
            continue
        info = extract.produkt_ur_sida(r.text)
        if not info or not info.get("pris"):
            post["matchad_id"] = None
            continue

        post.update({"namn": (info.get("namn") or "")[:120], "ean": info.get("ean"),
                     "sku": info.get("sku"), "pris": info["pris"],
                     "lager": info.get("lager")})

        # Sidan bär ofta relaterade produkter med egen streckkod och eget pris.
        # Att plocka dem också ger flera träffar per hämtning i stället för en.
        for extra in extract.bulk_ean_priser(r.text, info.get("ean"), info.get("pris")):
            träffad = index.per_ean.get(extra["ean"])
            if not träffad:
                continue
            observationer.append({
                "product_id": int(träffad["id"]),
                "sku": träffad.get("sku", ""),
                "ean": extra["ean"],
                "source": doman,
                "source_product": "",
                "source_url": url[:1000],
                "competitor": kalla["namn"],
                "price": extra["pris"],
                "currency": "SEK",
                "stock_status": "UNKNOWN",
                "matched_by": "ean",
                "_sakerhet": 1.0,
                "_roll": kalla.get("roll", "konkurrent"),
            })
            träffar_tot += 1
            log(f"      {träffad['name'][:34]:34} {extra['pris']:>9.0f} kr  [via sidans data]")

        vår, hur, säkerhet = _matcha(info, kandidater, index)
        if not vår:
            post["matchad_id"] = None
            continue
        post["matchad_id"] = int(vår["id"])

        träffar_tot += 1
        observationer.append({
            "product_id": int(vår["id"]),
            "sku": vår.get("sku", ""),
            "ean": str(vår.get("ean") or ""),
            "source": doman,
            "source_product": (info.get("sku") or "")[:100],
            "source_url": url[:1000],
            "competitor": kalla["namn"],
            "price": info["pris"],
            "currency": info.get("valuta") or "SEK",
            "stock_status": info.get("lager") or "UNKNOWN",
            "matched_by": hur,
            "_sakerhet": round(säkerhet, 3),
            "_roll": kalla.get("roll", "konkurrent"),
        })
        flagga = "" if hur == "ean" else f"  [namnmatch {säkerhet:.2f}]"
        log(f"      {vår['name'][:34]:34} {info['pris']:>9.0f} kr{flagga}")

    state.notera_utbyte(minne, hamtade, träffar_tot)
    storlek = state.spara(doman, minne)
    log(f"    klart: {hamtade} sidor hämtade, {träffar_tot} träffar  "
        f"|  minne sparat: {state.sammanfatta(minne)} ({storlek // 1024} kB)")
    return observationer


def _matcha(info: dict, kandidater: list, index: Katalogindex):
    """(vår produkt, matched_by, säkerhet) – eller (None, ...) om osäkert.

    EAN först: den är global och exakt. Namnmatchning är en kvalificerad
    gissning och märks som sådan, så prismotorn kan välja bort den.
    """
    ean = re.sub(r"\D", "", str(info.get("ean") or ""))
    if extract.giltig_ean(ean) and ean in index.per_ean:
        return index.per_ean[ean], "ean", 1.0

    namn = info.get("namn") or ""
    if not namn:
        return None, "", 0.0

    deras = tokenisera(namn)
    bäst, bästpoäng = None, 0.0
    for vår, _ in kandidater:
        # Modellnamnet måste finnas i båda. "Ebba Hörnsoffa" och "Viola
        # Hörnsoffa" delar bara varutypen men får 0,83 i strängliknelse – nog
        # för att passera tröskeln, och nog för att föreslå att vi tredubblar
        # priset på Ebba. Kräv därför att minst ett SÄLLSYNT ord delas: det är
        # modellnamnet, inte "hörnsoffa", som avgör om det är samma vara.
        egna = vår.get("_tokens") or set()
        gemensamma = egna & deras
        if not gemensamma:
            continue
        # Relativt, inte ett fast tal: vikterna skalar med katalogens storlek,
        # så ett golv som fungerar för 8 produkter är fel för 12 800. Kräv att
        # det delade ordet väger nära produktens tyngsta – då är det modellnamnet.
        tyngsta = max((index.vikt.get(t, 0.0) for t in egna), default=0.0)
        delat = max(index.vikt.get(t, 0.0) for t in gemensamma)
        if tyngsta > 0 and delat < ANDEL_SARSKILJANDE * tyngsta:
            continue
        p = namnlikhet(vår.get("name", ""), namn)
        if p > bästpoäng:
            bäst, bästpoäng = vår, p
    if bäst and bästpoäng >= NAMN_TROSKEL:
        return bäst, "name", bästpoäng
    return None, "", bästpoäng
