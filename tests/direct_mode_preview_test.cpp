#include <array>
#include <cassert>
#include <cstdint>
#include <iostream>
#include <vector>

#include "components/direct_mode_preview.h"

using direct_mode_preview::DirectionDetector;

static std::vector<uint8_t> read_response(uint8_t slave, uint16_t count,
                                          uint16_t seed) {
  std::vector<uint8_t> frame{slave, 0x03U,
                             static_cast<uint8_t>(count * 2U)};
  for (uint16_t index = 0; index < count; index++) {
    const uint16_t value = seed + index;
    frame.push_back(static_cast<uint8_t>(value >> 8U));
    frame.push_back(static_cast<uint8_t>(value & 0xFFU));
  }
  const uint16_t crc =
      direct_mode_preview::modbus_crc16(frame.data(), frame.size());
  frame.push_back(static_cast<uint8_t>(crc & 0xFFU));
  frame.push_back(static_cast<uint8_t>(crc >> 8U));
  return frame;
}

static void feed_pair(DirectionDetector &detector, uint8_t request_source,
                      uint8_t response_source, size_t schedule_index,
                      uint32_t timestamp_ms) {
  const auto range =
      direct_mode_preview::OBSERVED_READ_SCHEDULE[schedule_index];
  const auto request =
      direct_mode_preview::make_fc03_request(1U, range.start, range.count);
  detector.feed(request_source, timestamp_ms,
                std::vector<uint8_t>(request.begin(), request.begin() + 3));
  detector.feed(request_source, timestamp_ms + 1U,
                std::vector<uint8_t>(request.begin() + 3, request.end()));
  const auto response = read_response(1U, range.count,
                                      static_cast<uint16_t>(100U + schedule_index));
  const size_t split = response.size() / 2U;
  detector.feed(response_source, timestamp_ms + 100U,
                std::vector<uint8_t>(response.begin(), response.begin() + split));
  detector.feed(response_source, timestamp_ms + 120U,
                std::vector<uint8_t>(response.begin() + split, response.end()));
}

static void test_observed_request_frames() {
  constexpr std::array<std::array<uint8_t, 8>, 4> expected{{
      {{0x01, 0x03, 0x00, 0x00, 0x00, 0x3D, 0x84, 0x1B}},
      {{0x01, 0x03, 0x00, 0x64, 0x00, 0x33, 0x44, 0x00}},
      {{0x01, 0x03, 0x00, 0xC9, 0x00, 0x10, 0x94, 0x38}},
      {{0x01, 0x03, 0x00, 0x97, 0x00, 0x32, 0x75, 0xF3}},
  }};
  direct_mode_preview::ReadOnlyPollPlanner planner;
  for (size_t index = 0; index < expected.size(); index++) {
    assert(planner.index() == index);
    assert(planner.current_request() == expected[index]);
    planner.advance();
  }
  assert(planner.index() == 0U);
}

static void test_direction_detection_both_orientations() {
  DirectionDetector below_threshold;
  for (size_t index = 0; index < 3U; index++)
    feed_pair(below_threshold, 1U, 2U, index,
              static_cast<uint32_t>(index * 500U));
  assert(!below_threshold.detected());
  assert(below_threshold.paired_transactions() == 3U);

  DirectionDetector normal;
  for (size_t index = 0; index < 4U; index++)
    feed_pair(normal, 1U, 2U, index, static_cast<uint32_t>(index * 500U));
  assert(normal.detected());
  assert(normal.request_source() == 1U);
  assert(normal.response_source() == 2U);
  assert(normal.paired_transactions() == 4U);
  assert(normal.conflicting_pairs() == 0U);
  assert(normal.unpaired_responses() == 0U);

  DirectionDetector swapped;
  for (size_t index = 0; index < 4U; index++)
    feed_pair(swapped, 2U, 1U, index, static_cast<uint32_t>(index * 500U));
  assert(swapped.detected());
  assert(swapped.request_source() == 2U);
  assert(swapped.response_source() == 1U);
  assert(swapped.paired_transactions() == 4U);
  assert(swapped.conflicting_pairs() == 0U);
}

static void test_permanent_idle_gate() {
  direct_mode_preview::DirectModeGate gate;
  gate.restore(false, false, 100U);
  assert(!gate.requested());
  assert(!gate.preview_ready());
  gate.note_uart_activity(200U);
  gate.arm_permanently();
  gate.tick(30199U);
  assert(gate.requested());
  assert(!gate.preview_ready());
  gate.tick(30200U);
  assert(gate.preview_ready());
  assert(gate.state() == "preview_ready_no_tx_compiled");
  gate.note_uart_activity(50000U);
  gate.tick(50001U);
  assert(gate.preview_ready());

  direct_mode_preview::DirectModeGate restored;
  restored.restore(false, true, 900U);
  assert(restored.requested());
  assert(restored.preview_ready());
}

static void test_uart_activity_restarts_idle_gate() {
  direct_mode_preview::DirectModeGate gate;
  gate.restore(false, false, 0U);
  gate.arm_permanently();
  for (uint32_t now = 10000U; now <= 60000U; now += 10000U) {
    gate.note_uart_activity(now);
    gate.tick(now + 29999U);
    assert(!gate.preview_ready());
  }
  gate.tick(89999U);
  assert(!gate.preview_ready());
  gate.tick(90000U);
  assert(gate.preview_ready());
}

int main() {
  test_observed_request_frames();
  test_direction_detection_both_orientations();
  test_permanent_idle_gate();
  test_uart_activity_restarts_idle_gate();
  std::cout << "direct_mode_preview_test: PASS\n";
  return 0;
}
