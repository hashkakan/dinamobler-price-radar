#!/usr/bin/env python3
"""Engångsdiagnostik: vilka prisjamforelsesajter slapper in GitHub Actions?

Provar ENDAST startsidor, som samtliga robots.txt tillater. Syftet ar att
skilja "IP-block" fran "gar att na" – inte att hamta data.
"""
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "sv-SE,sv;q=0.9",
     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

for url in ("https://www.pricerunner.se/",
            "https://www.prisjakt.nu/",
            "https://www.kelkoo.se/",
            "https://www.google.com/"):
    try:
        r = requests.get(url, headers=H, timeout=25)
        verdict = "OK" if r.status_code == 200 and len(r.content) > 50000 else "BLOCKERAD/UTMANING"
        print(f"{url:35} HTTP {r.status_code}  {len(r.content):>9} b   {verdict}")
    except Exception as e:
        print(f"{url:35} FEL: {type(e).__name__}: {str(e)[:70]}")
