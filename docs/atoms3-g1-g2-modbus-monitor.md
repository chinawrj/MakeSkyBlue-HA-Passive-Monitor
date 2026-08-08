# AtomS3 G1/G2 Modbus UART monitor

The matching ESPHome firmware is
[`makeskyblue-modbus-monitor.yaml`](../makeskyblue-modbus-monitor.yaml).
It is a passive two-channel TTL UART monitor: GPIO1 and GPIO2 are configured as
receive-only inputs and the firmware never transmits on either pin.

The installed unit does not require USB. Its primary and optional backup Wi-Fi
networks are loaded from ignored local files created from the repository's
sanitized examples. Physical USB serial logging is disabled; logs, UART
events, entities, and OTA remain available over Wi-Fi.

## Wiring

The AtomS3 HY2.0-4P connector is, in connector color order:

| Wire | AtomS3 signal | Monitor use |
| --- | --- | --- |
| Black | GND | Connect to the monitored device GND |
| Red | 5 V | Do not connect to a UART signal |
| Yellow | G2 / GPIO2 | Connect to the second UART signal |
| White | G1 / GPIO1 | Connect to the first UART signal |

A typical full-duplex TTL UART hookup is monitored-device TX to G1 and
monitored-device RX to G2. The names are intentionally neutral because the
actual direction depends on which device on the bus is used as the reference.

Only connect 3.3 V TTL UART signals directly. Do not connect RS485 A/B, RS232,
5 V TTL, or an inverter power rail directly to GPIO1/GPIO2. RS485 requires an
RS485 receiver/transceiver; RS232 and 5 V TTL require the appropriate receiver
or level shifting. Always share signal ground when monitoring TTL UART.

## Serial profile

The firmware defaults to `9600-8N1`, matching the MakeSkyBlue protocol work in
this repository. UART capture cannot auto-detect the baud rate without losing
data. If the monitored link differs, update the substitutions at the top of the
ESPHome file before flashing.

At 9600 baud, 8 ms of idle is treated as a frame boundary. Captures longer than
96 bytes are split into ordered chunks. No bytes are intentionally discarded;
use the global `sequence` field to preserve the arrival order across G1 and G2.

## Startup capture and RAM ring buffer

The two UART drivers initialize at ESPHome setup priority 1000. Their receive
FIFO threshold is one byte and each driver has a 2048-byte RX buffer. At setup
priority 900, before Wi-Fi initialization, firmware adds an onboard `BOOT`
record to a fixed 256-record RAM ring. It is always `sequence=1`; G1/G2 chunks
continue with a boot-local unsigned 32-bit sequence.

UART capture never waits for Wi-Fi or HA. Records accumulate in RAM, and only
start draining 2 seconds after a Home Assistant API client connects, allowing
HA time to subscribe to ESPHome actions. A CLI log client does not drain the
ring. The drain interval is 20 ms, preserving the global sequence order.

When the ring is full, a new record overwrites the oldest record. Every record
contains `overwritten_total`, the cumulative number of overwritten records at
the time it was stored. Therefore overflow remains detectable even when the
original BOOT marker has itself been overwritten. HA also exposes `UART Ring
Buffered Records` and `UART Ring Overwritten Records` diagnostic sensors.

The ring is volatile. A reset or power loss clears it and begins a new boot at
`BOOT sequence=1`.

## Flash and inspect

Validate and compile:

```sh
.venv/bin/esphome config makeskyblue-modbus-monitor.yaml
.venv/bin/esphome compile makeskyblue-modbus-monitor.yaml
```

Initial USB flash:

```sh
.venv/bin/esphome run makeskyblue-modbus-monitor.yaml \
  --device /dev/cu.usbmodem101
```

After Home Assistant adds the ESPHome device, open the ESPHome integration's
device configuration and enable **Allow the device to perform Home Assistant
actions**. Every chunk is then emitted as the event
`esphome.modbus_uart_capture`, containing:

- `device`: `makeskybluemodbusmonitor`
- `boot_id`: random unsigned 32-bit session ID generated at monitor boot
- `source`: `G1` or `G2`
- `gpio`: `GPIO1` or `GPIO2`
- `sequence`: boot-local global arrival sequence
- `uptime_ms`: device uptime at capture
- `byte_count`: number of bytes in this chunk
- `hex`: continuous uppercase hexadecimal bytes
- `record_type`: `BOOT` or `UART`
- `overwritten_total`: cumulative ring overwrite count
- `buffer_remaining`: queued records remaining after this event

Home Assistant's Developer tools event listener can listen for
`esphome.modbus_uart_capture` to verify reception.

The Wi-Fi ESPHome log prints the same bytes in a human-readable raw form:

```text
[I][modbus_raw]: boot=305419896 #42 G1/GPIO1 8B RAW_HEX=01.03.00.64.00.01.C5.D5
[I][modbus_raw]: boot=305419896 #43 G2/GPIO2 7B RAW_HEX=01.03.02.12.34.B5.33
```

Follow the online log without USB using either mDNS or the current IP:

```sh
.venv/bin/esphome logs makeskyblue-modbus-monitor.yaml \
  --device makeskybluemodbusmonitor.local
```

## Append every HA event to a file

1. In `configuration.yaml`, merge this path into the existing `homeassistant:`
   section, then restart Home Assistant:

   ```yaml
   homeassistant:
     allowlist_external_dirs:
       - /config
   ```

2. Create `/config/modbus_uart_capture.csv` with this single header line before
   adding the notifier:

   ```text
   ha_timestamp,boot_id,record_type,sequence,uptime_ms,source,gpio,byte_count,overwritten_total,buffer_remaining,message,hex
   ```

3. Add the **File** integration in **Settings > Devices & services**. Configure
   a notification file such as `/config/modbus_uart_capture.csv`, disable its
   automatic timestamp, and rename its notify entity to
   `notify.modbus_uart_log`.

4. Create an `input_text` helper named `makeskyblue_uart_last_persisted_key`
   with maximum length 32. If managing helpers in YAML, use:

   ```yaml
   input_text:
     makeskyblue_uart_last_persisted_key:
       name: MakeSkyBlue UART last appended key
       max: 32
   ```

   Do not set `initial`; Home Assistant then restores its previous value after
   restart.

5. Add this automation (or paste it into an automation's YAML editor):

   ```yaml
   alias: Record MakeSkyBlue Modbus UART
   id: record_makeskyblue_modbus_uart
   triggers:
     - trigger: event
       event_type: esphome.modbus_uart_capture
       event_data:
         device: makeskybluemodbusmonitor
   variables:
     capture_key: >-
       {{ trigger.event.data.boot_id | string }}:{{ trigger.event.data.sequence | string }}
   actions:
     - if:
         - condition: template
           value_template: >-
             {{ states('input_text.makeskyblue_uart_last_persisted_key') != capture_key }}
       then:
         - action: notify.send_message
           target:
             entity_id: notify.modbus_uart_log
           data:
             message: >-
               {{ trigger.event.time_fired.isoformat() }},{{ trigger.event.data.boot_id }},{{ trigger.event.data.record_type }},{{ trigger.event.data.sequence }},{{ trigger.event.data.uptime_ms }},{{ trigger.event.data.source }},{{ trigger.event.data.gpio }},{{ trigger.event.data.byte_count }},{{ trigger.event.data.overwritten_total }},{{ trigger.event.data.buffer_remaining }},{{ trigger.event.data.message | default('') | replace(',', ';') }},{{ trigger.event.data.hex }}
         - action: input_text.set_value
           target:
             entity_id: input_text.makeskyblue_uart_last_persisted_key
           data:
             value: "{{ capture_key }}"
     - action: esphome.makeskybluemodbusmonitor_ack_uart_capture
       data:
         boot_id: "{{ trigger.event.data.boot_id | string }}"
         sequence: "{{ trigger.event.data.sequence | string }}"
   mode: queued
   max: 1000
   ```

Instead of copying the helper and automation separately, YAML-managed HA
installations may copy
[`home-assistant/makeskyblue_uart_capture.yaml`](../home-assistant/makeskyblue_uart_capture.yaml)
to `/config/packages/` and enable Home Assistant packages. The File integration
entity and CSV header are still prerequisites because integrations configured
through the UI are not created by this package.

The resulting CSV fields are `HA timestamp`, `boot ID`, `record type`, `sequence`,
`device uptime ms`, `source`, `GPIO`, `byte count`, `overwrite total`,
`buffer remaining`, `message`, and `hex payload`. A BOOT/onboarding event is
therefore retained even though it has no UART hex payload. The ACK action must
remain after the File action and helper update. If an ACK is lost, the helper
suppresses a duplicate append and the repeated event is ACKed. This confirms
that the File integration action completed; it is not a transaction or a
power-loss/fsync durability guarantee. A crash between File append and helper
update can still duplicate a row. Back up/restore the CSV and helper together,
and let downstream consumers deduplicate by `(boot_id, sequence)`.

## Capture limits

The two byte counters, ring counters, and last chunk from each pin are exposed
as HA entities. Data arriving without HA is retained only while it remains in
the volatile ring; ring overwrite is visible through `UART Ring Overwritten
Records`.

This design provides at-least-once delivery while the AtomS3 remains powered:
records are removed only after an explicit HA persistence ACK. The AtomS3 has
no nonvolatile capture storage, so a reboot/power loss clears pending RAM, and
a sufficiently long outage can overflow 256 records. UART hardware FIFO
overflow also cannot be recovered. A strict power-loss-safe forensic capture
still requires local nonvolatile storage or a dedicated logic analyzer.
