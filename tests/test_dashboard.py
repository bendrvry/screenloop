"""Tests for the dashboard's data: feed parsing, story picking, weather and the moon.

Run from the repository root:  python3 -m unittest discover tests
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dashboard as d  # noqa: E402

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example News</title>
  <item><title>Older story</title><description>First. Second.</description>
    <pubDate>Tue, 10 Mar 2026 06:00:00 GMT</pubDate></item>
  <item><title>Newer story</title>
    <description><![CDATA[<p>One sentence &amp; more. Two sentences. Three.</p>]]></description>
    <pubDate>Tue, 10 Mar 2026 07:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Blog</title>
  <entry><title>Atom entry</title><summary>Short summary.</summary>
    <updated>2026-03-10T06:30:00Z</updated></entry>
  <entry><title>Newer story</title><updated>2026-03-10T05:00:00Z</updated></entry>
</feed>"""


class Feeds(unittest.TestCase):
    def test_rss(self):
        title, items = d.parse_feed(RSS)
        self.assertEqual(title, "Example News")
        self.assertEqual([i["headline"] for i in items], ["Older story", "Newer story"])
        self.assertEqual(items[1]["summary"], "One sentence & more. Two sentences. Three.")
        self.assertEqual(items[1]["published"],
                         datetime(2026, 3, 10, 7, tzinfo=timezone.utc).timestamp())

    def test_atom(self):
        title, items = d.parse_feed(ATOM)
        self.assertEqual(title, "Example Blog")
        self.assertEqual(items[0]["summary"], "Short summary.")
        self.assertEqual(items[0]["publisher"], "Example Blog")

    def test_pick_news_sorts_newest_first_and_drops_repeats(self):
        items = d.parse_feed(RSS)[1] + d.parse_feed(ATOM)[1]
        news = d.pick_news(items)
        self.assertEqual(news["lead"]["headline"], "Newer story")
        self.assertEqual(news["lead"]["summary"], "One sentence & more. Two sentences.")
        self.assertEqual([b["headline"] for b in news["briefs"]], ["Atom entry", "Older story"])
        self.assertEqual(news["count"], 3)

    def test_pick_news_needs_a_story(self):
        with self.assertRaises(ValueError):
            d.pick_news([])

    def test_condense_limits_length(self):
        self.assertLessEqual(len(d.condense("word " * 200)), 301)


class Weather(unittest.TestCase):
    def test_url_carries_units(self):
        url = d.weather_url("40.7", "-74.0", "fahrenheit", "kmh")
        self.assertIn("temperature_unit=fahrenheit", url)
        self.assertIn("wind_speed_unit=kmh", url)
        self.assertIn("latitude=40.7", url)


class Moon(unittest.TestCase):
    def test_known_full_moon(self):
        m = d.moon(datetime(2026, 3, 3, 12, tzinfo=timezone.utc))
        self.assertEqual(m["name"], "Full Moon")
        self.assertGreater(m["lit"], 0.97)

    def test_known_new_moon(self):
        m = d.moon(datetime(2026, 3, 19, 2, tzinfo=timezone.utc))
        self.assertEqual(m["name"], "New Moon")
        self.assertLess(m["lit"], 0.03)


if __name__ == "__main__":
    unittest.main()
