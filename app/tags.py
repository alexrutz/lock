"""Tag CRUD. Names are encrypted at rest; a deterministic HMAC hash
enforces uniqueness without leaking the cleartext name."""

from fastapi import HTTPException

from . import crypto
from .db import get_conn
from .sessions import _Session

MAX_TAG_LEN = 64


def _normalize(name: str) -> str:
    return " ".join(name.strip().split()).lower()


def _decrypt_name(session: _Session, tag_id: int, name_ct: bytes, name_nonce: bytes) -> str:
    cached = session.tag_name_cache.get(tag_id)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        name = crypto.decrypt(session.master_key, name_nonce, name_ct).decode("utf-8")
    except Exception:
        name = f"tag-{tag_id}"
    session.tag_name_cache.put(tag_id, name)
    return name


def get_or_create_tag(session: _Session, raw_name: str) -> tuple[int, str]:
    name = _normalize(raw_name)
    if not name:
        raise HTTPException(status_code=400, detail="Tag name required")
    if len(name) > MAX_TAG_LEN:
        raise HTTPException(status_code=400, detail=f"Tag name too long (>{MAX_TAG_LEN})")

    name_hash = crypto.tag_hash(session.master_key, name)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM tags WHERE name_hash = ?", (name_hash,)
        ).fetchone()
        if row is not None:
            tag_id = row["id"]
        else:
            nonce, ct = crypto.encrypt(session.master_key, name.encode("utf-8"))
            cur = conn.execute(
                "INSERT INTO tags (name_ct, name_nonce, name_hash) VALUES (?, ?, ?)",
                (ct, nonce, name_hash),
            )
            tag_id = cur.lastrowid
    session.tag_name_cache.put(tag_id, name)
    return tag_id, name


def list_all_tags(session: _Session) -> list[dict]:
    """Returns every tag with its photo count, sorted by name (case-insensitive)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT t.id, t.name_ct, t.name_nonce,
                      (SELECT COUNT(*) FROM photo_tags pt WHERE pt.tag_id = t.id) AS uses
               FROM tags t"""
        ).fetchall()
    out = [
        {
            "id": r["id"],
            "name": _decrypt_name(session, r["id"], r["name_ct"], r["name_nonce"]),
            "uses": r["uses"],
        }
        for r in rows
    ]
    out.sort(key=lambda t: t["name"])
    return out


def tags_for_photos(session: _Session, photo_ids: list[int]) -> dict[int, list[dict]]:
    if not photo_ids:
        return {}
    placeholders = ",".join("?" * len(photo_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT pt.photo_id, t.id, t.name_ct, t.name_nonce
                FROM photo_tags pt
                JOIN tags t ON t.id = pt.tag_id
                WHERE pt.photo_id IN ({placeholders})""",
            photo_ids,
        ).fetchall()
    out: dict[int, list[dict]] = {pid: [] for pid in photo_ids}
    for r in rows:
        name = _decrypt_name(session, r["id"], r["name_ct"], r["name_nonce"])
        out[r["photo_id"]].append({"id": r["id"], "name": name})
    for lst in out.values():
        lst.sort(key=lambda t: t["name"])
    return out


def add_tag_to_photo(session: _Session, photo_id: int, raw_name: str) -> list[dict]:
    tag_id, _ = get_or_create_tag(session, raw_name)
    with get_conn() as conn:
        # Reject if the photo doesn't exist (FK alone would also catch this,
        # but the user-facing 404 is nicer than 500).
        if conn.execute("SELECT 1 FROM photos WHERE id = ?", (photo_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        conn.execute(
            "INSERT OR IGNORE INTO photo_tags (photo_id, tag_id) VALUES (?, ?)",
            (photo_id, tag_id),
        )
    return tags_for_photos(session, [photo_id])[photo_id]


def remove_tag_from_photo(session: _Session, photo_id: int, tag_id: int) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM photo_tags WHERE photo_id = ? AND tag_id = ?",
            (photo_id, tag_id),
        )
        # Garbage-collect orphan tags so the tag filter doesn't fill up with
        # one-shot labels the user no longer uses.
        orphan = conn.execute(
            "SELECT 1 FROM photo_tags WHERE tag_id = ? LIMIT 1", (tag_id,)
        ).fetchone()
        if orphan is None:
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            session.tag_name_cache.pop(tag_id)
    return tags_for_photos(session, [photo_id])[photo_id]
