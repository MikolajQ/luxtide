"""Shared test fixtures."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from luxtide import config  # noqa: E402

WARSAW = """\
[location]
latitude = 52.2297
longitude = 21.0122
timezone = Europe/Warsaw
"""


def make_config(extra="", base=WARSAW):
    return config.parse(base + extra)


def edid(manufacturer="TST", name="TESTMON", serial="SN123"):
    """A minimal valid EDID block with a name and a serial descriptor."""
    data = bytearray(128)
    data[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    code = 0
    for letter in manufacturer:
        code = (code << 5) | (ord(letter) - 64)
    data[8], data[9] = code >> 8, code & 0xFF
    data[10], data[11] = 0x34, 0x12

    def descriptor(offset, tag, text):
        block = bytearray(18)
        block[3] = tag
        payload = text.encode()[:13] + b"\x0a"
        block[5:5 + len(payload)] = payload
        for index in range(5 + len(payload), 18):
            block[index] = 0x20
        data[offset:offset + 18] = block

    descriptor(54, 0xFF, serial)
    descriptor(72, 0xFC, name)
    return bytes(data)
