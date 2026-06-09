# Сервер замера скорости (myip-speedtest)

Изолированный мини-сервис, через который приложение MyIP Tray меряет скорость интернета
вместо публичного `speed.cloudflare.com`. Только стандартная библиотека Python 3, ничего
не пишет на диск, не зависит от остальных сервисов на хосте.

В каталоге два файла:

- [`speedtest_server.py`](speedtest_server.py) — сам сервис;
- [`myip-speedtest.service`](myip-speedtest.service) — systemd-юнит для автозапуска.

## Эндпоинты

- `GET  /down?bytes=N` — потоково отдаёт `N` нулевых байт (тест загрузки). По умолчанию 10 МБ,
  максимум 500 МБ.
- `POST /up` — читает и отбрасывает тело запроса (тест отдачи) и **возвращает JSON
  `{"bytes": N, "seconds": S}`** — сколько байт и за сколько секунд сервер реально их принял.
- `GET  /ping` — мгновенный `200 ok` (для измерения пинга).

Слушает `0.0.0.0:8088` (HTTP). Каждый запрос пишет в journald строку вида
`[up] <ip> <bytes> bytes in <s>s -> <mbps>` — удобно для диагностики.

## Установка с нуля

Что нужно: Linux-сервер с публичным IP, `python3`, `systemd`, root-доступ по SSH и
открытый TCP-порт **8088**. Ниже команды запускаются **с машины разработчика**; `hk_srv_bot` —
это ваш алиас сервера из `~/.ssh/config` (подставьте свой). Файлы шлём через `cat | ssh`,
потому что в нашем SSH-конфиге задан `RemoteCommand` (мешает обычному `scp`); флаги
`-o RemoteCommand=none -o RequestTTY=no` его отключают.

### 1. Скопировать файлы на сервер

```bash
# из каталога server/ репозитория
ssh -o RemoteCommand=none -o RequestTTY=no hk_srv_bot "mkdir -p /opt/speedtest"

cat speedtest_server.py | ssh -o RemoteCommand=none -o RequestTTY=no hk_srv_bot \
    "cat > /opt/speedtest/speedtest_server.py && chmod 644 /opt/speedtest/speedtest_server.py"

cat myip-speedtest.service | ssh -o RemoteCommand=none -o RequestTTY=no hk_srv_bot \
    "cat > /etc/systemd/system/myip-speedtest.service"
```

### 2. Запустить как systemd-сервис

```bash
ssh -o RemoteCommand=none -o RequestTTY=no hk_srv_bot \
    "systemctl daemon-reload && systemctl enable --now myip-speedtest.service && \
     systemctl is-active myip-speedtest.service"
```

`enable --now` запускает сервис сразу и прописывает автозапуск при перезагрузке. Юнит
использует `DynamicUser` (сервис работает под одноразовым непривилегированным пользователем)
и sandbox-ограничения, поэтому отдельного юзера заводить не нужно.

### 3. Открыть порт

На нашем хосте файрвол выключен (`ufw` неактивен, `iptables INPUT` — `ACCEPT`), порт сразу
доступен снаружи. Если на вашем сервере есть файрвол или облачная панель — откройте
входящий TCP **8088**, например:

```bash
ufw allow 8088/tcp     # если используется ufw
```

### 4. Проверить, что работает

```bash
curl 'http://SERVER_IP:8088/ping'                                            # -> ok
curl -o /dev/null -w '%{speed_download} B/s\n' 'http://SERVER_IP:8088/down?bytes=10485760'
head -c 5242880 /dev/zero | curl --data-binary @- 'http://SERVER_IP:8088/up' # -> {"bytes":...}
```

### 5. Подключить клиент

Адрес сервера в репозиторий не коммитится. Скопируйте шаблон и впишите свой адрес:

```bash
# в корне репозитория
cp server_config.example.py server_config.py
```

```python
# server_config.py
SPEED_SERVER = "http://SERVER_IP:8088"
```

`server_config.py` в `.gitignore`. `tray_app.py` импортирует из него `SPEED_SERVER`; если
файла нет, замер скорости просто не сработает (адрес пустой).

## Обновление сервиса

После правок в `speedtest_server.py` — повторить копирование из шага 1 и перезапустить:

```bash
cat speedtest_server.py | ssh -o RemoteCommand=none -o RequestTTY=no hk_srv_bot \
    "cat > /opt/speedtest/speedtest_server.py"
ssh -o RemoteCommand=none -o RequestTTY=no hk_srv_bot \
    "systemctl restart myip-speedtest.service"
```

## Диагностика

```bash
ssh hk_srv_bot                                 # зайти на сервер
systemctl status myip-speedtest.service        # статус
journalctl -u myip-speedtest.service -n 50     # логи (в т.ч. строки [down]/[up])
journalctl -u myip-speedtest.service -f        # хвост логов в реальном времени
```

## Настройка

- **Порт** — константа `PORT` в `speedtest_server.py` (и `SPEED_SERVER` в `server_config.py`).
- **Размеры замера** — `SPEED_DOWNLOAD_BYTES` / `SPEED_UPLOAD_BYTES` в `tray_app.py`.
- **Потолок отдачи** `/down` — `MAX_DOWN` в `speedtest_server.py`.

## Удаление

```bash
ssh -o RemoteCommand=none -o RequestTTY=no hk_srv_bot \
    "systemctl disable --now myip-speedtest.service && \
     rm -f /etc/systemd/system/myip-speedtest.service /opt/speedtest/speedtest_server.py && \
     rmdir /opt/speedtest 2>/dev/null; systemctl daemon-reload"
```

## Как считается скорость (важно)

Клиент ходит через пул прокси, который после передачи данных держит TCP-соединение
открытым ещё десятки секунд, поэтому **нельзя мерить по событию `finished`** (оно приходит
с большой задержкой и занижает результат — наблюдали отдачу 0.6 Мбит/с вместо реальных
десятков). Поэтому:

- **Отдача (`/up`)** — берём время, измеренное **самим сервером** (от первого до последнего
  принятого байта тела), и возвращаем его клиенту в JSON. Клиент считает скорость по нему.
- **Загрузка (`/down`)** — меряется на клиенте по `downloadProgress`, от первого до
  последнего полученного байта, не дожидаясь `finished`.

Серверный лог `[down]` показывает лишь время записи в сокет-буфер ядра (не сетевое время),
поэтому он не годится для скорости загрузки — это делает только клиент.

## Ограничения точности

Замер идёт в **одно** TCP-соединение до сервера через прокси, поэтому на быстрых каналах с
большим пингом число может занижаться (ограничение TCP-окна), а из-за буферизации на прокси
отдача, наоборот, иногда завышается (сервер видит скорость «прокси → сервер», а не
«клиент → прокси»). Если понадобится точнее — можно добавить несколько параллельных потоков
`/down` и `/up` с суммированием байт (сервис к этому готов).
