"""Photo safety: magic bytes, declared type, GPS metadata removal."""

import pytest
from observation_images import GPS_LAT, heic, jpeg, png, tiff_with_gps, webp

from app.services import photo_safety as ps


@pytest.mark.parametrize("data,fmt", [
    (jpeg(), "jpeg"), (png(), "png"), (webp(), "webp"), (heic(), "heic"),
])
def test_observations_magic_bytes_detected(data, fmt):
    assert ps.detect_format(data[:64]) == fmt


@pytest.mark.parametrize("data", [
    b"GIF89a\x01\x00", b"%PDF-1.7", b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    b"<html><script>alert(1)</script>", b"RIFF\x00\x00\x00\x00AVI ", b"\x00\x00\x00\x18ftypisom",
    b"", b"\xff\xd8",
])
def test_observations_bad_magic_bytes_rejected(data):
    assert ps.detect_format(data) is None


def test_observations_declared_type_must_match():
    ps.check_declared_type("jpeg", "image/jpeg")
    ps.check_declared_type("jpeg", None)
    ps.check_declared_type("png", "application/octet-stream")
    ps.check_declared_type("heic", "image/heif")
    with pytest.raises(ps.PhotoRejected):
        ps.check_declared_type("jpeg", "image/png")
    with pytest.raises(ps.PhotoRejected):
        ps.check_declared_type("png", "text/html")


@pytest.mark.parametrize("endian", [">", "<"])
def test_observations_jpeg_gps_removed_orientation_kept(endian):
    original = jpeg(exif=tiff_with_gps(endian), xmp=True)
    assert ps.jpeg_has_gps(original)
    result = ps.scrub("jpeg", original)
    assert result.gps_removed is True
    cleaned = result.data
    assert not ps.jpeg_has_gps(cleaned)
    lat = GPS_LAT if endian == ">" else b"".join(
        v.to_bytes(4, "little") for v in (44, 1, 52, 1, 3345, 100)
    )
    assert lat not in cleaned and b"GPSLatitude" not in cleaned
    # Orientation entry still there, scan data untouched, same EXIF size
    assert b"\x01\x12" in cleaned or b"\x12\x01" in cleaned
    assert cleaned.endswith(b"\x12\x34\xff\x00\x56SCANDATA\xff\xd9")
    assert ps.detect_format(cleaned) == "jpeg"


def test_observations_jpeg_without_exif_unchanged():
    data = jpeg()
    assert ps.scrub("jpeg", data).data == data


def test_observations_jpeg_corrupt_exif_dropped_whole():
    data = jpeg(exif=b"MM\x00\x2a\x00\x00\xff\xff")  # IFD offset out of bounds
    cleaned = ps.scrub("jpeg", data).data
    assert b"Exif\x00\x00" not in cleaned
    assert cleaned.endswith(b"SCANDATA\xff\xd9")


def test_observations_jpeg_truncated_rejected():
    with pytest.raises(ps.PhotoRejected):
        ps.scrub("jpeg", b"\xff\xd8\xff\xe1\xff\xff\x00")


def test_observations_png_text_and_exif_removed():
    cleaned = ps.scrub("png", png()).data
    assert b"tEXt" not in cleaned and b"eXIf" not in cleaned and b"44.87" not in cleaned
    assert b"IHDR" in cleaned and b"IDAT" in cleaned and cleaned.endswith(b"IEND\xaeB`\x82")


def test_observations_webp_exif_xmp_removed():
    cleaned = ps.scrub("webp", webp()).data
    assert b"EXIF" not in cleaned and b"XMP " not in cleaned
    assert cleaned[:4] == b"RIFF" and cleaned[8:12] == b"WEBP"
    assert int.from_bytes(cleaned[4:8], "little") == len(cleaned) - 8
    vp8x_flags = cleaned[12 + 8]
    assert vp8x_flags & 0x0C == 0


def test_observations_heic_kept_as_is():
    data = heic()
    result = ps.scrub("heic", data)
    assert result.data == data and result.gps_removed is False
