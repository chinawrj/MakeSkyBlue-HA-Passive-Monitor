#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <string>
#include <vector>

// Pure in-memory preparation for a future direct communication role.
//
// This file deliberately has no ESPHome UART dependency and no transmit API.
// It can recognize which observed input carries Modbus read requests, latch a
// 30-second-idle preview gate, and produce request bytes for unit tests. The
// published preview firmware never passes those bytes to hardware.
namespace direct_mode_preview {

static constexpr uint32_t UART_IDLE_GATE_MS = 30000U;
static constexpr uint32_t PAIR_TIMEOUT_MS = 2000U;
static constexpr uint32_t MIN_DIRECTION_PAIRS = 4U;
static constexpr size_t MAX_STREAM_BYTES = 520U;
static constexpr size_t MAX_PENDING_READS = 8U;

inline uint16_t modbus_crc16(const uint8_t *data, size_t length) {
  uint16_t crc = 0xFFFFU;
  for (size_t index = 0; index < length; index++) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8U; bit++) {
      crc = (crc & 1U) ? static_cast<uint16_t>((crc >> 1U) ^ 0xA001U)
                       : static_cast<uint16_t>(crc >> 1U);
    }
  }
  return crc;
}

struct PendingRead {
  uint8_t source{0};
  uint8_t slave{0};
  uint8_t function{0};
  uint16_t start{0};
  uint16_t count{0};
  uint32_t timestamp_ms{0};
};

class DirectionDetector {
 public:
  void feed(uint8_t source, uint32_t timestamp_ms,
            const std::vector<uint8_t> &chunk) {
    if (source < 1U || source > 2U || chunk.empty()) return;
    this->expire_pending_(timestamp_ms);
    auto &stream = this->streams_[source - 1U];
    stream.insert(stream.end(), chunk.begin(), chunk.end());
    if (stream.size() > MAX_STREAM_BYTES) {
      const size_t remove = stream.size() - MAX_STREAM_BYTES;
      stream.erase(stream.begin(), stream.begin() + remove);
      this->discarded_bytes_ += static_cast<uint32_t>(remove);
    }
    this->process_stream_(source, timestamp_ms, stream);
  }

  bool detected() const {
    return this->request_source_ != 0U && this->response_source_ != 0U;
  }
  uint8_t request_source() const { return this->request_source_; }
  uint8_t response_source() const { return this->response_source_; }
  uint32_t paired_transactions() const {
    return this->detected()
               ? this->pair_votes_[this->request_source_ - 1U]
                                  [this->response_source_ - 1U]
               : std::max(this->pair_votes_[0][1], this->pair_votes_[1][0]);
  }
  uint32_t conflicting_pairs() const {
    return this->detected()
               ? this->pair_votes_[this->response_source_ - 1U]
                                  [this->request_source_ - 1U]
               : std::min(this->pair_votes_[0][1], this->pair_votes_[1][0]);
  }
  uint32_t request_frames() const { return this->request_frames_; }
  uint32_t response_frames() const { return this->response_frames_; }
  uint32_t unpaired_responses() const { return this->unpaired_responses_; }
  uint32_t discarded_bytes() const { return this->discarded_bytes_; }
  uint32_t last_pair_latency_ms() const { return this->last_pair_latency_ms_; }

 private:
  static bool crc_valid_(const std::vector<uint8_t> &bytes, size_t length) {
    if (bytes.size() < length || length < 4U) return false;
    const uint16_t expected = static_cast<uint16_t>(bytes[length - 2U]) |
                              (static_cast<uint16_t>(bytes[length - 1U]) << 8U);
    return modbus_crc16(bytes.data(), length - 2U) == expected;
  }

  static bool valid_read_request_(const std::vector<uint8_t> &bytes) {
    if (bytes.size() < 8U || bytes[0] < 1U || bytes[0] > 247U ||
        (bytes[1] != 0x03U && bytes[1] != 0x04U) ||
        !crc_valid_(bytes, 8U))
      return false;
    const uint16_t count =
        (static_cast<uint16_t>(bytes[4]) << 8U) | bytes[5];
    return count >= 1U && count <= 125U;
  }

  static size_t read_response_length_(const std::vector<uint8_t> &bytes) {
    if (bytes.size() < 3U) return 0U;
    const uint8_t byte_count = bytes[2];
    if (byte_count < 2U || (byte_count & 1U) != 0U || byte_count > 250U)
      return 0U;
    return static_cast<size_t>(5U + byte_count);
  }

  static bool valid_read_response_(const std::vector<uint8_t> &bytes,
                                   size_t length) {
    return length >= 7U && bytes.size() >= length && bytes[0] >= 1U &&
           bytes[0] <= 247U && (bytes[1] == 0x03U || bytes[1] == 0x04U) &&
           crc_valid_(bytes, length);
  }

  void process_stream_(uint8_t source, uint32_t timestamp_ms,
                       std::vector<uint8_t> &stream) {
    while (!stream.empty()) {
      if (stream.size() < 2U) return;
      if (stream[0] < 1U || stream[0] > 247U ||
          (stream[1] != 0x03U && stream[1] != 0x04U)) {
        stream.erase(stream.begin());
        this->discarded_bytes_++;
        continue;
      }

      // Eight bytes are required before deciding between the fixed-size read
      // request and a variable-size response. This avoids consuming the first
      // five bytes of a split request as a failed short response.
      if (stream.size() < 8U) return;
      if (valid_read_request_(stream)) {
        this->record_request_(source, timestamp_ms, stream);
        stream.erase(stream.begin(), stream.begin() + 8U);
        continue;
      }

      const size_t response_length = read_response_length_(stream);
      if (response_length == 0U) {
        stream.erase(stream.begin());
        this->discarded_bytes_++;
        continue;
      }
      if (stream.size() < response_length) return;
      if (!valid_read_response_(stream, response_length)) {
        stream.erase(stream.begin());
        this->discarded_bytes_++;
        continue;
      }
      this->record_response_(source, timestamp_ms, stream);
      stream.erase(stream.begin(), stream.begin() + response_length);
    }
  }

  void record_request_(uint8_t source, uint32_t timestamp_ms,
                       const std::vector<uint8_t> &frame) {
    PendingRead request{};
    request.source = source;
    request.slave = frame[0];
    request.function = frame[1];
    request.start = (static_cast<uint16_t>(frame[2]) << 8U) | frame[3];
    request.count = (static_cast<uint16_t>(frame[4]) << 8U) | frame[5];
    request.timestamp_ms = timestamp_ms;
    if (this->pending_.size() == MAX_PENDING_READS) this->pending_.pop_front();
    this->pending_.push_back(request);
    this->request_frames_++;
  }

  void record_response_(uint8_t source, uint32_t timestamp_ms,
                        const std::vector<uint8_t> &frame) {
    this->response_frames_++;
    const uint16_t count = frame[2] / 2U;
    auto match = std::find_if(
        this->pending_.begin(), this->pending_.end(),
        [source, count, &frame](const PendingRead &request) {
          return request.source != source && request.slave == frame[0] &&
                 request.function == frame[1] && request.count == count;
        });
    if (match == this->pending_.end()) {
      this->unpaired_responses_++;
      return;
    }
    const PendingRead request = *match;
    this->pending_.erase(match);
    this->last_pair_latency_ms_ = timestamp_ms - request.timestamp_ms;
    this->pair_votes_[request.source - 1U][source - 1U]++;
    this->update_direction_();
  }

  void expire_pending_(uint32_t timestamp_ms) {
    const auto expired = std::remove_if(
        this->pending_.begin(), this->pending_.end(),
        [timestamp_ms](const PendingRead &request) {
          return timestamp_ms - request.timestamp_ms > PAIR_TIMEOUT_MS;
        });
    this->pending_.erase(expired, this->pending_.end());
  }

  void update_direction_() {
    const uint32_t g1_to_g2 = this->pair_votes_[0][1];
    const uint32_t g2_to_g1 = this->pair_votes_[1][0];
    if (g1_to_g2 >= MIN_DIRECTION_PAIRS && g1_to_g2 >= g2_to_g1 + 2U) {
      this->request_source_ = 1U;
      this->response_source_ = 2U;
    } else if (g2_to_g1 >= MIN_DIRECTION_PAIRS &&
               g2_to_g1 >= g1_to_g2 + 2U) {
      this->request_source_ = 2U;
      this->response_source_ = 1U;
    }
  }

  std::array<std::vector<uint8_t>, 2> streams_{};
  std::deque<PendingRead> pending_{};
  std::array<std::array<uint32_t, 2>, 2> pair_votes_{};
  uint8_t request_source_{0};
  uint8_t response_source_{0};
  uint32_t request_frames_{0};
  uint32_t response_frames_{0};
  uint32_t unpaired_responses_{0};
  uint32_t discarded_bytes_{0};
  uint32_t last_pair_latency_ms_{0};
};

class DirectModeGate {
 public:
  void restore(bool requested, bool preview_ready, uint32_t now_ms) {
    this->requested_ = requested || preview_ready;
    this->preview_ready_ = preview_ready;
    this->last_uart_activity_ms_ = now_ms;
  }
  void arm_permanently() { this->requested_ = true; }
  void note_uart_activity(uint32_t now_ms) {
    this->last_uart_activity_ms_ = now_ms;
  }
  void tick(uint32_t now_ms) {
    if (this->requested_ && !this->preview_ready_ &&
        now_ms - this->last_uart_activity_ms_ >= UART_IDLE_GATE_MS) {
      this->preview_ready_ = true;
    }
  }
  bool requested() const { return this->requested_; }
  bool preview_ready() const { return this->preview_ready_; }
  uint32_t idle_ms(uint32_t now_ms) const {
    return now_ms - this->last_uart_activity_ms_;
  }
  std::string state() const {
    if (this->preview_ready_) return "preview_ready_no_tx_compiled";
    if (this->requested_) return "armed_waiting_for_30s_uart_idle";
    return "passive_observing";
  }

 private:
  bool requested_{false};
  bool preview_ready_{false};
  uint32_t last_uart_activity_ms_{0};
};

struct ReadRange {
  uint16_t start;
  uint16_t count;
};

static constexpr std::array<ReadRange, 4> OBSERVED_READ_SCHEDULE{{
    {0U, 61U},
    {100U, 51U},
    {201U, 16U},
    {151U, 50U},
}};

inline std::array<uint8_t, 8> make_fc03_request(uint8_t slave,
                                                uint16_t start,
                                                uint16_t count) {
  std::array<uint8_t, 8> frame{{
      slave,
      0x03U,
      static_cast<uint8_t>(start >> 8U),
      static_cast<uint8_t>(start & 0xFFU),
      static_cast<uint8_t>(count >> 8U),
      static_cast<uint8_t>(count & 0xFFU),
      0U,
      0U,
  }};
  const uint16_t crc = modbus_crc16(frame.data(), 6U);
  frame[6] = static_cast<uint8_t>(crc & 0xFFU);
  frame[7] = static_cast<uint8_t>(crc >> 8U);
  return frame;
}

class ReadOnlyPollPlanner {
 public:
  std::array<uint8_t, 8> current_request(uint8_t slave = 1U) const {
    const ReadRange range = OBSERVED_READ_SCHEDULE[this->index_];
    return make_fc03_request(slave, range.start, range.count);
  }
  ReadRange current_range() const { return OBSERVED_READ_SCHEDULE[this->index_]; }
  void advance() { this->index_ = (this->index_ + 1U) % OBSERVED_READ_SCHEDULE.size(); }
  size_t index() const { return this->index_; }

 private:
  size_t index_{0};
};

inline DirectionDetector direction_detector;
inline DirectModeGate direct_mode_gate;

}  // namespace direct_mode_preview
