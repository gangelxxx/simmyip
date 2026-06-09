import os
import requests
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap


def _data_dir():
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        path = os.path.join(appdata, "MyIP Tray")
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.abspath(".")


FLAGS_CACHE_DIR = os.path.join(_data_dir(), "flags_cache")
FLAG_URL = "https://flagcdn.com/w80/{code}.png"


def _cache_path(code):
    return os.path.join(FLAGS_CACHE_DIR, f"{code.lower()}.png")


def get_flag_pixmap(country_code, size=64):
    """
    Возвращает QPixmap с флагом страны.
    Сначала ищет в локальном кэше, если нет — скачивает с flagcdn.com.
    """
    if not country_code or len(country_code) != 2:
        return QPixmap()

    os.makedirs(FLAGS_CACHE_DIR, exist_ok=True)
    path = _cache_path(country_code)

    if not os.path.exists(path):
        try:
            url = FLAG_URL.format(code=country_code.lower())
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            with open(path, "wb") as f:
                f.write(response.content)
        except Exception:
            return QPixmap()

    pixmap = QPixmap(path)
    if pixmap.isNull():
        return QPixmap()

    return pixmap.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
