# Monitor prezzi Mac mini M4 (16 GB / 256 GB) → Telegram

Ti scrive su Telegram quando il Mac mini M4 16/256 **nuovo** è **disponibile** a **600 € o meno** su Unieuro o AliExpress. Gira su GitHub Actions: Unieuro ogni 15 minuti, AliExpress (che richiede un browser) ogni ora. Amazon.it e MediaWorld bloccano gli IP di GitHub (CAPTCHA / 403): MediaWorld e' disattivato, Amazon e' ancora in config ma senza avvisi di errore (`alert_on_failure: false`) e va seguito con un alert di prezzo esterno (es. Keepa, ASIN B0DLCVRCJS).

## Cosa fa e cosa no

- Avvisa quando un'offerta passa da "non disponibile" a "disponibile ≤ 600 €", e di nuovo se il prezzo scende di almeno 5 €. Non ripete lo stesso avviso a ogni controllo.
- Esclude usato e ricondizionato e controlla che il nome sia davvero Mac mini / M4 / 16 GB / 256 GB. Su Amazon distingue l'offerta **nuova** da quella **usata** guardando il pulsante di acquisto, non il testo.
- Se un sito non è leggibile per 6 ore (blocco anti-bot, pagina cambiata) ti manda **un** avviso, così non resti cieco senza saperlo.
- **Non compra nulla** e non aggira CAPTCHA o protezioni: se un sito blocca, lo segnala e basta.

## Filtro affidabilità venditore (AliExpress)

Il monitor cerca su AliExpress, scarta titoli sospetti (Intel/Ryzen "M4", M4 Pro, docking, accessori), apre al massimo 5 schede sotto i 600 € e avvisa **solo** se il venditore supera tutte le soglie in `config.json → seller_filter`:

| Soglia | Default |
|---|---|
| recensioni positive del negozio | ≥ 95 % |
| follower del negozio | ≥ 100 |
| voto dell'articolo | ≥ 4,7 |
| recensioni dell'articolo | ≥ 30 |
| pezzi venduti | ≥ 50 |
| spedizione da | UE (Spagna, Francia, Germania, Italia, Polonia…) |
| oneri di importazione | "Nessun costo di importazione" richiesto |

Un dato non trovato conta come soglia **non superata**. Metti `null` a una soglia per disattivarla. Il messaggio Telegram riporta i dati del venditore.
Limite: AliExpress mostra spesso prezzi con coupon applicati solo al checkout; il monitor legge il prezzo di listino, quindi un'offerta con coupon può sfuggire. Un prezzo troppo basso per un Mac mini nuovo (sotto ~450 €) va comunque trattato con sospetto.

## Stato dei negozi (verificato il 12/11/2026 con il browser)

| Negozio | Lettura | Verifica |
|---|---|---|
| Unieuro | JSON `ng-state` della pagina | **verificato** su pagina reale (esaurito, 549,90 €) |
| Amazon.it | HTML della pagina | struttura verificata col browser; **su GitHub risponde con CAPTCHA** (avvisi di errore disattivati) |
| MediaWorld | browser | **disattivato**: su GitHub risponde 403 (parser ancora nel codice e nei test) |
| AliExpress | browser, ricerca + scheda | oggi **nessun annuncio valido** (solo M4 Pro e mini PC Intel/Ryzen). Testo delle schede ricostruito dai pattern osservati |

Al primo giro guarda il log: `[ok]`, `[blocked]` o `[parse_error]` dice quali siti funzionano davvero. Il sito che blocca si può far girare dal Mac (Piano B).

## Installazione (circa 10 minuti)

### 1. Crea il bot Telegram (lo fai tu: il token è un segreto)

1. Su Telegram apri **@BotFather** → `/newbot` → scegli nome e username → copia il **token**.
2. Apri una chat col tuo nuovo bot e scrivigli "ciao".
3. Nel browser apri `https://api.telegram.org/bot<IL_TUO_TOKEN>/getUpdates` e cerca `"chat":{"id":123456789`. Quel numero è il **chat id**.

Non incollare il token in chat con nessuno, nemmeno con me.

### 2. Crea il repository GitHub

- **Pubblico (consigliato)**: i minuti di Actions sono gratuiti e illimitati. Nel repo ci sono solo URL e soglia, il token sta nei Secrets. Se resta senza attività per 60 giorni GitHub disattiva i workflow programmati.
- **Privato**: i minuti gratuiti sono limitati. Con un controllo ogni 15 minuti si superano probabilmente: i controlli con browser (installano Chromium a ogni giro, ~2 minuti) consumano molto: in `.github/workflows/monitor.yml` porta i cron a `"*/30 * * * *"` (http) e `"7 */3 * * *"` (browser), e aggiorna le due condizioni `github.event.schedule` di conseguenza. Controlla i minuti inclusi nel tuo piano.

```bash
cd mac-mini-monitor
git init -b main
git add .
git commit -m "Monitor prezzi Mac mini"
git remote add origin https://github.com/<TUO_UTENTE>/mac-mini-monitor.git
git push -u origin main
```

Attenzione: la cartella `.github` è nascosta in Finder (Cmd+Maiusc+.), ma `git add .` la include.

### 3. Aggiungi i segreti

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

- `TELEGRAM_BOT_TOKEN` = il token di BotFather
- `TELEGRAM_CHAT_ID` = il numero trovato al punto 1

### 4. Prova

1. Tab **Actions** → "Mac mini price monitor" → **Run workflow** con `telegram_test` spuntato → deve arrivarti il messaggio su Telegram.
2. Rilancia **Run workflow** senza spunta → partono due job (`http` e `browser`): nei log vedi una riga per negozio con prezzo e disponibilità.

Da qui in poi parte da solo.

## Piano B: farlo girare sul tuo Mac

Se nel log vedi `[blocked]` su quasi tutti i siti, GitHub Actions è bloccato (succede con gli IP dei datacenter). Lo stesso script funziona da casa:

```bash
export TELEGRAM_BOT_TOKEN="..."   # meglio in un file .env fuori dal repo
export TELEGRAM_CHAT_ID="..."
pip install -r requirements-browser.txt && playwright install chromium   # solo per MediaWorld/AliExpress
python3 monitor.py --check        # mostra cosa legge, senza notifiche
python3 monitor.py                # esecuzione completa (stato in state.json)
# ogni 15 minuti, finché il Mac è acceso:  crontab -e
# */15 * * * * cd /percorso/mac-mini-monitor && TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python3 monitor.py
```

## Personalizzare

Tutto in `config.json`:

- `max_price`: la soglia (600).
- `sources`: aggiungi o togli negozi. `type`: `unieuro` (serve `sku`), `amazon`, `mediaworld`, `aliexpress_search` (URL di una ricerca), oppure `jsonld` (siti che espongono dati schema.org del prodotto; non verificato). Yeppon, presente nella prima versione, è stato tolto perché la sua struttura non è verificata: si può riaggiungere come `jsonld`.
- `seller_filter`: soglie del filtro venditore AliExpress.
- `fail_alert_hours`, `realert_price_drop`, `realert_cooldown_hours`: frequenza degli avvisi.

Per un'altra configurazione (es. 512 GB) cambia `require_patterns` e gli URL.

## Quando hai comprato

Disattiva il workflow (Actions → Mac mini price monitor): **Actions → Mac mini price monitor → ⋯ → Disable workflow**.

## Test

```bash
python3 -I -m unittest discover -s tests -v
```

Unieuro, Amazon (usato) e MediaWorld (non disponibile) usano strutture reali catturate il 12/11/2026; i file `*_RECONSTRUCTED` e i testi AliExpress sono ricostruiti: verificano la logica, non il formato reale.

## Fase 2: weekend da Bari (voli diretti + hotel)

Cerca ogni giorno i voli **diretti** andata/ritorno **venerdì → domenica** da Bari verso le capitali europee (Roma e le città italiane escluse) per i **prossimi 8 weekend**, poi cerca l'hotel per le offerte migliori.

| Criterio | Valore (in `travel_config.json`) |
|---|---|
| Volo | ≤ 100 € a persona, andata e ritorno, solo diretti |
| Hotel | ≤ 80 €/notte, voto Booking ≥ 8,5, ≤ 1 km dal centro, 2 notti, 2 adulti, 1 camera |
| Avvisi Telegram | offerta volo sotto soglia (nuova, o ribasso di almeno 5 €) con l'hotel migliore se c'è |
| Riepilogo | ogni lunedì: top 10 combinazioni per costo totale |
| Dashboard | `docs/index.html` (tabella con filtri, ordinamento e andamento prezzi) |

**Fonti e costi** (chiavi come secret GitHub, mai nel codice):
- Voli: [Ignav](https://ignav.com) (`IGNAV_API_KEY`), 1.000 richieste gratis poi 2 $ ogni 1.000 (dati dal sito Ignav, da verificare in fase di registrazione). Una scansione completa usa fino a ~250 richieste la prima volta; le rotte senza voli diretti vengono sospese per 14 giorni dopo 3 scansioni vuote. Tetto di sicurezza: `limits.max_requests_per_run`.
- Hotel: [SerpApi](https://serpapi.com), motore `booking` (`SERPAPI_API_KEY`), 250 ricerche gratis al mese. Il voto è quello Booking in scala 10; il filtro "1 km dal centro" usa le coordinate della struttura e del centro città. Al massimo 8 ricerche hotel per scansione (~240/mese).
- Il sistema non prenota: mostra solo link a Google Flights e Booking.

**Attivazione** (una volta sola):
1. Registrati su Ignav e SerpApi e crea i secret `IGNAV_API_KEY` e `SERPAPI_API_KEY` (Settings → Secrets and variables → Actions).
2. Actions → "Weekend da Bari (voli + hotel)" → Run workflow con `mode = check`: fa 1 richiesta volo + 1 hotel e stampa valuta, itinerari e hotel trovati.
3. Se tutto torna (valuta EUR), lancia `mode = scan`: scrive `docs/data.json` e invia gli avvisi.
4. Dashboard: Settings → Pages → Deploy from a branch → `main` / `/docs`.

**Non verificato** (le API non erano raggiungibili dal mio ambiente, i test usano risposte ricostruite dagli schemi documentati):
- valuta restituita da Ignav (il campione di documentazione mostra USD): se non è EUR gli avvisi restano disattivati e il `check` lo segnala;
- se il prezzo Ignav è per persona (si richiede `adults: 1` per evitare l'ambiguità);
- coordinate di aeroporti/città sulle rotte realmente operate, e la qualità dei dati hotel di SerpApi per ogni città.

## Roadmap

- **Fase 1**: monitor del Mac mini M4 16/256 con notifica Telegram.
- **Fase 2** (in corso): weekend da Bari + hotel, con dashboard.
- **Fase 3** (futura): dashboard in cui scegliere l'oggetto da monitorare o incollare un link, con soglia di prezzo impostabile.
