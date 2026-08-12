"""Compare two screenshots, as pixels.

Exists because asking the *canvas* whether it changed is not the same question
as asking whether the screen changed, and the two disagree in ways that matter.
A WebGL canvas without preserveDrawingBuffer reads back empty, a canvas tainted
by a cross-origin image refuses to be read at all, and anything drawn outside
the canvas — a DOM score, an overlay — never appears in the readback. In each
case the canvas says "nothing changed" about a picture that plainly did.

The screenshots have no such gaps: they are what was composited, which is what a
person looking at the screen would see. So they are the authority, and the
canvas readback is the cheap hint.

No PIL and no numpy — neither is a dependency of trance and neither is worth
becoming one for this. PNG is a simple enough format to read directly, and the
images are a few hundred pixels square.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

#: Bytes per pixel for the colour types Chrome emits from captureScreenshot.
#: 6 = RGBA, 2 = RGB. Anything else (palette, greyscale, 16-bit) we decline to
#: decode rather than guess at.
CHANNELS = {6: 4, 2: 3}

#: Above this, comparing every pixel in Python starts costing more than the
#: answer is worth, so every Nth row is sampled instead. A moving starfield
#: differs on far more than one row in ten.
FULL_COMPARE_PIXELS = 2_000_000
ROW_STRIDE_WHEN_LARGE = 4


class Undecodable(RuntimeError):
    """Not a PNG we can read. The caller falls back to comparing bytes."""


@dataclass
class Diff:
    """How two screenshots relate."""

    identical: bool
    #: None when only the bytes were compared, so no count exists.
    differing: int | None = None
    total: int | None = None
    #: "pixels" — decoded and counted. "bytes" — byte-compared only.
    how: str = "pixels"
    note: str = ""
    #: Where the change sits: (x, y, w, h) around every differing pixel, with
    #: the frame size beside it. A HUD tick is a 40x12 patch; the scene moving
    #: is most of the frame — the location is the difference between them.
    box: tuple | None = None
    width: int = 0
    height: int = 0

    @property
    def fraction(self) -> float:
        if not self.total:
            return 0.0
        return (self.differing or 0) / self.total

    def describe(self) -> str:
        """One sentence, for an agent that has to act on it."""
        if self.how == "bytes":
            return ("The two screenshots are byte-for-byte identical."
                    if self.identical else
                    "The two screenshots differ (compared as encoded bytes; they could "
                    "not be decoded for a pixel count).")
        if self.identical:
            return f"The two screenshots are identical — every one of {self.total} pixels matches."
        percent = self.fraction * 100
        # Spelled out because "0.3% of pixels differ" is the difference between
        # "the app is frozen" and "one sprite moved", and an agent told only
        # "changed" cannot tell those apart.
        where = ""
        if self.box and self.width and self.height:
            x, y, w, h = self.box
            share = (w * h) / (self.width * self.height)
            if share <= 0.25:
                where = (f" All of it inside one {w}x{h} region at ({x}, {y}) — "
                         f"a patch of the screen, not the scene moving.")
            elif share >= 0.7 and self.fraction >= 0.05:
                where = " The changes span most of the frame."
        return (f"The two screenshots differ in {self.differing} of {self.total} pixels "
                f"({percent:.2f}%).{where}")


def _decode(png: bytes) -> tuple[int, int, int, bytearray]:
    """(width, height, bytes-per-pixel, raw pixels) from a PNG."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise Undecodable("not a PNG")
    pos, width, height, depth, colour = 8, 0, 0, 0, 0
    idat = bytearray()
    while pos + 8 <= len(png):
        (length,), kind = struct.unpack(">I", png[pos:pos + 4]), png[pos + 4:pos + 8]
        body = png[pos + 8:pos + 8 + length]
        pos += 12 + length                       # length + type + data + crc
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            if body[12] != 0:                    # interlaced
                raise Undecodable("interlaced PNG")
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if depth != 8 or colour not in CHANNELS:
        raise Undecodable(f"unsupported PNG: depth {depth}, colour type {colour}")

    step = CHANNELS[colour]
    stride = width * step
    raw = zlib.decompress(bytes(idat))
    if len(raw) < (stride + 1) * height:
        raise Undecodable("truncated image data")

    # Undo the per-scanline filters. This is the whole of PNG decoding that is
    # not zlib, and each filter refers to the pixel left of it and the row above.
    out = bytearray(stride * height)
    prev = bytearray(stride)
    at = 0
    for row in range(height):
        filt = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        if filt == 1:                                            # Sub
            for i in range(step, stride):
                line[i] = (line[i] + line[i - step]) & 0xFF
        elif filt == 2:                                          # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:                                          # Average
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filt == 4:                                          # Paeth
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                up = prev[i]
                upleft = prev[i - step] if i >= step else 0
                guess = left + up - upleft
                dl, du, dul = abs(guess - left), abs(guess - up), abs(guess - upleft)
                near = left if (dl <= du and dl <= dul) else (up if du <= dul else upleft)
                line[i] = (line[i] + near) & 0xFF
        elif filt != 0:
            raise Undecodable(f"unknown scanline filter {filt}")
        out[row * stride:(row + 1) * stride] = line
        prev = line
    return width, height, step, out


def compare(before: bytes, after: bytes) -> Diff:
    """Whether two screenshots show the same thing, and by how much they do not.

    Never raises. An image that cannot be decoded still gets an answer — the
    encoded bytes of the same pixels from the same encoder are the same bytes,
    so byte equality is a sound, if coarse, test.
    """
    if not before or not after:
        return Diff(identical=False, how="bytes", note="one side was not captured")
    if before == after:
        # Same bytes is same picture; no point decoding to confirm it.
        return Diff(identical=True, differing=0, how="bytes")
    try:
        w1, h1, step1, pixels1 = _decode(before)
        w2, h2, step2, pixels2 = _decode(after)
    except (Undecodable, zlib.error, struct.error) as exc:
        return Diff(identical=False, how="bytes", note=str(exc))

    if (w1, h1) != (w2, h2):
        return Diff(identical=False, how="pixels", total=w1 * h1,
                    differing=w1 * h1,
                    note=f"the canvas resized, {w1}x{h1} to {w2}x{h2}")
    if step1 != step2:
        return Diff(identical=False, how="bytes", note="different pixel formats")

    stride = w1 * step1
    rows = range(0, h1, ROW_STRIDE_WHEN_LARGE if w1 * h1 > FULL_COMPARE_PIXELS else 1)
    differing = counted = 0
    min_x = min_y = max_x = max_y = None
    for row in rows:
        base = row * stride
        line1 = pixels1[base:base + stride]
        line2 = pixels2[base:base + stride]
        counted += w1
        if line1 == line2:
            continue
        if min_y is None:
            min_y = row
        max_y = row
        for x in range(0, stride, step1):
            if line1[x:x + step1] != line2[x:x + step1]:
                differing += 1
                col = x // step1
                if min_x is None or col < min_x:
                    min_x = col
                if max_x is None or col > max_x:
                    max_x = col
    box = (None if min_x is None
           else (min_x, min_y, max_x - min_x + 1, max_y - min_y + 1))
    return Diff(identical=differing == 0, differing=differing, total=counted,
                how="pixels", box=box, width=w1, height=h1)
