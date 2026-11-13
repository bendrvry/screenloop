"""Tests for the parts of screenloop that don't need a Pi: the time windows, the
ping-based presence check, and the session rules in Controller.

Run from the repository root:  python3 -m unittest discover tests
"""

import configparser
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import screenloop as s  # noqa: E402

logging.disable(logging.CRITICAL)


def at(hour, minute=0):
    return datetime(2026, 3, 10, hour, minute)


class TimeHelpers(unittest.TestCase):
    def test_parse_hhmm(self):
        self.assertEqual(s.parse_hhmm("07:30"), 450)
        self.assertEqual(s.parse_hhmm(" 23:59 "), 1439)
        for bad in ("24:00", "12:60", "noon", "7"):
            with self.assertRaises(ValueError):
                s.parse_hhmm(bad)

    def test_fmt_minutes(self):
        self.assertEqual(s.fmt_minutes(45), "45 min")
        self.assertEqual(s.fmt_minutes(120), "2 h")
        self.assertEqual(s.fmt_minutes(91), "1 h 31 min")

    def test_host_key(self):
        self.assertEqual(s.host_key("Office-Mac.local"), s.host_key(" office-mac "))

    def test_quiet_hours_wrap_past_midnight(self):
        start, end = s.parse_hhmm("23:00"), s.parse_hhmm("07:00")
        self.assertTrue(s.in_quiet_hours(at(23, 30), start, end))
        self.assertTrue(s.in_quiet_hours(at(6, 59), start, end))
        self.assertFalse(s.in_quiet_hours(at(7, 0), start, end))
        self.assertFalse(s.in_quiet_hours(at(12, 0), start, end))

    def test_window_same_day(self):
        on, off = s.parse_hhmm("07:00"), s.parse_hhmm("18:00")
        self.assertEqual(s.window_at(at(9), on, off), (at(7), at(18)))
        self.assertIsNone(s.window_at(at(6, 59), on, off))
        self.assertIsNone(s.window_at(at(18), on, off))

    def test_window_past_midnight(self):
        on, off = s.parse_hhmm("22:00"), s.parse_hhmm("02:00")
        self.assertEqual(s.window_at(at(23), on, off), (at(22), at(2) + timedelta(days=1)))
        self.assertEqual(s.window_at(at(1), on, off), (at(22) - timedelta(days=1), at(2)))
        self.assertIsNone(s.window_at(at(12), on, off))


class PingPresence(unittest.TestCase):
    def poll(self, presence, reply):
        """reply: a TTL for an answered ping, or None for no answer."""
        result = (SimpleNamespace(returncode=0, stdout=f"64 bytes from x: icmp_seq=1 ttl={reply}")
                  if reply is not None else SimpleNamespace(returncode=1, stdout=""))
        with mock.patch.object(s, "network_up", return_value=True), \
                mock.patch.object(s, "run", return_value=result):
            return presence.poll()

    def test_awake_reply_decides_at_once(self):
        p = s.Presence("office-mac.local", absent_after=3, asleep_ttl=40)
        self.assertIs(self.poll(p, 64), True)
        self.assertIsNone(self.poll(p, 64))  # no change, nothing to report

    def test_low_ttl_means_asleep_at_once(self):
        p = s.Presence("office-mac.local", absent_after=3, asleep_ttl=40)
        self.poll(p, 64)
        self.assertIs(self.poll(p, 32), False)

    def test_silence_needs_several_misses(self):
        p = s.Presence("office-mac.local", absent_after=3, asleep_ttl=40)
        self.poll(p, 64)
        self.assertIsNone(self.poll(p, None))
        self.assertIsNone(self.poll(p, None))
        self.assertIs(self.poll(p, None), False)

    def test_no_judgement_while_the_pi_is_offline(self):
        p = s.Presence("office-mac.local", absent_after=1, asleep_ttl=40)
        with mock.patch.object(s, "network_up", return_value=False), \
                mock.patch.object(s, "run") as run:
            self.assertIsNone(p.poll())
            run.assert_not_called()


class ControllerCase(unittest.TestCase):
    host = "office-mac.local"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(s, "CONFIG_PATH", Path(tmp.name) / "screenloop.ini")
        patch.start()
        self.addCleanup(patch.stop)
        cfg = configparser.ConfigParser()
        cfg.read_dict(s.DEFAULTS)
        cfg["video"]["file"] = str(Path(tmp.name) / "missing.mp4")  # length falls back to 45 min
        cfg["presence"]["host"] = self.host
        self.c = s.Controller(cfg)

    def playing(self):
        return self.c.tick()[0]


class Sessions(ControllerCase):
    def test_default_length_is_loops_times_video_length(self):
        self.assertEqual(self.c.default_minutes, 90)  # 2 loops of 45 min

    def test_play_and_stop(self):
        self.c.play(60)
        self.assertTrue(self.playing())
        self.assertEqual(self.c.status()["state"], "playing")
        self.c.stop()
        self.assertFalse(self.playing())

    def test_timer_runs_out(self):
        self.c.play(60)
        self.c.until = datetime.now() - timedelta(seconds=1)
        self.assertFalse(self.playing())
        self.assertEqual(self.c.reason, "Timer ran out")

    def test_missing_video_is_reported(self):
        self.assertIn("file not found", self.c.status()["video"])


class HelperHandover(ControllerCase):
    """A Mac's helper reports its screen state; the video steps aside and comes back."""

    def test_screen_on_pauses_and_screen_off_resumes_with_a_fresh_timer(self):
        self.c.on_helper("Office-Mac", False)       # screen off: a session starts
        self.assertTrue(self.playing())
        self.c.until = datetime.now() + timedelta(minutes=5)  # nearly over

        self.c.on_helper("Office-Mac", True)        # someone wakes the Mac
        self.assertFalse(self.playing())
        self.assertEqual(self.c.status()["state"], "paused")

        self.c.on_helper("Office-Mac", False)       # and leaves again
        self.assertTrue(self.playing())
        self.assertGreater(self.c.until, datetime.now() + timedelta(minutes=80))

    def test_stale_helper_counts_as_screen_off(self):
        self.c.on_helper("Office-Mac", True)
        self.assertTrue(self.c.in_use)
        self.c.helpers[s.host_key("Office-Mac")]["seen"] -= self.c.helper_timeout + 1
        self.c.update_presence()
        self.assertFalse(self.c.in_use)

    def test_a_second_mac_also_interrupts(self):
        self.c.on_helper("Office-Mac", False)
        self.assertTrue(self.playing())
        self.c.on_helper("Laptop", True)
        self.assertFalse(self.playing())

    def test_no_new_session_in_quiet_hours(self):
        self.c.on_helper("Office-Mac", True)
        with mock.patch.object(self.c, "quiet_now", return_value=True):
            self.c.on_helper("Office-Mac", False)
            self.assertFalse(self.playing())


class PingOnly(ControllerCase):
    """Without a helper, short wake-ups (macOS "dark wakes") must not restart the video."""

    def ping(self, awake):
        self.c.presence.present = awake
        self.c.update_presence()

    def test_sleep_starts_a_session(self):
        self.ping(False)
        self.assertTrue(self.playing())

    def test_short_wake_does_not_restart_the_video(self):
        self.ping(False)
        self.c.stop()
        self.ping(True)
        self.c.present_since = datetime.now() - timedelta(minutes=2)  # awake for 2 min
        self.ping(False)
        self.assertFalse(self.playing())

    def test_long_wake_does_restart_the_video(self):
        self.ping(False)
        self.c.stop()
        self.ping(True)
        self.c.present_since = datetime.now() - timedelta(minutes=10)
        self.ping(False)
        self.assertTrue(self.playing())

    def test_wake_just_after_sleeping_must_last_before_the_video_steps_aside(self):
        self.ping(False)
        self.assertTrue(self.playing())
        self.ping(True)                      # within confirm_wake_window of sleeping
        self.assertTrue(self.playing())      # not handed back yet
        self.c.wake_seen = datetime.now() - self.c.confirm_for
        self.assertFalse(self.playing())     # it stayed awake: hand the screen back


class DailySchedule(ControllerCase):
    def around_now(self, before, after):
        now = datetime.now()
        return ((now - timedelta(minutes=before)).strftime("%H:%M"),
                (now + timedelta(minutes=after)).strftime("%H:%M"))

    def test_rejects_bad_input(self):
        self.assertIsNotNone(self.c.set_schedule("maybe"))
        self.assertIsNotNone(self.c.set_schedule("yes", "25:00", "18:00"))
        self.assertIsNotNone(self.c.set_schedule("yes", "07:00", "07:00"))

    def test_is_saved_and_reloaded(self):
        self.assertIsNone(self.c.set_schedule("yes", "07:00", "18:00"))
        again = s.Controller(self.c.cfg)
        self.assertEqual(again.schedule, {"enabled": True, "on": "07:00", "off": "18:00"})

    def test_window_starts_a_session_that_lasts_until_the_off_time(self):
        self.c.set_schedule("yes", *self.around_now(60, 120))
        self.assertTrue(self.playing())
        self.assertAlmostEqual(self.c.until, self.c.window_end, delta=timedelta(seconds=1))

    def test_stop_lasts_for_the_rest_of_the_window(self):
        self.c.set_schedule("yes", *self.around_now(60, 120))
        self.playing()
        self.c.stop()
        self.assertFalse(self.playing())
        self.assertFalse(self.playing())

    def test_switching_the_schedule_off_stops_at_once(self):
        self.c.set_schedule("yes", *self.around_now(60, 120))
        self.assertTrue(self.playing())
        self.c.set_schedule("no")
        self.assertFalse(self.playing())

    def test_moving_the_off_time_before_now_stops_at_once(self):
        on, off = self.around_now(60, 120)
        self.c.set_schedule("yes", on, off)
        self.assertTrue(self.playing())
        self.c.set_schedule(off=self.around_now(10, 0)[0])
        self.assertFalse(self.playing())

    def test_waits_for_the_computer_to_stop_using_the_screen(self):
        self.c.on_helper("Office-Mac", True)
        self.c.set_schedule("yes", *self.around_now(60, 120))
        self.assertFalse(self.playing())
        self.assertIn("back until", self.c.status()["summary"])
        self.c.on_helper("Office-Mac", False)
        self.assertTrue(self.playing())


if __name__ == "__main__":
    unittest.main()
