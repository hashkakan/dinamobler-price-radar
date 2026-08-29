# Vilka konkurrerar faktiskt med oss

Mätt på dinamobler.se:s eget sortiment, 2026-08-29. Siffran är antalet av
**våra** produkter som butiken också säljer — inte deras katalogstorlek.

Källan är den gamla PriceRunner-datan. Den dög inte som prisunderlag (feed-baserad,
24 dagar gammal, blandade ihop färgvarianter) och raderades därför. Men som karta
över vem som konkurrerar med oss är den det bästa vi har, och den är värd att
behålla: den är uppmätt, inte gissad.

Den första källistan byggdes på allmän kunskap om svensk möbelhandel. Den missade
sju av de arton största konkurrenterna och tog med Åhlens, som inte ens finns på
den här listan bland de tio första.

| Konkurrent | Våra produkter | Snittpris | Status |
|---|---:|---:|---|
| Bra Möbler | 1 149 | 4 754 | ❌ produktsidorna svarar inte (HTTP 000) |
| Furnroom | 1 030 | 3 291 | ❌ ingen sitemap |
| Bygghemma | 884 | 2 618 | ✅ källa |
| Shophome | 879 | 2 749 | ❌ inget pris i strukturerad form |
| First Kitchen → livello.se | 800 | 3 373 | ❌ granskningen gav falsk träff |
| Villahome | 791 | 3 700 | ❌ inget pris i strukturerad form |
| Buyersclub | 763 | 2 592 | ❌ blockerar molntrafik |
| Nordiska Rum | 717 | 3 644 | ✅ källa, **EAN** |
| XXX Lutz | 706 | 4 911 | ❌ blockerar molntrafik |
| ebuy24 | 643 | 4 141 | ❌ ingen sitemap |
| Room99 | 640 | 3 911 | ❌ inget pris i strukturerad form |
| Trademax | 496 | 2 946 | ✅ källa |
| Furniturebox | 480 | 2 937 | ✅ källa |
| Chilli | 477 | 2 945 | ✅ källa |
| Bonus Möbler | 473 | 7 030 | ✅ källa |
| Rowico Home | 459 | 8 137 | ⚠️ **leverantör**, ej konkurrent |
| Ulfåsa | 453 | 6 473 | ✅ källa |
| Norrmalms Möbler | 404 | 6 850 | ❌ inget pris i strukturerad form |
| Proffsmagasinet | 396 | 4 070 | ❓ **aldrig testad** |
| Sleepo | 367 | 6 670 | ✅ källa |
| All interiör | 357 | 5 928 | ✅ källa |
| Reforma STHLM | 352 | 4 530 | ✅ källa, **EAN** |
| Soffadirekt | 315 | 7 093 | ✅ källa, **EAN** |
| Möbeljätten | 276 | 4 845 | ❓ **aldrig testad** |
| Hornbach | 272 | 3 272 | ❓ **aldrig testad** |
| Åhléns | 266 | 3 314 | ✅ källa |
| BAUHAUS | 247 | 2 223 | ✅ källa |
| nolhagahem.se | 245 | 3 491 | ❓ **aldrig testad** |
| Folkhemmet | 175 | 4 147 | ❌ inget pris i strukturerad form |
| Tibergs Möbler | 174 | 6 591 | ❓ **aldrig testad** |
| Stalands Möbler | 74 | 6 738 | ❌ inget pris i strukturerad form |
| AO Möbler | 69 | 8 906 | ❓ **aldrig testad** |

## Kvar att prova

Fem av dem har aldrig granskats: **Proffsmagasinet, Möbeljätten, Hornbach,
Nolhaga Hem och Tibergs Möbler**. Tillsammans överlappar de drygt 1 500 av våra
produkter. Kör dem genom `check_sources.py` innan nästa utökning.

## Vad som redan är prövat och underkänt

Testa inte om utan skäl — skälen står i `sources.json`. Bra Möbler är den största
förlusten: 15 859 produkter i deras sitemap, men produktsidorna svarar inte alls
på våra anrop. Värd ett nytt försök om det ändrar sig.
