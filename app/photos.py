import io
from dataclasses import dataclass

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

from . import crypto
from .config import MAX_UPLOAD_BYTES, PHOTO_DIR, THUMB_DIR, THUMBNAIL_SIZE
from .db import get_conn

ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@dataclass
class PhotoMeta:
    id: int
    name: str
    mime: str
    size: int
    uploaded_at: str


def _make_thumbnail(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail(THUMBNAIL_SIZE)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=82)
        return out.getvalue()


def save_upload(master_key: bytes, upload: UploadFile) -> int:
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    mime = upload.content_type or "application/octet-stream"
    if mime not in ALLOWED_MIMES:
        raise HTTPException(status_code=415, detail=f"Unsupported type: {mime}")

    try:
        thumb_bytes = _make_thumbnail(data)
    except Exception:
        raise HTTPException(status_code=400, detail="Not a valid image")

    name_nonce, name_ct = crypto.encrypt(master_key, (upload.filename or "photo").encode("utf-8"))

    data_key, wrap_nonce, wrapped_key = crypto.wrap_data_key(master_key)
    nonce, ct = crypto.encrypt(data_key, data)

    thumb_key, thumb_wrap_nonce, thumb_wrapped_key = crypto.wrap_data_key(master_key)
    thumb_nonce, thumb_ct = crypto.encrypt(thumb_key, thumb_bytes)

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO photos
               (original_name_ct, name_nonce, mime, size,
                wrapped_key, wrap_nonce, nonce,
                thumb_wrapped_key, thumb_wrap_nonce, thumb_nonce)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name_ct, name_nonce, mime, len(data),
                wrapped_key, wrap_nonce, nonce,
                thumb_wrapped_key, thumb_wrap_nonce, thumb_nonce,
            ),
        )
        photo_id = cur.lastrowid

    (PHOTO_DIR / f"{photo_id}.bin").write_bytes(ct)
    (THUMB_DIR / f"{photo_id}.bin").write_bytes(thumb_ct)
    return photo_id


def list_photos(master_key: bytes) -> list[PhotoMeta]:
    out: list[PhotoMeta] = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, original_name_ct, name_nonce, mime, size, uploaded_at "
            "FROM photos ORDER BY uploaded_at DESC, id DESC"
        ).fetchall()
    for r in rows:
        try:
            name = crypto.decrypt(master_key, r["name_nonce"], r["original_name_ct"]).decode("utf-8")
        except Exception:
            name = f"photo-{r['id']}"
        out.append(PhotoMeta(
            id=r["id"],
            name=name,
            mime=r["mime"],
            size=r["size"],
            uploaded_at=r["uploaded_at"],
        ))
    return out


def _load(master_key: bytes, photo_id: int, *, thumb: bool) -> tuple[bytes, str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT mime, wrapped_key, wrap_nonce, nonce, "
            "thumb_wrapped_key, thumb_wrap_nonce, thumb_nonce FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404)

    if thumb:
        wrapped, wrap_nonce, nonce = row["thumb_wrapped_key"], row["thumb_wrap_nonce"], row["thumb_nonce"]
        path = THUMB_DIR / f"{photo_id}.bin"
        mime = "image/jpeg"
    else:
        wrapped, wrap_nonce, nonce = row["wrapped_key"], row["wrap_nonce"], row["nonce"]
        path = PHOTO_DIR / f"{photo_id}.bin"
        mime = row["mime"]

    if not path.exists():
        raise HTTPException(status_code=404)

    data_key = crypto.unwrap_data_key(master_key, wrap_nonce, wrapped)
    try:
        plaintext = crypto.decrypt(data_key, nonce, path.read_bytes())
    except Exception:
        raise HTTPException(status_code=500, detail="Decryption failed")
    return plaintext, mime


def load_full(master_key: bytes, photo_id: int) -> tuple[bytes, str]:
    return _load(master_key, photo_id, thumb=False)


def load_thumb(master_key: bytes, photo_id: int) -> tuple[bytes, str]:
    return _load(master_key, photo_id, thumb=True)


def delete_photo(photo_id: int) -> None:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404)
    for d in (PHOTO_DIR, THUMB_DIR):
        f = d / f"{photo_id}.bin"
        if f.exists():
            f.unlink()
