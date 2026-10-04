"""A bot's profile photo, set by its owner (SPEC 8.7): any image becomes a square JPEG."""

import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SIDE = 640


class InvalidImage(ValueError):
    """The upload is not an image we can use."""


def square_jpeg(data: bytes) -> bytes:
    """The image center-cropped to a square, at most SIDE pixels, as JPEG (Telegram's format)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise InvalidImage("the image is larger than 10 MB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            side = min(image.size)
            image = ImageOps.fit(image, (side, side)).resize((min(side, SIDE),) * 2)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=90)
    except (UnidentifiedImageError, OSError) as e:
        raise InvalidImage("this file is not an image") from e
    return out.getvalue()
