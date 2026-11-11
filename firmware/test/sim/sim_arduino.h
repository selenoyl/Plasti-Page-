// Stand-ins for the Arduino-ESP32 APIs the P833 firmware uses, so the real
// firmware source can be compiled and run on a PC for testing.
// By Ethan Brady & Ryan Raikes
//
// HTTP posts are real (plain sockets), so the simulated board talks to a real
// P833 receiver. The digital input and the serial console are scripted by the
// test (see sim_main.cpp). Not used on the real board.

#pragma once
#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using std::min;

// ---- time and pins
inline uint32_t millis() {
  static auto t0 = std::chrono::steady_clock::now();
  return (uint32_t)std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count();
}
inline void delay(uint32_t ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }
#define HIGH 1
#define LOW 0
#define INPUT_PULLUP 2
int sim_digital_read(int pin);           // provided by the test
inline void pinMode(int, int) {}
inline int digitalRead(int pin) { return sim_digital_read(pin); }
inline uint32_t esp_random() { return 0x5eed1234u; }
#define OUTPUT 3
#define ESP_ARDUINO_VERSION_MAJOR 3
inline void neopixelWrite(int, uint8_t, uint8_t, uint8_t) {}
inline void ledcAttach(int, int, int) {}
inline void ledcWrite(int, int) {}
#define F(x) (x)

// ---- String (the subset the firmware uses)
class String {
 public:
  String() {}
  String(const char* c) : s_(c ? c : "") {}
  String(const std::string& s) : s_(s) {}
  String(int v) : s_(std::to_string(v)) {}
  size_t length() const { return s_.size(); }
  const char* c_str() const { return s_.c_str(); }
  int indexOf(char c) const { size_t p = s_.find(c); return p == std::string::npos ? -1 : (int)p; }
  String substring(int a) const { return String(s_.substr(std::min((size_t)a, s_.size()))); }
  String substring(int a, int b) const {
    if (b < 0) b = 0;
    return String(s_.substr(std::min((size_t)a, s_.size()), (size_t)std::max(0, b - a)));
  }
  void trim() {
    size_t a = s_.find_first_not_of(" \t\r\n"), b = s_.find_last_not_of(" \t\r\n");
    s_ = a == std::string::npos ? "" : s_.substr(a, b - a + 1);
  }
  void toLowerCase() { for (auto& c : s_) c = (char)tolower(c); }
  long toInt() const { return atol(s_.c_str()); }
  char operator[](size_t i) const { return s_[i]; }
  String& operator+=(char c) { s_ += c; return *this; }
  String& operator+=(const String& o) { s_ += o.s_; return *this; }
  bool operator==(const char* o) const { return s_ == o; }
  bool operator==(const String& o) const { return s_ == o.s_; }
  bool operator!=(const char* o) const { return s_ != o; }
  friend String operator+(const String& a, const String& b) { return String(a.s_ + b.s_); }
  friend String operator+(const char* a, const String& b) { return String(std::string(a) + b.s_); }
  friend String operator+(const String& a, const char* b) { return String(a.s_ + b); }
  const std::string& str() const { return s_; }
 private:
  std::string s_;
};

// ---- Serial: output to stdout (prefixed), input from a scripted queue
class SimSerial {
 public:
  void begin(int) {}
  void printf(const char* fmt, ...) {
    va_list ap; va_start(ap, fmt); char b[512]; vsnprintf(b, sizeof(b), fmt, ap); va_end(ap);
    out(b);
  }
  void println(const char* s = "") { out(s); out("\n"); }
  void println(const String& s) { println(s.c_str()); }
  void print(const char* s) { out(s); }
  int available() { std::lock_guard<std::mutex> l(m_); return (int)in_.size(); }
  int read() { std::lock_guard<std::mutex> l(m_); if (in_.empty()) return -1; char c = in_.front(); in_.pop_front(); return c; }
  void type(const std::string& s) { std::lock_guard<std::mutex> l(m_); for (char c : s) in_.push_back(c); }
  std::string log() { std::lock_guard<std::mutex> l(m_); return log_; }
 private:
  void out(const char* s) {
    std::lock_guard<std::mutex> l(m_);
    log_ += s;
    for (const char* p = s; *p; p++) {
      if (bol_) { std::fputs("  [board] ", stdout); bol_ = false; }
      std::fputc(*p, stdout);
      if (*p == '\n') bol_ = true;
    }
    std::fflush(stdout);
  }
  std::mutex m_;
  std::deque<char> in_;
  std::string log_;
  bool bol_ = true;
};
extern SimSerial Serial;

struct SimEsp { void restart() { std::printf("[sim] ESP.restart()\n"); std::exit(3); } };
extern SimEsp ESP;

// ---- Preferences (kept in memory)
class Preferences {
 public:
  void begin(const char*, bool) {}
  String getString(const char* k, const String& d) { auto it = s().find(k); return it == s().end() ? d : String(it->second); }
  void putString(const char* k, const String& v) { s()[k] = v.str(); }
  int getInt(const char* k, int d) { auto it = s().find(k); return it == s().end() ? d : atoi(it->second.c_str()); }
  void putInt(const char* k, int v) { s()[k] = std::to_string(v); }
  static std::map<std::string, std::string>& s() { static std::map<std::string, std::string> m; return m; }
};

// ---- WiFi
#define WIFI_STA 1
#define WL_CONNECTED 3
#define WL_DISCONNECTED 6
class IPAddress {
 public:
  bool fromString(const String& s) { return inet_pton(AF_INET, s.c_str(), &a_) == 1; }
  String toString() const { char b[32]; inet_ntop(AF_INET, &a_, b, sizeof(b)); return String(b); }
  in_addr a_{};
};
class SimWiFi {
 public:
  std::atomic<bool> up{true};
  void mode(int) {}
  void setSleep(bool) {}
  void setAutoReconnect(bool) {}
  void config(IPAddress, IPAddress, IPAddress, IPAddress) {}
  void begin(const char*, const char*) {}
  void disconnect() {}
  void macAddress(uint8_t* m) { uint8_t v[6] = {0x24, 0x58, 0x7c, 0xab, 0xcd, 0xef}; memcpy(m, v, 6); }
  int status() { return up ? WL_CONNECTED : WL_DISCONNECTED; }
  IPAddress localIP() { IPAddress a; a.fromString("127.0.0.1"); return a; }
  int RSSI() { return -61; }
};
extern SimWiFi WiFi;

// ---- HTTPClient: a real HTTP/1.0 POST over a socket
class HTTPClient {
 public:
  void setTimeout(int ms) { timeout_ = ms; }
  void setConnectTimeout(int) {}
  bool begin(const String& url) {
    std::string u = url.str();
    if (u.compare(0, 7, "http://") != 0) return false;
    u = u.substr(7);
    size_t slash = u.find('/');
    std::string hostport = u.substr(0, slash);
    path_ = slash == std::string::npos ? "/" : u.substr(slash);
    size_t colon = hostport.find(':');
    host_ = hostport.substr(0, colon);
    port_ = colon == std::string::npos ? 80 : atoi(hostport.substr(colon + 1).c_str());
    headers_.clear();
    return true;
  }
  void addHeader(const char* k, const String& v) { headers_ += std::string(k) + ": " + v.str() + "\r\n"; }
  int POST(const String& body) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    timeval tv{timeout_ / 1000, (timeout_ % 1000) * 1000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)port_);
    inet_pton(AF_INET, host_.c_str(), &a.sin_addr);
    if (connect(fd, (sockaddr*)&a, sizeof(a)) != 0) { close(fd); return -1; }
    std::string req = "POST " + path_ + " HTTP/1.0\r\nHost: " + host_ + "\r\n" + headers_ +
                      "Content-Length: " + std::to_string(body.length()) + "\r\n\r\n" + body.str();
    if (send(fd, req.data(), req.size(), 0) < 0) { close(fd); return -1; }
    char buf[64] = {0};
    ssize_t n = recv(fd, buf, sizeof(buf) - 1, 0);
    close(fd);
    if (n < 12) return -1;
    return atoi(buf + 9);   // "HTTP/1.0 200 OK"
  }
  void end() {}
 private:
  std::string host_, path_, headers_;
  int port_ = 80;
  int timeout_ = 4000;
};

// ---- FreeRTOS (threads and a simple queue)
typedef uint32_t TickType_t;
#define pdTRUE 1
struct SimQueue { std::mutex m; std::deque<std::string> q; size_t item, cap; };
typedef SimQueue* QueueHandle_t;
inline QueueHandle_t xQueueCreate(size_t n, size_t item) { auto* q = new SimQueue; q->item = item; q->cap = n; return q; }
inline int xQueueSend(QueueHandle_t q, const void* p, int) {
  std::lock_guard<std::mutex> l(q->m);
  if (q->q.size() >= q->cap) return 0;
  q->q.push_back(std::string((const char*)p, q->item));
  return 1;
}
inline int xQueueReceive(QueueHandle_t q, void* p, int) {
  std::lock_guard<std::mutex> l(q->m);
  if (q->q.empty()) return 0;
  memcpy(p, q->q.front().data(), q->item);
  q->q.pop_front();
  return pdTRUE;
}
inline TickType_t xTaskGetTickCount() { return millis(); }
inline void vTaskDelayUntil(TickType_t* last, TickType_t inc) {
  *last += inc;
  while (millis() < *last) std::this_thread::sleep_for(std::chrono::microseconds(200));
}
inline void xTaskCreatePinnedToCore(void (*fn)(void*), const char*, int, void* arg, int, void*, int) {
  std::thread(fn, arg).detach();
}
