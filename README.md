# Adaptive Trading Bot

Automatický obchodní systém napojený na **TradingView**. Alerty z TradingView chodí
webhookem na tvůj server, bot je vyhodnotí vlastní adaptivní logikou, spočítá
velikost pozice podle **rizika 2 % majetku na obchod**, nastaví SL i stupňovité TP
podle aktuálního režimu trhu a odešle příkazy na burzu s pákovým obchodováním.

> **Než začneš:** obchodování s pákou je vysoce rizikové a většina retailových
> účtů na něm dlouhodobě prodělá. Bot může snížit chyby z emocí a udržet
> disciplínu v řízení rizika, ale **nedokáže zaručit zisk**. Výchozí nastavení je
> `paper` (simulace) — do živého režimu přepni až po týdnech testování na testnetu.

---

## Co bot umí

| Oblast | Co je hotové |
|---|---|
| **Vstup signálů** | TradingView webhook (JSON i prostý text), HMAC-SHA256 podpis, IP allowlist, deduplikace |
| **Adaptace na trh** | Detekce režimu (trend / range / volatilní / klidný) z ADX, ATR, EMA, RSI, objemu a vyššího timeframu |
| **Filtr kvality** | Confluence skóre 0–1 ze 6 faktorů; slabé signály se nezobchodují, silné dostanou větší size |
| **Trhy** | Perpetual kontrakty i spot — včetně Bybit EU, kde deriváty nejsou |
| **Řízení rizika** | Fixní % equity na obchod, portfoliový strop, denní stop-loss, cooldowny, kill switch |
| **SL / TP** | SL z násobku ATR podle režimu, TP jako ladder v násobcích R, breakeven po TP1, ATR trailing po TP2 |
| **Páka** | Dopočítaná z potřeby marže, omezená konfigurací, burzou i odstupem likvidace od SL |
| **Exekuce** | CCXT (10+ burz), SL/TP přímo na burze, automatické uzavření pozice, pokud SL nelze umístit |
| **Učení** | Riziko se posouvá podle historické expektance v daném režimu (SQLite historie) |
| **Živé rozhraní** | Webový dashboard: co robot vidí, proč vstoupil, pozice, historie — a nastavení všeho za běhu |
| **Autopilot** | Vlastní generátor signálů — funguje bez TradingView i bez veřejné adresy |
| **Výběr trhů** | Robot si vybírá ze **všech perpetual kontraktů na burze**, ne z pevného seznamu |
| **Provoz** | Aplikace pro macOS s ikonou, offline režim, paper broker, backtest, Telegram notifikace, Docker, 208 testů |

---

## Instalace na MacBook

Otevři **Terminál** (Cmd+mezerník → napiš „Terminál“) a vlož tyhle dva řádky:

```bash
git clone -b claude/tradingview-auto-trading-bot-iqmukq https://github.com/icirvortep/Webtrading-.git ~/AdaptiveTradingBot
~/AdaptiveTradingBot/scripts/install-mac.sh
```

Na ploše se objeví aplikace **Adaptive Trading Bot** s ikonou. Od té chvíle už
Terminál nepotřebuješ — stačí dvojklik.

> Kdyby macOS při prvním spuštění hlásil „od neověřeného vývojáře": klikni na
> ikonu pravým tlačítkem → *Otevřít* → *Otevřít*. Stačí jednou.

Aktualizace na novou verzi: `cd ~/AdaptiveTradingBot && git pull`.
Ikonu na ploše měnit nemusíš, ukazuje na tutéž složku.

Aplikace jde spustit i bez instalace — dvojklikem na `start-mac.command`
přímo ve složce, nebo z Terminálu `./start-mac.command`.

### Co dvojklik udělá

Otevře Terminál (aby bylo vidět, co se děje, a šlo to ukončit přes Ctrl+C),
poprvé si sám vytvoří prostředí a doinstaluje závislosti, zeptá se na režim
a otevře rozhraní v prohlížeči.

Na výběr jsou tři režimy:

| Režim | Co dělá | Potřebuje |
|---|---|---|
| **OFFLINE** | simulovaná data, celé rozhraní k vyzkoušení | nic |
| **PAPER** | reálná data z burzy, obchody jen nanečisto | nic (jen internet) |
| **LIVE** | skutečné peníze, vyžádá si potvrzení | API klíče |

Z Terminálu totéž: `./start-mac.command 1` (offline), `2` (paper), `3` (live).

### Kdyby něco nešlo

| Problém | Řešení |
|---|---|
| „od neověřeného vývojáře" | pravým tlačítkem na ikonu → *Otevřít* → *Otevřít* |
| Aplikace nic neudělá | v Terminálu: `~/AdaptiveTradingBot/start-mac.command` — uvidíš chybu |
| „Nenašel jsem složku s botem" | složka se přesunula; spusť znovu `scripts/install-mac.sh` |
| Chybí Python | `brew install python@3.12` (Homebrew z https://brew.sh) |
| Port 8080 je obsazený | `./start-mac.command` a pak v `config/config.yaml` změň `webhook.port` |

### Co uvidíš v rozhraní

`http://localhost:8080` — pět záložek, obnovuje se každé 3 sekundy:

* **Živý přehled** — karta pro každý sledovaný trh: cena, režim, ATR/ADX/RSI,
  graf, a **skóre pro long i short vedle sebe** včetně navrženého SL a všech TP.
  Zeleně orámovaný směr = robot by právě teď vstoupil. Pod tím vstupní spouštěče
  (pullback / mean-reversion / průraz) s vysvětlením, co se na trhu stalo.
* **Pozice** — otevřené obchody, průběžný PnL, tlačítko na okamžité zavření.
* **Rozhodnutí** — *proč* vstoupil nebo nevstoupil. Každý signál, skóre a důvod
  zamítnutí („protitrendový vstup v silném trendu", „spread příliš široký").
* **Historie** — uzavřené obchody s PnL a výsledkem v R.
* **Nastavení** — viz níže.

### Co si můžeš nastavit přímo v rozhraní

Změny platí okamžitě, bez restartu, a uloží se do `config/config.yaml`:

| Skupina | Volby |
|---|---|
| **Investice a riziko** | % účtu na obchod (výchozí 2 %), tvrdý strop, riziko portfolia, denní stop, max. páka, max. pozic, max. obchodů za den |
| **Kdy vstoupit** | minimální skóre, zákaz protitrendu, učení z historie, ADX prahy pro trend/range, hranice volatility, násobek vyššího TF |
| **SL / TP pro každý režim** | SL jako násobek ATR, trailing jako násobek ATR, a **libovolný počet TP** — každý s vlastním násobkem R a podílem pozice; tlačítkem přidáš nebo ubereš stupeň |
| **Sledované trhy** | výběr z celé burzy nebo pevný seznam, minimální objem, počet analyzovaných trhů, velikost dávky, váhy žebříčku, timeframe, interval, **autopilot** |
| **Ochrany** | pauza po ztrátě, pauza po sérii ztrát, max. spread, časový stop, kill switch |

Citlivá pole (režim, burza, API klíče) rozhraní změnit **nemůže** — mění se
jen v souboru, aby je nešlo přepnout omylem nebo přes prohlížeč.

### Z čeho si robot vybírá

Ve výchozím nastavení nesleduje pevný seznam, ale **celou nabídku burzy**.
Bybit má stovky perpetual kontraktů; projít je všechny v plné hloubce každých
pár vteřin nejde, limity API to nedovolí. Proto výběr probíhá ve dvou fázích:

1. **Hrubé síto** (jednou za 15 minut, jediný dotaz na burzu) — stáhne tickery
   všech trhů, vyhodí nelikvidní a se širokým spreadem, a zbytek seřadí podle
   složeného skóre: **likvidita** (0.40) + **volatilita** (0.35) + **denní pohyb**
   (0.25). U volatility se nehledá maximum, ale optimum — mrtvý trh nedá
   příležitost, chaotický nedá rozumný stop.
2. **Hloubková analýza** (každých 20 s) — 24 nejlepších kandidátů projde plnou
   analýzou režimu, skóre a SL/TP. Nedělá se najednou, ale po dávkách po osmi,
   takže každý trh se obnoví zhruba jednou za minutu a zátěž vychází na méně
   než 2 dotazy za sekundu — hluboko pod limity burzy.

Záložka **Příležitosti** ukazuje výsledek: žebříček všech analyzovaných trhů
seřazený podle nejlepšího nalezeného vstupu, se skóre, směrem, navrženým SL,
počtem TP a stavem (*připraveno* / *čeká*, včetně důvodu čekání).

Všechny váhy, prahy i počty se dají měnit v Nastavení. Když chceš zpátky pevný
seznam, stačí vypnout přepínač **Vybírat z celé burzy**.

### Autopilot vs. TradingView

Bot umí pracovat dvěma způsoby a můžeš je i kombinovat:

* **Autopilot (zapneš v Nastavení)** — skener sám hledá vstupy stejnou logikou,
  jakou má Pine skript. **Nepotřebuje TradingView ani veřejnou adresu**, takže
  na MacBooku funguje rovnou. Doporučená volba.
* **TradingView webhook** — signály chodí z tvých alertů. Vyžaduje placený plán
  TradingView a veřejnou HTTPS adresu; z domácího Macu potřebuješ tunel
  (`cloudflared tunnel --url http://localhost:8080`) a jeho URL vložit do alertu.

---

## Rychlý start (5 minut, bez rizika)

```bash
git clone <tvůj-repozitář> && cd Webtrading-
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # klíče můžeš zatím nechat prázdné

export PYTHONPATH=src
python -m atb demo                            # ← START ZDE: celý bot naživo, bez klíčů a bez internetu
python -m atb venues                          # přehled burz a jejich páky
python -m atb analyze BTC/USDT:USDT            # rozbor trhu: režim, skóre, návrh SL/TP
python -m atb backtest BTC/USDT:USDT --limit 1500
python -m atb run                              # webhook server v paper režimu
pytest                                         # 208 testů
```

Server pak poslouchá na `http://localhost:8080/webhook/tradingview`.
Stav účtu a pozic: `curl localhost:8080/status`.

### `python -m atb demo` — co uvidíš

Projde celý řetězec na vygenerovaných datech, takže si chování ověříš dřív,
než se bot vůbec připojí k burze:

```
[1/5] Účet: 10000.00 USDT, riziko na obchod 2.0 %
[2/5] Rozbor trhu BTC/USDT:USDT 15m
      cena 77309.72 | režim range | ATR 0.70 % | ADX 22.7 | RSI 63.1 | HTF +0
[3/5] Přichází signál z TradingView: LONG BTC/USDT:USDT
      → PŘIJAT (skóre 0.701)
[4/5] Plán obchodu
      vstup      77309.72
      stop loss  75392.18  (2.48 % od vstupu)
      TP1        78843.74  (0.8R, 60 % pozice)
      TP2        80186.02  (1.5R, 40 % pozice)
      množství   0.081605  (notional 6308.85 USDT)
      páka       1x
      riziko     156.48 USDT = 1.56 % účtu  ← zásah SL stojí přesně tolik
      kontrola   1917.53 × 0.081605 = 156.48 USDT
[5/5] Řízení pozice v čase
      cena +1.2R → SL posunut na 77363.84 (breakeven)
      cena   +8R → SL posunut na 89889.65 (trailing)
```

Výstup je deterministický — stejný běh dá stejná čísla. Zkus i
`--side short`, ať vidíš, jak vypadá zamítnutý signál.

---

## Spot vs. perpetual kontrakty

Bot umí obě varianty; volí se přes `exchange.account_type`.

| | `swap` (perpetual) | `spot` |
|---|---|---|
| Kde | Bybit globální, Binance, Bitget… | **Bybit EU**, každý spotový účet |
| Páka | až 100× | **žádná** (1×) |
| Shorty | ano | **ne** — prodat jde jen to, co vlastníš |
| SL/TP | leží na burze, chrání i při vypnutém botu | **hlídá bot lokálně** |
| Symboly | `BTC/USDT:USDT` | `BTC/USDT` |

Při `account_type: spot` si bot sám srovná konfiguraci: sníží páku na 1×,
vypne burzovní stopy, odmítne short signály a v přehledu trhů přestane
nabízet směr, který stejně nejde zobchodovat.

> **Důležité u spotu:** stop loss neleží na burze, takže **existuje jen dokud
> bot běží**. Když vypneš Mac nebo aplikaci, pozice zůstane bez ochrany.
> U perpetuálů to neplatí — tam SL drží burza.

**Bybit EU** (`bybiteu`) má MiCA licenci pro spot a margin, ale **nenabízí
perpetual kontrakty** — na ty je potřeba licence MiFID II, kterou zatím nemá.
Při `account_type: swap` proto bot rovnou při startu odmítne nastartovat
a vysvětlí proč.

## Výběr burzy

`python -m atb venues` vypíše aktuální katalog. Orientační stropy páky
(mění se podle risk tier, jurisdikce a pravidel burzy — bot si skutečný
limit vždy ověří z metadat trhu):

| Burza | Max páka | Testnet | Poznámka |
|---|---:|---|---|
| **Bybit** ⭐ | 100x | ano | Nejlepší poměr likvidita / API / testnet — výchozí volba |
| Binance USD-M | 125x | ano | Nejvyšší likvidita, páka klesá s velikostí pozice |
| Bitget | 125x | ano | Nízké poplatky, demo účet |
| OKX | 100x | ano | Vyžaduje passphrase k API klíči |
| KuCoin Futures | 100x | ano | Pozor na velikost kontraktu |
| BingX | 150x | ne | Vyšší páka, mělčí knihy u exotů |
| MEXC | 200x | ne | Nejvyšší nominální páka, vyšší slippage |
| Hyperliquid | 50x | ano | On-chain DEX, bez KYC |

**Doporučení:** začni na **Bybit testnetu**. Má plnohodnotný testnet
s reálnými daty, takže si celý řetězec TradingView → bot → burza otestuješ
s nulovým rizikem. Nejvyšší páka není výhoda — viz sekce níže.

---

## Jak bot rozhoduje

### 1. Signál z TradingView
Přiložený Pine skript `pine/adaptive_signal_engine.pine` posílá JSON:

```json
{
  "secret": "…", "action": "buy", "symbol": "BTCUSDT", "timeframe": "15",
  "price": 65000.0, "sl": 64100.0, "confidence": 0.78, "regime": "trend_up",
  "id": "1700000000BTCUSDTbuy"
}
```

Fungují i jednodušší alerty (`{"action":"buy","symbol":"BTCUSDT"}`, dokonce
i prostý text `BUY BTCUSDT 15m`) — chybějící údaje si bot dopočítá sám.

### 2. Detekce režimu trhu
Bot si stáhne svíčky ze signálního i **vyššího timeframu** (4×) a spočítá
ATR, ADX, ±DI, EMA 21/55, RSI, šířku Bollingera a z-skóre objemu. Z toho určí režim:

| Režim | Podmínka | Chování |
|---|---|---|
| `trend_up` / `trend_down` | ADX ≥ 23 a shoda EMA + HTF | Volnější SL (2× ATR), TP až 3.5R, trailing |
| `range` | ADX ≤ 18 | Těsný SL (1.2× ATR), rychlé TP 0.8R a 1.5R |
| `volatile` | ATR ≥ 3 % ceny | Široký SL (3× ATR), menší pozice (×0.6) |
| `quiet` | ATR ≤ 0.35 % ceny | Menší pozice (×0.75), skromné cíle |

### 3. Confluence skóre
Šest vážených faktorů → jedno číslo 0–1:
confidence z TV (20 %), soulad s režimem (25 %), potvrzení vyšším TF (20 %),
momentum (15 %), vhodnost volatility (10 %), účast objemu (10 %).

Skóre pod prahem (`min_score`, výchozí 0.45) = obchod se neotevře. Nad prahem
skóre lineárně řídí velikost pozice (0.6× až 1.0× základního rizika).
Navíc platí tvrdá veta: protitrendový vstup v silném trendu, RSI ≥ 85 pro long
nebo ≤ 15 pro short.

### 4. Velikost pozice — jádro celé věci

```
množství = (equity × riziko %) / |vstup − stop loss|
```

Při equity 10 000 USDT, riziku 2 % a SL 1,5 % pod vstupem:
riskuješ 200 USDT, pozice má notional ≈ 13 333 USDT.

Zásah SL tedy stojí **přesně 2 % účtu** — nezávisle na tom, jak daleko SL leží
a jakou páku burza nabízí. Toto pravidlo hlídá test
`test_position_size_risks_exactly_configured_percent`.

Základní 2 % se dál upravují (a nikdy nepřekročí tvrdý strop `max_risk_per_trade_pct`):

| Vliv | Násobek |
|---|---|
| Kvalita signálu (skóre) | 0.6× – 1.0× |
| Volatilní trh / mrtvý trh | 0.6× / 0.75× |
| Historická expektance v daném režimu | 0.4× – 1.3× |
| Série ztrát (2., 3., 4. v řadě) | 0.75× / 0.5× / 0.5× |

### 5. Páka není volba agresivity
Páka se **dopočítá** tak, aby na pozici stačila volná marže — a pak se ořízne
na minimum ze tří limitů: tvůj `max_leverage`, limit burzy, a páka, při níž je
likvidační cena bezpečně (1.6×) za stop lossem. Vyšší páka sama o sobě
nezvyšuje ztrátu při zásahu SL; zvyšuje jen kapitálovou efektivitu a riziko,
že tě dřív než SL trefí likvidace. Proto ji bot drží co nejnižší.

### 6. Výstupy
* **SL** = násobek ATR podle režimu, posunutý za nejbližší swing, s clampem 0.25 %–8 %.
* **TP ladder** v násobcích R, po částech (např. 40 % / 35 % / 25 % pozice).
* **Breakeven** po dosažení TP1 (včetně offsetu na poplatky).
* **ATR trailing** po TP2, stop se posouvá jen ve prospěch obchodu.
* **Časový stop** (volitelně) při překročení maximální doby držení.
* SL/TP leží **přímo na burze**, takže chrání pozici i při výpadku bota.
  Pokud se SL nepodaří umístit, bot pozici okamžitě zavře.

---

## Nastavení TradingView

1. **Přidej Pine skript**: TradingView → Pine Editor → vlož obsah
   `pine/adaptive_signal_engine.pine` → *Add to chart*.
2. Do pole „Webhook secret" vlož stejnou hodnotu jako `WEBHOOK_SECRET` v `.env`.
3. **Vytvoř alert**: Condition = *Adaptive Signal Engine* → *Any alert() function call*,
   Options = *Once Per Bar Close*.
4. **Notifications → Webhook URL**: `https://tvuj-server/webhook/tradingview`
   (message nech prázdnou, JSON posílá skript sám).

> Webhooky vyžadují placený plán TradingView (Essential a výše).
> Endpoint musí být dostupný z internetu přes HTTPS — použij reverzní proxy
> (Caddy/nginx) nebo tunel (Cloudflare Tunnel) a nikdy nevystavuj port přímo.

Bot přijímá i alerty z tvé vlastní strategie — stačí, aby message obsahovala
JSON s `action` a `symbol`.

---

## Napojení vlastního účtu na Bybit

### 1. Klíče do `.env`

```bash
cd ~/AdaptiveTradingBot
cp .env.example .env
open -e .env          # otevře v TextEditu
```

Vyplň dva řádky a ulož:

```
EXCHANGE_API_KEY=tvůj_klíč
EXCHANGE_API_SECRET=tvůj_secret
```

Soubor `.env` je v `.gitignore`, takže se nikdy nedostane do repozitáře.
Secret ti Bybit ukáže **jen jednou** při vytvoření klíče.

### 2. Burza a typ účtu v `config/config.yaml`

```yaml
exchange:
  id: bybiteu           # bybit = globální (perpetuály), bybiteu = EU (jen spot)
  account_type: spot    # spot pro Bybit EU, swap pro globální
  testnet: false
```

### 3. Obchodování ze všech měn

Tohle je **výchozí chování** — pevný seznam tří symbolů je jen záloha pro
případ, že automatický výběr vypneš:

```yaml
symbols_allowlist: []   # prázdné = povoleno vše, co burza nabízí
scanner:
  auto_universe: true   # vybírat z celé burzy
universe:
  min_volume_24h: 50000000.0   # ← tohle si na menší burze uprav
  deep_scan_count: 24          # kolik nejlepších analyzovat do hloubky
```

> **Práh objemu se přizpůsobí sám.** Je kalibrovaný na objemy globální burzy;
> na menší (třeba Bybit EU) by vyřadil úplně všechno. Když k tomu dojde, bot
> místo něj vezme nejlikvidnější špičku a napíše do logu, co udělal —
> neskončí naprázdno. Vypnout to jde přes `universe.adaptive_filters: false`.

### 4. Postup zapnutí

```
1) PAPER na reálných datech   ← nech běžet dny, sleduj záložku Rozhodnutí
2) mode: live, dry_run: true  ← počítá plán, nic neodesílá
3) mode: live, dry_run: false ← ostrý provoz, vyžádá si potvrzení v konzoli
```

V rozhraní si před krokem 3 sniž `risk_per_trade_pct` na 0.5 %, ať první
reálné obchody nic nestojí, a zvyš to až po pár desítkách obchodů.

## Ostrý provoz

```bash
# 1) klíče (jen obchodování futures, NIKDY oprávnění k výběru, ideálně IP whitelist)
cp .env.example .env && nano .env
openssl rand -hex 32          # → WEBHOOK_SECRET

# 2) týdny na testnetu
#    config.yaml: mode: paper, exchange.testnet: true
python -m atb run

# 3) mezikrok: živé klíče, ale jen výpočet plánu bez odeslání
#    config.yaml: mode: live, dry_run: true
python -m atb run

# 4) ostrý provoz (vyžádá si potvrzení v konzoli)
#    config.yaml: mode: live, dry_run: false, exchange.testnet: false
python -m atb run --live
```

Docker:
```bash
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f
```

### Ovládání za běhu

| Endpoint | Význam |
|---|---|
| `GET /` | Webové rozhraní |
| `GET /api/state` | Vše pro rozhraní v jednom dotazu |
| `PUT /api/settings` | Změna nastavení za běhu |
| `POST /api/scan` | Vynutí okamžitý sken trhů |
| `GET /health` | Kontrola, že bot žije |
| `GET /status` | Equity, otevřené pozice, statistika |
| `GET /trades?limit=20` | Otevřené a uzavřené obchody |
| `POST /control/kill-switch?enable=true` | Okamžitě zastaví nové vstupy |
| `POST /control/close-all` | Nouzově zavře všechny pozice |

CLI: `python -m atb status`, `python -m atb close-all`, `python -m atb test-signal BTC/USDT:USDT`.

---

## Bezpečnostní pojistky

| Pojistka | Výchozí hodnota | Co dělá |
|---|---|---|
| `risk_per_trade_pct` | 2 % | Maximální ztráta na jeden obchod |
| `max_risk_per_trade_pct` | 3 % | Strop i po adaptivním navýšení |
| `max_portfolio_risk_pct` | 6 % | Součet rizika všech otevřených pozic |
| `max_daily_loss_pct` | 6 % | Denní stop — po překročení bot přestane vstupovat |
| `max_daily_trades` | 20 | Ochrana proti rozbité strategii chrlící signály |
| `max_open_positions` | 4 | Limit souběžných pozic |
| `cooldown_after_loss_min` | 15 min | Pauza na symbolu po ztrátě |
| `streak_cooldown_min` | 120 min | Delší pauza po 3 ztrátách v řadě |
| `max_spread_bps` | 12 bps | Neobchoduje v nelikvidních podmínkách |
| `signal_max_age_sec` | 90 s | Zahodí zpožděné alerty |
| `kill_switch` | false | Tvrdé zastavení všech nových vstupů |

Klíče se čtou **jen z prostředí**, nikdy z konfigurace v gitu. `.env` a `data/`
jsou v `.gitignore`. V živém režimu je webhook secret povinný a `/docs` se vypne.

---

## Struktura projektu

```
src/atb/
├── main.py               CLI (demo, run, analyze, backtest, venues, status, close-all)
├── scanner.py            skener na pozadí: živý přehled + autopilot
├── universe.py           výběr a žebříček trhů z celé nabídky burzy
├── ui/                   webové rozhraní (HTML/CSS/JS, bez build kroku)
├── trader.py             orchestrace: signál → rozhodnutí → exekuce
├── config.py             YAML + přepisy z prostředí, validace přes pydantic
├── models.py             doménové typy (Signal, TradePlan, Position, …)
├── backtest.py           event-driven backtest stejnou logikou jako živý bot
├── strategy/
│   ├── indicators.py     EMA, ATR, ADX, RSI, Bollinger, z-skóre (čisté numpy)
│   ├── regime.py         klasifikace režimu trhu
│   ├── scoring.py        confluence skóre a veta
│   ├── signals.py        vlastní vstupní spouštěče (Python protějšek Pine)
│   ├── exits.py          SL, TP ladder, trailing, breakeven
│   └── engine.py         spojení dat, režimu, skóre a plánu
├── risk/manager.py       sizing, páka, limity, cooldowny, adaptace
├── execution/router.py   odeslání příkazů, umístění SL/TP, úklid po chybě
├── monitor/position_manager.py   trailing, breakeven, časový stop, rekonciliace
├── exchanges/            base, ccxt_adapter, paper broker, offline demo, katalog burz
├── webhook/              parsování payloadu, HMAC, API a FastAPI server
└── state/store.py        SQLite: obchody, signály, equity, statistiky režimů

macos/AdaptiveTradingBot.app   balíček aplikace pro macOS (ikona + spouštěč)
scripts/install-mac.sh         položí aplikaci na plochu
scripts/make_icon.py           vygeneruje ikonu (.icns) bez externích nástrojů
start-mac.command              spuštění bez instalace
```

---

## Testy

```bash
pytest                      # 208 testů, běží bez sítě proti falešné burze
pytest --cov=src/atb        # s pokrytím
ruff check src tests scripts   # lint
```

Testy pokrývají mimo jiné: přesnost sizingu na 2 %, stropy páky, denní
stop-loss, cooldowny, veta protitrendových vstupů, ověření HMAC podpisu,
IP allowlist, a to, že pozice bez stop lossu je okamžitě uzavřena.

---

## Co bot záměrně nedělá

* **Neslibuje zisk.** Adaptace parametrů zlepšuje konzistenci řízení rizika,
  nikoli předpověď trhu.
* **Neobchoduje bez stop lossu.** Když SL nelze umístit, pozice se zavře.
* **Nenavyšuje ztrátovou pozici** (žádné martingale ani průměrování dolů).
* **Neoptimalizuje parametry na historii automaticky** — to vede k overfittingu.
  Backtest je nástroj pro tebe, ne smyčka, která si sama přenastaví strategii.
* **Nepočítá s daněmi ani reportingem.** Zisky z obchodování v ČR se daní;
  historii obchodů najdeš v `data/atb.sqlite`.

---

## Licence

MIT — používáš na vlastní riziko. Autoři nenesou odpovědnost za obchodní ztráty.
