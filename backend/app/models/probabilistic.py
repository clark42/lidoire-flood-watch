"""JSON helpers for the probabilistic / observed-rain endpoints.

Their payloads are built as plain dicts by the services; ``api_json`` turns
them into the API contract: every datetime as ISO 8601 UTC with ``Z`` (naive
values are UTC), numpy scalars as Python numbers, NaN as null.
"""

import math
from datetime import date, datetime
from typing import Any

import numpy as np

from app.models.schemas import utc_isoformat


def api_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_isoformat(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): api_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [api_json(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value
