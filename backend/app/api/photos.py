"""Photos API for flood photo timeline.

This API receives photos uploaded from the flood-photos-tool
which runs locally on macOS with access to Photos.app.
"""
import json
import os
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel


router = APIRouter(tags=["photos"])

# Storage directory for photos and metadata
PHOTO_STORAGE_DIR = Path(os.environ.get("PHOTO_STORAGE_DIR", "/app/data/photos"))
PHOTO_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Metadata file
PHOTOS_METADATA_FILE = PHOTO_STORAGE_DIR / "metadata.json"


class PhotoLabel(BaseModel):
    key: str
    value: str


class PhotoData(BaseModel):
    id: str
    timestamp: str
    lat: float
    lon: float
    thumbnailUrl: str
    fullUrl: str
    labels: list[PhotoLabel]
    zone: Optional[str] = None
    description: Optional[str] = None
    episode_id: Optional[str] = None


class PhotoUpload(BaseModel):
    uuid: str
    timestamp: str
    lat: float
    lon: float
    labels: list[str] = []
    episode_id: Optional[str] = None
    filename: Optional[str] = None


class PhotoListResponse(BaseModel):
    total: int
    photos: list[PhotoData]
    episodes: dict


def load_metadata() -> dict:
    """Load photos metadata from file."""
    if not PHOTOS_METADATA_FILE.exists():
        return {"photos": [], "episodes": {}}

    with open(PHOTOS_METADATA_FILE) as f:
        return json.load(f)


def save_metadata(metadata: dict):
    """Save photos metadata to file."""
    with open(PHOTOS_METADATA_FILE, "w") as f:
        json.dump(metadata, f, indent=2, default=str)


def get_zone_from_coords(lat: float, lon: float) -> Optional[str]:
    """Determine zone from GPS coordinates."""
    zones = {
        "seuil": {"minLat": 44.8755, "maxLat": 44.8758, "minLon": 0.0010, "maxLon": 0.0020},
        "jardin": {"minLat": 44.8758, "maxLat": 44.8762, "minLon": 0.0020, "maxLon": 0.0030},
        "entree": {"minLat": 44.8762, "maxLat": 44.8765, "minLon": 0.0010, "maxLon": 0.0020},
        "moulin": {"minLat": 44.8758, "maxLat": 44.8762, "minLon": 0.0010, "maxLon": 0.0020},
        "camera": {"minLat": 44.8760, "maxLat": 44.8761, "minLon": 0.0005, "maxLon": 0.0010},
    }

    for zone_id, bounds in zones.items():
        if (bounds["minLat"] <= lat <= bounds["maxLat"] and
            bounds["minLon"] <= lon <= bounds["maxLon"]):
            return zone_id

    return None


def photo_to_response(photo: dict) -> PhotoData:
    """Convert stored photo metadata to API response."""
    uuid = photo["uuid"]

    labels = [
        PhotoLabel(key="label", value=label)
        for label in photo.get("labels", [])
    ]

    zone = photo.get("zone") or get_zone_from_coords(
        photo.get("lat", 0), photo.get("lon", 0)
    )

    return PhotoData(
        id=uuid,
        timestamp=photo.get("timestamp", ""),
        lat=photo.get("lat", 44.8759811),
        lon=photo.get("lon", 0.0015091),
        thumbnailUrl=f"/api/photos/{uuid}/thumbnail",
        fullUrl=f"/api/photos/{uuid}/full",
        labels=labels,
        zone=zone,
        description=photo.get("filename", ""),
        episode_id=photo.get("episode_id"),
    )


@router.get("", response_model=PhotoListResponse)
async def list_photos(
    episode_id: Optional[str] = Query(None, description="Filter by episode ID"),
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    zone: Optional[str] = Query(None, description="Filter by zone"),
    has_water_label: bool = Query(False, description="Only photos with water labels"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List uploaded flood photos with optional filters."""
    metadata = load_metadata()
    photos = metadata.get("photos", [])
    episodes = metadata.get("episodes", {})

    # Apply filters
    filtered = []
    for photo in photos:
        # Episode filter
        if episode_id and photo.get("episode_id") != episode_id:
            continue

        # Date range filter
        if start_date or end_date:
            photo_date = photo.get("timestamp", "")
            if photo_date:
                try:
                    photo_dt = datetime.fromisoformat(photo_date.replace("Z", "+00:00"))
                    if start_date:
                        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                        if photo_dt < start_dt:
                            continue
                    if end_date:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        if photo_dt > end_dt:
                            continue
                except ValueError:
                    pass

        # Zone filter
        if zone:
            photo_zone = photo.get("zone") or get_zone_from_coords(
                photo.get("lat", 0), photo.get("lon", 0)
            )
            if photo_zone != zone:
                continue

        # Water label filter
        if has_water_label:
            labels = photo.get("labels", [])
            water_keywords = ["eau", "water", "inondation", "flood", "riviere", "river"]
            has_water = any(
                any(kw in label.lower() for kw in water_keywords)
                for label in labels
            )
            if not has_water:
                continue

        filtered.append(photo)

    total = len(filtered)

    # Pagination
    paginated = filtered[offset:offset + limit]

    # Convert to response format
    response_photos = [photo_to_response(p) for p in paginated]

    return PhotoListResponse(
        total=total,
        photos=response_photos,
        episodes=episodes
    )


@router.get("/episodes")
async def get_photo_episodes():
    """Get list of episodes with photo counts."""
    metadata = load_metadata()
    return metadata.get("episodes", {})


@router.post("/upload")
async def upload_photo(
    file: UploadFile = File(...),
    uuid: str = Form(...),
    timestamp: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    labels: str = Form(""),  # Comma-separated
    episode_id: Optional[str] = Form(None),
):
    """Upload a photo with metadata.

    Called by flood-photos-tool to sync photos from Photos.app.
    """
    # Save the file
    photo_dir = PHOTO_STORAGE_DIR / uuid
    photo_dir.mkdir(exist_ok=True)

    # Determine file extension
    ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
    photo_path = photo_dir / f"full{ext}"

    with open(photo_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Parse labels
    label_list = [l.strip() for l in labels.split(",") if l.strip()]

    # Create/update metadata
    metadata = load_metadata()

    # Check if photo already exists
    existing_idx = next(
        (i for i, p in enumerate(metadata["photos"]) if p["uuid"] == uuid),
        None
    )

    photo_data = {
        "uuid": uuid,
        "timestamp": timestamp,
        "lat": lat,
        "lon": lon,
        "labels": label_list,
        "episode_id": episode_id,
        "filename": file.filename,
        "uploaded_at": datetime.utcnow().isoformat(),
        "zone": get_zone_from_coords(lat, lon),
    }

    if existing_idx is not None:
        metadata["photos"][existing_idx] = photo_data
    else:
        metadata["photos"].append(photo_data)

    # Update episode stats
    if episode_id:
        if episode_id not in metadata["episodes"]:
            metadata["episodes"][episode_id] = {"photo_count": 0}
        # Recount photos for this episode
        metadata["episodes"][episode_id]["photo_count"] = sum(
            1 for p in metadata["photos"] if p.get("episode_id") == episode_id
        )

    save_metadata(metadata)

    return {"status": "ok", "uuid": uuid}


@router.post("/upload-thumbnail/{uuid}")
async def upload_thumbnail(uuid: str, file: UploadFile = File(...)):
    """Upload a thumbnail for an existing photo."""
    photo_dir = PHOTO_STORAGE_DIR / uuid
    if not photo_dir.exists():
        raise HTTPException(status_code=404, detail="Photo not found")

    ext = Path(file.filename or "thumb.jpg").suffix or ".jpg"
    thumb_path = photo_dir / f"thumbnail{ext}"

    with open(thumb_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return {"status": "ok", "uuid": uuid}


@router.delete("/{uuid}")
async def delete_photo(uuid: str):
    """Delete a photo."""
    photo_dir = PHOTO_STORAGE_DIR / uuid

    if photo_dir.exists():
        shutil.rmtree(photo_dir)

    metadata = load_metadata()
    metadata["photos"] = [p for p in metadata["photos"] if p["uuid"] != uuid]
    save_metadata(metadata)

    return {"status": "ok"}


@router.get("/{uuid}/thumbnail")
async def get_photo_thumbnail(uuid: str):
    """Get photo thumbnail."""
    photo_dir = PHOTO_STORAGE_DIR / uuid

    # Try different extensions
    for ext in [".jpg", ".jpeg", ".png", ".heic"]:
        thumb_path = photo_dir / f"thumbnail{ext}"
        if thumb_path.exists():
            return FileResponse(
                thumb_path,
                media_type=f"image/{'jpeg' if ext in ['.jpg', '.jpeg'] else ext[1:]}",
                headers={"Cache-Control": "public, max-age=86400"}
            )

    # Fallback to full image
    for ext in [".jpg", ".jpeg", ".png", ".heic"]:
        full_path = photo_dir / f"full{ext}"
        if full_path.exists():
            return FileResponse(
                full_path,
                media_type=f"image/{'jpeg' if ext in ['.jpg', '.jpeg'] else ext[1:]}",
                headers={"Cache-Control": "public, max-age=86400"}
            )

    raise HTTPException(status_code=404, detail="Photo not found")


@router.get("/{uuid}/full")
async def get_photo_full(uuid: str):
    """Get full resolution photo."""
    photo_dir = PHOTO_STORAGE_DIR / uuid

    for ext in [".jpg", ".jpeg", ".png", ".heic"]:
        full_path = photo_dir / f"full{ext}"
        if full_path.exists():
            return FileResponse(
                full_path,
                media_type=f"image/{'jpeg' if ext in ['.jpg', '.jpeg'] else ext[1:]}",
                headers={"Cache-Control": "public, max-age=86400"}
            )

    raise HTTPException(status_code=404, detail="Photo not found")


@router.get("/{uuid}/info")
async def get_photo_info(uuid: str):
    """Get photo metadata."""
    metadata = load_metadata()

    for photo in metadata.get("photos", []):
        if photo.get("uuid") == uuid:
            return photo_to_response(photo)

    raise HTTPException(status_code=404, detail="Photo not found")
