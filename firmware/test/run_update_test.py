#!/usr/bin/env python3
# Test of the board's firmware-update page on a PC (no hardware needed): builds the
# real firmware against the stand-in ESP32 libraries and plays requests into the
# /update handlers. Only a whole firmware file, with the right password, from the
# board's own page (or a tool with no Origin), may restart the board.
#
#   python3 firmware/test/run_update_test.py
# By Ethan Brady & Ryan Raikes

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

CASES = [  # (case, should it restart?, reply code if not)
    ("post-no-file-no-password", False, 400),
    ("post-no-file-with-password", False, 400),
    ("updates-off", False, 400),
    ("wrong-password-upload", False, 401),
    ("other-site", False, 400),
    ("lockout", False, 429),
    ("good-upload", True, None),
    ("good-upload-no-origin", True, None),
]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        exe = os.path.join(tmp, "test_update")
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror",
                        "-I", os.path.join(HERE, "sim"), "-I", os.path.join(ROOT, "firmware", "p833_board"),
                        os.path.join(HERE, "sim", "test_update_main.cpp"), "-o", exe, "-pthread"], check=True)
        bad = 0
        for case, restart, code in CASES:
            r = subprocess.run([exe, case], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            out = r.stdout.decode()
            restarted = r.returncode == 3
            last = [ln for ln in out.splitlines() if ln.startswith("[sim] web reply")]
            got = int(last[-1].split()[-1]) if last else None
            ok = restarted == restart and (restart or got == code)
            bad += not ok
            print("%-28s %s  (restarted: %s, last reply: %s)" % (case, "OK  " if ok else "FAIL", restarted, got))
        print("\nAll update cases behave: %s" % ("YES" if not bad else "NO"))
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
