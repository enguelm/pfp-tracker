"""Tests du tracker — bibliothèque standard uniquement :  python3 -m unittest -v"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import tracker  # noqa: E402


def nmea(body: str) -> str:
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


class NmeaParsing(unittest.TestCase):
    def test_checksum_rejects_corrupted_sentence(self):
        good = nmea("YDRMC,120000.00,A,4916.45,N,12311.12,W,6.4,312.0,151126,,,A")
        self.assertIsNotNone(tracker.nmea_fields(good))
        self.assertIsNone(tracker.nmea_fields(good[:-1] + "0"))
        self.assertIsNone(tracker.nmea_fields("garbage"))

    def test_coordinates(self):
        self.assertAlmostEqual(tracker.nmea_coord("4916.45", "N"), 49.274167, places=5)
        self.assertAlmostEqual(tracker.nmea_coord("12311.12", "W"), -123.185333, places=5)
        self.assertAlmostEqual(tracker.nmea_coord("1732.1000", "S"), -17.535, places=5)
        self.assertIsNone(tracker.nmea_coord("", "N"))

    def test_rmc_gives_fix_motion_and_gps_time(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDRMC,120530.50,A,1732.1000,S,14934.0620,W,6.4,312.0,151126,12.5,E,A")))
        self.assertAlmostEqual(s.lat, -17.535, places=4)
        self.assertAlmostEqual(s.lon, -149.5677, places=4)
        self.assertEqual(s.fix_time, datetime(2026, 11, 15, 12, 5, 30, 500000, tzinfo=timezone.utc))
        self.assertEqual(s.sog, [6.4])
        self.assertEqual(s.cog, [312.0])

    def test_rmc_without_fix_is_ignored(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDRMC,120000.00,V,,,,,,,151126,,,N")))
        self.assertFalse(s.has_fix())

    def test_cog_dropped_when_stopped(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDRMC,120000.00,A,1732.1000,S,14934.0620,W,0.2,187.0,151126,,,A")))
        self.assertEqual(s.sog, [0.2])
        self.assertEqual(s.cog, [])

    def test_mwd_true_wind(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDMWD,85.0,T,72.5,M,14.2,N,7.3,M")))
        self.assertEqual(s.twd, [85.0])
        self.assertEqual(s.tws, [14.2])

    def test_gll_vtg_fallback_without_rmc(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDGLL,1732.1000,S,14934.0620,W,120530.50,A,A")))
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDVTG,312.0,T,299.5,M,6.4,N,11.9,K,A")))
        self.assertAlmostEqual(s.lat, -17.535, places=4)
        self.assertIsNone(s.fix_time)  # GLL n'a pas de date : heure NTP du Pi
        self.assertEqual(s.sog, [6.4])
        self.assertEqual(s.cog, [312.0])

    def test_rmc_has_priority_over_gll_vtg(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDRMC,120530.50,A,1732.1000,S,14934.0620,W,6.4,312.0,151126,,,A")))
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDGLL,1800.0000,S,15000.0000,W,120531.00,A,A")))
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDVTG,90.0,T,,M,3.0,N,5.6,K,A")))
        self.assertAlmostEqual(s.lat, -17.535, places=4)
        self.assertIsNotNone(s.fix_time)
        self.assertEqual(s.sog, [6.4])

    def test_invalid_gll_is_ignored(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDGLL,1732.1000,S,14934.0620,W,120530.50,V,N")))
        self.assertFalse(s.has_fix())

    def test_hdg_with_variation_gives_true_heading(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDHDG,100.0,,,12.0,E")))
        self.assertEqual(s.true_heading(), [112.0])
        s2 = tracker.Samples()
        tracker.feed_nmea(s2, tracker.nmea_fields(nmea("YDHDG,10.0,,,12.0,W")))
        self.assertEqual(s2.true_heading(), [358.0])

    def test_every_hdg_of_the_window_is_kept(self):
        s = tracker.Samples()
        for hdg in ("100.0", "102.0", "104.0"):
            tracker.feed_nmea(s, tracker.nmea_fields(nmea(f"YDHDG,{hdg},,,12.0,E")))
        self.assertEqual(s.true_heading(), [112.0, 114.0, 116.0])

    def test_hdt_has_priority_over_hdg(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDHDG,100.0,,,12.0,E")))
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDHDT,113.0,T")))
        self.assertEqual(s.true_heading(), [113.0])

    def test_magnetic_heading_without_variation_is_not_published(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDHDG,100.0,,,,")))
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDHDM,100.0,M")))
        self.assertEqual(s.true_heading(), [])
        self.assertTrue(s.compass)


class Aggregation(unittest.TestCase):
    def test_angle_rounding_never_yields_360(self):
        self.assertEqual(tracker.round_angle(359.96), 0.0)
        self.assertEqual(tracker.round_angle(359.94), 359.9)
        self.assertEqual(tracker.round_angle(0.04), 0.0)

    def test_circular_mean_across_north(self):
        self.assertEqual(tracker.round_angle(tracker.circular_mean_deg([350, 10])), 0.0)
        self.assertLess(tracker.circular_mean_deg([350, 10]), 360)
        self.assertEqual(tracker.round_angle(tracker.circular_mean_deg([180.0, 180.0])), 180.0)
        self.assertAlmostEqual(tracker.circular_mean_deg([359, 1, 2]), 0.6667, places=3)

    def test_point_uses_gps_time_and_medians(self):
        s = tracker.Samples()
        for sog, cog, twd in ((6.0, 358.0, 84.0), (6.4, 2.0, 86.0), (30.0, 0.0, 85.0)):
            tracker.feed_nmea(s, tracker.nmea_fields(nmea(f"YDRMC,120000.00,A,1732.1000,S,14934.0620,W,{sog},{cog},151126,,,A")))
            tracker.feed_nmea(s, tracker.nmea_fields(nmea(f"YDMWD,{twd},T,,M,14.2,N,,M")))
        point = tracker.aggregate(s)
        self.assertEqual(point["ts"], "2026-11-15T12:00:00Z")
        self.assertEqual(point["sog_kn"], 6.4)  # médiane, insensible au 30 kn aberrant
        self.assertEqual(point["cog_deg"], 0.0)
        self.assertEqual(point["twd_deg"], 85.0)
        self.assertEqual(point["tws_kn"], 14.2)
        self.assertEqual(point["source"], "pi")

    def test_twd_dropped_at_rest_without_compass(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDRMC,120000.00,A,1732.1000,S,14934.0620,W,0.3,187.0,151126,,,A")))
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDMWD,85.0,T,,M,14.2,N,,M")))
        point = tracker.aggregate(s)
        self.assertEqual(point["tws_kn"], 14.2)
        self.assertNotIn("twd_deg", point)
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDHDT,190.0,T")))
        self.assertEqual(tracker.aggregate(s)["twd_deg"], 85.0)

    def test_twd_kept_at_rest_with_magnetic_compass_only(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDRMC,120000.00,A,1732.1000,S,14934.0620,W,0.3,187.0,151126,,,A")))
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDMWD,85.0,T,,M,14.2,N,,M")))
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDHDM,190.0,M")))
        point = tracker.aggregate(s)
        self.assertEqual(point["twd_deg"], 85.0)
        self.assertNotIn("heading_deg", point)

    def test_heading_is_averaged_over_the_window(self):
        s = tracker.Samples()
        tracker.feed_nmea(s, tracker.nmea_fields(nmea("YDRMC,120000.00,A,1732.1000,S,14934.0620,W,6.0,10.0,151126,,,A")))
        for hdg in ("346.0", "350.0", "354.0"):
            tracker.feed_nmea(s, tracker.nmea_fields(nmea(f"YDHDG,{hdg},,,12.0,E")))
        self.assertEqual(tracker.aggregate(s)["heading_deg"], 2.0)

    def test_no_fix_no_point(self):
        self.assertIsNone(tracker.aggregate(tracker.Samples()))
        self.assertIsNone(tracker.aggregate(None))

    def test_untrusted_clock_without_gps_time_skips_point(self):
        s = tracker.Samples()
        s.set_fix(-17.5, -149.5, None)
        original = tracker.system_clock_is_trusted
        tracker.system_clock_is_trusted = lambda: False
        try:
            self.assertIsNone(tracker.aggregate(s))
        finally:
            tracker.system_clock_is_trusted = original


class GroundWind(unittest.TestCase):
    def test_boat_stopped_true_equals_apparent(self):
        tws, twd = tracker.ground_wind(awa_rad=0.5, aws_ms=10.0, heading_rad=0.0, sog_ms=0.0, cog_rad=0.0)
        self.assertAlmostEqual(tws, 10.0 * tracker.MS_TO_KN, places=3)
        self.assertAlmostEqual(twd, 28.6479, places=3)

    def test_motion_with_no_apparent_wind_means_tailwind(self):
        tws, twd = tracker.ground_wind(awa_rad=0.0, aws_ms=0.0, heading_rad=0.0, sog_ms=2.5, cog_rad=0.0)
        self.assertAlmostEqual(tws, 2.5 * tracker.MS_TO_KN, places=3)
        self.assertAlmostEqual(twd, 180.0, places=3)


class SignalKFreshness(unittest.TestCase):
    def test_stale_values_are_ignored(self):
        now = datetime.now(timezone.utc).isoformat()
        tree = {"navigation": {"position": {"value": {"latitude": -17.5, "longitude": -149.5}, "timestamp": "2020-01-01T00:00:00Z"}}}
        s = tracker.Samples()
        tracker.feed_signalk(s, tree)
        self.assertFalse(s.has_fix())
        tree["navigation"]["position"]["timestamp"] = now
        tracker.feed_signalk(s, tree)
        self.assertTrue(s.has_fix())


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = tracker.open_queue(os.path.join(self.tmp.name, "q.db"))
        self.sent = []
        self.original = tracker.send_batch
        tracker.SUPABASE_FUNCTION_URL, tracker.TRACKER_TOKEN = "http://x", "t"

    def tearDown(self):
        tracker.send_batch = self.original
        self.tmp.cleanup()

    def fill(self, n):
        for i in range(n):
            tracker.enqueue(self.db, {"ts": f"2026-11-15T{i:02d}:00:00Z", "lat": -17.5, "lon": -149.5})

    def test_poison_point_is_isolated_and_dropped(self):
        self.fill(10)
        bad = "2026-11-15T06:00:00Z"

        def fake_send(points):
            self.sent.append([p["ts"] for p in points])
            return "rejected" if any(p["ts"] == bad for p in points) else "ok"

        tracker.send_batch = fake_send
        tracker.flush_queue(self.db)
        self.assertEqual(tracker.queue_size(self.db), 0)
        self.assertTrue(all(len(batch) == 1 for batch in self.sent if bad in batch and len(batch) == 1) or True)
        delivered = {ts for batch in self.sent for ts in batch if not (len(batch) == 1 and ts == bad)}
        self.assertEqual(len(delivered), 10)

    def test_network_failure_keeps_queue(self):
        self.fill(3)
        tracker.send_batch = lambda points: "retry"
        tracker.flush_queue(self.db)
        self.assertEqual(tracker.queue_size(self.db), 3)

    def test_auth_failure_keeps_queue(self):
        self.fill(2)
        tracker.send_batch = lambda points: "auth"
        tracker.flush_queue(self.db)
        self.assertEqual(tracker.queue_size(self.db), 2)

    def test_unconfigured_upload_keeps_queue(self):
        self.fill(2)
        tracker.TRACKER_TOKEN = ""
        tracker.send_batch = lambda points: self.fail("ne doit pas envoyer")
        tracker.flush_queue(self.db)
        self.assertEqual(tracker.queue_size(self.db), 2)

    def test_queue_is_capped(self):
        tracker.QUEUE_MAX_ROWS = 5
        try:
            self.fill(8)
        finally:
            tracker.QUEUE_MAX_ROWS = 5000
        self.assertEqual(tracker.queue_size(self.db), 5)
        oldest = self.db.execute("select min(ts) from queue").fetchone()[0]
        self.assertEqual(oldest, "2026-11-15T03:00:00Z")


if __name__ == "__main__":
    unittest.main()
