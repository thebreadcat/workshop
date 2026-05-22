"""Workshop ticker — schedule matcher + notification engine (stdlib only)."""

import json
import logging
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import workshop_db as db

log = logging.getLogger("workshop.ticker")

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}
MONTH_DAYS = {"1st": 1, "15th": 15}

_ticker_state = {
    "alive": False,
    "last_tick": None,
    "last_error": None,
    "ticks": 0,
}
_apps_dir_fn = None  # set by Workshop: callable -> Path


def configure(apps_dir_resolver):
    global _apps_dir_fn
    _apps_dir_fn = apps_dir_resolver


def ticker_status() -> dict:
    return dict(_ticker_state)


def _parse_ts(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _weekday_ok(every: str, now: datetime) -> bool:
    el = every.lower().strip()
    if el == "weekday":
        return now.weekday() < 5
    if el == "weekend":
        return now.weekday() >= 5
    if el in WEEKDAYS:
        return now.weekday() == WEEKDAYS[el]
    if "," in el:
        allowed = {WEEKDAYS.get(p.strip().lower()) for p in el.split(",")}
        allowed.discard(None)
        return now.weekday() in allowed if allowed else True
    return True


def _monthday_ok(every: str, now: datetime) -> bool:
    el = every.lower().strip()
    if el in MONTH_DAYS:
        return now.day == MONTH_DAYS[el]
    return True


def is_due(schedule: dict, now: datetime = None) -> bool:
    """True if this schedule should fire now (and has not already fired this slot)."""
    now = now or datetime.now()
    every = (schedule.get("every") or "").lower().strip()
    at_time = schedule.get("at_time")
    last = _parse_ts(schedule.get("last_ran"))

    if every in ("15min", "30min", "hour"):
        mins = {"15min": 15, "30min": 30, "hour": 60}[every]
        if last is None:
            return True
        return (now - last) >= timedelta(minutes=mins)

    if every in ("day", "weekday", "weekend") or every in WEEKDAYS or "," in every:
        if not at_time:
            return False
        if not _weekday_ok(every, now):
            return False
        try:
            hh, mm = map(int, str(at_time).split(":")[:2])
        except (ValueError, TypeError):
            return False
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < slot:
            return False
        if last and last >= slot:
            return False
        return True

    if every in MONTH_DAYS:
        if not _monthday_ok(every, now):
            return False
        if not at_time:
            slot = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            try:
                hh, mm = map(int, str(at_time).split(":")[:2])
            except (ValueError, TypeError):
                return False
            slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < slot:
            return False
        if last and last >= slot:
            return False
        return True

    return False


def _play_sound():
    for cmd in (
        ["aplay", "/usr/share/sounds/alsa/Front_Center.wav"],
        ["afplay", "/System/Library/Sounds/Glass.aiff"],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=3)
            return
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    try:
        print("\a", end="", flush=True)
    except Exception:
        pass


def _run_notify(schedule: dict, payload: dict):
    title = schedule.get("app", "Workshop")
    msg = schedule.get("message") or "Scheduled reminder"
    db.notif_insert(schedule.get("app", "workshop"), title, msg)
    if payload.get("sound"):
        _play_sound()


def _find_app_dir(apps_base: Path, app: str) -> Path | None:
    if not apps_base or not app:
        return None
    shared = apps_base / "shared" / app
    if shared.is_dir():
        return shared
    users = apps_base / "users"
    if users.is_dir():
        for user_dir in users.iterdir():
            cand = user_dir / app
            if cand.is_dir():
                return cand
    return None


def _run_script(schedule: dict, payload: dict, apps_base: Path):
    script = payload.get("script") or payload.get("file")
    if not script or not apps_base:
        return
    app = schedule.get("app", "")
    app_dir = _find_app_dir(apps_base, app)
    if not app_dir:
        log.warning("run action: app dir not found: %s", app)
        return
    path = app_dir / script
    if not path.is_file():
        log.warning("run action: script not found: %s", path)
        return
    try:
        r = subprocess.run(
            ["python3", str(path)],
            capture_output=True, text=True, timeout=120, cwd=str(path.parent),
        )
        log.info("run %s: exit %s", path, r.returncode)
    except Exception as e:
        log.warning("run %s failed: %s", path, e)


def _run_fetch(payload: dict):
    url = payload.get("url")
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=15)
    except Exception as e:
        log.warning("fetch %s failed: %s", url, e)


def run_action(schedule: dict):
    payload = schedule.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    action = (schedule.get("action") or "notify").lower()
    apps_base = _apps_dir_fn() if _apps_dir_fn else None

    if action == "notify":
        _run_notify(schedule, payload)
    elif action == "run":
        _run_script(schedule, payload, apps_base)
    elif action == "fetch":
        _run_fetch(payload)
    else:
        _run_notify(schedule, payload)


def tick_once():
    now = datetime.now()
    fired = 0
    for sched in db.schedule_enabled():
        if is_due(sched, now):
            try:
                run_action(sched)
                db.schedule_mark_ran(sched["id"])
                fired += 1
            except Exception as e:
                log.exception("schedule %s failed: %s", sched.get("id"), e)
                _ticker_state["last_error"] = str(e)
    _ticker_state["last_tick"] = now.isoformat()
    _ticker_state["ticks"] += 1
    return fired


def _ticker_loop():
    _ticker_state["alive"] = True
    while True:
        try:
            tick_once()
            _ticker_state["last_error"] = None
        except Exception as e:
            log.exception("ticker tick failed: %s", e)
            _ticker_state["last_error"] = str(e)
        time.sleep(60)


def start_ticker_thread(apps_dir_resolver):
    configure(apps_dir_resolver)
    t = threading.Thread(target=_ticker_loop, name="workshop-ticker", daemon=True)
    t.start()
    return t
