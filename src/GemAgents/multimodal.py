from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any


def image_content_block(path: Path) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
    }

