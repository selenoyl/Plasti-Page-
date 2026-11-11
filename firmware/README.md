# P833 Cell Board Firmware

By Ethan Brady & Ryan Raikes

Firmware for the **Waveshare ESP32-S3-ETH-8DI-8RO** board at each cell. The
board reads the PLC's call-for-help pulses on **DI1**, decodes the code and
press number, and sends the call to the P833 receiver laptop over **Wi-Fi**.
One firmware serves every board; each board gets its own ID over USB.

```
PLC slot 2 OUT14 (24 V sourcing) --> DI1 --> decode pulses --> Wi-Fi --> laptop :8833
```

## What it does

- **Decodes the PLC's pulses.** Four pulses with 500 ms off between them. Pulse 1 is the code (500 + 500 × code ms) and pulses 2 to 4 are the press digits (500 + 100 × digit ms). Each width is accepted within ±40 ms.
- **Throws bad messages away.** A partial message (the PLC went to program mode or faulted), a bad width, or an input stuck on is ignored and logged.
- **Never misses a pulse.** The input is read every 1 ms by its own task, so a slow network send can't interfere.
- **Keeps calls until they're delivered.** It holds up to 10 calls and resends each until the laptop answers OK (every 2 s, slowing to every 10 s). The laptop drops repeats, so a resend never double-pages.
  - Each call carries how long it waited, so the laptop logs the real call time.
  - If Wi-Fi is down, calls wait and go out when it comes back.
  - Calls are kept in memory only, so a call waiting while the board loses power is lost.
- **Heartbeat every 60 s,** so the laptop shows the board as OK and its Wi-Fi signal.
- **Status LED:**

  | Colour | Meaning |
  |---|---|
  | green | Wi-Fi OK, nothing waiting |
  | blue | pulses coming in from the PLC |
  | yellow, blinking | a call waiting to be sent |
  | red, blinking | no Wi-Fi, Wi-Fi not set, or an error (see `show`) |

- **Buzzer:** a short chirp when a call is decoded. Turn it off with `beep off`.
- **Web pages on the board** (port 80, over Wi-Fi):
  - `http://<board IP>/` is a status page: ID, firmware, Wi-Fi signal, calls waiting and last error. No secrets are shown.
  - `http://<board IP>/update` updates the firmware over the network (see "Update over the network").

## Board facts (from Waveshare's wiki and demo code)

| Item | Value |
|---|---|
| Power | 7 to 36 V DC on the "DC 7~36V − +" terminal, or 5 V over USB-C |
| DI1 to DI8 | GPIO4 to GPIO11, through bidirectional optocouplers |
| Input level | Read with the pull-up on. An energised input reads **LOW** (Waveshare's demo treats LOW as "on" by default). |
| RGB LED | WS2812 on GPIO38 (green, red, blue order) |
| Buzzer | passive, GPIO46 |
| Arduino | esp32 board package **3.0.0 or newer** |

## Wiring (each cell)

| From | To | Note |
|---|---|---|
| PLC slot 2 **OUT14** (1769-OB16P, 24 V sourcing) | board **DI1** | the call signal |
| PLC 24 V supply **0 V** (the output card's DC common) | board **COM** (next to DGND) | **COM to the supply's negative = PNP / sourcing input, active high, 5 to 36 V** |
| 24 V DC **+** | board **DC 7~36V +** | **use a 24 V that stays on with the e-stop pressed**, like the output card's, so a call still goes out during an e-stop |
| 24 V DC **0 V** | board **DC 7~36V −** | |
| — | DGND | not used (it's for dry contacts) |

- The COM wiring comes from Waveshare's wiki for their Modbus 8-channel I/O module, which uses the same "DI COM / DGND" terminals and input design: "COM connected to the negative pole of the power supply: high-level active, PNP type wet contact active input, 5 V to 36 V DC". This board's own wiki doesn't describe it, so **confirm it with `watch` on the bench** (setup step 5) before relying on it.
- Keep the Wi-Fi antenna on the SMA connector and outside any metal enclosure if you can.

**If a call from the panel shows nothing in `watch`** (as happened on the first cell):
1. Jumper 24 V to DI1 to prove the input works.
2. Force `Local:2:O.Data.14` on in Studio 5000 to prove the wire is on the output the program uses.
3. Look at the card door label: outputs count from 0, so **OUT14 is the 15th terminal**.

## Flash it (once per board)

1. Install the **Arduino IDE** (2.x).
2. Go to Tools > Board > Boards Manager, search "esp32", and install **esp32 by Espressif Systems**, version **3.0.0 or newer**.
3. Open `firmware/p833_board/p833_board.ino`. The folder also holds `p833_decoder.h`, which opens as a tab.
4. Choose these settings under Tools:
   - Board: **ESP32S3 Dev Module**
   - **USB CDC On Boot: Enabled**. Without it, the setup console stays silent.
   - **Partition Scheme: Minimal SPIFFS (1.9MB APP with OTA/190KB SPIFFS)**. This leaves room for network updates; use the same setting every time.
   - Port: the board's COM port
5. Plug the board in by USB-C and click **Upload**.
   - If the upload fails, hold **BOOT**, press and release **RESET**, then release **BOOT**, and upload again (from Waveshare's FAQ).

No extra libraries are needed.

## Set it up (over USB)

Open the **Serial Monitor**: 115200 baud, line ending **Newline**. Type `help` to see every command. For RSP LH, for example:

```
id RSP_LH
ssid <IoT Wi-Fi name>
pass <IoT Wi-Fi password>
server 192.168.83.10:8833
key <the board key>
ip 192.168.83.101
gw 192.168.83.1
mask 255.255.255.0
reboot
```

1. **ID.** `id` must match the board ID in the receiver's board list (`RSP_LH`, `RSP_RH`, `FSP_LH`, `FSP_RH`). An unknown ID still pages, and its row on the admin page gets an **Add** button.
2. **Key.** `key` is `P833_BOARD_KEY` from the laptop. Run `sudo grep BOARD_KEY /etc/p833-receiver/secrets.env` there.
3. **Address.** For a fixed address, set `ip`, `gw` and `mask` (and `dns` if needed). Or use `ip dhcp`.
4. **Check.** After `reboot`, type `show`. Wi-Fi should say **connected**, and the LED should turn green within a few seconds.
5. **Check the input with `watch`** (do this before connecting to a live cell). Turn the PLC output on and off, either by forcing it in the PLC or with a 24 V test lead. The console should print `input ON` and `input off` to match.
   - If it's backwards, type `active high`, then `reboot`.
   - If nothing changes, check the wiring, and that `dipin` is 4 (DI1).
   - Press any key to stop `watch`.
6. **Test the path to the laptop** without the PLC: type `test 2 27`. The laptop's status page should show the call, from this board, as code 2, press 27.
7. **Test from the PLC.** Make a call from the cell's panel. The LED turns blue during the pulses, the buzzer chirps, and `CALL code ... queued` then `sent` appear.
8. **Set an update password:** `otapass <password>`, at least 8 characters. Keep it with the laptop's passwords. Until it's set, network updates are off.

## Update over the network

After the first USB upload, later versions can go in over Wi-Fi from any browser on the IoT network, for example on the laptop.

1. Make the file. In the Arduino IDE, with the same Tools settings as above, choose **Sketch > Export Compiled Binary**.
   - Use the file named `p833_board.ino.bin` in the sketch folder's `build` subfolder.
   - Don't use the `.bootloader.bin` or `.merged.bin` files.
2. Open `http://<board IP>/update` and log in. The user name is `p833` and the password is the board's `otapass`.
3. Choose the `.bin` file and click **Upload and restart**. After about 15 s, check the version on `http://<board IP>/`.

- If a call is waiting to be sent, the board refuses the update ("a call is waiting"). Try again a minute later.
- A failed or wrong upload leaves the old firmware running.
- Settings (ID, Wi-Fi, key and so on) are kept through an update.

Settings stay in the board's flash through power cuts and reflashing. To start clean, erase the flash when uploading (Tools > Erase All Flash Before Sketch Upload > Enabled).

## Commands

| Command | What it does |
|---|---|
| `show` | Settings, Wi-Fi, calls waiting, last error |
| `id`, `ssid`, `pass`, `server`, `key` | Board ID, Wi-Fi, receiver address, board key |
| `ip dhcp` / `ip <address>`, `gw`, `mask`, `dns` | Network address |
| `dipin <gpio>` | Input pin (default 4 = DI1) |
| `active low` / `high` / `auto` | Which level means "PLC output on" (default low, as on this board) |
| `beep on` / `off` | Chirp when a call is decoded |
| `otapass <password>` | Password for the update page; `otapass` with nothing turns updates off |
| `watch` | Show the input live |
| `test <code> <press>` | Send a test call |
| `heartbeat` | Send a heartbeat now |
| `reboot` | Restart (needed after network or input changes) |

## Tested so far, and how

Without the real board (the ESP32 build tools can't be downloaded in the build session):

- **The decoder, on a PC:** 1,240 messages covering every code, presses 0 to 999, ±20 ms PLC timing jitter, ±38 ms at the edge of the tolerance, 2 ms glitches, bad widths, a partial message, an input stuck on, the input on at power-up, and two calls 5 s apart. All passed.

  ```bash
  g++ -std=c++11 -Wall -I firmware/p833_board firmware/test/test_decoder.cpp -o /tmp/td && /tmp/td
  ```

- **The whole firmware, on a PC, against the real receiver:** the firmware runs with stand-ins for the ESP32 libraries (`firmware/test/sim`). The test checks that:
  - a call made while the receiver was off was resent until it arrived
  - a serial `test` call went through
  - a call made during a Wi-Fi drop waited and went out after it
  - a partial message was thrown away
  - a heartbeat arrived
  - each call arrived exactly once, in order

  ```bash
  python3 firmware/test/run_sim_test.py
  ```

**Not yet done:**
- Compiling with the real ESP32 tools, and running on the board. The first Arduino IDE upload and the bench steps above (5 to 8) are that check.
- The web status and update pages were compiled on the PC but not run there. Test the update page once on the bench by uploading the same version again.
