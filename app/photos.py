import io
from dataclasses import dataclass, field
from typing import Iterator

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps
from PIL.ExifTags import TAGS

from . import crypto, tags as tagmod
from .config import MAX_UPLOAD_BYTES, PHOTO_DIR, THUMB_DIR, THUMBNAIL_SIZE
from .db import get_conn
from .sessions import _Session


# EXIF fields that carry human-written descriptions. Windows' "Titel"
# (in the file-properties Beschreibung tab) writes XPTitle; Lightroom and
# friends use ImageDescription; UserComment is the EXIF standard.
_DESCRIPTION_TAGS = {
    "ImageDescription",
    "XPTitle",
    "XPSubject",
    "XPComment",
    "UserComment",
}


def _decode_exif_value(val) -> str:
    """Best-effort decode of an EXIF value into a Python string."""
    if isinstance(val, bytes):
        # XPTitle / XPSubject / XPComment are stored as UTF-16-LE with a
        # trailing NUL pair.
        for encoding in ("utf-16-le", "utf-8", "latin-1"):
            try:
                s = val.decode(encoding)
                if s and "\x00\x00" not in s[2:4]:  # plausible decode
                    return s.replace("\x00", "").strip()
            except UnicodeDecodeError:
                continue
        return ""
    if isinstance(val, str):
        return val.replace("\x00", "").strip()
    return ""


def _decode_exif_datetime(val) -> str:
    """Convert an EXIF datetime string (YYYY:MM:DD HH:MM:SS) to an
    ISO-ish string that sorts lexicographically. Returns '' on failure."""
    if isinstance(val, bytes):
        try:
            val = val.decode("ascii", errors="replace")
        except Exception:
            return ""
    if not isinstance(val, str):
        return ""
    val = val.replace("\x00", "").strip()
    if not val:
        return ""
    # EXIF uses colons in the date portion; only swap the first two so
    # the time portion's colons remain.
    date_part, _, time_part = val.partition(" ")
    date_iso = date_part.replace(":", "-", 2)
    return (f"{date_iso} {time_part}".strip()) or ""


# EXIF tag IDs we look for in the main IFD and the EXIF sub-IFD (0x8769).
_DATETIME_TAGS = (0x9003, 0x9004, 0x0132)  # Original, Digitized, ModifyDate


def extract_exif_metadata(image_bytes: bytes) -> dict:
    """Return {'description': str, 'taken_at': str} from an image's
    EXIF. 'taken_at' is an ISO-style string ('YYYY-MM-DD HH:MM:SS'),
    empty if the photo has no date tag."""
    result = {"description": "", "taken_at": ""}
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            exif = im.getexif()
            if not exif:
                return result

            # Description-like fields (Windows Titel + EXIF standard).
            seen: list[str] = []
            for tag_id, raw in exif.items():
                if TAGS.get(tag_id) in _DESCRIPTION_TAGS:
                    text = _decode_exif_value(raw)
                    if text and text not in seen:
                        seen.append(text)
            result["description"] = " ".join(seen)

            # Taken-at: try the main IFD, then the EXIF sub-IFD where
            # DateTimeOriginal usually lives.
            def _find_date(d):
                for tid in _DATETIME_TAGS:
                    if tid in d:
                        iso = _decode_exif_datetime(d[tid])
                        if iso:
                            return iso
                return ""

            taken = _find_date(exif)
            if not taken and hasattr(exif, "get_ifd"):
                try:
                    sub = exif.get_ifd(0x8769) or {}
                    taken = _find_date(sub)
                except Exception:
                    pass
            result["taken_at"] = taken
    except Exception:
        pass
    return result


def extract_description(image_bytes: bytes) -> str:
    # Back-compat wrapper used by the older reindex path.
    return extract_exif_metadata(image_bytes)["description"]

ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
STREAM_CHUNK = 64 * 1024

SORT_OPTIONS = ("name", "newest", "oldest", "rating", "taken")
DEFAULT_SORT = "name"


@dataclass
class PhotoMeta:
    id: int
    name: str
    mime: str
    size: int
    uploaded_at: str
    rating: int = 0
    hidden: bool = False
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

    meta = extract_exif_metadata(data)
    description = meta["description"]
    taken_at = meta["taken_at"]
    if description:
        desc_nonce, desc_ct = crypto.encrypt(master_key, description.encode("utf-8"))
    else:
        desc_nonce, desc_ct = None, None
    if taken_at:
        taken_nonce, taken_ct = crypto.encrypt(master_key, taken_at.encode("utf-8"))
    else:
        taken_nonce, taken_ct = None, None

    data_key, wrap_nonce, wrapped_key = crypto.wrap_data_key(master_key)
    nonce, ct = crypto.encrypt(data_key, data)

    thumb_key, thumb_wrap_nonce, thumb_wrapped_key = crypto.wrap_data_key(master_key)
    thumb_nonce, thumb_ct = crypto.encrypt(thumb_key, thumb_bytes)

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO photos
               (original_name_ct, name_nonce, mime, size,
                wrapped_key, wrap_nonce, nonce,
                thumb_wrapped_key, thumb_wrap_nonce, thumb_nonce,
                description_ct, description_nonce, description_indexed,
                exif_taken_ct, exif_taken_nonce, exif_v2_indexed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1)""",
            (
                name_ct, name_nonce, mime, len(data),
                wrapped_key, wrap_nonce, nonce,
                thumb_wrapped_key, thumb_wrap_nonce, thumb_nonce,
                desc_ct, desc_nonce,
                taken_ct, taken_nonce,
            ),
        )
        photo_id = cur.lastrowid

    (PHOTO_DIR / f"{photo_id}.bin").write_bytes(ct)
    (THUMB_DIR / f"{photo_id}.bin").write_bytes(thumb_ct)

    session.name_cache.put(photo_id, filename)
    session.thumb_cache.put(photo_id, thumb_bytes)
    if description:
        session.description_cache.put(photo_id, description)
    if taken_at:
        session.exif_date_cache.put(photo_id, taken_at)
    return photo_id


def _filter_clauses(
    min_rating: int,
    filter_tag_id: int | None,
    include_hidden: bool = False,
    id_filter: set[int] | None = None,
) -> tuple[str, list]:
    where: list[str] = []
    params: list = []
    if not include_hidden:
        where.append("hidden = 0")
    if min_rating > 0:
        where.append("rating >= ?")
        params.append(min_rating)
    if filter_tag_id is not None:
        where.append("id IN (SELECT photo_id FROM photo_tags WHERE tag_id = ?)")
        params.append(filter_tag_id)
    if id_filter is not None:
        if not id_filter:
            # Empty search match — force zero rows without breaking SQL.
            where.append("1 = 0")
        else:
            placeholders = ",".join("?" * len(id_filter))
            where.append(f"id IN ({placeholders})")
            params.extend(id_filter)
    sql = ("WHERE " + " AND ".join(where)) if where else ""
    return sql, params


def count_photos(
    min_rating: int = 0,
    filter_tag_id: int | None = None,
    include_hidden: bool = False,
    id_filter: set[int] | None = None,
) -> int:
    where_sql, params = _filter_clauses(min_rating, filter_tag_id, include_hidden, id_filter)
    with get_conn() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) AS c FROM photos {where_sql}", params
        ).fetchone()["c"])


def list_photos_page(
    session: _Session,
    *,
    limit: int,
    offset: int,
    sort: str = DEFAULT_SORT,
    min_rating: int = 0,
    filter_tag_id: int | None = None,
    include_hidden: bool = False,
    id_filter: set[int] | None = None,
) -> list[PhotoMeta]:
    if sort not in SORT_OPTIONS:
        sort = DEFAULT_SORT
    where_sql, where_params = _filter_clauses(
        min_rating, filter_tag_id, include_hidden, id_filter,
    )

    # Filename and EXIF-date sorting can't be done in SQL because both
    # fields are encrypted. Decrypt them into memory, sort, then slice
    # the requested page. With the per-session caches this is fast for
    # repeat queries.
    if sort in ("name", "taken"):
        return _python_sorted_page(
            session, sort=sort, limit=limit, offset=offset,
            where_sql=where_sql, where_params=where_params,
        )

    order_sql = {
        "newest": "ORDER BY uploaded_at DESC, id DESC",
        "oldest": "ORDER BY uploaded_at ASC,  id ASC",
        "rating": "ORDER BY rating DESC, uploaded_at DESC, id DESC",
    }[sort]

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, original_name_ct, name_nonce, mime, size,
                       uploaded_at, rating, hidden
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
            hidden=bool(r["hidden"]),
            tags=tag_map.get(pid, []),
        ))
    return out


def random_photo(
    session: _Session,
    *,
    min_rating: int = 0,
    filter_tag_id: int | None = None,
    include_hidden: bool = False,
    id_filter: set[int] | None = None,
) -> PhotoMeta | None:
    where_sql, where_params = _filter_clauses(
        min_rating, filter_tag_id, include_hidden, id_filter,
    )
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT id, original_name_ct, name_nonce, mime, size,
                       uploaded_at, rating, hidden
                FROM photos {where_sql}
                ORDER BY RANDOM() LIMIT 1""",
            where_params,
        ).fetchone()
    if row is None:
        return None

    pid = row["id"]
    cached = session.name_cache.get(pid)
    if cached is not None:
        name = cached  # type: ignore[assignment]
    else:
        try:
            name = crypto.decrypt(
                session.master_key, row["name_nonce"], row["original_name_ct"]
            ).decode("utf-8")
        except Exception:
            name = f"photo-{pid}"
        session.name_cache.put(pid, name)

    return PhotoMeta(
        id=pid,
        name=name,  # type: ignore[arg-type]
        mime=row["mime"],
        size=row["size"],
        uploaded_at=row["uploaded_at"],
        rating=row["rating"] or 0,
        hidden=bool(row["hidden"]),
    )


def search_by_description(session: _Session, query: str) -> set[int]:
    """Return the set of photo IDs whose EXIF description contains
    `query` (case-insensitive substring). Returns empty set if nothing
    matches; the caller should pass this through as an id_filter."""
    needle = query.strip().lower()
    if not needle:
        return set()
    matching: set[int] = set()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, description_ct, description_nonce "
            "FROM photos WHERE description_ct IS NOT NULL"
        ).fetchall()
    for r in rows:
        pid = r["id"]
        cached = session.description_cache.get(pid)
        if cached is None:
            try:
                cached = crypto.decrypt(
                    session.master_key, r["description_nonce"], r["description_ct"]
                ).decode("utf-8")
            except Exception:
                cached = ""
            session.description_cache.put(pid, cached)
        if needle in cached.lower():  # type: ignore[union-attr]
            matching.add(pid)
    return matching


def reindex_pending_count() -> int:
    with get_conn() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE exif_v2_indexed = 0"
        ).fetchone()["c"])


def reindex_descriptions_batch(session: _Session, limit: int = 100) -> dict:
    """Process up to `limit` photos that haven't been EXIF-scanned for
    the current metadata set. Decrypts the original, pulls description
    + taken-at, re-encrypts both, and flips exif_v2_indexed=1."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, wrapped_key, wrap_nonce, nonce "
            "FROM photos WHERE exif_v2_indexed = 0 LIMIT ?",
            (limit,),
        ).fetchall()

    master_key = session.master_key
    processed = 0
    for r in rows:
        pid = r["id"]
        path = PHOTO_DIR / f"{pid}.bin"
        desc, taken = "", ""
        if path.exists():
            try:
                data_key = crypto.unwrap_data_key(master_key, r["wrap_nonce"], r["wrapped_key"])
                plaintext = crypto.decrypt(data_key, r["nonce"], path.read_bytes())
                meta = extract_exif_metadata(plaintext)
                desc, taken = meta["description"], meta["taken_at"]
            except Exception:
                pass

        desc_ct = desc_nonce = None
        taken_ct = taken_nonce = None
        if desc:
            desc_nonce, desc_ct = crypto.encrypt(master_key, desc.encode("utf-8"))
        if taken:
            taken_nonce, taken_ct = crypto.encrypt(master_key, taken.encode("utf-8"))

        with get_conn() as conn:
            conn.execute(
                "UPDATE photos SET description_ct = ?, description_nonce = ?, "
                "description_indexed = 1, "
                "exif_taken_ct = ?, exif_taken_nonce = ?, exif_v2_indexed = 1 "
                "WHERE id = ?",
                (desc_ct, desc_nonce, taken_ct, taken_nonce, pid),
            )
        if desc:
            session.description_cache.put(pid, desc)
        if taken:
            session.exif_date_cache.put(pid, taken)
        processed += 1

    remaining = reindex_pending_count()
    return {"processed": processed, "remaining": remaining}


def set_hidden(photo_id: int, hidden: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE photos SET hidden = ? WHERE id = ?",
            (1 if hidden else 0, photo_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404)
    return hidden


def _python_sorted_page(
    session: _Session,
    *,
    sort: str,
    limit: int,
    offset: int,
    where_sql: str,
    where_params: list,
) -> list[PhotoMeta]:
    master_key = session.master_key
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, original_name_ct, name_nonce, mime, size,
                       uploaded_at, rating, hidden,
                       exif_taken_ct, exif_taken_nonce
                FROM photos {where_sql}""",
            where_params,
        ).fetchall()

    keyed: list[tuple] = []
    for r in rows:
        pid = r["id"]
        if sort == "name":
            cached = session.name_cache.get(pid)
            if cached is None:
                try:
                    cached = crypto.decrypt(
                        master_key, r["name_nonce"], r["original_name_ct"]
                    ).decode("utf-8")
                except Exception:
                    cached = f"photo-{pid}"
                session.name_cache.put(pid, cached)
            keyed.append(((cached.casefold(), pid), r, cached))
        else:  # taken — newest taken first; undated photos fall back to upload date
            taken = None
            if r["exif_taken_ct"] is not None:
                cached = session.exif_date_cache.get(pid)
                if cached is None:
                    try:
                        cached = crypto.decrypt(
                            master_key, r["exif_taken_nonce"], r["exif_taken_ct"]
                        ).decode("utf-8")
                    except Exception:
                        cached = ""
                    session.exif_date_cache.put(pid, cached)
                taken = cached or None
            sort_key = (1 if taken else 0, taken or r["uploaded_at"], pid)
            keyed.append((sort_key, r, None))

    if sort == "name":
        keyed.sort(key=lambda t: t[0])  # A → Z
    else:
        keyed.sort(key=lambda t: t[0], reverse=True)  # newest taken first

    page = keyed[offset:offset + limit]
    ids = [r["id"] for _, r, _ in page]
    tag_map = tagmod.tags_for_photos(session, ids)

    out: list[PhotoMeta] = []
    for _, r, decrypted_name in page:
        pid = r["id"]
        name = decrypted_name
        if name is None:
            cached = session.name_cache.get(pid)
            if cached is not None:
                name = cached
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
            name=name,
            mime=r["mime"],
            size=r["size"],
            uploaded_at=r["uploaded_at"],
            rating=r["rating"] or 0,
            hidden=bool(r["hidden"]),
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


def all_photo_ids() -> list[int]:
    with get_conn() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM photos ORDER BY uploaded_at DESC, id DESC"
        ).fetchall()]


_FORBIDDEN_FILENAME_CHARS = '/\\:*?"<>|\0'


def _safe_filename(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    base = "".join("_" if c in _FORBIDDEN_FILENAME_CHARS else c for c in base)
    base = base.lstrip(".") or "photo"
    return base[:200]


def _dedup_filenames(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen[n] = 1
            out.append(n)
            continue
        seen[n] += 1
        if "." in n:
            stem, ext = n.rsplit(".", 1)
            out.append(f"{stem} ({seen[n]}).{ext}")
        else:
            out.append(f"{n} ({seen[n]})")
    return out


def iter_export(session: _Session, photo_ids: list[int]):
    """Yield (filename, decrypted_bytes) for each requested photo, in order.

    Decryption happens lazily as the consumer pulls — combined with
    stream-zip this lets us serve a multi-GB export without ever holding
    more than one photo in memory.
    """
    if not photo_ids:
        return

    placeholders = ",".join("?" * len(photo_ids))
    with get_conn() as conn:
        rows = {r["id"]: r for r in conn.execute(
            f"""SELECT id, original_name_ct, name_nonce, mime,
                       wrapped_key, wrap_nonce, nonce
                FROM photos WHERE id IN ({placeholders})""",
            photo_ids,
        ).fetchall()}

    raw_names: list[str] = []
    ordered_rows = []
    for pid in photo_ids:
        r = rows.get(pid)
        if r is None:
            continue
        cached = session.name_cache.get(pid)
        if cached is not None:
            name = cached  # type: ignore[assignment]
        else:
            try:
                name = crypto.decrypt(
                    session.master_key, r["name_nonce"], r["original_name_ct"]
                ).decode("utf-8")
            except Exception:
                name = f"photo-{pid}"
            session.name_cache.put(pid, name)
        raw_names.append(_safe_filename(name))  # type: ignore[arg-type]
        ordered_rows.append(r)

    final_names = _dedup_filenames(raw_names)

    for name, r in zip(final_names, ordered_rows):
        path = PHOTO_DIR / f"{r['id']}.bin"
        if not path.exists():
            continue
        data_key = crypto.unwrap_data_key(
            session.master_key, r["wrap_nonce"], r["wrapped_key"]
        )
        try:
            plaintext = crypto.decrypt(data_key, r["nonce"], path.read_bytes())
        except Exception:
            continue
        yield name, plaintext


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
