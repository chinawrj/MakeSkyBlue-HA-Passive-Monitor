# Changelog

## 0.1.0-alpha.9 - 2026-08-09

- Add a host-side capture watchdog: record a local heartbeat every 30 seconds
  and reconnect an `esphome logs` child that remains alive without output for
  120 seconds. This only renews the ESPHome API log subscription and never
  opens or writes either monitored UART.
- Preserve connect, disconnect, stale-subscription and capture-heartbeat events
  in analyzer JSON so an API evidence outage is not mislabeled as UART silence.
- Remove the previous 20-gap and 50-parse-context report truncation. Every
  sequence gap now retains its boot, direction, surrounding sequence numbers,
  log order and timestamps; every parse issue retains its raw context.

## 0.1.0-alpha.8 - 2026-08-08

- Correlate a user-confirmed IoTRix “enable force-charge schedule” action with
  the passive FC06 request for Modbus D36 raw 1, its device-specific response
  word 0, and the first plus subsequent FC03 readbacks of D36 raw 1.
- Record the preceding D36 raw 0 state and the sanitized UI facts: force charge
  enabled for 22:00-08:00, interval zero days, while force discharge remained
  disabled.
- Expose `D36 force_charge_schedule_enabled_bit0` as a named HA diagnostic
  while retaining the complete D36 status word and explicitly leaving every
  untested force-discharge bit/value unresolved.
- Keep GPIO1/GPIO2 floating RX-only. This release adds no TX pin, UART write,
  active Modbus component, OTA operation, or copied application asset.

## 0.1.0-alpha.7 - 2026-08-08

- Record the controlled IoTRix/panel d13=70 operation as clean-room evidence:
  the on-wire request was FC06 Modbus D10 raw 700, its response value was zero,
  and the first FC03 readback remained D10 raw 600.
- Document that Modbus D13 simultaneously remained raw 70 (7.0 kW), providing
  an independent on-wire demonstration that lowercase panel d13 and uppercase
  Modbus D13 are different address spaces.
- Update only the D10 evidence text and protocol guide. Register names, scaling,
  HA entities, parser behavior and the strict RX-only electrical boundary are
  unchanged.

## 0.1.0-alpha.6 - 2026-08-08

- Add a pure in-memory, dual-RX direction detector. Four matched FC03/FC04
  request/response transactions are required before it identifies the observed
  request line as the future TX candidate and the response line as future RX;
  the detected GPIO mapping is persisted for the later physical cutover.
- Add the one-way HA `PREVIEW Arm Direct Mode Permanently` switch. Once armed,
  it cannot be cleared through HA and waits for 30 continuous seconds without
  bytes on either UART before latching `preview_ready` permanently.
- Keep the preview physically incapable of transmission: GPIO1/GPIO2 remain
  independent floating RX inputs and generated code still has no TX pin or
  UART-write path.
- Add a no-I/O FC03 poll planner with exact unit-tested requests for D0–D60,
  D100–D150, D201–D216 and D151–D200. The planner produces bytes only inside
  memory/tests and is not connected to an ESPHome UART.
- Replace the CI gate's undeclared `rg` dependency with a Python generated-code
  audit, after alpha.5 proved the full firmware compile but failed only because
  GitHub's runner did not provide ripgrep.

## 0.1.0-alpha.5 - 2026-08-08

- Fix the Ubuntu `-Werror=sign-compare` failure in the coherent UINT32 bounds
  check without changing parser behavior.
- Add the role-neutral `makeskyblue-local-link.yaml` entry point. It still
  resolves exclusively to the strict passive RX-only monitor; no TX role is
  shipped.
- Document the planned transition to a physically isolated
  `direct_readonly_bridge`, including project naming, GPIO role change,
  hardware interlocks, the observed four-range FC03 schedule, timing baseline,
  pre-cutover evidence checklist and explicit default ban on FC06/FC10 writes.
- Validate the role-neutral entry in CI while retaining the generated-code
  gate that requires exactly two RX pins and rejects every TX/UART-write path.

## 0.1.0-alpha.4 - 2026-08-08

- Replace installation-specific absolute Wi-Fi and WireGuard include paths
  with secret-selected local configuration files.
- Add sanitized Wi-Fi/WireGuard templates and a from-scratch reproduction
  guide covering hardware, firmware, HA, capture, reboot and 24-hour gates.
- Keep each ring record until Home Assistant explicitly acknowledges its
  sequence after the File append and idempotency-helper update; retry unacknowledged events at least once per
  second and expose send/retry/ACK diagnostics. Scope ACKs and deduplication to
  a random per-boot ID so sequence reset after reboot is unambiguous.
- Send a replacement ring front immediately if an outage overflows the staged
  record, instead of applying the previous record's retry cooldown.
- Raise the logger buffer, retain ESPHome's maximum API queue of 64, and reduce
  entity refresh rates to prevent raw UART log loss during state-update bursts.
  Stagger the generated D-register entities across eight cadences so a normal
  publish burst stays at 24 entities or fewer instead of exceeding the API
  queue in one tick.
- Add free-heap, minimum-free-heap and largest-free-block diagnostics for the
  24-hour runtime gate.
- Make CI compile the portable ESPHome configuration and reject generated code
  unless GPIO1/GPIO2 are exactly two input-only RX pins with no TX or UART
  write path.
- Remove the imported protocol definition and every generated semantic entity
  based on it. Publish all 178 locally observed addresses as stable `Dxxx`
  clean-room entities. Cross-check local UART against read-only IoTRix 2.2.31
  settings/live snapshots and controlled FC06/readback evidence.
- Report the catalog categories without conflating field mapping with evidence:
  67 confirmed semantic, 18 provisional semantic, 93 raw-only and 111
  unresolved addresses.
- Correct D11 scaling to 82.0 A rather than exposing raw 820 A; split D110 into
  independent load/inverter power-factor bytes; decode D145 as human-readable
  firmware version text while retaining its packed raw value.
- Cross-check seven cloud snapshots against a clean 90-second UART capture,
  confirm the D105/D106 grid/output-voltage distinction, and correct cumulative
  generation to a coherent D132-D133 UINT32 instead of a standalone D133 word.
  Record that D131 stayed at raw 84 and did not match daily energy, SOC or the
  tested scaled/offset cloud fields; keep it explicitly unresolved instead of
  assigning a plausible-looking name.
- Visually verify the product manual's lowercase panel d11-d14/d18 definitions
  and document that they map to different uppercase Modbus D addresses; correct
  stale guide text that still called the already corroborated D11/D12 fields
  provisional.
- Expose hour, minute and interval-day subfields plus formatted time for the
  D32-D35 force-charge/discharge packed words.
- Make the strict 24-hour gate fail on any CRC-valid function outside the
  independently validated FC03/FC04/FC10 set, and expose parser residual-byte
  and UART freshness diagnostics instead of silently calling such frames known.
- Validate FC10 quantity/byte-count invariants, classify slave-zero write
  broadcasts without waiting for a response, and expose structural-invalid and
  broadcast counters consistently in firmware and the offline analyzer. Reject
  reserved Modbus addresses 248-255 in both paths while accepting address 247
  and address-zero write broadcasts; reject truncated candidates before field
  access.
- Learn communication addresses only from structurally valid on-wire frames
  and expose the last address, unique count and observed address list to Home
  Assistant and offline summaries. Keep D18 raw because it remains 0 while all
  verified RTU frames use address 1, despite a local preview suggesting that
  D18 is an address-setting field.
- Parse the observed device-specific FC06 transaction completely: retain the
  requested D-register/value, pair the response by slave/function/register,
  preserve its separate 16-bit response value, and expose all three values as
  Home Assistant diagnostics. Captured transactions D11=820 and D10=620 both
  return response value 0, but FC03 proves only D11 changed (800 to 820), while
  D10 remained 600. No standard echo or response-success meaning is assumed.
- Make offline pairing boot-aware and time-aware, require clean capture metadata
  and a completed duration, and reject catalog-external addresses.
- Reassemble chunks by their boot-local capture sequence and report missing
  sequence numbers separately from logger emission reordering. Keep strict
  failure for true gaps, while allowing a complete out-of-order log to pass;
  add CLI-level regression coverage for both cases.
- Write frame-completion epoch milliseconds and normalized UTC ISO-8601 into
  every frames/registers CSV row and startup-marker record so raw traffic and
  IoTRix UI observations can be correlated without re-reading the source log.
- Emit CRC resynchronization, unsupported-length and incomplete-tail rows with
  raw candidate hex, direction, boot/session, sequence range, timestamp,
  expected length and dropped byte instead of exposing only aggregate counts.
- Normalize generated mapping and analysis CSV files to LF line endings so
  clean source archives pass Git whitespace checks on every platform.
- Add analyzer-native summary JSON and sibling SHA-256 output. Each report now
  binds itself to the absolute capture path, byte length, capture SHA-256 and
  UTC analysis time for auditable 30-minute checkpoints.
- Correlate every observed FC06 request with its device response and the first
  following FC03 readback of the same D address. CSV records `match`,
  `mismatch`, or an empty unverified status without treating response value 0
  as success.
- Require adjacent words to come from the same response before exposing a
  coherent UINT32 value.
- Document four externally corroborated Wi-Fi-module restart observations,
  including split 13-byte D30-D31 requests and the fact that `000000FF` is
  optional. Record the counterexample that the same three-write D30-D31 sync
  repeats about every five minutes without a restart, so it is not a restart
  signature by itself.

## 0.1.0-alpha.3 - 2026-08-08

- Classify the observed G1 `00 00 00 FF` Wi-Fi-module startup marker as a
  non-Modbus boot record instead of CRC noise.
- Preserve startup-marker sequence ranges in analyzer JSON/frames CSV and
  expose a dedicated Home Assistant counter.
- Confirm D30-D31 as the high/low words of the 32-bit packed network time,
  name both diagnostic words, and prefix the decoded time entity accordingly.
- Raise confirmed semantic address coverage from 91/178 to 93/178 (52.2%).

## 0.1.0-alpha.2 - 2026-08-08

- Expire unanswered requests after one second, including while UART is idle.
- Match read responses by byte count and write acknowledgements by echoed PDU
  or start/count, preventing a lost response from pairing with a later request.
- Decode and account for mask-write (FC16) and read/write-multiple (FC17)
  traffic without ever transmitting from the monitor.
- Expose mapped/unmapped address counts, semantic coverage, and expired-request
  diagnostics in Home Assistant.
- Order offline frames by completion sequence and recover deduplicated UART
  chunks from ESPHome text-sensor state lines when logger lines are congested.
- Add strict capture-integrity checks for sequence gaps, CRC errors, unmatched
  traffic, pending requests, and unknown functions.

## 0.1.0-alpha.1 - 2026-08-08

- Add strict RX-only GPIO1/GPIO2 UART capture at 9600-8N1.
- Add boot-first `uint32_t` sequence and 256-record RAM ring.
- Add bidirectional Modbus RTU reassembly, CRC validation and request/response
  pairing.
- Publish decoded passive register entities and diagnostics to Home Assistant.
- Correct register 110 into independent inverter factor and power factor bytes.
- Decode force-charge and force-discharge interval fields in registers 32/34.
- Prefix every HA entity name with its stable `Dxxx` register address and
  expose unmapped observed words as diagnostic `Dxxx raw_register` sensors.
- Add `Dxxx` labels and mapped-field names to analyzer CSV output.
- Fix offline capture parsing for short UART chunks without a repeated length.
- Add Wi-Fi OTA, WireGuard connectivity and AtomS3 RGB status indication.

Known alpha limitations:

- The complete 24-hour validation window has not yet passed.
- 111 observed register addresses still require semantic/reserved
  classification.
- HA delivery is at-least-once; duplicates must be deduplicated by sequence,
  and a long outage can still overflow the volatile 256-record ring.
- Full uncommon-function semantic coverage remains planned.
