import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone


def _data_dir():
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        path = os.path.join(appdata, "MyIP Tray")
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.abspath(".")


HISTORY_FILE = os.path.join(_data_dir(), "ip_history.json")
MAX_AGE_DAYS = 7

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


def load_history():
    """Загружает историю из JSON, удаляя записи старше MAX_AGE_DAYS."""
    if not os.path.exists(HISTORY_FILE):
        return []

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    cutoff = _now() - timedelta(days=MAX_AGE_DAYS)
    cleaned = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ts_str = entry.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            cleaned.append(entry)

    with _lock:
        if len(cleaned) != len(data):
            _save_history(cleaned)

    return cleaned


def _save_history(history):
    try:
        dir_name = os.path.dirname(HISTORY_FILE)
        fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HISTORY_FILE)
    except OSError:
        pass


def save_history(history):
    with _lock:
        _save_history(history)


def add_record(ip, country, speed_down=None, speed_up=None):
    """Добавляет новую запись в историю.
    Если скорость не указана и запись идентична последней — пропускает."""
    with _lock:
        history = load_history()

        if speed_down is None and speed_up is None and history:
            last = history[-1]
            if last.get("ip") == ip and last.get("country") == country:
                return history

        entry = {
            "timestamp": _now().isoformat(),
            "ip": ip,
            "country": country,
        }
        if speed_down is not None:
            entry["speed_down"] = round(speed_down, 2)
        if speed_up is not None:
            entry["speed_up"] = round(speed_up, 2)

        history.append(entry)
        _save_history(history)
        return history
