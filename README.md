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

**Status, version 0.12.1:** receiving, checking, logging, the admin page (HTTPS),
paging through the HT813 (working on the floor), the recorded page audio, the Piper page
voice, the voice lab and the optional voip.ms texts (off until set up) are in and tested. The whole
project was security-reviewed on 12 Nov 2026; see "Security". A fresh install starts
in dry run: no real page goes out until dry run is turned off on the admin page.

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
- Has an admin page at `https://<laptop>:8833/admin` (HTTPS, same port), behind a password, for test calls, custom pages, boards and the dry-run switch. See "The admin page" below.

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
   - Opens only port 8833/tcp in firewalld (to every network, or only to the networks you list; see "Security").
   - Leaves SELinux on and just restores the normal file labels.

3. Keep the laptop running with the lid shut, then reboot once:

   ```bash
   sudo bash install/laptop-stay-awake.sh
   ```
   Closing the lid then no longer locks the screen, so log out (or lock it) before walking away.

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

It fetches the newest version from GitHub, shows what changed, and **asks before installing** (`sudo bash install/update.sh -y` skips the question). The installer backs up the old install first and checks that the new version loads before replacing anything. Your config, secrets, board list, dry-run setting and call log are kept.

Without internet: download the new zip on another PC, unzip it into a **new** folder on the laptop, and run `sudo bash install/install.sh` from there.

## The admin page

Open `https://<laptop IP>:8833/admin` (plain `http://` is sent there). The first time, the browser warns about the certificate: the installer made it on the laptop, so no outside company vouches for it. Check that the fingerprint matches the one the installer printed (`sudo openssl x509 -in /etc/p833-receiver/tls/cert.pem -noout -fingerprint -sha256`), then choose **Advanced > Proceed**. The browser asks for a user name and password: any user name works, and the password is `P833_ADMIN_PASSWORD` from `secrets.env`. The status page at `/` stays open and read-only.

The admin page doesn't reload itself: the mode line, the boards, the recent calls and the Make all progress update in place every 10 s, so what you're typing or listening to is left alone.

| Section | What it does |
|---|---|
| **Phone and paging setup** | HT813 address and ports, 1-stage or 2-stage dialing, the timing waits, each zone's **dial digits** (0-9 * #, comma = half-second pause, F = hook flash), which zone each code pages, each code's department tone, the tone once, twice or off, the silence at the start, and how many times the message plays. Saved on the laptop on top of the config file's values; **Reset** goes back to the config. |
| **Settings** | Turns dry run on or off. Turning it off asks for confirmation. The choice is saved on the laptop and kept through restarts and updates; the config's `dry_run` is only the first-time default. |
| **Send a test call** | Pick a board, a call type (2 to 9) and a press. **1. Preview** shows the exact words and plays the exact audio, sending nothing. **2. Send** goes through exactly the same path as a real call from that board, and is logged with the detail "admin page test". |
| **Add or rename a board** | Enter the board ID (it must match the ID in that board's firmware) and a cell name. Using an existing ID renames its cell. |
| **Send a custom page** | Type your own English and, optionally, Spanish text and pick a zone. It is spoken by Piper with the page voice (see "Page voice"), and **Preview** lets you hear it. Without Piper installed, a live custom page fails. It is logged as code "custom". |
| **Boards table** | **Remove** takes a board off the list. A board that calls in but isn't on the list shows an **Add** box in its row. |

With dry run off, every test or custom page is really dialed (once the phone step exists).

## Everyday commands

| What | Command |
|---|---|
| Is it running? | `systemctl status p833-receiver` |
| Live log | `journalctl -u p833-receiver -f` |
| Call log | `sudo less /var/log/p833-receiver/calls.csv` (only root and the service can read the logs) |
| Status page | `http://<laptop IP>:8833/` |
| After editing config.json | `sudo systemctl restart p833-receiver` |
| Old install backups | `sudo ls -d /root/p833-backup-*`. Each holds an old copy of the secrets and logs; delete the old ones you don't need with `sudo rm -r /root/p833-backup-<date-time>`. |

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
- **Every message is one whole recorded sentence** per call type, press and language, so it flows naturally; nothing is joined mid-sentence. They're spoken a little slower than normal (pace 1.25, sample W2).
  - **English** reads the press **digit by digit**: "Maintenance requested to press two, seven."
  - **Spanish** says the whole number: "Se solicita mantenimiento en la prensa 27."
  - The setting `audio.speak_digits` lists which languages use digits.
  - The log and the admin page still show "press 27".
- **Presses 1 to 100 are recorded.** A press above 100 still pages, but says "press number unknown". The log and the Preview note it.
- The clips live in `audio/` in the repository: 1,616 sentences, 49 MB, stored as 8-bit µ-law, the same format as the phone line. They're installed to `/opt/p833-receiver/audio`.
  - Words a voice mispronounces get a spoken spelling in `tools/make_audio.py` (`PRONOUNCE`). For example, the Spanish voice says "Red Rábit"; the page text doesn't change.
- **Volume:** "Page volume (dB)" on the admin page (default **+6**, range -6 to +12) makes the voice and tones louder before they go out, with a soft limiter, so peaks are rounded off instead of clipping. +6 dB is about 5 dB louder on average; +12 dB is about 9 dB louder. A speech recognizer still understood every page at +12 after phone encoding. Preview and Listen play at the same volume. The paging amplifier's volume knob and the HT813's FXO **Gain TX** are the other places to turn it up.
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

  It takes about 6 minutes for all 1,616 sentences.
  ```

  Until the clips are re-made, the Audio box flags any that no longer match, and the old clips play. A page with a missing clip fails.
- Custom pages from the admin page have no audio, because they are free text. In dry run they are logged; live, they fail.

## Text messages (voip.ms), optional

Each call from a cell can also go out as a one-way text, for example
"Chapar: Maintenance requested to press 27 for camera fault." It's **off** until you switch it on in the
**Text messages (voip.ms)** box on the admin page.

**voip.ms setup:**
1. Go to **Main Menu > SOAP and REST/JSON API**.
2. Enable the API and set an **API password**. This isn't your voip.ms login password.
3. Add the site's **public internet IP address** to the allowed list. If you don't know it, send a test text: when the address isn't allowed yet, the result says which address voip.ms sees.
4. Turn **SMS on** for the voip.ms number (DID) the texts come from.
5. E-mail **messaging@voip.ms** before going live. Ask them to register the texts as business (A2P) messages, and to raise the daily limit if you need more. The default is 100 texts a day, and each number on the list counts. Unregistered automated texts to US mobiles can be quietly blocked by the carriers.

**On the admin page:**
- Enter the voip.ms account e-mail and the API password. The password is write-only: it's stored in `/var/lib/p833-receiver/sms_secret.json` (mode 0600, readable only by the paging service) and is never shown again or written to a log.
- Enter your **SMS-enabled voip.ms number** (DID) to send from, and up to 20 numbers to text. Each number gets its own text, because the voip.ms API doesn't do group chats.
- **Pause between texts** is a **random** time between two settings (default 1 to 10 s). It spreads one call's texts out with no fixed rhythm, so the carrier doesn't flag them as spam. **Most texts per hour** (default 60) caps cost and spam; texts over it are skipped and logged.
- Set the prefix (default "Chapar:") and the message pattern, then click **Send a test text**.
- **Send a text** at the top of the box sends one text to any number you type, with or without the prefix. It works even while automatic texts are off.

**How it behaves:**
- Texts go out from a separate queue, so a slow or failed text never delays a page.
- Repeats aren't texted. Test calls from the admin page are texted only if "Text test calls too" is ticked.
- Results show under **Recent texts**; the full log is `/var/log/p833-receiver/texts.csv`. "sent" means voip.ms accepted the text; there is no delivery receipt.
- If voip.ms says the account itself has a problem (wrong password, API off, address not allowed, no funds, daily limit), the other numbers are skipped instead of tried, and the result says what to fix.
- If voip.ms can't be reached, the text is tried once more. If the request went out but no answer came back, it is **not** retried, because that could text everyone twice.
- Texts are plain ASCII: accents are dropped ("Mañana" becomes "Manana"). Anything over 160 characters is cut and ends in "...".
- The laptop needs to reach voip.ms on the internet (HTTPS, port 443), even if the rest of the IoT network is isolated.
- voip.ms's API takes the password in the HTTPS address, so if the plant has a firewall that opens HTTPS traffic to inspect it, exempt voip.ms (or the laptop) from that.
- On the voip.ms account, turn off international and premium-rate calling if you don't use them, and keep a small prepaid balance with automatic recharge off, so a stolen API password can't run up a big bill.
- The API password can only be saved over HTTPS (the normal admin page). If the admin page ever shows "not encrypted", the password field is ignored.

## PA clarity (if people can hear pages but not understand them)

The **PA clarity** box on the admin page shapes the speech for paging horns instead of for listening:
- **Cut the bass** below about 300 Hz. Through a horn, bass is just mud.
- **Clarity boost** of a few dB at 2 to 4 kHz, where consonants are ("press" vs "tech").
- **Even out the loudness** so quiet syllables come up and every word carries over machine noise.
- **Output level**: the peaks are held smoothly at this level (-3 dB below the top to start) by a
  look-ahead limiter, so nothing is clipped. That includes the HT813, the ATA2, the Norstar and the PA
  amplifier further down the line.
- **Pause after the tone**: 0.4 s between the tone and the first word (0.3 s before); with the quiet
  start of the sentence, ears get about 0.7 s to turn to it. It applies with PA clarity on or off. Keep it
  at 0.4 s or under: the longest silence that has worked on the floor is about 0.8 s, and the feedback
  eliminator ends a page at a longer one.

Why: the **Page volume** of +8 dB pushes every page into a limiter. Measured on the recordings, it bends
13 to 19% of the speech and sends very hot peaks down the phone line. That gives "loud but mushy", and
it can overdrive the equipment after it. With PA clarity on, the Page volume isn't used.

How to try it:
1. Tick **Shape pages for the PA**, then **Preview** a page. Untick and Preview again to compare.
   Nothing is saved until you click **Save**.
2. Save it and page the floor. It's about 5 dB quieter than +8 dB was, but clean: turn the **paging
   amplifier** up rather than the Output level. If a page ever sounds crunchy or buzzy, the level is too
   hot somewhere: lower the Output level (or the HT813's FXO **Gain TX**) and raise the amplifier instead.
3. In noisy areas, "Even out the loudness: a lot" can help further.

Two things software can't fix: cone speakers in loud areas lose to machine noise (horn speakers carry
speech far better), and the phone line itself carries only 300 to 3,400 Hz.

## Page voice (choose the voice pages are spoken in)

The **Page voice** box on the admin page sets how pages sound, with Piper on the laptop. It needs Piper
installed (see "Set up once" under Voice lab).

1. For English and Spanish, pick a **voice**, a **speed** (1 = normal, higher = slower) and how the
   **press number** is said:
   - **digit by digit** ticked: "press two, seven"; unticked: "press twenty-seven". English and Spanish each
     have their own tick box (English is ticked and Spanish isn't, like the recordings).
   - **Pause between digits**: 0 says them like the recordings ("two, seven"). A number such as 300 says
     each digit on its own with that many milliseconds of silence between (at most 800: a longer silence can
     make the feedback eliminator end the page).
2. **Preview** plays a whole page (tone, English, Spanish, repeats, page volume) for the code and press
   you pick, in your browser. Nothing is saved or sent.
3. **Save as the page voice** when it sounds right. From then on:
   - each page is spoken by Piper as it comes in, and the spoken sentence is kept on the laptop
     (`/var/lib/p833-receiver/voice_cache`, at most 300 MB; the least used goes first), so the same message
     plays straight from disk next time;
   - changing any setting (or the wording in `config.json`) makes new audio for the messages affected;
   - presses above 100 are spoken properly (the recordings only go to 100), and custom pages are spoken too.
4. **Make all messages now** (optional) speaks every message (codes 2 to 9, presses 0 to 100, both
   languages, 1,616 sentences, about 50 MB) in the background, so no page ever waits for Piper. Measured
   here it took about 4.5 minutes. It runs at low priority and pauses while a page is going out.
5. **Go back to the recorded clips** returns to the shipped recordings at any time (your Piper settings stay
   in the form). **Remove kept audio** clears the kept sentences.

How fast is it? Measured on a 4-core 2.1 GHz Xeon: loading a voice takes about 1.5 s, once; after that a
sentence takes about 0.15 s, and a kept sentence about 0.001 s. A new message reached the call log 0.6 s
after the call came in; the same message again, 0.08 s. With the Piper voice saved, the laptop keeps Piper
running with both voices loaded, which used 550 to 700 MB of memory here. Piper runs at a lower priority
than the receiver, so it doesn't hold up the phone call. A slower laptop takes longer, so check the first
page after saving.

A page never fails because of Piper. A page spends at most 10 s on Piper in all; if Piper isn't installed,
stops, or is too slow, that sentence comes from the recorded clips instead, and the call log's detail says
so. A page waits at most 3 s for Piper to be free (for example while you preview), and after one Piper
problem the rest of that page uses the recordings. After Piper hangs or crashes on a page, pages use the
recorded clips for a minute while Piper restarts in the background. Previews, the voice lab and Make all
wait for a page that needs Piper before they start.

## Voice lab (try voices, speed and pauses)

The last box on the admin page speaks **any text** with Piper (the same offline voices the recordings are
made with), so you can hear what works on your speakers before the full set is recorded.

- **Pick** a voice, the speed (1 = normal, higher = slower), an optional department tone and how many plays.
- **Write the text.** Put a pause anywhere as **[350]** (milliseconds). Each part between pauses is spoken on its own, which gives digits a clear, separate sound. For example: `Maintenance requested to press two. [350] Seven.`
- **Preview** plays it in your browser at the page volume; nothing is sent.
- **Page it** dials the zone and plays it on the speakers. It works with dry run on, and it is written to the call log (code "voice lab", with the whole text).
- When something sounds right, set it in the **Page voice** box (above) and save it: pages then use it.

**Set up once** (needs internet, and Python 3.9 or newer; on AlmaLinux 8 run `sudo dnf install -y python3.11` first):

```bash
sudo bash install/install_tts.sh
```

It installs Piper into `/opt/p833-receiver/tts` and these voices into `/opt/p833-receiver/voices` (about 60 MB each):
- English: hfc_female (current), joe, john, mike, norman, hfc_male
- Spanish: claude (current, Mexican), ald (Mexican, male), davefx (Spain, male)

To add more voices, name them: `sudo bash install/install_tts.sh en_US-sam-medium`. Normal updates don't touch Piper or the voices, and don't copy them into backups.

Piper (piper-tts 1.8) is open source under the GPL; fine for internal use.

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
| FXO PORT | Check SIP User ID for incoming INVITE / Allow Incoming SIP Messages from SIP Proxy Only / Authenticate incoming INVITE | No / No / No (direct calls allowed): for first bring-up only. **Anyone who can reach the HT813 can then use the line: lock it down, see below.** |
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
| BASIC | End User Password | changed too (the default lets anyone log in and read the settings) |

**Lock down the HT813.** As set up above, the HT813 takes a call from any device that can reach it, and
that call can use the Norstar line: page the plant, or dial out. Do steps 1 and 2 as soon as you can:
0. **Norstar (phone admin):** check whether extension 5912 (the ATA2 line) can get an outside line or a
   line pool. If it can, remove that access and give 5912 a restriction filter that allows only the page
   feature codes. Then even a misused call can only page.
1. **Network (IT, UniFi):** put the HT813 and the laptop on the isolated IoT network, and add a firewall
   rule so that only the laptop can reach the HT813 (its SIP port 5062, its audio ports and its web page).
2. **A password on calls:** on the HT813's FXO PORT page set **Authenticate ID** (for
   example `p833`) and an **Authenticate Password**, then set **Authenticate incoming INVITE** to **Yes**.
   On the laptop add the same password to the secrets file and restart:
   ```bash
   echo 'P833_HT813_PASSWORD=<the password>' | sudo tee -a /etc/p833-receiver/secrets.env >/dev/null
   ```
   ```bash
   sudo systemctl restart p833-receiver
   ```
   If the Authenticate ID isn't `p833`, also set `"ht813_auth_id"` in the `phone` part of `config.json`.
   Then run **Test connection** on the admin page. If it fails, set Authenticate incoming INVITE back to No.
3. **Its own web page:** change the admin and end-user passwords (above). If the ADVANCED page offers it,
   turn SSH off and use HTTPS for the web page.

**Catching pages that don't open (line check).** "Sent" means the HT813 answered and the audio played;
it doesn't prove the Norstar opened the page. So after dialing, the laptop listens to what the line sends
back during the wait after dialing:
- **Dial tone** means the hook flash or the digits weren't taken.
- **A busy or reorder tone** means the page zone was busy (for example, the feedback eliminator still playing the last page), or the code was refused.
- **Ringing** means it called a phone instead of opening the page.

Set "Line check after dialing" under Phone and paging setup:

| Setting | What it does |
|---|---|
| **warn** (default) | The page plays anyway, and the call's Detail says `WARNING: …` |
| **retry** | Marks that try FAILED, hangs up and tries again (up to 3 times) |
| **off** | No check |

Start with warn. Once real pages on your line show no false warnings, switch to retry. The admin page keeps the line audio of the last page ("Last live page") and of the last failed try ("Last failed page try"). It also waits **6 s between calls** ("Gap between calls"), so the HT813 and Norstar fully release the line; the HT813 itself waits 4 s.

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

## Security

What protects what (receiver 0.10.0, board firmware 1.1.3):

| Part | Protection |
|---|---|
| Admin page | Password; **HTTPS** with the laptop's own certificate (same port; boards keep plain HTTP). 10 wrong passwords from one address lock it out for 10 minutes. Forms only from the page itself (Origin/Referer). Can't be framed by another site. |
| Board calls | Board key (`secrets.env`); no key set = calls refused. Board IDs checked. Old messages sent again are never paged. At most 20 pages waiting and 6 calls a minute per board. |
| Texts | voip.ms API password write-only, stored 0600, sent only to voip.ms over checked HTTPS (no redirects). Only boards on the list send texts. Queue and hourly caps. |
| Phone line | The laptop uses only SIP and audio from the HT813's address that belong to the call, so other devices can't hang up or divert a page. Optional password on calls (above). |
| Laptop | Service runs as `p833` in a systemd sandbox; logs and state readable only by root and the service; secrets root-only; SELinux on; firewalld opens only 8833/tcp. The installer checks a new version loads before installing it; `update.sh` asks first. |
| Open status page | Shows no call details, dial digits, HT813 address or board addresses (those are on the admin page). |

**To do on site** (see also the AAR's open points):
1. Isolate the IoT network (boards, HT813, laptop) from the plant LAN, and lock down the HT813 (above).
2. Let only the IoT network and your admin PC reach the receiver's port:
   ```bash
   sudo P833_ALLOW_FROM="BOARDS_NETWORK ADMIN_PC" bash install/install.sh
   ```
   Replace BOARDS_NETWORK with the network the boards are on (like `10.180.0.0/24`) and ADMIN_PC with the
   address of the PC you use for the admin page. The installer stops, changing nothing, if an entry isn't
   an address. The choice is kept for later updates; `P833_ALLOW_FROM=any` undoes it.
3. Put firmware 1.1.3 on each board (its update page now can't be used to restart the board), then turn
   network updates off on each board between updates: `otapass` with nothing after it, on the USB console.
4. The board key goes over the network as plain text in every call. On an isolated network with a strong
   Wi-Fi password that is acceptable; signed calls would need new firmware on every board (AAR, open points).

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

The laptop replies `200 {"ok": true, ...}`. The board resends the **same** body every few seconds until it gets a 200; repeats are dropped safely. A `400` reply means the message is wrong, so don't resend it. A `403` reply means the key is wrong. A `503` reply means the laptop is busy (too many pages waiting, or over 6 calls a minute from this board): resend later.

The laptop remembers the highest `seq` it took from each board for each `boot` (in `/var/lib/p833-receiver/seen_calls.json`, kept through restarts). A message sent again later, even days later, is answered 200 "repeat" and not paged.

**Heartbeat, every 60 s:** `POST /heartbeat`

```json
{"board": "RSP_LH", "boot": "a91c", "fw": "1.1.3", "rssi": -58, "uptime_s": 3600, "pending": 0, "dropped": 0}
```

`dropped` (firmware 1.1.3 on) counts calls the board lost because its queue was full; the laptop logs any increase.

## Proposed addresses (IoT network, can be changed)

The plan. **Today the laptop, the HT813 and the boards are on the plant network, not isolated yet**
(see "Security").

| Device | Address |
|---|---|
| Network | 192.168.83.0/24, to be isolated from the plant LAN |
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
| `install/update.sh` | Fetch the newest version from GitHub, show the changes, ask, and run the installer. |
| `install/voices.sha256` | Checksums the voice lab installer checks the downloaded voices against. |
| `receiver/p833_audio.py` | Builds a page from the recorded sentences and tones. |
| `receiver/p833_tts.py` | Piper helper: runs Piper in its own environment; stays running to speak pages and the voice lab. |
| `receiver/p833_voice.py` | The page voice: saved settings, live speech, the kept audio and Make all messages. |
| `install/install_tts.sh` | Sets up the Voice lab: Piper and voices. |
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
