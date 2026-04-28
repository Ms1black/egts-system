# Эмулятор EGTS-трекеров — документация по развёртыванию

## Назначение

Эмулятор читает GPS-треки из файлов `dataset/`, формирует бинарные EGTS-пакеты
по ГОСТ Р 56671-2015 и отправляет их на сервер по WebSocket.
Каждое ТС работает в отдельной корутине с автоматическим переподключением.

---

## Структура пакета (`emulator_package/`)

```
emulator_package/
├── Dockerfile.emulator
├── docker-compose.emulator.yml
├── requirements.txt
├── dataset/
│   └── (ваши CSV/TSV/TXT файлы треков)
└── app/
    ├── __init__.py
    ├── models.py       ← EGTS бинарный кодек (ГОСТ Р 56671-2015)
    ├── emulator.py
    └── utils.py
```

---

## Быстрый запуск (Docker)

Положите файлы треков в папку `dataset/`, затем:

```bash
# Сервер и эмулятор на разных физических устройствах
SERVER_WS_URL=ws://1.2.3.4:8000/ws/ingest docker compose -f docker-compose.emulator.yml up --build
```

`SERVER_WS_URL` обязателен. Не используйте `127.0.0.1` внутри контейнера эмулятора,
если сервер запущен на другой машине.

При штатной остановке эмулятора (`Ctrl+C` / container stop) сервер удалит соответствующие ТС
из live-состояния, и они исчезнут из интерфейса мониторинга.

---

## Запуск без Docker (Python 3.10+)

```bash
pip install -r requirements.txt

python -m app.emulator \
  --server ws://<HOST>:<PORT>/ws/ingest \
  --dataset-dir dataset \
  --vehicles 5 \
  --send-interval 1.0 \
  --vehicle-prefix CAR
```

---

## Переменные окружения (Docker)

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `SERVER_WS_URL` | _(обязательно указать)_ | WebSocket URL сервера, например `ws://192.168.1.10:8000/ws/ingest` |
| `VEHICLES` | `5` | Количество виртуальных ТС |
| `SEND_INTERVAL` | `1.0` | Интервал отправки пакетов (сек) |
| `VEHICLE_PREFIX` | `CAR` | Префикс ID ТС (`CAR-01`, `CAR-02`, …) |
| `DATASET_DIR` | `/app/dataset` | Папка с треками |
| `MAP_MATCH_ENABLED` | `0` | Привязка к дорогам OSRM (`1` = включить) |
| `MAP_MATCH_URL` | `http://router.project-osrm.org` | URL OSRM |
| `MAX_POINTS_PER_VEHICLE` | `2500` | Лимит точек на трек |
| `MAX_JUMP_KM` | `200.0` | Фильтр скачков координат (км) |
| `DENSEST_CELL_SIZE_DEG` | `1.0` | Размер spatial-ячейки |

---

## Аргументы командной строки

| Аргумент | По умолчанию | Описание |
|----------|-------------|----------|
| `--server` | _(обязателен)_ | WebSocket URL |
| `--dataset-dir` | `dataset` | Папка с треками |
| `--vehicles` | `5` | Количество ТС |
| `--send-interval` | `1.0` | Интервал, сек |
| `--vehicle-prefix` | `CAR` | Префикс ID |
| `--map-match` / `--no-map-match` | выкл | Snap-to-road |
| `--map-match-url` | публичный OSRM | URL OSRM |
| `--max-points-per-vehicle` | `2500` | Лимит точек |
| `--max-jump-km` | `200.0` | Фильтр разрывов |
| `--densest-cell-size-deg` | `1.0` | Spatial-ячейка |

---

## Форматы входных файлов (`dataset/`)

**С заголовком:**
```csv
vehicle_id,timestamp,longitude,latitude
CAR-A,2008-02-02 13:32:00,116.444570,39.921570
```

**Legacy (без заголовка):**
```
10,2008-02-02 13:32:00,116.444570,39.921570
```

**Только координаты (ID из имени файла):**
```
2008-02-02 13:32:00,116.444570,39.921570
```

---

## Что отправляется на сервер

Каждый пакет — JSON-конверт вокруг бинарного EGTS-пакета по ГОСТ Р 56671-2015.
Поле `raw_hex` содержит реальный бинарный пакет в hex: Transport Header (11 байт) +
HCS (1 байт) + SDR с EGTS_SR_POS_DATA (26 байт данных) + SFRCS CRC-16 (2 байта).

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
    "destination_latitude": null,
    "destination_longitude": null
  },
  "raw_hex": "010100000b002700..."
}
```

---

## Включение привязки к дорогам (OSRM)

```bash
# macOS: порт 5000 занят AirPlay — используйте 5001
MAP_MATCH_ENABLED=1 \
MAP_MATCH_URL=http://127.0.0.1:5001 \
SERVER_WS_URL=ws://1.2.3.4:8000/ws/ingest \
docker compose -f docker-compose.emulator.yml up --build
```

Проверка OSRM:
```bash
curl "http://127.0.0.1:5001/match/v1/driving/37.617,55.756;37.620,55.758?geometries=geojson&overview=false"
```

---

## Системные требования

- Docker 20.10+ и Compose v2, **или** Python 3.10+
- RAM: от 64 МБ
- Папка `dataset/` с хотя бы одним файлом трека
- Сетевая доступность сервера по WebSocket
