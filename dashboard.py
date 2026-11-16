"""Data for screenloop's dashboard: the weather, news headlines, and the moon.

Each source is fetched on its own and cached on disk, so a failed fetch leaves the
last good data on screen. Fetching only happens while a page is asking for data.
"""

import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

log = logging.getLogger("screenloop")

WEATHER_URL = "https://api.open-meteo.com/v1/forecast?"
WEATHER_EVERY = 15 * 60
NEWS_EVERY = 10 * 60
SYNODIC_MONTH = 29.530588853
KNOWN_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
WIND_LABELS = {"mph": "mph", "kmh": "km/h", "ms": "m/s", "kn": "kn"}


def fetch(url, timeout=15):
    request = urllib.request.Request(url, headers={"User-Agent": "screenloop-dashboard"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def weather_url(lat, lon, temperature_unit="celsius", wind_unit="mph"):
    return WEATHER_URL + urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,is_day,"
                   "wind_speed_10m,relative_humidity_2m",
        "hourly": "temperature_2m,weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
                 "precipitation_probability_max,uv_index_max",
        "timezone": "auto", "forecast_days": 7,
        "temperature_unit": temperature_unit, "wind_speed_unit": wind_unit,
    }, safe=",")


def parse_weather(raw):
    cur, hourly, daily = raw["current"], raw["hourly"], raw["daily"]
    now_hour = cur["time"][:13]
    start = next((i for i, t in enumerate(hourly["time"]) if t[:13] >= now_hour), 0)
    hours = [{"time": hourly["time"][i], "temp": hourly["temperature_2m"][i],
              "code": hourly["weather_code"][i], "day": bool(hourly["is_day"][i])}
             for i in range(start, min(start + 16, len(hourly["time"])))]
    days = [{"date": daily["time"][i], "code": daily["weather_code"][i],
             "hi": daily["temperature_2m_max"][i], "lo": daily["temperature_2m_min"][i],
             "sunrise": daily["sunrise"][i], "sunset": daily["sunset"][i],
             "rain": daily["precipitation_probability_max"][i], "uv": daily["uv_index_max"][i]}
            for i in range(len(daily["time"]))]
    return {
        "current": {"time": cur["time"], "temp": cur["temperature_2m"],
                    "feels": cur["apparent_temperature"], "code": cur["weather_code"],
                    "day": bool(cur["is_day"]), "wind": cur["wind_speed_10m"],
                    "humidity": cur["relative_humidity_2m"]},
        "hours": hours, "days": days,
    }


def tidy(text):
    """Plain text from a feed field that may hold HTML."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def condense(summary, sentences=2, limit=300):
    """The first sentences of a summary, for a screen read from across the room."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“‘])", tidy(summary))
    out = ""
    for part in parts[:sentences]:
        if out and len(out) + len(part) + 1 > limit:
            break
        out = f"{out} {part}".strip()
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0] + "…"
    return out


def local(tag):
    """An XML tag without its namespace: '{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1]


def child_text(node, *names):
    for child in node:
        if local(child.tag) in names and (child.text or "").strip():
            return child.text
    return ""


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        when = parsedate_to_datetime(text)  # RSS: "Tue, 10 Mar 2026 07:12:00 GMT"
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def parse_feed(text):
    """Headlines from an RSS or Atom feed, as (feed title, items)."""
    root = ET.fromstring(text)
    channel = next((c for c in root if local(c.tag) == "channel"), root)  # RSS has a channel
    title = tidy(child_text(channel, "title"))
    items = []
    for node in channel.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        headline = tidy(child_text(node, "title"))
        if not headline:
            continue
        items.append({
            "publisher": title,
            "headline": headline,
            "summary": tidy(child_text(node, "description", "summary", "content", "encoded")),
            "published": parse_date(child_text(node, "pubDate", "published", "updated", "date")),
        })
    return title, items


def pick_news(items):
    """The lead story with its summary, four more headlines, and four in brief."""
    if not items:
        raise ValueError("no stories found in the feeds")
    # Newest first; stories without a date keep their place behind the dated ones.
    items = sorted(items, key=lambda i: -(i["published"] or 0))
    seen, unique = set(), []
    for item in items:
        key = item["headline"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    lead = unique[0]
    return {
        "count": len(unique),
        "lead": {"publisher": lead["publisher"], "headline": lead["headline"],
                 "summary": condense(lead["summary"]), "published": lead["published"]},
        "briefs": [{"publisher": i["publisher"], "headline": i["headline"],
                    "published": i["published"]} for i in unique[1:5]],
        "noted": [i["headline"] for i in unique[5:9]],
    }


def moon(now=None):
    now = now or datetime.now(timezone.utc)
    age = ((now - KNOWN_NEW_MOON).total_seconds() / 86400) % SYNODIC_MONTH
    phase = age / SYNODIC_MONTH  # 0 new, 0.5 full
    lit = (1 - math.cos(2 * math.pi * phase)) / 2
    names = ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
             "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"]
    # The four named moments get a day either side; the rest are the in-between phases.
    window = 1 / SYNODIC_MONTH
    name = names[1 + 2 * int(phase * 4)]
    for index, at in ((0, 0.0), (2, 0.25), (4, 0.5), (6, 0.75), (0, 1.0)):
        if abs(phase - at) < window:
            name = names[index]
    events = [(0.25, "First quarter"), (0.5, "Full moon"), (0.75, "Last quarter"), (1.0, "New moon")]
    next_at, next_name = next((at, n) for at, n in events if at > phase + 1e-9)
    return {"name": name, "lit": round(lit, 3), "waxing": phase < 0.5,
            "next": next_name, "next_days": round((next_at - phase) * SYNODIC_MONTH, 1)}


class DashboardData:
    def __init__(self, settings, cache_path):
        self.place = settings.get("place", "London")
        self.lat = settings.get("latitude", "51.5074")
        self.lon = settings.get("longitude", "-0.1278")
        self.temperature_unit = settings.get("temperature_unit", "celsius").strip() or "celsius"
        self.wind_unit = settings.get("wind_unit", "mph").strip() or "mph"
        self.feeds = settings.get("news_feeds", "").split()
        self.news_title = settings.get("news_title", "Headlines").strip() or "Headlines"
        self.effects = settings.get("effects", "full")
        self.cache_path = Path(cache_path)
        self.lock = threading.Lock()
        self.busy = set()
        self.tried = {}  # source -> time.time() of the last attempt
        try:
            self.cache = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            self.cache = {}

    def save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.cache, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.cache_path)

    def refresh(self, source, every, work):
        """Run work() in the background if this source is due and not already running."""
        with self.lock:
            if source in self.busy or time.time() - self.tried.get(source, 0) < every:
                return
            self.busy.add(source)
            self.tried[source] = time.time()

        def run():
            try:
                result = work()
                if result is not None:
                    with self.lock:
                        self.cache[source] = {**result, "fetched": time.time()}
                        self.save()
            except Exception as e:  # a bad fetch must never take the dashboard down
                log.warning("dashboard: %s not updated: %s", source, e)
            finally:
                with self.lock:
                    self.busy.discard(source)

        threading.Thread(target=run, daemon=True).start()

    def get_weather(self):
        url = weather_url(self.lat, self.lon, self.temperature_unit, self.wind_unit)
        return parse_weather(json.loads(fetch(url)))

    def get_news(self):
        items = []
        for url in self.feeds:
            try:
                _, found = parse_feed(fetch(url))
                items += found
            except Exception as e:  # one broken feed shouldn't hide the others
                log.warning("dashboard: feed %s not read: %s", url, e)
        return pick_news(items)

    def snapshot(self):
        """What the page shows now; also starts any fetch that is due."""
        self.refresh("weather", WEATHER_EVERY, self.get_weather)
        if self.feeds:
            self.refresh("news", NEWS_EVERY, self.get_news)
        with self.lock:
            return {
                "place": self.place, "effects": self.effects,
                "units": {"temperature": "°F" if self.temperature_unit == "fahrenheit" else "°C",
                          "wind": WIND_LABELS.get(self.wind_unit, self.wind_unit)},
                "weather": self.cache.get("weather"),
                "news": self.cache.get("news"),
                "news_title": self.news_title,
                "news_configured": bool(self.feeds),
                "moon": moon(),
            }
