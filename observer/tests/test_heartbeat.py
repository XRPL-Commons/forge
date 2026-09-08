import os, json, tempfile, unittest
from observer import heartbeat

DAY = 86400
NOON_JAN2 = 1767355200.0  # 2026-01-02 12:00:00 UTC


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.sd = self.td.name
        self.sent = []
        self.runner = lambda cmd: (self.sent.append(cmd[1]) or True)

    def tearDown(self):
        self.td.cleanup()

    def hb(self, now):
        return heartbeat.maybe_heartbeat(self.sd, 5, "/x/notify.sh",
                                         now=now, runner=self.runner)

    def test_sends_on_new_day_when_clean(self):
        heartbeat.record_scan(self.sd, 3, now=NOON_JAN2 - 3600)
        self.assertTrue(self.hb(NOON_JAN2))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("all clear", self.sent[0])
        self.assertIn("3 merges", self.sent[0])
        self.assertIn("5 repos", self.sent[0])

    def test_skips_same_day(self):
        self.assertTrue(self.hb(NOON_JAN2))
        self.assertFalse(self.hb(NOON_JAN2 + 600))
        self.assertEqual(len(self.sent), 1)

    def test_sends_again_next_day(self):
        self.assertTrue(self.hb(NOON_JAN2))
        self.assertTrue(self.hb(NOON_JAN2 + DAY))
        self.assertEqual(len(self.sent), 2)

    def test_recent_alert_suppresses_but_stamps(self):
        heartbeat.record_alert(self.sd, now=NOON_JAN2 - 3600)
        self.assertFalse(self.hb(NOON_JAN2))
        self.assertEqual(self.sent, [])
        # stamped: same day later does not send either
        self.assertFalse(self.hb(NOON_JAN2 + 600))

    def test_old_alert_does_not_suppress(self):
        heartbeat.record_alert(self.sd, now=NOON_JAN2 - 2 * DAY)
        self.assertTrue(self.hb(NOON_JAN2))

    def test_scan_count_only_last_24h(self):
        heartbeat.record_scan(self.sd, 7, now=NOON_JAN2 - 2 * DAY)
        heartbeat.record_scan(self.sd, 2, now=NOON_JAN2 - 3600)
        heartbeat.record_scan(self.sd, 1, now=NOON_JAN2 - 60)
        self.assertTrue(self.hb(NOON_JAN2))
        self.assertIn("3 merges", self.sent[0])

    def test_scans_pruned_beyond_48h(self):
        heartbeat.record_scan(self.sd, 7, now=NOON_JAN2 - 3 * DAY)
        heartbeat.record_scan(self.sd, 2, now=NOON_JAN2 - 3600)
        self.hb(NOON_JAN2)
        p = os.path.join(self.sd, "scans.jsonl")
        lines = [json.loads(l) for l in open(p)]
        self.assertEqual([l["scanned"] for l in lines], [2])

    def test_zero_scans_still_sends(self):
        self.assertTrue(self.hb(NOON_JAN2))
        self.assertIn("0 merges", self.sent[0])

    def test_notify_failure_does_not_stamp(self):
        r = lambda cmd: False
        self.assertFalse(heartbeat.maybe_heartbeat(self.sd, 5, "/x/notify.sh",
                                                   now=NOON_JAN2, runner=r))
        # not stamped, so a later run the same day retries
        self.assertTrue(self.hb(NOON_JAN2 + 600))


if __name__ == "__main__":
    unittest.main()
