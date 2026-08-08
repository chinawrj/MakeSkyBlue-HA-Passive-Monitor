#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <iterator>
#include <string>
#include <vector>

namespace passive_modbus {

static constexpr size_t MAX_FRAME_LENGTH = 260;
static constexpr size_t MAX_PENDING_REQUESTS = 16;
static constexpr size_t REGISTER_CAPACITY = 256;
static constexpr uint32_t REQUEST_TIMEOUT_MS = 1000;
static constexpr std::array<uint8_t, 4> WIFI_MODULE_STARTUP_MARKER{
    0x00, 0x00, 0x00, 0xFF};

struct RegisterValue {
  uint16_t raw{0};
  uint32_t response_sequence{0};
  uint32_t updated_ms{0};
  bool valid{false};
};

struct PendingRequest {
  uint8_t slave{0};
  uint8_t function{0};
  uint16_t start{0};
  uint16_t count{0};
  uint32_t sequence{0};
  uint32_t timestamp_ms{0};
  std::vector<uint8_t> request_frame{};
};

struct StreamState {
  std::vector<uint8_t> bytes{};
  uint32_t first_sequence{0};
  uint32_t last_sequence{0};
};

class PassiveModbusMonitor {
 public:
  void feed(uint8_t source, uint32_t sequence, uint32_t timestamp_ms,
            const std::vector<uint8_t> &chunk) {
    if (source < 1 || source > 2 || chunk.empty()) return;
    this->expire_pending_(timestamp_ms);
    StreamState &stream = this->streams_[source - 1];
    if (stream.bytes.empty()) stream.first_sequence = sequence;
    stream.last_sequence = sequence;
    stream.bytes.insert(stream.bytes.end(), chunk.begin(), chunk.end());
    this->process_stream_(source, timestamp_ms, stream);
  }

  void tick(uint32_t timestamp_ms) { this->expire_pending_(timestamp_ms); }

  bool has_register(uint16_t address) const {
    return address < REGISTER_CAPACITY && this->registers_[address].valid;
  }

  uint16_t raw_u16(uint16_t address) const {
    return this->has_register(address) ? this->registers_[address].raw : 0;
  }

  int16_t raw_s16(uint16_t address) const {
    return static_cast<int16_t>(this->raw_u16(address));
  }

  bool has_u32(uint16_t address) const {
    return static_cast<size_t>(address) + 1U < REGISTER_CAPACITY &&
           this->has_register(address) &&
           this->has_register(address + 1) &&
           this->registers_[address].response_sequence ==
               this->registers_[address + 1].response_sequence;
  }

  uint32_t raw_u32(uint16_t address) const {
    if (!this->has_u32(address)) return 0;
    return (static_cast<uint32_t>(this->raw_u16(address)) << 16) |
           this->raw_u16(address + 1);
  }

  uint32_t register_sequence(uint16_t address) const {
    return this->has_register(address)
               ? this->registers_[address].response_sequence
               : 0;
  }

  uint32_t g1_frames() const { return this->g1_frames_; }
  uint32_t g2_frames() const { return this->g2_frames_; }
  uint32_t crc_errors() const { return this->crc_errors_; }
  uint32_t discarded_bytes() const { return this->discarded_bytes_; }
  uint32_t unknown_functions() const { return this->unknown_functions_; }
  uint32_t unsupported_semantic_frames() const {
    return this->unsupported_semantic_frames_;
  }
  uint32_t structurally_invalid_frames() const {
    return this->structurally_invalid_frames_;
  }
  uint32_t broadcast_requests() const { return this->broadcast_requests_; }
  uint32_t unpaired_responses() const { return this->unpaired_responses_; }
  uint32_t pending_requests() const { return this->pending_.size(); }
  uint32_t pending_overflows() const { return this->pending_overflows_; }
  uint32_t expired_requests() const { return this->expired_requests_; }
  uint32_t wifi_module_startup_markers() const {
    return this->wifi_module_startup_markers_;
  }
  uint32_t exception_responses() const { return this->exception_responses_; }
  uint32_t observed_writes() const { return this->observed_writes_; }
  uint32_t read_responses() const { return this->read_responses_; }
  uint32_t parsed_register_words() const { return this->parsed_register_words_; }
  uint8_t last_slave_address() const { return this->last_slave_address_; }
  uint16_t observed_slave_address_count() const {
    return this->observed_slave_address_count_;
  }
  std::string observed_slave_addresses() const {
    std::string result;
    for (uint16_t address = 1U; address <= 247U; address++) {
      if (!this->observed_slave_addresses_[address]) continue;
      if (!result.empty()) result += ",";
      result += std::to_string(address);
    }
    return result;
  }
  uint32_t valid_register_count() const {
    uint32_t count = 0;
    for (const auto &value : this->registers_) count += value.valid ? 1U : 0U;
    return count;
  }
  uint32_t last_frame_sequence() const { return this->last_frame_sequence_; }
  uint32_t last_frame_ms() const { return this->last_frame_ms_; }
  size_t stream_buffered_bytes(uint8_t source) const {
    return source >= 1U && source <= 2U ? this->streams_[source - 1U].bytes.size()
                                       : 0U;
  }
  uint32_t last_response_latency_ms() const {
    return this->last_response_latency_ms_;
  }
  uint16_t last_read_start() const { return this->last_read_start_; }
  uint16_t last_read_count() const { return this->last_read_count_; }
  uint16_t last_write_start() const { return this->last_write_start_; }
  uint16_t last_write_count() const { return this->last_write_count_; }
  uint16_t last_fc06_write_value() const {
    return this->last_fc06_write_value_;
  }
  uint16_t last_fc06_register() const { return this->last_fc06_register_; }
  uint16_t last_fc06_response_value() const {
    return this->last_fc06_response_value_;
  }
  uint32_t fc06_transactions() const { return this->fc06_transactions_; }
  uint32_t last_network_time_raw() const { return this->last_network_time_raw_; }
  bool has_network_time() const { return this->has_network_time_; }
  const std::string &last_frame_hex() const { return this->last_frame_hex_; }
  const std::string &last_frame_summary() const {
    return this->last_frame_summary_;
  }

 private:
  static uint16_t crc16_(const uint8_t *data, size_t length) {
    uint16_t crc = 0xFFFF;
    for (size_t index = 0; index < length; index++) {
      crc ^= data[index];
      for (uint8_t bit = 0; bit < 8; bit++) {
        crc = (crc & 1U) ? static_cast<uint16_t>((crc >> 1U) ^ 0xA001U)
                         : static_cast<uint16_t>(crc >> 1U);
      }
    }
    return crc;
  }

  static bool crc_valid_(const std::vector<uint8_t> &bytes, size_t length) {
    if (length < 4 || bytes.size() < length) return false;
    const uint16_t expected = static_cast<uint16_t>(bytes[length - 2]) |
                              (static_cast<uint16_t>(bytes[length - 1]) << 8U);
    return crc16_(bytes.data(), length - 2) == expected;
  }

  static bool frame_structure_valid_(uint8_t source,
                                     const std::vector<uint8_t> &frame) {
    if (frame.size() < 4U) return false;
    const uint8_t function = frame[1];
    if (frame[0] > 247U) return false;
    if ((function & 0x80U) != 0U)
      return source == 2U && frame[0] >= 1U && frame.size() == 5U;
    if (source == 2U && frame[0] == 0U) return false;
    if (source == 1U && frame[0] == 0U && function != 0x05U &&
        function != 0x06U && function != 0x0FU && function != 0x10U &&
        function != 0x16U)
      return false;
    if (function == 0x03U || function == 0x04U) {
      if (source == 1U) {
        if (frame.size() != 8U) return false;
        const uint16_t count =
            (static_cast<uint16_t>(frame[4]) << 8U) | frame[5];
        return frame[0] != 0U && count >= 1U && count <= 125U;
      }
      if (frame.size() < 5U) return false;
      const uint8_t byte_count = frame[2];
      return (byte_count & 1U) == 0U &&
             frame.size() == static_cast<size_t>(5U + byte_count);
    }
    if (function == 0x10U) {
      if (source == 1U) {
        if (frame.size() < 9U) return false;
        const uint16_t count =
            (static_cast<uint16_t>(frame[4]) << 8U) | frame[5];
        const uint8_t byte_count = frame[6];
        return frame[0] <= 247U && count >= 1U && count <= 123U &&
               byte_count == count * 2U &&
               frame.size() == static_cast<size_t>(9U + byte_count);
      }
      return frame.size() == 8U;
    }
    if (function == 0x06U) return frame.size() == 8U;
    return true;
  }

  static int expected_length_(uint8_t source,
                              const std::vector<uint8_t> &bytes) {
    if (bytes.size() < 2) return 0;
    const uint8_t function = bytes[1];
    if ((function & 0x80U) != 0U) return 5;

    if (source == 1) {
      switch (function) {
        case 0x01:
        case 0x02:
        case 0x03:
        case 0x04:
        case 0x05:
        case 0x06:
        case 0x08:
          return 8;
        case 0x07:
        case 0x0B:
        case 0x0C:
        case 0x11:
          return 4;
        case 0x16:
          return 10;
        case 0x0F:
        case 0x10:
          return bytes.size() < 7 ? 0 : 9 + bytes[6];
        case 0x17:
          return bytes.size() < 11 ? 0 : 13 + bytes[10];
        default:
          return -1;
      }
    }

    switch (function) {
      case 0x07:
        return 5;
      case 0x05:
      case 0x06:
      case 0x08:
      case 0x0B:
      case 0x0F:
      case 0x10:
        return 8;
      case 0x16:
        return 10;
      case 0x01:
      case 0x02:
      case 0x03:
      case 0x04:
      case 0x0C:
      case 0x11:
      case 0x17:
        return bytes.size() < 3 ? 0 : 5 + bytes[2];
      default:
        return -1;
    }
  }

  static std::string hex_(const uint8_t *data, size_t length) {
    static constexpr char HEX[] = "0123456789ABCDEF";
    std::string result;
    result.reserve(length * 2);
    for (size_t index = 0; index < length; index++) {
      result.push_back(HEX[data[index] >> 4]);
      result.push_back(HEX[data[index] & 0x0F]);
    }
    return result;
  }

  void process_stream_(uint8_t source, uint32_t timestamp_ms,
                       StreamState &stream) {
    while (!stream.bytes.empty()) {
      if (source == 1) {
        const size_t prefix_length =
            std::min(stream.bytes.size(), WIFI_MODULE_STARTUP_MARKER.size());
        if (std::equal(stream.bytes.begin(),
                       stream.bytes.begin() + prefix_length,
                       WIFI_MODULE_STARTUP_MARKER.begin())) {
          if (stream.bytes.size() < WIFI_MODULE_STARTUP_MARKER.size()) return;
          stream.bytes.erase(
              stream.bytes.begin(),
              stream.bytes.begin() + WIFI_MODULE_STARTUP_MARKER.size());
          this->wifi_module_startup_markers_++;
          this->last_frame_sequence_ = stream.last_sequence;
          this->last_frame_ms_ = timestamp_ms;
          this->last_frame_hex_ = "000000FF";
          this->last_frame_summary_ =
              "G1 Wi-Fi module startup marker 000000FF";
          stream.first_sequence = stream.last_sequence;
          continue;
        }
      }
      const int expected = expected_length_(source, stream.bytes);
      if (expected == 0) return;
      if (expected < 4 || expected > static_cast<int>(MAX_FRAME_LENGTH)) {
        stream.bytes.erase(stream.bytes.begin());
        this->discarded_bytes_++;
        this->unknown_functions_++;
        stream.first_sequence = stream.last_sequence;
        continue;
      }
      if (stream.bytes.size() < static_cast<size_t>(expected)) return;
      if (!crc_valid_(stream.bytes, expected)) {
        stream.bytes.erase(stream.bytes.begin());
        this->crc_errors_++;
        this->discarded_bytes_++;
        stream.first_sequence = stream.last_sequence;
        continue;
      }

      std::vector<uint8_t> frame(stream.bytes.begin(),
                                 stream.bytes.begin() + expected);
      this->handle_frame_(source, stream.first_sequence, stream.last_sequence,
                          timestamp_ms, frame);
      stream.bytes.erase(stream.bytes.begin(), stream.bytes.begin() + expected);
      stream.first_sequence = stream.last_sequence;
    }
  }

  static bool response_matches_(const PendingRequest &request,
                                const std::vector<uint8_t> &response) {
    if (response.size() < 5 || request.slave != response[0] ||
        request.function != (response[1] & 0x7FU))
      return false;
    if ((response[1] & 0x80U) != 0U) return true;
    if (request.function == 0x01 || request.function == 0x02)
      return response[2] == (request.count + 7U) / 8U;
    if (request.function == 0x03 || request.function == 0x04 ||
        request.function == 0x17)
      return response[2] == request.count * 2U;
    // This inverter's observed FC06 reply preserves slave/function/register,
    // but its final word is a device response value rather than the Modbus
    // specification's echoed write value. Match the independently observed
    // wire behavior without discarding either value.
    if (request.function == 0x06) {
      return response.size() == 8U &&
             response[2] == request.request_frame[2] &&
             response[3] == request.request_frame[3];
    }
    if (request.function == 0x05 || request.function == 0x08 ||
        request.function == 0x16) {
      if (response.size() != request.request_frame.size()) return false;
      return std::equal(response.begin(), response.end() - 2,
                        request.request_frame.begin());
    }
    if (request.function == 0x0F || request.function == 0x10) {
      const uint16_t response_start =
          (static_cast<uint16_t>(response[2]) << 8U) | response[3];
      const uint16_t response_count =
          (static_cast<uint16_t>(response[4]) << 8U) | response[5];
      return response_start == request.start && response_count == request.count;
    }
    return request.function == 0x07 || request.function == 0x0B ||
           request.function == 0x0C || request.function == 0x11;
  }

  void expire_pending_(uint32_t timestamp_ms) {
    const auto first_expired = std::remove_if(
        this->pending_.begin(), this->pending_.end(),
        [timestamp_ms](const PendingRequest &request) {
          return timestamp_ms - request.timestamp_ms > REQUEST_TIMEOUT_MS;
        });
    this->expired_requests_ +=
        static_cast<uint32_t>(std::distance(first_expired, this->pending_.end()));
    this->pending_.erase(first_expired, this->pending_.end());
  }

  void handle_frame_(uint8_t source, uint32_t first_sequence,
                     uint32_t last_sequence, uint32_t timestamp_ms,
                     const std::vector<uint8_t> &frame) {
    if (source == 1)
      this->g1_frames_++;
    else
      this->g2_frames_++;
    this->last_frame_sequence_ = last_sequence;
    this->last_frame_ms_ = timestamp_ms;
    this->last_frame_hex_ = hex_(frame.data(), frame.size());

    const uint8_t function = frame[1];
    const uint8_t base_function = function & 0x7FU;
    // These are the only functions whose complete request/response fields and
    // device-specific meaning have been independently validated on this UART.
    // Other CRC-valid functions remain preserved as raw frames but must make
    // the 24-hour semantic gate fail until a parser and evidence are added.
    if (base_function != 0x03U && base_function != 0x04U &&
        base_function != 0x06U && base_function != 0x10U) {
      this->unsupported_semantic_frames_++;
    }
    if (!frame_structure_valid_(source, frame)) {
      this->structurally_invalid_frames_++;
      this->last_frame_summary_ =
          (source == 1U ? "G1" : "G2") +
          std::string(" structurally invalid FC") + hex_(&function, 1);
      return;
    }
    if (frame[0] >= 1U) {
      this->last_slave_address_ = frame[0];
      if (!this->observed_slave_addresses_[frame[0]]) {
        this->observed_slave_addresses_[frame[0]] = true;
        this->observed_slave_address_count_++;
      }
    }
    if (source == 1) {
      PendingRequest request{};
      request.slave = frame[0];
      request.function = function;
      request.sequence = first_sequence;
      request.timestamp_ms = timestamp_ms;
      request.request_frame = frame;
      if (function == 0x01 || function == 0x02 || function == 0x03 ||
          function == 0x04 || function == 0x0F || function == 0x10 ||
          function == 0x17) {
        request.start = (static_cast<uint16_t>(frame[2]) << 8U) | frame[3];
        request.count = (static_cast<uint16_t>(frame[4]) << 8U) | frame[5];
      } else if (function == 0x05 || function == 0x06) {
        request.start = (static_cast<uint16_t>(frame[2]) << 8U) | frame[3];
        request.count = 1;
      } else if (function == 0x16) {
        request.start = (static_cast<uint16_t>(frame[2]) << 8U) | frame[3];
        request.count = 1;
      }
      if (frame[0] == 0U) {
        this->broadcast_requests_++;
      } else {
        if (this->pending_.size() == MAX_PENDING_REQUESTS) {
          this->pending_.pop_front();
          this->pending_overflows_++;
        }
        this->pending_.push_back(request);
      }

      if (function == 0x05 || function == 0x06 || function == 0x0F ||
          function == 0x10 || function == 0x16 || function == 0x17) {
        this->observed_writes_++;
        if (function == 0x17 && frame.size() >= 13) {
          this->last_write_start_ =
              (static_cast<uint16_t>(frame[6]) << 8U) | frame[7];
          this->last_write_count_ =
              (static_cast<uint16_t>(frame[8]) << 8U) | frame[9];
        } else {
          this->last_write_start_ = request.start;
          this->last_write_count_ = request.count;
        }
        if (function == 0x10 && request.start == 30 && request.count == 2 &&
            frame.size() >= 13 && frame[6] == 4) {
          this->last_network_time_raw_ =
              (static_cast<uint32_t>(frame[7]) << 24U) |
              (static_cast<uint32_t>(frame[8]) << 16U) |
              (static_cast<uint32_t>(frame[9]) << 8U) | frame[10];
          this->has_network_time_ = true;
        }
        if (function == 0x06U) {
          this->last_fc06_register_ = request.start;
          this->last_fc06_write_value_ =
              (static_cast<uint16_t>(frame[4]) << 8U) | frame[5];
        }
        this->last_frame_summary_ =
            "G1 observed write FC" + hex_(&function, 1) + " start=" +
            std::to_string(request.start) + " count=" +
            std::to_string(request.count);
      } else {
        this->last_frame_summary_ =
            "G1 request FC" + hex_(&function, 1) + " start=" +
            std::to_string(request.start) + " count=" +
            std::to_string(request.count);
      }
      return;
    }

    auto match = std::find_if(
        this->pending_.begin(), this->pending_.end(),
        [&frame](const PendingRequest &request) {
          return response_matches_(request, frame);
        });
    if (match == this->pending_.end()) {
      this->unpaired_responses_++;
      this->last_frame_summary_ = "G2 unpaired response FC" + hex_(&function, 1);
      return;
    }

    const PendingRequest request = *match;
    this->pending_.erase(match);
    this->last_response_latency_ms_ = timestamp_ms - request.timestamp_ms;
    if ((function & 0x80U) != 0U) {
      this->exception_responses_++;
      this->last_frame_summary_ =
          "G2 exception FC" + hex_(&function, 1) + " code=" +
          std::to_string(frame[2]);
      return;
    }

    if (request.function == 0x06U) {
      this->last_fc06_response_value_ =
          (static_cast<uint16_t>(frame[4]) << 8U) | frame[5];
      this->fc06_transactions_++;
    }

    if (request.function == 0x03 || request.function == 0x04 ||
        request.function == 0x17) {
      const size_t words = frame[2] / 2U;
      const size_t available_words = (frame.size() - 5U) / 2U;
      const size_t parsed_words = std::min(words, available_words);
      for (size_t index = 0; index < parsed_words; index++) {
        const uint32_t address = request.start + index;
        if (address >= REGISTER_CAPACITY) continue;
        RegisterValue &value = this->registers_[address];
        value.raw = (static_cast<uint16_t>(frame[3 + index * 2]) << 8U) |
                    frame[4 + index * 2];
        value.response_sequence = first_sequence;
        value.updated_ms = timestamp_ms;
        value.valid = true;
        this->parsed_register_words_++;
      }
      this->read_responses_++;
      this->last_read_start_ = request.start;
      this->last_read_count_ = request.count;
      this->last_frame_summary_ =
          "G2 read response FC" + hex_(&function, 1) + " start=" +
          std::to_string(request.start) + " count=" +
          std::to_string(request.count);
    } else {
      this->last_frame_summary_ =
          "G2 write acknowledgement FC" + hex_(&function, 1) + " start=" +
          std::to_string(request.start) + " count=" +
          std::to_string(request.count);
    }
  }

  std::array<StreamState, 2> streams_{};
  std::deque<PendingRequest> pending_{};
  std::array<RegisterValue, REGISTER_CAPACITY> registers_{};
  std::array<bool, 248> observed_slave_addresses_{};
  uint32_t g1_frames_{0};
  uint32_t g2_frames_{0};
  uint32_t crc_errors_{0};
  uint32_t discarded_bytes_{0};
  uint32_t unknown_functions_{0};
  uint32_t unsupported_semantic_frames_{0};
  uint32_t structurally_invalid_frames_{0};
  uint32_t broadcast_requests_{0};
  uint32_t unpaired_responses_{0};
  uint32_t pending_overflows_{0};
  uint32_t expired_requests_{0};
  uint32_t wifi_module_startup_markers_{0};
  uint32_t exception_responses_{0};
  uint32_t observed_writes_{0};
  uint32_t read_responses_{0};
  uint32_t parsed_register_words_{0};
  uint16_t observed_slave_address_count_{0};
  uint8_t last_slave_address_{0};
  uint32_t last_frame_sequence_{0};
  uint32_t last_frame_ms_{0};
  uint32_t last_response_latency_ms_{0};
  uint16_t last_read_start_{0};
  uint16_t last_read_count_{0};
  uint16_t last_write_start_{0};
  uint16_t last_write_count_{0};
  uint16_t last_fc06_register_{0};
  uint16_t last_fc06_write_value_{0};
  uint16_t last_fc06_response_value_{0};
  uint32_t fc06_transactions_{0};
  uint32_t last_network_time_raw_{0};
  bool has_network_time_{false};
  std::string last_frame_hex_{};
  std::string last_frame_summary_{};
};

inline PassiveModbusMonitor monitor;

}  // namespace passive_modbus
