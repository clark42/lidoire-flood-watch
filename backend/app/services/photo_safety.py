"""Observation photo validation and metadata scrubbing (no image library).

- The format comes from the magic bytes only (JPEG, PNG, WebP, HEIC/HEIF);
  the client content-type and file name are never trusted.
- Location metadata is removed without re-encoding the pixels:
  JPEG: the EXIF GPS IFD is zeroed and unlinked (orientation and the other
  tags are kept), XMP segments are dropped; PNG: eXIf and text chunks
  (tEXt/zTXt/iTXt, where XMP lives) are dropped; WebP: EXIF and XMP chunks
  are dropped. A JPEG EXIF block that cannot be parsed is dropped whole.
- HEIC/HEIF metadata is NOT rewritten (ISOBMFF item boxes): its GPS, if
  any, is kept. Photos are private (served with the API key only).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

# Detected format -> (extension, content-type)
FORMATS: dict[str, tuple[str, str]] = {
    "jpeg": ("jpg", "image/jpeg"),
    "png": ("png", "image/png"),
    "webp": ("webp", "image/webp"),
    "heic": ("heic", "image/heic"),
}
EXTENSION_CONTENT_TYPES = {ext: ctype for ext, ctype in FORMATS.values()}

# Client content-types accepted for each detected format (octet-stream and
# a missing type are accepted: the magic bytes decide)
DECLARED_TYPES: dict[str, set[str]] = {
    "jpeg": {"image/jpeg", "image/jpg", "image/pjpeg"},
    "png": {"image/png"},
    "webp": {"image/webp"},
    "heic": {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"},
}
NEUTRAL_TYPES = {"", "application/octet-stream"}

HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"heif"}

GPS_IFD_TAG = 0x8825
# TIFF type -> size in bytes of one value
_TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}


class PhotoRejected(ValueError):
    """The upload is not an accepted image (HTTP 415)."""


@dataclass
class ScrubResult:
    data: bytes
    gps_removed: bool  # False: the format keeps its metadata (HEIC)


def detect_format(head: bytes) -> Optional[str]:
    """Format from the first bytes (None when not an accepted image)."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brands = {head[8:12]}
        box_size = int.from_bytes(head[0:4], "big")
        end = min(box_size, len(head)) if box_size >= 16 else 16
        brands.update(head[i:i + 4] for i in range(16, end - 3, 4))
        if brands & HEIF_BRANDS:
            return "heic"
    return None


def check_declared_type(fmt: str, declared: Optional[str]) -> None:
    """Reject a client content-type that contradicts the magic bytes."""
    declared = (declared or "").split(";")[0].strip().lower()
    if declared in NEUTRAL_TYPES:
        return
    if declared not in DECLARED_TYPES[fmt]:
        raise PhotoRejected(f"content-type {declared!r} does not match the file ({fmt})")


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

_EXIF_HEADER = b"Exif\x00\x00"
_XMP_HEADERS = (b"http://ns.adobe.com/xap/1.0/\x00", b"http://ns.adobe.com/xmp/extension/\x00")


class _TiffError(ValueError):
    pass


def _strip_gps_from_tiff(tiff: bytearray) -> bool:
    """Zero the GPS IFD(s) of a TIFF/EXIF block and unlink them, in place.

    Only bytes inside IFD0/IFD1 and the GPS IFD are rewritten: no offset
    moves. Returns True when a GPS IFD was found. Raises _TiffError when
    the structure is invalid.
    """
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        raise _TiffError("bad byte order")
    size = len(tiff)

    def u16(off: int) -> int:
        if off < 0 or off + 2 > size:
            raise _TiffError("out of bounds")
        return struct.unpack_from(endian + "H", tiff, off)[0]

    def u32(off: int) -> int:
        if off < 0 or off + 4 > size:
            raise _TiffError("out of bounds")
        return struct.unpack_from(endian + "I", tiff, off)[0]

    if u16(2) != 42:
        raise _TiffError("bad TIFF magic")

    def zero(start: int, length: int) -> None:
        if start < 0 or start + length > size:
            raise _TiffError("out of bounds")
        tiff[start:start + length] = bytes(length)

    def wipe_gps_ifd(gps_off: int) -> None:
        count = u16(gps_off)
        for i in range(count):
            entry = gps_off + 2 + 12 * i
            typ, n = u16(entry + 2), u32(entry + 4)
            length = _TIFF_TYPE_SIZES.get(typ, 1) * n
            if length > 4:
                zero(u32(entry + 8), length)
        zero(gps_off, 2 + 12 * count + 4)

    found = False
    ifd = u32(4)
    seen: set[int] = set()
    while ifd and ifd not in seen and len(seen) < 4:  # IFD0, IFD1 (thumbnail)
        seen.add(ifd)
        count = u16(ifd)
        entries = [bytes(tiff[ifd + 2 + 12 * i: ifd + 14 + 12 * i]) for i in range(count)]
        if any(len(e) != 12 for e in entries):
            raise _TiffError("truncated IFD")
        next_ifd = u32(ifd + 2 + 12 * count)
        kept = []
        for entry in entries:
            tag = struct.unpack(endian + "H", entry[:2])[0]
            if tag == GPS_IFD_TAG:
                found = True
                wipe_gps_ifd(struct.unpack(endian + "I", entry[8:12])[0])
            else:
                kept.append(entry)
        if len(kept) != count:
            # Same span: count, kept entries, next-IFD offset, zero padding
            block = struct.pack(endian + "H", len(kept)) + b"".join(kept) + struct.pack(endian + "I", next_ifd)
            span = 2 + 12 * count + 4
            tiff[ifd:ifd + span] = block + bytes(span - len(block))
        ifd = next_ifd
    return found


def scrub_jpeg(data: bytes) -> bytes:
    """Drop XMP segments and the GPS part of the EXIF segment(s) of a JPEG."""
    if data[:2] != b"\xff\xd8":
        raise PhotoRejected("not a JPEG")
    out = bytearray(b"\xff\xd8")
    pos = 2
    size = len(data)
    while pos < size:
        if data[pos] != 0xFF:
            raise PhotoRejected("corrupt JPEG segment")
        marker = data[pos + 1] if pos + 1 < size else None
        if marker is None:
            raise PhotoRejected("truncated JPEG")
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[pos:pos + 2]
            pos += 2
            continue
        if marker == 0xD9:  # EOI
            out += data[pos:]
            return bytes(out)
        if pos + 4 > size:
            raise PhotoRejected("truncated JPEG")
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        end = pos + 2 + length
        if length < 2 or end > size:
            raise PhotoRejected("corrupt JPEG segment length")
        payload = data[pos + 4:end]
        if marker == 0xE1 and payload.startswith(_XMP_HEADERS):
            pos = end  # XMP may hold exif:GPS* properties: dropped
            continue
        if marker == 0xE1 and payload.startswith(_EXIF_HEADER):
            tiff = bytearray(payload[len(_EXIF_HEADER):])
            try:
                _strip_gps_from_tiff(tiff)
            except _TiffError:
                pos = end  # unreadable EXIF: dropped whole (no GPS can remain)
                continue
            out += data[pos:pos + 4] + _EXIF_HEADER + bytes(tiff)
            pos = end
            continue
        out += data[pos:end]
        pos = end
        if marker == 0xDA:  # start of scan: entropy-coded data until EOI
            out += data[pos:]
            return bytes(out)
    return bytes(out)


def jpeg_has_gps(data: bytes) -> bool:
    """True when an EXIF segment still links a GPS IFD (tests, checks)."""
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xDA:
            return False
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        payload = data[pos + 4:pos + 2 + length]
        if marker == 0xE1 and payload.startswith(_EXIF_HEADER):
            tiff = payload[len(_EXIF_HEADER):]
            endian = "<" if tiff[:2] == b"II" else ">"
            ifd = struct.unpack_from(endian + "I", tiff, 4)[0]
            count = struct.unpack_from(endian + "H", tiff, ifd)[0]
            for i in range(count):
                if struct.unpack_from(endian + "H", tiff, ifd + 2 + 12 * i)[0] == GPS_IFD_TAG:
                    return True
        if marker == 0xE1 and payload.startswith(_XMP_HEADERS):
            return True
        pos += 2 + length
    return False


# ---------------------------------------------------------------------------
# PNG / WebP
# ---------------------------------------------------------------------------

_PNG_DROPPED = {b"eXIf", b"tEXt", b"zTXt", b"iTXt"}


def scrub_png(data: bytes) -> bytes:
    out = bytearray(data[:8])
    pos = 8
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise PhotoRejected("corrupt PNG chunk")
        if ctype not in _PNG_DROPPED:
            out += data[pos:end]
        pos = end
        if ctype == b"IEND":
            break
    return bytes(out)


def scrub_webp(data: bytes) -> bytes:
    chunks = bytearray()
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        length = int.from_bytes(data[pos + 4:pos + 8], "little")
        end = pos + 8 + length + (length & 1)
        if pos + 8 + length > len(data):
            raise PhotoRejected("corrupt WebP chunk")
        chunk = bytearray(data[pos:min(end, len(data))])
        if fourcc in (b"EXIF", b"XMP "):
            pos = end
            continue
        if fourcc == b"VP8X" and length >= 1:
            chunk[8] &= ~0x0C & 0xFF  # clear the EXIF (0x08) and XMP (0x04) flags
        chunks += chunk
        pos = end
    return b"RIFF" + (len(chunks) + 4).to_bytes(4, "little") + b"WEBP" + bytes(chunks)


def scrub(fmt: str, data: bytes) -> ScrubResult:
    """Remove location metadata (see module docstring)."""
    if fmt == "jpeg":
        return ScrubResult(scrub_jpeg(data), True)
    if fmt == "png":
        return ScrubResult(scrub_png(data), True)
    if fmt == "webp":
        return ScrubResult(scrub_webp(data), True)
    return ScrubResult(data, False)
