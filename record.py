"""
Records Channel 14 shows from the live stream and uploads them to Google Drive.
Runs on GitHub Actions — nothing touches the user's computer or home connection.

Env:
  SHOW_ID          key in SHOWS (default "patriots")
  RECORD_MINUTES   if set, skip the schedule and record N minutes right now (test mode)
  QUALITY          0=240p, 1=360p, 2=720p  (default 1)
  KEEP_DAYS        delete recordings older than this from Drive; 0 = keep forever (default 0)
  DRY_RUN          "1" = don't upload
  PART_MINUTES     length of each uploaded part (default 30)
  HEIGHT, CRF      output size/compression (default 360p, CRF 30)
"""
import base64
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
# keyword: matched against the Channel 14 schedule. fallback: (weekdays Mon=0..Sun=6, start, end) —
# used when the schedule is unreachable (Cloudflare blocks GitHub's servers).
SUN_THU = {6, 0, 1, 2, 3}
SHOWS = {
    "patriots": {"keyword": "פטריוט", "title": "הפטריוטים", "folder": "הפטריוטים",
                 "fallback": (SUN_THU | {5}, "21:00", "22:30")},
    "news": {"keyword": "טביבי", "title": "החדשות עם מגי טביבי", "folder": "החדשות - מגי טביבי",
             "fallback": (SUN_THU, "19:50", "21:00")},
}
# GitHub kills a job after timeout-minutes (355); hand over to a fresh run before that.
JOB_BUDGET = timedelta(minutes=330)
RELAY_MARGIN = timedelta(minutes=12)
EPG_URL = "https://www.c14.co.il/api-epg-klh.php"
STREAMS = [
    "https://ch14channel14contentb-ioriver-cdn.encoders.immergo.tv/{q}/streamPlaylist.m3u8",
    "https://r.il.cdn-redge.media/livehls/oil/ch14/live/ch14/live.livx/playlist.m3u8",
]
LEAD = timedelta(minutes=2)   # start before scheduled time
TAIL = timedelta(minutes=8)   # keep going after scheduled end (shows run over)
SEGMENT_SECONDS = 6
DVR_WINDOW = timedelta(minutes=85)  # stream keeps ~89 min back; stay inside it
PART_MINUTES = int(os.environ.get("PART_MINUTES", "30"))
HEIGHT = int(os.environ.get("HEIGHT", "360"))  # output height in pixels
CRF = int(os.environ.get("CRF", "30"))          # higher = smaller file, lower quality
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def fetch_epg():
    req = urllib.request.Request(EPG_URL, headers={"User-Agent": UA, "Referer": "https://www.c14.co.il/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def tonight_slot(now, show):
    """Today's evening airing (not the overnight rerun). Returns (start, end) or None."""
    today = now.date().isoformat()
    try:
        epg = fetch_epg()
        for day in epg["shidurim"]:
            for p in day.get(today, []):
                if show["keyword"] in p["program"] and p["start"] >= "17:00":
                    s = datetime.fromisoformat(f"{today}T{p['start']}").replace(tzinfo=TZ)
                    e = datetime.fromisoformat(f"{today}T{p['end']}").replace(tzinfo=TZ)
                    if e <= s:
                        e += timedelta(days=1)
                    log("EPG:", p["program"], p["start"], "-", p["end"])
                    return s, e
        log("EPG: no evening airing today")
        return None
    except Exception as ex:  # EPG unreachable — fall back to the usual slot
        log("EPG failed:", ex)
        days, start, end = show["fallback"]
        if now.weekday() not in days:
            return None
        s = datetime.fromisoformat(f"{today}T{start}").replace(tzinfo=TZ)
        return s, datetime.fromisoformat(f"{today}T{end}").replace(tzinfo=TZ)


def github(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def another_run_active():
    """True if a different run of this workflow is already on the job (scheduled duplicates)."""
    me = os.environ.get("GITHUB_RUN_ID")
    for status in ("in_progress", "queued"):
        runs = github("GET", f"/actions/workflows/record.yml/runs?status={status}&per_page=20")
        if any(str(r["id"]) != me for r in runs["workflow_runs"]):
            return True
    return False


def relay(show_id):
    """Start a fresh run for this show. Manual dispatches start in seconds, unlike cron."""
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    github("POST", "/actions/workflows/record.yml/dispatches",
           {"ref": ref, "inputs": {"show": show_id, "relay": "true"}})
    log("handed over to a fresh run")


def already_in_drive(folder, prefix):
    r = subprocess.run(["rclone", "lsf", f"gdrive:{folder}", "--include", f"{prefix} חלק 1.mp4"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


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


def normalize_rclone_token():
    """rclone >= 1.70 `authorize` prints a base64 blob instead of the token JSON — accept both."""
    t = os.environ.get("RCLONE_CONFIG_GDRIVE_TOKEN", "").strip()
    if t and not t.startswith("{"):
        blob = json.loads(base64.b64decode(t + "=" * (-len(t) % 4)))
        os.environ["RCLONE_CONFIG_GDRIVE_TOKEN"] = blob["token"]


def main():
    normalize_rclone_token()
    t0 = datetime.now(TZ)
    show_id = os.environ.get("SHOW_ID") or "patriots"
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
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and another_run_active():
            log("another run is already handling today — exiting")
            return 0
        slot = tonight_slot(now, show)
        if not slot:
            log("nothing to record today")
            return 0
        start, end = slot
        lead, tail = LEAD, TAIL

    rec_start, rec_end = start - lead, end + tail
    name = f"{show['title']} {start:%Y-%m-%d}" + (" (בדיקה)" if test_minutes else "")
    if not test_minutes and not dry_run and already_in_drive(folder, name):
        log("already recorded today — exiting")
        return 0

    # Not enough job time left to wait and record? Sleep as long as is safe, then hand over.
    if rec_end + timedelta(minutes=15) > t0 + JOB_BUDGET:
        relay_at = min(t0 + JOB_BUDGET - RELAY_MARGIN, rec_start - RELAY_MARGIN)
        log(f"{show['title']} {start:%H:%M}-{end:%H:%M}: too far off for this run, relaying at {relay_at:%H:%M}")
        time.sleep(max(0.0, (relay_at - datetime.now(TZ)).total_seconds()))
        relay(show_id)
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
        return 1

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

    if keep_days > 0:
        subprocess.run(["rclone", "delete", f"gdrive:{folder}", "--min-age", f"{keep_days}d",
                        "--include", "*.mp4", "-v"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
