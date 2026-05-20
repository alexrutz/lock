import io
from dataclasses import dataclass, field
from typing import Iterator

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

from . import crypto, tags as tagmod
from .config import MAX_UPLOAD_BYTES, PHOTO_DIR, THUMB_DIR, THUMBNAIL_SIZE
from .db import get_conn
from .sessions import _Session

ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
STREAM_CHUNK = 64 * 1024

SORT_OPTIONS = ("newest", "oldest", "rating")


@dataclass
class PhotoMeta:
    id: int
    name: str
    mime: str
    size: int
    uploaded_at: str
    rating: int = 0
    tags: list[dict] = field(default_factory=list)


def _make_thumbnail(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail(THUMBNAIL_SIZE)
        # JPEG has no alpha channel. Composite RGBA / LA onto white so PNGs
        # with transparency don't crash with "cannot write mode RGBA as JPEG".
        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
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

    session.name_cache.put(photo_id, filename)
    session.thumb_cache.put(photo_id, thumb_bytes)
    return photo_id


def _filter_clauses(min_rating: int, filter_tag_id: int | None) -> tuple[str, list]:
    where: list[str] = []
    params: list = []
    if min_rating > 0:
        where.append("rating >= ?")
        params.append(min_rating)
    if filter_tag_id is not None:
        where.append("id IN (SELECT photo_id FROM photo_tags WHERE tag_id = ?)")
        params.append(filter_tag_id)
    sql = ("WHERE " + " AND ".join(where)) if where else ""
    return sql, params


def count_photos(min_rating: int = 0, filter_tag_id: int | None = None) -> int:
    where_sql, params = _filter_clauses(min_rating, filter_tag_id)
    with get_conn() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) AS c FROM photos {where_sql}", params
        ).fetchone()["c"])


def list_photos_page(
    session: _Session,
    *,
    limit: int,
    offset: int,
    sort: str = "newest",
    min_rating: int = 0,
    filter_tag_id: int | None = None,
) -> list[PhotoMeta]:
    if sort not in SORT_OPTIONS:
        sort = "newest"
    order_sql = {
        "newest": "ORDER BY uploaded_at DESC, id DESC",
        "oldest": "ORDER BY uploaded_at ASC,  id ASC",
        "rating": "ORDER BY rating DESC, uploaded_at DESC, id DESC",
    }[sort]
    where_sql, where_params = _filter_clauses(min_rating, filter_tag_id)

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, original_name_ct, name_nonce, mime, size,
                       uploaded_at, rating
                FROM photos
                {where_sql}
                {order_sql}
                LIMIT ? OFFSET ?""",
            [*where_params, limit, offset],
        ).fetchall()

    ids = [r["id"] for r in rows]
    tag_map = tagmod.tags_for_photos(session, ids)

    out: list[PhotoMeta] = []
    master_key = session.master_key
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
            rating=r["rating"] or 0,
            tags=tag_map.get(pid, []),
        ))
    return out


def set_rating(photo_id: int, rating: int) -> int:
    if not 0 <= rating <= 5:
        raise HTTPException(status_code=400, detail="Rating must be 0–5")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE photos SET rating = ? WHERE id = ?", (rating, photo_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404)
    return rating


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
        # Find every tag the photo holds before we delete, so we can
        # garbage-collect tags that become orphan as a result.
        tag_ids = [r["tag_id"] for r in conn.execute(
            "SELECT tag_id FROM photo_tags WHERE photo_id = ?", (photo_id,)
        ).fetchall()]
        cur = conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404)
        # photo_tags rows are removed by ON DELETE CASCADE.
        for tid in tag_ids:
            orphan = conn.execute(
                "SELECT 1 FROM photo_tags WHERE tag_id = ? LIMIT 1", (tid,)
            ).fetchone()
            if orphan is None:
                conn.execute("DELETE FROM tags WHERE id = ?", (tid,))
                session.tag_name_cache.pop(tid)

    for d in (PHOTO_DIR, THUMB_DIR):
        f = d / f"{photo_id}.bin"
        if f.exists():
            f.unlink()

    from .sessions import store
    store.evict_photo(photo_id)
