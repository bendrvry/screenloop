# screenloop

[![tests](https://github.com/bendrvry/screenloop/actions/workflows/tests.yml/badge.svg)](https://github.com/bendrvry/screenloop/actions/workflows/tests.yml)

A Raspberry Pi that plays a looping video on your monitor whenever your
computer isn't using it, and gets out of the way the moment it is.

The Pi shares the monitor with one or more computers, each on its own input.
When the computer's screen goes off, the video appears. When you wake the
computer, the Pi cuts its HDMI signal within about a second and the monitor
switches back. After a set time the Pi stops sending a signal altogether, so
the monitor can go to sleep. A small web page lets you control it from a phone.

## The problem

A monitor with automatic input switching shows whichever input has a signal,
and falls back to another input when the one it's showing loses its signal.
That's enough to share a screen between a computer and a Pi, if the Pi can do
two things: know when the computer is really in use, and give the monitor no
signal at all. Both were harder than they sound.

**1. A Mac that looks asleep isn't.** The first version pinged the Mac and
started the video when it stopped answering. But macOS wakes in the background
("dark wake") all day for backups, sync and notifications. I counted 93 dark
wakes in one day, some over an hour long. From the network, a dark wake looks
exactly like someone waking the Mac, so the video kept cutting to black and
restarting. The screen stays off during a dark wake, so the fix is a small
[helper](#mac-helper) on the Mac that reports its screen state. Dark wakes
never turn the screen on.

**2. A sleeping Mac still answers pings.** Its network chip replies on its
behalf, with **TTL 32** instead of the **64** that awake macOS uses. The Pi
reads the TTL, so one low-TTL reply means "asleep" at once, without waiting
for pings to time out. This is the fallback when no helper is installed.

**3. Turning an HDMI output off isn't the same as no signal.** Switching the
output off in the Pi's desktop got stuck whenever the monitor woke up and
re-announced itself. Instead, the Pi forces its HDMI port to look unplugged by
writing to the connector's `status` file in sysfs (a udev rule gives it
permission), and "replugs" it the same way.

## How it works

| What happens | What the Pi does | What the monitor does |
|---|---|---|
| The computer's screen goes off | Starts the video for a set number of loops | Switches to the Pi |
| The computer's screen comes on | Cuts its HDMI at once and keeps its place in the video | Falls back to the computer |
| The screen goes off again | Picks up where it stopped, with a fresh timer | Switches back to the Pi |
| The timer runs out, or quiet hours start | Turns its HDMI off | Sleeps, unless another input is live |
| A second computer with the helper connects | Cuts its HDMI | Switches to that computer |

New sessions start at a random point in the video. Resumed ones pick up where
they stopped.

There are two ways for the Pi to know the computer is in use:

- **Mac helper (recommended for Macs).** Reports the screen state on every
  change and every 10 s. This also covers display sleep and hot corners. Once
  the Pi has heard from the helper on `host`, it trusts it over pings for 7
  days, across restarts. A helper that goes quiet for 35 s counts as screen
  off.
- **Pings (any computer).** The Pi pings `host` every 2 s. An answer from the
  computer's own network stack counts as awake; a low-TTL answer or 3 missed
  pings count as asleep. Two rules soften the dark-wake problem: a wake within
  2 minutes of sleeping must last 30 s before the video steps aside, and a new
  session needs the computer awake for at least 4 minutes first. Pings are
  only judged while the Pi can reach its router, so a network drop doesn't
  look like the computer going to sleep.

## What you need

- A Raspberry Pi 4 running Raspberry Pi OS with the desktop (Wayland, labwc).
- A monitor with automatic input switching, the Pi on one input and your
  computer on another.
- A video file in **H.264, 1080p or smaller**.
- `mpv`, `ffmpeg` and `wlr-randr` (`sudo apt install mpv ffmpeg wlr-randr`).

## Install

On the Pi:

```bash
git clone https://github.com/bendrvry/screenloop.git
cd screenloop
bash install.sh              # user service, no sudo
sudo bash install-root.sh    # once: the udev rule for the HDMI port
```

Then put your video at `~/videos/display.mp4` and set `host` in
`~/.config/screenloop/screenloop.ini` to your computer's name (for example
`office-mac.local`). Restart with `systemctl --user restart screenloop`.

### Mac helper

On each Mac that shares the monitor, from a copy of this repository:

```bash
bash mac/install-mac.sh http://raspberrypi.local:8080
```

It needs Apple's command line tools (`xcode-select --install`). The script
builds a small Swift program and starts it at every login. If macOS asks
whether **Screenloop Helper** may find devices on your local network, click
**Allow**. It counts the screen as on only while an external display is
connected and the screens are awake, so a laptop interrupts the video only
while it's plugged in. It logs to `~/Library/Logs/screenloop-helper.log`. To
remove it, run `bash mac/uninstall-mac.sh`.

## Control page

Open `http://raspberrypi.local:8080` on your phone.

- **Play** starts a session of the default length, or 1 hour. It works during
  quiet hours too.
- **Keep playing** plays until you press Stop, with a safety cap
  (`keep_playing_hours`).
- **Pause** freezes the picture. The timer keeps running, so a forgotten
  pause still ends with the monitor asleep.
- **Unmute** turns the sound on. The video always comes back muted.
- **Stop** ends the session.
- **Daily schedule** plays between an on time and an off time every day.
  During the window there's no timer: the video steps aside whenever the
  computer is in use and returns when it isn't. At the off time everything
  stops. Stop during the window lasts for the rest of that day's window.
  Windows can run past midnight.

The page has no password, so anyone on your network can use it.

### API

Every button is also an HTTP endpoint, for example for an iPhone Shortcut
(**Get Contents of URL**, method POST):

| Request | What it does |
|---|---|
| `POST /api/play` | Play for the default length |
| `POST /api/play?minutes=60` | Play for a set time (1 to 1440 minutes) |
| `POST /api/keep` | Keep playing |
| `POST /api/stop` | Stop |
| `POST /api/pause?to=on\|off\|toggle` | Pause or resume (`on` = paused) |
| `POST /api/sound?to=on\|off\|toggle` | Unmute or mute |
| `POST /api/schedule?enabled=yes&on=07:00&off=18:00` | Change the daily schedule (any of the three) |
| `GET /api/status` | The current state as JSON |
| `GET /api/log` | The last 1,000 log lines |
| `POST /api/mac?name=…&screen=on\|off` | What the Mac helper calls |

Pause and sound reply with 409 if the video isn't on screen.

## Settings

Edit `~/.config/screenloop/screenloop.ini`, then run
`systemctl --user restart screenloop`. Each setting is explained in the file.
The main ones:

| Setting | What it does |
|---|---|
| `file` | The video to play |
| `crop` | Trims letterbox bars so the picture fills the screen |
| `loops` | How many times the video plays each session |
| `quiet_start` / `quiet_end` | No automatic playback between these times |
| `host` | The computer that shares the monitor |
| `resolution` | The display mode, or `auto` for the monitor's preferred one |
| `input_command` | Optional command that switches the monitor to the Pi when you press Play (for example with `ddcutil`) |
| `mpv_args` | Extra options for mpv |

## Notes from the build

- **Playback path.** The video is decoded by the Pi's hardware H.264 decoder
  and handed straight to the compositor (`--vo=dmabuf-wayland
  --hwdec=v4l2m2m`), at about 4% CPU. On my Pi 4 at 3840×1600, mpv's usual GPU
  output never refreshed the picture: the video looked frozen while mpv kept
  playing. That's why the video must be H.264 at 1080p or smaller.
- **60 Hz at 4K-class resolutions.** With `hdmi_enable_4kp60` set, my Pi 4
  stuttered and then locked up during playback. 30 Hz is plenty for video.
- **Power.** Use a proper 5 V / 3 A supply, not the monitor's USB port.
- **Monitors that keep the link up when switched off.** The Pi ends the
  session if the monitor disappears from its HDMI port for 5 s. Some monitors
  keep the HDMI connection alive while switched off, so the Pi can't tell.
  Use Stop on the control page in that case.
- **Automatic switching only goes one way.** Most monitors switch when the
  shown input loses its signal, not when a new one appears. That's why the Pi
  has to drop its signal for the computer to take over, and why pressing Play
  while the computer is in use needs `input_command` to bring the Pi forward.

## Tests

The time windows, the ping logic and the session rules are tested without a
Pi:

```bash
python3 -m unittest discover tests
```

## Troubleshooting

Open `http://raspberrypi.local:8080/api/log`, or on the Pi:

```bash
systemctl --user status screenloop
journalctl --user-unit screenloop -f
```

## Uninstall

```bash
bash uninstall.sh
```

It also prints the command that removes the udev rule.

## Licence

MIT. See [LICENSE](LICENSE).
