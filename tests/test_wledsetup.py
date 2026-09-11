import unittest

from ledbar.wledsetup import configure, plan

FRESH = {
    "hw": {"led": {"total": 30, "maxpwr": 850,
                   "ins": [{"len": 30, "pin": [2], "ref": False, "order": 0, "type": 22,
                            "maxpwr": 850, "start": 0, "rev": False, "skip": 0}]}},
    "def": {"on": False, "bri": 128, "ps": 0},
    "if": {"live": {"maxbri": False, "no-gc": False, "timeout": 25}},
}


class PlanTests(unittest.TestCase):
    def test_sets_everything_that_matters(self):
        payload, changes = plan(FRESH, total=24)
        text = " | ".join(changes)
        for expected in ("LED count", "output length", "max PSU current", "off refresh",
                         "master on at boot", "force max brightness", "realtime gamma disabled"):
            self.assertIn(expected, text)
        self.assertEqual(payload["hw"]["led"]["total"], 24)
        self.assertEqual(payload["hw"]["led"]["ins"][0]["len"], 24)
        self.assertTrue(payload["hw"]["led"]["ins"][0]["ref"])
        self.assertTrue(payload["def"]["on"])
        self.assertTrue(payload["if"]["live"]["maxbri"])
        self.assertTrue(payload["if"]["live"]["no-gc"])

    def test_preserves_fields_it_does_not_own(self):
        """Colour order, reverse and skip are the user's wiring - never touch them."""
        payload, _ = plan(FRESH, total=24)
        ins0 = payload["hw"]["led"]["ins"][0]
        self.assertEqual(ins0["order"], 0)
        self.assertEqual(ins0["rev"], False)
        self.assertEqual(ins0["skip"], 0)
        self.assertEqual(ins0["type"], 22)
        self.assertNotIn("bri", payload["def"])      # brightness 0 reads as "off" - never set it

    def test_leaves_the_pin_alone_unless_asked(self):
        payload, changes = plan(FRESH, total=24)
        self.assertEqual(payload["hw"]["led"]["ins"][0]["pin"], [2])
        self.assertNotIn("data GPIO", " ".join(changes))
        payload, changes = plan(FRESH, total=24, pin=4)
        self.assertEqual(payload["hw"]["led"]["ins"][0]["pin"], [4])
        self.assertIn("data GPIO", " ".join(changes))

    def test_current_limit_clears_the_worst_case(self):
        _, _ = plan(FRESH, total=24)
        payload, _ = plan(FRESH, total=24)
        # 24 x 55mA = 1320mA worst case; the limiter must sit above it
        self.assertGreater(payload["hw"]["led"]["maxpwr"], 24 * 55)

    def test_no_changes_when_already_correct(self):
        payload, _ = plan(FRESH, total=24)
        applied = {
            "hw": {"led": {"total": 24, "maxpwr": payload["hw"]["led"]["maxpwr"],
                           "ins": [payload["hw"]["led"]["ins"][0]]}},
            "def": {"on": True},
            "if": {"live": {"maxbri": True, "no-gc": True}},
        }
        _, changes = plan(applied, total=24)
        self.assertEqual(changes, [])


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.posted = []

    def get(self, url, timeout=5.0):
        return FRESH

    def post(self, url, payload, timeout=5.0):
        self.posted.append((url, payload))
        return {"success": True}

    def test_applies_and_reports(self):
        changes = configure("1.2.3.4", 24, get=self.get, post=self.post)
        self.assertTrue(changes)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.posted[0][0], "http://1.2.3.4/json/cfg")

    def test_dry_run_writes_nothing(self):
        changes = configure("1.2.3.4", 24, dry_run=True, get=self.get, post=self.post)
        self.assertTrue(changes)
        self.assertEqual(self.posted, [])

    def test_raises_when_wled_rejects_it(self):
        with self.assertRaises(RuntimeError):
            configure("1.2.3.4", 24, get=self.get, post=lambda u, p, t=5.0: {"success": False})


if __name__ == "__main__":
    unittest.main()
