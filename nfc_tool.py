#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nfc_tool.py - tools for the built-in NFC tag of HONOR MagicBook laptops.

Chip: Shanghai Fudan FM11NT082C, ACPI HID "NTAG0001", I2C address 0x57.
Reads work on every unit. Writes work only on the NTCZ variant (factory
static lock bytes 0000h); the NTCI variant (FFFFh) rejects all writes to
locked pages at the hardware level - this is one-time-programmable, not
a software protection.

Pure Python 3 stdlib (fcntl, os, argparse). No dependencies.

    sudo modprobe i2c-dev
    sudo python3 nfc_tool.py check
    sudo python3 nfc_tool.py read
    sudo python3 nfc_tool.py write-url https://example.com/ --backup before.bin
    sudo python3 nfc_tool.py restore before.bin

Keep phones away from the NFC antenna while running any command.
"""

import argparse
import fcntl
import os
import sys
import time
from pathlib import Path

I2C_SLAVE = 0x0703
CHIP_ADDR = 0x57
USER_BASE = 0x0010      # first byte of the Type 2 mapped user area
SIZE = 872              # user area bytes: CC says 0x6D * 8 = 872
END = USER_BASE + SIZE  # 0x0378 exclusive
CC_ADDR = 0x000C
CC_EXPECTED = bytes([0xE1, 0x10, 0x6D, 0x00])
LOCKS_ADDR = 0x000A     # static lock bytes (CL Static Lock Bytes)
LOCK_PROG_DELAY = 0.02  # datasheet program time is 10 ms; double it

# NFC Forum URI identifier codes ("URI RTD", prefix prepended to the payload)
URI_PREFIX = {
    0x00: "",
    0x01: "http://www.",
    0x02: "https://www.",
    0x03: "http://",
    0x04: "https://",
    0x05: "tel:",
    0x06: "mailto:",
    0x07: "ftp://anonymous:anonymous@",
    0x08: "ftp://ftp.",
    0x09: "ftps://",
    0x0A: "sftp://",
    0x0B: "smb://",
    0x0C: "nfs://",
    0x0D: "ftp://",
    0x0E: "dav://",
    0x0F: "news:",
    0x10: "telnet://",
    0x11: "imap:",
    0x12: "rtsp://",
    0x13: "urn:",
    0x14: "pop:",
    0x15: "sip:",
    0x16: "sips:",
    0x17: "tftp:",
    0x18: "btsp://",
    0x19: "btspp://",
    0x1A: "btl2cap://",
    0x1B: "btgoep://",
    0x1C: "tcpobex://",
    0x1D: "irdaobex://",
    0x1E: "file://",
    0x1F: "urn:epc:id:",
    0x20: "urn:epc:tag:",
    0x21: "urn:epc:pat:",
    0x22: "urn:epc:raw:",
    0x23: "urn:epc:",
    0x24: "urn:nfc:",
}


# ----------------------------------------------------------------- device ---

def open_device(path: str = None):
    """Open the i2c adapter that hosts the NTAG0001 ACPI device, bind to 0x57."""
    if path is None:
        try:
            node = Path("/sys/bus/i2c/devices/i2c-NTAG0001:00").resolve()
            path = "/dev/" + node.parent.name  # adapter hosting the NTAG0001
        except OSError:
            die("NTAG0001 ACPI device not found in sysfs; pass --device /dev/i2c-N")
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.ioctl(fd, I2C_SLAVE, CHIP_ADDR)
    except OSError as e:
        os.close(fd)
        die(f"cannot claim {path} for address 0x57: {e}")
    # serialize cooperating users of the adapter
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        die(f"another process is using {path}")
    # sanity: CC must read E1 10 6D 00
    cc = read_bytes(fd, CC_ADDR, 4)
    if cc != CC_EXPECTED:
        os.close(fd)
        die(f"CC mismatch at {CC_ADDR:04X}: got {cc.hex(' ')}; expected "
            f"{CC_EXPECTED.hex(' ')}. Is this really the NTAG0001? "
            "Another device on a shared bus may have answered.")
    return fd


# ----------------------------------------------------------------- I/O ------

def read_bytes(fd, addr: int, n: int) -> bytes:
    """Read n bytes: write the 2-byte big-endian pointer, then read (STOP
    separated). This is the transaction shape the Windows driver uses."""
    os.write(fd, addr.to_bytes(2, "big"))
    out = b""
    while len(out) < n:
        chunk = os.read(fd, n - len(out))
        if not chunk:
            die(f"short read at {addr:04X}")
        out += chunk
    return out


def read_bytes_combined(fd, addr: int, n: int) -> bytes:
    """Fallback: repeated-START combined transaction via I2C_RDWR."""
    import ctypes

    class i2c_msg(ctypes.Structure):
        _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                    ("len", ctypes.c_uint16), ("buf", ctypes.c_void_p)]

    class i2c_rdwr_ioctl_data(ctypes.Structure):
        _fields_ = [("msgs", ctypes.POINTER(i2c_msg)),
                    ("nmsgs", ctypes.c_uint32)]

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    b1 = ctypes.create_string_buffer(addr.to_bytes(2, "big"), 2)
    b2 = ctypes.create_string_buffer(n)
    arr = (i2c_msg * 2)(
        i2c_msg(CHIP_ADDR, 0, 2, ctypes.cast(b1, ctypes.c_void_p)),
        i2c_msg(CHIP_ADDR, 0x0001, n, ctypes.cast(b2, ctypes.c_void_p)),  # I2C_M_RD
    )
    d = i2c_rdwr_ioctl_data(ctypes.cast(arr, ctypes.POINTER(i2c_msg)), 2)
    if libc.ioctl(fd, 0x0707, ctypes.byref(d)) < 0:
        raise OSError(ctypes.get_errno(), "I2C_RDWR failed")
    time.sleep(0.01)
    return b2.raw


def verify_read(fd, addr: int, n: int) -> bytes:
    """Read with resilience: the chip can temporarily NAK even reads after
    write bursts; retry with pauses, then fall back to combined reads."""
    last = None
    for attempt in range(5):
        try:
            a = read_bytes(fd, addr, n)
            try:
                b = read_bytes_combined(fd, addr, n)
                if b != a:
                    # trust the split read (the vendor stack's shape) but warn
                    print(f"warn: split/combined read disagree at {addr:04X}: "
                          f"{a.hex()} vs {b.hex()}", file=sys.stderr)
            except OSError:
                pass
            return a
        except OSError as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise ChipLocked(f"reads keep failing at {addr:04X} after retries ({last}); "
                     "chip may be busy - wait 10-30 s and retry")


def write_block(fd, offset_in_area: int, data: bytes):
    """Write up to 16 bytes at USER_BASE+offset (offset 16-aligned).
    One transaction: [addr_hi, addr_lo] + data. Verify by readback.
    NEVER retry a failed write automatically."""
    assert offset_in_area % 16 == 0 and 0 < len(data) <= 16
    assert USER_BASE + offset_in_area + len(data) <= END
    packet = (USER_BASE + offset_in_area).to_bytes(2, "big") + data
    try:
        if os.write(fd, packet) != len(packet):
            raise OSError("short write")
    except OSError as e:
        raise ChipLocked(str(e)) from None
    time.sleep(LOCK_PROG_DELAY)
    actual = read_bytes(fd, USER_BASE + offset_in_area, len(data))
    if actual != data:
        raise RuntimeError(
            f"readback mismatch at {USER_BASE + offset_in_area:04X}; stopped")


class ChipLocked(RuntimeError):
    """Write NAKed by the chip (errno 121 / EIO)."""


# ----------------------------------------------------------------- NDEF -----

def read_area(fd) -> bytes:
    area = b""
    for off in range(0, SIZE, 16):
        area += verify_read(fd, USER_BASE + off, min(16, SIZE - off))
    return area


def parse_tlv(area: bytes):
    if area[0] != 0x03:
        return None, 0, b""
    if area[1] == 0xFF:
        start, ln = 4, int.from_bytes(area[2:4], "big")
    else:
        start, ln = 2, area[1]
    if ln == 0 or start + ln >= SIZE or area[start + ln] != 0xFE:
        return None, 0, b""
    return start, ln, area[start:start + ln]


def parse_records(msg: bytes):
    """Minimal NDEF record walker; returns list of (tnf, type, payload)."""
    recs, rest, ended = [], msg, False
    pos = 0
    while pos < len(rest):
        h = rest[pos]; pos += 1
        if ended or (h & 0x80 != 0) != (not recs) or h & 0x20 or (h & 7) >= 6:
            raise ValueError("unsupported NDEF flags or chunking")
        tl = rest[pos]; pos += 1
        pl = rest[pos]; pos += 1
        if h & 0x10 == 0:
            pl = int.from_bytes(rest[pos - 1:pos + 3], "big"); pos += 3
        il = rest[pos] if h & 0x08 else 0
        if h & 0x08:
            pos += 1
        typ = rest[pos:pos + tl]; pos += tl + il
        payload = rest[pos:pos + pl]; pos += pl
        recs.append((h & 7, typ, payload))
        ended = bool(h & 0x40)
    if not ended:
        raise ValueError("NDEF message missing final record")
    return recs


def describe(area: bytes) -> str:
    start, ln, msg = parse_tlv(area)
    if msg is None:
        return "  no valid NDEF TLV (empty or corrupt)"
    lines = [f"  NDEF message: {ln} bytes"]
    try:
        recs = parse_records(msg)
    except (ValueError, IndexError) as e:
        return lines[0] + f"  (parse error: {e})"
    for tnf, typ, payload in recs:
        if tnf == 1 and typ == b"U":
            code = payload[0] if payload else 0
            url = URI_PREFIX.get(code, f"<0x{code:02X}:>") + payload[1:].decode(
                "utf-8", "replace")
            lines.append(f"  URI   : {url}")
        elif tnf == 1 and typ == b"T":
            status = payload[0]
            lang_n = status & 0x3F
            text = payload[1 + lang_n:].decode("utf-8", "replace")
            lines.append(f"  TEXT  : {text!r}")
        elif tnf == 2:
            lines.append(f"  MIME  : {typ.decode('utf-8', 'replace')} "
                         f"({len(payload)} bytes)")
        else:
            lines.append(f"  TNF {tnf}  type={typ!r}  ({len(payload)} bytes)")
    return "\n".join(lines)


# ---------------------------------------------------------------- images ---

def uri_image(url: str) -> bytes:
    if not (url.startswith("https://") or url.startswith("http://")) \
            or any(ord(c) < 0x20 for c in url):
        die("expected an http(s) URL without control characters")
    if len(url) > SIZE:
        die("URL exceeds tag capacity")
    if url.startswith("https://www."):
        code = 0x02; rest = url[12:]
    elif url.startswith("http://www."):
        code = 0x01; rest = url[11:]
    elif url.startswith("https://"):
        code = 0x04; rest = url[8:]
    elif url.startswith("http://"):
        code = 0x03; rest = url[7:]
    else:
        code = 0x00; rest = url
    payload = bytes([code]) + rest.encode()
    ndef = bytes([0xD1, 0x01, len(payload)]) + b"U" + payload  # short record
    area = bytes([0x03, len(ndef)]) + ndef + b"\xFE"
    if len(area) > SIZE:
        die("URL exceeds tag capacity")
    return area.ljust(SIZE, b"\x00")


def text_image(text: str, lang: str = "en") -> bytes:
    if not 0 < len(lang) <= 0x3F:
        die("language code must be 1..63 chars")
    payload = bytes([len(lang)]) + lang.encode() + text.encode()
    if len(payload) > 255:
        die("text too long for a single short record")
    ndef = bytes([0xD1, 0x01, len(payload)]) + b"T" + payload
    area = bytes([0x03, len(ndef)]) + ndef + b"\xFE"
    if len(area) > SIZE:
        die("text exceeds tag capacity")
    return area.ljust(SIZE, b"\x00")


# ---------------------------------------------------------------- ops ------

def snapshot(fd) -> bytes:
    a = read_area(fd)
    if read_area(fd) != a:
        die("data changed during read; keep phones away from the antenna")
    return a


def check(fd):
    print("== NTAG0001 / FM11NT082C check ==")
    print(f"CC @ {CC_ADDR:04X}: {read_bytes(fd, CC_ADDR, 4).hex(' ')}")
    head = verify_read(fd, 0x0000, 16)
    print(f"block 0     : {head.hex(' ')}")
    locks = head[LOCKS_ADDR:LOCKS_ADDR + 2]
    # block-0 read already contains 0x0A/0x0B
    print(f"static locks: {locks.hex(' ')} @ {LOCKS_ADDR:04X}")
    if locks == b"\xff\xff":
        variant = "NTCI"
        verdict = ("FACTORY-LOCKED variant. Static lock bits are one-time-"
                   "programmable and were burned at the factory; every write "
                   "to a locked page is NAKed in hardware. No software can "
                   "unlock this unit.")
    elif locks == b"\x00\x00":
        variant = "NTCZ"
        verdict = ("unlocked variant: user area should be writable "
                   "(write-url should work).")
    else:
        variant = "?"
        verdict = ("non-default lock bytes; consult the FM11NT082C datasheet "
                   "before any write.")
    print(f"variant     : {variant} (order number FM11NT082C-{variant or '?'}-...)")
    print(f"verdict     : {verdict}")
    area = snapshot(fd)
    print(f"user area   : {USER_BASE:04X}..{END - 1:04X} ({SIZE} bytes)")
    print(describe(area))


def cmd_read(args):
    fd = open_device(args.device)
    try:
        area = snapshot(fd)
    finally:
        os.close(fd)
    print(describe(area))
    if args.out:
        Path(args.out).write_bytes(area)
        print(f"saved 872 user-area bytes -> {args.out}")


def load_expected(path: str) -> bytes:
    b = Path(path).read_bytes()
    if len(b) != SIZE or b[0] != 0x03:
        die(f"{path}: expected an 872-byte user-area image starting with "
            "NDEF TLV 0x03")
    return b


def apply_image(fd, expected: bytes, desired: bytes):
    if snapshot(fd) != expected:
        die("current content differs from the backup; refusing to write. "
            "Make a fresh backup first.")
    if expected == desired:
        print("already identical; no writes")
        return
    # publish length last: blank the TLV length first
    head = bytearray(desired[:16]); head[1] = 0
    write_block(fd, 0, bytes(head))
    for off in range(16, SIZE, 16):
        end = min(off + 16, SIZE)
        if expected[off:end] != desired[off:end]:
            write_block(fd, off, desired[off:end])
    write_block(fd, 0, desired[:16])          # publish
    if snapshot(fd) != desired:
        die("full-area verification failed")
    print("written and verified 872 user bytes; confirm with a phone")


def cmd_write_url(args):
    fd = open_device(args.device)
    try:
        if args.backup and not Path(args.backup).exists():
            Path(args.backup).write_bytes(snapshot(fd))
            print(f"backup saved -> {args.backup}")
        expected = load_expected(args.backup) if args.backup else snapshot(fd)
        apply_image(fd, expected, uri_image(args.url))
    except ChipLocked as e:
        die(f"chip NAKed the write ({e}). This is the NTCI factory-locked "
            "variant (static locks FFFFh, one-time-programmable) - see "
            "README 'NTCI vs NTCZ'. Nothing software-side can lift this.")
    finally:
        os.close(fd)


def cmd_write_text(args):
    fd = open_device(args.device)
    try:
        if args.backup and not Path(args.backup).exists():
            Path(args.backup).write_bytes(snapshot(fd))
            print(f"backup saved -> {args.backup}")
        expected = load_expected(args.backup) if args.backup else snapshot(fd)
        apply_image(fd, expected, text_image(args.text, args.lang))
    except ChipLocked as e:
        die(f"chip NAKed the write ({e}). See README 'NTCI vs NTCZ'.")
    finally:
        os.close(fd)


def cmd_restore(args):
    fd = open_device(args.device)
    try:
        expected = snapshot(fd)
        apply_image(fd, expected, load_expected(args.image))
    except ChipLocked as e:
        die(f"chip NAKed the write ({e}). See README 'NTCI vs NTCZ'.")
    finally:
        os.close(fd)


# ---------------------------------------------------------------- cli ------

def die(msg: str):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    p = argparse.ArgumentParser(
        description="HONOR MagicBook built-in NFC tag (FM11NT082C / NTAG0001)")
    p.add_argument("--device", help="i2c adapter, e.g. /dev/i2c-0 "
                                    "(default: auto via ACPI NTAG0001:00)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="identify chip variant (NTCI/NTCZ) + show NDEF")

    r = sub.add_parser("read", help="read user area, decode NDEF")
    r.add_argument("--out", help="save raw 872-byte user area to file")

    w = sub.add_parser("write-url", help="write an https:// URL as NDEF URI")
    w.add_argument("url")
    w.add_argument("--backup", help="expected-state image; auto-created if missing")

    t = sub.add_parser("write-text", help="write an NDEF text record")
    t.add_argument("text")
    t.add_argument("--lang", default="en")
    t.add_argument("--backup")

    rp = sub.add_parser("restore", help="restore an 872-byte user-area image")
    rp.add_argument("image")

    a = p.parse_args()
    try:
        if a.cmd == "check":
            fd = open_device(a.device)
            try:
                check(fd)
            finally:
                os.close(fd)
        elif a.cmd == "read":
            cmd_read(a)
        elif a.cmd == "write-url":
            cmd_write_url(a)
        elif a.cmd == "write-text":
            cmd_write_text(a)
        elif a.cmd == "restore":
            cmd_restore(a)
    except ChipLocked as e:
        die(str(e))


if __name__ == "__main__":
    main()
