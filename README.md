# Price Radar – konkurrentpris-skrapa

Skrapar konkurrenternas priser från **PriceRunner** och matar in dem i Dina Möblers
Price Radar (WordPress). Prismotorn på sajten tar sedan vid: föreslår och applicerar
lägre priser som hamnar i rea → Google Ads-kampanjen.

Körs på **GitHub Actions** (gratis, schemalagt, ingen server att sköta). Skrapan får
**aldrig** köras från webbservern – PriceRunner blockerar server-IP:n.

---

## Så kommer du igång (engång, ~5 min)

1. **Skapa ett privat GitHub-repo** (t.ex. `dinamobler-price-radar`).
2. Lägg upp innehållet i den här mappen i repots **rot** (`radar.py`, `requirements.txt`,
   `README.md` och `.github/workflows/scrape.yml`).
3. I repot: **Settings → Secrets and variables → Actions → New repository secret**, lägg till:
   - `SITE_URL` = `https://dinamobler.se`
   - `VS_TOKEN` = *(den hemliga token från WP – fråga om du inte har den)*
4. Gå till **Actions**-fliken → *Price Radar* → **Run workflow** för en första körning.

Efter det kör den **automatiskt varje natt** (03:15 UTC). Ändra tiden i `scrape.yml` (`cron`).

---

## Testa lokalt (valfritt)

```bash
pip install -r requirements.txt
export SITE_URL=https://dinamobler.se
export VS_TOKEN=din_hemliga_token
python radar.py --limit 25 --dry-run      # 25 produkter, skriver INGET
python radar.py --limit 25                # 25 produkter, matar in på riktigt
python radar.py                           # full körning
```

Flaggor: `--limit N` (testa få), `--dry-run` (skriv inget), `--sleep 2` (långsammare mot
PriceRunner), `--batch 200` (observationer per POST).

---

## Hur det hänger ihop

```
radar.py (GitHub Actions)
  │  GET  /wp-json/venture-price/v1/products?token=…&only_ean=1   ← hämtar katalog + EAN + COGS
  │  → slår upp varje EAN på PriceRunner, läser konkurrenternas priser
  └  POST /wp-json/venture-price/v1/observations  (X-VS-Token)    → matar in priserna

WordPress (Price Radar-plugin)
  → prismotorn genererar prisförslag (över COGS-golv) och applicerar
  → sänkta priser hamnar i rea → Google Ads rea-PMax
```

---

## Om PriceRunner slutar hitta träffar

PriceRunners publika API-URL:er ändras ibland. Skrapan använder två endpoints överst i
`radar.py`:

```python
PR_SEARCH = "https://www.pricerunner.se/public/search/v5/SE/search"
PR_OFFERS = "https://www.pricerunner.se/public/product/v4/SE/{pid}/offers"
```

Om körningen ger `med_traff=0`: öppna en produkt på pricerunner.se i webbläsaren, kolla i
**Nätverksfliken** vilka `…/public/…`-anrop sidan gör för sök och erbjudanden, och uppdatera
de två URL:erna + fältnamnen i `pr_search_product_id()` / `pr_offers()`. Resten av flödet
(katalog + inmatning) är stabilt och behöver inte röras.

> **Obs:** kontrollera PriceRunners villkor för skrapning. Håll `--sleep` ≥ 1,5 s och kör
> nattetid för att vara skonsam.
