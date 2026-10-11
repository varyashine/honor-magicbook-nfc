[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md)

# honor-magicbook-nfc

Everything I learned about the NFC tag built into HONOR MagicBook laptops,
plus a small tool that works with it on Linux. The chip is a Shanghai Fudan
**FM11NT082C**, it shows up in ACPI as `NTAG0001` and sits on I²C address
`0x57`.

The tool has no dependencies. Plain Python 3 standard library, fcntl and
ctypes only.

## Where the NFC module actually is

Dead center of the touchpad. Not near the palm rest, not in the keyboard
deck corners. Put your phone on the center of the touchpad and it will
catch the tag.

## What the tag contains from the factory

```
0000: 1d 19 db 57 3c 07 00 10  2b b0 ff ff e1 10 6d 00   lock ff ff @0x0A, CC @0x0C
0010: 03 8c 91 01 33 55 04 77  77 77 2e 68 69 68 6f 6e   NDEF TLV, URI record
0020: 6f 72 2e 63 6f 6d 2f 67  6c 6f 62 61 6c 2f 73 75   ...
0048: MIME record application/vnd.huawei.handoff.ndfmsg + 44-byte token
```

Two NDEF records: a URI pointing at
`https://www.hihonor.com/global/support/magic_link_welcome/` and a MIME
record `application/vnd.huawei.handoff.ndfmsg` that the Honor One-Tap
handoff feature rotates. Phones also report the "UID" as
`1D:19:DB:3C:07:00:10`, which is really just the first bytes of the EEPROM
block 0, the chip hands out the head of its memory as the UID.

## What worked

- Reading the whole user area over I²C, reliably, on Linux, with no
  vendor software. Two transaction shapes both work: write a 2-byte
  big-endian pointer then read, or a combined repeated-START transaction.
- Backing up the 872-byte user area (`0x0010` to `0x0377`).
- Decoding the NDEF (URI with prefix codes, text and MIME records).
- Writing to the pages the factory left unlocked, which is the handoff
  token area around `0x0070` to `0x008D`. That is all Honor's own software
  ever needs to rotate, which explains the weird "half writable" map.
- Identifying the exact chip variant from lock bytes, see below.

## What did not work, on my unit, at all

The laptop I did this on is a MagicBook 14 Pro 2023, the **"Only for
China Mainland"** edition: RTX 3050, i5, 16 GB + 1 TB, SKU `GLO-G561`
(DMI product name `GLO-GXXX`). On that unit:

- **Every I²C write to the NDEF area is NAKed.** Byte-exact factory data,
  16-byte block shape copied from the vendor driver, 150+ attempts with a
  phone actively reading the tag over RF during the writes. Nothing. The
  chip refuses in hardware, error 121 on the bus.
- Writing to the config registers (`0x0388` dyVal, `0x038C` fdMode) is
  NAKed too.
- **Writing over the phone fails as well.** Android NFC Tools reads the
  tag fine (it reports Type A and ISO-DEP, in other words a Type 4 tag)
  and reports "WRITABLE: YES", which is a lie. Every write attempt ends in
  a write error.
- iPhones ignore the tag completely, no app can even see it.
- Going through the Windows driver does not help either. I reverse engineered
  `SpbNfcDriver.sys` and its write dispatcher (`0x140003160` in Ghidra)
  verifies SHA-256 digests of the data you write. All four embedded
  digests cracked: they are SHA-256 of pieces of the factory URL
  (`www.hihon`, `or.com/global/su`, `pport/magic_link`, `_welcome/`).
  In other words the driver only lets the factory URL through and cannot
  write any other address, even to a chip that would accept it.
- Chasing the wrong datasheet wastes a day. FM11NT021 is a contactless
  Type 2 tag with 144 bytes and no I²C. It is not this chip, even though
  forum posts and ACPI strings make it tempting.
- After bursts of write attempts the chip wedges for a while and NAKs even
  reads. It recovers after 10 to 30 seconds. The tool waits and retries
  for this reason.
- A side effect: a probe write of mine landed a `0x58` byte inside the
  factory URL, so phones now read
  `https://www.hihonor.com/global/suXport/magic_link_welcome/`. I restored
  every page from backup except that one, because the page stopped
  accepting writes after the burst. A reboot might let
  `python3 nfc_tool.py restore` fix it. Not a big loss for me, since
  replacing that URL was the goal anyway.

## The verdict, and why your machine might be different

The FM11NT082C datasheet has the answer in its ordering table:

| Suffix | Factory static lock bits | I²C writes to user area |
|---|---|---|
| **NTCZ** | `0000h` | work |
| **NTCI** | `FFFFh` | NAK forever |

Lock bits are one-time programmable. They can be set, they cannot be
cleared, that is the entire point of them. Both variants evidently ended
up in Honor laptops; which one you get likely depends on the model or
batch.

My lock bytes at `0x000A` read `ff ff`. That is NTCI: the bits are set at
the factory and cannot be cleared by software.

[ZhuRuoLing/magicbookartd](https://github.com/ZhuRuoLing/magicbookartd)
implements `nfc write-url` for the MagicBook Art 14, and according to its
author it works there. Its docs name the chip as FM11NT082C-NTCZ. The tool
in this repo implements the same safe write procedure, so if your `check`
says NTCZ, there is a real chance it just works for you.

## Quick start

```sh
sudo modprobe i2c-dev
python3 nfc_tool.py check                  # variant + NDEF contents
python3 nfc_tool.py read --out backup.bin  # dump the 872-byte user area
```

Writes, NTCZ units only:

```sh
python3 nfc_tool.py write-url https://example.com --backup before.bin
python3 nfc_tool.py write-text "hello" --lang en
python3 nfc_tool.py restore before.bin     # roll back
```

The write procedure is the careful one: verify the CC, back up, zero the
TLV length, rewrite changed 16-byte blocks, publish by writing the first
block last, then verify everything. Keep phones away from the touchpad
while any command runs.

If you own an NTCI unit like mine and want a custom URL when tapping the
laptop, the realistic answer is a 50 cent NTAG213 sticker in the center of
the touchpad. Write it from any Android with NFC Tools or from this same
laptop with a cheap USB reader. It coexists with the built-in tag fine,
phones read whichever is closer to the coil.

## Compatibility

| Machine | Chip | Variant | Custom NDEF write |
|---|---|---|---|
| HONOR MagicBook 14 Pro 2023 (Only for China Mainland, GLO-G561) | FM11NT082C | NTCI | no, hardware OTP |
| HONOR MagicBook Art 14 (MRA-XXX) | FM11NT082C | NTCZ | yes, see magicbookartd |
| everything else | ? | run `check` | ? |

## Protocol notes

The full memory map, transaction shapes and the Windows driver analysis
live in [docs/PROTOCOL.md](docs/PROTOCOL.md) and
[docs/RESEARCH.md](docs/RESEARCH.md) (both in Russian). Short version:

- User area `0x0010` to `0x0377`, 872 bytes, NDEF TLV `03 8c` at `0x0010`.
- CC at `0x000C` must read `E1 10 6D 00`, that is also the sanity check
  that you are talking to the right chip on the shared bus. An HID device
  hangs on the same bus at `0x2B`, leave it alone.
- Static lock bytes at `0x000A`, registers from `0x0388`, fdMode byte at
  `0x038C` with factory value `06`.
- Writes are one transaction `[addr_hi, addr_lo]` plus up to 16 bytes,
  16-byte aligned. Datasheet program time is 10 ms, the tool waits 20 ms
  and reads back. Never blindly retry a failed write.

## Credits

- [ZhuRuoLing/magicbookartd](https://github.com/ZhuRuoLing/magicbookartd),
  the Rust daemon for the MagicBook Art 14. Its driver reverse engineering
  matches mine byte for byte, and it is what pointed me to the NTCZ/NTCI
  difference.
- FM11NT082C datasheet (Chinese) and FM11NC08 product brief (English),
  published by [Shanghai Fudan Microelectronics](https://www.fmsh.com/).

## License

MIT, see [LICENSE](LICENSE).

## Search keywords

FM11NT082C, FM11NC08, FM11NT021, NTAG0001, Fudan, Shanghai Fudan
Microelectronics, 复旦微, Honor MagicBook NFC, MagicBook 14 Pro, MagicBook
Art 14, NTCI, NTCZ, static lock bytes, OTP lock, NDEF write over I2C,
errno 121 Remote I/O error, SpbNfcDriver.sys, nfc_midware.dll, one-tap
handoff, 一碰传.
