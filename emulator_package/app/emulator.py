from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import socket
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from app.models import encode_egts_pos_data
from app.utils import ensure_python_310_plus, haversine_meters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("telemetry-emulator")
DIRECT_HTTP_OPENER = build_opener(ProxyHandler({}))


@dataclass(frozen=True)
class TrackPoint:
    timestamp: datetime
    latitude: float
    longitude: float


DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
)

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "vehicle": ("vehicle_id", "car_id", "id", "source_id", "tracker_id", "imei"),
    "timestamp": ("timestamp", "time", "datetime", "date_time", "gps_time", "ts"),
    "longitude": ("longitude", "lon", "lng", "x"),
    "latitude": ("latitude", "lat", "y"),
}


def _normalize_header(header: str) -> str:
    return "".join(ch for ch in header.lower().strip() if ch.isalnum() or ch == "_")


def _parse_datetime(raw: str) -> datetime:
    candidate = raw.strip()
    if not candidate:
        raise ValueError("Empty datetime")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported datetime format: {raw}")


def _parse_float(raw: str) -> float:
    return float(raw.strip().replace(",", "."))


def _sniff_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return ","


def _detect_header_indices(header: list[str]) -> dict[str, int]:
    normalized = [_normalize_header(col) for col in header]
    indices: dict[str, int] = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if _normalize_header(alias) in normalized:
                indices[field] = normalized.index(_normalize_header(alias))
                break
    return indices


def _parse_row_with_header(
    row: list[str],
    indices: dict[str, int],
    default_vehicle: str,
) -> tuple[str, TrackPoint]:
    timestamp = _parse_datetime(row[indices["timestamp"]])
    longitude = _parse_float(row[indices["longitude"]])
    latitude = _parse_float(row[indices["latitude"]])
    vehicle = row[indices["vehicle"]].strip() if "vehicle" in indices else default_vehicle
    if not vehicle:
        vehicle = default_vehicle
    return vehicle, TrackPoint(timestamp=timestamp, latitude=latitude, longitude=longitude)


def _parse_row_without_header(row: list[str], default_vehicle: str) -> tuple[str, TrackPoint]:
    values = [value.strip() for value in row if value.strip()]
    if len(values) < 3:
        raise ValueError("Row must contain at least 3 fields")

    if len(values) >= 4:
        try:
            timestamp = _parse_datetime(values[1])
            longitude = _parse_float(values[2])
            latitude = _parse_float(values[3])
            vehicle = values[0] or default_vehicle
            return vehicle, TrackPoint(timestamp=timestamp, latitude=latitude, longitude=longitude)
        except ValueError:
            pass

    permutations = ((0, 1, 2), (2, 0, 1), (2, 1, 0))
    for ts_idx, lon_idx, lat_idx in permutations:
        try:
            timestamp = _parse_datetime(values[ts_idx])
            longitude = _parse_float(values[lon_idx])
            latitude = _parse_float(values[lat_idx])
            return default_vehicle, TrackPoint(timestamp=timestamp, latitude=latitude, longitude=longitude)
        except ValueError:
            continue

    raise ValueError("Unable to parse row without header")


def parse_track_file(path: Path) -> dict[str, list[TrackPoint]]:
    content = path.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip() and not line.startswith("#")]
    if not lines:
        return {}

    delimiter = _sniff_delimiter("\n".join(lines[:5]))
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = [row for row in reader if row]
    if not rows:
        return {}

    default_vehicle_id = path.stem
    first_row = rows[0]
    header_indices = _detect_header_indices(first_row)
    has_header = {"timestamp", "longitude", "latitude"}.issubset(header_indices.keys())

    data_rows = rows[1:] if has_header else rows
    grouped: dict[str, list[TrackPoint]] = {}

    for row in data_rows:
        try:
            if has_header:
                vehicle_id, point = _parse_row_with_header(row=row, indices=header_indices, default_vehicle=default_vehicle_id)
            else:
                vehicle_id, point = _parse_row_without_header(row=row, default_vehicle=default_vehicle_id)
        except (ValueError, IndexError):
            continue
        grouped.setdefault(vehicle_id, []).append(point)

    filtered: dict[str, list[TrackPoint]] = {}
    for vehicle_id, points in grouped.items():
        points.sort(key=lambda item: item.timestamp)
        if len(points) >= 2:
            filtered[vehicle_id] = points
    return filtered


def _downsample_points(points: list[TrackPoint], max_points: int) -> list[TrackPoint]:
    if len(points) <= max_points or max_points < 2:
        return points
    if max_points == 2:
        return [points[0], points[-1]]
    step = (len(points) - 1) / (max_points - 1)
    sampled = [points[round(i * step)] for i in range(max_points)]
    deduped: list[TrackPoint] = []
    for point in sampled:
        if not deduped or point != deduped[-1]:
            deduped.append(point)
    return deduped if len(deduped) >= 2 else points[:2]


def _select_densest_spatial_cell(points: list[TrackPoint], grid_size_deg: float) -> list[TrackPoint]:
    if len(points) < 2 or grid_size_deg <= 0:
        return points
    buckets: dict[tuple[int, int], list[TrackPoint]] = defaultdict(list)
    for point in points:
        cell = (int(point.latitude / grid_size_deg), int(point.longitude / grid_size_deg))
        buckets[cell].append(point)
    densest = max(buckets.values(), key=len)
    return densest if len(densest) >= 2 else points


def _largest_contiguous_segment(points: list[TrackPoint], max_jump_km: float) -> list[TrackPoint]:
    if len(points) < 2 or max_jump_km <= 0:
        return points

    best_start = 0
    best_len = 1
    current_start = 0

    for idx in range(1, len(points)):
        jump_km = haversine_meters(
            points[idx - 1].latitude, points[idx - 1].longitude,
            points[idx].latitude, points[idx].longitude,
        ) / 1000.0
        if jump_km > max_jump_km:
            current_len = idx - current_start
            if current_len > best_len:
                best_start = current_start
                best_len = current_len
            current_start = idx

    tail_len = len(points) - current_start
    if tail_len > best_len:
        best_start = current_start
        best_len = tail_len

    segment = points[best_start: best_start + best_len]
    return segment if len(segment) >= 2 else points


def normalize_track_for_emulation(
    points: list[TrackPoint],
    max_points_per_vehicle: int,
    max_jump_km: float,
    densest_cell_size_deg: float,
) -> list[TrackPoint]:
    if len(points) < 2:
        return points

    points = sorted(points, key=lambda item: item.timestamp)
    localized = _select_densest_spatial_cell(points, grid_size_deg=densest_cell_size_deg)
    localized = sorted(localized, key=lambda item: item.timestamp)
    contiguous = _largest_contiguous_segment(localized, max_jump_km=max_jump_km)
    return _downsample_points(contiguous, max_points=max_points_per_vehicle)


def load_vehicle_tracks(
    dataset_dir: Path,
    vehicles: int,
    max_points_per_vehicle: int,
    max_jump_km: float,
    densest_cell_size_deg: float,
) -> list[tuple[str, list[TrackPoint]]]:
    files = sorted([*dataset_dir.glob("*.txt"), *dataset_dir.glob("*.csv"), *dataset_dir.glob("*.tsv")])
    if not files:
        raise ValueError(f"No dataset files found in {dataset_dir}. Expected .txt/.csv/.tsv files.")

    all_tracks: dict[str, list[TrackPoint]] = {}
    for path in files:
        for source_vehicle_id, points in parse_track_file(path).items():
            all_tracks.setdefault(source_vehicle_id, []).extend(points)

    normalized_tracks: list[tuple[str, list[TrackPoint]]] = []
    for source_vehicle_id, points in all_tracks.items():
        points.sort(key=lambda item: item.timestamp)
        if len(points) >= 2:
            prepared = normalize_track_for_emulation(
                points,
                max_points_per_vehicle=max_points_per_vehicle,
                max_jump_km=max_jump_km,
                densest_cell_size_deg=densest_cell_size_deg,
            )
            if len(prepared) >= 2:
                normalized_tracks.append((source_vehicle_id, prepared))

    normalized_tracks.sort(key=lambda item: item[0])
    if len(normalized_tracks) < vehicles:
        raise ValueError(
            f"Need at least {vehicles} vehicle tracks, found {len(normalized_tracks)} in {dataset_dir}"
        )
    return normalized_tracks[:vehicles]


def speed_kmh(current: TrackPoint, previous: TrackPoint) -> float:
    delta_seconds = (current.timestamp - previous.timestamp).total_seconds()
    if delta_seconds <= 0:
        return 0.0
    distance_m = haversine_meters(previous.latitude, previous.longitude, current.latitude, current.longitude)
    return (distance_m / delta_seconds) * 3.6


def map_match_track(track: list[TrackPoint], map_match_url: str) -> tuple[list[TrackPoint], bool]:
    if len(track) < 2:
        return track, False

    chunk_size = 10
    merged: list[TrackPoint] = []
    start = 0

    while start < len(track):
        end = min(start + chunk_size, len(track))
        chunk = track[start:end]
        coords = ";".join(f"{point.longitude:.6f},{point.latitude:.6f}" for point in chunk)
        radiuses = ";".join(["25"] * len(chunk))
        query = urlencode({"geometries": "geojson", "overview": "false", "gaps": "ignore", "tidy": "true", "radiuses": radiuses})
        url = f"{map_match_url.rstrip('/')}/match/v1/driving/{coords}?{query}"

        try:
            request = Request(url, headers={"User-Agent": "telemetry-emulator/1.0"})
            with DIRECT_HTTP_OPENER.open(request, timeout=8.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            server_header = exc.headers.get("Server", "")
            logger.warning("Map matching unavailable (HTTP %s from %s, server=%s). Falling back to raw GPS.", exc.code, map_match_url, server_header or "unknown")
            if "127.0.0.1:5000" in map_match_url and "AirTunes" in server_header:
                logger.warning("Port 5000 is occupied by AirTunes/AirPlay, not OSRM. Run OSRM on another port.")
            return track, False
        except (URLError, TimeoutError, socket.timeout, ValueError) as exc:
            logger.warning("Map matching unavailable (%s). Falling back to raw GPS.", exc)
            return track, False

        tracepoints = payload.get("tracepoints")
        mapped_chunk: list[TrackPoint] = []
        if isinstance(tracepoints, list) and len(tracepoints) == len(chunk):
            for idx, tracepoint in enumerate(tracepoints):
                location = tracepoint.get("location") if isinstance(tracepoint, dict) else None
                if isinstance(location, list) and len(location) == 2:
                    lon, lat = location
                    mapped_chunk.append(TrackPoint(timestamp=chunk[idx].timestamp, latitude=float(lat), longitude=float(lon)))
                else:
                    mapped_chunk.append(chunk[idx])
        else:
            mapped_chunk = chunk

        if merged and mapped_chunk:
            mapped_chunk = mapped_chunk[1:]
        merged.extend(mapped_chunk)

        if end == len(track):
            break
        start = end - 1

    if len(merged) >= 2:
        return merged, True
    return track, False


async def _drain_reader(reader: asyncio.StreamReader) -> None:
    """Discard data sent by the server (EGTS_PT_RESPONSE packets)."""
    try:
        while True:
            await reader.read(256)
    except Exception:
        pass


async def emulate_vehicle(
    vehicle_id: str,
    track: Iterable[TrackPoint],
    server_host: str,
    server_port: int,
    send_interval_sec: float,
    object_id: int,
) -> None:
    track_points = list(track)
    idx = 1
    reached_destination = False
    packet_id = 0
    reconnect_delay = 1.0

    while True:
        try:
            logger.info("%s connecting to %s:%d (EGTS/TCP)", vehicle_id, server_host, server_port)
            reader, writer = await asyncio.open_connection(server_host, server_port)
            logger.info("%s connected, object_id=%d", vehicle_id, object_id)
            reconnect_delay = 1.0
            drain_task = asyncio.create_task(_drain_reader(reader))
            try:
                while True:
                    if reached_destination:
                        prev_point = track_points[-1]
                        current_point = track_points[-1]
                    else:
                        prev_point = track_points[idx - 1]
                        current_point = track_points[idx]

                    raw = encode_egts_pos_data(
                        latitude=current_point.latitude,
                        longitude=current_point.longitude,
                        speed_kmh=round(speed_kmh(current_point, prev_point), 2),
                        timestamp=current_point.timestamp,
                        packet_id=packet_id,
                        object_id=object_id,
                    )
                    writer.write(raw)
                    await writer.drain()
                    packet_id = (packet_id + 1) & 0xFFFF

                    if not reached_destination:
                        if idx >= len(track_points) - 1:
                            reached_destination = True
                            logger.info("%s reached destination and will hold position", vehicle_id)
                        else:
                            idx += 1
                    await asyncio.sleep(send_interval_sec)
            finally:
                drain_task.cancel()
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("%s connection lost (%s), reconnect in %.1fs", vehicle_id, exc, reconnect_delay)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 15.0)


async def run_emulator(
    dataset_dir: Path,
    server_host: str,
    server_port: int,
    vehicles: int,
    send_interval_sec: float,
    vehicle_prefix: str,
    map_match: bool,
    map_match_url: str,
    max_points_per_vehicle: int,
    max_jump_km: float,
    densest_cell_size_deg: float,
) -> None:
    vehicle_tracks = load_vehicle_tracks(
        dataset_dir=dataset_dir,
        vehicles=vehicles,
        max_points_per_vehicle=max_points_per_vehicle,
        max_jump_km=max_jump_km,
        densest_cell_size_deg=densest_cell_size_deg,
    )
    tasks = []

    for index, (source_vehicle_id, track) in enumerate(vehicle_tracks, start=1):
        vehicle_id = f"{vehicle_prefix}-{index:02d}"
        if map_match:
            original_track = track
            track, matched_ok = map_match_track(track, map_match_url=map_match_url)
            if matched_ok:
                logger.info("Map-matched %s (source=%s): %d -> %d points", vehicle_id, source_vehicle_id, len(original_track), len(track))
            else:
                logger.info("Raw GPS mode for %s (source=%s): %d points (map matching unavailable)", vehicle_id, source_vehicle_id, len(track))
        logger.info("Loaded %d points for %s (object_id=%d) from dataset vehicle '%s'", len(track), vehicle_id, index, source_vehicle_id)
        tasks.append(
            asyncio.create_task(
                emulate_vehicle(
                    vehicle_id=vehicle_id,
                    track=track,
                    server_host=server_host,
                    server_port=server_port,
                    send_interval_sec=send_interval_sec,
                    object_id=index,
                )
            )
        )

    await asyncio.gather(*tasks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GPS tracker emulator (EGTS/TCP)")
    parser.add_argument("--dataset-dir", type=Path, default=Path("dataset"), help="Directory with track files")
    parser.add_argument("--server", default="127.0.0.1:6001", help="Server TCP address as host:port")
    parser.add_argument("--vehicles", type=int, default=5, help="How many vehicles to emulate")
    parser.add_argument("--send-interval", type=float, default=1.0, help="Delay in seconds between packets")
    parser.add_argument("--vehicle-prefix", default="CAR", help="Vehicle identifier prefix (used in logs)")
    parser.add_argument("--map-match", dest="map_match", action="store_true", help="Snap track points to roads via OSRM")
    parser.add_argument("--no-map-match", dest="map_match", action="store_false", help="Disable map matching")
    parser.add_argument("--map-match-url", default="http://router.project-osrm.org", help="OSRM base URL")
    parser.add_argument("--max-points-per-vehicle", type=int, default=2500)
    parser.add_argument("--max-jump-km", type=float, default=200.0)
    parser.add_argument("--densest-cell-size-deg", type=float, default=1.0)
    parser.set_defaults(map_match=False)
    return parser.parse_args()


def main() -> None:
    ensure_python_310_plus()
    args = parse_args()

    if ":" in args.server:
        host, port_str = args.server.rsplit(":", 1)
        server_port = int(port_str)
    else:
        host = args.server
        server_port = 6001

    asyncio.run(
        run_emulator(
            dataset_dir=args.dataset_dir,
            server_host=host,
            server_port=server_port,
            vehicles=args.vehicles,
            send_interval_sec=args.send_interval,
            vehicle_prefix=args.vehicle_prefix,
            map_match=args.map_match,
            map_match_url=args.map_match_url,
            max_points_per_vehicle=args.max_points_per_vehicle,
            max_jump_km=args.max_jump_km,
            densest_cell_size_deg=args.densest_cell_size_deg,
        )
    )


if __name__ == "__main__":
    main()
