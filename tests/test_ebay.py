import io
import json
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import monitor  # noqa: E402

CFG = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
SRC = next(s for s in CFG["sources"] if s["type"] == "ebay_search")
ENV = {"EBAY_CLIENT_ID": "CID", "EBAY_CLIENT_SECRET": "SECRETXYZ"}
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


# Risposta ricostruita dallo schema documentato (Browse API, itemSummaries): il formato reale non e' stato osservato.
def item(iid="v1|1|0", title="Apple Mac mini M4 16GB 256GB nuovo sigillato", price="549.00", ship="0.00",
         pct="99.2", score=1520, cur="EUR"):
    d = {"itemId": iid, "title": title, "price": {"value": price, "currency": cur},
         "itemWebUrl": f"https://www.ebay.it/itm/{iid[3:4]}", "condition": "Nuovo",
         "seller": {"username": "negozio_it", "feedbackPercentage": pct, "feedbackScore": score}}
    if ship is not None:
        d["shippingOptions"] = [{"shippingCost": {"value": ship, "currency": "EUR"}}]
    return d


def run(items, env=ENV):
    with mock.patch.dict("os.environ", env, clear=True), \
         mock.patch.object(monitor, "ebay_token", return_value="TOK"), \
         mock.patch.object(monitor, "ebay_search", return_value={"itemSummaries": items}):
        return monitor.check_source(SRC, CFG)


class Parse(unittest.TestCase):
    def test_fields(self):
        p = monitor.parse_ebay_items({"itemSummaries": [item()]})[0]
        self.assertEqual((p["price"], p["shipping"], p["fb_pct"], p["fb_score"]), (549.0, 0.0, 99.2, 1520.0))

    def test_cheapest_shipping_and_missing(self):
        it = item(ship=None)
        it["shippingOptions"] = [{"shippingCost": {"value": "12.00", "currency": "EUR"}}, {"shippingCost": {"value": "5.50", "currency": "EUR"}}]
        self.assertEqual(monitor.parse_ebay_items({"itemSummaries": [it]})[0]["shipping"], 5.5)
        self.assertIsNone(monitor.parse_ebay_items({"itemSummaries": [item(ship=None)]})[0]["shipping"])

    def test_non_eur_and_empty(self):
        self.assertEqual(monitor.parse_ebay_items({"itemSummaries": [item(cur="USD")]}), [])
        self.assertEqual(monitor.parse_ebay_items({}), [])


class Flow(unittest.TestCase):
    def test_good_listing_is_deal(self):
        res = run([item()])
        self.assertEqual(res.status, "ok")
        o = res.offers[0]
        self.assertTrue(o.in_stock)
        self.assertEqual(o.price, 549.0)
        self.assertEqual(o.key, "eBay.it#v1|1|0")
        self.assertIn("99.2% feedback", o.note)

    def test_shipping_counts_toward_threshold(self):
        self.assertEqual(run([item(price="590.00", ship="15.00")]).offers, [])

    def test_unknown_shipping_flagged(self):
        o = run([item(ship=None)]).offers[0]
        self.assertIn("spedizione da verificare", o.note)

    def test_seller_filter(self):
        res = run([item("a|1|0", pct="95.0"), item("b|2|0", score=40), item("c|3|0", pct=None)])
        self.assertTrue(all(not o.in_stock for o in res.offers))
        self.assertTrue(all("venditore scartato" in o.detail for o in res.offers))

    def test_title_filter(self):
        titles = ["Apple Mac mini M4 Pro 24GB 512GB", "Mac mini M4 16GB 256GB ricondizionato", "Mac mini M2 16GB 256GB",
                  "Docking station per Apple Mac mini M4 16GB 256GB", "Apple Mac mini M4 16GB 256GB per parti difettoso"]
        res = run([item(f"x|{i}|0", title=t) for i, t in enumerate(titles)])
        self.assertEqual(res.offers, [])
        self.assertTrue(any("scartato dal titolo" in n for n in res.notes))

    def test_missing_credentials(self):
        res = run([item()], env={})
        self.assertEqual(res.status, "error")
        self.assertIn("EBAY_CLIENT_ID", res.detail)

    def test_auth_error_is_blocked_and_clean(self):
        with mock.patch.dict("os.environ", ENV, clear=True), \
             mock.patch.object(monitor, "ebay_token", side_effect=monitor.FetchError("HTTP 401 (invalid_client)")):
            res = monitor.check_source(SRC, CFG)
        self.assertEqual(res.status, "blocked")

    def test_alert_via_process(self):
        res = run([item()])
        st, pend = monitor.process({}, [res], CFG, T0)
        self.assertEqual(len(pend), 1)
        self.assertIn("eBay.it", pend[0][2])
        self.assertIn("Venditore", pend[0][2])


class Safety(unittest.TestCase):
    def test_errors_never_leak_secrets(self):
        body = io.BytesIO(b'{"error_description":"client authentication failed"}')
        err = urllib.error.HTTPError("https://api.ebay.com/identity/v1/oauth2/token", 401, "x", {}, body)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(monitor.FetchError) as cm:
                monitor.ebay_token("CID", "SECRETXYZ")
        self.assertNotIn("SECRETXYZ", str(cm.exception))
        self.assertIn("401", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_token_request_shape(self):
        captured = {}

        def fake(url, data=None, headers=None, timeout=25):
            captured.update(url=url, data=data, headers=headers)
            return {"access_token": "T"}

        with mock.patch.object(monitor, "_json_request", side_effect=fake):
            self.assertEqual(monitor.ebay_token("CID", "SECRETXYZ"), "T")
        self.assertTrue(captured["headers"]["Authorization"].startswith("Basic "))
        self.assertIn(b"client_credentials", captured["data"])

    def test_search_request_filters(self):
        with mock.patch.object(monitor, "_json_request", return_value={}) as m:
            monitor.ebay_search("TOK", SRC, CFG)
        url, headers = m.call_args.args[0], m.call_args.kwargs["headers"]
        self.assertIn("conditions%3A%7BNEW%7D", url)
        self.assertIn("buyingOptions%3A%7BFIXED_PRICE%7D", url)
        self.assertIn("price%3A%5B..600%5D", url)
        self.assertEqual(headers["X-EBAY-C-MARKETPLACE-ID"], "EBAY_IT")
        self.assertEqual(headers["Authorization"], "Bearer TOK")


class Config(unittest.TestCase):
    def test_ebay_in_http_mode(self):
        self.assertEqual(monitor.MODES["ebay_search"], "http")
        self.assertEqual(SRC["seller_filter"], {"min_feedback_pct": 98, "min_feedback_score": 100})


if __name__ == "__main__":
    unittest.main()
