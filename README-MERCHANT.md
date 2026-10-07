# Price Radar via Google Merchant Center

Skrapan (`radar.py`) går igenom konkurrenternas sajter. Merchant Center har
redan jämfört sortimentet mot vad resten av marknaden tar för samma vara –
matchat på GTIN och titel över hela Shoppingindexet, inte mot en handplockad
lista konkurrenter. Det här intaget hämtar den jämförelsen.

## Hur det hänger ihop

Ingenting nytt byggdes på WordPress-sidan. Googles benchmark läggs in som en
vanlig observation i det API som redan fanns:

```
merchant.py
  │  GET  /wp-json/venture-price/v1/products        ← katalog: pris, COGS, EAN
  │  (titelmatchning mot Merchant Centers rader)
  └  POST /wp-json/venture-price/v1/observations    → source=google,
                                                      competitor="Google marknadspris"

WordPress (Price Radar-plugin, oförändrad)
  → wpuo_venture_price_watch (unik på product_id + source + competitor)
  → wp-admin → Price Radar: diff mot marknaden, COGS-golv, godkänn-flöde
```

Eftersom unika nyckeln är `(product_id, source, competitor)` skrivs raden över
vid varje körning, och `prev_price` / `price_changed_at` fylls i automatiskt
när marknadsnivån rör sig. Skrapans egna källor ligger kvar oförändrade.

## Spärrar

Googles benchmark matchar ibland fel vara – en hel matgrupp jämförs mot en
ensam stol och ser då ut att vara 170 % för dyr. Sådana rader får aldrig bli
prisförslag, så `merchant.py` kastar dem:

* `--max-gap` (standard 0.60): större gap än så är nästan alltid felmatchning.
* `--min-marginal` (standard 0.10): benchmarken måste ligga minst 10 % över
  vårt inköpspris, annars gäller jämförelsen en annan vara.
* Priset Google rapporterar måste ligga inom 25 % av katalogens pris, annars
  är flödet gammalt och jämförelsen gäller ett pris vi inte längre tar.
* Titlar som bärs av flera produkter löses på pris: den kandidat som ligger
  närmast Googles pris är den annonsen gällde.

Priser ändras fortfarande bara när någon godkänner förslaget i wp-admin.

## Köra nattligt (behöver göras en gång)

1. **Google Cloud** → skapa (eller återanvänd) ett projekt, aktivera
   *Merchant API* och skapa ett **tjänstekonto**. Ladda ner JSON-nyckeln.
2. **Merchant Center** → Inställningar → Personer och åtkomst → lägg till
   tjänstekontots e-postadress (`...@...iam.gserviceaccount.com`) som
   användare med läsrättighet.
3. **GitHub** → repots Settings → Secrets and variables → Actions:
   * `GOOGLE_SA_KEY` = hela JSON-nyckeln
   * `VS_TOKEN` = samma token som skrapan använder
4. Workflowet `.github/workflows/merchant.yml` kör sedan 04:15 UTC varje natt.
   Utan `GOOGLE_SA_KEY` hoppar det över sig självt i stället för att falla.

## Köra utan tjänstekonto

Rader på formen `titel|vårt pris|benchmark` (en per rad), eller en CSV
exporterad ur Merchant Centers prisrapport:

```bash
export VS_TOKEN=...            # venture_price_token i WP
python merchant.py --rows mc-rows.txt --dry-run
python merchant.py --rows mc-rows.txt
```

CSV:en får gärna ha kolumnerna *Title*, *Your price*, *Benchmark* och
*Offer ID* – namnen matchas på både svenska och engelska.

## Vad Googles benchmark faktiskt mäter

Benchmarken är ett klickviktat snitt för **Googles produktkluster**, inte priset
hos en namngiven konkurrent. För möbler med många storlekar klumpas varianterna
ofta ihop, så två olika storlekar av samma modell kan få nästan identisk
benchmark. Mätt mot produkter där vi redan hade skrapade priser ligger
benchmarken systematiskt lägre än vad svenska återförsäljare faktiskt tar.

Använd den som riktning, inte som exakt mål för en prissänkning på en enskild
vara: styr på medianen över alla källor (`market_price` i dashboarden), inte på
lägsta observationen, när Google är enda källan.

Rader där benchmarken ligger mer än 25 % under det lägsta pris vi själva skrapat
märks `OUT_OF_STOCK` i stället för att raderas – `positions()` filtrerar bort
dem, men raden finns kvar. Kontrollen körs i dag som en efterhandsfråga mot
databasen; för att få den automatisk behöver `/products` returnera lägsta
skrapade pris per produkt.
