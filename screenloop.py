#!/usr/bin/env python3
"""screenloop: loop a video on a Raspberry Pi's HDMI output while no other computer
is using the monitor, then switch the output fully off so the monitor's automatic
input switching can fall back to another computer or go to sleep.

Runs as a systemd user service inside the Pi's labwc (Wayland) session and
serves a small control page for phones and iPhone Shortcuts.
"""

import collections
import configparser
import json
import logging
import os
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dashboard

log = logging.getLogger("screenloop")

CONFIG_PATH = Path(os.environ.get(
    "SCREENLOOP_CONFIG", Path.home() / ".config/screenloop/screenloop.ini"))

DEFAULTS = {
    "video": {"file": "~/videos/display.mp4", "crop": "", "audio": "yes",
              "audio_device": "auto", "random_start": "yes", "mpv_args": ""},
    "schedule": {"loops": "2", "quiet_start": "", "quiet_end": "", "keep_playing_hours": "10"},
    "presence": {"host": "", "interval": "2", "absent_after": "3", "asleep_ttl": "40",
                 "rearm_after_minutes": "4", "confirm_wake_window": "120",
                 "confirm_wake_seconds": "30", "helper_timeout": "35",
                 "helper_memory_days": "7"},
    "display": {"output": "auto", "resolution": "auto", "monitor_off_seconds": "5"},
    "monitor": {"input_command": ""},
    "web": {"port": "8080"},
    # The optional dashboard: off unless both times are set.
    "dashboard": {"start": "", "end": "", "place": "London", "latitude": "51.5074",
                  "longitude": "-0.1278", "temperature_unit": "celsius", "wind_unit": "mph",
                  "news_feeds": "", "news_title": "Headlines", "effects": "full",
                  "browser": "auto", "browser_args": ""},
}


class RecentLog(logging.Handler):
    """Keeps the latest log lines for GET /api/log, so they can be read without SSH."""

    def __init__(self, size=1000):
        super().__init__()
        self.lines = collections.deque(maxlen=size)

    def emit(self, record):
        self.lines.append(f"{datetime.fromtimestamp(record.created):%Y-%m-%d %H:%M:%S} "
                          f"{record.getMessage()}")


recent_log = RecentLog()


def host_key(name):
    """Compare machine names loosely: "Office-Mac.local" matches "office-mac"."""
    return name.strip().lower().removesuffix(".local")


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("%s failed: %s", cmd[0], e)
        return None


def parse_hhmm(text):
    hours, minutes = (int(x) for x in text.strip().split(":"))
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(text)
    return hours * 60 + minutes


def window_at(now, on, off):
    """The daily on→off window (minutes past midnight) containing now, as (start, end)."""
    t = now.hour * 60 + now.minute
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if on < off:
        if on <= t < off:
            return midnight + timedelta(minutes=on), midnight + timedelta(minutes=off)
    elif t >= on:  # window runs past midnight
        return midnight + timedelta(minutes=on), midnight + timedelta(days=1, minutes=off)
    elif t < off:
        return midnight - timedelta(days=1) + timedelta(minutes=on), midnight + timedelta(minutes=off)
    return None


def save_json(path, data):
    """Write atomically and durably; the Pi may lose power at any moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def in_quiet_hours(now, start, end):
    t = now.hour * 60 + now.minute
    if start < end:
        return start <= t < end
    return t >= start or t < end  # wraps past midnight


def fmt_minutes(minutes):
    hours, minutes = divmod(int(round(minutes)), 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


def probe_duration(video):
    if not video.is_file():
        log.error("video file %s not found; put a video there or change 'file' in %s",
                  video, CONFIG_PATH)
        return 45 * 60.0
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)], timeout=30)
    try:
        return float(r.stdout.strip())
    except (AttributeError, ValueError):
        log.warning("could not read the length of %s; assuming 45 min", video)
        return 45 * 60.0


def hdmi_audio_device():
    """mpv's name for the HDMI sound output (the Pi's default is its headphone jack)."""
    r = run(["pw-dump"])
    try:
        objects = json.loads(r.stdout)
    except (AttributeError, ValueError):
        return None
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Sink" and "hdmi" in props.get("node.name", ""):
            return "pipewire/" + props["node.name"]
    return None


class Display:
    """The Pi's HDMI output.

    Switched off by forcing the port to look unplugged, so the monitor sees no signal
    and its wake-up hotplug flicker is ignored. Switching the output off in the
    compositor instead leaves it stuck when the monitor re-announces itself.
    Switched on by letting the port be detected again, like replugging the cable.
    """

    def __init__(self, output, resolution):
        self.output = output
        self.resolution = resolution
        self.forced = None  # sysfs dir of the port we forced off

    def ports(self):
        for status in sorted(Path("/sys/class/drm").glob("card*-HDMI-A-*/status")):
            name = status.parent.name.split("-", 1)[1]  # card1-HDMI-A-1 -> HDMI-A-1
            if self.output in ("auto", name):
                yield status.parent, name

    def connector(self):
        """Return (sysfs dir, output name) of the HDMI port in use, or (None, None)."""
        for path, name in self.ports():
            if path == self.forced or (path / "status").read_text().strip() == "connected":
                return path, name
        return None, None

    def force(self, path, state):
        try:
            (path / "status").write_text(state)
            return True
        except OSError as e:
            log.warning("can't force %s %s (run install-root.sh): %s", path.name, state, e)
            return False

    def reset(self):
        """Undo any forced-off state left behind by a previous run."""
        for path, _ in self.ports():
            try:
                (path / "status").write_text("detect")
            except OSError:
                pass

    def wait_enabled(self, path, seconds=10):
        for _ in range(int(seconds * 2)):
            if (path / "enabled").read_text().strip() == "enabled":
                return True
            time.sleep(0.5)
        return False

    def randr(self, name, args):
        r = run(["wlr-randr", "--output", name, *args])
        if r is None or r.returncode != 0:
            log.warning("wlr-randr %s failed: %s", " ".join(args), r and r.stderr.strip())
            return False
        return True

    def is_on(self):
        path, _ = self.connector()
        # Right after forcing the port off, sysfs still says "enabled" for a moment.
        return (path is not None and path != self.forced
                and (path / "enabled").read_text().strip() == "enabled")

    def mode_args(self, name):
        """Pick the highest refresh rate available at the configured resolution."""
        if self.resolution.strip().lower() in ("", "auto"):
            return ["--preferred"]
        r = run(["wlr-randr", "--json"])
        try:
            heads = json.loads(r.stdout)
            width, height = (int(x) for x in self.resolution.lower().split("x"))
        except (AttributeError, ValueError):
            return ["--preferred"]
        for head in heads:
            modes = [m for m in head.get("modes", [])
                     if head["name"] == name and (m["width"], m["height"]) == (width, height)]
            if modes:
                best = max(modes, key=lambda m: m["refresh"])
                return ["--mode", f"{width}x{height}@{best['refresh']:.6f}Hz"]
        return ["--preferred"]

    def set(self, on):
        path, name = self.connector()
        if name is None:
            return False
        if not on:
            if self.force(path, "off"):
                self.forced = path
            elif not self.randr(name, ["--off"]):
                return False
            log.info("HDMI output %s off", name)
            return True
        if self.forced is not None:
            self.force(self.forced, "detect")
            self.forced = None
            self.wait_enabled(path)
        mode = self.mode_args(name)
        if not self.randr(name, ["--on", *mode]):
            # The compositor can get stuck on this output; a software replug clears it.
            log.info("replugging %s in software", name)
            if not (self.force(path, "off") and self.force(path, "detect")):
                return False
            self.wait_enabled(path)
            if not self.randr(name, ["--on", *mode]):
                return False
        log.info("HDMI output %s on %s", name, " ".join(mode))
        return True


class Player:
    def __init__(self, video, duration, settings):
        self.video = video
        self.duration = duration
        self.settings = settings
        self.proc = None
        self.last_start = 0.0
        self.resume_at = None  # where to pick up when the same session comes back
        self.missing = False   # the video file wasn't there at the last start
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        self.ipc_path = runtime / "screenloop-mpv.sock"

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def ipc(self, *command):
        """Send one command to mpv's control socket; return its reply data, or None."""
        if not self.running():
            return None
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(2)
                s.connect(str(self.ipc_path))
                s.sendall(json.dumps({"command": list(command)}).encode() + b"\n")
                buf = b""
                while chunk := s.recv(4096):
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        reply = json.loads(line)
                        if "error" in reply:  # skip mpv's unrelated event messages
                            return reply.get("data") if reply["error"] == "success" else None
        except (OSError, ValueError):
            pass
        return None

    def start(self, output_name):
        if self.running():
            return
        if time.monotonic() - self.last_start < 10:  # don't spin if mpv keeps crashing
            return
        self.last_start = time.monotonic()
        if not self.video.is_file():
            if not self.missing:
                log.error("video file %s not found; nothing to play", self.video)
            self.missing = True
            return
        self.missing = False
        v = self.settings
        # The hardware decoder hands frames straight to the compositor (dmabuf-wayland).
        # On a Pi 4 at ultrawide resolutions, mpv's usual GPU output never refreshes the screen.
        cmd = ["mpv", "--really-quiet", "--fullscreen", f"--fs-screen-name={output_name}",
               "--loop-file=inf", "--no-osc", "--osd-level=0", "--no-input-default-bindings",
               "--cursor-autohide=always", "--stop-screensaver",
               "--vo=dmabuf-wayland", "--hwdec=v4l2m2m"]
        if v["crop"].strip():
            width, height, x, y = v["crop"].strip().split(":")
            cmd.append(f"--video-crop={width}x{height}+{x}+{y}")
        cmd.append(f"--input-ipc-server={self.ipc_path}")
        if v.getboolean("audio"):
            # Sound always starts muted; the control page can unmute it.
            cmd += ["--mute=yes", "--volume=100"]
            device = hdmi_audio_device() if v["audio_device"] == "auto" else v["audio_device"]
            if device:
                cmd.append(f"--audio-device={device}")
        else:
            cmd.append("--no-audio")
        if self.resume_at is not None:
            cmd.append(f"--start={self.resume_at:.1f}")
        elif v.getboolean("random_start") and self.duration > 120:
            cmd.append(f"--start={random.randint(0, int(self.duration) - 60)}")
        cmd += v["mpv_args"].split()
        cmd.append(str(self.video))
        log.info("starting mpv%s", f" at {self.resume_at:.0f}s" if self.resume_at is not None else "")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)

    def checkpoint(self):
        """Remember the position, so the video resumes there after any interruption."""
        position = self.ipc("get_property", "time-pos")
        if isinstance(position, (int, float)):
            self.resume_at = position

    def stop(self):
        if self.running():
            self.checkpoint()
            log.info("stopping mpv")
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None


class DashboardPlayer:
    """The dashboard: a full-screen browser showing the page this program serves."""

    def __init__(self, settings, port):
        self.settings = settings
        self.url = f"http://localhost:{port}/dashboard"
        self.proc = None
        self.last_start = 0.0
        self.missing = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def browser(self):
        name = self.settings["browser"].strip()
        names = ["chromium", "chromium-browser"] if name in ("", "auto") else [name]
        return next((path for n in names if (path := shutil.which(n))), None)

    def start(self):
        if self.running():
            return
        if time.monotonic() - self.last_start < 15:  # don't spin if the browser keeps crashing
            return
        self.last_start = time.monotonic()
        browser = self.browser()
        if browser is None:
            if not self.missing:
                log.error("no Chromium browser found; the dashboard can't be shown")
            self.missing = True
            return
        self.missing = False
        profile = Path.home() / ".cache/screenloop/browser"
        cmd = [browser, "--kiosk", f"--app={self.url}", "--ozone-platform=wayland",
               f"--user-data-dir={profile}", "--no-first-run", "--noerrdialogs",
               "--disable-infobars", "--disable-session-crashed-bubble", "--hide-scrollbars",
               "--password-store=basic", "--disable-features=Translate",
               *shlex.split(self.settings["browser_args"])]
        log.info("starting the dashboard browser")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)

    def stop(self):
        if self.running():
            log.info("stopping the dashboard browser")
            # Chromium runs as several processes; stop the whole group.
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
            except OSError:
                pass
        self.proc = None


def network_up():
    """True if the Pi can reach its router, so a silent computer really means it's asleep."""
    r = run(["ip", "-4", "route", "show", "default"])
    fields = r.stdout.split() if r is not None and r.returncode == 0 else []
    if "via" not in fields:
        return False
    gateway = fields[fields.index("via") + 1]
    if (r := run(["ping", "-c", "1", "-W", "1", "-q", gateway], timeout=5)) and r.returncode == 0:
        return True
    # Some routers ignore pings; a resolved ARP entry still proves the link works.
    r = run(["ip", "neigh", "show", gateway])
    return r is not None and "lladdr" in r.stdout and "FAILED" not in r.stdout


class Presence:
    """Whether the watched computer is awake, judged from its ping replies.

    A sleeping Mac's network chip keeps answering pings, but with TTL 32 instead of
    macOS's 64, so a low-TTL reply means asleep. Awake and asleep replies are decided
    at once; unanswered pings only count after several in a row.
    """

    def __init__(self, host, absent_after, asleep_ttl):
        self.host = host
        self.absent_after = absent_after
        self.asleep_ttl = asleep_ttl
        self.misses = 0
        self.present = None
        self.online = None

    def poll(self):
        """Ping once; return the new state if it changed, else None."""
        online = network_up()
        if online != self.online:
            log.info("the Pi is %s", "online" if online else "offline; not judging " + self.host)
            self.online = online
        if not online:
            return None
        r = run(["ping", "-c", "1", "-W", "1", self.host], timeout=8)
        ttl = None
        if r is not None and r.returncode == 0:
            m = re.search(r"ttl=(\d+)", r.stdout)
            ttl = int(m.group(1)) if m else 64
        if ttl is not None and ttl > self.asleep_ttl:
            self.misses, new = 0, True
        else:
            # A low-TTL reply only ever comes from a sleeping Mac's network chip.
            self.misses = self.absent_after if ttl is not None else self.misses + 1
            if self.misses < self.absent_after:
                return None
            new = False
        if new == self.present:
            return None
        self.present = new
        return new


class Controller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.video = Path(cfg["video"]["file"]).expanduser()
        self.duration = probe_duration(self.video)
        self.default_minutes = max(1, round(cfg["schedule"].getint("loops") * self.duration / 60))
        s = cfg["schedule"]
        self.quiet = ((parse_hhmm(s["quiet_start"]), parse_hhmm(s["quiet_end"]))
                      if s["quiet_start"].strip() and s["quiet_end"].strip() else None)
        self.keep_minutes = s.getfloat("keep_playing_hours") * 60
        # The daily schedule is changed from the control page, so it lives in its own file.
        self.schedule_path = CONFIG_PATH.with_name("schedule.json")
        self.schedule = {"enabled": False, "on": "07:00", "off": "18:00"}
        try:
            self.schedule.update(json.loads(self.schedule_path.read_text()))
        except (OSError, ValueError):
            pass
        p = cfg["presence"]
        self.presence = Presence(p["host"].strip(), p.getint("absent_after"), p.getint("asleep_ttl"))
        self.poll_interval = p.getfloat("interval")
        self.rearm_after = timedelta(minutes=p.getfloat("rearm_after_minutes"))
        self.confirm_window = timedelta(seconds=p.getfloat("confirm_wake_window"))
        self.confirm_for = timedelta(seconds=p.getfloat("confirm_wake_seconds"))
        self.helper_timeout = p.getfloat("helper_timeout")
        self.helper_memory = p.getfloat("helper_memory_days") * 86400
        self.watched = host_key(self.presence.host) if self.presence.host else None
        # Macs running the helper (mac/install-mac.sh), and when each was last heard from.
        # Saved, so a Pi restart keeps trusting the helper instead of the pings.
        self.helpers_path = CONFIG_PATH.with_name("helpers.json")
        try:
            self.helpers_known = dict(json.loads(self.helpers_path.read_text()))
        except (OSError, ValueError, TypeError):
            self.helpers_known = {}
        self.helpers = {}  # name -> {"on": screen on?, "seen": time.monotonic() of last report}
        self.started = time.monotonic()
        self.display = Display(cfg["display"]["output"], cfg["display"]["resolution"])
        self.monitor_off_after = cfg["display"].getfloat("monitor_off_seconds")
        self.disconnected_since = None  # when the Pi's HDMI port lost the monitor
        self.connected = None
        self.player = Player(self.video, self.duration, cfg["video"])
        # The optional dashboard replaces the video between these times.
        d = cfg["dashboard"]
        self.dash_times = ((parse_hhmm(d["start"]), parse_hhmm(d["end"]))
                           if d["start"].strip() and d["end"].strip() else None)
        self.dash_data = dashboard.DashboardData(d, Path.home() / ".cache/screenloop/dashboard.json")
        self.dash_player = DashboardPlayer(d, cfg["web"]["port"])
        self.dash_stopped = None  # start of a dashboard window the user pressed Stop in
        self.dash_showing = None  # for the log: was the dashboard showing at the last look?

        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.running = True
        # Session state, guarded by self.lock.
        self.until = None          # when the current play session ends
        self.length = self.default_minutes  # minutes the session runs each time it (re)starts
        self.paused = False        # session on hold because a computer is using the screen
        self.manual = False        # started from the control page (ignores quiet hours)
        self.reason = "Starting up"
        self.in_use = None         # a Mac is using the monitor (None until known)
        self.present_since = None
        self.asleep_since = None   # when the Mac last stopped using the screen
        self.wake_seen = None      # a wake soon after sleeping, not yet confirmed
        self.switch_input = False  # ask the monitor to show the Pi once playing
        self.window_end = None     # end of the schedule window we're in: everything stops then
        self.stopped_window = None  # start of a schedule window the user pressed Stop in

    def quiet_now(self):
        return self.quiet is not None and in_quiet_hours(datetime.now(), *self.quiet)

    def window(self, now=None):
        """The daily schedule window we're in, as (start, end), or None."""
        sch = self.schedule
        if not sch["enabled"]:
            return None
        return window_at(now or datetime.now(), parse_hhmm(sch["on"]), parse_hhmm(sch["off"]))

    def dash_window(self, now=None):
        """The dashboard window we're in, as (start, end), or None."""
        if self.dash_times is None:
            return None
        return window_at(now or datetime.now(), *self.dash_times)

    def dash_now(self, now=None):
        """True while the dashboard, not the video, owns the screen."""
        w = self.dash_window(now)
        return w is not None and self.dash_stopped != w[0]

    def stopped_for_window(self, now=None):
        w = self.window(now)
        return w is not None and self.stopped_window == w[0]

    # --- session changes (call with self.lock held) ---

    def start_session(self, minutes, reason, manual):
        self.until = datetime.now() + timedelta(minutes=minutes)
        self.length = minutes
        self.paused, self.manual, self.reason = False, manual, reason
        self.wake_seen = None
        self.player.resume_at = None  # a new session picks a fresh starting point
        log.info("playing until %s: %s", f"{self.until:%H:%M}", reason)

    def end_session(self, reason):
        if self.until is not None:
            log.info("off: %s", reason)
        self.until, self.paused, self.manual, self.reason = None, False, False, reason
        self.wake_seen = None

    # --- events ---

    def play(self, minutes=None, reason="Started from the control page"):
        with self.lock:
            self.start_session(minutes or self.default_minutes, reason, True)
            self.switch_input = True
        self.wake.set()

    def keep_playing(self):
        self.play(self.keep_minutes, f"Keep playing (up to {fmt_minutes(self.keep_minutes)})")

    def stop(self):
        with self.lock:
            w = self.window()
            dw = self.dash_window()
            if dw is not None:
                self.dash_stopped = dw[0]  # no dashboard for the rest of this window
            if w is not None:
                # Stop lasts for the rest of today's window; the schedule returns tomorrow.
                self.stopped_window = w[0]
                self.end_session(f"Stopped until the schedule's next start ({self.schedule['on']})")
            else:
                self.end_session("Stopped from the control page")
        self.wake.set()

    def set_schedule(self, enabled=None, on=None, off=None):
        """Change the daily schedule; returns an error message, or None if saved."""
        new = dict(self.schedule)
        if enabled is not None:
            if enabled.lower() not in ("yes", "no", "on", "off", "true", "false", "1", "0"):
                return "enabled must be yes or no"
            new["enabled"] = enabled.lower() in ("yes", "on", "true", "1")
        for key, value in (("on", on), ("off", off)):
            if value is not None:
                try:
                    minutes = parse_hhmm(value)
                except ValueError:
                    return f"{key} must be a time like 07:00"
                new[key] = f"{minutes // 60:02d}:{minutes % 60:02d}"
        if new["on"] == new["off"]:
            return "the on and off times must be different"
        with self.lock:
            in_window = self.window_end is not None
            self.schedule, self.stopped_window = new, None
            if in_window and self.window() is None:
                # The change put us outside the window, so it ends now, not at the old off time.
                self.window_end = None
                self.end_session("Daily schedule changed")
        save_json(self.schedule_path, new)
        log.info("daily schedule %s: on %s, off %s",
                 "enabled" if new["enabled"] else "disabled", new["on"], new["off"])
        self.wake.set()
        return None

    def playback(self):
        """Pause and sound state of the video on screen (it only exists while mpv runs)."""
        on_screen = self.player.running()
        sound_available = self.cfg["video"].getboolean("audio")
        return {
            "on_screen": on_screen,
            "paused": on_screen and bool(self.player.ipc("get_property", "pause")),
            "sound": on_screen and sound_available and self.player.ipc("get_property", "mute") is False,
            "sound_available": sound_available,
        }

    def set_playback(self, what, to):
        """Set "pause" or "sound" to "on", "off" or "toggle". False if no video is on screen."""
        if not self.player.running():
            return False
        prop = "pause" if what == "pause" else "mute"
        if to == "toggle":
            self.player.ipc("cycle", prop)
        else:
            on = to == "on"
            self.player.ipc("set_property", prop, on if what == "pause" else not on)
        log.info("%s %s from the control page", what, to)
        return True

    # --- is a Mac using the monitor? ---
    #
    # With the helper installed, a Mac reports when its screen turns on or off. That's the
    # only reliable signal: macOS wakes in the background ("dark wake") dozens of times a
    # day, and pings can't tell those from a real wake, but the screen stays off.
    # Without the helper, the watched computer is judged by pings alone.

    def helper_known(self, key):
        heard = self.helpers_known.get(key)
        return isinstance(heard, (int, float)) and time.time() - heard < self.helper_memory

    def on_helper(self, name, on):
        """A Mac's helper reported its screen state (called from the web server)."""
        key = host_key(name)
        with self.lock:
            previous = self.helpers.get(key)
            self.helpers[key] = {"on": on, "seen": time.monotonic()}
            if previous is None or previous["on"] != on:
                log.info("%s says its screen is %s", name, "on" if on else "off")
            heard = self.helpers_known.get(key)
            if not isinstance(heard, (int, float)) or time.time() - heard > 6 * 3600:
                self.helpers_known[key] = time.time()  # saved now and then, not every report
                known = dict(self.helpers_known)
            else:
                known = None
        if known is not None:
            save_json(self.helpers_path, known)
        self.update_presence()

    def judge_in_use(self):
        """(in use?, decided by a helper?). in use is None while still unknown. Lock held."""
        now = time.monotonic()
        on = {k for k, h in self.helpers.items() if h["on"] and now - h["seen"] < self.helper_timeout}
        if on:
            return True, True
        if self.watched is not None and self.helper_known(self.watched):
            if (self.watched not in self.helpers and self.presence.present is not False
                    and now - self.started < self.helper_timeout):
                return None, True  # just started: wait for the helper, or a ping saying asleep
            return False, True
        if self.watched is not None:
            # No helper on the watched computer: a reply from its own network stack counts as awake.
            return self.presence.present, False
        return (False, True) if self.helpers else (None, True)

    def update_presence(self):
        with self.lock:
            in_use, trusted = self.judge_in_use()
            if in_use is None or in_use == self.in_use:
                return
            self.in_use = in_use
            log.info("%s", "a computer is using the screen" if in_use else "no computer is using the screen")
            self.presence_changed(in_use, trusted)
        self.wake.set()

    def presence_changed(self, present, trusted):
        """React to a Mac starting or stopping using the screen. Lock held."""
        now = datetime.now()
        if present:
            self.present_since = now
            if self.until is not None and not self.paused:
                if not trusted and self.asleep_since and now - self.asleep_since < self.confirm_window:
                    # Macs often wake for ~20 s on their own just after sleeping;
                    # tick() hands the screen back only if this wake lasts.
                    self.wake_seen = now
                    log.info("%s woke soon after sleeping; checking it stays awake", self.presence.host)
                else:
                    self.paused, self.reason = True, "A computer is using the screen"
            return
        awake_for = now - self.present_since if self.present_since else None
        self.present_since = None
        self.asleep_since = now
        what = "Mac's screen went off" if trusted else f"{self.presence.host} went to sleep"
        if self.dash_now(now) and not self.manual:
            # During the dashboard's hours the dashboard takes the screen; the video doesn't start.
            self.wake_seen = None
            log.info("%s; the dashboard has the screen", what)
        elif self.wake_seen is not None:
            self.wake_seen = None
            log.info("%s went back to sleep after %s; the video carries on", self.presence.host, awake_for)
        elif self.until is not None and not self.paused:
            pass  # already playing (Play was pressed while the Mac was in use)
        elif self.paused and self.until is not None:
            # Coming back gets a fresh timer, so there's always a full session ahead.
            self.paused = False
            self.until = now + timedelta(minutes=self.length)
            self.reason = f"Resumed: {what}"
            log.info("resumed, playing until %s", f"{self.until:%H:%M}")
        elif self.stopped_for_window(now):
            log.info("%s, but the video was stopped for today's schedule", what)
        elif self.quiet_now() and self.window(now) is None:
            log.info("%s during quiet hours; staying off", what)
        elif trusted or awake_for is None or awake_for >= self.rearm_after:
            self.start_session(self.default_minutes, what, False)
        else:
            # A short blip (e.g. a background "dark wake") doesn't restart the video.
            log.info("%s was only awake for %s; not restarting", self.presence.host, awake_for)

    def presence_loop(self):
        while self.running:
            changed = self.presence.poll()
            if changed is not None:
                log.info("%s %s (pings)", self.presence.host, "awake" if changed else "asleep or off")
                if not changed and self.watched in self.helpers:
                    # It's asleep, so its screen is off whatever the helper last said.
                    with self.lock:
                        self.helpers[self.watched]["on"] = False
                self.update_presence()
            time.sleep(self.poll_interval)

    # --- making the hardware match the session ---

    def tick(self):
        with self.lock:
            now = datetime.now()
            if self.wake_seen is not None and now - self.wake_seen >= self.confirm_for:
                self.wake_seen = None
                self.paused, self.reason = True, "A computer is using the screen"
                log.info("%s stayed awake; handing the screen back", self.presence.host)
            # The schedule's off time ends everything, whatever started it.
            if self.window_end is not None and now >= self.window_end:
                self.window_end = None
                self.end_session("Daily schedule ended")
            # During the dashboard's hours only a video started by hand may play.
            dash = self.dash_now(now)
            if dash and self.until is not None and not self.manual:
                self.end_session("Dashboard")
            w = self.window(now)
            if w is not None:
                self.window_end = w[1]
                if (not dash and self.stopped_window != w[0]
                        and (self.until is None or self.until < w[1])):
                    if self.until is None:
                        self.start_session((w[1] - now).total_seconds() / 60, "Daily schedule", False)
                        if self.in_use:
                            # Wait for the Mac to sleep before taking the screen.
                            self.paused, self.reason = True, "A computer is using the screen"
                    else:
                        self.until = w[1]  # stretch whatever is playing to the schedule's end
                        log.info("daily schedule: playing until %s", f"{self.until:%H:%M}")
            if self.until is not None and now >= self.until:
                self.end_session("Timer ran out")
            elif self.until is not None and not self.manual and self.quiet_now() and w is None:
                self.end_session("Quiet hours")
            switch, self.switch_input = self.switch_input, False
            if dash and self.until is None:
                # Shown whenever no computer is using the screen (and not before that's known).
                showing = self.in_use is False
                if showing != self.dash_showing:
                    log.info("dashboard %s", "on" if showing else "waiting: a computer is using the screen")
                self.dash_showing = showing
                return showing, False, "dashboard"
            if self.dash_showing is not None:
                log.info("dashboard over")
                self.dash_showing = None
            return self.until is not None and not self.paused, switch, "video"

    def reconcile(self):
        self.update_presence()  # helper reports go stale if a Mac stops sending them
        want_on, switch_input, mode = self.tick()
        _, name = self.display.connector()
        connected = name is not None
        if connected != self.connected:
            if self.connected is not None:
                log.info("monitor %s the Pi's HDMI port", "back on" if connected else "gone from")
            self.connected = connected
        if not connected:
            # The monitor was switched off (or unplugged). If that lasts while the video
            # should be on screen, treat it like Stop rather than carrying on unseen.
            self.player.stop()
            self.dash_player.stop()
            if not want_on or mode == "dashboard":
                # The dashboard simply comes back with the monitor.
                self.disconnected_since = None
            elif self.disconnected_since is None:
                self.disconnected_since = time.monotonic()
            elif time.monotonic() - self.disconnected_since >= self.monitor_off_after:
                self.disconnected_since = None
                with self.lock:
                    self.end_session("The monitor was switched off")
            return
        self.disconnected_since = None
        if want_on:
            if not self.display.is_on():
                if not self.display.set(True):
                    return
                time.sleep(2)
                if self.wake.is_set():
                    return  # something changed meanwhile (e.g. the Mac woke); look again
            if mode == "dashboard":
                self.player.stop()
                self.dash_player.start()
                return
            self.dash_player.stop()
            if self.player.running():
                self.player.checkpoint()
            else:
                self.player.start(name)
            if switch_input:
                self.select_pi_input()
        else:
            # Hand the screen back first, then tidy up the player.
            if self.player.running():
                self.player.checkpoint()
            if self.display.is_on():
                self.display.set(False)
            self.player.stop()
            if mode == "dashboard":
                # Keep the browser ready behind the dark screen: it takes about ten
                # seconds to start, but shows at once when the HDMI comes back.
                self.dash_player.start()
            else:
                self.dash_player.stop()

    def select_pi_input(self):
        """Run the optional input_command, which switches the monitor to the Pi (for manual Play)."""
        cmd = shlex.split(self.cfg["monitor"]["input_command"])
        if cmd and shutil.which(cmd[0]):
            run(cmd)
            log.info("asked the monitor to switch to the Pi: %s", " ".join(cmd))
        elif cmd:
            log.warning("input_command: %s is not installed", cmd[0])

    def status(self):
        with self.lock:
            if self.until is None:
                state, summary = "off", f"Off · {self.reason}"
            elif self.paused:
                state = "paused"
                back = f"for {fmt_minutes(self.length)}"
                if (self.window_end is not None
                        and self.window_end - datetime.now() < timedelta(minutes=self.length)):
                    back = f"until {self.window_end:%H:%M}"  # the schedule's off time comes first
                summary = f"On hold while a computer is using the screen · back {back} when it sleeps"
            else:
                state, summary = "playing", f"Playing until {self.until:%H:%M} · {self.reason}"
            present = self.presence.present
            ends = self.until
            until = ends.isoformat(timespec="seconds") if ends else None
            now = time.monotonic()
            helpers = [{"name": k, "screen": "on" if h["on"] else "off",
                        "seconds_ago": round(now - h["seen"]),
                        "fresh": now - h["seen"] < self.helper_timeout}
                       for k, h in sorted(self.helpers.items())]
            dw = self.dash_window()
            if dw is not None and self.until is None and self.dash_stopped != dw[0]:
                if self.in_use is False:
                    state, summary = "dashboard", f"Dashboard until {dw[1]:%H:%M}"
                else:
                    state = "dashboard-waiting"
                    summary = f"Dashboard (until {dw[1]:%H:%M}) appears when the computer's screen goes off"
            elif dw is not None and self.until is None:
                summary = "Off · Dashboard stopped for today"
            watched = self.helpers.get(self.watched)
            watched_helper = self.watched is not None and self.helper_known(self.watched)
            in_use = self.in_use
        playback = self.playback()
        if state == "playing" and playback["paused"]:
            summary = f"Video paused · session ends {ends:%H:%M}"
        host = self.presence.host
        if not host:
            mac_text = "Not set"
        elif present is None:
            mac_text = "Checking…"
        elif not present:
            mac_text = "Asleep or off"
        elif not watched_helper:
            mac_text = "Awake (no helper, so background wake-ups count too)"
        elif watched and watched["on"] and now - watched["seen"] < self.helper_timeout:
            mac_text = "Awake · screen on"
        else:
            mac_text = "Awake · screen off"
        helper_text = ", ".join(
            f"{h['name']}: screen {h['screen']}" + ("" if h["fresh"] else f" ({h['seconds_ago']} s ago)")
            for h in helpers) or "None heard from"
        quiet_text = "None"
        if self.quiet:
            q = self.cfg["schedule"]
            quiet_text = f"{q['quiet_start'].strip()}–{q['quiet_end'].strip()}" + (" · now" if self.quiet_now() else "")
        dash_text = "Off"
        if self.dash_times:
            d = self.cfg["dashboard"]
            dash_text = f"{d['start'].strip()}–{d['end'].strip()} every day" + (" · now" if self.dash_window() else "")
        sch, w = dict(self.schedule), self.window()
        if not sch["enabled"]:
            sch_text = "Off"
        elif w is None:
            sch_text = f"{sch['on']}–{sch['off']} every day · next on at {sch['on']}"
        elif self.stopped_for_window():
            sch_text = f"{sch['on']}–{sch['off']} every day · stopped until {sch['on']} tomorrow"
        else:
            sch_text = f"{sch['on']}–{sch['off']} every day · on now until {sch['off']}"
        return {
            "state": state, "summary": summary, "until": until,
            "output_on": self.display.is_on(),
            "playback": playback,
            "computer": {"host": host, "present": present, "text": mac_text, "in_use": in_use},
            "helpers": {"list": helpers, "text": helper_text},
            "quiet": {"active": self.quiet_now(), "text": quiet_text},
            "dashboard": {"active": self.dash_window() is not None, "text": dash_text},
            "schedule": {**sch, "active": w is not None, "text": sch_text},
            "keep_minutes": self.keep_minutes,
            "video": (f"{self.video.name} · {fmt_minutes(self.default_minutes)} per play"
                      if self.video.is_file() else f"{self.video} · file not found"),
            "default_minutes": self.default_minutes,
        }

    def serve(self):
        server = ThreadingHTTPServer(("", self.cfg["web"].getint("port")), make_handler(self))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("control page on port %s", self.cfg["web"]["port"])

    def run(self):
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        wayland = runtime / os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        while self.running and not wayland.exists():
            log.info("waiting for the desktop session (%s)", wayland)
            time.sleep(3)
        self.display.reset()
        self.serve()
        if self.presence.host:
            threading.Thread(target=self.presence_loop, daemon=True).start()
        elif not self.quiet_now():
            with self.lock:
                self.start_session(self.default_minutes, "Pi started", False)
        while self.running:
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            self.wake.wait(2)
            self.wake.clear()
        # On shutdown, leave the Pi's desktop visible as normal.
        self.player.stop()
        self.dash_player.stop()
        self.display.set(True)


def make_handler(ctrl):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, code, body, ctype):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                page = (PAGE.replace("{{DEFAULT}}", fmt_minutes(ctrl.default_minutes))
                        .replace("{{KEEP}}", fmt_minutes(ctrl.keep_minutes)))
                self.send(200, page, "text/html; charset=utf-8")
            elif path == "/api/status":
                self.send(200, json.dumps(ctrl.status()), "application/json")
            elif path == "/dashboard":
                try:
                    page = (Path(__file__).resolve().parent / "dashboard.html").read_text()
                except OSError:
                    return self.send(500, "dashboard.html is missing; run install.sh again\n", "text/plain")
                self.send(200, page, "text/html; charset=utf-8")
            elif path == "/api/dashboard":
                self.send(200, json.dumps(ctrl.dash_data.snapshot()), "application/json")
            elif path == "/api/log":
                self.send(200, "\n".join(recent_log.lines) + "\n", "text/plain; charset=utf-8")
            else:
                self.send(404, "Not found. Try POST /api/play, /api/keep, /api/stop, /api/pause,"
                          " /api/sound, /api/schedule or GET /api/status, /api/log\n", "text/plain")

        def do_POST(self):
            url = urlparse(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode(errors="replace")
            route = url.path.removeprefix("/api")
            if url.path == "/api/mac":
                # From the helper on a Mac: ?name=Office-Mac&screen=on|off
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if not query.get("name") or query.get("screen") not in ("on", "off"):
                    return self.send(400, "need name=... and screen=on|off\n", "text/plain")
                ctrl.on_helper(query["name"], query["screen"] == "on")
                return self.send(200, '{"ok": true}\n', "application/json")
            if route == "/play":
                query = parse_qs(url.query)
                minutes = None
                if "minutes" in query:
                    try:
                        minutes = float(query["minutes"][0])
                    except ValueError:
                        minutes = -1
                    if not 0 < minutes <= 24 * 60:
                        return self.send(400, "minutes must be between 1 and 1440\n", "text/plain")
                ctrl.play(minutes)
            elif route == "/stop":
                ctrl.stop()
            elif route == "/keep":
                ctrl.keep_playing()
            elif route == "/schedule":
                fields = {k: v[0] for k, v in parse_qs(url.query).items()}
                fields.update({k: v[0] for k, v in parse_qs(body).items()})
                if "form" in fields:  # an unticked checkbox sends nothing
                    fields.setdefault("enabled", "no")
                error = ctrl.set_schedule(fields.get("enabled"), fields.get("on"), fields.get("off"))
                if error:
                    return self.send(400, error + "\n", "text/plain")
            elif route in ("/pause", "/sound"):
                to = parse_qs(url.query).get("to", ["toggle"])[0]
                if to not in ("on", "off", "toggle"):
                    return self.send(400, "to must be on, off or toggle\n", "text/plain")
                if not ctrl.set_playback(route[1:], to) and url.path.startswith("/api/"):
                    return self.send(409, "The video isn't on screen right now\n", "text/plain")
            else:
                return self.send(404, "Not found\n", "text/plain")
            if url.path.startswith("/api/"):
                self.send(200, json.dumps(ctrl.status()), "application/json")
            else:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()

    return Handler


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Screen loop</title>
<style>
:root { --bg:#0f0f11; --fg:#f2f1ee; --muted:#9b9aa3; --card:#1c1c20; --accent:#ece6d6; --on-accent:#141414; --stop:#3b2626; }
@media (prefers-color-scheme: light) {
  :root { --bg:#f5f4f0; --fg:#151515; --muted:#6b6b70; --card:#ffffff; --accent:#151515; --on-accent:#f5f4f0; --stop:#f1e2e2; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.45 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
main { max-width:420px; margin:0 auto; padding:32px 16px 40px; }
h1 { font-size:24px; font-weight:650; margin:0 0 6px; letter-spacing:-0.01em; }
#state { color:var(--muted); margin:0 0 28px; min-height:3em; }
form { margin:0 0 12px; }
button { width:100%; padding:18px 20px; border:0; border-radius:14px; background:var(--card); color:var(--fg);
         font-family:inherit; font-size:18px; font-weight:600; text-align:left; cursor:pointer; }
button span { float:right; font-weight:450; opacity:.65; }
button.primary { background:var(--accent); color:var(--on-accent); }
button.stop { background:var(--stop); }
button:active { transform:scale(.985); }
button:disabled { opacity:.4; transform:none; }
.row { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:0 0 12px; }
.row form { margin:0; }
.row button { text-align:center; }
[hidden] { display:none !important; }
.card { background:var(--card); border-radius:14px; padding:16px; margin:28px 0 0; }
.card form { margin:0; }
.card button { background:var(--bg); text-align:center; }
.sched-head { display:flex; justify-content:space-between; align-items:center; margin:0 0 14px; }
h2 { font-size:17px; font-weight:650; margin:0; }
.toggle { display:flex; align-items:center; gap:8px; }
.toggle input { width:22px; height:22px; margin:0; accent-color:var(--accent); }
.times { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:0 0 12px; }
.times label { display:flex; flex-direction:column; gap:6px; font-size:14px; color:var(--muted); }
input[type=time] { width:100%; padding:10px 12px; border-radius:10px; border:1px solid rgba(127,127,127,.35);
                   background:var(--bg); color:var(--fg); font-family:inherit; font-size:18px; }
.hint { color:var(--muted); font-size:14px; margin:12px 0 0; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:8px 16px; margin:32px 0 0; font-size:14px; }
dt { color:var(--muted); } dd { margin:0; }
</style>
</head>
<body>
<main>
  <h1>Screen loop</h1>
  <p id="state">Loading…</p>
  <form method="post" action="/play"><button class="primary">Play <span>{{DEFAULT}}</span></button></form>
  <form method="post" action="/play?minutes=60"><button>Play <span>1 h</span></button></form>
  <form method="post" action="/keep"><button>Keep playing <span>up to {{KEEP}}</span></button></form>
  <div class="row">
    <form method="post" action="/pause"><button id="pause" disabled>Pause</button></form>
    <form method="post" action="/sound" id="sound-form"><button id="sound" disabled>Unmute</button></form>
  </div>
  <form method="post" action="/stop"><button class="stop">Stop</button></form>
  <section class="card">
    <form method="post" action="/schedule" id="schedule">
      <input type="hidden" name="form" value="1">
      <div class="sched-head">
        <h2>Daily schedule</h2>
        <label class="toggle"><input type="checkbox" name="enabled" value="yes" id="sch-enabled"> On</label>
      </div>
      <div class="times">
        <label>On at<input type="time" name="on" id="sch-on" value="07:00" required></label>
        <label>Off at<input type="time" name="off" id="sch-off" value="18:00" required></label>
      </div>
      <button>Save schedule</button>
      <p class="hint" id="sch-text">…</p>
    </form>
  </section>
  <dl>
    <dt>Computer</dt><dd id="mac">…</dd>
    <dt>Mac helpers</dt><dd id="helpers">…</dd>
    <dt>Quiet hours</dt><dd id="quiet">…</dd>
    <dt>Dashboard</dt><dd id="dash">…</dd>
    <dt>Video</dt><dd id="video">…</dd>
  </dl>
</main>
<script>
let scheduleEdited = false;  // don't overwrite the form while it's being changed
document.getElementById('schedule').addEventListener('input', () => { scheduleEdited = true; });
async function refresh() {
  try {
    const s = await (await fetch('/api/status', {cache: 'no-store'})).json();
    document.getElementById('state').textContent = s.summary;
    document.getElementById('mac').textContent = s.computer.text;
    document.getElementById('helpers').textContent = s.helpers.text;
    document.getElementById('quiet').textContent = s.quiet.text;
    document.getElementById('dash').textContent = s.dashboard.text;
    document.getElementById('video').textContent = s.video;
    const f = s.playback, pause = document.getElementById('pause'), sound = document.getElementById('sound');
    pause.disabled = sound.disabled = !f.on_screen;
    pause.textContent = f.paused ? 'Resume' : 'Pause';
    sound.textContent = f.sound ? 'Mute' : 'Unmute';
    document.getElementById('sound-form').hidden = !f.sound_available;
    const sch = s.schedule;
    if (!scheduleEdited) {
      document.getElementById('sch-enabled').checked = sch.enabled;
      document.getElementById('sch-on').value = sch.on;
      document.getElementById('sch-off').value = sch.off;
    }
    document.getElementById('sch-text').textContent = sch.text;
  } catch (e) {
    document.getElementById('state').textContent = 'The Pi is not responding.';
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger().addHandler(recent_log)
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read_dict(DEFAULTS)
    if not cfg.read(CONFIG_PATH):
        log.warning("no config at %s; using defaults", CONFIG_PATH)
    ctrl = Controller(cfg)

    def shutdown(*_):
        ctrl.running = False
        ctrl.wake.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    ctrl.run()


if __name__ == "__main__":
    main()
