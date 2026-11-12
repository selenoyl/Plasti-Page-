// PC test of the board's firmware-update page (POST /update): which requests may
// restart the board. Built and run by firmware/test/run_update_test.py.
// By Ethan Brady & Ryan Raikes
//
// Usage: test_update <case>. Exit code 3 = the board restarted (ESP.restart()),
// 0 = it did not. The script checks each case against what it should do.

#include "sim_arduino.h"

SimSerial Serial;
SimEsp ESP;
SimWiFi WiFi;
UpdateClass Update;

#include "../../p833_board/p833_board.ino"

int sim_digital_read(int) { return HIGH; }      // the PLC input stays idle in this test

static void uploadFile() {
  HTTPUpload& up = web.upload();
  up.filename = "p833_board.ino.bin";
  up.status = UPLOAD_FILE_START; webUpdateUpload();
  up.status = UPLOAD_FILE_WRITE; up.currentSize = 512; webUpdateUpload();
  up.status = UPLOAD_FILE_END; webUpdateUpload();
}

int main(int argc, char** argv) {
  std::string t = argc > 1 ? argv[1] : "";
  cfg.otapass = "update-pass-1";
  web.simHost = "192.168.83.30";
  if (t == "post-no-file-no-password") {           // must NOT restart (was the bug)
    web.simAuthOk = false; webUpdateDone();
  } else if (t == "post-no-file-with-password") {  // must NOT restart: no firmware came
    web.simAuthOk = true; webUpdateDone();
  } else if (t == "updates-off") {                 // otapass empty: must NOT restart
    cfg.otapass = ""; web.simAuthOk = true; uploadFile(); webUpdateDone();
  } else if (t == "wrong-password-upload") {       // must NOT restart
    web.simAuthOk = false; web.simHasAuthHeader = true; uploadFile(); webUpdateDone();
  } else if (t == "other-site") {                  // a page on another site posts: must NOT restart
    web.simAuthOk = true; web.simOrigin = "http://evil.example"; uploadFile(); webUpdateDone();
  } else if (t == "lockout") {                     // 5 wrong passwords, then even the right one waits
    web.simHasAuthHeader = true;
    for (int i = 0; i < 5; i++) { web.simAuthOk = false; uploadFile(); webUpdateDone(); }
    web.simAuthOk = true; uploadFile(); webUpdateDone();
  } else if (t == "raw-post") {                    // curl -X POST, or a text/plain form: no crash
    web.simAuthOk = false; web.simRaw = true; web.simContentType = "text/plain";
    webUpdateUpload(); webUpdateDone();
  } else if (t == "raw-post-no-type") {            // a POST with no Content-Type at all
    web.simRaw = true; web.simContentType = "";
    webUpdateUpload(); webUpdateDone();
  } else if (t == "good-upload") {                 // right password, whole file: restarts
    web.simAuthOk = true; web.simOrigin = "http://192.168.83.30"; uploadFile(); webUpdateDone();
  } else if (t == "good-upload-no-origin") {       // e.g. curl: restarts
    web.simAuthOk = true; uploadFile(); webUpdateDone();
  } else {
    std::printf("unknown case\n");
    return 2;
  }
  std::printf("[sim] last reply %d\n", web.simLastCode);
  return 0;
}
