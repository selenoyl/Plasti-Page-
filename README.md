# P833 Call Receiver

By Ethan Brady & Ryan Raikes

The laptop program that receives the P833 cells' call-for-help messages and
pages the factory floor. It replaces the Raspberry Pi in the original design.

```
PLC 24 V output --> Waveshare ESP32-S3 board --Wi-Fi--> AlmaLinux laptop
                    (decodes pulses)                     (this program)
                                                              |
                                                     SIP call | (next step)
                                                              v
                    Norstar <-- Nortel ATA2 <-- Grandstream HT813 FXO port
                    page zone 1 (floor)
```

**Status, version 0.1.0:** receiving, checking, logging and dry-run paging work
and are tested. The phone call to the HT813 is not built yet, so the receiver
refuses to start with dry run off. No real page can go out.

## What it does

- Listens on TCP port **8833** for calls and heartbeats from the boards.
- Checks each call: code 2 to 9, press 0 to 999. A bad call is refused and logged.
- Drops repeats, in two ways:
  - **Same message:** the same board, boot and sequence number within 10 minutes. This is a board resending after a lost reply.
  - **Same call:** the same board, code and press within 15 s. This covers a board that restarted and resent.
- Builds the message in English and Spanish. Press 0 reads as "Press number unknown".
- Sends pages one at a time, because there is one phone line. A failed page is retried 3 times, 10 s apart.
- Logs every call and its result to `/var/log/p833-receiver/calls.csv`. Results are dry-run, sent, FAILED or repeat dropped.
- Shows each board's last-heard time and the last 50 calls at `http://<laptop>:8833/`. It also logs a warning when a board has been silent for 3 minutes.

## Install on the laptop (AlmaLinux 8 or 9)

1. Get the files into a new, empty folder. There are two ways:

   - **If the laptop has internet:**

     ```bash
     git clone https://github.com/selenoyl/plasti-page- ~/plasti-page && cd ~/plasti-page
     ```

     If `git` is missing, install it first with `sudo dnf install git`. To update later, run `cd ~/plasti-page && git pull`, then run the installer again.
   - **If it has no internet:** on another PC, open https://github.com/selenoyl/plasti-page-, click Code, then Download ZIP. Copy the zip to the laptop by USB, then unzip it into a new folder:

     ```bash
     mkdir ~/plasti-page && cd ~/plasti-page && unzip ~/Downloads/plasti-page--main.zip && cd plasti-page--main
     ```

2. Install:

   ```bash
   sudo bash install/install.sh
   ```

   The installer does the following:
   - Backs up any earlier install to a new folder, `/root/p833-backup-<date-time>`.
   - Creates a service user, `p833`.
   - Copies the program to `/opt/p833-receiver`.
   - Writes `/etc/p833-receiver/config.json`, with dry run on. If one exists, it is kept and never overwritten.
   - Creates `/etc/p833-receiver/secrets.env` with a random board key. Only root can read it.
   - Installs and starts the systemd service `p833-receiver`, which restarts itself if it stops.
   - Opens only port 8833/tcp in firewalld.
   - Leaves SELinux on and just restores the normal file labels.

3. Keep the laptop running with the lid shut, then reboot once:

   ```bash
   sudo bash install/laptop-stay-awake.sh
   ```

4. Note the board key. Each board's firmware needs it:

   ```bash
   sudo cat /etc/p833-receiver/secrets.env
   ```

## Everyday commands

| What | Command |
|---|---|
| Is it running? | `systemctl status p833-receiver` |
| Live log | `journalctl -u p833-receiver -f` |
| Call log | `less /var/log/p833-receiver/calls.csv` |
| Status page | `http://<laptop IP>:8833/` |
| After editing config.json | `sudo systemctl restart p833-receiver` |

## Test it without a board

The test sender plays a board. Run it on the laptop:

```bash
export P833_BOARD_KEY=$(sudo sed -n 's/^P833_BOARD_KEY=//p' /etc/p833-receiver/secrets.env)
```

```bash
python3 /opt/p833-receiver/p833_test_sender.py --board RSP_LH --code 2 --press 27
```

Other uses:
- `--all`: sends codes 2 to 9.
- `--press 0`: tests the unknown-press message.
- `--resend 3`: tests repeat dropping.
- `--heartbeat`: sends a heartbeat.
- `--pulses --all --press 27`: prints the pulse widths the PLC sends, for checking a scope trace.
- `--host <laptop IP>`: sends from another PC.

## Settings: `/etc/p833-receiver/config.json`

| Setting | Meaning |
|---|---|
| `dry_run` | `true` = log the page, never dial. Leave it on until the phone step is built and approved. |
| `boards` | Board ID to cell name, for example `"RSP_LH": {"cell": "RSP LH"}`. A call from an unknown board is still paged, with a warning. |
| `zones` | The digits the HT813 dials for each zone. **`TO-VERIFY`** until checked on the ATA2. |
| `codes` | For each code: who is called, its zone (all `floor` now), and the English and Spanish text. `{press}` is replaced by the press number. `_no_press` text is used for press 0. |
| `repeat_window_s`, `same_call_window_s` | Repeat-drop windows, 600 s and 15 s. |
| `board_silent_after_s` | How long a board can be silent before it is flagged, 180 s. |
| `page_retries`, `page_retry_wait_s` | Page retries, 3 tries 10 s apart. |

Credentials never go in `config.json`. The board key is in `secrets.env`, which only root can read.

## Board to laptop message (for the board firmware)

Over Wi-Fi, by HTTP POST to `http://192.168.83.10:8833`, with the header
`X-P833-Key: <board key>` and a JSON body.

**Call:** `POST /call`

```json
{"board": "RSP_LH", "boot": "a91c", "seq": 41, "code": 2, "press": 27, "age_ms": 0}
```

| Field | Meaning |
|---|---|
| `board` | The board's ID, up to 32 characters. |
| `boot` | A random value picked at each power-up. |
| `seq` | Counts up by 1 for each new call. |
| `code` | 2 to 9. |
| `press` | 0 to 999, from the pulses. |
| `age_ms` | How long the call waited on the board before this send. |

The laptop replies `200 {"ok": true, ...}`. The board resends the **same** body every few seconds until it gets a 200; repeats are dropped safely. A `400` reply means the message is wrong, so don't resend it. A `403` reply means the key is wrong.

**Heartbeat, every 60 s:** `POST /heartbeat`

```json
{"board": "RSP_LH", "boot": "a91c", "fw": "1.0", "rssi": -58, "uptime_s": 3600}
```

## Proposed addresses (IoT network, can be changed)

| Device | Address |
|---|---|
| Network | 192.168.83.0/24, isolated, off the plant LAN |
| Laptop (receiver) | 192.168.83.10, fixed |
| Grandstream HT813 | 192.168.83.20, fixed |
| Cell boards | 192.168.83.101 RSP LH, .102 RSP RH, .103 FSP LH, .104 FSP RH |

## Files

| Path in the zip | What |
|---|---|
| `receiver/p833_receiver.py` | The receiver. Python 3.6+, standard library only. |
| `receiver/config.example.json` | Default config. |
| `tools/p833_test_sender.py` | Test sender that plays a board. |
| `install/install.sh` | Installer and updater. |
| `install/p833-receiver.service` | systemd unit. |
| `install/laptop-stay-awake.sh` | Lid and sleep settings. |
| `tests/test_receiver.py` | Unit tests: `python3 -m unittest discover -s tests -v` |
| `FLOOR_TEST.md` | Floor-test checklist. |
| `AAR.md` | Decisions and open points. |
