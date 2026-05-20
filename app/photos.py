import io
from dataclasses import dataclass
from typing import Iterator

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

from . import crypto
from .config import MAX_UPLOAD_BYTES, PHOTO_DIR, THUMB_DIR, THUMBNAIL_SIZE
from .db import get_conn
from .sessions import _Session

ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
STREAM_CHUNK = 64 * 1024


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


def save_upload(session: _Session, upload: UploadFile) -> int:
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

    master_key = session.master_key
    filename = upload.filename or "photo"
    name_nonce, name_ct = crypto.encrypt(master_key, filename.encode("utf-8"))

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

    # Warm the per-session caches so the new photo renders without an extra
    # decrypt round-trip on the next gallery view.
    session.name_cache.put(photo_id, filename)
    session.thumb_cache.put(photo_id, thumb_bytes)
    return photo_id


def count_photos() -> int:
    with get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"])


def list_photos_page(session: _Session, *, limit: int, offset: int) -> list[PhotoMeta]:
    master_key = session.master_key
    out: list[PhotoMeta] = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, original_name_ct, name_nonce, mime, size, uploaded_at "
            "FROM photos ORDER BY uploaded_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    for r in rows:
        pid = r["id"]
        cached = session.name_cache.get(pid)
        if cached is not None:
            name = cached  # type: ignore[assignment]
        else:
            try:
                name = crypto.decrypt(
                    master_key, r["name_nonce"], r["original_name_ct"]
                ).decode("utf-8")
            except Exception:
                name = f"photo-{pid}"
            session.name_cache.put(pid, name)
        out.append(PhotoMeta(
            id=pid,
            name=name,  # type: ignore[arg-type]
            mime=r["mime"],
            size=r["size"],
            uploaded_at=r["uploaded_at"],
        ))
    return out


def _row_for(photo_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT mime, wrapped_key, wrap_nonce, nonce, "
            "thumb_wrapped_key, thumb_wrap_nonce, thumb_nonce "
            "FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404)
    return row


def load_thumb(session: _Session, photo_id: int) -> tuple[bytes, str]:
    cached = session.thumb_cache.get(photo_id)
    if cached is not None:
        return cached, "image/jpeg"  # type: ignore[return-value]

    row = _row_for(photo_id)
    path = THUMB_DIR / f"{photo_id}.bin"
    if not path.exists():
        raise HTTPException(status_code=404)

    data_key = crypto.unwrap_data_key(
        session.master_key, row["thumb_wrap_nonce"], row["thumb_wrapped_key"]
    )
    try:
        plaintext = crypto.decrypt(data_key, row["thumb_nonce"], path.read_bytes())
    except Exception:
        raise HTTPException(status_code=500, detail="Decryption failed")
    session.thumb_cache.put(photo_id, plaintext)
    return plaintext, "image/jpeg"


def load_full_stream(session: _Session, photo_id: int) -> tuple[Iterator[bytes], str, int]:
    row = _row_for(photo_id)
    path = PHOTO_DIR / f"{photo_id}.bin"
    if not path.exists():
        raise HTTPException(status_code=404)

    data_key = crypto.unwrap_data_key(
        session.master_key, row["wrap_nonce"], row["wrapped_key"]
    )
    try:
        plaintext = crypto.decrypt(data_key, row["nonce"], path.read_bytes())
    except Exception:
        raise HTTPException(status_code=500, detail="Decryption failed")

    size = len(plaintext)
    mime = row["mime"]

    def chunks() -> Iterator[bytes]:
        view = memoryview(plaintext)
        for i in range(0, size, STREAM_CHUNK):
            yield bytes(view[i:i + STREAM_CHUNK])

    return chunks(), mime, size


def delete_photo(session: _Session, photo_id: int) -> None:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404)
    for d in (PHOTO_DIR, THUMB_DIR):
        f = d / f"{photo_id}.bin"
        if f.exists():
            f.unlink()
    # Evict from every active session, not just the caller's.
    from .sessions import store
    store.evict_photo(photo_id)
