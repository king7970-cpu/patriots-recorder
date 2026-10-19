"""
Records Channel 14 shows from the live stream and uploads them to Google Drive.
Runs on GitHub Actions — nothing touches the user's computer or home connection.

Two modes:
  SHOW_ID=scheduler  long-running coordinator: sleeps until 15 min before each airing and starts a
                     recording run for it. Hands over to a fresh scheduler run before GitHub's
                     job time limit. (GitHub's own cron is too unreliable — it skips/delays runs.)
  SHOW_ID=<show>     record today's airing of that show.

Env:
  RECORD_MINUTES   if set, skip the schedule and record N minutes right now (test mode)
  QUALITY          0=240p, 1=360p, 2=720p  (default 1)
  KEEP_DAYS        delete recordings older than this from Drive; 0 = keep forever (default 0)
  DRY_RUN          "1" = don't upload
  PART_MINUTES     length of each uploaded part (default 30)
  HEIGHT, CRF      output size/compression (default 360p, CRF 30)
  SINCE            scheduler: airings dispatched before this ISO time were already handled
"""
import base64
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
EPG_URL = "https://www.c14.co.il/api-epg-klh.php"
STREAMS = [
    "https://ch14channel14contentb-ioriver-cdn.encoders.immergo.tv/{q}/streamPlaylist.m3u8",
    "https://r.il.cdn-redge.media/livehls/oil/ch14/live/ch14/live.livx/playlist.m3u8",
]

# keyword: matched against the Channel 14 schedule (EPG) when it's reachable — Cloudflare blocks
# GitHub's servers, so in practice `slots` (exact dates) and then `weekly` are what get used.
# after: ignore airings starting earlier than this (skips the overnight reruns).
SUN_THU = {6, 0, 1, 2, 3}  # Python weekday(): Mon=0 .. Sun=6
SHOWS = {
    "noon": {"keyword": "חדשות צהריים", "title": "חדשות צהריים", "folder": "חדשות צהריים",
             "after": "11:00", "weekly": (SUN_THU, "12:00", "13:00")},
    "news": {"keyword": "טביבי", "title": "החדשות עם מגי טביבי", "folder": "החדשות - מגי טביבי",
             "after": "17:00", "weekly": (SUN_THU, "19:50", "21:00")},
    "patriots": {"keyword": "פטריוט", "title": "הפטריוטים", "folder": "הפטריוטים",
                 "after": "17:00", "weekly": (SUN_THU | {5}, "21:00", "22:30")},
    # 2026 Knesset campaign broadcasts — times vary nightly, taken from the EPG on 2026-10-19.
    "election": {"keyword": "תעמולת", "title": "תעמולת בחירות", "folder": "תעמולת בחירות",
                 "after": "20:00", "slots": {
                     "2026-10-19": ("22:55", "23:22"), "2026-10-20": ("23:40", "00:20"),
                     "2026-10-21": ("23:40", "00:18"), "2026-10-22": ("23:40", "00:31"),
                     "2026-10-24": ("23:40", "00:25"), "2026-10-25": ("23:40", "00:50")}},
}
LEAD = timedelta(minutes=2)   # start before scheduled time
TAIL = timedelta(minutes=8)   # keep going after scheduled end (shows run over)
DISPATCH_LEAD = timedelta(minutes=15)  # scheduler starts a recording run this early
SEGMENT_SECONDS = 6
DVR_WINDOW = timedelta(minutes=85)  # stream keeps ~89 min back; stay inside it
JOB_BUDGET = timedelta(minutes=330)  # GitHub kills jobs at timeout-minutes (355)
PART_MINUTES = int(os.environ.get("PART_MINUTES", "30"))
HEIGHT = int(os.environ.get("HEIGHT", "360"))  # output height in pixels
CRF = int(os.environ.get("CRF", "30"))          # higher = smaller file, lower quality
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


# ---------- schedule ----------

_epg = None


def epg():
    global _epg
    if _epg is None:
        try:
            req = urllib.request.Request(EPG_URL, headers={"User-Agent": UA, "Referer": "https://www.c14.co.il/"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            _epg = {k: v for day in data["shidurim"] for k, v in day.items()}
        except Exception as ex:
            log("EPG unavailable:", ex)
            _epg = {}
    return _epg


def at(d, hhmm):
    return datetime.fromisoformat(f"{d.isoformat()}T{hhmm}").replace(tzinfo=TZ)


def span(d, start, end):
    s, e = at(d, start), at(d, end)
    return s, (e + timedelta(days=1) if e <= s else e)


def slot(show, d):
    """(start, end) of the show's airing on date d, or None."""
    key = d.isoformat()
    if key in epg():
        for p in epg()[key]:
            if show["keyword"] in p["program"] and p["start"] >= show["after"]:
                return span(d, p["start"], p["end"])
        return None
    if key in show.get("slots", {}):
        return span(d, *show["slots"][key])
    if "weekly" in show:
        days, start, end = show["weekly"]
        if d.weekday() in days:
            return span(d, start, end)
    return None


# ---------- GitHub ----------

def github(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def other_active_run(name):
    """True if an older run with this run-name is queued or in progress (the newer one yields,
    so two runs starting together don't both exit)."""
    me = int(os.environ.get("GITHUB_RUN_ID") or 0)
    for status in ("in_progress", "queued"):
        runs = github("GET", f"/actions/workflows/record.yml/runs?status={status}&per_page=50")
        if any(r["id"] < me and r.get("display_title") == name for r in runs["workflow_runs"]):
            return True
    return False


def dispatch(inputs):
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    for attempt in range(5):
        try:
            github("POST", "/actions/workflows/record.yml/dispatches", {"ref": ref, "inputs": inputs})
            return
        except Exception as ex:
            log("dispatch failed, retrying:", ex)
            time.sleep(20)
    raise RuntimeError("could not dispatch " + json.dumps(inputs))


def sleep_until(t):
    time.sleep(max(0.0, (t - datetime.now(TZ)).total_seconds()))


# ---------- scheduler ----------

def scheduler():
    t0 = datetime.now(TZ)
    since = os.environ.get("SINCE", "").strip()
    if since:
        handled_until = datetime.fromisoformat(since)
    else:
        # fresh start (cron / manual): make sure we're the only scheduler, and catch anything airing now
        if other_active_run("scheduler"):
            log("another scheduler is running — exiting")
            return 0
        handled_until = t0 - timedelta(hours=6)

    while True:
        now = datetime.now(TZ)
        upcoming = []
        for show_id, show in SHOWS.items():
            for days in (-1, 0, 1, 2):
                s = slot(show, now.date() + timedelta(days=days))
                if not s or s[1] + TAIL < now:
                    continue
                go = s[0] - LEAD - DISPATCH_LEAD
                if go > handled_until:
                    upcoming.append((go, show_id, s))
        upcoming.sort()
        if not upcoming:
            log("nothing scheduled in the next days")
            nxt = None
        else:
            nxt = upcoming[0]
            for go, sid, (s, e) in upcoming[:6]:
                log(f"  next: {SHOWS[sid]['title']} {s:%a %d/%m %H:%M}-{e:%H:%M} (start run {go:%H:%M})")

        deadline = t0 + JOB_BUDGET
        if nxt is None or nxt[0] > deadline:
            sleep_until(deadline)
            log("handing the scheduler over to a fresh run")
            dispatch({"show": "scheduler", "since": handled_until.isoformat()})
            return 0

        go, show_id, _ = nxt
        sleep_until(go)
        log(f"starting recording run: {show_id}")
        dispatch({"show": show_id})
        handled_until = go


# ---------- recording ----------

def stream_ok(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200 and b"#EXTM3U" in r.read(2000)
    except Exception as ex:
        log("stream check failed:", url, ex)
        return False


def start_recorder(url, pattern, seconds, back_seconds, first_part):
    """Record the live stream, compressing on the fly into PART_MINUTES-long mp4 parts."""
    part = PART_MINUTES * 60
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-user_agent", UA,
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30",
           "-http_persistent", "0"]
    if back_seconds > 0:
        # we're late — start from inside the stream's DVR buffer
        cmd += ["-live_start_index", str(-math.ceil(back_seconds / SEGMENT_SECONDS))]
    cmd += ["-i", url, "-t", str(int(seconds)),
            "-vf", f"scale=-2:{HEIGHT},fps=25",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF), "-profile:v", "main",
            "-force_key_frames", f"expr:gte(t,n_forced*{part})",
            "-c:a", "aac", "-b:a", "48k", "-ac", "1",
            "-f", "segment", "-segment_time", str(part), "-reset_timestamps", "1",
            "-segment_start_number", str(first_part), "-segment_format_options", "movflags=+faststart",
            "-y", pattern]
    log(f"ffmpeg {url} for {seconds/60:.0f} min, parts from {first_part}")
    return subprocess.Popen(cmd)


def part_files(work):
    files = [f for f in os.listdir(work) if f.endswith(".mp4")]
    return sorted((f"{work}/{f}" for f in files), key=lambda f: int(f.rsplit(" ", 1)[1][:-4]))


def upload(path, folder, dry_run):
    mb = os.path.getsize(path) / 1e6
    if dry_run:
        log(f"(dry run) {os.path.basename(path)}: {mb:.0f} MB")
        return
    log(f"uploading {os.path.basename(path)}: {mb:.0f} MB")
    subprocess.run(["rclone", "copy", path, f"gdrive:{folder}",
                    "--drive-chunk-size", "64M", "--retries", "5", "-q"], check=True)


def already_in_drive(folder, prefix):
    r = subprocess.run(["rclone", "lsf", f"gdrive:{folder}", "--include", f"{prefix} חלק 1.mp4"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def normalize_rclone_token():
    """rclone >= 1.70 `authorize` prints a base64 blob instead of the token JSON — accept both."""
    t = os.environ.get("RCLONE_CONFIG_GDRIVE_TOKEN", "").strip()
    if t and not t.startswith("{"):
        blob = json.loads(base64.b64decode(t + "=" * (-len(t) % 4)))
        os.environ["RCLONE_CONFIG_GDRIVE_TOKEN"] = blob["token"]


def record_show(show_id):
    normalize_rclone_token()
    show = SHOWS[show_id]
    folder = show["folder"]
    quality = os.environ.get("QUALITY", "1")
    dry_run = os.environ.get("DRY_RUN") == "1"
    keep_days = int(os.environ.get("KEEP_DAYS", "0") or 0)
    now = datetime.now(TZ)
    test_minutes = os.environ.get("RECORD_MINUTES", "").strip()

    if test_minutes:
        start, end = now, now + timedelta(minutes=float(test_minutes))
        lead = tail = timedelta(0)
    else:
        if os.environ.get("GITHUB_RUN_ID") and other_active_run(show_id):
            log(f"another {show_id} run is already recording — exiting")
            return 0
        # today's airing, or last night's if it runs past midnight
        found = [s for s in (slot(show, now.date()), slot(show, now.date() - timedelta(days=1)))
                 if s and s[1] + TAIL > now]
        if not found:
            log("nothing to record now")
            return 0
        start, end = found[0]
        lead, tail = LEAD, TAIL

    rec_start, rec_end = start - lead, end + tail
    name = f"{show['title']} {start:%Y-%m-%d}" + (" (בדיקה)" if test_minutes else "")
    if not test_minutes and not dry_run and already_in_drive(folder, name):
        log("already recorded — exiting")
        return 0
    if rec_start - now > JOB_BUDGET - (rec_end - rec_start) - timedelta(minutes=15):
        log(f"{show['title']} starts {start:%H:%M} — too far off for one run; the scheduler will start it")
        return 0

    wait = (rec_start - datetime.now(TZ)).total_seconds()
    if wait > 0:
        log(f"waiting {wait/60:.0f} min until {rec_start:%H:%M}")
        time.sleep(wait)

    now = datetime.now(TZ)
    back = max(0.0, (now - rec_start).total_seconds())
    if back > DVR_WINDOW.total_seconds():
        log(f"started {back/60:.0f} min late — beginning is outside the buffer, recording what's left")
        back = DVR_WINDOW.total_seconds()
    duration = (rec_end - now).total_seconds() + back
    if duration <= 60:
        log("show already over")
        return 0

    work = "/tmp/rec"
    os.makedirs(work, exist_ok=True)
    pattern = f"{work}/{name} חלק %d.mp4"
    urls = [u.format(q=quality) for u in STREAMS]
    url = next((u for u in urls if stream_ok(u)), None)
    if not url:
        log("no stream reachable")
        return 2

    log(f"recording {duration/60:.0f} min (from {back/60:.1f} min back) -> {name}")
    sent = set()
    proc = start_recorder(url, pattern, duration, back, 1)
    restarts = 0
    while True:
        # every part except the one being written is finished — send it to Drive right away
        done = part_files(work)[:-1] if proc.poll() is None else part_files(work)
        for f in done:
            if f not in sent:
                upload(f, folder, dry_run)
                sent.add(f)
        if proc.poll() is not None:
            remaining = (rec_end - datetime.now(TZ)).total_seconds()
            if remaining < 60 or restarts >= 6:
                break
            # connection dropped early — continue into the next part number
            restarts += 1
            next_part = len(sent) + 1
            log(f"ffmpeg stopped early (rc={proc.returncode}); resuming as part {next_part}")
            proc = start_recorder(url, pattern, remaining, 0, next_part)
        time.sleep(20)

    if not sent:
        return 3
    log(f"done — {len(sent)} parts in Drive folder: {folder}")

    if keep_days > 0 and not dry_run:
        subprocess.run(["rclone", "delete", f"gdrive:{folder}", "--min-age", f"{keep_days}d",
                        "--include", "*.mp4", "-v"])
    return 0


if __name__ == "__main__":
    sid = os.environ.get("SHOW_ID") or "scheduler"
    sys.exit(scheduler() if sid == "scheduler" else record_show(sid))
