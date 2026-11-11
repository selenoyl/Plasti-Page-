// P833 cell board firmware - Waveshare ESP32-S3-ETH-8DI-8RO
// By Ethan Brady & Ryan Raikes
//
// Reads the PLC's call-for-help pulses on one digital input, decodes them
// (see p833_decoder.h) and sends each call to the P833 receiver laptop over
// Wi-Fi. Calls wait in a queue and are resent until the laptop answers OK;
// the laptop drops repeats. A heartbeat goes out every 60 s.
//
// Setup is over USB (Serial Monitor, 115200 baud, line ending "Newline"):
// type "help". Settings are kept in the board's flash. One firmware for every
// board; each board gets its own ID ("id RSP_LH").
//
// Arduino IDE: board "ESP32S3 Dev Module", esp32 core 3.0.0 or newer (Waveshare's
// requirement), Tools > USB CDC On Boot: Enabled. No extra libraries.

#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_system.h>

#include "p833_decoder.h"

#if !defined(ESP_ARDUINO_VERSION_MAJOR) || ESP_ARDUINO_VERSION_MAJOR < 3
#error "Needs the esp32 board package 3.0.0 or newer (Arduino IDE: Tools > Board > Boards Manager > esp32)"
#endif

#define FW_VERSION "1.0.0"

// ---- Board pins (Waveshare wiki and demo code for the ESP32-S3-ETH-8DI-8RO):
//   DI1..DI8 = GPIO4..GPIO11, optocoupler inputs read with the pull-up on;
//   an energised input reads LOW (the demo's default "DIN_Inverse_Enable 1").
//   WS2812 RGB LED = GPIO38 (green, red, blue order), passive buzzer = GPIO46.
static const int DEFAULT_DI_PIN = 4;       // DI1
static const int LED_PIN = 38;
static const int BUZZER_PIN = 46;
static const char* DEFAULT_SERVER = "192.168.83.10:8833";

static const uint32_t HEARTBEAT_MS = 60000;
static const int MAX_QUEUE = 10;

Preferences prefs;
p833::Decoder decoder;

struct Settings {
  String id, ssid, pass, server, key, ip, gw, mask, dns, active, beep;
  int diPin;
} cfg;

struct PendingCall {
  uint32_t seq;
  int code;
  int press;
  uint32_t decodedAt;
};

// Calls decoded by the sampler task, waiting to be sent by loop().
QueueHandle_t decodedQ;
QueueHandle_t logQ;
PendingCall pending[MAX_QUEUE];
int pendingCount = 0;
uint32_t nextSeq = 1;
char bootId[9];
uint32_t nextTryAt = 0, retryDelay = 2000, lastHeartbeat = 0, wifiLostAt = 0;
bool activeHigh = false;
volatile bool watching = false;
String lastError = "";

struct LogMsg { char text[96]; };

void queueCall(int code, int press, const char* why);

void logEvent(const char* fmt, ...) {
  LogMsg m;
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(m.text, sizeof(m.text), fmt, ap);
  va_end(ap);
  xQueueSend(logQ, &m, 0);
}

// --------------------------------------------------------------- settings ---

String getS(const char* k, const String& def) { return prefs.getString(k, def); }

void loadSettings() {
  prefs.begin("p833", false);
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char defId[20];
  snprintf(defId, sizeof(defId), "P833-%02X%02X%02X", mac[3], mac[4], mac[5]);
  cfg.id = getS("id", defId);
  cfg.ssid = getS("ssid", "");
  cfg.pass = getS("pass", "");
  cfg.server = getS("server", DEFAULT_SERVER);
  cfg.key = getS("key", "");
  cfg.ip = getS("ip", "dhcp");
  cfg.gw = getS("gw", "");
  cfg.mask = getS("mask", "255.255.255.0");
  cfg.dns = getS("dns", "");
  cfg.active = getS("active", "low");
  cfg.beep = getS("beep", "on");
  cfg.diPin = prefs.getInt("dipin", DEFAULT_DI_PIN);
}

void showSettings() {
  Serial.printf("\nP833 board firmware %s\n", FW_VERSION);
  Serial.printf("  id      %s\n", cfg.id.c_str());
  Serial.printf("  ssid    %s\n", cfg.ssid.length() ? cfg.ssid.c_str() : "(not set)");
  Serial.printf("  pass    %s\n", cfg.pass.length() ? "(set)" : "(not set)");
  Serial.printf("  server  %s\n", cfg.server.c_str());
  Serial.printf("  key     %s\n", cfg.key.length() ? "(set)" : "(not set)");
  Serial.printf("  ip      %s  gw %s  mask %s  dns %s\n", cfg.ip.c_str(), cfg.gw.c_str(), cfg.mask.c_str(),
                cfg.dns.c_str());
  Serial.printf("  dipin   GPIO %d   active %s (now using: on = %s)   beep %s\n", cfg.diPin,
                cfg.active.c_str(), activeHigh ? "HIGH" : "LOW", cfg.beep.c_str());
  Serial.printf("  wifi    %s", WiFi.status() == WL_CONNECTED ? "connected" : "NOT connected");
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf(", %s, %d dBm", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  Serial.printf("\n  queue   %d call(s) waiting   last error: %s\n\n", pendingCount,
                lastError.length() ? lastError.c_str() : "none");
}

void printHelp() {
  Serial.println(F(
      "\nCommands (settings are saved; 'reboot' to apply network changes):\n"
      "  show                  settings and status\n"
      "  id RSP_LH             this board's ID (must match the receiver's board list)\n"
      "  ssid <name>           Wi-Fi network name (spaces allowed)\n"
      "  pass <password>       Wi-Fi password\n"
      "  server 192.168.83.10:8833   receiver address\n"
      "  key <board key>       from /etc/p833-receiver/secrets.env on the laptop\n"
      "  ip dhcp | ip 192.168.83.101   fixed address (then set gw, mask, dns)\n"
      "  gw / mask / dns <address>\n"
      "  dipin <gpio>          GPIO of the input the PLC is wired to (DI1)\n"
      "  active low|high|auto  input level that means PLC output ON (this board: low)\n"
      "  beep on|off           chirp when a call is decoded\n"
      "  watch                 show the input live (any key to stop)\n"
      "  test <code> <press>   send a test call, e.g. test 2 27\n"
      "  heartbeat             send a heartbeat now\n"
      "  reboot\n"));
}

void saveS(const char* k, String& field, const String& v) {
  field = v;
  prefs.putString(k, v);
  Serial.printf("saved %s\n", k);
}

void handleCommand(String line) {
  line.trim();
  if (!line.length()) return;
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  arg.trim();
  cmd.toLowerCase();

  if (cmd == "help" || cmd == "?") printHelp();
  else if (cmd == "show") showSettings();
  else if (cmd == "id") {
    bool ok = arg.length() > 0 && arg.length() <= 32;
    for (size_t i = 0; i < arg.length(); i++) ok &= isalnum(arg[i]) || arg[i] == '_' || arg[i] == '-';
    if (ok) saveS("id", cfg.id, arg);
    else Serial.println("id: 1-32 letters, numbers, _ or -");
  } else if (cmd == "ssid") saveS("ssid", cfg.ssid, arg);
  else if (cmd == "pass") saveS("pass", cfg.pass, arg);
  else if (cmd == "server") saveS("server", cfg.server, arg);
  else if (cmd == "key") saveS("key", cfg.key, arg);
  else if (cmd == "ip") saveS("ip", cfg.ip, arg.length() ? arg : String("dhcp"));
  else if (cmd == "gw") saveS("gw", cfg.gw, arg);
  else if (cmd == "mask") saveS("mask", cfg.mask, arg);
  else if (cmd == "dns") saveS("dns", cfg.dns, arg);
  else if (cmd == "dipin") {
    int p = arg.toInt();
    if (p > 0 && p < 49) { cfg.diPin = p; prefs.putInt("dipin", p); Serial.println("saved dipin; reboot to use it"); }
    else Serial.println("dipin: a GPIO number");
  } else if (cmd == "active") {
    if (arg == "auto" || arg == "high" || arg == "low") { saveS("active", cfg.active, arg); Serial.println("reboot to use it"); }
    else Serial.println("active: auto, high or low");
  } else if (cmd == "beep") {
    if (arg == "on" || arg == "off") saveS("beep", cfg.beep, arg);
    else Serial.println("beep: on or off");
  } else if (cmd == "watch") {
    Serial.printf("Watching GPIO %d (on = %s). Turn the PLC output on and off. Any key stops.\n", cfg.diPin,
                  activeHigh ? "HIGH" : "LOW");
    watching = true;
  } else if (cmd == "test") {
    int s2 = arg.indexOf(' ');
    int code = arg.substring(0, s2).toInt(), press = s2 < 0 ? -1 : arg.substring(s2 + 1).toInt();
    if (code >= 2 && code <= 9 && press >= 0 && press <= 999) queueCall(code, press, "test from serial");
    else Serial.println("test <code 2-9> <press 0-999>");
  } else if (cmd == "heartbeat") lastHeartbeat = 0;
  else if (cmd == "reboot") { Serial.println("rebooting"); delay(200); ESP.restart(); }
  else Serial.println("unknown command; type help");
}

// ---------------------------------------------------------------- sampler ---
// Runs every 1 ms on its own, so a slow network send never makes it miss a pulse.

void samplerTask(void*) {
  TickType_t last = xTaskGetTickCount();
  bool lastRaw = false;
  for (;;) {
    bool raw = digitalRead(cfg.diPin) == HIGH;
    bool on = activeHigh ? raw : !raw;
    if (watching && on != lastRaw) logEvent("input %s", on ? "ON" : "off");
    lastRaw = on;
    p833::Decoded d;
    p833::Result r = decoder.feed(on, millis(), &d);
    if (r == p833::CALL) {
      PendingCall c = {0, d.code, d.press, millis()};
      xQueueSend(decodedQ, &c, 0);
    } else if (r == p833::BAD) {
      logEvent("ignored pulses: %s", d.why);
    }
    vTaskDelayUntil(&last, 1);
  }
}

// Decide which level means "on". auto: the PLC output is normally off, so the
// level seen at power-up is "off".
void choosePolarity() {
  if (cfg.active == "high") activeHigh = true;
  else if (cfg.active == "low") activeHigh = false;
  else {
    int highs = 0;
    for (int i = 0; i < 200; i++) { highs += digitalRead(cfg.diPin) == HIGH; delay(2); }
    activeHigh = highs < 100;    // idle low -> on is high
    Serial.printf("input idle level is %s, so ON = %s (set 'active high/low' to fix it)\n",
                  highs < 100 ? "LOW" : "HIGH", activeHigh ? "HIGH" : "LOW");
  }
}

// ------------------------------------------------------------ LED / buzzer ---

uint32_t beepUntil = 0;

void led(uint8_t r, uint8_t g, uint8_t b) { neopixelWrite(LED_PIN, g, r, b); }   // this LED takes G, R, B

void beep(uint32_t ms) {
  if (cfg.beep != "on") return;
  ledcWrite(BUZZER_PIN, 128);
  beepUntil = millis() + ms;
}

// Status: blue = pulses coming in; yellow blink = call waiting to send;
// red blink = no Wi-Fi or an error; green = all OK.
void updateStatus() {
  static uint32_t last = 0;
  if (beepUntil && millis() > beepUntil) { ledcWrite(BUZZER_PIN, 0); beepUntil = 0; }
  if (millis() - last < 100) return;
  last = millis();
  bool blink = (millis() / 400) % 2;
  if (decoder.busy()) led(0, 0, 40);
  else if (pendingCount) led(blink ? 40 : 0, blink ? 25 : 0, 0);
  else if (!cfg.ssid.length() || WiFi.status() != WL_CONNECTED || lastError.length()) led(blink ? 40 : 0, 0, 0);
  else led(0, 12, 0);
}

// ---------------------------------------------------------------- network ---

void startWifi() {
  if (!cfg.ssid.length()) { Serial.println("Wi-Fi not set: use 'ssid' and 'pass', then 'reboot'"); return; }
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);              // lower latency, steadier link
  WiFi.setAutoReconnect(true);
  if (cfg.ip != "dhcp") {
    IPAddress ip, gw, mask, dns;
    if (ip.fromString(cfg.ip) && gw.fromString(cfg.gw.length() ? cfg.gw : cfg.ip) && mask.fromString(cfg.mask)) {
      if (!dns.fromString(cfg.dns)) dns = gw;
      WiFi.config(ip, gw, mask, dns);
    } else {
      Serial.println("fixed IP settings are not valid; using DHCP");
    }
  }
  WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
  Serial.printf("Wi-Fi: joining %s\n", cfg.ssid.c_str());
}

const int NO_WIFI = -100;

int post(const char* path, const String& body) {
  if (WiFi.status() != WL_CONNECTED) return NO_WIFI;
  HTTPClient http;
  http.setTimeout(4000);
  http.setConnectTimeout(3000);
  String url = "http://" + cfg.server + path;
  if (!http.begin(url)) return -2;
  http.addHeader("Content-Type", "application/json");
  if (cfg.key.length()) http.addHeader("X-P833-Key", cfg.key);
  int status = http.POST(body);
  http.end();
  return status;
}

void queueCall(int code, int press, const char* why) {
  if (pendingCount == MAX_QUEUE) {
    Serial.println("QUEUE FULL: oldest call dropped");
    memmove(&pending[0], &pending[1], sizeof(PendingCall) * (MAX_QUEUE - 1));
    pendingCount--;
  }
  PendingCall c = {nextSeq++, code, press, millis()};
  pending[pendingCount++] = c;
  Serial.printf("CALL code %d press %d (%s) queued, seq %lu\n", code, press, why, (unsigned long)c.seq);
  beep(120);
  nextTryAt = 0;
  retryDelay = 2000;
}

void sendPending() {
  if (!pendingCount || millis() < nextTryAt) return;
  PendingCall& c = pending[0];
  char body[200];
  snprintf(body, sizeof(body),
           "{\"board\":\"%s\",\"boot\":\"%s\",\"seq\":%lu,\"code\":%d,\"press\":%d,\"age_ms\":%lu}",
           cfg.id.c_str(), bootId, (unsigned long)c.seq, c.code, c.press, (unsigned long)(millis() - c.decodedAt));
  int st = post("/call", String(body));
  if (st == 200) {
    Serial.printf("sent: code %d press %d (seq %lu)\n", c.code, c.press, (unsigned long)c.seq);
    memmove(&pending[0], &pending[1], sizeof(PendingCall) * (pendingCount - 1));
    pendingCount--;
    lastError = "";
    retryDelay = 2000;
    nextTryAt = 0;
    return;
  }
  if (st == 400) {   // the receiver says the message itself is wrong: resending won't help
    lastError = "receiver refused the call (400)";
    Serial.printf("DROPPED: receiver refused code %d press %d (400)\n", c.code, c.press);
    memmove(&pending[0], &pending[1], sizeof(PendingCall) * (pendingCount - 1));
    pendingCount--;
    return;
  }
  lastError = st == 403 ? "wrong board key (403)" : st == NO_WIFI ? "no Wi-Fi"
                                                  : "receiver not reachable (" + String(st) + ")";
  Serial.printf("send failed: %s; will retry in %lu s (%d waiting)\n", lastError.c_str(),
                (unsigned long)(retryDelay / 1000), pendingCount);
  nextTryAt = millis() + retryDelay;
  retryDelay = min<uint32_t>(retryDelay * 2, st == 403 ? 60000 : 10000);
}

void heartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (lastHeartbeat && millis() - lastHeartbeat < HEARTBEAT_MS) return;
  lastHeartbeat = millis() | 1;
  char body[200];
  snprintf(body, sizeof(body),
           "{\"board\":\"%s\",\"boot\":\"%s\",\"fw\":\"%s\",\"rssi\":%d,\"uptime_s\":%lu,\"pending\":%d}",
           cfg.id.c_str(), bootId, FW_VERSION, WiFi.RSSI(), (unsigned long)(millis() / 1000), pendingCount);
  int st = post("/heartbeat", String(body));
  if (st != 200) {
    Serial.printf("heartbeat failed (%d); trying again in 10 s\n", st);
    lastHeartbeat = (millis() - HEARTBEAT_MS + 10000) | 1;
  }
}

// ----------------------------------------------------------- setup / loop ---

String serialLine;

void setup() {
  Serial.begin(115200);
  delay(1500);
  snprintf(bootId, sizeof(bootId), "%08lx", (unsigned long)esp_random());
  loadSettings();
  pinMode(cfg.diPin, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  ledcAttach(BUZZER_PIN, 2000, 8);
  ledcWrite(BUZZER_PIN, 0);
  led(0, 0, 40);
  choosePolarity();
  decodedQ = xQueueCreate(MAX_QUEUE, sizeof(PendingCall));
  logQ = xQueueCreate(16, sizeof(LogMsg));
  xTaskCreatePinnedToCore(samplerTask, "sampler", 4096, nullptr, 5, nullptr, 1);
  showSettings();
  Serial.println("Type 'help' for setup commands.");
  startWifi();
}

void loop() {
  PendingCall c;
  while (xQueueReceive(decodedQ, &c, 0) == pdTRUE) queueCall(c.code, c.press, "from PLC");
  LogMsg m;
  while (xQueueReceive(logQ, &m, 0) == pdTRUE) Serial.println(m.text);

  while (Serial.available()) {
    char ch = Serial.read();
    if (watching) { watching = false; Serial.println("watch stopped"); serialLine = ""; continue; }
    if (ch == '\n' || ch == '\r') { if (serialLine.length()) handleCommand(serialLine); serialLine = ""; }
    else if (serialLine.length() < 200) serialLine += ch;
  }

  if (cfg.ssid.length()) {
    if (WiFi.status() == WL_CONNECTED) {
      if (wifiLostAt) { Serial.printf("Wi-Fi connected: %s\n", WiFi.localIP().toString().c_str()); wifiLostAt = 0; }
      sendPending();
      heartbeat();
    } else {
      if (!wifiLostAt) { wifiLostAt = millis() | 1; Serial.println("Wi-Fi not connected"); }
      // Kick the connection if it has been down a while (auto-reconnect sometimes stalls).
      if (millis() - wifiLostAt > 30000) { WiFi.disconnect(); WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
                                           wifiLostAt = millis() | 1; }
    }
  }
  updateStatus();
  delay(5);
}
