# Xiaomi Mi Gaming Laptop (TIMI TM1801) – GamingBox reverse-engineering notes

Source: Xiaomi's driver bundle (not included in this repo) → `GamingBox_Setup_1.2.3.1_20180706` (the actual control app) and
`MiService2_Setup_3.0.0.48` (just an updater/telemetry service — no hardware access).
The app runs as a 32-bit binary, `GamingBox.exe`. All hardware control lives in it,
in C++ classes `base::wmi::syncoperation::*`.

## Transport: one WMI data block

| Thing | Value |
|---|---|
| WMI class | `root\wmi : RW_TMAWMI` (property `BufferBytes`, `uint8[32]`) |
| GUID (on a TM1801) | `E2A89D40-784F-4E91-BE22-AE373CDEA97A`, object id `AA`, setable |
| ACPI device | `\_SB.MIAP` (PNP0C14:02, "Mi AP") |
| ACPI methods (per WMI-ACPI spec) | `\_SB.MIAP.WSAA(inst, buf32)` = write, `\_SB.MIAP.WQAA(inst)` = read |
| Event class | `TMA_WMIEvent` (property `EventDetail`) → most likely GUID `B74AF83F-8B2F-4069-ACAC-36D176F62FC0`, notify `0x80`, also on `\_SB.MIAP` |

How the app uses it: it runs `SELECT * from RW_TMAWMI`, sets `BufferBytes` to a
32-byte command and calls `PutInstance` (which becomes `WSAA`). To get a reply it
re-queries the instance and reads `BufferBytes` back (which becomes `WQAA`).

## 32-byte command buffer

Internally the app writes commands as ASCII hex tokens and converts them to
little-endian values:

```
off  size  field
0    u16   cmd     0xFA00 = read/get, 0xFB00 = write/set
2    u16   func    0x0300 = misc switches, 0x0102 = fan, 0x0100 = light effect, 0x0101 = light colour
4    u32   arg0
8    u32   arg1
12   u32   arg2
16   u32   arg3
20   u32   arg4
24   8     zero
```

Reply (read back via WQAA): same layout. The app checks that `u16 @0 == 0` (success).
For simple GETs the value is `u16 @2`.

## Commands recovered from the vtables (exact)

| Operation | cmd | func | arg0 | arg1 |
|---|---|---|---|---|
| GetTouchpadStatus | FA00 | 0300 | 0 | – |
| Enable/DisableTouchpad | FB00 | 0300 | 0 | 1 / 0 |
| GetFnKeyStatus (Fn-lock) | FA00 | 0300 | 1 | – |
| Enable/DisableFnKey | FB00 | 0300 | 1 | 1 / 0 |
| GetWindowsKeyStatus (Win-key lock) | FA00 | 0300 | 2 | – |
| Enable/DisableWindowsKey | FB00 | 0300 | 2 | 1 / 0 |
| GetPowerOnOffLedStatus | FA00 | 0300 | 4 | – |
| SetPowerOnOffLedStatus | FB00 | 0300 | 4 | 1 / 0 |
| GetFanStatus | FA00 | 0102 | – | – |
| SetFanStatus (**Turbo** on/off) | FB00 | 0102 | 1 / 0 | – |

GetFanStatus reply: `u16@0` status, `u16@2` fan mode (turbo flag), then four u32s at
offsets 4, 8, 12, 16. These feed three UI callbacks, probably fan speeds/levels (still unconfirmed).

## Lighting (layout exact, field meanings partly inferred)

`SetLightEffectForKeyboardForLightBar`: `FB00 0100`
- `arg0` = target device. Keyboard code passes **4**. The light-bar code passes a value
  from the UI, probably 1 and 2 for left and right (UI JSON uses `area: 1, 2`).
  It may be a bitmask: 1 = left bar, 2 = right bar, 4 = keyboard.
- `arg1` = `(p1 << 16) | (p2 << 8) | effect`. p1/p2 are most likely brightness and speed.
  The effect index comes from the UI: keyboard 0..2, light bar 0..3, where 3 = cycle through the colour list.

`SetColourForKeyboardForLightBar`: `FB00 0101`
- `arg0` = `(group << 16) | (n << 8) | index` (index is 1-based). The light bar uses group=1,
  n=number of colours. The keyboard uses group=0.
- `arg1` = `0x00RRGGBB`

`GetLedStatusForKeyboardForLightBar`: `FA00 0100`, `arg0` = device.

Sequence the app uses: set effect with effect=0, set each colour, check the reply,
then set the final effect.

UI defaults (from embedded JSON): keyboard has 4 colour zones, brightness 0–5, effects 0–2.
Light bars have up to 5 colours per cycle, with brightness and speed.

## Other hardware bits
- ITE `048d:8910` USB HID (vendor page 0xFF89, feature report 0x5A, 16 bytes) is the
  keyboard controller. The app enumerates HID devices (strings `quanta_KB`, `quanta_KBC`,
  `quanta_LB_L`, `quanta_LB_R`, `quanta_CP`, `quanta_CP_LB`), but lighting commands go through WMI, not raw HID.
- Keyboard macros/shortcuts use an embedded AutoHotkey DLL (pure software, so it's easy to replace on Linux).
- The GPU monitor uses `nvml.dll` (on Linux use `nvidia-smi`/NVML directly).

## Confirmed from the DSDT (`acpi/dsdt.dsl`, `\_SB.MIAP.WSAA`) and tested on hardware

Turbo on/off and all reads were tested on 2026-09-25 and returned status 0.

| func | Read (FA00) returns | Write (FB00) |
|---|---|---|
| 0x0100 | arg0→LEDZ; reply: RET2=LCAM, byte8=LETY, 9=LSPD, 10=LEBR (a read also writes LCAM=byte9!) | arg0→LEDZ (zone), byte8→LETY (effect), byte9→LSPD (speed), byte10→LEBR (brightness) |
| 0x0101 | – | byte4 = slot 1-8 → C0..C7, byte5→LCAM (colour count), bytes 8/9/10 → C?ZR/G/B |
| 0x0102 | u16@2=FANM, u32@4=fan1 rpm, @8=fan2 rpm, @12=CPU °C, @16=GPU °C | arg0 0/1 → FANM (turbo) |
| 0x0300 | arg0 selects: 0 TPON, 1 FNKY, 2 WINK, 3 ARPL(?), 4 PWLE; value in u16@2 | same arg0, arg1 = 0/1 |
| 0x0400 | u16@2=KBBL (**inverted: 1 = keyboard backlight off**, verified), u32@4=KBIT (16-bit) | arg0→KBBL (0/1), arg1→KBIT |
| 0x0500 | u16@2=ATFN (16-bit, unknown) | arg0 0/1 → ATFN |

The EC fields live in `EMEM` (SystemMemory 0xFE708000). The lighting block starts at +0xB00.
Keyboard areas A–D are LEDZ 4–7 (GamingBox writes zones 4,5,6,7, then the final effect on 4).
The rear bars are LEDZ 2 (left) and 3 (right).
UI effect names: Static, Breath, Wave, Colorful (→ LETY 0–3).
Colour byte order: GamingBox sends 0x00RRGGBB little-endian (byte8=B), although the EC
field names suggest byte8=R. Verify by eye.

## Keyboard lighting: verified on hardware (2026-09-25, `tools/kbdtest`)

- **The real sequence** (GamingBox.exe keyboard routine at 0x41b7e0, per-zone helper at 0x41c930):
  `effect(zone 4, LETY=0)`, then for each area: colours + `effect(zone, LETY=1)`,
  then `effect(zone 4, LETY=mode)` **only if mode > 1**. LETY 1 is a commit, not "Breath".
- **A bare LETY=0 write blacks the keyboard out**, and later writes and the brightness key
  don't bring it back. It recovers with a full begin → colour → commit sequence followed by
  a brightness-key press (or a power cycle). This is what "broke" the keyboard before.
- Zones 4, 5, 6, 7 = areas A–D, **left to right**.
- **Colour bytes: byte8 → CxZR is red** (the EC names are right; there's no R/B swap).
- The EC clears LEDZ inside the WSAA's own 60 ms sleep, so writes don't need extra pacing.
- **KBBL (FB00 0400) switches the backlight off/on** (1 = off) with the lighting intact. It
  worked in the GUI once the keyboard was in a sane state, but six toggles in the early
  power-on test changed nothing (KBBR read 0 then, yet the keys were lit), so it may not work
  when the chip's state is out of sync.
- **Keyboard LEBR is inverted: 0 = brightest, 4 = dimmest, 5 = off.** Every apply at
  "brightness 5" switched the light off, and that was the whole "goes dark" mystery.
  The driver now takes level 0 (off)–5 (brightest) and sends LEBR = 5 − level.
- Final LETY after the commits (areas blue/green/white/red, full brightness): 2 = each area
  breathes in its own colour, 3 = similar breathing (maybe a wave), 4 = the whole keyboard
  pulses in the **last area's** colour (red), 5 = no visible animation.
- LSPD for the keyboard: 0 = slowest … 4 = fastest (5 looked slower again).
- Stress: 20 `apply_keyboard` calls back to back (180 writes) ended lit with the right colours.
- KBBR is the live brightness (the Fn key cycles 5→4→3→2→1→0→5). An effect write copies LEBR into KBBR.
- EC lighting registers survive a shutdown. What a shutdown resets is the ITE chip.
- The ITE chip's HID feature report 0x5A reads `00 ff…` while lit (not informative yet).

## Rear light bars: status read + factory state (2026-09-25)

- **Reading a zone is safe**: GamingBox's GetLedStatus (0x419c20, used by 0x41cd20) sends
  `FA00 0100` with arg0 = **zone + 0x10** (the EC's read flag) and arg1 = 0, which returns
  LCAM (count) in u32@4 and LETY/LSPD/LEBR in bytes 8/9/10. Then one call per colour pair,
  arg1 = `(count << 8) | pair`, returns two colours at bytes 12–14 and 16–18 (R, G, B).
  `sudo miwmi light [ZONE ...]` does this.
- Factory state read on a TM1801 (bars visibly cycling 4 colours in a breathing pattern):

  | zone | LETY | LSPD | LEBR | colours |
  |---|---|---|---|---|
  | 0, 1 | 0 | 0 | 0 | none |
  | 2, 3 | 3 | 0 | 0 | E10000 0087FF 00FF14 FFAA00 |
  | 4 (keyboard) | 1 | 0 | 0 | E10000 |

  These are GamingBox's first bar preset (FF0000 0087FF 00FF14 FFAA00). Red FF reads back as E1,
  so the EC probably scales red.
- **GamingBox's bar routine (0x41b5a0)**: `effect(zone, LETY 0)`, then only if mode ≠ 0 the colours
  in group 1 (the whole list for mode 3, otherwise just the first), then `effect(zone, LETY = mode)`.
  There's **no LETY 1 commit** like the keyboard's. Its args: brightness → LEBR, speed → LSPD.
  Mode 0 never sends colours, so it's probably "off". GamingBox's bar page has no effect picker, only
  colours (≤ 5), brightness (5 steps) and speed (4 steps).
- **Verified with `kbdtest run bars` (2026-09-25):** LEDZ **2 = left bar, 3 = right bar**, and 1 shows
  nothing. LETY **0 = off, 1 = steady, 2 = breathing (one colour), 3 = cycle through the list**
  (the factory animation). LSPD 0 = slowest, and the effect gets faster with higher values.
- **The EC clamps bar LEBR and LSPD at 2**: writing 3–7 reads back as 2, and speeds 3 and 4 looked the
  same as 2. Brightness: 0 is brightest, and 2 is a little dimmer (subtle, `kbdtest run barbright`).
- After writes, zone 1 reads back the last write, so the status read is only a rough check.

## Still to confirm
- Which LETY value (>1) is Breath / Wave / Colorful on the keyboard (`kbdtest run effects`).
- What mode 3 is exactly (wave?), and GamingBox's UI-effect → mode mapping.
- Meaning of ARPL, ATFN and KBIT (KBIT: writing various values had no visible effect on the backlight).

## Macro keys (the 5 extra keys)
They don't send normal key codes, and the Windows app's `KeyboardMonitor.dll` hook only
blocks the Win keys (for Win-key lock). The EC raises query events instead:

| EC query | EVBF (EVT0, EVT1) |
|---|---|
| `_Q61`–`_Q65` | 0x0200, 1–5 |
| `_Q71`–`_Q75` | 0x0200, 6–10 = release of key 1–5 (verified 2026-09-25) |

Each one notifies `\_SB.MIAP` with 0x80, which is WMI event `B74AF83F-…`. The event data comes
from `\_SB.MIAP._WED(0x80)` (the 32-byte EVBF: u16 EVT0, u16 EVT1, u16 EVT2).
Other events on the same channel: 0x0300 (touchpad/Fn toggles, EVT2 = new state) and
0x0100 (keyboard brightness key, EVT1 = KBBR).
Linux has no driver for this GUID. `mikeysd` listens on ACPI netlink (the WMI core
still broadcasts the event), reads `_WED`, and emits uinput keys.
