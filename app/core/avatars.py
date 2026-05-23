"""Хранение аватаров операторов на диске."""

from pathlib import Path

AVATAR_DIR = Path(__file__).resolve().parent.parent / "uploads" / "avatars"
MAX_AVATAR_BYTES = 5 * 1024 * 1024

CONTENT_TYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

EXT_TO_MEDIA_TYPE = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def ensure_avatar_dir() -> None:
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)


def detect_image_ext(content: bytes, content_type: str | None = None) -> str | None:
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    ct = (content_type or "").lower().split(";")[0].strip()
    return CONTENT_TYPE_TO_EXT.get(ct)


def media_type_for_ext(ext: str) -> str:
    return EXT_TO_MEDIA_TYPE.get(ext.lower(), "application/octet-stream")


def avatar_path_for_operator(operator_id: int, ext: str) -> Path:
    return AVATAR_DIR / f"{operator_id}{ext}"


def resolve_avatar_path(photo_path: str | None) -> Path | None:
    if not photo_path:
        return None
    safe_name = Path(photo_path).name
    if safe_name != photo_path or ".." in photo_path:
        return None
    path = AVATAR_DIR / safe_name
    return path if path.is_file() else None


def delete_operator_avatar_files(operator_id: int) -> None:
    ensure_avatar_dir()
    for path in AVATAR_DIR.glob(f"{operator_id}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)
