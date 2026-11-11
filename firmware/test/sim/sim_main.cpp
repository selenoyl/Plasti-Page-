// Runs the real P833 board firmware on a PC with a scripted PLC input, a
// scripted serial console and a Wi-Fi drop, talking to a real receiver.
// Built and driven by firmware/test/run_sim_test.py.
// By Ethan Brady & Ryan Raikes
//
// Usage: sim_board <receiver port> <seconds to run>

#include "sim_arduino.h"

SimSerial Serial;
SimEsp ESP;
SimWiFi WiFi;

#include "../../p833_board/p833_board.ino"

// The board's input reads HIGH when idle and LOW when the PLC output is on
// (an opto input with a pull-up). The firmware has to work this out itself
// ("active auto").
struct Pulse { uint32_t on, off; };
static std::vector<Pulse> pulses;
static std::mutex pulsesM;

int sim_digital_read(int) {
  uint32_t t = millis();
  std::lock_guard<std::mutex> l(pulsesM);
  for (const Pulse& p : pulses) if (t >= p.on && t < p.off) return LOW;
  return HIGH;
}

// The PLC message for code/press starting at t, as the brief describes it.
static void plcSends(uint32_t t, int code, int press, int pulsesToSend = 4) {
  int w[4] = {500 + 500 * code, 500 + 100 * (press / 100), 500 + 100 * ((press / 10) % 10), 500 + 100 * (press % 10)};
  std::lock_guard<std::mutex> l(pulsesM);
  for (int i = 0; i < pulsesToSend; i++) {
    pulses.push_back({t, t + (uint32_t)w[i]});
    t += w[i] + 500;
  }
}

int main(int argc, char** argv) {
  int port = argc > 1 ? atoi(argv[1]) : 18833;
  uint32_t runFor = (argc > 2 ? atoi(argv[2]) : 45) * 1000;

  // PLC timeline (ms from start)
  plcSends(2000, 2, 27);        // receiver not up yet: must be retried
  plcSends(15000, 9, 42);       // during the Wi-Fi drop: must wait in the queue
  plcSends(32000, 4, 512, 2);   // PLC stops part way: must be thrown away

  setup();
  char server[64];
  snprintf(server, sizeof(server), "server 127.0.0.1:%d\n", port);
  Serial.type(std::string("id RSP_LH\nkey sim-key\nssid TestNet\npass secret\n") + server + "show\n");

  bool typedTest = false, typedHb = false, droppedWifi = false, restoredWifi = false;
  while (millis() < runFor) {
    uint32_t t = millis();
    if (!typedTest && t > 12000) { Serial.type("test 5 63\n"); typedTest = true; }
    if (!droppedWifi && t > 14000) { WiFi.up = false; droppedWifi = true; std::printf("[sim] Wi-Fi DOWN\n"); }
    if (!restoredWifi && t > 30000) { WiFi.up = true; restoredWifi = true; std::printf("[sim] Wi-Fi UP\n"); }
    if (!typedHb && t > 38000) { Serial.type("heartbeat\n"); typedHb = true; }
    loop();
  }
  std::printf("[sim] done\n");
  return 0;
}
