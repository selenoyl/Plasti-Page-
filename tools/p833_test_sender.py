#!/usr/bin/env python3
# P833 test sender: plays a cell's board and sends calls to the receiver.
# By Ethan Brady & Ryan Raikes
#
# Examples:
#   p833_test_sender.py --board RSP_LH --code 2 --press 27
#   p833_test_sender.py --board RSP_LH --all --press 27     (codes 2 to 9)
#   p833_test_sender.py --board RSP_LH --code 2 --press 27 --resend 3
#   p833_test_sender.py --board RSP_LH --heartbeat
#   p833_test_sender.py --pulses --code 2 --press 27          (no network)
#
# The board key is read from --key or the P833_BOARD_KEY environment variable.

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request


def pulses(code, press):
    """Pulse widths in ms the PLC sends for this code and press."""
    h, t, o = press // 100, (press // 10) % 10, press % 10
    return [500 + 500 * code] + [500 + 100 * d for d in (h, t, o)]


def post(url, body, key):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-P833-Key": key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def main():
    p = argparse.ArgumentParser(description="Send test calls to the P833 receiver")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8833)
    p.add_argument("--key", default=os.environ.get("P833_BOARD_KEY", ""))
    p.add_argument("--board", default="TEST")
    p.add_argument("--code", type=int)
    p.add_argument("--press", type=int, default=27)
    p.add_argument("--all", action="store_true", help="send codes 2 to 9, 20 s apart")
    p.add_argument("--gap", type=float, default=20, help="seconds between calls with --all")
    p.add_argument("--resend", type=int, default=0,
                   help="send the same message this many extra times, like a board retrying")
    p.add_argument("--heartbeat", action="store_true")
    p.add_argument("--pulses", action="store_true", help="only print the pulse widths")
    a = p.parse_args()

    if a.pulses:
        codes = range(2, 10) if a.all else [a.code or 2]
        for c in codes:
            print("code %d press %d: %s ms" % (c, a.press, ", ".join(map(str, pulses(c, a.press)))))
        return 0

    base = "http://%s:%d" % (a.host, a.port)
    # Like a board: a new random boot ID for each run, and calls numbered 1, 2, 3... (the
    # receiver refuses a number that jumps far past the last one, as a forged message would).
    boot = "test%08x" % random.getrandbits(32)

    if a.heartbeat:
        st, txt = post(base + "/heartbeat", {"board": a.board, "boot": boot, "fw": "test",
                                             "rssi": -55, "uptime_s": 1}, a.key)
        print("heartbeat:", st, txt)
        return 0 if st == 200 else 1

    if a.all:
        codes = list(range(2, 10))
    elif a.code is not None:
        codes = [a.code]
    else:
        p.error("give --code, --all, --heartbeat or --pulses")

    failed = 0
    seq = 0
    for i, code in enumerate(codes):
        if i:
            time.sleep(a.gap)
        seq += 1
        body = {"board": a.board, "boot": boot, "seq": seq, "code": code,
                "press": a.press, "age_ms": 0}
        for n in range(1 + a.resend):
            st, txt = post(base + "/call", body, a.key)
            print("call code %d press %d seq %d send %d: %s %s" % (code, a.press, seq, n + 1, st, txt))
            if st != 200:
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
