import sys
import json
import os
import time
import ipaddress
import re
from datetime import datetime
from typing import TypedDict, Optional
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QDialog,
    QVBoxLayout, QLabel, QPushButton, QAbstractItemView, QHeaderView,
    QTableWidget, QTableWidgetItem, QHBoxLayout, QLineEdit, QCheckBox,
)
from PySide6.QtCore import QTimer, Qt, QObject, QThread, Signal, QUrl, QByteArray, QPoint, QRect
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QPen, QIntValidator
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from history_store import add_record, load_history
from settings_store import load_settings, save_settings
import flags_service


# --- Constants ---
# Адрес сервера замера скорости вынесен в server_config.py (он в .gitignore и
# не попадает в коммит). Шаблон — server_config.example.py.
try:
    from server_config import SPEED_SERVER
except ImportError:
    SPEED_SERVER = ""
SPEED_DOWNLOAD_BYTES = 10_485_760
SPEED_UPLOAD_BYTES = 5_242_880
ICON_SIZE = 64
OVERLAY_HEIGHT = 20
IP_API_URL = "https://ipwho.is/"


# --- Types ---
class IpResult(TypedDict):
    status: str
    error: Optional[str]
    error_code: Optional[int]
    ip: Optional[str]
    country: Optional[str]
    country_code: Optional[str]


# --- Helpers ---
def _to_mbps(num_bytes: int, elapsed: float) -> float:
    """Преобразует байты и секунды в Мбит/с."""
    if elapsed <= 0:
        return 0.0
    return (num_bytes / elapsed) / 1024 / 1024 * 8


def _format_error(error_code: int, error_text: str) -> str:
    """Преобразует ошибку QNetworkReply в человекочитаемую."""
    if error_code == QNetworkReply.NetworkError.HostNotFoundError:
        return "Проблемы с интернет-соединением"
    if error_code == QNetworkReply.NetworkError.TimeoutError:
        return "Превышено время ожидания ответа от сервера"
    if error_code == QNetworkReply.NetworkError.ConnectionRefusedError:
        return "Сервер временно недоступен"
    if error_code == QNetworkReply.NetworkError.TemporaryNetworkFailureError:
        return "Временный сбой сети"
    text = str(error_text).lower()
    if "timed out" in text or "timeout" in text:
        return "Превышено время ожидания ответа от сервера"
    if "connection refused" in text or "reset by peer" in text:
        return "Сервер временно недоступен"
    if "proxy" in text:
        return "Ошибка подключения через прокси"
    if "too many requests" in text or "слишком много запросов" in text:
        return "Слишком много запросов, попробуйте позже"
    return "Не удалось определить IP-адрес"


def parse_ip_response(
    err_code: int,
    err_text: str,
    http_status: int,
    raw_bytes: bytes,
) -> IpResult:
    """Чистая функция парсинга ответа IP-геолокации.
    Может быть протестирована без живого QNetworkReply / event loop.
    """
    if err_code != QNetworkReply.NetworkError.NoError:
        return {
            "status": "error",
            "error": err_text,
            "error_code": err_code,
            "ip": None,
            "country": None,
            "country_code": "",
        }

    if http_status == 429:
        return {
            "status": "error",
            "error": "Слишком много запросов, попробуйте позже",
            "error_code": err_code,
            "ip": None,
            "country": None,
            "country_code": "",
        }

    try:
        data = json.loads(raw_bytes)
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "error_code": err_code,
            "ip": None,
            "country": None,
            "country_code": "",
        }

    if not data.get("success"):
        return {
            "status": "error",
            "error": data.get("message", "Unknown API error"),
            "error_code": err_code,
            "ip": None,
            "country": None,
            "country_code": "",
        }

    raw_ip = data.get("ip", "")
    raw_cc = data.get("country_code", "")
    try:
        ipaddress.ip_address(raw_ip)
    except ValueError:
        raw_ip = ""
    if not re.fullmatch(r"[A-Za-z]{2}", str(raw_cc)):
        raw_cc = ""

    return {
        "ip": raw_ip if raw_ip else None,
        "country": data.get("country", "Unknown"),
        "country_code": raw_cc,
        "status": "success",
        "error": None,
        "error_code": None,
    }


def create_icon(
    flag_pixmap: Optional[QPixmap] = None,
    country_code: str = "",
) -> QIcon:
    """Рисует базовую иконку для трея: флаг страны с кодом или текст 'IP'."""
    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(QColor("#2d89ef"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    if flag_pixmap and not flag_pixmap.isNull():
        painter.drawPixmap(0, 0, flag_pixmap)

        overlay_rect = pixmap.rect().adjusted(0, ICON_SIZE - OVERLAY_HEIGHT, 0, 0)
        painter.fillRect(overlay_rect, QColor(0, 0, 0, 160))

        font = QFont("Segoe UI", 10, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(
            overlay_rect, Qt.AlignmentFlag.AlignCenter, country_code.upper()
        )
    else:
        font = QFont("Segoe UI", 20, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "IP")

    painter.end()
    return QIcon(pixmap)


# --- Workers ---
class HistoryWriteWorker(QThread):
    def __init__(self, ip: str, country: str, speed_down: Optional[float] = None, speed_up: Optional[float] = None, parent=None):
        super().__init__(parent)
        self.ip = ip
        self.country = country
        self.speed_down = speed_down
        self.speed_up = speed_up

    def run(self):
        add_record(self.ip, self.country, self.speed_down, self.speed_up)


class AnimationWorker(QThread):
    """Изолированный поток анимации (цикл + msleep, без QTimer)."""
    frame_ready = Signal(int)

    def __init__(self, interval_ms: int = 200, parent=None):
        super().__init__(parent)
        self._interval = interval_ms
        self._running = False

    def start_animation(self):
        self._running = True
        if not self.isRunning():
            self.start()

    def stop_animation(self):
        self._running = False

    def run(self):
        frame = 0
        while not self.isInterruptionRequested():
            if self._running:
                self.frame_ready.emit(frame)
                frame = (frame + 1) % 8
                self.msleep(self._interval)
            else:
                self.msleep(50)


# --- UI dialogs ---
class HistoryWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("История IP")
        self.setMinimumSize(400, 300)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowTitleHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.label = QLabel("Последние изменения (7 дней):")
        layout.addWidget(self.label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Время", "IP-адрес", "Страна", "Скорость"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.load_data)
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.load_data()

    def load_data(self):
        history = load_history()
        self.table.setRowCount(len(history))
        for i, entry in enumerate(history):
            ts = entry.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    ts_str = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    ts_str = ts
            else:
                ts_str = ""
            self.table.setItem(i, 0, QTableWidgetItem(ts_str))
            self.table.setItem(i, 1, QTableWidgetItem(entry.get("ip", "")))
            self.table.setItem(i, 2, QTableWidgetItem(entry.get("country", "")))

            speed_down = entry.get("speed_down")
            speed_up = entry.get("speed_up")
            if speed_down is not None or speed_up is not None:
                down_str = f"{speed_down:.1f}" if speed_down is not None else "—"
                up_str = f"{speed_up:.1f}" if speed_up is not None else "—"
                speed_str = f"↓{down_str} ↑{up_str}"
            else:
                speed_str = ""
            self.table.setItem(i, 3, QTableWidgetItem(speed_str))

        self.table.scrollToBottom()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setMinimumSize(350, 200)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowTitleHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        interval_layout = QHBoxLayout()
        interval_layout.addWidget(QLabel("Период обновления (мс):"))
        self.interval_edit = QLineEdit(str(settings.get("check_interval_ms", 300000)))
        self.interval_edit.setValidator(QIntValidator(5000, 3600000))
        interval_layout.addWidget(self.interval_edit)
        layout.addLayout(interval_layout)

        self.notify_cb = QCheckBox("Показывать уведомления")
        self.notify_cb.setChecked(settings.get("show_notifications", True))
        layout.addWidget(self.notify_cb)

        self.flag_cb = QCheckBox("Показывать флаг страны в иконке трея")
        self.flag_cb.setChecked(settings.get("show_flag_icon", True))
        layout.addWidget(self.flag_cb)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def get_settings(self):
        try:
            interval = int(self.interval_edit.text())
        except ValueError:
            interval = 300000
        interval = max(5000, min(3600000, interval))
        return {
            "check_interval_ms": interval,
            "show_notifications": self.notify_cb.isChecked(),
            "show_flag_icon": self.flag_cb.isChecked(),
        }


# --- Main application ---
class TrayApp(QObject):
    def __init__(self):
        super().__init__()
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.settings = load_settings()

        self.current_ip = "..."
        self.current_country = "..."
        self.current_country_code = ""
        self.last_error = None
        self._notify_after_check = False

        # Ретрай: 1 мин, 3 мин, 5 мин
        self.retry_delays = [60000, 180000, 300000]
        self.retry_attempt = 0

        self._check_generation = 0
        self._speed_test_generation = 0

        self._anim_worker = AnimationWorker(200, parent=self)
        self._anim_worker.frame_ready.connect(self._on_anim_frame, Qt.ConnectionType.QueuedConnection)
        self._anim_stopping = False
        self._current_anim_frame = 0
        # Минимальное время показа спиннера, чтобы при быстром ответе и
        # закэшированном флаге он не мигал, а был виден плавно.
        self._anim_min_visible_ms = 700
        self._anim_start_time = 0.0
        self._anim_stop_timer = QTimer(self)
        self._anim_stop_timer.setSingleShot(True)
        self._anim_stop_timer.timeout.connect(self._do_stop_loading_animation)

        self._network = QNetworkAccessManager(self)
        self._current_reply = None
        self._flag_reply = None
        self._speed_download_reply = None
        self._speed_upload_reply = None

        self.current_speed_down = None
        self.current_speed_up = None
        self._speed_test_ip = ""
        self._speed_test_country = ""

        # Speedtest measurement state (initialized here, not lazily in methods).
        # Загрузку меряем от первого до последнего байта по downloadProgress,
        # а не до finished: через прокси finished может прийти на десятки секунд
        # позже прихода последнего байта и испортить замер.
        self._speed_test_start = 0.0
        self._speed_download_last = 0.0
        self._speed_download_started = False
        self._speed_download_bytes = 0
        # Скорость отдачи берём из времени, измеренного самим сервером (он
        # возвращает его в ответе на /up) — по той же причине. Локальный таймер
        # оставлен как запасной вариант, если ответ не разобрать.
        self._speed_upload_start = 0.0

        self._active_history_workers = set()
        self._show_speed_indicator = False
        self._current_flag_pixmap = QPixmap()
        self._current_base_icon = create_icon()

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self._current_base_icon)
        self.tray.setVisible(True)
        self.tray.activated.connect(self.on_tray_activated)

        self.menu = QMenu()
        self.show_action = self.menu.addAction("История")
        self.show_action.triggered.connect(self.show_history)
        self.refresh_action = self.menu.addAction("Обновить сейчас")
        self.refresh_action.triggered.connect(lambda: self.check_ip(show_notification=True))
        self.speed_action = self.menu.addAction("Проверить скорость")
        self.speed_action.triggered.connect(self._start_speed_test)
        self.settings_action = self.menu.addAction("Настройки")
        self.settings_action.triggered.connect(self.show_settings)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("Выход")
        self.quit_action.triggered.connect(self.quit)
        self.tray.setContextMenu(self.menu)

        self.history_window = None
        self.settings_window = None

        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.show_history)

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._do_retry_check)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.check_ip(show_notification=False))
        self.timer.start(self.settings["check_interval_ms"])

        QTimer.singleShot(500, lambda: self.check_ip(show_notification=False))

    def update_tooltip(self):
        lines = []
        if self.last_error:
            lines.append(f"Ошибка: {self.last_error}")
        else:
            lines.append(f"IP: {self.current_ip}")
            lines.append(f"Страна: {self.current_country}")

        if self.current_speed_down is not None:
            down_str = f"{self.current_speed_down:.1f}"
            up_str = f"{self.current_speed_up:.1f}" if self.current_speed_up is not None else "н/д"
            lines.append(f"↓{down_str} Мбит/с  ↑{up_str} Мбит/с")

        self.tray.setToolTip("\n".join(lines))

    def _abort_current_request(self):
        reply = self._current_reply
        self._current_reply = None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _abort_flag_request(self):
        reply = self._flag_reply
        self._flag_reply = None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _abort_speed_test(self):
        reply_down = self._speed_download_reply
        self._speed_download_reply = None
        if reply_down is not None:
            reply_down.abort()
            reply_down.deleteLater()
        reply_up = self._speed_upload_reply
        self._speed_upload_reply = None
        if reply_up is not None:
            reply_up.abort()
            reply_up.deleteLater()

    def _reset_speed_test_state(self):
        self._speed_test_start = 0.0
        self._speed_download_last = 0.0
        self._speed_download_started = False
        self._speed_download_bytes = 0
        self._speed_upload_start = 0.0

    def _finish_speed_test(self):
        self.speed_action.setEnabled(True)
        self._show_speed_indicator = False
        self._render_tray_icon()

    def _render_tray_icon(self):
        """Рендерит иконку трея: базовая иконка + оверлей анимации / буквы S."""
        pixmap = self._current_base_icon.pixmap(ICON_SIZE, ICON_SIZE).copy()
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if not self._anim_stopping:
            # Затемняем базовую иконку и рисуем поверх спиннер
            painter.fillRect(pixmap.rect(), QColor(0, 0, 0, 80))
            pen = QPen(QColor("white"))
            pen.setWidth(4)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            margin = 10
            painter.drawArc(
                margin, margin, ICON_SIZE - margin * 2, ICON_SIZE - margin * 2,
                self._current_anim_frame * 45 * 16, 120 * 16,
            )

        if self._show_speed_indicator:
            speed_radius = 12
            speed_center = QPoint(ICON_SIZE - speed_radius - 2, speed_radius + 2)
            painter.setBrush(QColor("#ff6b6b"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(speed_center, speed_radius, speed_radius)

            font = QFont("Segoe UI", 13, QFont.Weight.Bold)
            painter.setFont(font)
            painter.setPen(QColor("white"))
            speed_rect = QRect(ICON_SIZE - speed_radius * 2 - 2, 0, speed_radius * 2, speed_radius * 2 + 4)
            painter.drawText(speed_rect, Qt.AlignmentFlag.AlignCenter, "S")

        painter.end()
        self.tray.setIcon(QIcon(pixmap))

    def _on_speed_download_progress(self, bytes_received: int, bytes_total: int):
        self._speed_download_bytes = bytes_received
        if bytes_received > 0:
            now = time.time()
            if not self._speed_download_started:
                self._speed_download_started = True
                self._speed_test_start = now
            # Метка времени последнего полученного байта — по ней и считаем
            # длительность, не дожидаясь finished.
            self._speed_download_last = now

    def _parse_upload_speed(self, payload: bytes):
        """Скорость отдачи по ответу сервера {"bytes": N, "seconds": S}.
        При неразборчивом ответе — запасной расчёт по локальному таймеру."""
        try:
            data = json.loads(payload)
            num_bytes = int(data["bytes"])
            seconds = float(data["seconds"])
            if num_bytes > 0 and seconds > 0:
                return _to_mbps(num_bytes, seconds)
        except (ValueError, KeyError, TypeError):
            pass
        elapsed = time.time() - self._speed_upload_start
        return _to_mbps(SPEED_UPLOAD_BYTES, elapsed)

    def _start_speed_test(self):
        self._speed_test_generation += 1
        gen = self._speed_test_generation
        self._abort_speed_test()
        self._reset_speed_test_state()
        self.current_speed_down = None
        self.current_speed_up = None
        self.update_tooltip()
        self.speed_action.setEnabled(False)

        # Снимаем снапшот IP/страны, чтобы приписать результат правильной сессии
        self._speed_test_ip = self.current_ip
        self._speed_test_country = self.current_country

        # Показываем индикатор speedtest поверх текущей иконки
        self._show_speed_indicator = True
        self._render_tray_icon()

        # Download test
        request = QNetworkRequest(QUrl(f"{SPEED_SERVER}/down?bytes={SPEED_DOWNLOAD_BYTES}"))
        self._speed_download_reply = self._network.get(request)
        self._speed_download_reply.downloadProgress.connect(self._on_speed_download_progress)
        self._speed_download_reply.finished.connect(lambda: self._on_speed_download_done(gen))

    def _on_speed_download_done(self, generation):
        if generation != self._speed_test_generation:
            if self._speed_download_reply:
                self._speed_download_reply.deleteLater()
                self._speed_download_reply = None
            return

        reply = self._speed_download_reply
        self._speed_download_reply = None

        if reply.error() == QNetworkReply.NetworkError.OperationCanceledError:
            reply.deleteLater()
            return

        if reply.error() == QNetworkReply.NetworkError.NoError:
            # От первого до последнего полученного байта (без хвоста до finished).
            elapsed = self._speed_download_last - self._speed_test_start if self._speed_download_started else 0.0
            self.current_speed_down = _to_mbps(self._speed_download_bytes, elapsed)
        else:
            self.current_speed_down = None

        reply.deleteLater()

        # Upload test
        request = QNetworkRequest(QUrl(f"{SPEED_SERVER}/up"))
        upload_data = QByteArray(SPEED_UPLOAD_BYTES, 0)
        self._speed_upload_start = time.time()
        self._speed_upload_reply = self._network.post(request, upload_data)
        self._speed_upload_reply.finished.connect(lambda: self._on_speed_upload_done(generation))

    def _on_speed_upload_done(self, generation):
        self._finish_speed_test()

        if generation != self._speed_test_generation:
            if self._speed_upload_reply:
                self._speed_upload_reply.deleteLater()
                self._speed_upload_reply = None
            return

        reply = self._speed_upload_reply
        self._speed_upload_reply = None

        if reply.error() == QNetworkReply.NetworkError.OperationCanceledError:
            reply.deleteLater()
            return

        if reply.error() == QNetworkReply.NetworkError.NoError:
            # Сервер вернул, сколько байт и за сколько секунд он реально принял.
            # Считаем по ним; если ответ не разобрать — по локальному таймеру.
            payload = bytes(reply.readAll())
            self.current_speed_up = self._parse_upload_speed(payload)
        else:
            self.current_speed_up = None

        reply.deleteLater()
        self.update_tooltip()

        if self.current_speed_down is None and self.current_speed_up is None:
            self.tray.showMessage(
                "MyIP Tray",
                "Не удалось измерить скорость",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
        else:
            down_str = f"{self.current_speed_down:.1f}" if self.current_speed_down is not None else "н/д"
            up_str = f"{self.current_speed_up:.1f}" if self.current_speed_up is not None else "н/д"
            self.tray.showMessage(
                "MyIP Tray",
                f"Скорость: ↓{down_str} Мбит/с  ↑{up_str} Мбит/с",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

        if self.current_speed_down is not None or self.current_speed_up is not None:
            worker = HistoryWriteWorker(
                self._speed_test_ip, self._speed_test_country,
                self.current_speed_down, self.current_speed_up,
                parent=self
            )
            self._active_history_workers.add(worker)
            worker.finished.connect(lambda w=worker: self._active_history_workers.discard(w))
            worker.finished.connect(worker.deleteLater)
            worker.start()

    def check_ip(self, show_notification=False):
        # Ручной запрос сбрасывает ретрай-цепочку
        self.retry_attempt = 0
        self._retry_timer.stop()
        self._request_ip(notify=show_notification)

    def _do_retry_check(self):
        self._request_ip(notify=False)

    def _request_ip(self, notify):
        self._notify_after_check = notify
        self._check_generation += 1
        gen = self._check_generation
        self._start_loading_animation()
        self._abort_current_request()

        request = QNetworkRequest(QUrl(IP_API_URL))
        self._current_reply = self._network.get(request)
        self._current_reply.finished.connect(lambda: self._on_ip_reply(gen))

    def _on_ip_reply(self, generation):
        if generation != self._check_generation:
            if self._current_reply:
                self._current_reply.deleteLater()
                self._current_reply = None
            return

        reply = self._current_reply
        self._current_reply = None

        http_status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        err_code = reply.error()
        err_text = reply.errorString()
        raw_bytes = reply.readAll().data()
        reply.deleteLater()

        info = parse_ip_response(err_code, err_text, http_status, raw_bytes)
        self._on_ip_checked(info, generation)

    def _on_ip_checked(self, info: IpResult, generation):
        if generation != self._check_generation:
            return

        if info["status"] == "success":
            self.retry_attempt = 0
            self._retry_timer.stop()
            self.current_ip = info["ip"]
            self.current_country = info["country"]
            self.current_country_code = info.get("country_code", "")
            self.last_error = None
            worker = HistoryWriteWorker(self.current_ip, self.current_country, parent=self)
            self._active_history_workers.add(worker)
            worker.finished.connect(lambda w=worker: self._active_history_workers.discard(w))
            worker.finished.connect(worker.deleteLater)
            worker.start()
        else:
            if self.retry_attempt < len(self.retry_delays):
                delay = self.retry_delays[self.retry_attempt]
                self.retry_attempt += 1
                self._retry_timer.start(delay)
                self.last_error = _format_error(
                    info.get("error_code") or QNetworkReply.NetworkError.NoError,
                    info.get("error", ""),
                )
                self._stop_loading_animation()
                self.update_tooltip()
                self._current_flag_pixmap = QPixmap()
                self._current_base_icon = create_icon()
                self._render_tray_icon()
                return
            else:
                self.last_error = _format_error(
                    info.get("error_code") or QNetworkReply.NetworkError.NoError,
                    info.get("error", ""),
                )
                self.retry_attempt = 0

        self.update_tooltip()

        if self.settings.get("show_flag_icon", True) and not self.last_error:
            self._load_flag(self.current_country_code, generation)
        else:
            self._stop_loading_animation()
            self._current_flag_pixmap = QPixmap()
            self._current_base_icon = create_icon()
            self._render_tray_icon()

        if self._notify_after_check and self.settings.get("show_notifications", True):
            if not self.last_error:
                self.tray.showMessage(
                    "MyIP Tray",
                    f"IP обновлён: {self.current_ip} ({self.current_country})",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000,
                )
            else:
                self.tray.showMessage(
                    "MyIP Tray",
                    self.last_error,
                    QSystemTrayIcon.MessageIcon.Warning,
                    2000,
                )
            self._notify_after_check = False

    def _load_flag(self, country_code, generation):
        if generation != self._check_generation:
            return

        if not country_code or not re.fullmatch(r"[A-Za-z]{2}", country_code):
            self._stop_loading_animation()
            self._current_flag_pixmap = QPixmap()
            self._current_base_icon = create_icon()
            self._render_tray_icon()
            return

        self._abort_flag_request()

        path = flags_service._cache_path(country_code)
        if os.path.exists(path):
            pixmap = flags_service.get_cached_flag_pixmap(country_code, size=ICON_SIZE)
            self._on_flag_loaded(pixmap, generation)
            return

        url = flags_service.FLAG_URL.format(code=country_code.lower())
        request = QNetworkRequest(QUrl(url))
        self._flag_reply = self._network.get(request)
        self._flag_reply.finished.connect(lambda: self._on_flag_reply(generation, country_code, path))

    def _on_flag_reply(self, generation, country_code, path):
        if generation != self._check_generation:
            if self._flag_reply:
                self._flag_reply.deleteLater()
                self._flag_reply = None
            return

        reply = self._flag_reply
        self._flag_reply = None

        if reply.error() == QNetworkReply.NetworkError.NoError:
            http_status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            if http_status == 200:
                data = reply.readAll()
                # Защита от path traversal
                cache_dir = os.path.abspath(flags_service.FLAGS_CACHE_DIR)
                target = os.path.abspath(path)
                if target.startswith(cache_dir + os.sep) or target == cache_dir:
                    try:
                        os.makedirs(flags_service.FLAGS_CACHE_DIR, exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(data)
                        # Проверим, что записанный файл — валидное изображение
                        test_pixmap = QPixmap()
                        test_pixmap.loadFromData(data)
                        if test_pixmap.isNull():
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                    except OSError:
                        pass

        reply.deleteLater()
        # Только из кэша: качать здесь нельзя — это UI-поток, а блокирующий
        # requests.get на десятки секунд заморозил бы трей. Если асинхронная
        # загрузка не удалась, просто покажем иконку без флага.
        pixmap = flags_service.get_cached_flag_pixmap(country_code, size=ICON_SIZE)
        self._on_flag_loaded(pixmap, generation)

    def _on_flag_loaded(self, flag_pixmap, generation):
        if generation != self._check_generation:
            return
        self._current_flag_pixmap = flag_pixmap
        self._stop_loading_animation()
        self._current_base_icon = create_icon(
            flag_pixmap=flag_pixmap,
            country_code=self.current_country_code,
        )
        self._render_tray_icon()

    def _update_tray_icon(self, flag_pixmap=None):
        if flag_pixmap is None:
            flag_pixmap = self._current_flag_pixmap
        self._current_base_icon = create_icon(
            flag_pixmap=flag_pixmap,
            country_code=self.current_country_code,
        )
        self._render_tray_icon()

    def _start_loading_animation(self):
        # Отменяем отложенную остановку предыдущего цикла, иначе её таймер
        # погасил бы только что запущенную анимацию.
        self._anim_stop_timer.stop()
        self._anim_stopping = False
        self._anim_start_time = time.time()
        self._anim_worker.start_animation()
        # Рисуем первый кадр немедленно: иначе спиннер появляется только после
        # отложенного frame_ready и при быстром ответе может не успеть мелькнуть.
        self._render_tray_icon()

    def _stop_loading_animation(self):
        # Держим спиннер минимум _anim_min_visible_ms, чтобы он не мигал при
        # мгновенном ответе. Если время уже вышло — гасим сразу.
        elapsed_ms = (time.time() - self._anim_start_time) * 1000
        remaining_ms = self._anim_min_visible_ms - elapsed_ms
        if remaining_ms > 0:
            self._anim_stop_timer.start(int(remaining_ms))
        else:
            self._do_stop_loading_animation()

    def _do_stop_loading_animation(self):
        self._anim_stopping = True
        self._anim_worker.stop_animation()
        self._render_tray_icon()

    def _on_anim_frame(self, frame):
        if self._anim_stopping:
            return
        self._current_anim_frame = frame
        self._render_tray_icon()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._click_timer.stop()
            self.check_ip(show_notification=True)
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._click_timer.start(250)

    def show_history(self):
        if self.history_window is None:
            self.history_window = HistoryWindow()
            self.history_window.finished.connect(self.on_history_closed)
        self.history_window.show()
        self.history_window.raise_()
        self.history_window.activateWindow()
        self.history_window.load_data()

    def on_history_closed(self):
        self.history_window = None

    def show_settings(self):
        if self.settings_window is None:
            self.settings_window = SettingsDialog(self.settings)
            if self.settings_window.exec() == QDialog.DialogCode.Accepted:
                self.settings = self.settings_window.get_settings()
                if not save_settings(self.settings):
                    self.tray.showMessage(
                        "MyIP Tray",
                        "Не удалось сохранить настройки",
                        QSystemTrayIcon.MessageIcon.Warning,
                        3000,
                    )
                self.apply_settings()
            self.settings_window = None
        else:
            self.settings_window.raise_()
            self.settings_window.activateWindow()

    def apply_settings(self):
        self.timer.setInterval(self.settings["check_interval_ms"])
        flag_pixmap = QPixmap()
        if self.settings.get("show_flag_icon", True) and not self.last_error:
            flag_pixmap = flags_service.get_cached_flag_pixmap(self.current_country_code, size=ICON_SIZE)
        self._current_flag_pixmap = flag_pixmap
        self._current_base_icon = create_icon(
            flag_pixmap=flag_pixmap,
            country_code=self.current_country_code,
        )
        self._render_tray_icon()

    def quit(self):
        self._abort_current_request()
        self._abort_flag_request()
        self._abort_speed_test()
        self._anim_stop_timer.stop()
        self._anim_worker.requestInterruption()
        self._anim_worker.stop_animation()
        self._anim_worker.wait(1000)
        for worker in list(self._active_history_workers):
            worker.wait(2000)
        self.tray.hide()
        self.app.quit()

    def run(self):
        self.update_tooltip()
        sys.exit(self.app.exec())


def main():
    app = TrayApp()
    app.run()


if __name__ == "__main__":
    main()
