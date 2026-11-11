# P833 Call Receiver: Floor-Test Checklist

By Ethan Brady & Ryan Raikes

Keep the status page `http://<laptop IP>:8833/` and the live log
(`journalctl -u p833-receiver -f`) open for every step. Write the time, cell and
result for each step.

## A. Bench, laptop only (can be done now)

| # | Step | Expected | Pass |
|---|---|---|---|
| A1 | `systemctl status p833-receiver` | active (running) | ☐ |
| A2 | `getenforce` | Enforcing | ☐ |
| A3 | `sudo firewall-cmd --list-ports` | 8833/tcp only, plus any ports opened before | ☐ |
| A4 | Test sender, `--all --press 27` | 8 calls, each logged "dry-run" with the right text | ☐ |
| A5 | Test sender, `--code 2 --press 0` | "Press number unknown" | ☐ |
| A6 | Test sender, `--code 2 --press 27 --resend 3` | 1 dry-run and 3 "repeat dropped" | ☐ |
| A7 | Test sender with `--key wrong` | refused (403), nothing paged | ☐ |
| A8 | `sudo systemctl kill p833-receiver` | it is running again within ~5 s | ☐ |
| A9 | Reboot the laptop with the lid shut | it comes back, and the status page shows the old calls | ☐ |
| A10 | From another PC on the IoT network, browse to the status page | page loads | ☐ |
| A11 | Open `/admin` with a wrong password, then the right one | refused, then the admin page | ☐ |
| A12 | Admin: send a test call from each board | each logged with its own cell, "admin page test" | ☐ |
| A13 | Admin: add a board `TEST_1`, rename it, remove it | the list updates each time and stays the same after a restart | ☐ |
| A14 | Admin: send a custom page | logged as code "custom", dry-run | ☐ |
| A15 | Admin: turn dry run off, send one test call, then turn it back on | FAILED "phone pager not built yet" (until the phone step exists), then dry-run again | ☐ |
| A17 | Admin: **Preview** codes 2 to 9 at press 27, code 2 at press 0, and code 2 at press 150 | right department tone, English, Spanish, twice; words and number right; 150 says "press number unknown" with a note; nothing logged | ☐ |
| A18 | Admin page, Audio box | "OK: all messages recorded" | ☐ |
| A19 | On the real PA: a test page | the start of the tone is not cut off (raise "Silence at the start" if it is); one voice throughout; words clear | ☐ |
| A16 | `sudo bash install/update.sh` | "Already the newest version", service running, boards and dry-run setting kept | ☐ |

## B. At the cell (needs the board firmware)

| # | Step | Expected | Pass |
|---|---|---|---|
| B1 | Board powered on | shows OK on the status page within 60 s | ☐ |
| B2 | A call from each cell's panel: RSP LH, RSP RH, then FSP LH, FSP RH | right cell, code and press logged | ☐ |
| B3 | A call with the e-stop pressed | logged as normal | ☐ |
| B4 | Press set to 0 on the panel, then a call | "Press number unknown" | ☐ |
| B5 | Codes 7, 8 and 9 (audit notice, camera fail, Red Rabbit fail) | right text, and 9 before 8 before 7 if they queue | ☐ |
| B6 | Restart the receiver mid-day, and make a call while it is down | the board resends and the call arrives once it is back | ☐ |
| B7 | Unplug a board | SILENT on the status page after 3 min, and a warning in the log | ☐ |
| B8 | PLC to program mode mid-message | nothing logged (the board discards a partial message) | ☐ |

## C. Phone (after the phone step is built and you approve live pages)

| # | Step | Expected | Pass |
|---|---|---|---|
| C1 | Analog phone on the ATA2: find the digits that page zone 1 | digits written into config `zones.floor.dial` | ☐ |
| C2 | One live test page, at an agreed quiet time | heard on the floor, in English then Spanish | ☐ |
| C3 | Unplug the HT813, then a call | logged FAILED after 3 tries | ☐ |
