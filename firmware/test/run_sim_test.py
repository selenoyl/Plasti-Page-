#!/usr/bin/env python3
# End-to-end test of the board firmware on a PC (no hardware needed):
# builds the real firmware against stand-in ESP32 libraries, plays the PLC's
# pulses into its input, and checks the calls arrive at a real P833 receiver.
#
#   python3 firmware/test/run_sim_test.py
#
# Scenario (seconds from start):
#   2   PLC sends code 2, press 27 - the receiver is NOT running yet, so the
#       board must keep the call and retry
#   9   receiver starts
#   12  "test 5 63" typed on the serial console
#   14  Wi-Fi drops
#   15  PLC sends code 9, press 42 - must wait in the queue
#   30  Wi-Fi back - the queued call must go out
#   32  PLC stops part way through a message - must be thrown away
#   38  "heartbeat" typed - the receiver must show the board as heard
# By Ethan Brady & Ryan Raikes

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PORT = 18833


def main():
    tmp = tempfile.mkdtemp()
    try:
        exe = os.path.join(tmp, "sim_board")
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror",
                        "-I", os.path.join(HERE, "sim"), "-I", os.path.join(ROOT, "firmware", "p833_board"),
                        os.path.join(HERE, "sim", "sim_main.cpp"), "-o", exe, "-pthread"], check=True)
        cfg = json.load(open(os.path.join(ROOT, "receiver", "config.example.json"), encoding="utf-8"))
        cfg["port"], cfg["listen"], cfg["page_retry_wait_s"] = PORT, "127.0.0.1", 0
        cfg_path = os.path.join(tmp, "config.json")
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"))

        board = subprocess.Popen([exe, str(PORT), "45"])
        time.sleep(9)
        env = dict(os.environ, P833_BOARD_KEY="sim-key", P833_ADMIN_PASSWORD="x")
        rlog = open(os.path.join(tmp, "receiver.log"), "w")
        receiver = subprocess.Popen([sys.executable, os.path.join(ROOT, "receiver", "p833_receiver.py"),
                                     "--config", cfg_path, "--state-dir", tmp, "--log-dir", tmp],
                                    env=env, stdout=rlog, stderr=subprocess.STDOUT)
        board.wait(timeout=60)
        time.sleep(1)
        status = json.load(urllib.request.urlopen("http://127.0.0.1:%d/status.json" % PORT))
        receiver.terminate()
        receiver.wait()

        rows = list(csv.DictReader(open(os.path.join(tmp, "calls.csv"), encoding="utf-8")))
        got = [(r["board"], r["code"], r["press"], r["result"]) for r in rows]
        print("\nReceiver call log:")
        for r in rows:
            print("  %s %s code %s press %s %s | %s" % (r["time"], r["board"], r["code"], r["press"],
                                                       r["result"], r["message"]))
        want = [("RSP_LH", "2", "27", "dry-run"), ("RSP_LH", "5", "63", "dry-run"), ("RSP_LH", "9", "42", "dry-run")]
        ok = got == want
        lh = [b for b in status["boards"] if b["board"] == "RSP_LH"]
        hb_ok = bool(lh) and lh[0]["fw"] == "1.0.0" and lh[0]["ok"]
        print("\nExpected calls arrived exactly once each, in order: %s" % ("YES" if ok else "NO, got %s" % got))
        print("Heartbeat seen (fw 1.0.0, board OK): %s" % ("YES" if hb_ok else "NO"))
        return 0 if ok and hb_ok else 1
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    sys.exit(main())
