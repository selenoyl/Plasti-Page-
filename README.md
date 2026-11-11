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

**Status, version 0.5.1:** receiving, checking, logging, dry-run paging, the
admin page, the editable phone settings and the recorded page audio are in and
tested. The phone call to the HT813 is not built yet:
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
| **Phone and paging setup** | HT813 address and ports, 1-stage or 2-stage dialing, the timing waits, each zone's **dial digits** (0-9 * #, comma = half-second pause, F = hook flash), which zone each code pages, each code's department tone, the tone once, twice or off, the silence at the start, and how many times the message plays. Saved on the laptop on top of the config file's values; **Reset** goes back to the config. |
| **Settings** | Turns dry run on or off. Turning it off asks for confirmation. The choice is saved on the laptop and kept through restarts and updates; the config's `dry_run` is only the first-time default. |
| **Send a test call** | Pick a board, a call type (2 to 9) and a press. **1. Preview** shows the exact words and plays the exact audio, sending nothing. **2. Send** goes through exactly the same path as a real call from that board, and is logged with the detail "admin page test". |
| **Add or rename a board** | Enter the board ID (it must match the ID in that board's firmware) and a cell name. Using an existing ID renames its cell. |
| **Send a custom page** | Type your own English and, optionally, Spanish text and pick a zone. **Preview** shows it; there is no audio for free text. It is logged as code "custom". |
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
| `audio` | Tones, times played, silence at the start, languages, and how many dry-run pages' audio to keep (default 0 = none). |
| `codes` | For each code: who is called, its zone (all `floor` now), an optional `why` shown on the admin page, and the English and Spanish text. `{press}` is replaced by the press number. `_no_press` text is used for press 0. |
| `repeat_window_s`, `same_call_window_s` | Repeat-drop windows, 600 s and 15 s. |
| `board_silent_after_s` | How long a board can be silent before it is flagged, 180 s. |
| `page_retries`, `page_retry_wait_s` | Page retries, 3 tries 10 s apart. |

Credentials never go in `config.json`. The board key and admin password are in `secrets.env`, which only root can read.

## Page audio

Every page is spoken from clips recorded ahead of time, so the laptop needs no
speech program and no internet. A page plays: **2.5 s of silence** (so the
paging system is open before anything plays; adjustable on the admin page),
then the **department tone**, English, Spanish, and the tone and message again
(`audio.repeat`, default 2).

- **Department tones** (`audio/tones/`, your andon tones, phone versions):

  | Tone | Department | Codes |
  |---|---|---|
  | maintenance bell | Maintenance | 2, 8 (camera fault), 9 (Red Rabbit) |
  | technician wood | Technician | 3 |
  | supervisor glide | Supervisor | 4, 7 (audit check-in) |
  | quality flute | Quality | 5 |
  | packaging pluck | Packaging | 6 |

  - On the admin page (Phone and paging setup) you can change each code's tone, and set the tone to play **once**, **twice** (the `twice` tone files) or **off**.
  - A code with no tone, and custom pages, get a short chime instead.
  - To add a tone, put an 8 kHz 16-bit mono WAV with the same name in both `audio/tones/single/` and `audio/tones/twice/`.
- **Voices** (free, offline Piper voices): English `en_US-hfc_female-medium`, Spanish `es_MX-claude-high` (Mexican).
- **Press numbers 1 to 100 are recorded.** A press above 100 still pages, but says "press number unknown". The log and the Preview note it.
- The clips live in `audio/` in the repository (243 files, 5 MB) and are installed to `/opt/p833-receiver/audio`.
  - Pages say **"press number"**, then the number: "Maintenance requested to press number" + "27" ("Se solicita mantenimiento en la prensa número" + "27").
  - The department phrase is **one recording per department** ("Maintenance requested to press number"), shared by codes with the same start (2, 8 and 9; 4 and 7). Recording the department word on its own made it sound like a different voice.
  - The numbers 1 to 100 are recorded once per language and shared by every department, with a 120 ms pause before them.
  - The reason for codes 7 to 9 follows the number ("for camera fault."). The press-unknown message is one whole recording per code.
  - Words a voice mispronounces get a spoken spelling in `tools/make_audio.py` (`PRONOUNCE`). For example, the Spanish voice says "Red Rábit"; the page text doesn't change.
- **Checked by machine:** every press 1 to 100 and every message, in both languages, was transcribed with a speech recognizer (Whisper), and the numbers all came out right.
- **Length:** a page is about 22 to 25 s with the message played twice, and about 30 s for the longest (Red Rabbit), including the 2.5 s of silence. Set "Times the message plays" to 1 on the admin page to roughly halve it.
- On the admin page:
  - **Preview** (Send a test call) shows the exact words, tone and zone, and plays the exact audio in your browser. Nothing is sent or logged.
  - The **Audio** box shows whether every message is recorded and matches the config wording.
- In dry run, **no page audio is saved** (use Preview to hear a message). To keep the audio of recent dry-run pages for troubleshooting, set "Keep audio of the last dry-run pages" on the admin page, for example to 20. They go in `/var/lib/p833-receiver/last_pages/`, and setting it back to 0 removes them at the next start.
- If you change the wording, the voices or the number range, re-make the clips on a PC with internet, Python 3.9+ and ffmpeg. Then commit the `audio/` folder and update the laptop.

  ```bash
  python3 -m pip install piper-tts
  ```

  ```bash
  python3 tools/make_audio.py --config receiver/config.example.json --out audio --max-press 100
  ```

  Until the clips are re-made, the Audio box flags any that no longer match, and the old clips play. A page with a missing clip fails.
- Custom pages from the admin page have no audio, because they are free text. In dry run they are logged; live, they fail.

## Phone paging through the HT813

With dry run off, each page is a phone call from the laptop to the Grandstream
HT813's FXO port (direct SIP call, no phone server). The HT813 takes the ATA2
line off-hook; the laptop sends the zone's dial digits as tones (2-stage), waits
for the page to open, plays the page audio and hangs up. The settings are under
**Phone and paging setup** on the admin page.

**HT813 settings that matter** (firmware 1.0.17.3; the factory settings already
match except where marked):

| Page | Setting | Value |
|---|---|---|
| FXO PORT | Account Active | Yes |
| FXO PORT | **SIP Registration** | **No** (no phone server) |
| FXO PORT | Local SIP Port | 5062 (the admin page's "HT813 FXO SIP port") |
| FXO PORT | Check SIP User ID for incoming INVITE / Allow Incoming SIP Messages from SIP Proxy Only / Authenticate incoming INVITE | No / No / No (direct calls allowed) |
| FXO PORT | Stage Method | 2 |
| FXO PORT | Preferred DTMF method | RFC2833 first, payload 101 |
| FXO PORT | Send Hook Flash To PSTN | Yes (so `F` works) |
| FXO PORT | **Hook Flash Duration** | **300 ms**. At 600 ms (the default) the Norstar ATA2 took it as a short hang-up, not Link, and the call went to extension 5939. |
| FXO PORT | Preferred Vocoder | PCMU first |
| FXO PORT | **Number of Rings** | **50** (it never answers calls coming in on the ATA2 line) |
| ADVANCED | Disable Direct IP Call | No |
| ADVANCED | **TR-069** | **No** (otherwise it reports to Grandstream's cloud) |
| ADVANCED | **3CX Auto Provision** / **Allow DHCP Option 66 or 160 to override server** | **No** / **No** |
| ADVANCED | Automatic Upgrade | No; and **Always Skip the Firmware Check** |
| BASIC | Reply to ICMP on WAN port | Yes (so it answers ping when troubleshooting) |
| BASIC | Admin password | changed from the label's |

**Admin page tools:**
- **Call the HT813 (no dialing):** holds the ATA2 line about 3 s and dials nothing. Proves laptop → HT813 → line.
- **Try page digits:** dials what you type, then plays the maintenance bell tone for about 4 s (no spoken message), and records what the line sends back (play it under "Last phone test").

**The floor page code is `F,*60,2,1`:**
- `F` is Link, the hook flash.
- `*60` is the general page.
- `2` picks the speakers.
- `1` is zone 1.

It's the same as PAGE, SPEAKER, 1 on a Norstar desk phone. The ATA2 is extension 5912. Every ATA2 feature code must start with Link ([pbxbook: Enhanced ATA, ATA2 Access Codes](https://pbxbook.com/norstar/enhanced_ata.html)).

Both work with dry run on.

## Cell boards

Each cell's Waveshare ESP32-S3-ETH-8DI-8RO runs the firmware in `firmware/`.
That covers wiring, flashing with the Arduino IDE, and setup over USB; see
[firmware/README.md](firmware/README.md).

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
| `firmware/p833_board/` | Cell board firmware (Arduino sketch and pulse decoder). |
| `firmware/README.md` | Board wiring, flashing and setup. |
| `firmware/test/` | Decoder test and the end-to-end firmware simulation (PC, no hardware). |
| `FLOOR_TEST.md` | Floor-test checklist. |
| `AAR.md` | Decisions and open points. |
