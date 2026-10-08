"""Validate browser image inputs without reading arbitrary local files or URLs."""
from __future__ import annotations

import base64
import binascii

from .store import CoordinationError

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ENCODED_IMAGE_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
# Includes base64 expansion, filenames, JSON escaping, and the text message.
MAX_MESSAGE_BODY_BYTES = MAX_IMAGES * (MAX_ENCODED_IMAGE_BYTES + 2048) + 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def message_images(body: dict) -> tuple[str, list[dict]]:
    message = body.get("message", "")
    if not isinstance(message, str) or len(message) > 100000:
        raise CoordinationError("Message must contain at most 100000 characters.")
    message = message.strip()
    images = body.get("images", [])
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise CoordinationError(f"Attach at most {MAX_IMAGES} images per message.")
    validated = []
    for image in images:
        if not isinstance(image, dict):
            raise CoordinationError("Each image must include a data URL.")
        url = image.get("url")
        name = image.get("name", "Image")
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise CoordinationError("Image names must contain 1–255 characters.")
        if not isinstance(url, str) or len(url) > MAX_ENCODED_IMAGE_BYTES + 32:
            raise CoordinationError("Each image must be at most 5 MiB.")
        header, separator, encoded = url.partition(",")
        mime = header.removeprefix("data:").removesuffix(";base64")
        if not separator or mime not in IMAGE_TYPES or header != f"data:{mime};base64":
            raise CoordinationError("Choose a PNG, JPEG, WebP, or GIF image file.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CoordinationError("The image data could not be read. Attach the file again.") from exc
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise CoordinationError("Each image must be nonempty and at most 5 MiB.")
        signatures = {
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        }
        if not signatures[mime]:
            raise CoordinationError("The image contents do not match its file type.")
        validated.append({"name": name.strip(), "url": url})
    if not message and not validated:
        raise CoordinationError("Enter a message or attach an image.")
    command = message.split(maxsplit=1)[0].lower() if message else ""
    if validated and command in {"/cd", "/permissions", "/model", "/effort", "/help"}:
        raise CoordinationError("Remove attached images before using chat configuration commands.")
    return message, validated
