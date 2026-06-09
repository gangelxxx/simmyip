# MyIP Tray

> A tiny Windows system-tray app that shows your current external IP address, country and flag.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![GUI](https://img.shields.io/badge/GUI-PySide6%20%2F%20Qt-41CD52)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- 🌍 **Tray icon** — shows the country flag or an "IP" fallback.
- 💬 **Hover tooltip** — current IP, country and internet speed (when measured).
- 🖱️ **Left click** — opens a window with the IP history for the last 7 days.
- 🔄 **Double click** — forces an immediate IP refresh with a notification.
- ⏱️ **Automatic checks** — the IP is polled on a configurable interval (5 minutes by default).
- ✨ **Refresh animation** — a smooth spinner in the tray during a request, running on a separate thread.
- 🚀 **Internet speed test** — the "Проверить скорость" menu item measures download/upload through your
  own server (see [Server side](#server-side-speed-test)).
- 📊 **History with speed** — the history window has 4 columns: time, IP, country and speed (if measured).
- 🚩 **Country flags** — flags are fetched from `flagcdn.com` and cached locally.
- 🔁 **Smart retries** — on a network error the request is retried after 1 / 3 / 5 minutes.
- ⚙️ **Settings** — refresh interval, notifications, flag display.

## Architecture

The app is built on **PySide6** and uses the built-in `QNetworkAccessManager` for all network
requests. This means:

- Requests run on Qt's C++ threads and never block the Python GIL.
- The tray animation does not freeze while waiting for a server response.
- No external HTTP libraries are used on the main thread (the flag fallback uses `requests`).

## Installation

1. Make sure Python 3.10+ is installed.
2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Configure the speed-test server address (it is not stored in the repository):

   ```bash
   cp server_config.example.py server_config.py
   ```

   Open `server_config.py` and set the address of your own server, e.g.
   `SPEED_SERVER = "http://SERVER_IP:8088"`. See [Server side](#server-side-speed-test) for how to
   stand the server up. Without this file the app still runs, but the "Проверить скорость" button
   won't work.

## Running

```bash
python tray_app.py
```

Or just double-click `run.bat`.

## Building an .exe

```bash
build.bat
```

The resulting file appears at `dist\MyIP_Tray.exe`.

## Server side (speed test)

Speed is measured not through a public service but through **your own** tiny server: it serves data
for the download test and accepts data for the upload test. It's a small Python 3 standard-library
service that runs on any Linux server with a public IP under `systemd`.

Full setup and configuration instructions are in [`server/README.md`](server/README.md): copying the
files to the server, running it as a `systemd` service, opening the port, verifying it, and pointing
the client at it via `server_config.py`.

The server address is never committed: it lives in `server_config.py` (git-ignored), and the template
is `server_config.example.py`.

## Files

| Path | Description |
| --- | --- |
| `tray_app.py` | Main application: tray, menus, windows, animation, network logic. |
| `flags_service.py` | Downloading and caching country flags from `flagcdn.com`. |
| `history_store.py` | Local history storage (JSON), including speed. |
| `settings_store.py` | Saving settings to `settings.json`. |
| `server_config.example.py` | Template for `server_config.py` (speed-test server address). |
| `server/` | Speed-test server side (service + systemd unit + instructions). |
| `requirements.txt` | Dependencies. |
| `build.bat` / `run.bat` | Build and run scripts. |

## Application data

History, settings and the flag cache are stored in `%LOCALAPPDATA%\MyIP Tray`.

## License

[MIT](LICENSE)
