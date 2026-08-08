#include <cassert>
#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

#include "components/passive_modbus_monitor.h"

static std::vector<uint8_t> from_hex(const std::string &value) {
  std::vector<uint8_t> result;
  assert((value.size() & 1U) == 0U);
  for (size_t index = 0; index < value.size(); index += 2) {
    result.push_back(static_cast<uint8_t>(
        std::stoul(value.substr(index, 2), nullptr, 16)));
  }
  return result;
}

static uint16_t modbus_crc(const std::vector<uint8_t> &data) {
  uint16_t crc = 0xFFFF;
  for (const uint8_t byte : data) {
    crc ^= byte;
    for (uint8_t bit = 0; bit < 8; ++bit)
      crc = (crc & 1U) ? static_cast<uint16_t>((crc >> 1U) ^ 0xA001U)
                       : static_cast<uint16_t>(crc >> 1U);
  }
  return crc;
}

static std::vector<uint8_t> with_crc(const std::string &pdu_hex) {
  auto result = from_hex(pdu_hex);
  const uint16_t crc = modbus_crc(result);
  result.push_back(static_cast<uint8_t>(crc & 0xFFU));
  result.push_back(static_cast<uint8_t>(crc >> 8U));
  return result;
}

int main() {
  passive_modbus::PassiveModbusMonitor monitor;
  const auto request = from_hex("01030000003D841B");
  const auto response = from_hex(
      "01037A000200010003000000000003000101E001EA020802580320012C004600"
      "320064000100010000000000000000001E02280000000000000000000000000A"
      "10C92BB000400000000000000000000000000000000000000000000000000000"
      "00000000000000000000000000000000000000000000000000000000005AF1");
  assert(response.size() == 127);

  monitor.feed(1, 10, 1000, request);
  monitor.feed(2, 11, 1050,
               std::vector<uint8_t>(response.begin(), response.begin() + 96));
  assert(monitor.g2_frames() == 0);
  monitor.feed(2, 12, 1070,
               std::vector<uint8_t>(response.begin() + 96, response.end()));

  assert(monitor.g1_frames() == 1);
  assert(monitor.g2_frames() == 1);
  assert(monitor.pending_requests() == 0);
  assert(monitor.crc_errors() == 0);
  assert(monitor.discarded_bytes() == 0);
  assert(monitor.unpaired_responses() == 0);
  assert(monitor.read_responses() == 1);
  assert(monitor.valid_register_count() == 61);
  assert(monitor.raw_u16(0) == 2);
  assert(monitor.raw_u16(7) == 480);
  assert(monitor.raw_u16(30) == 0x0A10);
  assert(monitor.raw_u16(31) == 0xC92B);
  assert(monitor.last_response_latency_ms() == 70);
  assert(monitor.last_slave_address() == 1);
  assert(monitor.observed_slave_address_count() == 1);
  assert(monitor.observed_slave_addresses() == "1");

  const auto time_write =
      from_hex("0110001E0002040A10C9442751");
  const auto time_ack = from_hex("0110001E000221CE");
  monitor.feed(1, 13, 1100, time_write);
  monitor.feed(2, 14, 1120, time_ack);
  assert(monitor.observed_writes() == 1);
  assert(monitor.has_network_time());
  assert(monitor.last_network_time_raw() == 0x0A10C944U);
  assert(monitor.pending_requests() == 0);
  assert(monitor.g1_frames() == 2);
  assert(monitor.g2_frames() == 2);

  monitor.feed(1, 15, 1150, from_hex("000000FF"));
  assert(monitor.wifi_module_startup_markers() == 1);
  assert(monitor.crc_errors() == 0);
  assert(monitor.discarded_bytes() == 0);
  assert(monitor.unknown_functions() == 0);
  assert(monitor.unsupported_semantic_frames() == 0);
  monitor.feed(1, 16, 1160, request);
  assert(monitor.g1_frames() == 3);

  // Real device trace: FC06 writes D11=0x0334, while the response keeps D11
  // but returns 0x0000 rather than echoing 0x0334.
  passive_modbus::PassiveModbusMonitor fc06_monitor;
  fc06_monitor.feed(1, 1, 1000, from_hex("0106000B0334F92F"));
  fc06_monitor.feed(2, 2, 1050, from_hex("0106000B0000F808"));
  assert(fc06_monitor.pending_requests() == 0);
  assert(fc06_monitor.unpaired_responses() == 0);
  assert(fc06_monitor.unsupported_semantic_frames() == 0);
  assert(fc06_monitor.observed_writes() == 1);
  assert(fc06_monitor.last_write_start() == 11);
  assert(fc06_monitor.last_fc06_register() == 11);
  assert(fc06_monitor.last_fc06_write_value() == 0x0334);
  assert(fc06_monitor.last_fc06_response_value() == 0x0000);
  assert(fc06_monitor.fc06_transactions() == 1);

  passive_modbus::PassiveModbusMonitor strict_ack_monitor;
  strict_ack_monitor.feed(1, 1, 1000, time_write);
  strict_ack_monitor.feed(2, 2, 1010, with_crc("0110001E0003"));
  assert(strict_ack_monitor.unpaired_responses() == 1);
  assert(strict_ack_monitor.pending_requests() == 1);
  strict_ack_monitor.feed(2, 3, 1020, time_ack);
  assert(strict_ack_monitor.pending_requests() == 0);

  passive_modbus::PassiveModbusMonitor malformed_monitor;
  const auto malformed_fc10 = with_crc("0110001E000202AAAA");
  malformed_monitor.feed(1, 1, 1000, malformed_fc10);
  malformed_monitor.feed(2, 2, 1010, time_ack);
  assert(malformed_monitor.structurally_invalid_frames() == 1);
  assert(malformed_monitor.observed_writes() == 0);
  assert(malformed_monitor.pending_requests() == 0);
  assert(malformed_monitor.unpaired_responses() == 1);

  passive_modbus::PassiveModbusMonitor broadcast_monitor;
  const auto broadcast_fc10 = with_crc("0010001E00020411223344");
  broadcast_monitor.feed(1, 1, 1000, broadcast_fc10);
  broadcast_monitor.tick(3000);
  assert(broadcast_monitor.broadcast_requests() == 1);
  assert(broadcast_monitor.observed_writes() == 1);
  assert(broadcast_monitor.pending_requests() == 0);
  assert(broadcast_monitor.expired_requests() == 0);
  broadcast_monitor.feed(1, 2, 3010, with_crc("000300000001"));
  assert(broadcast_monitor.structurally_invalid_frames() == 1);

  passive_modbus::PassiveModbusMonitor address_monitor;
  address_monitor.feed(1, 1, 1000, with_crc("F70300000001"));
  address_monitor.feed(2, 2, 1010, with_crc("F703020001"));
  assert(address_monitor.pending_requests() == 0);
  assert(address_monitor.structurally_invalid_frames() == 0);
  assert(address_monitor.last_slave_address() == 247);
  assert(address_monitor.observed_slave_address_count() == 1);
  assert(address_monitor.observed_slave_addresses() == "247");
  address_monitor.feed(1, 3, 1020, with_crc("F80300000001"));
  address_monitor.feed(2, 4, 1030, with_crc("F803020001"));
  address_monitor.feed(1, 5, 1040, with_crc("FF10001E00020411223344"));
  assert(address_monitor.structurally_invalid_frames() == 3);
  assert(address_monitor.pending_requests() == 0);
  assert(address_monitor.last_slave_address() == 247);
  assert(address_monitor.observed_slave_address_count() == 1);

  // The structural validator must reject truncated CRC-valid candidates
  // without reading fields beyond the supplied frame.
  address_monitor.feed(1, 6, 1050, with_crc("0103"));
  address_monitor.feed(1, 7, 1060, with_crc("01100000"));
  assert(address_monitor.structurally_invalid_frames() == 3);
  // The stream parser waits for the fixed/declared length, so both fragments
  // remain buffered rather than being accepted as Modbus frames.
  assert(address_monitor.stream_buffered_bytes(1) > 0);

  passive_modbus::PassiveModbusMonitor timeout_monitor;
  timeout_monitor.feed(1, 1, 1000, request);
  timeout_monitor.feed(1, 2, 2001, from_hex("0103006400334400"));
  assert(timeout_monitor.expired_requests() == 1);
  assert(timeout_monitor.pending_requests() == 1);

  passive_modbus::PassiveModbusMonitor read_write_monitor;
  const auto read_write_request =
      with_crc("01170064000200C900020411223344");
  const auto read_write_response = with_crc("011704AAAA5555");
  read_write_monitor.feed(1, 1, 1000, read_write_request);
  read_write_monitor.feed(2, 2, 1050, read_write_response);
  assert(read_write_monitor.pending_requests() == 0);
  assert(read_write_monitor.observed_writes() == 1);
  assert(read_write_monitor.last_write_start() == 201);
  assert(read_write_monitor.last_write_count() == 2);
  assert(read_write_monitor.last_read_start() == 100);
  assert(read_write_monitor.last_read_count() == 2);
  assert(read_write_monitor.raw_u16(100) == 0xAAAA);
  assert(read_write_monitor.raw_u16(101) == 0x5555);
  assert(read_write_monitor.unsupported_semantic_frames() == 2);

  // Adjacent words from different responses must never be exposed as a
  // coherent UINT32 value.
  passive_modbus::PassiveModbusMonitor torn_u32_monitor;
  const auto first_word_request = with_crc("010300640001");
  const auto second_word_request = with_crc("010300650001");
  torn_u32_monitor.feed(1, 1, 1000, first_word_request);
  torn_u32_monitor.feed(2, 2, 1010, with_crc("0103021122"));
  torn_u32_monitor.feed(1, 3, 1020, second_word_request);
  torn_u32_monitor.feed(2, 4, 1030, with_crc("0103023344"));
  assert(torn_u32_monitor.has_register(100));
  assert(torn_u32_monitor.has_register(101));
  assert(!torn_u32_monitor.has_u32(100));

  std::cout << "passive_modbus_monitor_test: PASS\n";
  return 0;
}
