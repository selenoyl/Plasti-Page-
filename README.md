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

**Status, version 0.3.1:** receiving, checking, logging, dry-run paging, the
admin page and joining the recorded audio into a page work and are tested.
The recorded clips themselves are not in the repository yet (see "Page audio"). The phone call to the HT813 is not built yet:
with dry run off, every page is logged as FAILED ("phone pager not built yet").
No real page can go out.

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
- Has an admin page at `http://<laptop>:8833/admin`, behind a password, for test calls, custom pages, boards and the dry-run switch. See "The admin page" below.

## Install on the laptop (AlmaLinux 8 or 9)

1. Get the files into a new, empty folder. There are two ways:

   - **If the laptop has internet:**

     ```bash
     git clone https://github.com/selenoyl/plasti-page- ~/plasti-page && cd ~/plasti-page
     ```

     If `git` is missing, install it first with `sudo dnf install git`.
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
   - Creates `/etc/p833-receiver/secrets.env` with a random board key and admin page password, and prints the password. Only root can read the file. On an update it is kept, and anything new is added to it.
   - Installs and starts the systemd service `p833-receiver`, which restarts itself if it stops.
   - Opens only port 8833/tcp in firewalld.
   - Leaves SELinux on and just restores the normal file labels.

3. Keep the laptop running with the lid shut, then reboot once:

   ```bash
   sudo bash install/laptop-stay-awake.sh
   ```

4. Note the board key, which each board's firmware needs, and the admin password:

   ```bash
   sudo cat /etc/p833-receiver/secrets.env
   ```

   To change the admin password, edit that file, then restart the service with `sudo systemctl restart p833-receiver`.

## Update to a new version

If you installed with `git clone`:

```bash
cd ~/plasti-page && sudo bash install/update.sh
```

It fetches the newest version from GitHub, shows what changed and runs the installer. The installer backs up the old install first. Your config, secrets, board list, dry-run setting and call log are kept.

Without internet: download the new zip on another PC, unzip it into a **new** folder on the laptop, and run `sudo bash install/install.sh` from there.

## The admin page

Open `http://<laptop IP>:8833/admin`. The browser asks for a user name and password: any user name works, and the password is `P833_ADMIN_PASSWORD` from `secrets.env`. The status page at `/` stays open and read-only.

| Section | What it does |
|---|---|
| **Phone and paging setup** | HT813 address and ports, 1-stage or 2-stage dialing, the timing waits, each zone's **dial digits** (0-9 * #, comma = half-second pause, F = hook flash), which zone each code pages, the chime, and how many times the message plays. Saved on the laptop on top of the config file's values; **Reset** goes back to the config. |
| **Settings** | Turns dry run on or off. Turning it off asks for confirmation. The choice is saved on the laptop and kept through restarts and updates; the config's `dry_run` is only the first-time default. |
| **Send a test call** | Pick a board, a call type (2 to 9) and a press. It goes through exactly the same path as a real call from that board, and is logged with the detail "admin page test". |
| **Add or rename a board** | Enter the board ID (it must match the ID in that board's firmware) and a cell name. Using an existing ID renames its cell. |
| **Send a custom page** | Type your own English and, optionally, Spanish text and pick a zone. It is logged as code "custom". |
| **Boards table** | **Remove** takes a board off the list. A board that calls in but isn't on the list shows an **Add** box in its row. |

With dry run off, every test or custom page is really dialed (once the phone step exists).

## Everyday commands

| What | Command |
|---|---|
| Is it running? | `systemctl status p833-receiver` |
| Live log | `journalctl -u p833-receiver -f` |
| Call log | `less /var/log/p833-receiver/calls.csv` |
| Status page | `http://<laptop IP>:8833/` |
| After editing config.json | `sudo systemctl restart p833-receiver` |

## Test it without a board

The admin page's test call is the easy way. The command-line test sender also plays a board, and can test things the page can't, such as repeats and a wrong key. Run it on the laptop:

```bash
export $(sudo grep P833_BOARD_KEY /etc/p833-receiver/secrets.env)
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
| `dry_run` | The first-time default only. After that, the switch on the admin page decides. |
| `boards` | The **starting** board list, used only on the very first start. After that, boards are added and removed on the admin page; the list is kept in `/var/lib/p833-receiver/board_list.json`. A call from a board that isn't on the list is still paged, with a warning. |
| `zones` | The digits dialed for each zone; empty = not set. Easier to set on the admin page. |
| `phone` | HT813 address, ports, dial method and timings. Easier to tune on the admin page. |
| `audio` | Chime, times played, languages, and how many dry-run pages' audio to keep. |
| `codes` | For each code: who is called, its zone (all `floor` now), an optional `why` shown on the admin page, and the English and Spanish text. `{press}` is replaced by the press number. `_no_press` text is used for press 0. |
| `repeat_window_s`, `same_call_window_s` | Repeat-drop windows, 600 s and 15 s. |
| `board_silent_after_s` | How long a board can be silent before it is flagged, 180 s. |
| `page_retries`, `page_retry_wait_s` | Page retries, 3 tries 10 s apart. |

Credentials never go in `config.json`. The board key and admin password are in `secrets.env`, which only root can read.

## Page audio

Every page is spoken from clips recorded ahead of time, so the laptop needs no
speech program and no internet. A page plays: chime, English, Spanish, and the
whole thing again (`audio.repeat`, default 2).

- The clips live in `audio/` in the repository and are installed to `/opt/p833-receiver/audio`.
- They are made by `tools/make_audio.py` with the free offline Piper voices: `en_US-lessac-medium` (English) and `es_MX-claude-high` (Spanish).
- Each message is recorded in pieces ("Maintenance requested to press", the number, "for camera fault."), and the numbers 1 to 999 are recorded once each. That's about 2,000 clips.
- On the admin page:
  - The **Audio** box shows whether every message is recorded and matches the config wording.
  - **Listen** plays any call in the browser without paging.
- In dry run, the audio of the last 20 pages is saved in `/var/lib/p833-receiver/last_pages/`.
- If you change the wording in the config, re-make the clips. The Audio box flags clips that no longer match the wording. Until they're re-made, the old clips play. A page with a missing clip fails.

  ```bash
  python3 -m pip install piper-tts
  ```

  ```bash
  python3 tools/make_audio.py --config /etc/p833-receiver/config.json --out audio
  ```

  Then run `sudo bash install/install.sh`. This needs Python 3.9 or newer, ffmpeg, and internet the first time, to download the voices.
- Custom pages from the admin page have no audio yet, because they are free text. In dry run they are logged; live, they fail.

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
| `install/install.sh` | First setup, or reinstall from this folder. Safe to run again. |
| `install/update.sh` | Fetch the newest version from GitHub and run the installer. |
| `receiver/p833_audio.py` | Joins the recorded clips into a page. |
| `tools/make_audio.py` | Records the clips with Piper (run on a PC with internet). |
| `audio/` | The recorded clips and `manifest.json` (once made). |
| `install/migrate_config.py` | Run by the installer: updates default wording in an installed config, keeping your own edits. |
| `install/p833-receiver.service` | systemd unit. |
| `install/laptop-stay-awake.sh` | Lid and sleep settings. |
| `tests/test_receiver.py` | Unit tests: `python3 -m unittest discover -s tests -v` |
| `FLOOR_TEST.md` | Floor-test checklist. |
| `AAR.md` | Decisions and open points. |
