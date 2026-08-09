#!/usr/bin/env python3

import csv
import json
import hashlib
import subprocess
import sys
import unittest
import yaml
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.analyze_uart_capture import (
    Frame,
    StreamState,
    Chunk,
    feed,
    frame_completion_order,
    frame_semantic_status,
    frame_structure_valid,
    load_register_mapping,
    modbus_crc,
    parse_capture_metadata,
    parse_chunks,
    parse_ha_capture_csv,
    parse_log_sequence_records,
    request_details,
    response_matches,
    sequence_integrity,
    timestamp_iso8601,
)
from tools.capture_esphome_logs import stale_output_due
from tools.generate_makeskyblue_passive import (
    OBSERVED_REGISTERS,
    UPDATE_INTERVAL_SECONDS,
    generate_entities,
    write_mapping,
)
from tools.verify_public_release import ABSOLUTE_USER_PATH, INLINE_SECRET


class ParseChunkTests(unittest.TestCase):
    @staticmethod
    def _run_strict_fixture(
        directory: Path,
        catalog_addresses: tuple[int, ...],
        end_timestamp: str = "2026-08-08T00:00:01+08:00",
        duration_seconds: int = 1,
        max_idle_seconds: int = 10,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        mapping = directory / "mapping.csv"
        mapping.write_text(
            "register,register_label,status,mapped_fields,evidence\n"
            + "".join(
                f"{address},D{address},confirmed_semantic,field_{address},test\n"
                for address in catalog_addresses
            ),
            encoding="utf-8",
        )
        capture = directory / "capture.log"
        capture.write_text(
            "2026-08-08T00:00:00+08:00 CAPTURE_START device=x "
            f"duration_seconds={duration_seconds} git_commit=abc git_dirty=false "
            "config_sha256=aa firmware_sha256=bb\n"
            "2026-08-08T00:00:00.100+08:00 boot=7 #1 G1/GPIO1 8B "
            "RAW_HEX=01.03.00.00.00.01.84.0A\n"
            "2026-08-08T00:00:00.200+08:00 boot=7 #2 G2/GPIO2 7B "
            "RAW_HEX=01.03.02.00.01.79.84\n"
            f"{end_timestamp} CAPTURE_END reason=duration_complete\n",
            encoding="utf-8",
        )
        tool = (
            Path(__file__).resolve().parent.parent
            / "tools"
            / "analyze_uart_capture.py"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(tool),
                "--strict",
                "--mapping-csv",
                str(mapping),
                "--min-duration-seconds",
                str(duration_seconds),
                "--max-idle-seconds",
                str(max_idle_seconds),
                "--summary-json",
                str(directory / "summary.json"),
                str(capture),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        summary = json.loads(result.stdout)
        assert (directory / "summary.json").read_text(encoding="utf-8") == result.stdout
        checksum = (directory / "summary.json.sha256").read_text(encoding="utf-8")
        assert checksum == (
            f"{hashlib.sha256(result.stdout.encode()).hexdigest()}  "
            "summary.json\n"
        )
        assert summary["capture_path"] == str(capture)
        assert len(summary["capture_sha256"]) == 64
        return result, summary

    @staticmethod
    def _run_ha_csv_strict_fixture(
        directory: Path,
        *,
        overwritten_total: int = 0,
        conflicting_duplicate: bool = False,
        malformed_gpio: bool = False,
        include_boot: bool = True,
        response_uptime_ms: int = 200,
        include_other_boot: bool = False,
        boot_sequence: int = 1,
        final_buffer_remaining: int = 0,
        include_api_uart: bool = True,
        duration_seconds: int = 1,
        capture_end_timestamp: str = "2026-08-08T00:00:01+08:00",
        heartbeat_uptime_ms: int | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        mapping = directory / "mapping.csv"
        mapping.write_text(
            "register,register_label,status,mapped_fields,evidence\n"
            "0,D0,confirmed_semantic,field_0,test\n",
            encoding="utf-8",
        )
        capture = directory / "capture.log"
        api_uart_lines = (
            "2026-08-08T00:00:00.100+08:00 boot=7 #2 G1/GPIO1 8B "
            "RAW_HEX=01.03.00.00.00.01.84.0A\n"
            "2026-08-08T00:00:00.200+08:00 boot=7 #5 G2/GPIO2 7B "
            "RAW_HEX=01.03.02.00.01.79.84\n"
            if include_api_uart
            else ""
        )
        capture.write_text(
            (
            "2026-08-08T00:00:00+08:00 CAPTURE_START device=x "
            f"duration_seconds={duration_seconds} git_commit=abc git_dirty=false "
            "config_sha256=aa firmware_sha256=bb\n"
            "2026-08-08T00:00:00.010+08:00 [I][app]: Project "
            "rjwang.makeskyblue_modbus_monitor version 0.1.0-alpha.10\n"
            + api_uart_lines
            + f"{capture_end_timestamp} CAPTURE_END reason=duration_complete\n"
            ),
            encoding="utf-8",
        )
        header = (
            "ha_timestamp,boot_id,record_type,sequence,uptime_ms,source,gpio,"
            "byte_count,overwritten_total,buffer_remaining,message,hex\n"
        )
        rows = []
        if include_boot:
            rows.append(
                "2026-08-08T00:00:00.050+08:00,7,BOOT,"
                f"{boot_sequence},10,ONBOARD,INTERNAL,"
                f"0,{overwritten_total},2,BOOT test,"
            )
        rows.extend(
            [
                "2026-08-08T00:00:00.100+08:00,7,UART,2,100,G1,"
                f"{'GPIO2' if malformed_gpio else 'GPIO1'},8,{overwritten_total},1,,"
                "010300000001840A",
                "2026-08-08T00:00:00.200+08:00,7,UART,3,"
                f"{response_uptime_ms},G2,GPIO2,7,"
                f"{overwritten_total},0,,01030200017984",
                # At-least-once replay: HA timestamp/buffer depth may differ while
                # the record identity and stable payload remain identical.
                "2026-08-08T00:00:00.300+08:00,7,UART,3,"
                f"{response_uptime_ms},G2,GPIO2,7,"
                f"{overwritten_total},2,,"
                + (
                    "01030200027985"
                    if conflicting_duplicate
                    else "01030200017984"
                ),
                "2026-08-08T00:00:01.000+08:00,7,HEARTBEAT,4,"
                f"{heartbeat_uptime_ms if heartbeat_uptime_ms is not None else max(1010, response_uptime_ms + 10)},"
                "ONBOARD,INTERNAL,0,"
                f"{overwritten_total},{final_buffer_remaining},"
                "HEARTBEAT test,",
            ]
        )
        if include_other_boot:
            rows.append(
                "2026-08-08T00:00:00.400+08:00,8,BOOT,1,10,ONBOARD,INTERNAL,"
                "0,0,0,BOOT other session,"
            )
        ha_csv = directory / "ha.csv"
        ha_csv.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
        tool = (
            Path(__file__).resolve().parent.parent
            / "tools"
            / "analyze_uart_capture.py"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(tool),
                "--strict",
                "--ha-csv",
                str(ha_csv),
                "--boot-id",
                "7",
                "--expected-project-version",
                "0.1.0-alpha.10",
                "--mapping-csv",
                str(mapping),
                "--min-duration-seconds",
                str(duration_seconds),
                "--max-idle-seconds",
                "10",
                str(capture),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result, json.loads(result.stdout)

    def test_short_chunk_without_repeated_length(self) -> None:
        chunks = parse_chunks(
            ["[D] monitor: #134 G1/GPIO1 1B RAW_HEX=01"]
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].sequence, 134)
        self.assertEqual(chunks[0].data, b"\x01")

    def test_public_release_secret_patterns_avoid_code_identifier_false_positives(self) -> None:
        self.assertIsNone(INLINE_SECRET.search("capture_key: >-"))
        self.assertIsNone(INLINE_SECRET.search("chunks_by_key: dict = {}"))
        self.assertIsNone(INLINE_SECRET.search("key: !secret api_key"))
        self.assertIsNone(INLINE_SECRET.search("password: !env_var CI_PASSWORD"))
        self.assertIsNotNone(INLINE_SECRET.search('key: "actual-secret"'))
        self.assertIsNotNone(INLINE_SECRET.search('wireguard_private_key: abc'))
        self.assertIsNotNone(
            ABSOLUTE_USER_PATH.search("/" + "Users/example/project")
        )
        self.assertIsNone(ABSOLUTE_USER_PATH.search("docs/project"))

    def test_chunk_with_repeated_length(self) -> None:
        chunks = parse_chunks(
            ["[D] monitor: #135 G2/GPIO2 3B RAW_HEX=01.03.02 (3)"]
        )
        self.assertEqual(chunks[0].data, b"\x01\x03\x02")

    def test_state_line_recovers_missing_raw_log_and_deduplicates(self) -> None:
        chunks = parse_chunks(
            [
                "[S][text_sensor]: 'G1 Last UART Chunk' >> '#1729 8B 01030000003D841B'",
                "[I][modbus_raw]: #1729 G1/GPIO1 8B RAW_HEX=01.03.00.00.00.3D.84.1B (8)",
                "[S][text_sensor]: 'G2 Last UART Chunk' >> '#1730 3B 010302'",
            ]
        )
        self.assertEqual([chunk.sequence for chunk in chunks], [1729, 1730])
        self.assertEqual(chunks[1].data, b"\x01\x03\x02")

    def test_boot_session_is_part_of_chunk_identity(self) -> None:
        chunks = parse_chunks(
            [
                "2026-08-08T00:00:00+08:00 boot=11 #1 G1/GPIO1 1B RAW_HEX=01",
                "2026-08-08T00:00:01+08:00 boot=22 #1 G1/GPIO1 1B RAW_HEX=02",
            ]
        )
        self.assertEqual([(c.boot_id, c.sequence) for c in chunks], [(11, 1), (22, 1)])
        self.assertLess(chunks[0].timestamp_ms, chunks[1].timestamp_ms)
        self.assertIsNone(chunks[0].uptime_ms)

    def test_api_log_sequence_includes_boot_and_device_heartbeat(self) -> None:
        lines = [
            "2026-08-08T00:00:00+08:00 [W][modbus_buffer]: BOOT "
            "session=7 buffered as seq=1 uptime_ms=5, ring capacity=256 records",
            "2026-08-08T00:00:00.100+08:00 boot=7 #2 G1/GPIO1 8B "
            "RAW_HEX=01.03.00.00.00.01.84.0A",
            "2026-08-08T00:00:30+08:00 [I][modbus_checkpoint]: "
            "boot=7 #3 HEARTBEAT uptime_ms=30005",
            "2026-08-08T00:00:30.100+08:00 boot=7 #4 G2/GPIO2 7B "
            "RAW_HEX=01.03.02.00.01.79.84",
        ]
        uart_chunks = parse_chunks(lines)
        records = parse_log_sequence_records(lines, uart_chunks)
        gaps, out_of_order = sequence_integrity(records)
        self.assertEqual([record.sequence for record in records], [1, 2, 3, 4])
        self.assertEqual([record.source for record in records], [0, 1, 0, 2])
        self.assertEqual(records[2].uptime_ms, 30005)
        self.assertEqual(gaps, [])
        self.assertEqual(out_of_order, [])

    def test_mismatched_length_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_chunks(["#136 G1/GPIO1 2B RAW_HEX=01"])

    def test_ha_csv_is_authoritative_and_allows_identical_replay(self) -> None:
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            result, summary = self._run_ha_csv_strict_fixture(
                directory
            )
            persisted = parse_ha_capture_csv(directory / "ha.csv", 7)
            request_frames = feed(
                StreamState(role="request"), persisted.uart_chunks[0]
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["analysis_chunk_source"], "ha_csv")
        self.assertEqual(summary["first_sequence"], 1)
        self.assertEqual(summary["last_sequence"], 4)
        self.assertEqual(summary["missing_sequence_count"], 0)
        self.assertEqual(summary["api_log_missing_sequence_count"], 2)
        self.assertEqual(summary["ha_csv_duplicate_record_count"], 1)
        self.assertEqual(summary["ha_csv_conflicting_duplicate_count"], 0)
        self.assertEqual(summary["ha_csv_boot_record_count"], 1)
        self.assertEqual(summary["ha_csv_heartbeat_record_count"], 1)
        self.assertEqual(summary["ha_csv_max_overwritten_total"], 0)
        self.assertEqual(
            summary["capture_reported_projects"],
            [
                {
                    "project": "rjwang.makeskyblue_modbus_monitor",
                    "version": "0.1.0-alpha.10",
                }
            ],
        )
        self.assertEqual(persisted.uart_chunks[0].uptime_ms, 100)
        self.assertEqual(request_frames[0].uptime_ms, 100)
        self.assertEqual(
            summary["max_uart_idle_segment"]["time_basis"], "device_uptime"
        )

    def test_ha_csv_requires_final_ring_drain(self) -> None:
        with TemporaryDirectory() as directory_name:
            result, summary = self._run_ha_csv_strict_fixture(
                Path(directory_name), final_buffer_remaining=5
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["ha_csv_last_buffer_remaining"], 5)

    def test_explicit_ha_boot_does_not_require_api_uart_chunks(self) -> None:
        with TemporaryDirectory() as directory_name:
            result, summary = self._run_ha_csv_strict_fixture(
                Path(directory_name), include_api_uart=False
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["api_log_chunk_count"], 0)
        self.assertEqual(summary["api_log_boot_session_count"], 0)

    def test_ha_wall_clock_delay_cannot_fake_24h_device_activity(self) -> None:
        with TemporaryDirectory() as directory_name:
            result, summary = self._run_ha_csv_strict_fixture(
                Path(directory_name),
                duration_seconds=86400,
                capture_end_timestamp="2026-08-09T00:00:00+08:00",
                heartbeat_uptime_ms=1010,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["capture_elapsed_seconds"], 86400)
        self.assertEqual(summary["device_uptime_span_seconds"], 1.0)

    def test_ha_csv_conflicting_duplicate_fails_strict_with_raw_context(self) -> None:
        with TemporaryDirectory() as directory_name:
            result, summary = self._run_ha_csv_strict_fixture(
                Path(directory_name), conflicting_duplicate=True
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["ha_csv_conflicting_duplicate_count"], 1)
        conflict = summary["ha_csv_conflicting_duplicates"][0]
        self.assertEqual(conflict["boot_id"], 7)
        self.assertEqual(conflict["sequence"], 3)
        self.assertEqual(conflict["first_hex"], "01030200017984")
        self.assertEqual(conflict["conflicting_hex"], "01030200027985")

    def test_ha_csv_uses_device_uptime_to_reject_compressed_append_gap(self) -> None:
        with TemporaryDirectory() as directory_name:
            result, summary = self._run_ha_csv_strict_fixture(
                Path(directory_name), response_uptime_ms=100_200
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            summary["max_uart_idle_segment"]["time_basis"], "device_uptime"
        )
        self.assertAlmostEqual(summary["max_uart_idle_seconds"], 100.1)

    def test_ha_csv_malformed_gpio_and_overwrite_fail_strict(self) -> None:
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            result, summary = self._run_ha_csv_strict_fixture(
                directory, overwritten_total=4, malformed_gpio=True
            )
            parsed = parse_ha_capture_csv(directory / "ha.csv", 7)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["ha_csv_malformed_row_count"], 1)
        self.assertIn(
            "source and gpio disagree",
            summary["ha_csv_malformed_rows"][0]["reason"],
        )
        self.assertEqual(summary["ha_csv_max_overwritten_total"], 4)
        self.assertEqual(parsed.summary["ha_csv_max_overwritten_total"], 4)

    def test_ha_csv_requires_the_onboarding_record(self) -> None:
        with TemporaryDirectory() as directory_name:
            result, summary = self._run_ha_csv_strict_fixture(
                Path(directory_name), include_boot=False
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["ha_csv_boot_record_count"], 0)

    def test_ha_csv_rejects_another_boot_in_the_formal_file(self) -> None:
        with TemporaryDirectory() as directory_name:
            result, summary = self._run_ha_csv_strict_fixture(
                Path(directory_name), include_other_boot=True
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["ha_csv_other_boot_rows"], 1)

    def test_ha_csv_requires_boot_sequence_one(self) -> None:
        with TemporaryDirectory() as directory_name:
            result, summary = self._run_ha_csv_strict_fixture(
                Path(directory_name), boot_sequence=0
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["ha_csv_boot_sequences"], [0])

    def test_crc_resynchronization_preserves_raw_context(self) -> None:
        state = StreamState(role="request")
        frames = feed(
            state,
            Chunk(
                200,
                1,
                bytes.fromhex("0103000000010000"),
                boot_id=9,
                timestamp_ms=1786118400456,
            ),
        )
        self.assertEqual(frames, [])
        self.assertGreaterEqual(state.crc_errors, 1)
        issue = state.issues[0]
        self.assertEqual(issue.kind, "crc_error_resync")
        self.assertEqual(issue.first_sequence, 200)
        self.assertEqual(issue.last_sequence, 200)
        self.assertEqual(issue.expected_length, 8)
        self.assertEqual(issue.candidate_hex, "0103000000010000")
        self.assertEqual(issue.dropped_byte_hex, "01")
        self.assertEqual(issue.timestamp_ms, 1786118400456)

    def test_d_register_mapping_keeps_raw_and_named_fields_distinct(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        mapping = load_register_mapping(
            repo / "registers" / "makeskyblue-observed-registers.csv"
        )
        self.assertIn("grid_voltage_range", mapping[1])
        self.assertIn("observed_raw_u16", mapping[24])
        self.assertIn("load_power_factor", mapping[110])
        self.assertIn("inverter_power_factor", mapping[110])
        self.assertIn("pv_to_battery_max_charge_current_a", mapping[11])
        self.assertIn("inverter_max_grid_current_a", mapping[12])
        self.assertIn("observed_network_time_high_word", mapping[30])
        self.assertIn("observed_network_time_low_word", mapping[31])

    def test_wifi_module_startup_marker_is_not_crc_noise(self) -> None:
        state = StreamState(role="request")
        frames = feed(
            state,
            Chunk(477, 1, b"\x00\x00\x00\xff", timestamp_ms=1786118400123),
        )
        self.assertEqual(frames, [])
        self.assertEqual(
            state.startup_markers, [(477, 477, 1786118400123, None)]
        )
        self.assertEqual(
            timestamp_iso8601(1786118400123), "2026-08-07T16:00:00.123Z"
        )
        self.assertEqual(state.crc_errors, 0)
        self.assertEqual(state.discarded_bytes, 0)

    def test_strict_semantic_gate_classifies_unvalidated_function(self) -> None:
        validated = Frame(1, 1, 1, bytes.fromhex("010300000001840A"))
        unvalidated = Frame(1, 2, 2, bytes.fromhex("01050001FF00DDFA"))
        self.assertEqual(frame_semantic_status(validated), "fully_parsed")
        self.assertEqual(
            frame_semantic_status(unvalidated), "unsupported_semantics"
        )

    def test_observed_fc06_pairs_by_d_address_and_preserves_both_values(self) -> None:
        request = Frame(1, 1, 1, bytes.fromhex("0106000B0334F92F"))
        response = Frame(2, 2, 2, bytes.fromhex("0106000B0000F808"))
        self.assertTrue(frame_structure_valid(request))
        self.assertTrue(frame_structure_valid(response))
        self.assertEqual(frame_semantic_status(request), "fully_parsed")
        self.assertEqual(frame_semantic_status(response), "fully_parsed")
        self.assertTrue(response_matches(request, response))
        details = request_details(request)
        self.assertEqual(details["start_register_label"], "D11")
        self.assertEqual(details["write_data_hex"], "0334")

    def test_fc06_csv_records_first_readback_match_and_mismatch(self) -> None:
        def with_crc(pdu: bytes) -> bytes:
            crc = modbus_crc(pdu)
            return pdu + bytes((crc & 0xFF, crc >> 8))

        transactions = [
            (1, with_crc(bytes.fromhex("0106000B0334"))),
            (2, with_crc(bytes.fromhex("0106000B0000"))),
            (1, with_crc(bytes.fromhex("0103000B0001"))),
            (2, with_crc(bytes.fromhex("0103020334"))),
            (1, with_crc(bytes.fromhex("0106000A026C"))),
            (2, with_crc(bytes.fromhex("0106000A0000"))),
            (1, with_crc(bytes.fromhex("0103000A0001"))),
            (2, with_crc(bytes.fromhex("0103020258"))),
        ]
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            capture = directory / "capture.log"
            lines = []
            for sequence, (source, payload) in enumerate(transactions, 1):
                lines.append(
                    f"2026-08-08T00:00:00.{sequence:03d}+08:00 boot=7 "
                    f"#{sequence} G{source}/GPIO{source} {len(payload)}B "
                    f"RAW_HEX={payload.hex('.').upper()}"
                )
            capture.write_text("\n".join(lines) + "\n", encoding="utf-8")
            registers = directory / "registers.csv"
            tool = (
                Path(__file__).resolve().parent.parent
                / "tools"
                / "analyze_uart_capture.py"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(tool),
                    "--registers-csv",
                    str(registers),
                    str(capture),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with registers.open(encoding="utf-8") as source:
                write_rows = [
                    row
                    for row in csv.DictReader(source)
                    if row["operation"] == "observed_write_register"
                ]
        self.assertEqual(len(write_rows), 2)
        self.assertEqual(write_rows[0]["register_label"], "D11")
        self.assertEqual(write_rows[0]["write_response_value_u16"], "0")
        self.assertEqual(write_rows[0]["readback_raw_u16"], "820")
        self.assertEqual(write_rows[0]["readback_status"], "match")
        self.assertEqual(write_rows[1]["register_label"], "D10")
        self.assertEqual(write_rows[1]["readback_raw_u16"], "600")
        self.assertEqual(write_rows[1]["readback_status"], "mismatch")

    def test_malformed_fc10_is_structurally_invalid(self) -> None:
        malformed = Frame(
            1, 1, 1, bytes.fromhex("0110001E000202AAAA5B75")
        )
        self.assertFalse(frame_structure_valid(malformed))
        self.assertEqual(frame_semantic_status(malformed), "structurally_invalid")

    def test_modbus_unicast_address_range_is_enforced(self) -> None:
        valid_request = Frame(1, 1, 1, bytes.fromhex("F703000000010000"))
        reserved_request = Frame(1, 2, 2, bytes.fromhex("F803000000010000"))
        reserved_response = Frame(2, 3, 3, bytes.fromhex("FF030200010000"))
        broadcast_write = Frame(
            1, 4, 4, bytes.fromhex("0010001E000204112233440000")
        )
        self.assertTrue(frame_structure_valid(valid_request))
        self.assertFalse(frame_structure_valid(reserved_request))
        self.assertFalse(frame_structure_valid(reserved_response))
        self.assertTrue(frame_structure_valid(broadcast_write))

    def test_truncated_frames_are_structurally_invalid(self) -> None:
        short_read_request = Frame(1, 1, 1, bytes.fromhex("0103"))
        short_read_response = Frame(2, 2, 2, bytes.fromhex("0103"))
        short_fc10_request = Frame(1, 3, 3, bytes.fromhex("01100000"))
        self.assertFalse(frame_structure_valid(short_read_request))
        self.assertFalse(frame_structure_valid(short_read_response))
        self.assertFalse(frame_structure_valid(short_fc10_request))

    def test_coil_address_is_not_labeled_as_d_register(self) -> None:
        details = request_details(
            Frame(1, 1, 1, bytes.fromhex("01050001FF00DDFA"))
        )
        self.assertEqual(details["start_bit_label"], "C1")
        self.assertNotIn("start_register_label", details)

    def test_capture_metadata_requires_clean_complete_session(self) -> None:
        metadata = parse_capture_metadata(
            [
                "2026-08-08T00:00:00+08:00 CAPTURE_START device=x duration_seconds=86400 git_commit=abc git_dirty=false config_sha256=aa firmware_sha256=bb",
                "2026-08-09T00:00:00+08:00 CAPTURE_END reason=duration_complete",
            ]
        )
        self.assertTrue(metadata["capture_complete"])
        self.assertEqual(metadata["capture_elapsed_seconds"], 86400)

    def test_capture_metadata_preserves_transport_watchdog_events(self) -> None:
        metadata = parse_capture_metadata(
            [
                "2026-08-08T00:00:00+08:00 CAPTURE_START device=x duration_seconds=86400 git_commit=abc git_dirty=false config_sha256=aa firmware_sha256=bb stale_output_seconds=120 heartbeat_seconds=30",
                "2026-08-08T00:00:00+08:00 CONNECT_ATTEMPT device=x",
                "2026-08-08T00:00:30+08:00 CAPTURE_HEARTBEAT child_pid=123 child_alive=true seconds_since_output=0.500",
                "2026-08-08T00:02:00+08:00 STALE_OUTPUT child_pid=123 seconds_since_output=120.001 action=reconnect",
                "2026-08-08T00:02:00+08:00 DISCONNECTED reason=stale_output returncode=-15 retry_seconds=3.0",
            ]
        )
        self.assertEqual(metadata["transport_connect_attempt_count"], 1)
        self.assertEqual(metadata["transport_disconnect_count"], 1)
        self.assertEqual(metadata["transport_stale_output_count"], 1)
        self.assertEqual(metadata["capture_heartbeat_count"], 1)
        self.assertEqual(metadata["capture_stale_output_limit_seconds"], 120)
        self.assertEqual(
            metadata["capture_transport_events"][-1]["reason"], "stale_output"
        )

    def test_capture_stale_output_watchdog_boundary(self) -> None:
        self.assertFalse(stale_output_due(10.0, 129.999, 120.0))
        self.assertTrue(stale_output_due(10.0, 130.0, 120.0))

    def test_strict_requires_every_catalog_address_to_be_read(self) -> None:
        with TemporaryDirectory() as directory:
            result, summary = self._run_strict_fixture(
                Path(directory), (0, 1)
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["missing_read_register_addresses"], [1])

    def test_strict_rejects_fake_24h_metadata_without_uart_activity(self) -> None:
        with TemporaryDirectory() as directory:
            result, summary = self._run_strict_fixture(
                Path(directory),
                (0,),
                end_timestamp="2026-08-09T00:00:00+08:00",
                duration_seconds=86400,
                max_idle_seconds=180,
            )
        self.assertEqual(result.returncode, 2)
        self.assertGreater(summary["max_uart_idle_seconds"], 86000)

    def test_strict_accepts_complete_short_fixture_when_thresholds_are_lowered(self) -> None:
        with TemporaryDirectory() as directory:
            result, summary = self._run_strict_fixture(Path(directory), (0,))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["read_observed_register_addresses"], [0])
        self.assertEqual(summary["observed_modbus_slave_addresses"], [1])
        self.assertEqual(summary["observed_modbus_slave_address_count"], 1)

    def test_strict_accepts_all_178_addresses_when_all_are_confirmed(self) -> None:
        def with_crc(pdu: bytes) -> bytes:
            crc = modbus_crc(pdu)
            return pdu + bytes((crc & 0xFF, crc >> 8))

        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            mapping = directory / "mapping.csv"
            mapping.write_text(
                "register,register_label,status,mapped_fields,evidence\n"
                + "".join(
                    f"{address},D{address},confirmed_semantic,field_{address},test\n"
                    for address in OBSERVED_REGISTERS
                ),
                encoding="utf-8",
            )
            lines = [
                "2026-08-08T00:00:00+08:00 CAPTURE_START device=x "
                "duration_seconds=1 git_commit=abc git_dirty=false "
                "config_sha256=aa firmware_sha256=bb"
            ]
            sequence = 0
            elapsed_tenths = 0
            for start, count in ((0, 61), (100, 117)):
                request = with_crc(
                    bytes((1, 3, start >> 8, start & 0xFF, count >> 8, count & 0xFF))
                )
                response = with_crc(bytes((1, 3, count * 2)) + bytes(count * 2))
                for source, payload in ((1, request), (2, response)):
                    for offset in range(0, len(payload), 96):
                        sequence += 1
                        elapsed_tenths += 1
                        chunk = payload[offset : offset + 96]
                        lines.append(
                            "2026-08-08T00:00:00."
                            f"{elapsed_tenths:03d}+08:00 boot=7 #{sequence} "
                            f"G{source}/GPIO{source} {len(chunk)}B "
                            f"RAW_HEX={chunk.hex('.').upper()}"
                        )
            lines.append(
                "2026-08-08T00:00:01+08:00 CAPTURE_END reason=duration_complete"
            )
            capture = directory / "capture.log"
            capture.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tool = (
                Path(__file__).resolve().parent.parent
                / "tools"
                / "analyze_uart_capture.py"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(tool),
                    "--strict",
                    "--mapping-csv",
                    str(mapping),
                    "--min-duration-seconds",
                    "1",
                    "--max-idle-seconds",
                    "10",
                    str(capture),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            summary = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["read_observed_register_address_count"], 178)
        self.assertEqual(summary["missing_read_register_addresses"], [])
        self.assertEqual(summary["observed_modbus_slave_addresses"], [1])

    def test_frames_sort_by_completion_not_first_chunk(self) -> None:
        response = Frame(
            source=2,
            first_sequence=10,
            last_sequence=13,
            data=b"\x01\x03\x02\x00\x01\x79\x84",
        )
        request = Frame(
            source=1,
            first_sequence=11,
            last_sequence=11,
            data=b"\x01\x03\x00\x00\x00\x01\x84\x0a",
        )
        ordered = sorted([response, request], key=frame_completion_order)
        self.assertEqual(ordered, [request, response])

    def test_sequence_integrity_separates_missing_from_log_reordering(self) -> None:
        complete_but_reordered = [
            Chunk(1, 1, b"a", boot_id=7, order=0),
            Chunk(3, 2, b"b", boot_id=7, order=1),
            Chunk(2, 1, b"c", boot_id=7, order=2),
            Chunk(4, 2, b"d", boot_id=7, order=3),
        ]
        gaps, out_of_order = sequence_integrity(complete_but_reordered)
        self.assertEqual(gaps, [])
        self.assertEqual(len(out_of_order), 1)
        self.assertEqual(out_of_order[0]["after"], 3)
        self.assertEqual(out_of_order[0]["before"], 2)

        missing_and_reordered = [
            Chunk(1, 1, b"a", boot_id=9, order=0),
            Chunk(4, 2, b"b", boot_id=9, order=1),
            Chunk(2, 1, b"c", boot_id=9, order=2),
            Chunk(5, 2, b"d", boot_id=9, order=3),
        ]
        gaps, out_of_order = sequence_integrity(missing_and_reordered)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["expected"], 3)
        self.assertEqual(gaps[0]["missing_count"], 1)
        self.assertEqual(len(out_of_order), 1)

    def test_sequence_integrity_retains_every_gap_with_context(self) -> None:
        chunks = [
            Chunk(
                sequence,
                1 if sequence % 4 == 1 else 2,
                b"x",
                boot_id=7,
                order=order,
                timestamp_ms=1786118400000 + order * 1000,
            )
            for order, sequence in enumerate(range(1, 52, 2))
        ]
        gaps, _ = sequence_integrity(chunks)
        self.assertEqual(len(gaps), 25)
        self.assertEqual(gaps[-1]["expected"], 50)
        self.assertEqual(gaps[-1]["before"], 51)
        self.assertEqual(gaps[-1]["after_timestamp_ms"], 1786118424000)
        self.assertEqual(gaps[-1]["before_timestamp_ms"], 1786118425000)

    def test_strict_accepts_complete_capture_with_only_log_reordering(self) -> None:
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            mapping = directory / "mapping.csv"
            mapping.write_text(
                "register,register_label,status,mapped_fields,evidence\n"
                "0,D0,confirmed_semantic,field_0,test\n",
                encoding="utf-8",
            )
            capture = directory / "capture.log"
            capture.write_text(
                "2026-08-08T00:00:00+08:00 CAPTURE_START device=x "
                "duration_seconds=1 git_commit=abc git_dirty=false "
                "config_sha256=aa firmware_sha256=bb\n"
                "2026-08-08T00:00:00.100+08:00 boot=7 #1 G1/GPIO1 8B "
                "RAW_HEX=01.03.00.00.00.01.84.0A\n"
                "2026-08-08T00:00:00.150+08:00 boot=7 #3 G1/GPIO1 8B "
                "RAW_HEX=01.03.00.00.00.01.84.0A\n"
                "2026-08-08T00:00:00.200+08:00 boot=7 #2 G2/GPIO2 7B "
                "RAW_HEX=01.03.02.00.01.79.84\n"
                "2026-08-08T00:00:00.300+08:00 boot=7 #4 G2/GPIO2 7B "
                "RAW_HEX=01.03.02.00.01.79.84\n"
                "2026-08-08T00:00:01+08:00 CAPTURE_END "
                "reason=duration_complete\n",
                encoding="utf-8",
            )
            tool = (
                Path(__file__).resolve().parent.parent
                / "tools"
                / "analyze_uart_capture.py"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(tool),
                    "--strict",
                    "--mapping-csv",
                    str(mapping),
                    "--min-duration-seconds",
                    "1",
                    "--max-idle-seconds",
                    "10",
                    str(capture),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            summary = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["sequence_gap_count"], 0)
        self.assertEqual(summary["missing_sequence_count"], 0)
        self.assertEqual(summary["sequence_out_of_order_count"], 1)
        self.assertEqual(summary["pending_requests"], 0)
        self.assertEqual(summary["unpaired_responses"], 0)

    def test_clean_room_generator_covers_every_observed_d_register(self) -> None:
        rendered = generate_entities()
        # Packed words and multi-word values expose every decoded subfield;
        # the stable address catalog therefore produces more than 178 entities.
        self.assertEqual(rendered.count("  - platform: template"), 196)
        for address in (1, 2, 11, 12, 24, 30, 31, 216):
            self.assertIn(f'name: "D{address} ', rendered)
        self.assertEqual(len(OBSERVED_REGISTERS), 178)
        interval_counts = {
            interval: rendered.count(f"    update_interval: {interval}s")
            for interval in UPDATE_INTERVAL_SECONDS
        }
        self.assertEqual(sum(interval_counts.values()), 196)
        self.assertLessEqual(max(interval_counts.values()), 27)
        self.assertIn('name: "D110 load_power_factor"', rendered)
        self.assertIn('name: "D110 inverter_power_factor"', rendered)
        self.assertIn('name: "D132-D133 total_generated_energy_kwh"', rendered)
        self.assertIn('name: "D32 force_charge_interval_days"', rendered)
        self.assertIn('name: "D34 force_discharge_interval_days"', rendered)
        self.assertIn(
            'name: "D36 force_charge_schedule_enabled_bit0"', rendered
        )
        self.assertIn(
            "passive_modbus::monitor.raw_u16(36) & 0x0001U", rendered
        )
        self.assertIn('name: "D145 firmware_version"', rendered)

    def test_generated_mapping_csv_matches_entities(self) -> None:
        with TemporaryDirectory() as directory:
            mapping_path = Path(directory) / "mapping.csv"
            write_mapping(mapping_path)
            mapping = load_register_mapping(mapping_path)
        self.assertEqual(set(mapping), set(OBSERVED_REGISTERS))
        self.assertEqual(mapping[24], ["observed_raw_u16"])
        self.assertEqual(mapping[12], ["inverter_max_grid_current_a"])
        self.assertEqual(
            mapping[110], ["load_power_factor", "inverter_power_factor"]
        )
        self.assertEqual(
            mapping[145], ["firmware_version_packed", "firmware_version"]
        )
        self.assertEqual(
            mapping[132],
            ["total_generated_energy_high_word", "total_generated_energy_kwh"],
        )
        self.assertIn("force_charge_start_hour", mapping[32])
        self.assertIn("force_charge_start_time", mapping[32])
        self.assertEqual(
            mapping[36],
            [
                "force_charge_discharge_status",
                "force_charge_schedule_enabled_bit0",
            ],
        )
        self.assertEqual(
            mapping[30], ["observed_network_time_high_word"]
        )

    def test_ha_package_persists_before_ack(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        package = yaml.safe_load(
            (repo / "home-assistant" / "makeskyblue_uart_capture.yaml").read_text(
                encoding="utf-8"
            )
        )
        actions = package["automation"][0]["actions"]
        persist_actions = actions[0]["then"]
        self.assertEqual(persist_actions[0]["action"], "notify.send_message")
        self.assertEqual(persist_actions[1]["action"], "input_text.set_value")
        self.assertEqual(
            actions[1]["action"],
            "esphome.makeskybluemodbusmonitor_ack_uart_capture",
        )


if __name__ == "__main__":
    unittest.main()
