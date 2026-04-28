# Сервер телематики EGTS — документация по развёртыванию

## Назначение

Сервер принимает EGTS-пакеты (ГОСТ Р 56671-2015) от эмуляторов через WebSocket,
хранит историю треков в памяти и транслирует обновления на веб-карту в реальном времени.

---

## Структура пакета (`server_package/`)

```
server_package/
├── Dockerfile.server
├── docker-compose.server.yml
├── requirements.txt
├── app/
│   ├── __init__.py
│   ├── models.py       ← EGTS бинарный кодек (ГОСТ Р 56671-2015)
│   ├── server.py
│   └── utils.py
└── web/
    └── index.html
```

---

## Быстрый запуск (Docker)

```bash
# Порт 8000 (по умолчанию)
docker compose -f docker-compose.server.yml up --build

# Другой порт, например 8080
PORT=8080 docker compose -f docker-compose.server.yml up --build
```

Откройте в браузере: `http://<IP-сервера>:<PORT>/`

---

## Запуск без Docker (Python 3.10+)

```bash
pip install -r requirements.txt
python -m app.server --host 0.0.0.0 --port 8000
```

---

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `HOST` | `0.0.0.0` | Адрес прослушивания |
| `PORT` | `8000` | HTTP/WebSocket порт |

---

## Схема для двух устройств

Если сервер и эмулятор работают на разных физических машинах:

1. Поднимите сервер на первой машине:
   ```bash
   docker compose -f docker-compose.server.yml up --build
   ```
2. На второй машине (где эмулятор) укажите адрес сервера:
   ```bash
   SERVER_WS_URL=ws://<IP-сервера>:8000/ws/ingest docker compose -f docker-compose.emulator.yml up --build
   ```
3. Убедитесь, что входящий TCP-порт `8000` открыт в firewall/NAT на машине сервера.

---

## API

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | Веб-карта мониторинга |
| GET | `/api/health` | `{"status":"ok"}` |
| GET | `/api/state` | Снимок всех ТС и треков |
| WS | `/ws/ingest` | Приём EGTS-пакетов от эмуляторов |
| WS | `/ws/live` | Трансляция обновлений на фронтенд |

---

## Поведение при отключении эмулятора

- Сервер отслеживает, какие `vehicle_id` пришли с каждого ingest WebSocket.
- Если ingest-соединение закрывается (эмулятор остановлен/упал), связанные машины считаются offline.
- Offline-машины удаляются из `latest_packets` и `tracks`, а фронтенду отправляется новый `snapshot`.
- В UI такие машины исчезают с карты и из списка ТС автоматически, без ручного refresh.

---

## Формат принимаемого пакета

Сервер принимает JSON-конверт с бинарным EGTS-пакетом в поле `raw_hex`:

```json
{
  "protocol": "EGTS",
  "header": {
    "protocol_version": 1,
    "packet_type": "EGTS_PT_APPDATA",
    "packet_id": 42,
    "object_id": 1
  },
  "record": {
    "vehicle_id": "CAR-01",
    "timestamp": "2024-03-15T10:30:00+00:00",
    "latitude": 55.7558,
    "longitude": 37.6173,
    "speed_kmh": 60.5,
    "direction_deg": 270.0,
    "altitude_m": 150.0,
    "valid_fix": true,
    "destination_latitude": 55.993,
    "destination_longitude": 37.395
  },
  "raw_hex": "010100000b00270042000151..."
}
```

`raw_hex` — это бинарный EGTS-пакет (Transport Header + SDR + SFRCS) по ГОСТ Р 56671-2015
в hex-кодировке. В текущей реализации ingest-путь валидирует JSON-структуру пакета;
проверка CRC-8/CRC-16 доступна через `decode_egts_packet()`, но не вызывается автоматически
в обработчике `/ws/ingest`.

---

## Открытие порта на сервере

```bash
# ufw (Ubuntu/Debian)
ufw allow 8000/tcp

# firewalld (CentOS/RHEL)
firewall-cmd --permanent --add-port=8000/tcp && firewall-cmd --reload

# AWS / Hetzner — откройте входящий TCP 8000 в Security Group / Firewall panel
```

---

## Проверка работоспособности

```bash
curl http://<IP>:8000/api/health
# → {"status":"ok"}

curl http://<IP>:8000/api/state
# → {"vehicles":{...},"tracks":{...}}
```

---

## Системные требования

- Docker 20.10+ и Compose v2, **или** Python 3.10+
- RAM: от 128 МБ (для 50+ ТС)
- Входящий TCP-порт 8000 открыт
