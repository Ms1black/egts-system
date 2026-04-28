"""
EGTS (ERA-GLONASS Telematics Standard) — ГОСТ Р 56671-2015.

Бинарное кодирование/декодирование транспортного и сервисного уровней.

Структура пакета (Transport Layer):
  [PRE(1)] [VER(1)] [SKID(1)] [FLAGS(1)] [HL(1)] [FDL(2)] [PID(2)] [PT(1)] [HCS(1)]
  [Body: Service Data Records]
  [SFRCS(2)]

Service Data Record (SDR):
  [RL(2)] [RN(2)] [FLAGS(1)] [OID(4)] [SST(1)]
  [Subrecords...]

EGTS_SR_POS_DATA (subtype 0x10):
  [NTM(4)] [LAT(4)] [LONG(4)] [FLAGS(1)] [SPD_LO(1)] [SPD_HI(1)] [DIR(1)]
  [ODOMETER(3)] [DIN(1)] [SRC(1)] [reserved(1)] [ALT(3)] [SRCD(2)]
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone

# ─── Constants ────────────────────────────────────────────────────────────────

EGTS_PROTOCOL_VERSION: int = 0x01
EGTS_PC_OK: int = 0x00

EGTS_PT_RESPONSE: int = 0x00
EGTS_PT_APPDATA: int = 0x01
EGTS_PT_SIGNED_APPDATA: int = 0x02

EGTS_TELEDATA_SERVICE: int = 0x02
EGTS_AUTH_SERVICE: int = 0x01

EGTS_SR_POS_DATA: int = 0x10
EGTS_SR_EXT_POS_DATA: int = 0x11
EGTS_SR_AD_SENSORS_DATA: int = 0x12

# EGTS epoch = 2010-01-01 00:00:00 UTC
_EGTS_EPOCH = int(datetime(2010, 1, 1, tzinfo=timezone.utc).timestamp())

_TRANSPORT_HEADER_SIZE = 11  # PRE+VER+SKID+FLAGS+HL+FDL(2)+PID(2)+PT+HCS


# ─── CRC helpers ──────────────────────────────────────────────────────────────

def _crc8(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# ─── Transport header builder ─────────────────────────────────────────────────

def _build_transport_header(fdl: int, pid: int, pt: int) -> bytes:
    header_before_hcs = struct.pack(
        "<BBBBBHHB",
        0x01, EGTS_PROTOCOL_VERSION, 0x00, 0x00,
        _TRANSPORT_HEADER_SIZE, fdl, pid & 0xFFFF, pt,
    )
    hcs = _crc8(header_before_hcs)
    return header_before_hcs + bytes([hcs])


# ─── Encoder ──────────────────────────────────────────────────────────────────

def encode_egts_pos_data(
    *,
    latitude: float,
    longitude: float,
    speed_kmh: float,
    direction_deg: float = 0.0,
    altitude_m: float = 0.0,
    timestamp: datetime | None = None,
    packet_id: int = 0,
    record_number: int = 0,
    object_id: int = 1,
) -> bytes:
    """Return a complete binary EGTS_PT_APPDATA packet with one EGTS_SR_POS_DATA subrecord."""
    ts = timestamp or datetime.now(tz=timezone.utc)
    ntm = max(0, int(ts.timestamp()) - _EGTS_EPOCH)

    lat_raw = int(abs(latitude) * (0xFFFFFFFF / 90.0)) & 0xFFFFFFFF
    lon_raw = int(abs(longitude) * (0xFFFFFFFF / 180.0)) & 0xFFFFFFFF

    pos_flags = 0x01  # VLD
    if latitude < 0:
        pos_flags |= 0x20  # LAHS
    if longitude < 0:
        pos_flags |= 0x40  # LOHS

    speed_raw = min(int(speed_kmh * 10), 0x3FFF)
    spd_lo = speed_raw & 0xFF
    spd_hi = (speed_raw >> 8) & 0x3F

    dir_int = int(direction_deg) % 360
    dir_byte = dir_int & 0xFF
    if dir_int > 255:
        spd_hi |= 0x80  # DIRH

    alt_int = int(abs(altitude_m))
    if altitude_m < 0:
        pos_flags |= 0x10  # ALTS

    sr_body = struct.pack(
        "<IIIBBBBBBBBB",
        ntm, lat_raw, lon_raw,
        pos_flags, spd_lo, spd_hi, dir_byte,
        0, 0, 0,   # ODOMETER
        0,         # DIN
        0,         # SRC
        0,         # reserved
    ) + bytes([alt_int & 0xFF, (alt_int >> 8) & 0xFF, (alt_int >> 16) & 0xFF]) + struct.pack("<H", 0)

    subrecord = struct.pack("<BH", EGTS_SR_POS_DATA, len(sr_body)) + sr_body

    sdr_header = struct.pack(
        "<HHBIB",
        len(subrecord),
        record_number & 0xFFFF,
        0x01,  # FLAGS: OBFE (OID present)
        object_id,
        EGTS_TELEDATA_SERVICE,
    )
    body = sdr_header + subrecord
    return _build_transport_header(len(body), packet_id, EGTS_PT_APPDATA) + body + struct.pack("<H", _crc16(body))


def build_egts_response(rpid: int, pc: int = EGTS_PC_OK, packet_id: int = 0) -> bytes:
    """Return an EGTS_PT_RESPONSE packet acknowledging rpid."""
    body = struct.pack("<HB", rpid & 0xFFFF, pc)
    return _build_transport_header(len(body), packet_id, EGTS_PT_RESPONSE) + body + struct.pack("<H", _crc16(body))


# ─── Decoder ──────────────────────────────────────────────────────────────────

def decode_egts_packet(data: bytes) -> dict:
    """
    Decode a raw EGTS binary packet.
    Returns a dict with parsed fields.
    Raises ValueError on CRC mismatch or malformed data.
    """
    if len(data) < _TRANSPORT_HEADER_SIZE:
        raise ValueError(f"Packet too short: {len(data)} bytes")

    pre, ver, skid, flags, hl, fdl, pid, pt = struct.unpack_from("<BBBBBHHB", data, 0)
    hcs_stored = data[10]
    if _crc8(data[:10]) != hcs_stored:
        raise ValueError(f"Header CRC mismatch: stored={hcs_stored:#04x}")
    if pre != 0x01:
        raise ValueError(f"Invalid PRE byte: {pre:#04x}")

    body = data[hl: hl + fdl]
    sfrcs_stored = struct.unpack_from("<H", data, hl + fdl)[0]
    if _crc16(body) != sfrcs_stored:
        raise ValueError(f"Body CRC mismatch: stored={sfrcs_stored:#06x}")

    result: dict = {"packet_id": pid, "packet_type": pt, "records": []}
    if pt != EGTS_PT_APPDATA:
        return result

    offset = 0
    while offset + 5 <= len(body):
        rl, rn, rec_flags = struct.unpack_from("<HHB", body, offset)
        offset += 5
        obj_id = None
        if rec_flags & 0x01:  # OBFE
            if offset + 4 > len(body):
                break
            obj_id = struct.unpack_from("<I", body, offset)[0]
            offset += 4
        if offset >= len(body):
            break
        sst = body[offset]
        offset += 1

        record: dict = {"record_number": rn, "object_id": obj_id, "service_type": sst, "subrecords": []}
        parsed = 0
        while parsed < rl and offset + 3 <= len(body):
            srt = body[offset]
            srl = struct.unpack_from("<H", body, offset + 1)[0]
            offset += 3
            parsed += 3
            sr_data = body[offset: offset + srl]
            offset += srl
            parsed += srl

            sub: dict = {"subrecord_type": srt, "length": srl}
            if srt == EGTS_SR_POS_DATA and len(sr_data) >= 16:
                ntm, lat_raw, lon_raw, pos_flags = struct.unpack_from("<IIIB", sr_data, 0)
                spd_lo, spd_hi, dir_byte = struct.unpack_from("<BBB", sr_data, 13)
                lat = lat_raw * 90.0 / 0xFFFFFFFF
                lon = lon_raw * 180.0 / 0xFFFFFFFF
                if pos_flags & 0x20:
                    lat = -lat
                if pos_flags & 0x40:
                    lon = -lon
                sub.update({
                    "latitude": round(lat, 7),
                    "longitude": round(lon, 7),
                    "speed_kmh": round(((spd_hi & 0x3F) << 8 | spd_lo) / 10.0, 1),
                    "direction_deg": float(dir_byte + (256 if spd_hi & 0x80 else 0)),
                    "timestamp_utc": datetime.fromtimestamp(ntm + _EGTS_EPOCH, tz=timezone.utc).isoformat(),
                    "valid_fix": bool(pos_flags & 0x01),
                })
            record["subrecords"].append(sub)
        result["records"].append(record)

    return result
