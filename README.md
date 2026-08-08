# MakeSkyBlue passive UART monitor

ESPHome firmware for an M5Stack AtomS3 that passively observes both sides of
the 3.3 V TTL UART between a MakeSkyBlue Wi-Fi module and its inverter. It
reassembles and validates Modbus RTU frames, pairs requests with responses,
caches observed registers, and publishes decoded values to Home Assistant.

Version documented here: `v0.1.0-alpha.4`. This is the current parsing and HA
sensor candidate. It is not yet the final 24-hour, zero-unknown validation
build.

## Electrical and safety boundary

- G1 is GPIO1 RX; G2 is GPIO2 RX.
- Both pins are input-only, floating: no internal pull-up or pull-down.
- There is no `tx_pin`, UART write call, or Modbus controller in the monitor.
- The monitor observes requests from the Wi-Fi module and responses from the
  inverter, but never writes to either side.
- Connect only 3.3 V TTL plus common ground. Do not connect 5 V TTL, RS232, or
  RS485 A/B directly to GPIO1/GPIO2.

Older active-probe experiments are intentionally excluded from the public
repository and release archive. The published monitor is RX-only.

## What the alpha exposes

- in-order UART chunks with a boot-local unsigned 32-bit sequence and explicit
  gap/overflow diagnostics;
- a BOOT/onboarding record inserted before Wi-Fi starts;
- a 256-record volatile RAM ring for startup/API outages;
- Modbus CRC, discarded byte, pending, expired, unpaired, exception and queue
  counters, plus an explicit unsupported-semantic-frame gate;
- strict request/response pairing for reads and write acknowledgements, with
  explicit classification for supported write-function traffic;
- communication-address auto-learning from every structurally valid frame,
  exposed as last address, unique-address count and the complete observed list;
- explicit classification of the optional G1 `000000FF` marker, while keeping
  periodic D30-D31 time synchronization distinct from externally corroborated
  Wi-Fi-module restart windows;
- decoded `D30-D31` packed network time with named high/low diagnostic words;
- parsed FC03/FC04 register responses, FC10 time synchronization and the
  observed device-specific FC06 D-register request/response format;
- clean-room `Dxxx` entities for all 178 observed addresses: 67 confirmed
  semantic addresses, 18 explicitly provisional addresses, and 93 raw-only
  addresses,
  with no imported third-party register definitions;
- a checked-in CSV mapping between each HA entity and its stable D address;
- WireGuard status, raw last chunks, parsed frame summary and RGB status LED.

The inverter currently polls 178 register words (`0–60`, `100–216`). Every HA
name starts with its stable `Dxxx` address. Sixty-seven addresses now have
independently cross-checked semantics from local UART plus read-only IoTRix
snapshots; another 18 retain explicit provisional labels until a controlled
change or additional live correlation confirms them. All remaining words stay
explicit raw values.
For example, HA and the CSV use `D11 pv_to_battery_max_charge_current_a`,
`D12 inverter_max_grid_current_a`, and `D24 observed_raw_u16`.
`D18` remains raw: a local preview calls it a communication-address setting,
but its captured value is consistently 0 while the actual RTU frame address is
1, so the firmware reports the wire address directly instead of guessing an
unverified register encoding.

## Build, OTA and online log

Create local configuration files from the sanitized templates:

```sh
cp secrets.example.yaml secrets.yaml
cp wifi.example.yaml wifi.local.yaml
cp wireguard.example.yaml wireguard.local.yaml
```

Fill the three untracked local files for your network and VPN. The root YAML
selects the local include paths through `secrets.yaml`; it contains no
installation-specific absolute paths.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/esphome compile makeskyblue-modbus-monitor.yaml
.venv/bin/esphome upload makeskyblue-modbus-monitor.yaml \
  --device makeskybluemodbusmonitor.local
```

Follow raw UART output online, without USB:

```sh
.venv/bin/esphome logs makeskyblue-modbus-monitor.yaml \
  --device makeskybluemodbusmonitor.local
```

Raw lines look like:

```text
[I][modbus_raw]: boot=305419896 #42 G1/GPIO1 8B RAW_HEX=01.03.00.64.00.33.44.00
[I][modbus_raw]: boot=305419896 #43 G2/GPIO2 37B RAW_HEX=01.03.20...
```

The analyzer's frames and registers CSV outputs include completion epoch
milliseconds plus normalized UTC ISO-8601 timestamps. CRC resynchronization,
unsupported-length data and an incomplete stream tail are emitted as explicit
raw-context rows rather than being hidden behind aggregate counters.
Sequence integrity reports true missing-number ranges/counts separately from
logger emission reordering; reassembly follows the boot-local sequence, so a
complete but out-of-order log is not mislabeled as byte loss.

## Home Assistant

The device can be reached by HA over its installation-specific WireGuard
address. On its device page:

- Sensors contains decoded inverter measurements;
- Diagnostic contains G1/G2 raw chunks, sequence, parser counters and VPN
  state;
- `Onboard Boot Record` displays the startup message;
- Activity displays recent entity changes, including raw chunks.

For a cumulative raw capture, enable the ESPHome integration option “Allow the
device to perform Home Assistant actions”, then use the best-effort
deduplicating File
integration automation in
[`docs/atoms3-g1-g2-modbus-monitor.md`](docs/atoms3-g1-g2-modbus-monitor.md).
YAML-managed HA installations can reuse the matching
[`home-assistant/makeskyblue_uart_capture.yaml`](home-assistant/makeskyblue_uart_capture.yaml)
package after creating the File integration entity.

The HA event drain is at-least-once. AtomS3 retains the ring front and retries
it every second until HA acknowledges that exact boot-session plus `uint32`
sequence after the
File integration append. Duplicate events are possible if the append succeeds
but its ACK is lost; the documented HA helper suppresses a second append by
`(boot ID, sequence)`. A long outage can still exceed the 256-record volatile
ring, and a monitor power loss clears RAM.

## Tests and capture analysis

```sh
c++ -std=c++17 -Wall -Wextra -Werror -I. \
  tests/passive_modbus_monitor_test.cpp -o /tmp/passive_monitor_test
/tmp/passive_monitor_test
c++ -std=c++17 -Wall -Wextra -Werror -I. \
  tests/uart_capture_ring_test.cpp -o /tmp/uart_capture_ring_test
/tmp/uart_capture_ring_test
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python tools/analyze_uart_capture.py --strict \
  --summary-json reports/session.json captures/session.log
```

`--strict` deliberately fails while any catalog address remains unresolved.
The current candidate has 111 unresolved addresses (93 raw-only plus 18
provisional) and therefore cannot yet pass the final semantic gate.

See [`CHANGELOG.md`](CHANGELOG.md) for release status and
[`NOTICE.md`](NOTICE.md) for clean-room provenance.
The independently implementable startup/protocol guide is
[`docs/wifi-module-startup-sequence.md`](docs/wifi-module-startup-sequence.md).
For an end-to-end reproduction, follow
[`docs/reproduce-from-scratch.md`](docs/reproduce-from-scratch.md).

## License

This project's original code, configuration, generated entity catalog and
documentation are licensed under the [Apache License 2.0](LICENSE). The
clean-room boundary and excluded third-party material are described in
[`NOTICE.md`](NOTICE.md).
