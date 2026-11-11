// Tests for the P833 pulse decoder, run on a PC:
//   g++ -std=c++11 -Wall -Wextra -I firmware/p833_board firmware/test/test_decoder.cpp -o /tmp/td && /tmp/td
// By Ethan Brady & Ryan Raikes

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "p833_decoder.h"

using p833::Decoded;
using p833::Decoder;

struct Seg { bool on; uint32_t ms; };
struct Event { int result; int code; int press; std::string why; };

static int failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { failures++; std::printf("FAIL line %d: ", __LINE__); \
  std::printf(__VA_ARGS__); std::printf("\n"); } } while (0)

// Play segments into the decoder, sampling every `step` ms. Returns the events seen.
static std::vector<Event> play(const std::vector<Seg>& segs, uint32_t step = 1) {
  Decoder d;
  std::vector<Event> ev;
  uint32_t t = 0;
  Decoded out;
  d.feed(false, t, &out);
  for (const Seg& s : segs) {
    for (uint32_t i = 0; i < s.ms; i += step) {
      t += step;
      p833::Result r = d.feed(s.on, t, &out);
      if (r != p833::NOTHING) ev.push_back({(int)r, out.code, out.press, out.why});
    }
  }
  return ev;
}

// The PLC's message for code and press, with an optional jitter function (ms added to each segment).
static std::vector<Seg> message(int code, int press, int jitter = 0) {
  int digits[3] = {press / 100, (press / 10) % 10, press % 10};
  std::vector<Seg> s;
  s.push_back({true, (uint32_t)(500 + 500 * code + jitter)});
  for (int i = 0; i < 3; i++) {
    s.push_back({false, (uint32_t)(500 - jitter)});
    s.push_back({true, (uint32_t)(500 + 100 * digits[i] + jitter)});
  }
  return s;
}

static std::vector<Seg> cat(std::vector<Seg> a, const std::vector<Seg>& b) {
  a.insert(a.end(), b.begin(), b.end());
  return a;
}

static std::vector<Seg> idle(uint32_t ms) { return {{false, ms}}; }

static void expectCall(const std::vector<Event>& ev, int code, int press, const char* what) {
  bool ok = ev.size() == 1 && ev[0].result == p833::CALL && ev[0].code == code && ev[0].press == press;
  CHECK(ok, "%s: wanted call %d/%d, got %zu events%s%s", what, code, press, ev.size(),
        ev.empty() ? "" : " first: ", ev.empty() ? "" : (ev[0].result == p833::CALL ? "call" : ev[0].why.c_str()));
  if (ok) return;
  for (const Event& e : ev) std::printf("   event %d code %d press %d %s\n", e.result, e.code, e.press, e.why.c_str());
}

int main() {
  // The brief's example: Maintenance (code 2) at press 27 = 1500, 500, 700, 1200 ms.
  {
    std::vector<Seg> s = {{false, 1000}, {true, 1500}, {false, 500}, {true, 500}, {false, 500},
                          {true, 700}, {false, 500}, {true, 1200}, {false, 2000}};
    expectCall(play(s), 2, 27, "brief example");
  }
  // Every code, a spread of presses, and the PLC's 20 ms update jitter both ways.
  int n = 0;
  for (int code = 2; code <= 9; code++) {
    for (int press : {0, 1, 9, 10, 27, 99, 100, 451, 909, 999}) {
      for (int jitter : {-20, 0, 20}) {
        expectCall(play(cat(cat(idle(800), message(code, press, jitter)), idle(1500))), code, press, "sweep");
        n++;
      }
    }
  }
  // All presses 0..999 at code 5.
  for (int press = 0; press <= 999; press++) {
    expectCall(play(cat(cat(idle(300), message(5, press)), idle(1500))), 5, press, "all presses");
    n++;
  }
  // Sampling every 2 ms instead of 1 ms still works.
  expectCall(play(cat(cat(idle(500), message(7, 318)), idle(1500)), 2), 7, 318, "2 ms sampling");

  // Edges right at the tolerance: +/-38 ms on every pulse (gaps adjust).
  expectCall(play(cat(cat(idle(500), message(3, 64, 38)), idle(1500))), 3, 64, "+38 ms");
  expectCall(play(cat(cat(idle(500), message(3, 64, -38)), idle(1500))), 3, 64, "-38 ms");

  // Out of tolerance: a 1560 ms first pulse is neither code 2 (1500) nor anything else.
  {
    std::vector<Seg> s = {{false, 500}, {true, 1560}, {false, 500}, {true, 500}, {false, 500},
                          {true, 700}, {false, 500}, {true, 1200}, {false, 2000}};
    std::vector<Event> ev = play(s);
    CHECK(!ev.empty() && ev[0].result == p833::BAD, "bad code width should be rejected");
    for (const Event& e : ev) CHECK(e.result != p833::CALL, "no call from a bad message");
  }
  // A digit of 1450 ms (between 9 = 1400 and nothing) is rejected.
  {
    std::vector<Seg> s = message(2, 27);
    s[5].ms = 1450;
    std::vector<Event> ev = play(cat(cat(idle(500), s), idle(1500)));
    bool anyCall = false;
    for (const Event& e : ev) anyCall |= e.result == p833::CALL;
    CHECK(!anyCall && !ev.empty(), "bad digit width should be rejected");
  }
  // Partial message (PLC to program mode after 2 pulses), then a good one 5 s later.
  {
    std::vector<Seg> part = message(4, 512);
    part.resize(3);   // code pulse, gap, first digit
    std::vector<Event> ev = play(cat(cat(cat(idle(500), part), idle(5000)), cat(message(6, 33), idle(1500))));
    CHECK(ev.size() == 2 && ev[0].result == p833::BAD && ev[1].result == p833::CALL && ev[1].code == 6 &&
              ev[1].press == 33, "partial thrown away, next call still decoded (got %zu events)", ev.size());
  }
  // Glitches: 2 ms spikes while idle and 2 ms dropouts inside a pulse are ignored.
  {
    std::vector<Seg> s = {{false, 300}, {true, 2}, {false, 300}};
    std::vector<Seg> m = message(8, 120);
    std::vector<Seg> glitchy;
    for (size_t i = 0; i < m.size(); i++) {
      if (m[i].on && m[i].ms > 600) {   // split a long pulse with a 2 ms dropout
        glitchy.push_back({true, m[i].ms / 2});
        glitchy.push_back({false, 2});
        glitchy.push_back({true, m[i].ms - m[i].ms / 2 - 2});
      } else {
        glitchy.push_back(m[i]);
      }
    }
    expectCall(play(cat(cat(s, glitchy), idle(1500))), 8, 120, "glitches");
  }
  // Two calls back to back with the cell's 5 s spacing.
  {
    std::vector<Event> ev = play(cat(cat(cat(idle(500), message(9, 7)), idle(5000)), cat(message(8, 7), idle(1500))));
    CHECK(ev.size() == 2 && ev[0].code == 9 && ev[1].code == 8, "two calls in a row (got %zu)", ev.size());
  }
  // Input stuck on (wiring fault): reported once, and no call.
  {
    std::vector<Event> ev = play({{false, 500}, {true, 9000}, {false, 1000}});
    CHECK(ev.size() == 1 && ev[0].result == p833::BAD, "stuck input reported once (got %zu)", ev.size());
  }
  // Input already on at power-up: ignored until it goes off, then a normal call works.
  {
    Decoder d;
    Decoded out;
    uint32_t t = 0;
    d.feed(true, t, &out);
    int calls = 0, bad = 0;
    std::vector<Seg> s = cat(cat({{true, 2000}, {false, 1000}}, message(2, 5)), idle(1500));
    for (const Seg& g : s) for (uint32_t i = 0; i < g.ms; i++) {
      p833::Result r = d.feed(g.on, ++t, &out);
      calls += r == p833::CALL; bad += r == p833::BAD;
    }
    CHECK(calls == 1 && bad == 0, "on at power-up ignored (calls %d, bad %d)", calls, bad);
  }

  std::printf("%d message checks plus edge cases: %s\n", n, failures ? "FAILED" : "all passed");
  return failures ? 1 : 0;
}
