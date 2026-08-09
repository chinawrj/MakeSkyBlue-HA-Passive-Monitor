#!/usr/bin/env python3
"""Reassemble and analyze passive G1/G2 Modbus RTU capture logs."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


RAW_RE = re.compile(
    r"(?:boot=(?P<boot_id>\d+) )?#(?P<sequence>\d+) "
    r"G(?P<source>[12])/GPIO[12] "
    r"(?P<count>\d+)B RAW_HEX=(?P<hex>[0-9A-Fa-f. ]*?[0-9A-Fa-f])"
    r"(?: \((?P<count2>\d+)\))?\s*$"
)
STATE_RE = re.compile(
    r"'G(?P<source>[12]) Last UART Chunk' >> "
    r"'(?:boot=(?P<boot_id>\d+) )?#(?P<sequence>\d+) "
    r"(?P<count>\d+)B (?P<hex>[0-9A-Fa-f]+)'\s*$"
)
TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))"
)
CAPTURE_START_RE = re.compile(
    r"^(?P<timestamp>\S+) CAPTURE_START .*duration_seconds=(?P<duration>\d+) "
    r"git_commit=(?P<commit>\S+) git_dirty=(?P<dirty>true|false) "
    r"config_sha256=(?P<config_sha256>\S+) firmware_sha256=(?P<firmware_sha256>\S+)"
    r"(?: stale_output_seconds=(?P<stale_output_seconds>[0-9.]+)"
    r" heartbeat_seconds=(?P<heartbeat_seconds>[0-9.]+))?"
)
CAPTURE_END_RE = re.compile(
    r"^(?P<timestamp>\S+) CAPTURE_END reason=(?P<reason>\S+)"
)
CONNECT_ATTEMPT_RE = re.compile(
    r"^(?P<timestamp>\S+) CONNECT_ATTEMPT device=(?P<device>\S+)"
)
DISCONNECTED_RE = re.compile(
    r"^(?P<timestamp>\S+) DISCONNECTED(?: reason=(?P<reason>\S+)"
    r" returncode=(?P<returncode>-?\d+))? retry_seconds=(?P<retry_seconds>[0-9.]+)"
)
STALE_OUTPUT_RE = re.compile(
    r"^(?P<timestamp>\S+) STALE_OUTPUT child_pid=(?P<child_pid>\d+) "
    r"seconds_since_output=(?P<seconds_since_output>[0-9.]+) action=reconnect"
)
CAPTURE_HEARTBEAT_RE = re.compile(
    r"^(?P<timestamp>\S+) CAPTURE_HEARTBEAT child_pid=(?P<child_pid>\d+) "
    r"child_alive=(?P<child_alive>true|false) "
    r"seconds_since_output=(?P<seconds_since_output>[0-9.]+)"
)
PROJECT_VERSION_RE = re.compile(
    r"Project (?P<project>\S+) version (?P<version>\S+)"
)
BOOT_RECORD_RE = re.compile(
    r"BOOT session=(?P<boot_id>\d+) buffered as seq=(?P<sequence>\d+)"
    r"(?: uptime_ms=(?P<uptime_ms>\d+))?"
)
DEVICE_HEARTBEAT_RE = re.compile(
    r"boot=(?P<boot_id>\d+) #(?P<sequence>\d+) HEARTBEAT "
    r"uptime_ms=(?P<uptime_ms>\d+)"
)
HA_CSV_REQUIRED_FIELDS = (
    "ha_timestamp",
    "boot_id",
    "record_type",
    "sequence",
    "uptime_ms",
    "source",
    "gpio",
    "byte_count",
    "overwritten_total",
    "buffer_remaining",
    "message",
    "hex",
)
UINT32_MAX = (1 << 32) - 1


def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def crc_valid(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    expected = frame[-2] | (frame[-1] << 8)
    return modbus_crc(frame[:-2]) == expected


def timestamp_iso8601(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return ""
    return (
        dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def timestamp_from_log_line(line: str) -> int | None:
    match = TIMESTAMP_RE.match(line)
    if match is None:
        return None
    return int(
        dt.datetime.fromisoformat(
            match.group("timestamp").replace("Z", "+00:00")
        ).timestamp()
        * 1000
    )


def load_register_catalog(path: Path) -> dict[int, dict[str, str]]:
    """Load this project's evidence-backed D-register CSV catalog."""
    catalog: dict[int, dict[str, str]] = {}
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            address = int(row["register"])
            catalog[address] = {
                "status": row["status"].strip(),
                "mapped_fields": row["mapped_fields"].strip(),
                "evidence": row["evidence"].strip(),
            }
    return catalog


def load_register_mapping(path: Path) -> dict[int, list[str]]:
    """Load D-register field labels for backwards-compatible callers."""
    mapping: dict[int, list[str]] = {}
    for address, entry in load_register_catalog(path).items():
        mapping[address] = [
            value.strip()
            for value in entry["mapped_fields"].split("|")
            if value.strip()
        ]
    return mapping


@dataclass
class Chunk:
    sequence: int
    source: int
    data: bytes
    boot_id: int = 0
    order: int = -1
    timestamp_ms: int | None = None
    capture_time_ms: int | None = None
    uptime_ms: int | None = None


@dataclass
class HACsvCapture:
    uart_chunks: list[Chunk] = field(default_factory=list)
    sequence_records: list[Chunk] = field(default_factory=list)
    records: list[dict[str, object]] = field(default_factory=list)
    summary: dict[str, object] = field(default_factory=dict)


@dataclass
class Frame:
    source: int
    first_sequence: int
    last_sequence: int
    data: bytes
    boot_id: int = 0
    first_order: int = -1
    last_order: int = -1
    timestamp_ms: int | None = None
    capture_time_ms: int | None = None
    uptime_ms: int | None = None

    @property
    def address(self) -> int:
        return self.data[0]

    @property
    def function(self) -> int:
        return self.data[1]

    @property
    def hex(self) -> str:
        return self.data.hex().upper()


@dataclass
class ParseIssue:
    kind: str
    first_sequence: int
    last_sequence: int
    timestamp_ms: int | None
    capture_time_ms: int | None
    uptime_ms: int | None
    expected_length: int
    candidate_hex: str
    dropped_byte_hex: str


@dataclass
class StreamState:
    role: str
    data: bytearray = field(default_factory=bytearray)
    first_sequence: int = 0
    last_sequence: int = 0
    first_order: int = 0
    last_timestamp_ms: int | None = None
    last_capture_time_ms: int | None = None
    last_uptime_ms: int | None = None
    discarded_bytes: int = 0
    crc_errors: int = 0
    startup_markers: list[tuple[int, int, int | None, int | None]] = field(
        default_factory=list
    )
    issues: list[ParseIssue] = field(default_factory=list)


FIXED_REQUEST_LENGTHS = {
    0x01: 8,
    0x02: 8,
    0x03: 8,
    0x04: 8,
    0x05: 8,
    0x06: 8,
    0x07: 4,
    0x08: 8,
    0x0B: 4,
    0x0C: 4,
    0x11: 4,
    0x16: 10,
}
FIXED_RESPONSE_LENGTHS = {
    0x07: 5,
    0x05: 8,
    0x06: 8,
    0x08: 8,
    0x0B: 8,
    0x0F: 8,
    0x10: 8,
    0x16: 10,
}
WIFI_MODULE_STARTUP_MARKER = b"\x00\x00\x00\xFF"
FULLY_VALIDATED_FUNCTIONS = {0x03, 0x04, 0x06, 0x10}


def frame_semantic_status(frame: Frame) -> str:
    if not frame_structure_valid(frame):
        return "structurally_invalid"
    return (
        "fully_parsed"
        if (frame.function & 0x7F) in FULLY_VALIDATED_FUNCTIONS
        else "unsupported_semantics"
    )


def frame_structure_valid(frame: Frame) -> bool:
    function = frame.function & 0x7F
    if frame.address > 247:
        return False
    if frame.function & 0x80:
        return frame.source == 2 and frame.address >= 1 and len(frame.data) == 5
    if frame.source == 2 and frame.address == 0:
        return False
    if (
        frame.source == 1
        and frame.address == 0
        and function not in (0x05, 0x06, 0x0F, 0x10, 0x16)
    ):
        return False
    if function in (0x03, 0x04):
        if frame.source == 1:
            if len(frame.data) != 8:
                return False
            count = int.from_bytes(frame.data[4:6], "big")
            return frame.address != 0 and 1 <= count <= 125
        if len(frame.data) < 5:
            return False
        byte_count = frame.data[2]
        return byte_count % 2 == 0 and len(frame.data) == 5 + byte_count
    if function == 0x10:
        if frame.source == 1:
            if len(frame.data) < 9:
                return False
            count = int.from_bytes(frame.data[4:6], "big")
            byte_count = frame.data[6]
            return (
                frame.address <= 247
                and 1 <= count <= 123
                and byte_count == count * 2
                and len(frame.data) == 9 + byte_count
            )
        return len(frame.data) == 8
    if function == 0x06:
        return len(frame.data) == 8
    return True


def expected_length(role: str, data: bytearray) -> int | None:
    if len(data) < 2:
        return None
    function = data[1]
    if function & 0x80:
        return 5

    if role == "request":
        if function in FIXED_REQUEST_LENGTHS:
            return FIXED_REQUEST_LENGTHS[function]
        if function in (0x0F, 0x10):
            return None if len(data) < 7 else 9 + data[6]
        if function == 0x17:
            return None if len(data) < 11 else 13 + data[10]
        return -1

    if function in FIXED_RESPONSE_LENGTHS:
        return FIXED_RESPONSE_LENGTHS[function]
    if function in (0x01, 0x02, 0x03, 0x04, 0x0C, 0x11, 0x17):
        return None if len(data) < 3 else 5 + data[2]
    return -1


def parse_chunks(lines: Iterable[str]) -> list[Chunk]:
    chunks_by_key: dict[tuple[int, int], Chunk] = {}
    for order, line in enumerate(lines):
        match = RAW_RE.search(line)
        source_kind = "raw"
        if not match:
            match = STATE_RE.search(line)
            source_kind = "state"
        if not match:
            continue
        compact = re.sub(r"[. ]", "", match.group("hex"))
        data = bytes.fromhex(compact)
        declared = int(match.group("count"))
        count2 = match.groupdict().get("count2")
        declared2 = int(count2) if count2 is not None else declared
        if len(data) != declared or declared != declared2:
            raise ValueError(
                f"chunk length mismatch at sequence {match.group('sequence')}: "
                f"parsed={len(data)} declared={declared}/{declared2}"
            )
        chunk = Chunk(
            sequence=int(match.group("sequence")),
            source=int(match.group("source")),
            data=data,
            boot_id=int(match.group("boot_id") or 0),
            order=order,
            timestamp_ms=timestamp_from_log_line(line),
        )
        chunk.capture_time_ms = chunk.timestamp_ms
        key = (chunk.boot_id, chunk.sequence)
        existing = chunks_by_key.get(key)
        if existing is not None and (
            existing.source != chunk.source or existing.data != chunk.data
        ):
            raise ValueError(
                f"conflicting {source_kind} chunk at boot={chunk.boot_id} "
                f"sequence {chunk.sequence}"
            )
        if existing is None:
            chunks_by_key[key] = chunk
    return list(chunks_by_key.values())


def parse_log_sequence_records(
    lines: Iterable[str], uart_chunks: Iterable[Chunk]
) -> list[Chunk]:
    """Merge UART chunks with internal BOOT/HEARTBEAT sequence records."""
    records_by_key: dict[tuple[int, int], Chunk] = {
        (chunk.boot_id, chunk.sequence): chunk for chunk in uart_chunks
    }
    for order, line in enumerate(lines):
        match = BOOT_RECORD_RE.search(line)
        if match is None:
            match = DEVICE_HEARTBEAT_RE.search(line)
        if match is None:
            continue
        uptime_text = match.groupdict().get("uptime_ms")
        uptime_ms = int(uptime_text) if uptime_text is not None else None
        record = Chunk(
            sequence=int(match.group("sequence")),
            source=0,
            data=b"",
            boot_id=int(match.group("boot_id")),
            order=order,
            timestamp_ms=timestamp_from_log_line(line),
            capture_time_ms=uptime_ms,
            uptime_ms=uptime_ms,
        )
        key = (record.boot_id, record.sequence)
        existing = records_by_key.get(key)
        if existing is not None:
            if existing.source != 0 or existing.uptime_ms != record.uptime_ms:
                raise ValueError(
                    "conflicting API log record at "
                    f"boot={record.boot_id} sequence={record.sequence}"
                )
            continue
        records_by_key[key] = record
    return sorted(records_by_key.values(), key=lambda record: record.order)


def parse_iso8601_ms(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return int(parsed.timestamp() * 1000)


def parse_uint_field(row: dict[str, str], field_name: str) -> int:
    raw_value = row.get(field_name, "")
    if not isinstance(raw_value, str):
        raise ValueError(f"{field_name} is missing")
    value = raw_value.strip()
    if not value or not value.isdecimal():
        raise ValueError(f"{field_name} is not an unsigned decimal integer")
    parsed = int(value)
    if parsed > UINT32_MAX:
        raise ValueError(f"{field_name} exceeds uint32")
    return parsed


def parse_ha_capture_csv(path: Path, boot_id: int | None) -> HACsvCapture:
    """Load the append-ACK HA CSV as the authoritative capture record stream."""
    source_bytes = path.read_bytes()
    malformed_rows: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    records_by_key: dict[tuple[int, int], dict[str, object]] = {}
    total_rows = 0
    other_boot_rows = 0
    selected_rows = 0
    duplicate_rows = 0

    text = source_bytes.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    fieldnames = tuple(reader.fieldnames or ())
    missing_fields = [
        field_name
        for field_name in HA_CSV_REQUIRED_FIELDS
        if field_name not in fieldnames
    ]
    if missing_fields:
        malformed_rows.append(
            {
                "row_number": 1,
                "reason": "missing_header_fields",
                "fields": missing_fields,
            }
        )

    for row_number, row in enumerate(reader, start=2):
        total_rows += 1
        row_copy = {
            str(key): "" if value is None else str(value)
            for key, value in row.items()
            if key is not None
        }
        try:
            if row.get(None):
                raise ValueError("row has unquoted extra CSV columns")
            row_boot_id = parse_uint_field(row_copy, "boot_id")
            if row_boot_id == 0:
                raise ValueError("boot_id must be nonzero")
            if boot_id is not None and row_boot_id != boot_id:
                other_boot_rows += 1
                continue
            selected_rows += 1
            sequence = parse_uint_field(row_copy, "sequence")
            uptime_ms = parse_uint_field(row_copy, "uptime_ms")
            overwritten_total = parse_uint_field(row_copy, "overwritten_total")
            buffer_remaining = parse_uint_field(row_copy, "buffer_remaining")
            if buffer_remaining >= 256:
                raise ValueError("buffer_remaining is outside the 256-record ring")
            timestamp_ms = parse_iso8601_ms(row_copy.get("ha_timestamp", ""))
            record_type = row_copy.get("record_type", "").strip().upper()
            source = row_copy.get("source", "").strip().upper()
            gpio = row_copy.get("gpio", "").strip().upper()
            byte_count = parse_uint_field(row_copy, "byte_count")
            compact_hex = re.sub(r"[. ]", "", row_copy.get("hex", "").strip())
            if compact_hex and not re.fullmatch(r"[0-9A-Fa-f]+", compact_hex):
                raise ValueError("hex contains non-hexadecimal characters")
            if len(compact_hex) % 2:
                raise ValueError("hex contains an odd number of nibbles")
            payload = bytes.fromhex(compact_hex)
            message = row_copy.get("message", "").strip()

            if record_type in ("BOOT", "HEARTBEAT"):
                if source != "ONBOARD" or gpio != "INTERNAL":
                    raise ValueError(
                        f"{record_type} source/gpio must be ONBOARD/INTERNAL"
                    )
                if byte_count != 0 or payload:
                    raise ValueError(
                        f"{record_type} must have byte_count=0 and empty hex"
                    )
                if not message:
                    raise ValueError(f"{record_type} message is empty")
                source_number = 0
            elif record_type == "UART":
                if source not in ("G1", "G2"):
                    raise ValueError("UART source must be G1 or G2")
                source_number = int(source[1])
                if gpio != f"GPIO{source_number}":
                    raise ValueError("UART source and gpio disagree")
                if not (1 <= byte_count <= 96):
                    raise ValueError("UART byte_count is outside 1..96")
                if len(payload) != byte_count:
                    raise ValueError(
                        f"UART payload length {len(payload)} != byte_count {byte_count}"
                    )
            else:
                raise ValueError("record_type must be BOOT, HEARTBEAT, or UART")

            normalized: dict[str, object] = {
                "boot_id": row_boot_id,
                "sequence": sequence,
                "uptime_ms": uptime_ms,
                "record_type": record_type,
                "source": source,
                "source_number": source_number,
                "gpio": gpio,
                "byte_count": byte_count,
                "payload": payload,
                "message": message,
                "overwritten_total": overwritten_total,
                "buffer_remaining": buffer_remaining,
                "timestamp_ms": timestamp_ms,
                "row_number": row_number,
            }
            key = (row_boot_id, sequence)
            existing = records_by_key.get(key)
            if existing is not None:
                stable_fields = (
                    "uptime_ms",
                    "record_type",
                    "source",
                    "gpio",
                    "byte_count",
                    "payload",
                    "message",
                    "overwritten_total",
                )
                differences = [
                    name for name in stable_fields if existing[name] != normalized[name]
                ]
                if differences:
                    conflicts.append(
                        {
                            "boot_id": row_boot_id,
                            "sequence": sequence,
                            "first_row_number": existing["row_number"],
                            "conflicting_row_number": row_number,
                            "differing_fields": differences,
                            "first_hex": bytes(existing["payload"]).hex().upper(),
                            "conflicting_hex": payload.hex().upper(),
                            "first_record": {
                                name: (
                                    bytes(existing[name]).hex().upper()
                                    if name == "payload"
                                    else existing[name]
                                )
                                for name in stable_fields
                            },
                            "conflicting_record": {
                                name: (
                                    payload.hex().upper()
                                    if name == "payload"
                                    else normalized[name]
                                )
                                for name in stable_fields
                            },
                        }
                    )
                else:
                    duplicate_rows += 1
                continue
            records_by_key[key] = normalized
        except (ValueError, TypeError) as error:
            malformed_rows.append(
                {
                    "row_number": row_number,
                    "reason": str(error),
                    "row": row_copy,
                }
            )

    selected_records = sorted(
        records_by_key.values(), key=lambda record: int(record["row_number"])
    )
    sequence_records = [
        Chunk(
            sequence=int(record["sequence"]),
            source=int(record["source_number"]),
            data=bytes(record["payload"]),
            boot_id=int(record["boot_id"]),
            order=int(record["row_number"]),
            timestamp_ms=int(record["timestamp_ms"]),
            capture_time_ms=int(record["uptime_ms"]),
            uptime_ms=int(record["uptime_ms"]),
        )
        for record in selected_records
    ]
    uart_chunks = [
        chunk
        for chunk, record in zip(sequence_records, selected_records)
        if record["record_type"] == "UART"
    ]
    boot_records = [
        record for record in selected_records if record["record_type"] == "BOOT"
    ]
    heartbeat_records = [
        record
        for record in selected_records
        if record["record_type"] == "HEARTBEAT"
    ]
    overwritten_values = [
        int(record["overwritten_total"]) for record in selected_records
    ]
    summary: dict[str, object] = {
        "analysis_chunk_source": "ha_csv",
        "ha_csv_path": str(path),
        "ha_csv_bytes": len(source_bytes),
        "ha_csv_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "ha_csv_header_fields": list(fieldnames),
        "ha_csv_missing_header_fields": missing_fields,
        "ha_csv_total_data_rows": total_rows,
        "ha_csv_selected_boot_id": boot_id,
        "ha_csv_selected_data_rows": selected_rows,
        "ha_csv_other_boot_rows": other_boot_rows,
        "ha_csv_unique_record_count": len(selected_records),
        "ha_csv_uart_record_count": len(uart_chunks),
        "ha_csv_boot_record_count": len(boot_records),
        "ha_csv_boot_sequences": [int(record["sequence"]) for record in boot_records],
        "ha_csv_first_record_type": (
            str(selected_records[0]["record_type"]) if selected_records else None
        ),
        "ha_csv_heartbeat_record_count": len(heartbeat_records),
        "ha_csv_heartbeat_sequences": [
            int(record["sequence"]) for record in heartbeat_records
        ],
        "ha_csv_duplicate_record_count": duplicate_rows,
        "ha_csv_conflicting_duplicate_count": len(conflicts),
        "ha_csv_conflicting_duplicates": conflicts,
        "ha_csv_malformed_row_count": len(malformed_rows),
        "ha_csv_malformed_rows": malformed_rows,
        "ha_csv_max_overwritten_total": max(overwritten_values, default=None),
        "ha_csv_last_buffer_remaining": (
            int(selected_records[-1]["buffer_remaining"]) if selected_records else None
        ),
    }
    return HACsvCapture(
        uart_chunks=uart_chunks,
        sequence_records=sequence_records,
        records=selected_records,
        summary=summary,
    )


def feed(state: StreamState, chunk: Chunk) -> list[Frame]:
    if not state.data:
        state.first_sequence = chunk.sequence
        state.first_order = chunk.order
    state.last_sequence = chunk.sequence
    state.last_timestamp_ms = chunk.timestamp_ms
    state.last_capture_time_ms = chunk.capture_time_ms
    state.last_uptime_ms = chunk.uptime_ms
    state.data.extend(chunk.data)
    frames: list[Frame] = []

    while state.data:
        if state.role == "request":
            prefix_length = min(len(state.data), len(WIFI_MODULE_STARTUP_MARKER))
            if state.data[:prefix_length] == WIFI_MODULE_STARTUP_MARKER[:prefix_length]:
                if len(state.data) < len(WIFI_MODULE_STARTUP_MARKER):
                    break
                state.startup_markers.append(
                    (
                        state.first_sequence,
                        chunk.sequence,
                        chunk.timestamp_ms,
                        chunk.uptime_ms,
                    )
                )
                del state.data[: len(WIFI_MODULE_STARTUP_MARKER)]
                state.first_sequence = chunk.sequence
                state.first_order = chunk.order
                continue
        length = expected_length(state.role, state.data)
        if length is None:
            break
        if length < 4 or length > 260:
            state.issues.append(
                ParseIssue(
                    kind="unsupported_function_or_length",
                    first_sequence=state.first_sequence,
                    last_sequence=chunk.sequence,
                    timestamp_ms=chunk.timestamp_ms,
                    capture_time_ms=chunk.capture_time_ms,
                    uptime_ms=chunk.uptime_ms,
                    expected_length=length,
                    candidate_hex=bytes(state.data[:32]).hex().upper(),
                    dropped_byte_hex=f"{state.data[0]:02X}",
                )
            )
            del state.data[0]
            state.discarded_bytes += 1
            state.first_sequence = chunk.sequence
            state.first_order = chunk.order
            continue
        if len(state.data) < length:
            break

        candidate = bytes(state.data[:length])
        if not crc_valid(candidate):
            state.issues.append(
                ParseIssue(
                    kind="crc_error_resync",
                    first_sequence=state.first_sequence,
                    last_sequence=chunk.sequence,
                    timestamp_ms=chunk.timestamp_ms,
                    capture_time_ms=chunk.capture_time_ms,
                    uptime_ms=chunk.uptime_ms,
                    expected_length=length,
                    candidate_hex=candidate.hex().upper(),
                    dropped_byte_hex=f"{state.data[0]:02X}",
                )
            )
            del state.data[0]
            state.crc_errors += 1
            state.discarded_bytes += 1
            state.first_sequence = chunk.sequence
            state.first_order = chunk.order
            continue

        frames.append(
            Frame(
                source=chunk.source,
                first_sequence=state.first_sequence,
                last_sequence=chunk.sequence,
                data=candidate,
                boot_id=chunk.boot_id,
                first_order=state.first_order,
                last_order=chunk.order,
                timestamp_ms=chunk.timestamp_ms,
                capture_time_ms=chunk.capture_time_ms,
                uptime_ms=chunk.uptime_ms,
            )
        )
        del state.data[:length]
        state.first_sequence = chunk.sequence
        state.first_order = chunk.order

    return frames


def request_details(frame: Frame) -> dict[str, object]:
    function = frame.function
    details: dict[str, object] = {
        "address": frame.address,
        "function": f"0x{function:02X}",
        "kind": "request",
    }
    if function in (0x01, 0x02):
        details["start_bit"] = int.from_bytes(frame.data[2:4], "big")
        details["start_bit_label"] = (
            "C" if function == 0x01 else "DI"
        ) + str(details["start_bit"])
        details["bit_count"] = int.from_bytes(frame.data[4:6], "big")
        details["operation"] = "read_bits"
    elif function in (0x03, 0x04):
        details["start_register"] = int.from_bytes(frame.data[2:4], "big")
        details["start_register_label"] = f"D{details['start_register']}"
        details["register_count"] = int.from_bytes(frame.data[4:6], "big")
        details["operation"] = "read"
    elif function in (0x05, 0x06):
        address = int.from_bytes(frame.data[2:4], "big")
        if function == 0x05:
            details["start_bit"] = address
            details["start_bit_label"] = f"C{address}"
            details["bit_count"] = 1
        else:
            details["start_register"] = address
            details["start_register_label"] = f"D{address}"
            details["register_count"] = 1
        details["operation"] = (
            "observed_write_coil"
            if function == 0x05
            else "observed_write_register"
        )
        details["write_data_hex"] = frame.data[4:6].hex().upper()
    elif function in (0x0F, 0x10):
        address = int.from_bytes(frame.data[2:4], "big")
        count = int.from_bytes(frame.data[4:6], "big")
        if function == 0x0F:
            details["start_bit"] = address
            details["start_bit_label"] = f"C{address}"
            details["bit_count"] = count
        else:
            details["start_register"] = address
            details["start_register_label"] = f"D{address}"
            details["register_count"] = count
        details["operation"] = (
            "observed_write_coils"
            if function == 0x0F
            else "observed_write_registers"
        )
        details["write_data_hex"] = frame.data[7:-2].hex().upper()
    elif function == 0x16:
        details["start_register"] = int.from_bytes(frame.data[2:4], "big")
        details["start_register_label"] = f"D{details['start_register']}"
        details["register_count"] = 1
        details["operation"] = "observed_mask_write_register"
        details["and_mask"] = int.from_bytes(frame.data[4:6], "big")
        details["or_mask"] = int.from_bytes(frame.data[6:8], "big")
        details["write_data_hex"] = frame.data[4:8].hex().upper()
    elif function == 0x17:
        details["start_register"] = int.from_bytes(frame.data[2:4], "big")
        details["start_register_label"] = f"D{details['start_register']}"
        details["register_count"] = int.from_bytes(frame.data[4:6], "big")
        details["write_start_register"] = int.from_bytes(frame.data[6:8], "big")
        details["write_register_count"] = int.from_bytes(frame.data[8:10], "big")
        details["operation"] = "observed_read_write_registers"
        details["write_data_hex"] = frame.data[11:-2].hex().upper()
    else:
        details["operation"] = "other"
    return details


def response_matches(request: Frame, response: Frame) -> bool:
    if request.boot_id != response.boot_id:
        return False
    if request.address != response.address:
        return False
    response_function = response.function & 0x7F
    if request.function != response_function:
        return False
    if response.function & 0x80:
        return True
    if request.function in (0x01, 0x02):
        count = int.from_bytes(request.data[4:6], "big")
        return response.data[2] == (count + 7) // 8
    if request.function in (0x03, 0x04, 0x17):
        count = int.from_bytes(request.data[4:6], "big")
        return response.data[2] == count * 2
    # Observed device behavior: the FC06 response retains the register address
    # but returns a separate 16-bit device value instead of echoing the write.
    # Keep both values explicit in the CSV rather than forcing standard echo
    # semantics onto this wire trace.
    if request.function == 0x06:
        return response.data[2:4] == request.data[2:4]
    if request.function in (0x05, 0x08, 0x16):
        return response.data[:-2] == request.data[:-2]
    if request.function in (0x0F, 0x10):
        return response.data[2:6] == request.data[2:6]
    return request.function in (0x07, 0x0B, 0x0C, 0x11)


def frame_completion_order(frame: Frame) -> tuple[int, int, int]:
    return (frame.boot_id, frame.last_sequence, frame.source)


def sequence_integrity(
    chunks: list[Chunk],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Separate missing sequence numbers from logger emission reordering."""
    sequences_by_boot: dict[int, set[int]] = {}
    for chunk in chunks:
        sequences_by_boot.setdefault(chunk.boot_id, set()).add(chunk.sequence)

    first_chunk_by_key: dict[tuple[int, int], Chunk] = {}
    for chunk in chunks:
        first_chunk_by_key.setdefault((chunk.boot_id, chunk.sequence), chunk)

    gaps: list[dict[str, object]] = []
    for boot_id, sequences in sequences_by_boot.items():
        ordered = sorted(sequences)
        for previous, current in zip(ordered, ordered[1:]):
            if current == previous + 1:
                continue
            previous_chunk = first_chunk_by_key[(boot_id, previous)]
            current_chunk = first_chunk_by_key[(boot_id, current)]
            gaps.append(
                {
                    "boot_id": boot_id,
                    "after": previous,
                    "before": current,
                    "expected": previous + 1,
                    "missing_count": current - previous - 1,
                    "after_source": f"G{previous_chunk.source}",
                    "before_source": f"G{current_chunk.source}",
                    "after_log_order": previous_chunk.order,
                    "before_log_order": current_chunk.order,
                    "after_timestamp_ms": previous_chunk.timestamp_ms,
                    "before_timestamp_ms": current_chunk.timestamp_ms,
                    "after_capture_time_ms": previous_chunk.capture_time_ms,
                    "before_capture_time_ms": current_chunk.capture_time_ms,
                    "after_uptime_ms": previous_chunk.uptime_ms,
                    "before_uptime_ms": current_chunk.uptime_ms,
                    "after_timestamp_iso8601": timestamp_iso8601(
                        previous_chunk.timestamp_ms
                    ),
                    "before_timestamp_iso8601": timestamp_iso8601(
                        current_chunk.timestamp_ms
                    ),
                }
            )

    out_of_order: list[dict[str, object]] = []
    previous_by_boot: dict[int, Chunk] = {}
    for chunk in chunks:
        previous = previous_by_boot.get(chunk.boot_id)
        if previous is not None and chunk.sequence < previous.sequence:
            out_of_order.append(
                {
                    "boot_id": chunk.boot_id,
                    "after": previous.sequence,
                    "before": chunk.sequence,
                    "previous_log_order": previous.order,
                    "current_log_order": chunk.order,
                    "previous_timestamp_ms": previous.timestamp_ms,
                    "current_timestamp_ms": chunk.timestamp_ms,
                    "previous_capture_time_ms": previous.capture_time_ms,
                    "current_capture_time_ms": chunk.capture_time_ms,
                    "previous_uptime_ms": previous.uptime_ms,
                    "current_uptime_ms": chunk.uptime_ms,
                    "previous_timestamp_iso8601": timestamp_iso8601(
                        previous.timestamp_ms
                    ),
                    "current_timestamp_iso8601": timestamp_iso8601(
                        chunk.timestamp_ms
                    ),
                }
            )
        previous_by_boot[chunk.boot_id] = chunk
    return gaps, out_of_order


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def parse_capture_metadata(lines: Iterable[str]) -> dict[str, object]:
    starts: list[re.Match[str]] = []
    ends: list[re.Match[str]] = []
    transport_events: list[dict[str, object]] = []
    project_versions: list[dict[str, str]] = []
    for line in lines:
        start = CAPTURE_START_RE.match(line)
        if start:
            starts.append(start)
        end = CAPTURE_END_RE.match(line)
        if end:
            ends.append(end)
        project_version = PROJECT_VERSION_RE.search(line)
        if project_version:
            candidate = {
                "project": project_version.group("project"),
                "version": project_version.group("version"),
            }
            if candidate not in project_versions:
                project_versions.append(candidate)
        for kind, pattern in (
            ("connect_attempt", CONNECT_ATTEMPT_RE),
            ("disconnected", DISCONNECTED_RE),
            ("stale_output", STALE_OUTPUT_RE),
            ("capture_heartbeat", CAPTURE_HEARTBEAT_RE),
        ):
            event = pattern.match(line)
            if not event:
                continue
            values = dict(
                (name, value)
                for name, value in event.groupdict().items()
                if value is not None
            )
            event_time = dt.datetime.fromisoformat(
                values["timestamp"].replace("Z", "+00:00")
            )
            values.update(
                {
                    "kind": kind,
                    "timestamp_ms": int(event_time.timestamp() * 1000),
                }
            )
            for key in ("child_pid", "returncode"):
                if key in values:
                    values[key] = int(values[key])
            for key in ("retry_seconds", "seconds_since_output"):
                if key in values:
                    values[key] = float(values[key])
            if "child_alive" in values:
                values["child_alive"] = values["child_alive"] == "true"
            transport_events.append(values)
            break
    result: dict[str, object] = {
        "capture_start_count": len(starts),
        "capture_end_count": len(ends),
        "capture_complete": False,
        "transport_connect_attempt_count": sum(
            event["kind"] == "connect_attempt" for event in transport_events
        ),
        "transport_disconnect_count": sum(
            event["kind"] == "disconnected" for event in transport_events
        ),
        "transport_stale_output_count": sum(
            event["kind"] == "stale_output" for event in transport_events
        ),
        "capture_heartbeat_count": sum(
            event["kind"] == "capture_heartbeat" for event in transport_events
        ),
        "capture_transport_events": transport_events,
        "capture_reported_projects": project_versions,
        "capture_reported_project_count": len(project_versions),
    }
    if len(starts) == 1:
        start_time = dt.datetime.fromisoformat(
            starts[0].group("timestamp").replace("Z", "+00:00")
        )
        result.update(
            {
                "capture_planned_duration_seconds": int(starts[0].group("duration")),
                "capture_start_timestamp": starts[0].group("timestamp"),
                "capture_start_timestamp_ms": int(start_time.timestamp() * 1000),
                "git_commit": starts[0].group("commit"),
                "git_dirty": starts[0].group("dirty") == "true",
                "config_sha256": starts[0].group("config_sha256"),
                "firmware_sha256": starts[0].group("firmware_sha256"),
            }
        )
        if starts[0].group("stale_output_seconds") is not None:
            result["capture_stale_output_limit_seconds"] = float(
                starts[0].group("stale_output_seconds")
            )
            result["capture_heartbeat_interval_seconds"] = float(
                starts[0].group("heartbeat_seconds")
            )
    if len(starts) == 1 and len(ends) == 1:
        start_time = dt.datetime.fromisoformat(
            starts[0].group("timestamp").replace("Z", "+00:00")
        )
        end_time = dt.datetime.fromisoformat(
            ends[0].group("timestamp").replace("Z", "+00:00")
        )
        result.update(
            {
                "capture_end_timestamp": ends[0].group("timestamp"),
                "capture_end_timestamp_ms": int(end_time.timestamp() * 1000),
                "capture_end_reason": ends[0].group("reason"),
                "capture_elapsed_seconds": (end_time - start_time).total_seconds(),
                "capture_complete": ends[0].group("reason") == "duration_complete",
            }
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("--frames-csv", type=Path)
    parser.add_argument("--registers-csv", type=Path)
    parser.add_argument(
        "--ha-csv",
        type=Path,
        help=(
            "use the Home Assistant append-and-ACK CSV as the authoritative "
            "record stream; the positional API log remains capture metadata "
            "and transport diagnostics"
        ),
    )
    parser.add_argument(
        "--boot-id",
        type=int,
        help=(
            "select one uint32 boot session from --ha-csv; when omitted, a "
            "single boot ID in the API diagnostic log is selected automatically"
        ),
    )
    parser.add_argument(
        "--expected-project-version",
        help=(
            "require the device runtime log to report this ESPHome project "
            "version (for example 0.1.0-alpha.10)"
        ),
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        help="write the summary plus a sibling .sha256 integrity file",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return non-zero when sequence/frame integrity checks fail",
    )
    parser.add_argument(
        "--mapping-csv",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "registers"
        / "makeskyblue-observed-registers.csv",
    )
    parser.add_argument("--min-duration-seconds", type=int, default=86400)
    parser.add_argument(
        "--max-idle-seconds",
        type=float,
        default=180.0,
        help=(
            "fail strict validation when no UART chunk is observed for longer "
            "than this interval, including capture boundaries"
        ),
    )
    parser.add_argument(
        "--max-checkpoint-interval-seconds",
        type=float,
        default=45.0,
        help=(
            "fail HA CSV strict validation when BOOT/HEARTBEAT device-uptime "
            "checkpoints are farther apart than this interval"
        ),
    )
    args = parser.parse_args()

    register_catalog = load_register_catalog(args.mapping_csv)
    register_mapping = load_register_mapping(args.mapping_csv)

    capture_bytes = args.capture.read_bytes()
    capture_lines = capture_bytes.decode("utf-8").splitlines()
    capture_metadata = parse_capture_metadata(capture_lines)
    diagnostic_chunks = parse_chunks(capture_lines)
    diagnostic_sequence_records = parse_log_sequence_records(
        capture_lines, diagnostic_chunks
    )
    diagnostic_sequence_gaps, diagnostic_sequence_out_of_order = sequence_integrity(
        diagnostic_sequence_records
    )
    selected_boot_id = args.boot_id
    if selected_boot_id is not None and not (1 <= selected_boot_id <= UINT32_MAX):
        parser.error("--boot-id must be in the uint32 range 1..4294967295")
    diagnostic_boot_ids = sorted(
        {record.boot_id for record in diagnostic_sequence_records}
    )
    if args.ha_csv and selected_boot_id is None and len(diagnostic_boot_ids) == 1:
        selected_boot_id = diagnostic_boot_ids[0]

    ha_capture: HACsvCapture | None = None
    if args.ha_csv:
        ha_capture = parse_ha_capture_csv(args.ha_csv, selected_boot_id)
        chunks = ha_capture.uart_chunks
        sequence_records = ha_capture.sequence_records
    else:
        chunks = diagnostic_chunks
        sequence_records = diagnostic_sequence_records

    boot_log_order: dict[int, int] = {}
    for chunk in sequence_records:
        boot_log_order.setdefault(chunk.boot_id, chunk.order)
    ordered_chunks = sorted(
        chunks,
        key=lambda chunk: (
            boot_log_order[chunk.boot_id],
            chunk.sequence,
            chunk.order,
        ),
    )
    states: dict[tuple[int, int], StreamState] = {}
    frames: list[Frame] = []
    for chunk in ordered_chunks:
        key = (chunk.boot_id, chunk.source)
        state = states.setdefault(
            key,
            StreamState(role="request" if chunk.source == 1 else "response"),
        )
        frames.extend(feed(state, chunk))
    # Pair in completion order. A long G2 response can start before the G1
    # request's idle-timeout callback, yet finish after that request callback.
    # Sorting by first_sequence incorrectly reports that valid exchange as an
    # unpaired response; firmware also handles frames only when complete.
    frames.sort(
        key=lambda frame: (
            boot_log_order.get(frame.boot_id, 0),
            frame.last_sequence,
            frame.source,
            frame.first_sequence,
        )
    )

    sequence_gaps, sequence_out_of_order = sequence_integrity(sequence_records)

    pending: deque[Frame] = deque()
    frame_rows: list[dict[str, object]] = []
    register_rows: list[dict[str, object]] = []
    function_counts: Counter[str] = Counter()
    unpaired_responses = 0
    expired_requests = 0
    structurally_invalid_frames = 0
    broadcast_requests = 0
    fc06_rows_by_request: dict[tuple[int, int], dict[str, object]] = {}
    pending_fc06_readback: dict[tuple[int, int], dict[str, object]] = {}

    for (boot_id, source), state in states.items():
        if source == 1:
            for (
                first_sequence,
                last_sequence,
                timestamp_ms,
                uptime_ms,
            ) in state.startup_markers:
                frame_rows.append(
                    {
                        "boot_id": boot_id,
                        "source": "G1",
                        "first_sequence": first_sequence,
                        "last_sequence": last_sequence,
                        "kind": "wifi_module_startup_marker",
                        "operation": "startup_marker",
                        "semantic_status": "fully_parsed",
                        "completion_timestamp_ms": (
                            timestamp_ms if timestamp_ms is not None else ""
                        ),
                        "completion_timestamp_iso8601": timestamp_iso8601(
                            timestamp_ms
                        ),
                        "completion_uptime_ms": (
                            uptime_ms if uptime_ms is not None else ""
                        ),
                        "frame_hex": WIFI_MODULE_STARTUP_MARKER.hex().upper(),
                        "crc_valid": "",
                    }
                )

        for issue in state.issues:
            frame_rows.append(
                {
                    "boot_id": boot_id,
                    "source": f"G{source}",
                    "first_sequence": issue.first_sequence,
                    "last_sequence": issue.last_sequence,
                    "completion_timestamp_ms": issue.timestamp_ms or "",
                    "completion_timestamp_iso8601": timestamp_iso8601(
                        issue.timestamp_ms
                    ),
                        "completion_uptime_ms": (
                            issue.uptime_ms if issue.uptime_ms is not None else ""
                        ),
                    "kind": issue.kind,
                    "operation": "stream_resynchronization",
                    "semantic_status": "parse_error",
                    "parse_issue_expected_length": issue.expected_length,
                    "parse_issue_dropped_byte_hex": issue.dropped_byte_hex,
                    "frame_hex": issue.candidate_hex,
                    "crc_valid": False if issue.kind == "crc_error_resync" else "",
                }
            )
        if state.data:
            frame_rows.append(
                {
                    "boot_id": boot_id,
                    "source": f"G{source}",
                    "first_sequence": state.first_sequence,
                    "last_sequence": state.last_sequence,
                    "completion_timestamp_ms": state.last_timestamp_ms or "",
                    "completion_timestamp_iso8601": timestamp_iso8601(
                        state.last_timestamp_ms
                    ),
                    "completion_uptime_ms": (
                        state.last_uptime_ms
                        if state.last_uptime_ms is not None
                        else ""
                    ),
                    "kind": "incomplete_stream_tail",
                    "operation": "stream_reassembly",
                    "semantic_status": "incomplete",
                    "parse_issue_expected_length": expected_length(
                        state.role, state.data
                    ),
                    "frame_hex": bytes(state.data).hex().upper(),
                    "crc_valid": "",
                }
            )

    for frame in frames:
        if frame.capture_time_ms is not None:
            retained: deque[Frame] = deque()
            while pending:
                request = pending.popleft()
                if (
                    request.capture_time_ms is not None
                    and frame.capture_time_ms - request.capture_time_ms > 1000
                ):
                    expired_requests += 1
                else:
                    retained.append(request)
            pending = retained
        function_counts[f"G{frame.source}_FC{frame.function:02X}"] += 1
        row: dict[str, object] = {
            "boot_id": frame.boot_id,
            "source": f"G{frame.source}",
            "first_sequence": frame.first_sequence,
            "last_sequence": frame.last_sequence,
            "address": frame.address,
            "function": f"0x{frame.function:02X}",
            "frame_hex": frame.hex,
            "crc_valid": True,
            "semantic_status": frame_semantic_status(frame),
            "completion_timestamp_ms": frame.timestamp_ms or "",
            "completion_timestamp_iso8601": timestamp_iso8601(frame.timestamp_ms),
            "completion_uptime_ms": (
                frame.uptime_ms if frame.uptime_ms is not None else ""
            ),
        }

        if not frame_structure_valid(frame):
            structurally_invalid_frames += 1
            row["kind"] = (
                "structurally_invalid_request"
                if frame.source == 1
                else "structurally_invalid_response"
            )
            frame_rows.append(row)
            continue

        if frame.source == 1:
            details = request_details(frame)
            row.update(details)
            if frame.address == 0:
                broadcast_requests += 1
                row["kind"] = "broadcast_request"
            else:
                pending.append(frame)
            if details.get("operation") in (
                "observed_write_register",
                "observed_write_registers",
                "observed_read_write_registers",
            ):
                start = int(
                    details.get("write_start_register", details["start_register"])
                )
                data = bytes.fromhex(str(details.get("write_data_hex", "")))
                for index in range(0, len(data) - 1, 2):
                    raw = int.from_bytes(data[index : index + 2], "big")
                    register_row: dict[str, object] = {
                            "boot_id": frame.boot_id,
                            "operation": str(details["operation"]),
                            "request_sequence": frame.first_sequence,
                            "response_sequence": "",
                            "function": f"0x{frame.function:02X}",
                            "completion_timestamp_ms": frame.timestamp_ms or "",
                            "completion_timestamp_iso8601": timestamp_iso8601(
                                frame.timestamp_ms
                            ),
                            "completion_uptime_ms": (
                                frame.uptime_ms
                                if frame.uptime_ms is not None
                                else ""
                            ),
                            "register": start + index // 2,
                            "register_label": f"D{start + index // 2}",
                            "mapped_fields": "|".join(
                                register_mapping.get(start + index // 2, [])
                            ),
                            "mapping_status": register_catalog.get(
                                start + index // 2, {}
                            ).get("status", "not_cataloged"),
                            "raw_u16": raw,
                            "raw_s16": raw - 0x10000 if raw & 0x8000 else raw,
                        }
                    register_rows.append(register_row)
                    if frame.function == 0x06:
                        fc06_rows_by_request[
                            (frame.boot_id, frame.first_sequence)
                        ] = register_row
                        pending_fc06_readback[
                            (frame.boot_id, start + index // 2)
                        ] = register_row
        else:
            match_index = next(
                (index for index, request in enumerate(pending) if response_matches(request, frame)),
                None,
            )
            if match_index is None:
                unpaired_responses += 1
                row["kind"] = "unpaired_response"
            else:
                request = pending[match_index]
                del pending[match_index]
                details = request_details(request)
                row.update(
                    {
                        "kind": "response",
                        "request_sequence": request.first_sequence,
                        "operation": details["operation"],
                        "start_register": details.get("start_register", ""),
                        "start_register_label": details.get(
                            "start_register_label", ""
                        ),
                        "register_count": details.get("register_count", ""),
                    }
                )
                if frame.function & 0x80:
                    row["exception_code"] = frame.data[2]
                elif request.function == 0x06:
                    response_value = int.from_bytes(frame.data[4:6], "big")
                    row["write_response_value_u16"] = response_value
                    row["write_response_value_s16"] = (
                        response_value - 0x10000
                        if response_value & 0x8000
                        else response_value
                    )
                    row["write_response_data_hex"] = frame.data[4:6].hex().upper()
                    tracked_write = fc06_rows_by_request.get(
                        (request.boot_id, request.first_sequence)
                    )
                    if tracked_write is not None:
                        tracked_write["response_sequence"] = frame.first_sequence
                        tracked_write["write_response_timestamp_ms"] = (
                            frame.timestamp_ms or ""
                        )
                        tracked_write["write_response_timestamp_iso8601"] = (
                            timestamp_iso8601(frame.timestamp_ms)
                        )
                        tracked_write["write_response_value_u16"] = response_value
                        tracked_write["write_response_value_s16"] = (
                            response_value - 0x10000
                            if response_value & 0x8000
                            else response_value
                        )
                elif request.function in (0x03, 0x04, 0x17):
                    start = int(details["start_register"])
                    payload = frame.data[3:-2]
                    for index in range(0, len(payload), 2):
                        raw = int.from_bytes(payload[index : index + 2], "big")
                        register_address = start + index // 2
                        register_rows.append(
                            {
                                "boot_id": frame.boot_id,
                                "operation": "read_response",
                                "request_sequence": request.first_sequence,
                                "response_sequence": frame.first_sequence,
                                "function": f"0x{request.function:02X}",
                                "completion_timestamp_ms": frame.timestamp_ms or "",
                                "completion_timestamp_iso8601": timestamp_iso8601(
                                    frame.timestamp_ms
                                ),
                                "completion_uptime_ms": (
                                    frame.uptime_ms
                                    if frame.uptime_ms is not None
                                    else ""
                                ),
                                "register": register_address,
                                "register_label": f"D{register_address}",
                                "mapped_fields": "|".join(
                                    register_mapping.get(start + index // 2, [])
                                ),
                                "mapping_status": register_catalog.get(
                                    start + index // 2, {}
                                ).get("status", "not_cataloged"),
                                "raw_u16": raw,
                                "raw_s16": raw - 0x10000 if raw & 0x8000 else raw,
                            }
                        )
                        tracked_write = pending_fc06_readback.pop(
                            (frame.boot_id, register_address), None
                        )
                        if tracked_write is not None:
                            tracked_write["readback_sequence"] = frame.first_sequence
                            tracked_write["readback_timestamp_ms"] = (
                                frame.timestamp_ms or ""
                            )
                            tracked_write["readback_timestamp_iso8601"] = (
                                timestamp_iso8601(frame.timestamp_ms)
                            )
                            tracked_write["readback_raw_u16"] = raw
                            tracked_write["readback_status"] = (
                                "match"
                                if raw == int(tracked_write["raw_u16"])
                                else "mismatch"
                            )
        frame_rows.append(row)

    observed_modbus_slave_addresses = sorted(
        {
            frame.address
            for frame in frames
            if frame_structure_valid(frame) and 1 <= frame.address <= 247
        }
    )
    diagnostic_summary: dict[str, object] = {
        "analysis_chunk_source": "api_log" if ha_capture is None else "ha_csv",
        "api_log_chunk_count": len(diagnostic_chunks),
        "api_log_record_count": len(diagnostic_sequence_records),
        "api_log_boot_sessions": diagnostic_boot_ids,
        "api_log_boot_session_count": len(diagnostic_boot_ids),
        "api_log_sequence_gap_count": len(diagnostic_sequence_gaps),
        "api_log_missing_sequence_count": sum(
            int(gap["missing_count"]) for gap in diagnostic_sequence_gaps
        ),
        "api_log_sequence_gaps": diagnostic_sequence_gaps,
        "api_log_sequence_out_of_order_count": len(
            diagnostic_sequence_out_of_order
        ),
        "api_log_sequence_out_of_order": diagnostic_sequence_out_of_order,
    }
    if ha_capture is not None:
        diagnostic_summary.update(ha_capture.summary)
        diagnostic_summary["ha_csv_selected_boot_matches_api_log"] = (
            selected_boot_id is not None
            and selected_boot_id in diagnostic_boot_ids
        )

    summary = {
        "analysis_timestamp_iso8601": timestamp_iso8601(
            int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
        ),
        "capture_path": str(args.capture),
        "capture_bytes": len(capture_bytes),
        "capture_sha256": hashlib.sha256(capture_bytes).hexdigest(),
        "chunks": len(chunks),
        "capture_records": len(sequence_records),
        "boot_sessions": sorted({chunk.boot_id for chunk in sequence_records}),
        "boot_session_count": len(
            {chunk.boot_id for chunk in sequence_records}
        ),
        "first_sequence": sequence_records[0].sequence if sequence_records else None,
        "last_sequence": sequence_records[-1].sequence if sequence_records else None,
        "sequence_gap_count": len(sequence_gaps),
        "sequence_gaps": sequence_gaps,
        "missing_sequence_count": sum(
            gap["missing_count"] for gap in sequence_gaps
        ),
        "sequence_out_of_order_count": len(sequence_out_of_order),
        "sequence_out_of_order": sequence_out_of_order,
        "frames": len(frames),
        "observed_modbus_slave_addresses": observed_modbus_slave_addresses,
        "observed_modbus_slave_address_count": len(
            observed_modbus_slave_addresses
        ),
        "function_counts": dict(sorted(function_counts.items())),
        "pending_requests": len(pending),
        "expired_requests": expired_requests,
        "unpaired_responses": unpaired_responses,
        "structurally_invalid_frames": structurally_invalid_frames,
        "broadcast_requests": broadcast_requests,
        "wifi_module_startup_markers": sum(
            len(state.startup_markers)
            for (_boot_id, source), state in states.items()
            if source == 1
        ),
        "unsupported_semantic_frames": sum(
            1
            for frame in frames
            if (frame.function & 0x7F) not in FULLY_VALIDATED_FUNCTIONS
        ),
        "register_catalog_addresses": len(register_catalog),
        "confirmed_semantic_addresses": sum(
            entry["status"] == "confirmed_semantic"
            for entry in register_catalog.values()
        ),
        "provisional_semantic_addresses": sum(
            entry["status"] == "provisional_semantic"
            for entry in register_catalog.values()
        ),
        "raw_only_addresses": sum(
            entry["status"] == "observed_raw"
            for entry in register_catalog.values()
        ),
        "unresolved_semantic_addresses": sum(
            entry["status"] != "confirmed_semantic"
            for entry in register_catalog.values()
        ),
        "unresolved_semantic_address_labels": [
            f"D{address}"
            for address, entry in sorted(register_catalog.items())
            if entry["status"] != "confirmed_semantic"
        ],
        "startup_marker_sequences": [
            {
                "boot_id": boot_id,
                "first": first,
                "last": last,
                "completion_timestamp_ms": timestamp_ms,
                "completion_timestamp_iso8601": timestamp_iso8601(timestamp_ms),
                "completion_uptime_ms": uptime_ms,
            }
            for (boot_id, source), state in states.items()
            if source == 1
            for first, last, timestamp_ms, uptime_ms in state.startup_markers
        ],
        "parse_issue_count": sum(len(state.issues) for state in states.values())
        + sum(bool(state.data) for state in states.values()),
        "parse_issue_contexts": [
            {
                "boot_id": boot_id,
                "source": f"G{source}",
                "kind": issue.kind,
                "first_sequence": issue.first_sequence,
                "last_sequence": issue.last_sequence,
                "completion_timestamp_ms": issue.timestamp_ms,
                "completion_timestamp_iso8601": timestamp_iso8601(
                    issue.timestamp_ms
                ),
                "completion_uptime_ms": issue.uptime_ms,
                "expected_length": issue.expected_length,
                "dropped_byte_hex": issue.dropped_byte_hex,
                "candidate_hex": issue.candidate_hex,
            }
            for (boot_id, source), state in states.items()
            for issue in state.issues
        ],
        "streams": {
            f"boot={boot_id}/G{source}": {
                "discarded_bytes": state.discarded_bytes,
                "crc_errors": state.crc_errors,
                "incomplete_bytes": len(state.data),
                "incomplete_hex": bytes(state.data).hex().upper(),
            }
            for (boot_id, source), state in states.items()
        },
        **diagnostic_summary,
        **capture_metadata,
    }

    not_cataloged_register_addresses = sorted(
        {
            int(row["register"])
            for row in register_rows
            if row.get("mapping_status") == "not_cataloged"
        }
    )
    summary["not_cataloged_register_addresses"] = not_cataloged_register_addresses
    summary["not_cataloged_register_address_labels"] = [
        f"D{address}" for address in not_cataloged_register_addresses
    ]

    read_observed_register_addresses = sorted(
        {
            int(row["register"])
            for row in register_rows
            if row.get("operation") == "read_response"
            and int(row["register"]) in register_catalog
        }
    )
    missing_read_register_addresses = sorted(
        set(register_catalog) - set(read_observed_register_addresses)
    )
    summary["read_observed_register_addresses"] = read_observed_register_addresses
    summary["read_observed_register_address_count"] = len(
        read_observed_register_addresses
    )
    summary["missing_read_register_addresses"] = missing_read_register_addresses
    summary["missing_read_register_address_labels"] = [
        f"D{address}" for address in missing_read_register_addresses
    ]

    timestamp_missing_chunks = [chunk for chunk in chunks if chunk.timestamp_ms is None]
    capture_time_missing_chunks = [
        chunk for chunk in chunks if chunk.capture_time_ms is None
    ]
    idle_segments: list[dict[str, object]] = []
    start_ms = capture_metadata.get("capture_start_timestamp_ms")
    end_ms = capture_metadata.get("capture_end_timestamp_ms")
    wall_clock_chunks = sorted(
        (chunk for chunk in chunks if chunk.timestamp_ms is not None),
        key=lambda chunk: (int(chunk.timestamp_ms), chunk.order),
    )
    timestamped_chunks = (
        [chunk for chunk in ordered_chunks if chunk.timestamp_ms is not None]
        if ha_capture is not None
        else wall_clock_chunks
    )
    if ha_capture is None and timestamped_chunks and isinstance(start_ms, int):
        idle_segments.append(
            {
                "kind": "capture_start_to_first_chunk",
                "seconds": max(
                    0.0, (timestamped_chunks[0].timestamp_ms - start_ms) / 1000.0
                ),
                "before_sequence": None,
                "after_sequence": timestamped_chunks[0].sequence,
                "time_basis": "wall_clock",
            }
        )
    capture_timed_chunks = (
        [chunk for chunk in ordered_chunks if chunk.capture_time_ms is not None]
        if ha_capture is not None
        else wall_clock_chunks
    )
    if ha_capture is not None and sequence_records and capture_timed_chunks:
        boot_record = next(
            (record for record in sequence_records if record.source == 0), None
        )
        if boot_record is not None and boot_record.capture_time_ms is not None:
            idle_segments.append(
                {
                    "kind": "boot_to_first_uart_chunk",
                    "seconds": (
                        (
                            capture_timed_chunks[0].capture_time_ms
                            - boot_record.capture_time_ms
                        )
                        & UINT32_MAX
                    )
                    / 1000.0,
                    "before_sequence": boot_record.sequence,
                    "after_sequence": capture_timed_chunks[0].sequence,
                    "time_basis": "device_uptime",
                }
            )
    for previous, current in zip(capture_timed_chunks, capture_timed_chunks[1:]):
        idle_segments.append(
            {
                "kind": "between_chunks",
                "seconds": (
                    (
                        (current.capture_time_ms - previous.capture_time_ms)
                        & UINT32_MAX
                    )
                    / 1000.0
                    if ha_capture is not None
                    else max(
                        0.0,
                        (current.capture_time_ms - previous.capture_time_ms)
                        / 1000.0,
                    )
                ),
                "before_sequence": previous.sequence,
                "after_sequence": current.sequence,
                "time_basis": (
                    "device_uptime" if ha_capture is not None else "wall_clock"
                ),
            }
        )
    if ha_capture is None and timestamped_chunks and isinstance(end_ms, int):
        idle_segments.append(
            {
                "kind": "last_chunk_to_capture_end",
                "seconds": max(
                    0.0, (end_ms - timestamped_chunks[-1].timestamp_ms) / 1000.0
                ),
                "before_sequence": timestamped_chunks[-1].sequence,
                "after_sequence": None,
                "time_basis": "wall_clock",
            }
        )
    device_uptime_span_seconds: float | None = None
    checkpoint_segments: list[dict[str, object]] = []
    if ha_capture is not None and sequence_records:
        onboard_records = [record for record in sequence_records if record.source == 0]
        if onboard_records:
            first_record = sequence_records[0]
            last_record = sequence_records[-1]
            device_uptime_span_seconds = (
                ((last_record.uptime_ms - first_record.uptime_ms) & UINT32_MAX)
                / 1000.0
            )
            for previous, current in zip(onboard_records, onboard_records[1:]):
                checkpoint_segments.append(
                    {
                        "before_sequence": previous.sequence,
                        "after_sequence": current.sequence,
                        "seconds": (
                            ((current.uptime_ms - previous.uptime_ms) & UINT32_MAX)
                            / 1000.0
                        ),
                    }
                )
            if onboard_records[-1].sequence != last_record.sequence:
                checkpoint_segments.append(
                    {
                        "before_sequence": onboard_records[-1].sequence,
                        "after_sequence": last_record.sequence,
                        "seconds": (
                            (
                                last_record.uptime_ms
                                - onboard_records[-1].uptime_ms
                            )
                            & UINT32_MAX
                        )
                        / 1000.0,
                    }
                )
            if capture_timed_chunks and last_record.uptime_ms is not None:
                idle_segments.append(
                    {
                        "kind": "last_uart_chunk_to_last_persisted_record",
                        "seconds": (
                            (
                                last_record.uptime_ms
                                - capture_timed_chunks[-1].capture_time_ms
                            )
                            & UINT32_MAX
                        )
                        / 1000.0,
                        "before_sequence": capture_timed_chunks[-1].sequence,
                        "after_sequence": last_record.sequence,
                        "time_basis": "device_uptime",
                    }
                )
    max_checkpoint_segment = max(
        checkpoint_segments,
        key=lambda segment: float(segment["seconds"]),
        default=None,
    )
    max_idle_segment = max(
        idle_segments, key=lambda segment: float(segment["seconds"]), default=None
    )
    summary["chunk_timestamp_missing_count"] = len(timestamp_missing_chunks)
    summary["chunk_capture_time_missing_count"] = len(capture_time_missing_chunks)
    summary["max_uart_idle_seconds"] = (
        float(max_idle_segment["seconds"]) if max_idle_segment else None
    )
    summary["max_uart_idle_segment"] = max_idle_segment
    summary["max_uart_idle_limit_seconds"] = args.max_idle_seconds
    summary["device_uptime_span_seconds"] = device_uptime_span_seconds
    summary["checkpoint_segments"] = checkpoint_segments
    summary["max_checkpoint_interval_seconds"] = (
        float(max_checkpoint_segment["seconds"])
        if max_checkpoint_segment is not None
        else None
    )
    summary["max_checkpoint_interval_limit_seconds"] = (
        args.max_checkpoint_interval_seconds
    )
    summary["expected_project_version"] = args.expected_project_version

    frame_rows.sort(
        key=lambda row: (
            int(row.get("boot_id", 0)),
            int(row.get("last_sequence", 0)),
            str(row.get("source", "")),
            int(row.get("first_sequence", 0)),
        )
    )

    if args.frames_csv:
        write_csv(
            args.frames_csv,
            frame_rows,
            [
                "source",
                "boot_id",
                "first_sequence",
                "last_sequence",
                "completion_timestamp_ms",
                "completion_timestamp_iso8601",
                "completion_uptime_ms",
                "kind",
                "operation",
                "semantic_status",
                "request_sequence",
                "address",
                "function",
                "start_register",
                "start_register_label",
                "register_count",
                "start_bit",
                "start_bit_label",
                "bit_count",
                "write_start_register",
                "write_register_count",
                "and_mask",
                "or_mask",
                "exception_code",
                "write_data_hex",
                "write_response_data_hex",
                "write_response_value_u16",
                "write_response_value_s16",
                "parse_issue_expected_length",
                "parse_issue_dropped_byte_hex",
                "crc_valid",
                "frame_hex",
            ],
        )
    if args.registers_csv:
        write_csv(
            args.registers_csv,
            register_rows,
            [
                "operation",
                "boot_id",
                "request_sequence",
                "response_sequence",
                "write_response_timestamp_ms",
                "write_response_timestamp_iso8601",
                "write_response_value_u16",
                "write_response_value_s16",
                "readback_sequence",
                "readback_timestamp_ms",
                "readback_timestamp_iso8601",
                "readback_raw_u16",
                "readback_status",
                "completion_timestamp_ms",
                "completion_timestamp_iso8601",
                "completion_uptime_ms",
                "function",
                "register",
                "register_label",
                "mapped_fields",
                "mapping_status",
                "raw_u16",
                "raw_s16",
            ],
        )

    summary_text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(summary_text, encoding="utf-8", newline="\n")
        checksum_path = args.summary_json.with_name(args.summary_json.name + ".sha256")
        checksum_path.write_text(
            f"{hashlib.sha256(summary_text.encode()).hexdigest()}  "
            f"{args.summary_json.name}\n",
            encoding="utf-8",
            newline="\n",
        )
    print(summary_text, end="")
    ha_integrity_failed = ha_capture is not None and (
        selected_boot_id is None
        or int(summary.get("api_log_boot_session_count", 0)) > 1
        or (
            int(summary.get("api_log_boot_session_count", 0)) == 1
            and summary.get("ha_csv_selected_boot_matches_api_log") is not True
        )
        or (
            args.boot_id is None
            and int(summary.get("api_log_boot_session_count", 0)) != 1
        )
        or bool(summary.get("ha_csv_missing_header_fields"))
        or int(summary.get("ha_csv_malformed_row_count", 0)) != 0
        or int(summary.get("ha_csv_conflicting_duplicate_count", 0)) != 0
        or int(summary.get("ha_csv_boot_record_count", 0)) != 1
        or summary.get("ha_csv_boot_sequences") != [1]
        or summary.get("ha_csv_first_record_type") != "BOOT"
        or int(summary.get("ha_csv_heartbeat_record_count", 0)) < 1
        or int(summary.get("ha_csv_other_boot_rows", 0)) != 0
        or not sequence_records
        or sequence_records[0].source != 0
        or sequence_records[0].sequence != 1
        or summary.get("ha_csv_max_overwritten_total") != 0
        or summary.get("ha_csv_last_buffer_remaining") != 0
        or float(summary.get("device_uptime_span_seconds") or 0)
        < args.min_duration_seconds
        or summary.get("max_checkpoint_interval_seconds") is None
        or float(summary.get("max_checkpoint_interval_seconds") or 0)
        > args.max_checkpoint_interval_seconds
    )
    integrity_failed = (
        ha_integrity_failed
        or bool(sequence_gaps)
        or summary["boot_session_count"] != 1
        or bool(pending)
        or expired_requests != 0
        or unpaired_responses != 0
        or structurally_invalid_frames != 0
        or bool(not_cataloged_register_addresses)
        or bool(missing_read_register_addresses)
        or summary["unsupported_semantic_frames"] != 0
        or summary["unresolved_semantic_addresses"] != 0
        or summary.get("capture_start_count") != 1
        or summary.get("capture_end_count") != 1
        or not summary.get("capture_complete", False)
        or summary.get("git_dirty") is not False
        or summary.get("firmware_sha256") in (None, "missing")
        or (
            args.expected_project_version is not None
            and summary.get("capture_reported_projects")
            != [
                {
                    "project": "rjwang.makeskyblue_modbus_monitor",
                    "version": args.expected_project_version,
                }
            ]
        )
        or int(summary.get("capture_planned_duration_seconds", 0))
        < args.min_duration_seconds
        or float(summary.get("capture_elapsed_seconds", 0))
        < args.min_duration_seconds
        or not chunks
        or bool(timestamp_missing_chunks)
        or bool(capture_time_missing_chunks)
        or max_idle_segment is None
        or float(max_idle_segment["seconds"]) > args.max_idle_seconds
        or any(
            state.crc_errors != 0
            or state.discarded_bytes != 0
            or bool(state.data)
            for state in states.values()
        )
    )
    return 2 if args.strict and integrity_failed else 0


if __name__ == "__main__":
    sys.exit(main())
