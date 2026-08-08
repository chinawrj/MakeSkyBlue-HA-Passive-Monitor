#!/usr/bin/env python3
"""Generate clean-room HA entities for empirically observed D-registers.

The generator embeds independently written protocol facts corroborated from
local UART captures and public UI/manual labels. It reads no third-party
protocol source file and distributes no third-party code or data table.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path


OBSERVED_REGISTERS = tuple(range(0, 61)) + tuple(range(100, 217))
# Publishing all generated entities on the same tick can exceed ESPHome's
# bounded API send queue. Eight coprime-ish cadences keep each normal burst at
# 27 entities or fewer while refreshing every diagnostic within 97 s.
UPDATE_INTERVAL_SECONDS = (61, 67, 71, 73, 79, 83, 89, 97)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    status: str
    evidence: str
    unit: str | None = None
    decimals: int = 0
    expression: str | None = None
    presence_expression: str | None = None
    display_prefix: str | None = None


LIVE_DATA = (
    "timestamped local UART/IoTRix 2.2.31 live-data cross-checks including "
    "a sanitized seven-snapshot 90-second window on 2026-08-08"
)
LIVE_SETTINGS = (
    "local UART plus authenticated read-only IoTRix 2.2.31 settings snapshot "
    "on 2026-08-08"
)
PROVISIONAL_SETTINGS = LIVE_SETTINGS + "; address meaning not yet changed under control"
RAW_EVIDENCE = {
    18: (
        "local FC03 response capture; user-provided register preview suggests "
        "communication_address, but D18 remained raw 0 while every observed "
        "CRC-valid RTU frame used slave address 1; encoding is unresolved"
    ),
    131: (
        "local FC03 response capture; D131 remained raw 84 throughout a "
        "sanitized seven-snapshot 90-second IoTRix comparison, but did not "
        "match the displayed daily energy 18.6, SOC 81, or another exposed "
        "cloud field under the tested integer/scaled transforms"
    ),
}


def field(
    name: str,
    status: str,
    evidence: str,
    unit: str | None = None,
    decimals: int = 0,
    expression: str | None = None,
    presence_expression: str | None = None,
    display_prefix: str | None = None,
) -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(
            name,
            status,
            evidence,
            unit,
            decimals,
            expression,
            presence_expression,
            display_prefix,
        ),
    )


FIELD_SPECS: dict[int, tuple[FieldSpec, ...]] = {
    0: field("work_mode", "confirmed_semantic", LIVE_SETTINGS),
    1: field("grid_voltage_range", "confirmed_semantic", LIVE_SETTINGS),
    2: field("charging_priority", "confirmed_semantic", LIVE_SETTINGS),
    3: field("ac_frequency_setting", "confirmed_semantic", LIVE_SETTINGS),
    4: field("power_saving_mode", "confirmed_semantic", LIVE_SETTINGS),
    5: field(
        "inverter_ac_voltage_level", "confirmed_semantic", LIVE_SETTINGS
    ),
    6: field("battery_type", "confirmed_semantic", LIVE_SETTINGS),
    7: field(
        "battery_rated_voltage_v",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "V",
        1,
        "passive_modbus::monitor.raw_u16(7) / 10.0f",
    ),
    8: field(
        "battery_discharge_min_voltage_v",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "V",
        1,
        "passive_modbus::monitor.raw_u16(8) / 10.0f",
    ),
    9: field(
        "battery_discharge_start_voltage_v",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "V",
        1,
        "passive_modbus::monitor.raw_u16(9) / 10.0f",
    ),
    10: field(
        "grid_to_battery_max_charge_current_a",
        "confirmed_semantic",
        LIVE_SETTINGS
        + "; FC06 requests raw 620 and 700 both read back raw 600; the raw 700 request followed an operator setting IoTRix/panel d13 to 70, independently confirming lowercase d13 maps to Modbus D10 rather than Modbus D13",
        "A",
        1,
        "passive_modbus::monitor.raw_u16(10) / 10.0f",
    ),
    11: field(
        "pv_to_battery_max_charge_current_a",
        "confirmed_semantic",
        LIVE_SETTINGS + "; FC06 changed raw 800 to 820 and FC03 read back 820",
        "A",
        1,
        "passive_modbus::monitor.raw_u16(11) / 10.0f",
    ),
    12: field(
        "inverter_max_grid_current_a",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "A",
        1,
        "passive_modbus::monitor.raw_u16(12) / 10.0f",
    ),
    13: field(
        "inverter_max_output_power_kw",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "kW",
        1,
        "passive_modbus::monitor.raw_u16(13) / 10.0f",
    ),
    14: field("charging_soc_pct", "confirmed_semantic", LIVE_SETTINGS, "%"),
    15: field("discharging_soc_pct", "confirmed_semantic", LIVE_SETTINGS, "%"),
    16: field("anti_reverse_mode", "confirmed_semantic", LIVE_SETTINGS),
    17: field("system_power_state", "confirmed_semantic", LIVE_SETTINGS),
    19: field("reset_energy_counter", "provisional_semantic", PROVISIONAL_SETTINGS),
    20: field(
        "force_charge_discharge_control",
        "provisional_semantic",
        PROVISIONAL_SETTINGS,
    ),
    21: field("remote_anti_reverse", "provisional_semantic", PROVISIONAL_SETTINGS),
    22: field(
        "anti_reverse_power_w", "confirmed_semantic", LIVE_SETTINGS, "W"
    ),
    23: field(
        "battery_cutoff_voltage_v",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "V",
        1,
        "passive_modbus::monitor.raw_u16(23) / 10.0f",
    ),
    30: field(
        "observed_network_time_high_word",
        "confirmed_semantic",
        "local FC10 captures plus decoded wall-clock agreement",
    ),
    31: field(
        "observed_network_time_low_word",
        "confirmed_semantic",
        "local FC10 captures plus decoded wall-clock agreement",
    ),
    32: (
        FieldSpec("force_charge_start_packed", "confirmed_semantic", LIVE_SETTINGS),
        FieldSpec(
            "force_charge_start_hour",
            "confirmed_semantic",
            LIVE_SETTINGS,
            expression="(passive_modbus::monitor.raw_u16(32) >> 11U) & 0x1FU",
        ),
        FieldSpec(
            "force_charge_start_minute",
            "confirmed_semantic",
            LIVE_SETTINGS,
            expression="(passive_modbus::monitor.raw_u16(32) >> 5U) & 0x3FU",
        ),
        FieldSpec(
            "force_charge_interval_days",
            "confirmed_semantic",
            LIVE_SETTINGS,
            "d",
            0,
            "passive_modbus::monitor.raw_u16(32) & 0x1FU",
        ),
    ),
    33: (
        FieldSpec("force_charge_end_packed", "confirmed_semantic", LIVE_SETTINGS),
        FieldSpec(
            "force_charge_end_hour",
            "confirmed_semantic",
            LIVE_SETTINGS,
            expression="(passive_modbus::monitor.raw_u16(33) >> 11U) & 0x1FU",
        ),
        FieldSpec(
            "force_charge_end_minute",
            "confirmed_semantic",
            LIVE_SETTINGS,
            expression="(passive_modbus::monitor.raw_u16(33) >> 5U) & 0x3FU",
        ),
    ),
    34: (
        FieldSpec(
            "force_discharge_start_packed", "confirmed_semantic", LIVE_SETTINGS
        ),
        FieldSpec(
            "force_discharge_start_hour",
            "confirmed_semantic",
            LIVE_SETTINGS,
            expression="(passive_modbus::monitor.raw_u16(34) >> 11U) & 0x1FU",
        ),
        FieldSpec(
            "force_discharge_start_minute",
            "confirmed_semantic",
            LIVE_SETTINGS,
            expression="(passive_modbus::monitor.raw_u16(34) >> 5U) & 0x3FU",
        ),
        FieldSpec(
            "force_discharge_interval_days",
            "confirmed_semantic",
            LIVE_SETTINGS,
            "d",
            0,
            "passive_modbus::monitor.raw_u16(34) & 0x1FU",
        ),
    ),
    35: (
        FieldSpec("force_discharge_end_packed", "confirmed_semantic", LIVE_SETTINGS),
        FieldSpec(
            "force_discharge_end_hour",
            "confirmed_semantic",
            LIVE_SETTINGS,
            expression="(passive_modbus::monitor.raw_u16(35) >> 11U) & 0x1FU",
        ),
        FieldSpec(
            "force_discharge_end_minute",
            "confirmed_semantic",
            LIVE_SETTINGS,
            expression="(passive_modbus::monitor.raw_u16(35) >> 5U) & 0x3FU",
        ),
    ),
    36: field(
        "force_charge_discharge_status", "confirmed_semantic", LIVE_SETTINGS
    ),
    100: field("system_type_code", "confirmed_semantic", LIVE_DATA),
    101: field("inverter_status_code", "confirmed_semantic", LIVE_DATA),
    102: field("mppt_status_code", "confirmed_semantic", LIVE_DATA),
    103: field("busbar_voltage_v", "confirmed_semantic", LIVE_SETTINGS, "V"),
    104: field(
        "grid_frequency_hz",
        "confirmed_semantic",
        LIVE_DATA,
        "Hz",
        1,
        "passive_modbus::monitor.raw_u16(104) / 10.0f",
    ),
    105: field("grid_voltage_v", "confirmed_semantic", LIVE_DATA, "V"),
    106: field("inverter_output_voltage_v", "confirmed_semantic", LIVE_DATA, "V"),
    107: field(
        "inverter_off_grid_load_current_a",
        "confirmed_semantic",
        LIVE_DATA,
        "A",
        1,
        "passive_modbus::monitor.raw_u16(107) / 10.0f",
    ),
    108: field(
        "inverter_grid_load_current_a",
        "confirmed_semantic",
        LIVE_DATA,
        "A",
        1,
        "(static_cast<int32_t>(passive_modbus::monitor.raw_u16(108)) - 1000) / 10.0f",
    ),
    109: field(
        "inverter_current_a",
        "confirmed_semantic",
        LIVE_DATA,
        "A",
        1,
        "(static_cast<int32_t>(passive_modbus::monitor.raw_u16(109)) - 1000) / 10.0f",
    ),
    110: (
        FieldSpec(
            "load_power_factor",
            "confirmed_semantic",
            LIVE_DATA + "; IoTRix UI low-byte split",
            None,
            2,
            "(passive_modbus::monitor.raw_u16(110) & 0xFFU) / 100.0f",
        ),
        FieldSpec(
            "inverter_power_factor",
            "confirmed_semantic",
            LIVE_DATA + "; IoTRix UI high-byte split",
            None,
            2,
            "((passive_modbus::monitor.raw_u16(110) >> 8U) & 0xFFU) / 100.0f",
        ),
    ),
    111: field(
        "grid_power_w",
        "confirmed_semantic",
        LIVE_DATA,
        "W",
        0,
        "static_cast<int32_t>(passive_modbus::monitor.raw_u16(111)) - 30000",
    ),
    112: field(
        "load_power_w",
        "confirmed_semantic",
        LIVE_DATA,
        "W",
        0,
        "static_cast<int32_t>(passive_modbus::monitor.raw_u16(112)) - 30000",
    ),
    113: field(
        "inverter_real_power_w",
        "confirmed_semantic",
        LIVE_DATA,
        "W",
        0,
        "static_cast<int32_t>(passive_modbus::monitor.raw_u16(113)) - 30000",
    ),
    114: field(
        "battery_voltage_v",
        "confirmed_semantic",
        LIVE_DATA,
        "V",
        1,
        "passive_modbus::monitor.raw_u16(114) / 10.0f",
    ),
    115: field(
        "battery_charging_current_a",
        "confirmed_semantic",
        LIVE_DATA,
        "A",
        1,
        "passive_modbus::monitor.raw_s16(115) / 10.0f",
    ),
    122: field(
        "pv1_voltage_v",
        "confirmed_semantic",
        LIVE_DATA,
        "V",
        1,
        "passive_modbus::monitor.raw_u16(122) / 10.0f",
    ),
    123: field(
        "pv2_voltage_v",
        "confirmed_semantic",
        LIVE_DATA,
        "V",
        1,
        "passive_modbus::monitor.raw_u16(123) / 10.0f",
    ),
    124: field(
        "pv3_voltage_v",
        "provisional_semantic",
        "three-channel register layout; IoTRix UI does not expose PV3",
        "V",
        1,
        "passive_modbus::monitor.raw_u16(124) / 10.0f",
    ),
    125: field(
        "pv1_current_a",
        "confirmed_semantic",
        LIVE_DATA,
        "A",
        1,
        "passive_modbus::monitor.raw_u16(125) / 10.0f",
    ),
    126: field(
        "pv2_current_a",
        "confirmed_semantic",
        LIVE_DATA,
        "A",
        1,
        "passive_modbus::monitor.raw_u16(126) / 10.0f",
    ),
    127: field(
        "pv3_current_a",
        "provisional_semantic",
        "three-channel register layout; IoTRix UI does not expose PV3",
        "A",
        1,
        "passive_modbus::monitor.raw_u16(127) / 10.0f",
    ),
    128: field("pv1_power_w", "confirmed_semantic", LIVE_DATA, "W"),
    129: field("pv2_power_w", "confirmed_semantic", LIVE_DATA, "W"),
    130: field(
        "pv3_power_w",
        "provisional_semantic",
        "three-channel register layout; IoTRix UI does not expose PV3",
        "W",
    ),
    132: (
        FieldSpec(
            "total_generated_energy_high_word", "confirmed_semantic", LIVE_DATA
        ),
        FieldSpec(
            "total_generated_energy_kwh",
            "confirmed_semantic",
            LIVE_DATA + "; coherent D132-D133 UINT32",
            "kWh",
            1,
            "passive_modbus::monitor.raw_u32(132) / 10.0f",
            "passive_modbus::monitor.has_u32(132)",
            "D132-D133",
        ),
    ),
    133: field(
        "total_generated_energy_low_word",
        "confirmed_semantic",
        LIVE_DATA,
    ),
    134: field("system_error_code", "confirmed_semantic", LIVE_DATA),
    135: field(
        "dc_temperature_c",
        "provisional_semantic",
        LIVE_DATA + "; equal-temperature samples do not disambiguate D135/D137/D138",
        "°C",
        1,
        "(static_cast<int32_t>(passive_modbus::monitor.raw_u16(135)) - 400) / 10.0f",
    ),
    136: field(
        "inverter_temperature_c",
        "confirmed_semantic",
        LIVE_DATA,
        "°C",
        1,
        "(static_cast<int32_t>(passive_modbus::monitor.raw_u16(136)) - 400) / 10.0f",
    ),
    137: field(
        "mppt1_temperature_c",
        "provisional_semantic",
        LIVE_DATA + "; equal-temperature samples do not disambiguate D135/D137/D138",
        "°C",
        1,
        "(static_cast<int32_t>(passive_modbus::monitor.raw_u16(137)) - 400) / 10.0f",
    ),
    138: field(
        "mppt2_temperature_c",
        "provisional_semantic",
        LIVE_DATA + "; equal-temperature samples do not disambiguate D135/D137/D138",
        "°C",
        1,
        "(static_cast<int32_t>(passive_modbus::monitor.raw_u16(138)) - 400) / 10.0f",
    ),
    139: field(
        "mppt3_temperature_c",
        "provisional_semantic",
        "three-channel register layout; IoTRix UI does not expose MPPT3",
        "°C",
        1,
        "(static_cast<int32_t>(passive_modbus::monitor.raw_u16(139)) - 400) / 10.0f",
    ),
    140: field("relay_status_code", "confirmed_semantic", LIVE_SETTINGS),
    141: field("solar_status_code", "confirmed_semantic", LIVE_DATA),
    142: field("battery_status_code", "confirmed_semantic", LIVE_DATA),
    143: field("grid_status_code", "confirmed_semantic", LIVE_DATA),
    144: field("load_status_code", "confirmed_semantic", LIVE_DATA),
    145: field("firmware_version_packed", "confirmed_semantic", LIVE_DATA),
    201: field(
        "charging_limit_voltage_v",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "V",
        2,
        "passive_modbus::monitor.raw_u16(201) / 100.0f",
    ),
    202: field(
        "charging_limit_current_a",
        "confirmed_semantic",
        LIVE_DATA,
        "A",
        2,
        "passive_modbus::monitor.raw_u16(202) / 100.0f",
    ),
    203: field(
        "discharging_limit_voltage_v",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "V",
        2,
        "passive_modbus::monitor.raw_u16(203) / 100.0f",
    ),
    204: field(
        "discharging_limit_current_a",
        "confirmed_semantic",
        LIVE_SETTINGS,
        "A",
        2,
        "passive_modbus::monitor.raw_u16(204) / 100.0f",
    ),
    205: field(
        "battery_sum_voltage_v",
        "confirmed_semantic",
        LIVE_DATA,
        "V",
        2,
        "passive_modbus::monitor.raw_u16(205) / 100.0f",
    ),
    206: field(
        "battery_sum_current_a",
        "confirmed_semantic",
        LIVE_DATA,
        "A",
        2,
        "passive_modbus::monitor.raw_s16(206) / 100.0f",
    ),
    207: field("battery_max_temperature_c", "provisional_semantic", PROVISIONAL_SETTINGS, "°C"),
    208: field("battery_soc_pct", "confirmed_semantic", LIVE_DATA, "%"),
    209: field("battery_soh_pct", "confirmed_semantic", LIVE_SETTINGS, "%"),
    210: field("battery_remaining_capacity", "provisional_semantic", PROVISIONAL_SETTINGS),
    211: field("battery_full_capacity", "provisional_semantic", PROVISIONAL_SETTINGS),
    212: field("battery_rated_capacity", "provisional_semantic", PROVISIONAL_SETTINGS),
    213: field("battery_cycle_count", "provisional_semantic", PROVISIONAL_SETTINGS),
    214: field("warning_status_flags", "provisional_semantic", LIVE_DATA),
    215: field("protection_status_flags", "provisional_semantic", LIVE_DATA),
    216: field("error_status_flags", "provisional_semantic", LIVE_DATA),
}


def entity_name(address: int) -> str:
    if address in FIELD_SPECS:
        return FIELD_SPECS[address][0].name
    return "observed_raw_u16"


def generate_entities() -> str:
    lines = [
        "# Generated by tools/generate_makeskyblue_passive.py.",
        "# Evidence source: this project's own passive UART captures.",
        "# Safety invariant: no UART TX or Modbus write component exists here.",
        "",
        "sensor:",
    ]
    for index, address in enumerate(OBSERVED_REGISTERS):
        specs = FIELD_SPECS.get(
            address,
            (
                FieldSpec(
                    "observed_raw_u16",
                    "observed_raw",
                    RAW_EVIDENCE.get(address, "local FC03 response capture"),
                ),
            ),
        )
        update_interval = UPDATE_INTERVAL_SECONDS[index % len(UPDATE_INTERVAL_SECONDS)]
        for spec in specs:
            expression = spec.expression or (
                f"static_cast<float>(passive_modbus::monitor.raw_u16({address}))"
            )
            presence = spec.presence_expression or (
                f"passive_modbus::monitor.has_register({address})"
            )
            display_prefix = spec.display_prefix or f"D{address}"
            lines.extend(
                [
                    "  - platform: template",
                    f"    id: passive_msb_d{address}_{spec.name}",
                    f'    name: "{display_prefix} {spec.name}"',
                    f"    update_interval: {update_interval}s",
                    "    entity_category: diagnostic",
                    *(
                        [f'    unit_of_measurement: "{spec.unit}"']
                        if spec.unit
                        else []
                    ),
                    f"    accuracy_decimals: {spec.decimals}",
                    "    lambda: |-",
                    f"      if (!{presence}) return {{}};",
                    f"      return static_cast<float>({expression});",
                    "",
                ]
            )
    lines.extend(["", "text_sensor:"])
    for address, name, update_interval in (
        (32, "force_charge_start_time", 61),
        (33, "force_charge_end_time", 67),
        (34, "force_discharge_start_time", 71),
        (35, "force_discharge_end_time", 73),
    ):
        lines.extend(
            [
                "  - platform: template",
                f"    id: passive_msb_d{address}_{name}",
                f'    name: "D{address} {name}"',
                f"    update_interval: {update_interval}s",
                "    entity_category: diagnostic",
                "    lambda: |-",
                f"      if (!passive_modbus::monitor.has_register({address})) return {{}};",
                f"      const uint16_t raw = passive_modbus::monitor.raw_u16({address});",
                '      return str_sprintf("%02u:%02u", (raw >> 11U) & 0x1FU,',
                "                         (raw >> 5U) & 0x3FU);",
                "",
            ]
        )
    lines.extend(
        [
            "  - platform: template",
            "    id: passive_msb_d145_firmware_version",
            '    name: "D145 firmware_version"',
            "    update_interval: 71s",
            "    entity_category: diagnostic",
            "    lambda: |-",
            "      if (!passive_modbus::monitor.has_register(145)) return {};",
            "      const uint16_t raw = passive_modbus::monitor.raw_u16(145);",
            '      return str_sprintf("%u.%u.%u", raw >> 10U,',
            "                         (raw >> 6U) & 0x0FU, raw & 0x3FU);",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_mapping(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=(
                "register",
                "register_label",
                "status",
                "mapped_fields",
                "evidence",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        for address in OBSERVED_REGISTERS:
            fields = FIELD_SPECS.get(address)
            mapped_names = [field.name for field in fields] if fields else []
            mapped_names.extend(
                {
                    32: ["force_charge_start_time"],
                    33: ["force_charge_end_time"],
                    34: ["force_discharge_start_time"],
                    35: ["force_discharge_end_time"],
                }.get(address, [])
            )
            if address == 145:
                mapped_names.append("firmware_version")
            writer.writerow(
                {
                    "register": address,
                    "register_label": f"D{address}",
                    "status": fields[0].status if fields else "observed_raw",
                    "mapped_fields": (
                        "|".join(mapped_names)
                        if mapped_names
                        else "observed_raw_u16"
                    ),
                    "evidence": (
                        "; ".join(dict.fromkeys(field.evidence for field in fields))
                        if fields
                        else RAW_EVIDENCE.get(
                            address, "local FC03 response capture"
                        )
                    ),
                }
            )


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "packages" / "makeskyblue-passive-entities.yaml",
    )
    parser.add_argument(
        "--mapping-output",
        type=Path,
        default=repo_root / "registers" / "makeskyblue-observed-registers.csv",
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(generate_entities(), encoding="utf-8")
    write_mapping(args.mapping_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
