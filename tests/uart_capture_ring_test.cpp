#include <cassert>
#include <cstdint>
#include <iostream>
#include <vector>

#include "components/uart_capture_ring.h"

int main() {
  modbus_capture::CaptureRing ring;
  ring.push_boot(1U, 5U);
  ring.push_uart(1U, 2U, 10U, std::vector<uint8_t>{0x01U, 0x03U});

  assert(ring.size() == 2U);
  assert(ring.stage_front());
  assert(ring.staged_sequence() == 1U);
  assert(!ring.acknowledge_staged(2U));
  assert(ring.size() == 2U);
  assert(ring.has_staged());

  // Retrying must preserve the same front until the exact ACK arrives.
  assert(ring.stage_front());
  assert(ring.staged_sequence() == 1U);
  assert(ring.acknowledge_staged(1U));
  assert(ring.size() == 1U);
  assert(!ring.has_staged());

  assert(ring.stage_front());
  assert(ring.staged_sequence() == 2U);
  assert(ring.staged_hex() == "0103");
  assert(ring.acknowledge_staged(2U));
  assert(ring.size() == 0U);
  assert(!ring.acknowledge_staged(2U));

  ring.push_heartbeat(3U, 30U);
  assert(ring.size() == 1U);
  assert(ring.stage_front());
  assert(ring.staged_type() == "HEARTBEAT");
  assert(ring.staged_source() == "ONBOARD");
  assert(ring.staged_gpio() == "INTERNAL");
  assert(ring.staged_hex().empty());
  assert(ring.acknowledge_staged(3U));

  // A bounded ring remains live under prolonged HA outage and reports loss.
  for (uint32_t sequence = 10U;
       sequence < 10U + modbus_capture::CAPACITY; ++sequence) {
    ring.push_uart(2U, sequence, sequence, std::vector<uint8_t>{0xAAU});
  }
  assert(ring.stage_front());
  const uint32_t overwritten_sequence = ring.staged_sequence();
  ring.push_uart(2U, 999U, 999U, std::vector<uint8_t>{0xBBU});
  assert(ring.overwritten_total() == 1U);
  assert(!ring.has_staged());
  assert(!ring.acknowledge_staged(overwritten_sequence));

  std::cout << "uart_capture_ring_test: PASS\n";
  return 0;
}
