"""Tiny synthetic images (bytes built by hand, no image library)."""

import struct
import zlib

GPS_LAT = struct.pack(">IIIIII", 44, 1, 52, 1, 3345, 100)  # 44 deg 52' 33.45"


def tiff_with_gps(endian: str = ">") -> bytes:
    """TIFF block: IFD0 = Orientation + GPS pointer; GPS IFD = LatitudeRef + Latitude."""
    bo = b"MM" if endian == ">" else b"II"
    ifd0 = 8
    ifd0_size = 2 + 2 * 12 + 4
    gps_ifd = ifd0 + ifd0_size
    gps_size = 2 + 2 * 12 + 4
    lat_off = gps_ifd + gps_size
    out = bo + struct.pack(endian + "HI", 42, ifd0)
    out += struct.pack(endian + "H", 2)
    out += struct.pack(endian + "HHI", 0x0112, 3, 1) + struct.pack(endian + "HH", 6, 0)  # Orientation=6
    out += struct.pack(endian + "HHII", 0x8825, 4, 1, gps_ifd)
    out += struct.pack(endian + "I", 0)
    out += struct.pack(endian + "H", 2)
    out += struct.pack(endian + "HHI", 0x0001, 2, 2) + b"N\x00\x00\x00"
    out += struct.pack(endian + "HHII", 0x0002, 5, 3, lat_off)
    out += struct.pack(endian + "I", 0)
    out += struct.pack(endian + "IIIIII", 44, 1, 52, 1, 3345, 100)
    return out


def jpeg(exif: bytes | None = None, xmp: bool = False) -> bytes:
    data = b"\xff\xd8"
    data += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    if exif is not None:
        payload = b"Exif\x00\x00" + exif
        data += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    if xmp:
        payload = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta exif:GPSLatitude='44,52.5N'/>"
        data += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    sos = b"\x01\x01\x00\x00\x3f\x00"
    data += b"\xff\xda" + struct.pack(">H", len(sos) + 2) + sos
    data += b"\x12\x34\xff\x00\x56SCANDATA"  # entropy-coded data (stuffed 0xFF00)
    return data + b"\xff\xd9"


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload))


def png(with_text: bool = True) -> bytes:
    data = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    if with_text:
        data += _png_chunk(b"tEXt", b"GPS\x0044.87,0.0015")
        data += _png_chunk(b"eXIf", tiff_with_gps())
    data += _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    return data + _png_chunk(b"IEND", b"")


def webp(with_exif: bool = True) -> bytes:
    def chunk(fourcc: bytes, payload: bytes) -> bytes:
        pad = b"\x00" if len(payload) & 1 else b""
        return fourcc + struct.pack("<I", len(payload)) + payload + pad

    flags = 0x08 | 0x04 if with_exif else 0
    body = chunk(b"VP8X", bytes([flags, 0, 0, 0]) + b"\x00\x00\x00\x00\x00\x00")
    body += chunk(b"VP8 ", b"\x00" * 11)
    if with_exif:
        body += chunk(b"EXIF", tiff_with_gps())
        body += chunk(b"XMP ", b"<x:xmpmeta/>")
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def heic() -> bytes:
    ftyp = b"ftyp" + b"heic" + b"\x00\x00\x00\x00" + b"mif1heic"
    return struct.pack(">I", len(ftyp) + 4) + ftyp + b"\x00\x00\x00\x08free"
