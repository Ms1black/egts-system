from __future__ import annotations

import argparse
import asyncio
import json
import logging
import struct
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.models import EGTSPacket, build_egts_response, decode_egts_packet
from app.utils import ensure_python_310_plus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("telemetry-server")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# ─── Shared state ─────────────────────────────────────────────────────────────

latest_packets: dict[str, EGTSPacket] = {}
tracks: dict[str, list[list[float]]] = {}
vehicle_connection_refs: dict[str, int] = {}
frontend_clients: set[WebSocket] = set()
state_lock = asyncio.Lock()
MAX_TRACK_POINTS = 2_000

# Set by main() before uvicorn.run()
TCP_HOST = "0.0.0.0"
TCP_PORT = 6001


# ─── Frontend broadcast ───────────────────────────────────────────────────────

async def broadcast_to_frontend(payload: dict) -> None:
    if not frontend_clients:
        return
    message = json.dumps(payload, default=str)
    stale: list[WebSocket] = []
    for ws in frontend_clients:
        try:
            await ws.send_text(message)
        except Exception:
            stale.append(ws)
    for ws in stale:
        frontend_clients.discard(ws)


def build_state_payload(*, include_type: bool = False) -> dict:
    payload: dict = {
        "vehicles": {vid: pkt.model_dump(mode="json") for vid, pkt in latest_packets.items()},
        "tracks": tracks,
    }
    if include_type:
        payload["type"] = "snapshot"
    return payload


# ─── TCP EGTS ingest ──────────────────────────────────────────────────────────

async def _read_egts_packet(reader: asyncio.StreamReader) -> bytes:
    """Read exactly one EGTS packet from the TCP stream."""
    header = await reader.readexactly(11)
    hl = header[4]
    fdl = struct.unpack_from("<H", header, 5)[0]
    extra = b""
    if hl > 11:
        extra = await reader.readexactly(hl - 11)
    body_and_crc = await reader.readexactly(fdl + 2)
    return header + extra + body_and_crc


async def handle_egts_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    logger.info("EGTS client connected: %s", peer)
    connection_vehicle_ids: set[str] = set()
    response_pid = 0

    try:
        while True:
            raw = await _read_egts_packet(reader)
            try:
                decoded = decode_egts_packet(raw)
            except ValueError as exc:
                logger.warning("Invalid EGTS packet from %s: %s", peer, exc)
                continue

            # Acknowledge the packet
            ack = build_egts_response(rpid=decoded["packet_id"], packet_id=response_pid)
            response_pid = (response_pid + 1) & 0xFFFF
            writer.write(ack)
            await writer.drain()

            packet = EGTSPacket.from_decoded(decoded)
            if packet is None:
                continue

            record = packet.record
            async with state_lock:
                if record.vehicle_id not in connection_vehicle_ids:
                    connection_vehicle_ids.add(record.vehicle_id)
                    vehicle_connection_refs[record.vehicle_id] = (
                        vehicle_connection_refs.get(record.vehicle_id, 0) + 1
                    )
                latest_packets[record.vehicle_id] = packet
                history = tracks.setdefault(record.vehicle_id, [])
                history.append([record.longitude, record.latitude])
                if len(history) > MAX_TRACK_POINTS:
                    del history[:-MAX_TRACK_POINTS]

            await broadcast_to_frontend(
                {"type": "telemetry", "packet": packet.model_dump(mode="json")}
            )

    except asyncio.IncompleteReadError:
        logger.info("EGTS client disconnected: %s", peer)
    except Exception as exc:
        logger.exception("EGTS client error %s: %s", peer, exc)
    finally:
        writer.close()
        removed_vehicle_ids: list[str] = []
        snapshot_payload: dict = {}
        async with state_lock:
            for vehicle_id in connection_vehicle_ids:
                refs = vehicle_connection_refs.get(vehicle_id, 0)
                if refs <= 1:
                    vehicle_connection_refs.pop(vehicle_id, None)
                    if latest_packets.pop(vehicle_id, None) is not None:
                        removed_vehicle_ids.append(vehicle_id)
                    tracks.pop(vehicle_id, None)
                else:
                    vehicle_connection_refs[vehicle_id] = refs - 1
            if removed_vehicle_ids:
                snapshot_payload = build_state_payload(include_type=True)
        if removed_vehicle_ids:
            logger.info("Removed offline vehicles after disconnect %s: %s", peer, ", ".join(sorted(removed_vehicle_ids)))
            await broadcast_to_frontend(snapshot_payload)


# ─── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app_: FastAPI):
    tcp_server = await asyncio.start_server(handle_egts_client, TCP_HOST, TCP_PORT)
    logger.info("EGTS TCP ingest listening on %s:%d", TCP_HOST, TCP_PORT)
    task = asyncio.create_task(tcp_server.serve_forever())
    try:
        yield
    finally:
        task.cancel()
        tcp_server.close()
        await tcp_server.wait_closed()


app = FastAPI(title="Telemetry Processing Server", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ─── HTTP endpoints ───────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/state")
async def state() -> JSONResponse:
    async with state_lock:
        payload = build_state_payload()
    return JSONResponse(payload)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# ─── WebSocket for frontend ───────────────────────────────────────────────────

@app.websocket("/ws/live")
async def live_updates(ws: WebSocket) -> None:
    await ws.accept()
    frontend_clients.add(ws)
    logger.info("Frontend connected: %s", ws.client)

    async with state_lock:
        snapshot = build_state_payload(include_type=True)
    await ws.send_text(json.dumps(snapshot, default=str))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        frontend_clients.discard(ws)
        logger.info("Frontend disconnected: %s", ws.client)
    except Exception as exc:
        frontend_clients.discard(ws)
        logger.exception("Frontend socket error: %s", exc)


# ─── Entry point ──────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telemetry processing server")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host to bind")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port to bind")
    parser.add_argument("--tcp-port", type=int, default=6001, help="TCP port for EGTS ingest")
    return parser.parse_args()


def main() -> None:
    global TCP_HOST, TCP_PORT
    ensure_python_310_plus()
    args = parse_args()
    TCP_HOST = args.host
    TCP_PORT = args.tcp_port
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
