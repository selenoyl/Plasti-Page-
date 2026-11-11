// P833 board firmware - pulse decoder (plain C++, no Arduino, so it can be
// tested on a PC: see firmware/test/test_decoder.cpp).
// By Ethan Brady & Ryan Raikes
//
// The PLC sends one call as four 24 V pulses with 500 ms off between them:
//   pulse 1 = the code:            500 ms + 500 ms x code   (code 2..9)
//   pulses 2-4 = press digits:      500 ms + 100 ms x digit  (hundreds, tens, ones)
// Example, code 2 at press 27: 1500, 500, 700, 1200 ms.
// Each width is accepted within +/-40 ms. The PLC drops the output mid-message
// on program mode or a fault; a partial message is thrown away.

#pragma once
#include <stdint.h>

namespace p833 {

const uint32_t WIDTH_TOL_MS = 40;     // spec: each width within 40 ms
const uint32_t GAP_MS = 500;          // off time between pulses
const uint32_t GAP_TOL_MS = 120;      // gaps carry no data; be a little lenient
const uint32_t PARTIAL_TIMEOUT_MS = GAP_MS + 400;  // off this long mid-message = give up
const uint32_t STUCK_ON_MS = 6000;    // longer than any valid pulse (code 9 = 5000)
const uint32_t DEBOUNCE_MS = 5;       // input must be steady this long to count

enum Result { NOTHING = 0, CALL = 1, BAD = 2 };

struct Decoded {
  int code;
  int press;
  const char* why;   // reason for BAD, for the serial log
};

// Width -> value: returns -1 if the width isn't base + step * n (n in lo..hi) within the tolerance.
inline int widthValue(uint32_t w, uint32_t step, int lo, int hi) {
  for (int n = lo; n <= hi; n++) {
    uint32_t want = 500 + step * (uint32_t)n;
    uint32_t diff = w > want ? w - want : want - w;
    if (diff <= WIDTH_TOL_MS) return n;
  }
  return -1;
}

class Decoder {
 public:
  Decoder() { reset(); }

  void reset() {
    stable_ = false;
    rawLevel_ = false;
    rawSince_ = 0;
    started_ = false;
    count_ = 0;
    pulseStart_ = 0;
    lastFall_ = 0;
    waitIdle_ = false;
  }

  bool level() const { return stable_; }
  bool busy() const { return count_ > 0 || stable_; }

  // Call often (every 1-2 ms) with the input state (true = PLC output on).
  Result feed(bool on, uint32_t now, Decoded* out) {
    out->code = 0; out->press = 0; out->why = "";
    if (!started_) { started_ = true; rawLevel_ = on; rawSince_ = now; stable_ = on;
                     waitIdle_ = on; return NOTHING; }

    // Debounce: the level must hold for DEBOUNCE_MS. The edge time is when it first changed.
    if (on != rawLevel_) { rawLevel_ = on; rawSince_ = now; }
    if (rawLevel_ != stable_ && now - rawSince_ >= DEBOUNCE_MS) {
      stable_ = rawLevel_;
      return stable_ ? rise(rawSince_, out) : fall(rawSince_, out);
    }

    // Timeouts while nothing changes.
    if (stable_ && !waitIdle_ && count_ < 4 && now - pulseStart_ > STUCK_ON_MS) {
      return fail("input on too long (over 6 s)", out);
    }
    if (!stable_ && count_ > 0 && now - lastFall_ > PARTIAL_TIMEOUT_MS) {
      return fail("message stopped part way (partial message thrown away)", out);
    }
    return NOTHING;
  }

 private:
  Result rise(uint32_t t, Decoded* out) {
    if (waitIdle_) return NOTHING;
    if (count_ > 0) {
      uint32_t gap = t - lastFall_;
      if (gap + GAP_TOL_MS < GAP_MS || gap > GAP_MS + GAP_TOL_MS) {
        Result r = fail("gap between pulses out of range", out);
        waitIdle_ = false;          // this rise may be the start of a fresh message
        pulseStart_ = t;
        return r;
      }
    }
    pulseStart_ = t;
    return NOTHING;
  }

  Result fall(uint32_t t, Decoded* out) {
    if (waitIdle_) { waitIdle_ = false; return NOTHING; }
    uint32_t w = t - pulseStart_;
    lastFall_ = t;
    if (count_ == 0) {
      int c = widthValue(w, 500, 2, 9);
      if (c < 0) return fail("first pulse is not a valid code width", out);
      code_ = c;
    } else {
      int d = widthValue(w, 100, 0, 9);
      if (d < 0) return fail("digit pulse is not a valid width", out);
      digits_[count_ - 1] = d;
    }
    count_++;
    if (count_ == 4) {
      out->code = code_;
      out->press = digits_[0] * 100 + digits_[1] * 10 + digits_[2];
      count_ = 0;
      return CALL;
    }
    return NOTHING;
  }

  Result fail(const char* why, Decoded* out) {
    out->why = why;
    count_ = 0;
    waitIdle_ = stable_;   // if the input is on now, ignore it until it goes off
    return BAD;
  }

  bool started_;
  bool stable_;
  bool rawLevel_;
  uint32_t rawSince_;
  int count_;
  int code_;
  int digits_[3];
  uint32_t pulseStart_;
  uint32_t lastFall_;
  bool waitIdle_;
};

}  // namespace p833
