import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import monitor  # noqa: E402

CFG = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
FX = HERE / "fixtures"
UNIEURO = (FX / "unieuro_out_of_stock.html").read_text(encoding="utf-8")
AMZ_USED = (FX / "amazon_used_only.html").read_text(encoding="utf-8")
AMZ_NEW = (FX / "amazon_new_RECONSTRUCTED.html").read_text(encoding="utf-8")
AMZ_CAPTCHA = (FX / "amazon_captcha.html").read_text(encoding="utf-8")
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)

ALI_SRC = next(s for s in CFG["sources"] if s["type"] == "aliexpress_search")
MW_SRC = next(s for s in CFG["sources"] if s["type"] == "mediaworld")


def offer(price=549.9, in_stock=True, source="Unieuro", item="", note=""):
    return monitor.Offer(source, "https://example.test/p", "ok", price, in_stock, "Mac mini M4 16GB 256GB", item=item, note=note)


class ParsePrice(unittest.TestCase):
    def test_formats(self):
        cases = {"549,90 €": 549.9, "€ 1.079,00": 1079.0, "549.90": 549.9, "1.079": 1079.0,
                 "599": 599.0, 549.9: 549.9, "1,234.50": 1234.5, None: None, "n/d": None}
        for raw, expected in cases.items():
            self.assertEqual(monitor.parse_price(raw), expected, raw)


class Unieuro(unittest.TestCase):
    def test_out_of_stock_reads_right_product(self):
        p = monitor.parse_unieuro(UNIEURO, "APLMU9D3TA")
        self.assertEqual(p.price, 549.9)
        self.assertFalse(p.in_stock)
        self.assertIn("256GB", p.name)

    def test_in_stock(self):
        page = UNIEURO.replace('"stockLevel":0,"stockLevelStatus":"outOfStock"', '"stockLevel":3,"stockLevelStatus":"lowStock"')
        p = monitor.parse_unieuro(page, "APLMU9D3TA")
        self.assertTrue(p.in_stock)
        self.assertEqual(p.price, 549.9)

    def test_unknown_sku_or_page(self):
        self.assertIsNone(monitor.parse_unieuro(UNIEURO, "NOPE"))
        self.assertIsNone(monitor.parse_unieuro("<html></html>", "APLMU9D3TA"))


class Amazon(unittest.TestCase):
    def test_used_only_is_not_a_deal(self):
        p = monitor.parse_amazon(AMZ_USED)
        self.assertEqual(p.price, 712.94)
        self.assertFalse(p.in_stock)
        self.assertTrue(p.extra.get("used_only"))

    def test_used_cheap_still_not_a_deal(self):
        p = monitor.parse_amazon(AMZ_USED.replace("712,94", "400,00"))
        self.assertFalse(p.in_stock)  # un usato a 400 non deve mai far scattare l'avviso

    def test_new_offer(self):
        p = monitor.parse_amazon(AMZ_NEW)
        self.assertEqual(p.price, 579.0)
        self.assertTrue(p.in_stock)

    def test_captcha(self):
        self.assertEqual(monitor.parse_amazon(AMZ_CAPTCHA), "blocked")

    def test_unknown_page(self):
        self.assertIsNone(monitor.parse_amazon("<html><body>ciao</body></html>"))


class MediaWorld(unittest.TestCase):
    REAL_UNAVAILABLE = {"h1": ["APPLE DESKTOP MAC MINI M4 16GB 256 GB, SSD, CHIP M4"], "price": [""],
                        "avail": ["Non Disponibile"], "cofr": ["mms-cofr-delivery_NOT_AVAILABLE"]}

    def test_unavailable_real_dom(self):
        p = monitor.parse_mediaworld(self.REAL_UNAVAILABLE)
        self.assertFalse(p.in_stock)
        self.assertIsNone(p.price)
        self.assertTrue(p.extra["unavailable"])

    def test_available_assumed_when_price_and_no_not_available(self):  # markup 'disponibile' NON verificato
        d = {"h1": ["APPLE MAC MINI M4"], "price": ["599,00 €"], "avail": ["Disponibile"], "cofr": ["mms-cofr-delivery_HOME"]}
        p = monitor.parse_mediaworld(d)
        self.assertTrue(p.in_stock)
        self.assertEqual(p.price, 599.0)

    def test_struck_price_ignored(self):
        d = {"h1": ["APPLE MAC MINI M4"], "price": ["699,00 € 579,00 €"], "avail": [], "cofr": []}
        self.assertEqual(monitor.parse_mediaworld(d).price, 579.0)

    def test_no_h1(self):
        self.assertIsNone(monitor.parse_mediaworld({"h1": [], "price": [], "avail": [], "cofr": []}))

    def test_source_flow_unavailable_is_ok_status(self):
        r = mock.Mock()
        r.get.return_value = (200, self.REAL_UNAVAILABLE)
        res = monitor.check_mediaworld(MW_SRC, CFG, r)
        self.assertEqual(res.status, "ok")
        self.assertFalse(res.offers[0].in_stock)


# Testo ricostruito dai pattern osservati su pagine reali: la forma esatta di alcune righe NON e' verificata.
ALI_ITEM_GOOD = """Apple Mac mini M4 16GB 256GB
4.9
1.250 Recensioni
2.300+ venduti
549,00 €
Prezzo IVA inclusa | Nessun costo di importazione
Spedizione da Spagna
Venduto da Apple Official Store, processato da AliExpress
98.5% Recensioni positive | 5.400 Seguiti
"""
ALI_ITEM_BAD = """Apple Mac mini M4 16GB 256GB
4.2
8 Recensioni
12 venduti
499,00 €
Spedizione da Cina
Venduto da Shady Store
0.0% Recensioni positive | 26 Seguiti
"""


class AliExpress(unittest.TestCase):
    def test_card(self):
        c = monitor.parse_ali_card("Apple Mac mini M4 16GB 256GB\n4.8\n1.979€\n1.979 €\nSpedizione gratuita")
        self.assertEqual(c["price"], 1979.0)
        self.assertEqual(monitor.parse_ali_card("solo titolo"), None)

    def test_item_parse(self):
        i = monitor.parse_ali_item(ALI_ITEM_GOOD)
        self.assertEqual(i["store_pct"], 98.5)
        self.assertEqual(i["followers"], 5400)
        self.assertEqual(i["rating"], 4.9)
        self.assertEqual(i["reviews"], 1250)
        self.assertEqual(i["sold"], 2300)
        self.assertEqual(i["ship_from"], "Spagna")
        self.assertEqual(i["price"], 549.0)
        self.assertTrue(i["no_import_fees"])

    def test_filter_good_passes(self):
        self.assertEqual(monitor.seller_reasons(monitor.parse_ali_item(ALI_ITEM_GOOD), CFG["seller_filter"]), [])

    def test_filter_bad_rejected_with_reasons(self):
        reasons = monitor.seller_reasons(monitor.parse_ali_item(ALI_ITEM_BAD), CFG["seller_filter"])
        self.assertGreaterEqual(len(reasons), 4)
        self.assertTrue(any("spedito da Cina" in r for r in reasons))

    def test_missing_data_fails_closed(self):
        reasons = monitor.seller_reasons(monitor.parse_ali_item("pagina vuota"), CFG["seller_filter"])
        self.assertTrue(any("dato non trovato" in r for r in reasons))

    def test_unavailable_page(self):
        i = monitor.parse_ali_item("Questo articolo non è più disponibile")
        self.assertTrue(i["unavailable"])

    def _run(self, cards, items):
        r = mock.Mock()
        calls = []

        def get(url, js, **kw):
            calls.append(url)
            if "/item/" in url:
                return 200, {"text": items[url]}
            return 200, cards

        r.get.side_effect = get
        return monitor.check_aliexpress_search(ALI_SRC, CFG, r), calls

    def test_search_filters_decoys_and_checks_seller(self):
        cards = [
            {"id": "1", "url": "https://it.aliexpress.com/item/1.html", "text": "Apple Mac mini M4 16GB 256GB Nuovo\n549,00 €"},
            {"id": "2", "url": "https://it.aliexpress.com/item/2.html", "text": "Mini PC M4 Intel N100 16GB 256GB\n199,00 €"},
            {"id": "3", "url": "https://it.aliexpress.com/item/3.html", "text": "Apple Mac mini M4 Pro 24GB 512GB\n1.979 €"},
            {"id": "4", "url": "https://it.aliexpress.com/item/4.html", "text": "Apple Mac mini M4 16GB 256GB\n499,00 €"},
            {"id": "5", "url": "https://it.aliexpress.com/item/5.html", "text": "Apple Mac mini M4 16GB 256GB\n950,00 €"},
        ]
        items = {cards[0]["url"]: ALI_ITEM_GOOD, cards[3]["url"]: ALI_ITEM_BAD}
        res, calls = self._run(cards, items)
        self.assertEqual(res.status, "ok")
        by_id = {o.item: o for o in res.offers}
        self.assertEqual(set(by_id), {"1", "4"})  # Intel, Pro e fuori prezzo mai aperti
        self.assertEqual(len(calls), 3)
        self.assertTrue(by_id["1"].in_stock)
        self.assertFalse(by_id["4"].in_stock)
        self.assertIn("venditore scartato", by_id["4"].detail)

    def test_logs_rejected_apple_mac_mini_titles(self):
        cards = [{"id": "9", "url": "https://it.aliexpress.com/item/9.html", "text": "Apple Mac mini M4 Pro 24GB 512GB\n1.979 €"}]
        res, _ = self._run(cards, {})
        self.assertTrue(any("scartata dal titolo" in n and "M4 Pro" in n for n in res.notes))

    def test_empty_search_is_parse_error(self):
        res, _ = self._run([], {})
        self.assertEqual(res.status, "parse_error")


class HttpSources(unittest.TestCase):
    def test_status_mapping(self):
        src = CFG["sources"][0]
        for code, expected in [(404, "gone"), (403, "blocked"), (429, "blocked"), (500, "error")]:
            with mock.patch.object(monitor, "fetch", return_value=(code, "")):
                self.assertEqual(monitor.check_source(src, CFG).status, expected, code)

    def test_amazon_flow_used_only(self):
        src = next(s for s in CFG["sources"] if s["type"] == "amazon")
        with mock.patch.object(monitor, "fetch", return_value=(200, AMZ_USED)):
            res = monitor.check_source(src, CFG)
        self.assertEqual(res.status, "ok")
        self.assertFalse(res.offers[0].in_stock)

    def test_amazon_captcha_flow(self):
        src = next(s for s in CFG["sources"] if s["type"] == "amazon")
        with mock.patch.object(monitor, "fetch", return_value=(200, AMZ_CAPTCHA)):
            self.assertEqual(monitor.check_source(src, CFG).status, "blocked")

    def test_mismatch_title(self):
        src = next(s for s in CFG["sources"] if s["type"] == "amazon")
        page = AMZ_NEW.replace("256 GB di archiviazione", "512 GB di archiviazione")
        with mock.patch.object(monitor, "fetch", return_value=(200, page)):
            self.assertEqual(monitor.check_source(src, CFG).status, "mismatch")


class Evaluate(unittest.TestCase):
    def test_alert_on_transition(self):
        st, msgs = monitor.evaluate_offer({}, offer(549.9), CFG, T0)
        self.assertEqual(len(msgs), 1)
        self.assertTrue(st["deal"])

    def test_no_repeat(self):
        st, _ = monitor.evaluate_offer({}, offer(549.9), CFG, T0)
        st2, msgs = monitor.evaluate_offer(st, offer(549.9), CFG, T0 + timedelta(minutes=15))
        self.assertEqual(msgs, [])
        self.assertEqual(st2, st)

    def test_price_drop_realert(self):
        st, _ = monitor.evaluate_offer({}, offer(590), CFG, T0)
        _, msgs = monitor.evaluate_offer(st, offer(579), CFG, T0 + timedelta(minutes=15))
        self.assertEqual(len(msgs), 1)

    def test_small_drop_ignored(self):
        st, _ = monitor.evaluate_offer({}, offer(590), CFG, T0)
        _, msgs = monitor.evaluate_offer(st, offer(587), CFG, T0 + timedelta(minutes=15))
        self.assertEqual(msgs, [])

    def test_over_threshold_or_out_of_stock(self):
        self.assertEqual(monitor.evaluate_offer({}, offer(649), CFG, T0)[1], [])
        self.assertEqual(monitor.evaluate_offer({}, offer(549, in_stock=False), CFG, T0)[1], [])

    def test_flapping_cooldown(self):
        st, _ = monitor.evaluate_offer({}, offer(549.9), CFG, T0)
        st, _ = monitor.evaluate_offer(st, offer(549.9, in_stock=False), CFG, T0 + timedelta(minutes=15))
        _, msgs = monitor.evaluate_offer(st, offer(549.9), CFG, T0 + timedelta(minutes=30))
        self.assertEqual(msgs, [])
        _, msgs = monitor.evaluate_offer(st, offer(549.9), CFG, T0 + timedelta(hours=4))
        self.assertEqual(len(msgs), 1)

    def test_note_in_message(self):
        _, msgs = monitor.evaluate_offer({}, offer(549.9, note="Venditore: X"), CFG, T0)
        self.assertIn("Venditore: X", msgs[0])


class Health(unittest.TestCase):
    def res(self, status):
        return monitor.SourceResult("Sito", "https://x", status, "dettaglio")

    def test_fail_alert_once_and_recovery(self):
        st, m = monitor.evaluate_health({}, self.res("blocked"), CFG, T0)
        self.assertEqual(m, [])
        st, m = monitor.evaluate_health(st, self.res("blocked"), CFG, T0 + timedelta(hours=7))
        self.assertEqual(len(m), 1)
        st, m = monitor.evaluate_health(st, self.res("blocked"), CFG, T0 + timedelta(hours=8))
        self.assertEqual(m, [])
        st, m = monitor.evaluate_health(st, self.res("ok"), CFG, T0 + timedelta(hours=9))
        self.assertEqual(len(m), 1)
        self.assertNotIn("fail_since", st)

    def test_short_glitch_silent(self):
        st, _ = monitor.evaluate_health({}, self.res("error"), CFG, T0)
        st, m = monitor.evaluate_health(st, self.res("ok"), CFG, T0 + timedelta(minutes=15))
        self.assertEqual(m, [])
        self.assertEqual(st, {})


class Process(unittest.TestCase):
    def test_multi_offer_keys_and_vanished_listing(self):
        o1 = offer(549, source="AliExpress", item="1")
        res = monitor.SourceResult("AliExpress", "u", "ok", offers=[o1])
        st, pend = monitor.process({}, [res], CFG, T0)
        self.assertEqual(len(pend), 1)
        self.assertTrue(st["offers"]["AliExpress#1"]["deal"])
        res2 = monitor.SourceResult("AliExpress", "u", "ok", offers=[])
        st2, pend2 = monitor.process(st, [res2], CFG, T0 + timedelta(hours=1))
        self.assertEqual(pend2, [])
        self.assertFalse(st2["offers"]["AliExpress#1"]["deal"])

    def test_failed_source_keeps_offer_state(self):
        res = monitor.SourceResult("Unieuro", "u", "ok", offers=[offer(549)])
        st, _ = monitor.process({}, [res], CFG, T0)
        bad = monitor.SourceResult("Unieuro", "u", "blocked", "403")
        st2, pend = monitor.process(st, [bad], CFG, T0 + timedelta(minutes=15))
        self.assertEqual(pend, [])
        self.assertTrue(st2["offers"]["Unieuro"]["deal"])


class Telegram(unittest.TestCase):
    def test_missing_env(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(monitor.TelegramError):
                monitor.telegram_send("x")

    def test_token_never_leaks(self):
        import urllib.error

        err = urllib.error.HTTPError("https://api.telegram.org/botSECRET123/sendMessage", 401, "x", {}, None)
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "SECRET123", "TELEGRAM_CHAT_ID": "1"}):
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with self.assertRaises(monitor.TelegramError) as cm:
                    monitor.telegram_send("x")
        self.assertNotIn("SECRET123", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_telegram_reason_shown_without_token(self):
        import io
        import urllib.error

        body = io.BytesIO(b'{"ok":false,"error_code":403,"description":"Forbidden: bots can\'t send messages to bots"}')
        err = urllib.error.HTTPError("https://api.telegram.org/botSECRET123/sendMessage", 403, "x", {}, body)
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "SECRET123", "TELEGRAM_CHAT_ID": "1"}):
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with self.assertRaises(monitor.TelegramError) as cm:
                    monitor.telegram_send("x")
        self.assertIn("bots can't send messages to bots", str(cm.exception))
        self.assertNotIn("SECRET123", str(cm.exception))


class Config(unittest.TestCase):
    def test_all_source_types_known(self):
        for s in CFG["sources"]:
            self.assertIn(s["type"], monitor.MODES)

    def test_ali_decoys_rejected_by_title(self):
        bad = ["Mini PC Apple style M4 Intel N100 16GB 256GB Mac mini", "Apple Mac mini M4 Pro 24GB 256GB", "Docking Station per Mac mini M4 16GB 256GB Apple"]
        for t in bad:
            self.assertIsNotNone(monitor.title_reject_reason(t, CFG, ALI_SRC), t)
        self.assertIsNone(monitor.title_reject_reason("Apple Mac mini M4 16GB 256GB nuovo sigillato", CFG, ALI_SRC))


if __name__ == "__main__":
    unittest.main()
