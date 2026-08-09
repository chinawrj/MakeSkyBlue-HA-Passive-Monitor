#pragma once

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <vector>

namespace modbus_capture {

static constexpr size_t CAPACITY = 256;
static constexpr size_t MAX_PAYLOAD = 96;

enum class RecordType : uint8_t {
  BOOT = 0,
  UART = 1,
  HEARTBEAT = 2,
};

struct Record {
  RecordType type{RecordType::BOOT};
  uint8_t source{0};  // 0=onboard, 1=G1/GPIO1, 2=G2/GPIO2
  uint16_t length{0};
  uint32_t sequence{0};
  uint32_t uptime_ms{0};
  uint32_t overwritten_total{0};
  std::array<uint8_t, MAX_PAYLOAD> payload{};
};

class CaptureRing {
 public:
  void push_boot(uint32_t sequence, uint32_t uptime_ms) {
    Record record{};
    record.type = RecordType::BOOT;
    record.sequence = sequence;
    record.uptime_ms = uptime_ms;
    this->push_(record);
  }

  void push_uart(uint8_t source, uint32_t sequence, uint32_t uptime_ms,
                 const std::vector<uint8_t> &bytes) {
    Record record{};
    record.type = RecordType::UART;
    record.source = source;
    record.sequence = sequence;
    record.uptime_ms = uptime_ms;
    record.length = static_cast<uint16_t>(std::min(bytes.size(), MAX_PAYLOAD));
    std::copy_n(bytes.begin(), record.length, record.payload.begin());
    this->push_(record);
  }

  void push_heartbeat(uint32_t sequence, uint32_t uptime_ms) {
    Record record{};
    record.type = RecordType::HEARTBEAT;
    record.sequence = sequence;
    record.uptime_ms = uptime_ms;
    this->push_(record);
  }

  bool stage_front() {
    if (this->staged_valid_) {
      return true;
    }
    if (this->count_ == 0) {
      this->staged_valid_ = false;
      return false;
    }
    this->staged_ = this->records_[this->head_];
    this->staged_valid_ = true;
    return true;
  }

  bool acknowledge_staged(uint32_t sequence) {
    if (!this->staged_valid_ || this->count_ == 0) {
      return false;
    }
    if (sequence != this->staged_.sequence ||
        this->records_[this->head_].sequence != this->staged_.sequence) {
      return false;
    }
    this->head_ = (this->head_ + 1) % CAPACITY;
    this->count_--;
    this->staged_valid_ = false;
    return true;
  }

  size_t size() const { return this->count_; }
  uint32_t overwritten_total() const { return this->overwritten_total_; }
  bool has_staged() const { return this->staged_valid_; }

  std::string staged_type() const {
    if (this->staged_.type == RecordType::BOOT) return "BOOT";
    if (this->staged_.type == RecordType::HEARTBEAT) return "HEARTBEAT";
    return "UART";
  }

  std::string staged_source() const {
    if (this->staged_.source == 1) return "G1";
    if (this->staged_.source == 2) return "G2";
    return "ONBOARD";
  }

  std::string staged_gpio() const {
    if (this->staged_.source == 1) return "GPIO1";
    if (this->staged_.source == 2) return "GPIO2";
    return "INTERNAL";
  }

  uint32_t staged_sequence() const { return this->staged_.sequence; }
  uint32_t staged_uptime_ms() const { return this->staged_.uptime_ms; }
  uint16_t staged_length() const { return this->staged_.length; }
  uint32_t staged_overwritten_total() const {
    return this->staged_.overwritten_total;
  }

  std::string staged_hex() const {
    static constexpr char HEX[] = "0123456789ABCDEF";
    std::string value;
    value.reserve(this->staged_.length * 2);
    for (size_t index = 0; index < this->staged_.length; index++) {
      const uint8_t byte = this->staged_.payload[index];
      value.push_back(HEX[byte >> 4]);
      value.push_back(HEX[byte & 0x0F]);
    }
    return value;
  }

 private:
  void push_(Record &record) {
    if (this->count_ == CAPACITY) {
      if (this->staged_valid_ &&
          this->records_[this->head_].sequence == this->staged_.sequence) {
        // The pending HA record can no longer be acknowledged because the
        // bounded ring must make room for newly captured UART data.
        this->staged_valid_ = false;
      }
      this->head_ = (this->head_ + 1) % CAPACITY;
      this->count_--;
      this->overwritten_total_++;
    }
    record.overwritten_total = this->overwritten_total_;
    const size_t tail = (this->head_ + this->count_) % CAPACITY;
    this->records_[tail] = record;
    this->count_++;
  }

  std::array<Record, CAPACITY> records_{};
  size_t head_{0};
  size_t count_{0};
  uint32_t overwritten_total_{0};
  Record staged_{};
  bool staged_valid_{false};
};

inline CaptureRing ring;

}  // namespace modbus_capture
