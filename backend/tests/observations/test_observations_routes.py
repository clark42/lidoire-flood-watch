"""Observations routes: auth on writes, JSON and multipart, upload limits, photo serving."""

import json
import os
import stat
from datetime import datetime
from pathlib import Path

import pytest
from observation_images import GPS_LAT, jpeg, png, tiff_with_gps

from app.services import photo_safety


def _photo_files(settings) -> list[Path]:
    directory = Path(settings.current["value"].observations_photo_dir)
    return sorted(directory.iterdir()) if directory.exists() else []


def _multipart(client, auth, data: dict, photo: bytes | None = None, content_type: str = "image/jpeg",
               filename: str = "IMG_0001.jpg"):
    files = {"photo": (filename, photo, content_type)} if photo is not None else None
    return client.post("/api/observations", data={"data": json.dumps(data)}, files=files, headers=auth)


# --- auth ---------------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/observations"),
    ("DELETE", "/api/observations/1"),
    ("GET", "/api/observations/1/photo"),
])
def test_observations_writes_and_photo_require_key(client, method, path):
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers={"X-API-Key": "nope"}).status_code == 401


def test_observations_list_is_public(client):
    assert client.get("/api/observations").status_code == 200


def test_observations_dev_mode_without_key(client, settings, fake_store):
    settings(api_key="")
    resp = client.post("/api/observations", json={"kind": "normal"})
    assert resp.status_code == 201


# --- JSON ---------------------------------------------------------------------


def test_observations_create_json_and_list(client, auth):
    resp = client.post("/api/observations", headers=auth, json={
        "observed_at": "2026-02-14T06:30:00+01:00", "kind": "flooded",
        "water_height_cm": 12, "reference": "plancher", "note": "eau dans le moulin",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["observed_at"] == "2026-02-14T05:30:00Z"  # stored and served in UTC
    assert body["kind"] == "flooded" and body["reference"] == "plancher"
    assert body["has_photo"] is False and body["photo_url"] is None and body["source"] == "user"

    client.post("/api/observations", headers=auth, json={"observed_at": "2026-03-01T10:00:00Z", "kind": "normal"})
    listed = client.get("/api/observations").json()
    assert [o["kind"] for o in listed] == ["normal", "flooded"]  # newest first
    window = client.get("/api/observations", params={"start": "2026-02-01T00:00:00Z", "end": "2026-02-28T00:00:00Z"})
    assert [o["kind"] for o in window.json()] == ["flooded"]
    bad = client.get("/api/observations", params={"start": "2026-03-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"})
    assert bad.status_code == 422


@pytest.mark.parametrize("payload", [
    {"kind": "submerged"},
    {},
    {"kind": "flooded", "water_height_cm": 5000},
    {"kind": "flooded", "reference": "x" * 101},
    ["kind", "flooded"],
])
def test_observations_invalid_json_rejected(client, auth, payload):
    assert client.post("/api/observations", headers=auth, json=payload).status_code == 422


def test_observations_malformed_json_rejected(client, auth):
    resp = client.post("/api/observations", headers={**auth, "Content-Type": "application/json"}, content=b"{nope")
    assert resp.status_code == 422


# --- multipart + photo ----------------------------------------------------------


def test_observations_multipart_with_jpeg_strips_gps(client, auth, settings):
    photo = jpeg(exif=tiff_with_gps())
    resp = _multipart(client, auth, {"kind": "water_rising", "water_height_cm": -20, "reference": "seuil du moulin"},
                      photo, filename="../../etc/passwd.jpg")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["has_photo"] is True and body["photo_url"] == f"/api/observations/{body['id']}/photo"

    files = _photo_files(settings)
    assert len(files) == 1
    stored = files[0]
    assert stored.parent == Path(settings.current["value"].observations_photo_dir)
    assert len(stored.stem) == 32 and stored.suffix == ".jpg"  # server-side name, client name ignored
    assert stat.S_IMODE(os.stat(stored).st_mode) == 0o600
    content = stored.read_bytes()
    assert GPS_LAT not in content and not photo_safety.jpeg_has_gps(content)

    served = client.get(body["photo_url"], headers=auth)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in served.headers["cache-control"]
    assert served.content == content


def test_observations_multipart_individual_fields(client, auth):
    resp = client.post("/api/observations", headers=auth, data={"kind": "near_miss", "note": "a 5 cm du seuil"},
                       files={"photo": ("p.png", png(), "image/png")})
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "near_miss" and resp.json()["has_photo"] is True


def test_observations_multipart_without_photo(client, auth, settings):
    resp = _multipart(client, auth, {"kind": "normal"})
    assert resp.status_code == 201 and resp.json()["has_photo"] is False
    assert _photo_files(settings) == []


@pytest.mark.parametrize("photo,ctype", [
    (b"<html><script>alert(1)</script></html>", "image/jpeg"),  # lies about its type
    (b"GIF89a\x01\x00\x01\x00", "image/gif"),
    (b"%PDF-1.7\n", "application/pdf"),
    (jpeg(), "text/html"),  # real JPEG, contradicting declared type
])
def test_observations_bad_photo_rejected_nothing_stored(client, auth, settings, fake_store, photo, ctype):
    resp = _multipart(client, auth, {"kind": "flooded"}, photo, content_type=ctype)
    assert resp.status_code == 415
    assert _photo_files(settings) == [] and fake_store.rows == {}


def test_observations_empty_photo_rejected(client, auth, fake_store):
    resp = client.post("/api/observations", headers=auth, data={"data": json.dumps({"kind": "flooded"})},
                       files={"photo": ("x.jpg", b"", "image/jpeg")})
    assert resp.status_code in (201, 400)
    if resp.status_code == 201:  # an empty file part without name is ignored
        assert resp.json()["has_photo"] is False


def test_observations_photo_too_large(client, auth, settings, fake_store):
    settings(observation_photo_max_bytes=1024)
    big = jpeg() + b"\x00" * 2048
    resp = _multipart(client, auth, {"kind": "flooded"}, big)
    assert resp.status_code == 413
    assert _photo_files(settings) == [] and fake_store.rows == {}


def test_observations_request_too_large_rejected_before_parsing(client, auth, settings):
    settings(observation_photo_max_bytes=1024)
    resp = client.post(
        "/api/observations",
        headers={**auth, "Content-Type": "multipart/form-data; boundary=x"},
        content=b"x" * (1024 + 300 * 1024),
    )
    assert resp.status_code == 413


def test_observations_default_limit_is_8_mb(settings):
    assert settings.current["value"].observation_photo_max_bytes == 8 * 1024 * 1024


def test_observations_invalid_data_field(client, auth, settings):
    resp = client.post("/api/observations", headers=auth, data={"data": "{oops"},
                       files={"photo": ("p.jpg", jpeg(), "image/jpeg")})
    assert resp.status_code == 422
    assert _photo_files(settings) == []


# --- photo serving / traversal ---------------------------------------------------


@pytest.mark.parametrize("stored_name", [
    "../../etc/passwd",
    "/etc/passwd",
    "..",
    "0123456789abcdef0123456789abcdef.jpg/../../x",
    "0123456789abcdef0123456789abcdef.exe",
])
def test_observations_traversal_in_stored_name_is_refused(client, auth, fake_store, settings, stored_name, tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    row = fake_store.add_row(observed_at=datetime(2026, 1, 1), photo_path=stored_name)
    resp = client.get(f"/api/observations/{row.id}/photo", headers=auth)
    assert resp.status_code == 404


@pytest.mark.parametrize("path", [
    "/api/observations/..%2F..%2Fetc%2Fpasswd/photo",
    "/api/observations/1%2F..%2F2/photo",
    "/api/observations/abc/photo",
])
def test_observations_traversal_in_url_is_refused(client, auth, path):
    assert client.get(path, headers=auth).status_code in (404, 422)


def test_observations_photo_missing_file_and_row(client, auth, fake_store):
    row = fake_store.add_row(observed_at=datetime(2026, 1, 1), photo_path="0123456789abcdef0123456789abcdef.jpg")
    assert client.get(f"/api/observations/{row.id}/photo", headers=auth).status_code == 404
    assert client.get("/api/observations/999/photo", headers=auth).status_code == 404


def test_observations_delete_removes_photo(client, auth, settings):
    body = _multipart(client, auth, {"kind": "flooded"}, jpeg()).json()
    assert len(_photo_files(settings)) == 1
    resp = client.delete(f"/api/observations/{body['id']}", headers=auth)
    assert resp.status_code == 200 and resp.json() == {"status": "deleted", "id": body["id"]}
    assert _photo_files(settings) == []
    assert client.delete(f"/api/observations/{body['id']}", headers=auth).status_code == 404


def test_observations_openapi_documents_both_bodies():
    import re

    from fastapi.testclient import TestClient

    from app.main import create_app

    schema = TestClient(create_app()).get("/openapi.json").json()
    body = schema["paths"]["/api/observations"]["post"]["requestBody"]["content"]
    assert {"application/json", "multipart/form-data"} <= set(body)
    refs = set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(schema)))
    assert refs <= set(schema["components"]["schemas"])
