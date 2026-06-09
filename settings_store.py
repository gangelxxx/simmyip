import json
import os


def _data_dir():
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        path = os.path.join(appdata, "MyIP Tray")
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.abspath(".")


SETTINGS_FILE = os.path.join(_data_dir(), "settings.json")

DEFAULT_SETTINGS = {
    "check_interval_ms": 5 * 60 * 1000,  # 5 минут
    "show_notifications": True,
    "show_flag_icon": True,
}


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Проверяем, что все ключи на месте
        settings = DEFAULT_SETTINGS.copy()
        settings.update(data)
        return settings
    except (json.JSONDecodeError, OSError):
        return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False
