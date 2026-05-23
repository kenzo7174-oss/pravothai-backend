"""Хранение аватаров операторов на диске."""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_LEGACY_AVATAR_DIR = Path(__file__).resolve().parent.parent / "uploads" / "avatars"
AVATAR_DIR = Path(
    os.getenv("AVATAR_UPLOAD_DIR", str(_BACKEND_ROOT / "uploads" / "avatars"))
)
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


def ensure_avatar_dir() -> Path:
    """Создаёт каталог для аватаров. Возвращает путь или бросает OSError."""
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    return AVATAR_DIR


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
    for base in (AVATAR_DIR, _LEGACY_AVATAR_DIR):
        path = base / safe_name
        if path.is_file():
            return path
    return None


def delete_operator_avatar_files(operator_id: int) -> None:
    for base in (AVATAR_DIR, _LEGACY_AVATAR_DIR):
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Cannot access avatar dir %s for cleanup (operator %s): %s", base, operator_id, exc)
            continue
        for path in base.glob(f"{operator_id}.*"):
            if path.is_file():
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("Failed to delete avatar file %s: %s", path, exc)


def save_operator_avatar(operator_id: int, content: bytes, ext: str) -> str:
    """Атомарно сохраняет аватар на диск. Возвращает имя файла."""
    ensure_avatar_dir()
    delete_operator_avatar_files(operator_id)
    dest = avatar_path_for_operator(operator_id, ext)
    tmp = dest.with_name(f"{dest.name}.tmp")
    try:
        tmp.write_bytes(content)
        tmp.replace(dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    if not dest.is_file():
        raise OSError(f"Avatar file was not written: {dest}")
    return dest.name
