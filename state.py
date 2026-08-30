#!/usr/bin/env python3
"""Minne mellan nattliga körningar.

Utan det här börjar varje natt om från noll och hinner 400 sidor per källa.
Trademax har 31 905 URL:er som liknar vårt sortiment – vid den takten skulle
en full genomgång ta tre månader, och då vore varvet ett innan varv två är
klart. Med ett index som byggs på växer täckningen i stället varje natt.

Vad som sparas per källa:
  sidor[url] = {namn, ean, sku, pris, lager, sedd, matchad_id}

`matchad_id` är nyckeln: en URL vi vet matchar en av våra produkter ska
prisuppdateras ofta, medan en URL som visat sig irrelevant inte behöver
hämtas igen på länge. Att skilja på de två är hela vinsten.

Lagras som gzippad JSON under state/ och bärs mellan körningar av
actions/cache. Går cachen förlorad byggs indexet upp igen – långsammare,
men inget går sönder.
"""
from __future__ import annotations

import gzip
import json
import re
import time
from pathlib import Path

STATE_DIR = Path(__file__).with_name("state")

# Hur ofta en sida behöver hämtas om.
FARSK_MATCH_DYGN = 2      # kända träffar: priset måste vara aktuellt
FARSK_MISS_DYGN = 45      # kända missar: sällan värt ett nytt anrop


def _fil(doman: str) -> Path:
    trygg = re.sub(r"[^a-z0-9.-]", "_", doman.lower())
    return STATE_DIR / f"{trygg}.json.gz"


def las(doman: str) -> dict:
    f = _fil(doman)
    if not f.exists():
        return {"sidor": {}, "uppdaterad": 0}
    try:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("sidor"), dict):
            return data
    except Exception:
        pass  # Trasig cache ska inte stoppa körningen – bygg om i stället.
    return {"sidor": {}, "uppdaterad": 0}


def spara(doman: str, state: dict) -> int:
    STATE_DIR.mkdir(exist_ok=True)
    state["uppdaterad"] = int(time.time())
    f = _fil(doman)
    with gzip.open(f, "wt", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, separators=(",", ":"))
    return f.stat().st_size


def behover_hamtas(post: dict | None, nu: float | None = None) -> bool:
    """Ska den här URL:en hämtas den här natten?"""
    if not post:
        return True
    nu = nu if nu is not None else time.time()
    alder_dygn = (nu - float(post.get("sedd") or 0)) / 86400
    grans = FARSK_MATCH_DYGN if post.get("matchad_id") else FARSK_MISS_DYGN
    return alder_dygn >= grans


HISTORIK_MAX = 5          # antal körningar som väger in i utbytet


def notera_utbyte(state: dict, sidor: int, traffar: int) -> None:
    """Sparar nattens utfall så budgeten kan följa var träffarna faktiskt finns."""
    h = state.setdefault("historik", [])
    h.append({"sidor": int(sidor), "traffar": int(traffar)})
    del h[:-HISTORIK_MAX]


def utbyte(doman: str) -> tuple[int, int]:
    """(hämtade sidor, träffar) över de senaste körningarna."""
    h = las(doman).get("historik") or []
    return sum(int(x.get("sidor") or 0) for x in h), sum(int(x.get("traffar") or 0) for x in h)


KATALOG_FIL = STATE_DIR / "katalog.json.gz"
KATALOG_FARSK_DYGN = 7


# Fält katalogen måste bära. Saknas något har endpointen utökats sedan cachen
# skrevs, och då är cachen för gammal oavsett ålder – annars körs en ny körning
# på gammalt format utan att någon märker det.
KATALOG_FALT = ("id", "name", "ean", "total_sales")


def las_katalog() -> tuple[list, float]:
    """(produkter, ålder i dygn). Tom lista om cachen saknas, är trasig eller föråldrad."""
    if not KATALOG_FIL.exists():
        return [], 1e9
    try:
        with gzip.open(KATALOG_FIL, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        produkter = data.get("produkter") or []
        if not produkter:
            return [], 1e9
        if any(f not in produkter[0] for f in KATALOG_FALT):
            return [], 1e9
        alder = (time.time() - float(data.get("hamtad") or 0)) / 86400
        return produkter, alder
    except Exception:
        return [], 1e9


def spara_katalog(produkter: list) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    with gzip.open(KATALOG_FIL, "wt", encoding="utf-8") as fh:
        json.dump({"hamtad": int(time.time()), "produkter": produkter},
                  fh, ensure_ascii=False, separators=(",", ":"))


def sammanfatta(state: dict) -> str:
    sidor = state.get("sidor", {})
    matchade = sum(1 for p in sidor.values() if p.get("matchad_id"))
    return f"{len(sidor)} kända URL:er, {matchade} matchade"
