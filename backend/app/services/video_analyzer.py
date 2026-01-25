"""Video analysis service for cascade/weir monitoring.

Analyzes camera feed to detect water level changes:
- Captures periodic snapshots from RTSP/HTTP stream
- Processes images to estimate relative water level
- Detects anomalies (rapid rise, unusual patterns)
"""

import asyncio
from datetime import datetime
from typing import Optional
import io

from app.config import get_settings
from app.models.schemas import VideoAnalysis

# Try to import OpenCV, gracefully handle if not available
try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    cv2 = None
    np = None


# Baseline water level for comparison
_baseline_level: Optional[float] = None
_last_analysis: Optional[VideoAnalysis] = None


class WaterLevelAnalyzer:
    """Analyzes water level from camera images."""

    def __init__(self):
        self.roi = None  # Region of interest (x, y, w, h)
        self.baseline_image = None
        self.water_color_range = {
            "lower": [80, 30, 30],   # HSV lower bound for water
            "upper": [130, 255, 200],  # HSV upper bound
        }
        # Reference points for level calibration
        self.reference_points = []

    def set_roi(self, x: int, y: int, w: int, h: int):
        """Set the region of interest for water detection."""
        self.roi = (x, y, w, h)

    def add_reference_point(self, y_pixel: int, level_value: float):
        """Add a reference point for calibration (pixel Y -> real level)."""
        self.reference_points.append((y_pixel, level_value))
        self.reference_points.sort(key=lambda p: p[0])

    def analyze_frame(self, frame) -> dict:
        """
        Analyze a single frame to estimate water level.

        Returns:
            dict with:
            - water_level_index: 0-100 relative level
            - water_pixels_ratio: ratio of detected water pixels
            - edge_y: detected water edge Y coordinate
        """
        if not CV2_AVAILABLE:
            return {"water_level_index": 50.0, "error": "OpenCV not available"}

        if frame is None:
            return {"water_level_index": 0.0, "error": "No frame"}

        # Apply ROI if set
        if self.roi:
            x, y, w, h = self.roi
            roi_frame = frame[y:y+h, x:x+w]
        else:
            roi_frame = frame
            h = frame.shape[0]

        # Convert to HSV for color-based water detection
        hsv = cv2.cvtColor(roi_frame, cv2.COLOR_BGR2HSV)

        # Create mask for water color
        lower = np.array(self.water_color_range["lower"])
        upper = np.array(self.water_color_range["upper"])
        water_mask = cv2.inRange(hsv, lower, upper)

        # Apply morphological operations to clean up mask
        kernel = np.ones((5, 5), np.uint8)
        water_mask = cv2.morphologyEx(water_mask, cv2.MORPH_CLOSE, kernel)
        water_mask = cv2.morphologyEx(water_mask, cv2.MORPH_OPEN, kernel)

        # Calculate water pixel ratio
        total_pixels = water_mask.size
        water_pixels = cv2.countNonZero(water_mask)
        water_ratio = water_pixels / total_pixels

        # Find water edge (highest row with significant water)
        row_sums = np.sum(water_mask, axis=1)
        threshold = water_mask.shape[1] * 0.3 * 255  # 30% of row width

        water_edge_y = h  # Default to bottom
        for i, row_sum in enumerate(row_sums):
            if row_sum > threshold:
                water_edge_y = i
                break

        # Convert to level index (0-100)
        # Higher Y = lower in image = lower water = lower index
        level_index = (1 - water_edge_y / h) * 100

        return {
            "water_level_index": level_index,
            "water_pixels_ratio": water_ratio,
            "edge_y": water_edge_y,
        }

    def detect_motion(self, frame1, frame2) -> float:
        """Detect motion between two frames (for rapid level change)."""
        if not CV2_AVAILABLE or frame1 is None or frame2 is None:
            return 0.0

        # Convert to grayscale
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)

        # Calculate absolute difference
        diff = cv2.absdiff(gray1, gray2)

        # Threshold
        _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)

        # Calculate motion ratio
        motion_pixels = cv2.countNonZero(thresh)
        total_pixels = thresh.size

        return motion_pixels / total_pixels


# Global analyzer instance
_analyzer = WaterLevelAnalyzer()


async def capture_frame_from_stream(url: str) -> Optional[any]:
    """Capture a single frame from RTSP/HTTP stream."""
    if not CV2_AVAILABLE:
        return None

    try:
        # Open video capture
        cap = cv2.VideoCapture(url)

        if not cap.isOpened():
            print(f"Could not open video stream: {url}")
            return None

        # Read a frame
        ret, frame = cap.read()
        cap.release()

        if not ret:
            return None

        return frame

    except Exception as e:
        print(f"Error capturing frame: {e}")
        return None


async def capture_and_analyze() -> VideoAnalysis:
    """Capture frame from camera and analyze water level."""
    global _baseline_level, _last_analysis

    settings = get_settings()
    now = datetime.utcnow()

    if not settings.camera_rtsp_url:
        return VideoAnalysis(
            timestamp=now,
            water_level_index=50.0,
            delta_from_baseline=None,
            anomaly_detected=False,
        )

    # Capture frame
    frame = await capture_frame_from_stream(settings.camera_rtsp_url)

    if frame is None:
        # Return last known analysis or default
        if _last_analysis:
            return VideoAnalysis(
                timestamp=now,
                water_level_index=_last_analysis.water_level_index,
                delta_from_baseline=_last_analysis.delta_from_baseline,
                anomaly_detected=False,
            )
        return VideoAnalysis(
            timestamp=now,
            water_level_index=50.0,
            delta_from_baseline=None,
            anomaly_detected=False,
        )

    # Analyze frame
    result = _analyzer.analyze_frame(frame)
    level_index = result.get("water_level_index", 50.0)

    # Calculate delta from baseline
    if _baseline_level is None:
        _baseline_level = level_index
        delta = 0.0
    else:
        delta = level_index - _baseline_level

    # Detect anomaly (rapid change)
    anomaly = False
    if _last_analysis:
        level_change = abs(level_index - _last_analysis.water_level_index)
        if level_change > 10:  # More than 10% change in one interval
            anomaly = True

    analysis = VideoAnalysis(
        timestamp=now,
        water_level_index=level_index,
        delta_from_baseline=delta,
        anomaly_detected=anomaly,
    )

    _last_analysis = analysis
    return analysis


async def set_baseline():
    """Set current level as baseline for comparison."""
    global _baseline_level

    analysis = await capture_and_analyze()
    _baseline_level = analysis.water_level_index

    return {"baseline_set": _baseline_level}


async def get_current_analysis() -> Optional[VideoAnalysis]:
    """Get the most recent analysis result."""
    return _last_analysis


def configure_analyzer(
    roi: Optional[tuple] = None,
    water_color_lower: Optional[list] = None,
    water_color_upper: Optional[list] = None,
):
    """Configure the video analyzer."""
    if roi:
        _analyzer.set_roi(*roi)

    if water_color_lower:
        _analyzer.water_color_range["lower"] = water_color_lower

    if water_color_upper:
        _analyzer.water_color_range["upper"] = water_color_upper


async def analyze_snapshot(image_data: bytes) -> VideoAnalysis:
    """Analyze a provided image snapshot."""
    now = datetime.utcnow()

    if not CV2_AVAILABLE:
        return VideoAnalysis(
            timestamp=now,
            water_level_index=50.0,
            delta_from_baseline=None,
            anomaly_detected=False,
        )

    try:
        # Decode image
        nparr = np.frombuffer(image_data, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            raise ValueError("Could not decode image")

        # Analyze
        result = _analyzer.analyze_frame(frame)
        level_index = result.get("water_level_index", 50.0)

        return VideoAnalysis(
            timestamp=now,
            water_level_index=level_index,
            delta_from_baseline=None,
            anomaly_detected=False,
        )

    except Exception as e:
        print(f"Error analyzing snapshot: {e}")
        return VideoAnalysis(
            timestamp=now,
            water_level_index=50.0,
            delta_from_baseline=None,
            anomaly_detected=False,
        )
