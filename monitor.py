#!/usr/bin/env python3
"""Monitor prezzi: avvisa su Telegram quando il prodotto e' disponibile sotto soglia.

Due modalita' di lettura:
  http     richieste semplici, solo libreria standard (Unieuro, Amazon.it, siti con schema.org)
  browser  browser headless Playwright, per i siti che caricano i dati via JavaScript
           (MediaWorld, ricerca AliExpress). Playwright e' opzionale: serve solo per queste fonti.

Uso:
  python3 monitor.py --check               # mostra cosa legge da ogni sito (nessuna notifica, nessuno stato)
  python3 monitor.py --dry-run             # logica completa, ma stampa i messaggi invece di inviarli
  python3 monitor.py --only http|browser   # esegue solo le fonti di una modalita' (stato in state-<modalita>.json)
  python3 monitor.py --test-telegram       # invia un messaggio di prova
  python3 monitor.py                       # esecuzione normale

Variabili d'ambiente per Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Il token non va mai scritto nel codice o nei file di configurazione.

Lo script legge solo pagine prodotto pubbliche. Se un sito mostra un CAPTCHA o blocca
la richiesta, lo segnala come "blocked" e NON tenta di aggirare la protezione.
"""
from __future__ import annotations

import argparse
import gzip
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

MODES = {
    "unieuro": "http",
    "amazon": "http",
    "jsonld": "http",
    "mediaworld": "browser",
    "aliexpress_search": "browser",
}

# Estrazioni eseguite dentro la pagina renderizzata (Playwright / browser).
MEDIAWORLD_JS = """(() => {
  const q = s => [...document.querySelectorAll(s)].slice(0, 3).map(e => e.innerText.replace(/\\s+/g, ' ').trim().slice(0, 200));
  return {
    h1: q('h1'),
    price: q('[data-test="mms-product-price"]'),
    avail: q('[data-test="mms-pdp-details-availability"]'),
    cofr: [...document.querySelectorAll('[data-test^="mms-cofr-delivery"]')].map(e => e.getAttribute('data-test')),
  };
})()"""

ALI_SEARCH_JS = """(() => {
  const seen = new Set(), out = [];
  for (const a of document.querySelectorAll('a[href*="/item/"]')) {
    const m = a.href.match(/\\/item\\/(\\d+)\\.html/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    out.push({id: m[1], url: 'https://it.aliexpress.com/item/' + m[1] + '.html', text: a.innerText});
  }
  return out;
})()"""

ALI_ITEM_JS = "({text: document.body.innerText.slice(0, 80000)})"


# --------------------------------------------------------------------------- modello

@dataclass
class Parsed:
    name: str
    price: Optional[float]
    in_stock: bool
    note: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Offer:
    source: str
    url: str
    status: str  # ok | blocked | gone | error | parse_error | mismatch
    price: Optional[float] = None
    in_stock: Optional[bool] = None
    name: str = ""
    detail: str = ""
    item: str = ""  # id annuncio, per le fonti di ricerca
    note: str = ""  # info da mostrare nell'avviso (es. dati venditore)

    @property
    def key(self) -> str:
        return f"{self.source}#{self.item}" if self.item else self.source


@dataclass
class SourceResult:
    name: str
    url: str
    status: str
    detail: str = ""
    offers: list = field(default_factory=list)
    notes: list = field(default_factory=list)


class FetchError(Exception):
    pass


class RenderError(Exception):
    pass


class TelegramError(Exception):
    pass


# --------------------------------------------------------------------------- rete (http)

def _decode(raw: bytes, encoding: str, charset: str) -> str:
    encoding = (encoding or "").lower()
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode(charset or "utf-8", "replace")


def fetch(url: str, timeout: int = 25, retries: int = 2) -> tuple[int, str]:
    """Ritorna (status_http, testo). Gli errori HTTP non sollevano eccezioni."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = _decode(
                    resp.read(),
                    resp.headers.get("Content-Encoding", ""),
                    resp.headers.get_content_charset() or "utf-8",
                )
                return resp.status, text
        except urllib.error.HTTPError as e:
            try:
                body = _decode(e.read(), e.headers.get("Content-Encoding", ""), "utf-8")
            except Exception:
                body = ""
            return e.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise FetchError(type(last).__name__ if last else "errore di rete")


# --------------------------------------------------------------------------- rete (browser)

class BrowserRenderer:
    """Browser headless Playwright, avviato solo se serve davvero."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None

    def __enter__(self) -> "BrowserRenderer":
        return self

    def __exit__(self, *exc: Any) -> None:
        for obj in (self._ctx, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def _start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RenderError("Playwright non installato (pip install -r requirements-browser.txt && playwright install chromium)") from None
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._ctx = self._browser.new_context(
                user_agent=USER_AGENT, locale="it-IT", timezone_id="Europe/Rome",
                viewport={"width": 1366, "height": 900},
            )
        except Exception as e:
            raise RenderError(f"browser non avviabile: {type(e).__name__}") from None

    def get(self, url: str, js: str, wait_ms: int = 3500, scroll: bool = False) -> tuple[int, Any]:
        if self._ctx is None:
            self._start()
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(wait_ms)
            if scroll:
                for _ in range(3):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(1200)
            return (resp.status if resp else 0), page.evaluate(js)
        except Exception as e:
            raise RenderError(f"{type(e).__name__} durante il rendering") from None
        finally:
            try:
                page.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- parsing comune

def parse_price(value: Any) -> Optional[float]:
    """'549,90 EUR' -> 549.9 ; '1.079,00' -> 1079.0 ; '1.979' -> 1979.0 ; 549.9 -> 549.9"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    t = re.sub(r"[^\d.,]", "", str(value))
    if not t:
        return None
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    elif "." in t:
        head, tail = t.split(".")
        if len(tail) == 3 and 1 <= len(head) <= 3:  # "1.079" = migliaia all'italiana
            t = head + tail
    try:
        return float(t)
    except ValueError:
        return None


def _clean(fragment: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ").strip()


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def fmt_eur(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def title_reject_reason(title: str, cfg: dict, src: dict) -> Optional[str]:
    require = src.get("require_patterns", cfg.get("require_patterns", []))
    reject = list(cfg.get("reject_patterns", [])) + list(src.get("reject_patterns", []))
    if not all(re.search(p, title, re.I) for p in require):
        return "il nome non corrisponde al prodotto cercato"
    if any(re.search(p, title, re.I) for p in reject):
        return "prodotto escluso (usato, ricondizionato, accessorio o clone)"
    return None


# --------------------------------------------------------------------------- parser: schema.org

def _iter_jsonld(page: str) -> Iterator[Any]:
    pattern = r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    for m in re.finditer(pattern, page, re.S | re.I):
        try:
            yield json.loads(m.group(1).strip(), strict=False)
        except ValueError:
            continue


def _is_type(node: dict, wanted: str) -> bool:
    t = node.get("@type")
    return wanted in t if isinstance(t, list) else t == wanted


def parse_jsonld(page: str) -> Optional[Parsed]:
    """Cerca un Product schema.org con offerta."""
    for doc in _iter_jsonld(page):
        for node in _walk(doc):
            if not _is_type(node, "Product"):
                continue
            offers = node.get("offers")
            raw_list = offers if isinstance(offers, list) else ([offers] if offers else [])
            candidates: list[tuple[float, bool]] = []
            for off in raw_list:
                if not isinstance(off, dict):
                    continue
                nested = off.get("offers") if isinstance(off.get("offers"), list) else []
                for o in [off] + [n for n in nested if isinstance(n, dict)]:
                    raw_price = o.get("price") if o.get("price") is not None else o.get("lowPrice")
                    price = parse_price(raw_price)
                    if price is None:
                        continue
                    avail = str(o.get("availability", ""))
                    candidates.append((price, bool(re.search(r"InStock|LimitedAvailability|OnlineOnly", avail))))
            if candidates:
                available = [c for c in candidates if c[1]]
                price, in_stock = min(available or candidates)
                return Parsed(str(node.get("name", "")), price, in_stock)
    return None


# --------------------------------------------------------------------------- parser: Unieuro

def parse_unieuro(page: str, sku: str) -> Optional[Parsed]:
    """Unieuro e' un'app Angular: prezzo e stock stanno nel JSON 'ng-state' della pagina."""
    m = re.search(r'<script id="ng-state" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
    except ValueError:
        return None
    for node in _walk(state):
        if node.get("code") == sku and isinstance(node.get("price"), dict) and isinstance(node.get("stock"), dict):
            price = parse_price(node["price"].get("value"))
            status = str(node["stock"].get("stockLevelStatus", ""))
            return Parsed(str(node.get("name", "")), price, status in ("inStock", "lowStock"))
    return None


# --------------------------------------------------------------------------- parser: Amazon

_AMAZON_BLOCK = re.compile(
    r"api-services-support@amazon|Inserisci i caratteri|Enter the characters you see|Robot Check|/errors/validateCaptcha",
    re.I,
)


def parse_amazon(page: str):
    """Ritorna 'blocked', None oppure Parsed.

    Verificato sulla pagina reale del 12/11/2026: il prezzo sta in #apex-pricetopay-accessibility-label
    (il campo a-offscreen e' vuoto) e il pulsante e' 'add-to-cart-button' per il nuovo, 'add-to-cart-button-ubb'
    per l'usato. Per non confondere nuovo e usato si guarda il pulsante, non il testo della pagina.
    """
    if "productTitle" not in page and _AMAZON_BLOCK.search(page):
        return "blocked"
    title = re.search(r'id="productTitle"[^>]*>(.*?)</span>', page, re.S)
    name = _clean(title.group(1)) if title else ""
    pm = re.search(r'id="apex-pricetopay-accessibility-label"[^>]*>\s*([^<]+?)\s*<', page)
    price = parse_price(pm.group(1)) if pm else None
    if price is None:
        i = page.find('id="corePriceDisplay_desktop_feature_div"')
        area = page[i : i + 6000] if i != -1 else ""
        w = re.search(r'class="a-price-whole">\s*([\d.]+)', area)
        f = re.search(r'class="a-price-fraction">\s*(\d+)', area)
        if w:
            price = parse_price(w.group(1).rstrip(".") + "," + (f.group(1) if f else "00"))
    if not name and price is None:
        return None
    new_button = 'id="add-to-cart-button"' in page
    used_button = 'id="add-to-cart-button-ubb"' in page
    if used_button and not new_button:
        note = f"solo usato ({fmt_eur(price)})" if price is not None else "solo usato"
        return Parsed(name, price, False, note, {"used_only": True})
    return Parsed(name, price, bool(new_button and price is not None))


# --------------------------------------------------------------------------- parser: MediaWorld (renderizzato)

def parse_mediaworld(data: Any) -> Optional[Parsed]:
    """Dati estratti dalla pagina renderizzata (vedi MEDIAWORLD_JS).

    Stato 'non disponibile' verificato su pagine reali il 12/11/2026. Lo stato 'disponibile' NON e' stato
    potuto osservare: si considera disponibile quando manca NOT_AVAILABLE e il prezzo e' valorizzato.
    """
    if not isinstance(data, dict):
        return None
    h1 = next((x for x in data.get("h1", []) if x), "")
    if not h1:
        return None
    prices = [parse_price(m) for txt in data.get("price", []) for m in re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}\s*€", txt)]
    prices = [p for p in prices if p is not None]
    price = min(prices) if prices else None  # il prezzo barrato (storico) e' sempre piu' alto di quello attuale
    states = data.get("cofr", [])
    unavailable = any("NOT_AVAILABLE" in s for s in states) or any("non disponibile" in a.lower() for a in data.get("avail", []))
    if unavailable:
        return Parsed(h1, price, False, "non disponibile", {"unavailable": True})
    return Parsed(h1, price, price is not None)


# --------------------------------------------------------------------------- parser: AliExpress (renderizzato)

def parse_ali_card(text: str) -> Optional[dict]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    price = None
    for ln in lines[1:]:
        m = re.fullmatch(r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*€", ln)
        if m:
            price = parse_price(m.group(1))
            break
    return {"title": lines[0], "price": price}


def _count(s: Optional[str]) -> Optional[int]:
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else None


def parse_ali_item(text: str) -> dict:
    """Dati venditore e disponibilita' dal testo di una pagina articolo AliExpress."""
    t = text or ""
    m = re.search(r"Venduto da\s+(.+?),\s*processato da", t) or re.search(r"Venduto da\s*\n\s*([^\n]+)", t)
    seller = m.group(1).strip() if m else None
    m = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*%\s*Recensioni positive\s*\|\s*([\d.,]+)\s*Seguiti", t)
    store_pct = float(m.group(1).replace(",", ".")) if m else None
    followers = _count(m.group(2)) if m else None
    rating = reviews = None
    m = re.search(r"(\d(?:[.,]\d)?)\s*\n+\s*([\d.,]+)\s+Recension[ei]\b", t)
    if m:
        rating, reviews = float(m.group(1).replace(",", ".")), _count(m.group(2))
    m = re.search(r"([\d.,]+\+?)\s+venduti", t)
    sold = _count(m.group(1)) if m else None
    m = re.search(r"Spedizione da\s+([^\n]+)", t)
    ship = m.group(1).strip()[:40] if m else None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€", t)
    price = parse_price(m.group(1)) if m else None
    return {
        "seller": seller,
        "store_pct": store_pct,
        "followers": followers,
        "rating": rating,
        "reviews": reviews,
        "sold": sold,
        "ship_from": ship,
        "price": price,
        "no_import_fees": bool(re.search(r"Nessun costo di importazione", t)),
        "unavailable": bool(re.search(r"non è più disponibile|non può essere spedito", t, re.I)),
    }


def seller_reasons(item: dict, f: dict) -> list[str]:
    """Motivi per cui il venditore NON passa il filtro di affidabilita'. Lista vuota = ok.

    Un dato mancante conta come non superato: meglio un falso negativo che fidarsi alla cieca.
    """
    reasons: list[str] = []

    def need(label: str, value: Any, minimum: Any) -> None:
        if minimum is None:
            return
        if value is None:
            reasons.append(f"{label}: dato non trovato")
        elif value < minimum:
            reasons.append(f"{label} {value:g} < {minimum:g}")

    need("recensioni positive negozio %", item.get("store_pct"), f.get("min_store_positive_pct"))
    need("follower negozio", item.get("followers"), f.get("min_store_followers"))
    need("voto articolo", item.get("rating"), f.get("min_item_rating"))
    need("recensioni articolo", item.get("reviews"), f.get("min_item_reviews"))
    need("venduti", item.get("sold"), f.get("min_item_sold"))
    allowed = f.get("allowed_ship_from")
    if allowed:
        ship = item.get("ship_from")
        if not ship:
            reasons.append("paese di spedizione non trovato")
        elif not any(a.lower() in ship.lower() for a in allowed):
            reasons.append(f"spedito da {ship}")
    if f.get("require_no_import_fees") and not item.get("no_import_fees"):
        reasons.append("possibili oneri di importazione")
    return reasons


def ali_summary(item: dict) -> str:
    def v(x: Any, suffix: str = "") -> str:
        return "?" if x is None else f"{x}{suffix}"

    return (
        f"Venditore: {v(item.get('seller'))} · {v(item.get('store_pct'), '%')} recensioni positive · "
        f"{v(item.get('followers'))} follower · articolo {v(item.get('rating'), '★')} "
        f"({v(item.get('reviews'))} recensioni, {v(item.get('sold'))} venduti) · spedizione da {v(item.get('ship_from'))}. "
        "Il prezzo e' quello di listino: eventuali coupon si applicano al checkout."
    )


# --------------------------------------------------------------------------- controllo delle fonti

def _http_status_result(src: dict, status: int) -> Optional[SourceResult]:
    name, url = src["name"], src["url"]
    if status in (404, 410):
        return SourceResult(name, url, "gone", f"HTTP {status}: pagina non trovata")
    if status in (401, 403, 429, 503):
        return SourceResult(name, url, "blocked", f"HTTP {status}")
    if status >= 400:
        return SourceResult(name, url, "error", f"HTTP {status}")
    return None


def _single_result(src: dict, cfg: dict, parsed: Optional[Parsed]) -> SourceResult:
    name, url = src["name"], src["url"]
    if parsed is None:
        return SourceResult(name, url, "parse_error", "struttura della pagina non riconosciuta (sito cambiato o pagina anti-bot)")
    reason = title_reject_reason(parsed.name, cfg, src) if parsed.name else None
    if reason:
        return SourceResult(name, url, "mismatch", f"{reason}: {parsed.name[:70]}")
    if parsed.price is None and not parsed.extra.get("unavailable"):
        return SourceResult(name, url, "parse_error", "prezzo non trovato")
    offer = Offer(name, url, "ok", parsed.price, parsed.in_stock, parsed.name, detail=parsed.note)
    return SourceResult(name, url, "ok", offers=[offer])


def check_http_source(src: dict, cfg: dict) -> SourceResult:
    name, url, kind = src["name"], src["url"], src.get("type", "jsonld")
    try:
        status, page = fetch(url)
    except FetchError as e:
        return SourceResult(name, url, "error", f"rete: {e}")
    bad = _http_status_result(src, status)
    if bad:
        return bad
    if kind == "unieuro":
        parsed = parse_unieuro(page, src["sku"])
    elif kind == "amazon":
        parsed = parse_amazon(page)
        if parsed == "blocked":
            return SourceResult(name, url, "blocked", "pagina anti-bot / CAPTCHA")
    else:
        parsed = parse_jsonld(page)
    return _single_result(src, cfg, parsed)


def check_mediaworld(src: dict, cfg: dict, renderer: Any) -> SourceResult:
    name, url = src["name"], src["url"]
    try:
        status, data = renderer.get(url, MEDIAWORLD_JS)
    except RenderError as e:
        return SourceResult(name, url, "error", str(e))
    bad = _http_status_result(src, status)
    if bad:
        return bad
    return _single_result(src, cfg, parse_mediaworld(data))


def check_aliexpress_search(src: dict, cfg: dict, renderer: Any) -> SourceResult:
    name, url = src["name"], src["url"]
    try:
        status, cards = renderer.get(url, ALI_SEARCH_JS, scroll=True)
    except RenderError as e:
        return SourceResult(name, url, "error", str(e))
    bad = _http_status_result(src, status)
    if bad:
        return bad
    if not isinstance(cards, list) or not cards:
        return SourceResult(name, url, "parse_error", "nessuna scheda prodotto letta (pagina anti-bot o struttura cambiata)")

    res = SourceResult(name, url, "ok")
    max_price = float(cfg["max_price"])
    candidates: list[tuple[dict, dict]] = []
    skipped_title = 0
    for c in cards:
        card = parse_ali_card(c.get("text", ""))
        if not card:
            continue
        if title_reject_reason(card["title"], cfg, src):
            skipped_title += 1
            if re.search(r"mac\s*mini", card["title"], re.I) and re.search(r"apple", card["title"], re.I) and len(res.notes) < 8:
                res.notes.append(f"scartata dal titolo: {card['title'][:90]} ({fmt_eur(card['price']) if card['price'] else '?'})")
            continue
        if card["price"] is None or card["price"] > max_price:
            res.notes.append(f"scartata per prezzo ({fmt_eur(card['price']) if card['price'] else '?'}): {card['title'][:60]}")
            continue
        candidates.append((c, card))
    res.notes.insert(0, f"{len(cards)} schede lette: {skipped_title} non corrispondono (accessori, cloni, altra configurazione), {len(candidates)} candidate sotto soglia")

    limit = int(src.get("max_detail_pages", 5))
    for c, card in candidates[:limit]:
        try:
            _, data = renderer.get(c["url"], ALI_ITEM_JS, wait_ms=4000)
        except RenderError as e:
            res.notes.append(f"{c['id']}: pagina non letta ({e})")
            continue
        item = parse_ali_item((data or {}).get("text", ""))
        price = item["price"] if item["price"] is not None else card["price"]
        reasons = seller_reasons(item, cfg.get("seller_filter", {}))
        if item["unavailable"]:
            in_stock, detail = False, "non disponibile o non spedibile"
        elif reasons:
            in_stock, detail = False, "venditore scartato: " + "; ".join(reasons)
        else:
            in_stock, detail = True, ""
        res.offers.append(Offer(name, c["url"], "ok", price, in_stock, card["title"], detail, item=c["id"], note=ali_summary(item)))
    return res


def check_source(src: dict, cfg: dict, renderer: Any = None) -> SourceResult:
    kind = src.get("type", "jsonld")
    if kind == "aliexpress_search":
        return check_aliexpress_search(src, cfg, renderer)
    if kind == "mediaworld":
        return check_mediaworld(src, cfg, renderer)
    return check_http_source(src, cfg)


# --------------------------------------------------------------------------- logica avvisi

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def evaluate_health(prev: dict, res: SourceResult, cfg: dict, now: datetime) -> tuple[dict, list[str]]:
    """Avvisa una volta sola se un sito resta illeggibile a lungo, e quando torna a funzionare."""
    st = dict(prev)
    msgs: list[str] = []
    if res.status != "ok":
        if not st.get("fail_since"):
            st["fail_since"] = _iso(now)
            st["fail_alerted"] = False
        else:
            hours = (now - _parse_iso(st["fail_since"])).total_seconds() / 3600
            if hours >= cfg.get("fail_alert_hours", 6) and not st.get("fail_alerted"):
                msgs.append(
                    f"⚠️ {res.name}: non riesco a leggere la pagina da circa {hours:.0f} ore "
                    f"({res.status}: {res.detail}). Su questo sito il monitor e' cieco.\n{res.url}"
                )
                st["fail_alerted"] = True
        return st, msgs
    if st.get("fail_alerted"):
        msgs.append(f"✅ {res.name}: lettura della pagina ripristinata.")
    st.pop("fail_since", None)
    st.pop("fail_alerted", None)
    return st, msgs


def evaluate_offer(prev: dict, offer: Offer, cfg: dict, now: datetime) -> tuple[dict, list[str]]:
    """Decide se avvisare. Lo stato cambia solo quando serve, cosi' non si fanno commit a vuoto."""
    st = dict(prev)
    msgs: list[str] = []
    max_price = float(cfg["max_price"])
    deal = bool(offer.in_stock) and offer.price is not None and offer.price <= max_price
    if deal:
        was_deal = bool(st.get("deal"))
        last_price = st.get("last_alert_price")
        last_ts = st.get("last_alert_ts")
        cooldown_h = cfg.get("realert_cooldown_hours", 3)
        cooldown_ok = last_ts is None or (now - _parse_iso(last_ts)).total_seconds() / 3600 >= cooldown_h
        price_drop = last_price is not None and offer.price <= float(last_price) - cfg.get("realert_price_drop", 5)
        if (not was_deal and cooldown_ok) or price_drop:
            lines = [
                f"🟢 {cfg.get('product', 'Prodotto')}: disponibile sotto soglia!",
                f"{offer.source}: {fmt_eur(offer.price)} (soglia {fmt_eur(max_price)})",
            ]
            if offer.note:
                lines.append(offer.note)
            lines.append(offer.url)
            msgs.append("\n".join(lines))
            st["last_alert_price"] = offer.price
            st["last_alert_ts"] = _iso(now)
        st["deal"] = True
    else:
        st["deal"] = False
    st["last_price"] = offer.price
    st["last_in_stock"] = offer.in_stock
    return st, msgs


def process(state: dict, results: list, cfg: dict, now: datetime) -> tuple[dict, list[tuple[str, str, str]]]:
    """Ritorna (nuovo_stato, [(bucket, chiave, testo)]). Gli annunci spariti da una ricerca perdono lo stato 'deal'."""
    new = {"sources": dict(state.get("sources", {})), "offers": dict(state.get("offers", {}))}
    pending: list[tuple[str, str, str]] = []
    for res in results:
        sst, smsgs = evaluate_health(new["sources"].get(res.name, {}), res, cfg, now)
        new["sources"][res.name] = sst
        pending += [("sources", res.name, m) for m in smsgs]
        if res.status != "ok":
            continue
        seen = set()
        for off in res.offers:
            seen.add(off.key)
            ost, omsgs = evaluate_offer(new["offers"].get(off.key, {}), off, cfg, now)
            ost["source"] = res.name
            new["offers"][off.key] = ost
            pending += [("offers", off.key, m) for m in omsgs]
        for key, ost in list(new["offers"].items()):
            if ost.get("source") == res.name and key not in seen and ost.get("deal"):
                new["offers"][key] = {**ost, "deal": False}
    return new, pending


# --------------------------------------------------------------------------- Telegram

def telegram_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise TelegramError("mancano TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        reason = ""
        try:
            reason = str(json.loads(e.read().decode("utf-8", "replace")).get("description", ""))[:150]
        except Exception:
            pass
        if token and token in reason:
            reason = ""
        raise TelegramError(f"HTTP {e.code} (controlla token e chat id){': ' + reason if reason else ''}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise TelegramError("errore di rete verso Telegram") from None
    if not body.get("ok"):
        raise TelegramError("Telegram ha rifiutato il messaggio")


# --------------------------------------------------------------------------- main

def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def print_result(r: SourceResult) -> None:
    print(f"[{r.status:11}] {r.name}" + (f" | {r.detail}" if r.detail else ""))
    for o in r.offers:
        stock = {True: "DISPONIBILE", False: "non disponibile", None: "?"}[o.in_stock]
        price = fmt_eur(o.price) if o.price is not None else "-"
        extra = f" | {o.detail}" if o.detail else ""
        print(f"{'':14}{price:>12}  {stock}{extra}")
        if o.name:
            print(f"{'':14}{o.name[:90]}")
    for n in r.notes:
        print(f"{'':14}· {n}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Monitor prezzi con notifica Telegram")
    ap.add_argument("--check", action="store_true", help="mostra cosa legge da ogni sito e termina")
    ap.add_argument("--dry-run", action="store_true", help="non invia messaggi e non salva lo stato")
    ap.add_argument("--only", choices=["http", "browser"], help="esegue solo le fonti di una modalita'")
    ap.add_argument("--test-telegram", action="store_true", help="invia un messaggio di prova")
    args = ap.parse_args(argv)

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("config.json non trovato", file=sys.stderr)
        return 2

    if args.test_telegram:
        try:
            telegram_send(f"✅ Test del monitor prezzi: {cfg.get('product', '')} (soglia {fmt_eur(float(cfg['max_price']))}).")
        except TelegramError as e:
            print(f"Telegram: {e}", file=sys.stderr)
            return 1
        print("Messaggio di prova inviato.")
        return 0

    sources = [s for s in cfg["sources"] if not args.only or MODES.get(s.get("type", "jsonld"), "http") == args.only]
    results = []
    with BrowserRenderer() as renderer:
        for src in sources:
            results.append(check_source(src, cfg, renderer))
            time.sleep(1.5)
    for r in results:
        print_result(r)
    if args.check:
        return 0

    state_path = ROOT / f"state-{args.only}.json" if args.only else STATE_PATH
    state = load_json(state_path, {})
    if "sources" not in state and "offers" not in state:
        state = {"sources": {}, "offers": {}}  # formato vecchio o file vuoto
    now = datetime.now(timezone.utc)
    new_state, pending = process(state, results, cfg, now)

    exit_code = 0
    failed: set[tuple[str, str]] = set()
    for bucket, key, text in pending:
        if args.dry_run:
            print(f"--- [dry-run] messaggio Telegram ---\n{text}\n---")
            continue
        try:
            telegram_send(text)
            print(f"Notifica inviata ({key}).")
        except TelegramError as e:
            print(f"ERRORE Telegram ({key}): {e}", file=sys.stderr)
            failed.add((bucket, key))
            exit_code = 1
    # se l'invio fallisce, lo stato di quella voce resta quello vecchio: al prossimo giro riprova
    for bucket, key in failed:
        if key in state.get(bucket, {}):
            new_state[bucket][key] = state[bucket][key]
        else:
            new_state[bucket].pop(key, None)

    if not args.dry_run and new_state != state:
        state_path.write_text(json.dumps(new_state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
