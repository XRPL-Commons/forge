"""Daily all-clear heartbeat.

Silence from the observer is ambiguous: "all clean" and "observer dead"
look the same. Once per UTC day, if no suspicious/malicious alert went
out in the last 24h, send an all-clear with the real scan count. The
heartbeat doubles as a liveness signal.
"""
import json, os, time
from pathlib import Path

from observer import report as _report

DAY = 86400
STAMP = "heartbeat.date"
LASTALERT = "lastalert.ts"
SCANS = "scans.jsonl"


def _path(state_dir, name):
    return os.path.join(state_dir, name)


def record_scan(state_dir, scanned, now=None):
    now = time.time() if now is None else now
    os.makedirs(state_dir, exist_ok=True)
    with open(_path(state_dir, SCANS), "a") as f:
        f.write(json.dumps({"ts": now, "scanned": scanned}) + "\n")


def record_alert(state_dir, now=None):
    now = time.time() if now is None else now
    os.makedirs(state_dir, exist_ok=True)
    with open(_path(state_dir, LASTALERT), "w") as f:
        f.write(str(now))


def _last_alert(state_dir):
    p = _path(state_dir, LASTALERT)
    if not os.path.exists(p):
        return None
    try:
        return float(Path(p).read_text().strip())
    except ValueError:
        return None


def _scans_last_24h(state_dir, now):
    """Sum scan counts of the last 24h; prune entries older than 48h."""
    p = _path(state_dir, SCANS)
    if not os.path.exists(p):
        return 0
    keep, total = [], 0
    with open(p) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e["ts"] >= now - 2 * DAY:
                keep.append(e)
            if e["ts"] >= now - DAY:
                total += int(e["scanned"])
    with open(p, "w") as f:
        for e in keep:
            f.write(json.dumps(e) + "\n")
    return total


def maybe_heartbeat(state_dir, repo_count, notify_sh, now=None,
                    runner=_report._default_runner):
    now = time.time() if now is None else now
    today = time.strftime("%F", time.gmtime(now))
    stamp_p = _path(state_dir, STAMP)
    stamp = Path(stamp_p).read_text().strip() if os.path.exists(stamp_p) else ""
    if stamp == today:
        return False

    last = _last_alert(state_dir)
    if last is not None and now - last < DAY:
        # already heard from the observer today via a real alert
        with open(stamp_p, "w") as f:
            f.write(today)
        return False

    scanned = _scans_last_24h(state_dir, now)
    msg = (f"[forge-observer] daily: all clear — {scanned} merges scanned "
           f"across {repo_count} repos in the last 24h")
    if not _report.notify(msg, notify_sh, runner=runner):
        return False  # not stamped; a later run today retries
    with open(stamp_p, "w") as f:
        f.write(today)
    return True
