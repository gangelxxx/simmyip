#!/usr/bin/env python3
"""Минимальный HTTP-сервис для замера скорости MyIP Tray.

Эндпоинты:
    GET  /down?bytes=N  — потоково отдаёт N нулевых байт (для теста загрузки);
    POST /up            — читает и отбрасывает тело запроса (для теста отдачи);
    GET  /ping          — мгновенный ответ 200 (для измерения пинга).

Сервис самодостаточный (только стандартная библиотека Python 3),
не пишет на диск и работает под systemd-юнитом myip-speedtest.service.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
PORT = 8088

DEFAULT_DOWN = 10 * 1024 * 1024          # размер по умолчанию, если bytes не задан
MAX_DOWN = 500 * 1024 * 1024             # верхний предел отдаваемого объёма
CHUNK = 256 * 1024                       # размер блока чтения/записи
_ZEROS = b"\0" * CHUNK                   # переиспользуемый блок нулей


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # keep-alive + Content-Length

    # Отключаем штатный access-лог в stderr (его собирает journald).
    def log_message(self, *args):
        pass

    def _log(self, kind, num_bytes, dt):
        """Диагностика: сколько байт и за сколько сервер реально передал/принял."""
        mbps = (num_bytes / dt / 1024 / 1024 * 8) if dt > 0 else 0.0
        client = self.client_address[0] if self.client_address else "?"
        print(f"[{kind}] {client} {num_bytes} bytes in {dt:.3f}s -> {mbps:.2f} Mbit/s",
              flush=True)

    def _send_headers(self, length, content_type="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/ping":
            self._send_headers(2, "text/plain")
            self.wfile.write(b"ok")
            return

        if parsed.path == "/down":
            qs = parse_qs(parsed.query)
            try:
                n = int(qs.get("bytes", [str(DEFAULT_DOWN)])[0])
            except (ValueError, IndexError):
                n = DEFAULT_DOWN
            n = max(0, min(n, MAX_DOWN))

            self._send_headers(n)
            remaining = n
            t0 = time.monotonic()
            try:
                while remaining > 0:
                    if remaining >= CHUNK:
                        self.wfile.write(_ZEROS)
                        remaining -= CHUNK
                    else:
                        self.wfile.write(b"\0" * remaining)
                        remaining = 0
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self._log("down", n - remaining, time.monotonic() - t0)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/up":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            received = 0
            remaining = length
            t0 = None
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    if t0 is None:
                        t0 = time.monotonic()   # старт по первому байту тела
                    received += len(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            seconds = (time.monotonic() - t0) if t0 is not None else 0.0
            self._log("up", received, seconds)

            # Возвращаем измеренные сервером объём и время приёма. Клиент считает
            # скорость по ним и не зависит от задержки закрытия соединения: через
            # прокси сигнал finished может прийти на десятки секунд позже.
            body = json.dumps({"bytes": received, "seconds": round(seconds, 4)}).encode()
            self._send_headers(len(body), "application/json")
            self.wfile.write(body)
            return

        self.send_error(404)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"speedtest service listening on {HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
