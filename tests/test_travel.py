import io
import json
import sys
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import travel_monitor as t  # noqa: E402

CFG = json.loads((HERE.parent / "travel_config.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 13)  # venerdi'
NOW = datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc)
BUD = next(d for d in CFG["destinations"] if d["name"] == "Budapest")


def seg(code="W6", num="3062", arr="BUD", dep="2026-11-20T06:30:00"):
    return {"marketing_carrier_code": code, "flight_number": num, "operating_carrier_name": "Wizz Air",
            "arrival_airport": arr, "departure_time_local": dep}


def itin(price, cur="EUR", out=1, inn=1):
    return {"price": {"amount": price, "currency": cur, "status": "ok"},
            "outbound": {"carrier": "Wizz Air", "segments": [seg() for _ in range(out)]},
            "inbound": {"carrier": "Wizz Air", "segments": [seg(dep="2026-11-22T18:00:00") for _ in range(inn)]}}


# Risposte ricostruite dagli schemi documentati (Ignav, SerpApi): il formato reale non e' stato osservato.
def prop(name="Hotel Centro", rating=9.0, price=150.0, lat=47.4990, lon=19.0420, sold_out=False):
    return {"name": name, "rating": rating, "extracted_price": price, "link": "https://www.booking.com/hotel/x.html",
            "gps_coordinates": {"latitude": lat, "longitude": lon}, "sold_out": sold_out}


def fare(dest="Budapest", fri="2026-11-20", price=79.0, cur="EUR"):
    return _fin({"dest": dest, "airport": "BUD", "fri": fri, "sun": "2026-11-22", "price": price, "currency": cur,
            "carrier": "Wizz Air", "out_flight": "W63062", "out_time": "", "ret_flight": "", "ret_time": ""})


def _fin(f):
    t.finalize(CFG, [f])
    return f


class Dates(unittest.TestCase):
    def test_weekends(self):
        w = t.upcoming_weekends(TODAY, 8)
        self.assertEqual(len(w), 8)
        self.assertEqual(w[0], (date(2026, 11, 20), date(2026, 11, 22)))
        self.assertTrue(all(f.weekday() == 4 and s.weekday() == 6 for f, s in w))

    def test_from_other_days(self):
        self.assertEqual(t.upcoming_weekends(date(2026, 11, 14), 1)[0][0], date(2026, 11, 20))
        self.assertEqual(t.upcoming_weekends(date(2026, 11, 20), 1)[0][0], date(2026, 11, 27))

    def test_haversine(self):
        self.assertAlmostEqual(t.haversine_km(47.4979, 19.0402, 47.4979, 19.0402), 0.0, places=3)
        self.assertAlmostEqual(t.haversine_km(41.1171, 16.8719, 41.9028, 12.4964), 360, delta=15)  # Bari-Roma


class Ignav(unittest.TestCase):
    def test_picks_cheapest_nonstop(self):
        r = {"itineraries": [itin(120), itin(79), itin(40, out=2), itin(30, inn=2)]}
        b = t.parse_ignav(r)
        self.assertEqual(b["price"], 79)
        self.assertEqual(b["currency"], "EUR")
        self.assertEqual(b["out_flight"], "W63062")

    def test_no_nonstop(self):
        self.assertIsNone(t.parse_ignav({"itineraries": [itin(50, out=2)]}))
        self.assertIsNone(t.parse_ignav({"itineraries": []}))
        self.assertIsNone(t.parse_ignav({}))

    def test_bad_price_ignored(self):
        r = {"itineraries": [{"price": {"amount": None}, "outbound": {"segments": [seg()]}, "inbound": {"segments": [seg()]}}]}
        self.assertIsNone(t.parse_ignav(r))

    def test_request_is_per_person_nonstop(self):
        with mock.patch.object(t, "http_json", return_value={}) as m:
            t.ignav_round_trip("K", "BRI", "BUD", date(2026, 11, 20), date(2026, 11, 22), {})
        body = m.call_args.kwargs["body"]
        self.assertEqual((body["adults"], body["max_stops"]), (1, 0))
        self.assertEqual(m.call_args.kwargs["headers"], {"X-Api-Key": "K"})


class Booking(unittest.TestCase):
    H = CFG["hotel"]

    def parse(self, props):
        return t.parse_booking({"properties": props}, BUD, self.H, 2)

    def test_filters(self):
        props = [
            prop("ok", 9.0, 150),            # 75/notte, vicino, voto alto
            prop("caro", 9.0, 180),          # 90/notte
            prop("voto", 8.4, 120),          # voto < 8.5
            prop("lontano", 9.0, 120, lat=47.55, lon=19.15),  # > 1 km
            prop("esaurito", 9.0, 120, sold_out=True),
            prop("ok2", 8.5, 100),           # 50/notte, voto limite
        ]
        names = [h["name"] for h in self.parse(props)]
        self.assertEqual(names, ["ok2", "ok"])

    def test_limits_inclusive(self):
        self.assertEqual(len(self.parse([prop("x", 8.5, 160)])), 1)  # 80/notte, 8.5

    def test_distance_text_fallback(self):
        p = prop("t", 9, 100)
        p.pop("gps_coordinates")
        p["distance"] = "0.6 km from centre"
        self.assertEqual(len(self.parse([p])), 1)
        p["distance"] = "1.4 km from centre"
        self.assertEqual(len(self.parse([p])), 0)
        p["distance"] = "800 m from centre"
        self.assertEqual(len(self.parse([p])), 1)

    def test_unknown_distance_discarded(self):
        p = prop("t", 9, 100)
        p.pop("gps_coordinates")
        self.assertEqual(self.parse([p]), [])

    def test_missing_fields(self):
        self.assertEqual(self.parse([{"name": "x"}]), [])
        self.assertEqual(t.parse_booking({}, BUD, self.H, 2), [])


class Scan(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["destinations"] = [{"name": "Budapest", "airports": ["BUD"], "lat": 47.4979, "lon": 19.0402},
                                    {"name": "Praga", "airports": ["PRG"], "lat": 50.0755, "lon": 14.4378}]
        self.cfg["weekends"] = 2

    def run_scan(self, state, responder, limit=100):
        with mock.patch.object(t, "ignav_round_trip", side_effect=responder):
            return t.scan_flights(self.cfg, state, "K", t.Budget(limit), TODAY, log=lambda *_: None)

    def test_scan_collects(self):
        fares = self.run_scan({}, lambda k, o, d, dep, ret, e: {"itineraries": [itin(90 if d == "BUD" else 200)]})
        self.assertEqual(len(fares), 4)

    def test_prune_after_empty_scans(self):
        state = {}
        resp = lambda k, o, d, dep, ret, e: {"itineraries": [itin(90)]} if d == "BUD" else {"itineraries": []}
        for _ in range(3):
            self.run_scan(state, resp)
        self.assertIn("skip_until", state["routes"]["PRG"])
        calls = []
        self.run_scan(state, lambda k, o, d, dep, ret, e: calls.append(d) or {"itineraries": []})
        self.assertNotIn("PRG", calls)  # sospesa
        later = TODAY + timedelta(days=15)
        self.assertTrue(t._active(state["routes"]["PRG"], self.cfg, later))

    def test_budget_cap(self):
        n = []
        fares = self.run_scan({}, lambda *a: n.append(1) or {"itineraries": [itin(90)]}, limit=3)
        self.assertEqual(len(n), 3)
        self.assertEqual(len(fares), 3)

    def test_bad_key_stops(self):
        def boom(*a):
            raise t.ApiError("HTTP 401: chiave API rifiutata o non valida")
        with self.assertRaises(t.ApiError):
            self.run_scan({}, boom)

    def test_transient_error_skipped(self):
        def flaky(k, o, d, dep, ret, e):
            if d == "BUD":
                raise t.ApiError("HTTP 500")
            return {"itineraries": [itin(90)]}
        fares = self.run_scan({}, flaky)
        self.assertEqual({f["dest"] for f in fares}, {"Praga"})


class HotelsAndAlerts(unittest.TestCase):
    def test_hotel_targets(self):
        fares = [fare(fri=f"2026-12-{d:02d}", price=p) for d, p in [(4, 150), (11, 70), (18, 90), (25, 130), (1, 60), (8, 140), (15, 160)]]
        fares.append(fare("Praga", price=85))
        fares.append(fare("Roma", price=10, cur="USD"))
        chosen = t.pick_hotel_targets(fares, CFG)
        self.assertEqual(chosen[0]["price"], 60)
        self.assertNotIn(10, [c["price"] for c in chosen])  # valuta diversa esclusa
        deals = [c for c in chosen if c["price"] <= 100]
        self.assertEqual(len(deals), 4)
        self.assertLessEqual(len(chosen), CFG["hotel"]["searches_per_run"])

    def test_attach_hotel(self):
        f = fare()
        resp = {"properties": [prop("Hotel Centro", 9.0, 150)]}
        with mock.patch.object(t, "serpapi_booking", return_value=resp):
            t.attach_hotels(CFG, [f], "S", t.Budget(10), log=lambda *_: None)
        t.finalize(CFG, [f])
        self.assertEqual(f["hotel"]["per_night"], 75.0)
        self.assertEqual(f["flight_total"], 158.0)
        self.assertEqual(f["trip_total"], 308.0)
        self.assertIn("booking.com/searchresults", f["hotel_link"])

    def test_no_hotel_message(self):
        f = fare()
        with mock.patch.object(t, "serpapi_booking", return_value={"properties": [prop("x", 7, 90)]}):
            t.attach_hotels(CFG, [f], "S", t.Budget(10), log=lambda *_: None)
        t.finalize(CFG, [f])
        self.assertIn("Nessun hotel", t.fmt_fare_message(CFG, f))

    def test_alert_threshold_and_dedup(self):
        state = {}
        self.assertEqual(t.build_alerts(CFG, state, [fare(price=100.01)], NOW), [])
        alerts = t.build_alerts(CFG, state, [fare(price=100)], NOW)
        self.assertEqual(len(alerts), 1)
        state["alerts"] = {alerts[0][0]: {"price": 100, "ts": "x"}}
        self.assertEqual(t.build_alerts(CFG, state, [fare(price=98)], NOW), [])  # calo < 5 EUR
        self.assertEqual(len(t.build_alerts(CFG, state, [fare(price=94)], NOW)), 1)

    def test_foreign_currency_never_alerts(self):
        self.assertEqual(t.build_alerts(CFG, {}, [fare(price=10, cur="USD")], NOW), [])

    def test_message_content(self):
        f = fare()
        f["hotel"] = {"name": "Hotel Centro", "rating": 9.1, "per_night": 75.0, "total": 150.0, "km": 0.4, "link": ""}
        f["trip_total"] = 308.0
        t.finalize(CFG, [f])
        m = t.fmt_fare_message(CFG, f)
        for s in ["Budapest", "79,00", "158,00", "Hotel Centro", "0.4 km", "308,00", "google.com/travel/flights"]:
            self.assertIn(s, m)

    def test_history_and_cleanup(self):
        state = {"history": {"Budapest|2026-11-06": [["2026-11-01", 90]]}}
        t.update_history(CFG, state, [fare(price=79), fare(price=95)], TODAY)
        self.assertEqual(state["history"]["Budapest|2026-11-20"], [["2026-11-13", 79]])
        self.assertNotIn("Budapest|2026-11-06", state["history"])
        t.update_history(CFG, state, [fare(price=70)], TODAY)  # stesso giorno: sostituisce
        self.assertEqual(state["history"]["Budapest|2026-11-20"], [["2026-11-13", 70]])


class Digest(unittest.TestCase):
    def data(self):
        a, b, c = fare("Budapest", "2026-11-20", 79), fare("Praga", "2026-11-27", 60), fare("Sofia", "2026-12-04", 40)
        a["hotel"] = {"per_night": 70.0, "total": 140.0}
        for f in (a, b, c):
            f["flight_total"] = f["price"] * 2
        a["trip_total"] = a["flight_total"] + 140
        return {"currency": "EUR", "origin": "BRI", "generated_at": "2026-11-13T06:00:00Z",
                "criteria": {"adults": 2}, "fares": [a, b, c]}

    def test_ranking_and_labels(self):
        text = t.build_digest(self.data())
        lines = text.splitlines()
        self.assertIn("Sofia", lines[1])
        self.assertIn("solo voli", lines[1])
        self.assertTrue(any("Budapest" in l and "hotel" in l for l in lines))

    def test_empty(self):
        self.assertIn("nessun volo", t.build_digest({"fares": [], "currency": "EUR"}))


class Safety(unittest.TestCase):
    def test_api_errors_never_leak_key(self):
        err = urllib.error.HTTPError("https://serpapi.com/search?api_key=SECRETKEY", 403, "x", {}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(t.ApiError) as cm:
                t.http_json("GET", "https://serpapi.com/search?api_key=SECRETKEY")
        self.assertNotIn("SECRETKEY", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_missing_key(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(t.ApiError):
                t.need_key("IGNAV_API_KEY")

    def test_config_sane(self):
        self.assertEqual(CFG["flight"]["max_price_per_person"], 100)
        self.assertEqual((CFG["hotel"]["max_per_night"], CFG["hotel"]["min_rating"], CFG["hotel"]["max_km_from_centre"]), (80, 8.5, 1.0))
        names = {d["name"] for d in CFG["destinations"]}
        self.assertFalse({"Roma", "Milano", "Napoli"} & names)
        for d in CFG["destinations"]:
            self.assertTrue(d["airports"] and -90 < d["lat"] < 90 and -180 < d["lon"] < 180)


if __name__ == "__main__":
    unittest.main()
