#!/usr/bin/env python3
"""Weekend da Bari verso le capitali europee: voli diretti + hotel, con avvisi Telegram e dashboard.

Fonti (a pagamento/quota, chiavi come secret GitHub, mai nel codice):
  voli   Ignav     POST https://ignav.com/api/fares/round-trip      header X-Api-Key   (IGNAV_API_KEY)
  hotel  SerpApi   GET  https://serpapi.com/search?engine=booking   parametro api_key  (SERPAPI_API_KEY)

Modalita':
  --check [--route BUD]   una sola richiesta volo (+ una hotel con --with-hotel): verifica chiavi, valuta e formato
  (nessuna opzione)       scansione completa: voli per i prossimi N weekend, hotel per le offerte migliori, avvisi
  --digest                riepilogo settimanale su Telegram dai dati dell'ultima scansione (nessuna richiesta API)
  --dry-run               non invia messaggi e non salva stato/dati

Non prenota nulla: genera solo link.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from monitor import TelegramError, fmt_eur, telegram_send

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "travel_config.json"
STATE_PATH = ROOT / "travel_state.json"
DATA_PATH = ROOT / "docs" / "data.json"

IGNAV_URL = "https://ignav.com/api/fares/round-trip"
SERPAPI_URL = "https://serpapi.com/search"


class ApiError(Exception):
    """Errore di rete o API. Il testo non contiene mai chiavi o URL con chiavi."""


class BudgetExceeded(Exception):
    pass


# --------------------------------------------------------------------------- date e utilita'

def upcoming_weekends(today: date, n: int) -> list[tuple[date, date]]:
    """Prossimi n (venerdi', domenica), dal primo venerdi' successivo a oggi."""
    days_ahead = (4 - today.weekday()) % 7 or 7
    first = today + timedelta(days=days_ahead)
    return [(first + timedelta(weeks=i), first + timedelta(weeks=i, days=2)) for i in range(n)]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:[.,]\d+)?", str(value))
    return float(m.group(0).replace(",", ".")) if m else None


def booking_link(city: str, fri: date, sun: date, adults: int) -> str:
    q = urllib.parse.urlencode({
        "ss": city, "checkin": fri.isoformat(), "checkout": sun.isoformat(),
        "group_adults": adults, "no_rooms": 1, "selected_currency": "EUR",
    })
    return f"https://www.booking.com/searchresults.html?{q}"


def flights_link(origin: str, dest: str, fri: date, sun: date) -> str:
    q = urllib.parse.quote(f"Flights from {origin} to {dest} on {fri.isoformat()} through {sun.isoformat()}")
    return f"https://www.google.com/travel/flights?q={q}"


# --------------------------------------------------------------------------- rete

def http_json(method: str, url: str, headers: Optional[dict] = None, body: Optional[dict] = None,
              timeout: int = 40, retries: int = 2) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", "User-Agent": "analisi-prezzo/1.0", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    last = "errore di rete"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ApiError(f"HTTP {e.code}: chiave API rifiutata o non valida") from None
            if e.code == 404:
                raise ApiError("HTTP 404") from None
            if e.code == 400 or e.code == 422:
                detail = ""
                try:
                    detail = str(json.loads(e.read().decode("utf-8", "replace")))[:200]
                except Exception:
                    pass
                raise ApiError(f"HTTP {e.code} (richiesta non valida){': ' + detail if detail else ''}") from None
            last = f"HTTP {e.code}"
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
            else:
                time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError):
            last = "errore di rete"
            time.sleep(2 * (attempt + 1))
        except ValueError:
            raise ApiError("risposta non in formato JSON") from None
    raise ApiError(last)


class Budget:
    def __init__(self, limit: int) -> None:
        self.limit, self.used = limit, 0

    def spend(self) -> None:
        if self.used >= self.limit:
            raise BudgetExceeded()
        self.used += 1


# --------------------------------------------------------------------------- voli (Ignav)

def ignav_round_trip(key: str, origin: str, dest: str, dep: date, ret: date, extra: dict) -> dict:
    # adults=1: il prezzo Ignav non dichiara se e' per persona o totale; con 1 adulto e' per persona per costruzione.
    body = {"origin": origin, "destination": dest, "departure_date": dep.isoformat(),
            "return_date": ret.isoformat(), "adults": 1, "max_stops": 0, **extra}
    return http_json("POST", IGNAV_URL, headers={"X-Api-Key": key}, body=body)


def _segments(leg: Any) -> list:
    return leg.get("segments", []) if isinstance(leg, dict) else []


def parse_ignav(resp: Any) -> Optional[dict]:
    """Itinerario diretto (1 tratta all'andata e 1 al ritorno) piu' economico, o None."""
    best: Optional[dict] = None
    for it in (resp or {}).get("itineraries", []) or []:
        out, inn = _segments(it.get("outbound")), _segments(it.get("inbound"))
        if len(out) != 1 or len(inn) != 1:
            continue
        price = it.get("price") or {}
        amount = to_float(price.get("amount") if isinstance(price, dict) else price)
        if amount is None:
            continue
        cur = str(price.get("currency", "")).upper() if isinstance(price, dict) else ""
        cand = {
            "price": amount,
            "currency": cur or "?",
            "carrier": str(it.get("outbound", {}).get("carrier") or out[0].get("operating_carrier_name") or ""),
            "out_flight": f"{out[0].get('marketing_carrier_code', '')}{out[0].get('flight_number', '')}",
            "out_time": str(out[0].get("departure_time_local", ""))[:16],
            "ret_flight": f"{inn[0].get('marketing_carrier_code', '')}{inn[0].get('flight_number', '')}",
            "ret_time": str(inn[0].get("departure_time_local", ""))[:16],
            "airport": str(out[0].get("arrival_airport", "")),
        }
        if best is None or cand["price"] < best["price"]:
            best = cand
    return best


# --------------------------------------------------------------------------- hotel (SerpApi, engine booking)

def serpapi_booking(key: str, city: str, cin: date, cout: date, adults: int, currency: str, max_price: float) -> dict:
    params = {
        "engine": "booking", "q": city, "check_in_date": cin.isoformat(), "check_out_date": cout.isoformat(),
        "adults": adults, "rooms": 1, "currency": currency, "rating": 8, "max_price": int(max_price),
        "sort_by": "review_score_and_price", "limit": 40, "api_key": key,
    }
    return http_json("GET", SERPAPI_URL + "?" + urllib.parse.urlencode(params))


def _distance_text_km(text: Any) -> Optional[float]:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(km|m)\b", str(text or ""), re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v if m.group(2).lower() == "km" else v / 1000


def parse_booking(resp: Any, dest: dict, hcfg: dict, nights: int) -> list[dict]:
    """Hotel che rispettano voto minimo, prezzo massimo a notte e distanza massima dal centro."""
    out: list[dict] = []
    for p in (resp or {}).get("properties", []) or []:
        if p.get("sold_out") or p.get("availability") in ("sold_out", "Sold out"):
            continue
        rating = to_float(p.get("rating"))
        total = to_float(p.get("extracted_price"))
        if rating is None or total is None or nights <= 0:
            continue
        per_night = total / nights
        gps = p.get("gps_coordinates") or {}
        lat, lon = to_float(gps.get("latitude")), to_float(gps.get("longitude"))
        km = haversine_km(dest["lat"], dest["lon"], lat, lon) if lat is not None and lon is not None else _distance_text_km(p.get("distance"))
        if km is None:
            continue  # distanza non verificabile: scarto per prudenza
        if rating < hcfg["min_rating"] or per_night > hcfg["max_per_night"] or km > hcfg["max_km_from_centre"]:
            continue
        out.append({
            "name": str(p.get("name", ""))[:80], "rating": rating, "per_night": round(per_night, 2),
            "total": round(total, 2), "km": round(km, 2), "link": str(p.get("link", "")),
            "free_cancellation": bool(p.get("free_cancellation")),
        })
    out.sort(key=lambda h: (h["per_night"], -h["rating"]))
    return out


# --------------------------------------------------------------------------- scansione

def _active(route_state: dict, cfg: dict, today: date) -> bool:
    until = route_state.get("skip_until")
    return not until or date.fromisoformat(until) <= today


def scan_flights(cfg: dict, state: dict, key: str, budget: Budget, today: date, log=print) -> list[dict]:
    fares: list[dict] = []
    weekends = upcoming_weekends(today, cfg["weekends"])
    routes = state.setdefault("routes", {})
    for dest in cfg["destinations"]:
        for ap in dest["airports"]:
            rs = routes.setdefault(ap, {})
            if not _active(rs, cfg, today):
                continue
            found = 0
            for fri, sun in weekends:
                try:
                    budget.spend()
                    resp = ignav_round_trip(key, cfg["origin"], ap, fri, sun, cfg["flight"].get("request_extra", {}))
                except BudgetExceeded:
                    log(f"Limite richieste raggiunto ({budget.limit}): scansione interrotta.")
                    return fares
                except ApiError as e:
                    if "chiave" in str(e):
                        raise
                    log(f"{ap} {fri}: {e}")
                    continue
                best = parse_ignav(resp)
                if best:
                    found += 1
                    fares.append({"dest": dest["name"], "airport": ap, "fri": fri.isoformat(), "sun": sun.isoformat(), **best})
            if found == 0:
                rs["empty"] = rs.get("empty", 0) + 1
                if rs["empty"] >= cfg["limits"]["prune_after_empty_scans"]:
                    rs["skip_until"] = (today + timedelta(days=cfg["limits"]["prune_retry_days"])).isoformat()
                    rs["empty"] = 0
                    log(f"{ap}: nessun volo diretto per {cfg['limits']['prune_after_empty_scans']} scansioni, sospesa fino al {rs['skip_until']}")
            else:
                rs.pop("empty", None)
                rs.pop("skip_until", None)
    return fares


def pick_hotel_targets(fares: list[dict], cfg: dict) -> list[dict]:
    """Prima le offerte sotto soglia, poi le piu' economiche, una per (destinazione, weekend)."""
    ok = [f for f in fares if f["currency"] == cfg["currency"]]
    ok.sort(key=lambda f: f["price"])
    deals = [f for f in ok if f["price"] <= cfg["flight"]["max_price_per_person"]]
    chosen: list[dict] = []
    seen: set = set()
    for f in deals + ok[: cfg["hotel"]["top_n_cheapest_flights"]]:
        k = (f["dest"], f["fri"])
        if k in seen:
            continue
        seen.add(k)
        chosen.append(f)
    return chosen[: cfg["hotel"]["searches_per_run"]]


def attach_hotels(cfg: dict, fares: list[dict], key: str, budget: Budget, log=print) -> None:
    dests = {d["name"]: d for d in cfg["destinations"]}
    h = cfg["hotel"]
    for f in pick_hotel_targets(fares, cfg):
        fri, sun = date.fromisoformat(f["fri"]), date.fromisoformat(f["sun"])
        d = dests[f["dest"]]
        f["hotel_link"] = booking_link(d["name"], fri, sun, h["adults"])
        try:
            budget.spend()
            resp = serpapi_booking(key, d["name"], fri, sun, h["adults"], cfg["currency"], h["max_per_night"])
        except BudgetExceeded:
            log("Limite richieste raggiunto: hotel non cercati per le offerte rimanenti.")
            return
        except ApiError as e:
            if "chiave" in str(e):
                raise
            log(f"hotel {d['name']} {fri}: {e}")
            continue
        hotels = parse_booking(resp, d, h, h["nights"])
        f["hotel_checked"] = True
        f["hotel_count"] = len(hotels)
        if hotels:
            f["hotel"] = hotels[0]


def finalize(cfg: dict, fares: list[dict]) -> None:
    n = cfg["flight"]["adults_for_totals"]
    for f in fares:
        f["flight_total"] = round(f["price"] * n, 2)
        f["flights_link"] = flights_link(cfg["origin"], f["airport"], date.fromisoformat(f["fri"]), date.fromisoformat(f["sun"]))
        f.setdefault("hotel_link", booking_link(f["dest"], date.fromisoformat(f["fri"]), date.fromisoformat(f["sun"]), cfg["hotel"]["adults"]))
        if f.get("hotel"):
            f["trip_total"] = round(f["flight_total"] + f["hotel"]["total"], 2)


# --------------------------------------------------------------------------- avvisi e storico

def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_fare_message(cfg: dict, f: dict) -> str:
    n = cfg["flight"]["adults_for_totals"]
    fri, sun = date.fromisoformat(f["fri"]), date.fromisoformat(f["sun"])
    lines = [
        f"✈️ {f['dest']} ({f['airport']}) diretto, {fri.strftime('%a %d/%m')} → {sun.strftime('%a %d/%m')}",
        f"Volo A/R: {fmt_eur(f['price'])} a persona ({fmt_eur(f['price'] * n)} per {n})"
        + (f" · {f['carrier']}" if f.get("carrier") else ""),
    ]
    h = f.get("hotel")
    if h:
        lines.append(f"🏨 {h['name']}: {fmt_eur(h['per_night'])}/notte, voto {h['rating']:g}, {h['km']:g} km dal centro")
        if f.get("trip_total"):
            lines.append(f"Totale stimato volo + hotel per {n}: {fmt_eur(f['trip_total'])}")
    elif f.get("hotel_checked"):
        lines.append("🏨 Nessun hotel entro i tuoi criteri (voto, prezzo, distanza) per queste date.")
    lines.append(f"Voli: {f['flights_link']}")
    lines.append(f"Hotel: {f['hotel_link']}")
    return "\n".join(lines)


def build_alerts(cfg: dict, state: dict, fares: list[dict], now: datetime) -> list[tuple[str, str]]:
    """Ritorna [(chiave_stato, testo)]. Avvisa su nuova offerta sotto soglia o ribasso significativo."""
    alerts_state = state.setdefault("alerts", {})
    out: list[tuple[str, str]] = []
    for f in fares:
        if f["currency"] != cfg["currency"] or f["price"] > cfg["flight"]["max_price_per_person"]:
            continue
        key = f"{f['dest']}|{f['fri']}"
        prev = alerts_state.get(key)
        drop = cfg["flight"].get("realert_price_drop", 5)
        if prev is None or f["price"] <= float(prev["price"]) - drop:
            out.append((key, fmt_fare_message(cfg, f)))
    return out


def update_history(cfg: dict, state: dict, fares: list[dict], today: date) -> None:
    hist = state.setdefault("history", {})
    best: dict[str, float] = {}
    for f in fares:
        if f["currency"] != cfg["currency"]:
            continue
        k = f"{f['dest']}|{f['fri']}"
        best[k] = min(best.get(k, 1e9), f["price"])
    for k, price in best.items():
        series = [p for p in hist.get(k, []) if p[0] != today.isoformat()]
        series.append([today.isoformat(), price])
        cutoff = (today - timedelta(days=cfg["limits"]["history_days"])).isoformat()
        hist[k] = [p for p in series if p[0] >= cutoff]
    for k in list(hist):  # weekend passati
        if k.split("|")[1] < today.isoformat():
            del hist[k]


def write_data(cfg: dict, state: dict, fares: list[dict], now: datetime, path: Path) -> None:
    fares = sorted(fares, key=lambda f: (f["price"], f["fri"]))
    data = {
        "generated_at": _iso(now), "origin": cfg["origin"], "currency": cfg["currency"],
        "criteria": {"max_flight_pp": cfg["flight"]["max_price_per_person"], **{k: cfg["hotel"][k] for k in
                     ("max_per_night", "min_rating", "max_km_from_centre", "nights", "adults")}},
        "fares": fares, "history": state.get("history", {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def build_digest(data: dict, top: int = 10) -> str:
    fares = [f for f in data.get("fares", []) if f.get("currency") == data.get("currency")]
    if not fares:
        return "📊 Riepilogo settimanale voli da Bari: nessun volo diretto trovato nell'ultima scansione."

    def cost(f: dict) -> float:
        return f.get("trip_total") or f["flight_total"]

    ranked = sorted(fares, key=lambda f: (cost(f), f["fri"]))[:top]
    n = data["criteria"]["adults"]
    lines = [f"📊 Migliori weekend da {data['origin']} (aggiornato {data['generated_at'][:10]}, totali per {n} persone)"]
    for i, f in enumerate(ranked, 1):
        fri = date.fromisoformat(f["fri"])
        hotel = f" + hotel {fmt_eur(f['hotel']['per_night'])}/notte" if f.get("hotel") else ""
        lines.append(f"{i}. {f['dest']} {fri.strftime('%d/%m')}: volo {fmt_eur(f['price'])} a persona{hotel} → {fmt_eur(cost(f))}"
                     + ("" if f.get("hotel") else " (solo voli)"))
    return "\n".join(lines)


# --------------------------------------------------------------------------- main

def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def need_key(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ApiError(f"manca la variabile {name} (secret GitHub)")
    return v


def run_check(cfg: dict, route: str, with_hotel: bool) -> int:
    today = date.today()
    fri, sun = upcoming_weekends(today, 1)[0]
    key = need_key("IGNAV_API_KEY")
    print(f"Verifica volo {cfg['origin']}→{route} {fri} / {sun} (1 richiesta)")
    resp = ignav_round_trip(key, cfg["origin"], route, fri, sun, cfg["flight"].get("request_extra", {}))
    print(f"Itinerari ricevuti: {len((resp or {}).get('itineraries', []) or [])}")
    best = parse_ignav(resp)
    if not best:
        print("Nessun itinerario diretto trovato (rotta non operata quel weekend, o formato risposta diverso).")
    else:
        print(f"Miglior diretto: {best['price']} {best['currency']} per persona, {best['carrier']} {best['out_flight']}/{best['ret_flight']}")
        if best["currency"] != cfg["currency"]:
            print(f"ATTENZIONE: valuta {best['currency']} diversa da {cfg['currency']}: gli avvisi restano disattivati "
                  f"finche' non si imposta la valuta (flight.request_extra in travel_config.json) o si converte.")
    if with_hotel:
        dest = next((d for d in cfg["destinations"] if route in d["airports"]), cfg["destinations"][0])
        h = cfg["hotel"]
        skey = need_key("SERPAPI_API_KEY")
        resp = serpapi_booking(skey, dest["name"], fri, sun, h["adults"], cfg["currency"], h["max_per_night"])
        props = (resp or {}).get("properties", []) or []
        ok = parse_booking(resp, dest, h, h["nights"])
        print(f"Hotel {dest['name']}: {len(props)} strutture ricevute, {len(ok)} entro i criteri")
        for x in ok[:3]:
            print(f"  {x['name']}: {x['per_night']} {cfg['currency']}/notte, voto {x['rating']}, {x['km']} km")
    return 0


def run_scan(cfg: dict, dry: bool) -> int:
    now = datetime.now(timezone.utc)
    today = date.today()
    state = load_json(STATE_PATH, {})
    budget = Budget(cfg["limits"]["max_requests_per_run"])
    ikey, skey = need_key("IGNAV_API_KEY"), need_key("SERPAPI_API_KEY")
    fares = scan_flights(cfg, state, ikey, budget, today)
    foreign = {f["currency"] for f in fares if f["currency"] != cfg["currency"]}
    if foreign:
        print(f"ATTENZIONE: prezzi in {', '.join(sorted(foreign))} ignorati (attesa {cfg['currency']}).")
    attach_hotels(cfg, fares, skey, budget)
    finalize(cfg, fares)
    update_history(cfg, state, fares, today)
    print(f"Richieste usate: {budget.used}/{budget.limit}. Voli diretti trovati: {len(fares)}.")
    for f in sorted(fares, key=lambda x: x["price"])[:10]:
        print(f"  {f['dest']:<12} {f['fri']}  {f['price']:>7.2f} {f['currency']}  {f.get('hotel', {}).get('name', '-')}")

    alerts = build_alerts(cfg, state, fares, now)
    sent: list[str] = []
    code = 0
    for key, text in alerts:
        if dry:
            print(f"--- [dry-run] Telegram ---\n{text}\n---")
            continue
        try:
            telegram_send(text)
            sent.append(key)
        except TelegramError as e:
            print(f"ERRORE Telegram: {e}", file=sys.stderr)
            code = 1
    price_of = {f"{f['dest']}|{f['fri']}": f["price"] for f in fares}
    for key in sent:
        state.setdefault("alerts", {})[key] = {"price": price_of[key], "ts": _iso(now)}
    for key in list(state.get("alerts", {})):  # weekend passati
        if key.split("|")[1] < today.isoformat():
            del state["alerts"][key]
    if not dry:
        STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        write_data(cfg, state, fares, now, DATA_PATH)
    return code


def run_digest(dry: bool) -> int:
    data = load_json(DATA_PATH, None)
    if not data or not data.get("generated_at"):
        print("Nessun dato: esegui prima una scansione.", file=sys.stderr)
        return 1
    text = build_digest(data)
    if dry:
        print(text)
        return 0
    try:
        telegram_send(text)
    except TelegramError as e:
        print(f"ERRORE Telegram: {e}", file=sys.stderr)
        return 1
    print("Riepilogo inviato.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Weekend da Bari: voli diretti + hotel")
    ap.add_argument("--check", action="store_true", help="1 richiesta di prova (verifica chiavi, valuta, formato)")
    ap.add_argument("--route", default="BUD", help="aeroporto di prova per --check")
    ap.add_argument("--with-hotel", action="store_true", help="con --check: prova anche 1 ricerca hotel")
    ap.add_argument("--digest", action="store_true", help="invia il riepilogo settimanale")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("travel_config.json non trovato", file=sys.stderr)
        return 2
    try:
        if args.check:
            return run_check(cfg, args.route.upper(), args.with_hotel)
        if args.digest:
            return run_digest(args.dry_run)
        return run_scan(cfg, args.dry_run)
    except ApiError as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
