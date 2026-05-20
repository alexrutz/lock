from datetime import datetime, timezone
from pathlib import Path
from stat import S_IFREG

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from stream_zip import ZIP_64, stream_zip

from . import auth, photos, tags as tagmod
from .config import GALLERY_PAGE_SIZE, SESSION_COOKIE
from .db import init_db, vault_initialized
from .sessions import _Session

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Lock")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _redirect(target: str) -> RedirectResponse:
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    page: int = 1,
    sort: str = "newest",
    min_rating: int = 0,
    tag: int | None = None,
):
    if not vault_initialized():
        return _redirect("/setup")
    session = auth.read_session(request)
    if session is None:
        return _redirect("/login")

    if sort not in photos.SORT_OPTIONS:
        sort = "newest"
    min_rating = max(0, min(5, min_rating))
    if page < 1:
        page = 1

    all_tags = tagmod.list_all_tags(session)
    valid_tag_ids = {t["id"] for t in all_tags}
    if tag is not None and tag not in valid_tag_ids:
        tag = None

    total = photos.count_photos(min_rating=min_rating, filter_tag_id=tag)
    pages = max(1, (total + GALLERY_PAGE_SIZE - 1) // GALLERY_PAGE_SIZE)
    if page > pages:
        page = pages
    offset = (page - 1) * GALLERY_PAGE_SIZE
    items = photos.list_photos_page(
        session,
        limit=GALLERY_PAGE_SIZE, offset=offset,
        sort=sort, min_rating=min_rating, filter_tag_id=tag,
    )

    return templates.TemplateResponse(
        request, "gallery.html",
        {
            "photos": items,
            "page": page,
            "pages": pages,
            "total": total,
            "page_size": GALLERY_PAGE_SIZE,
            "sort": sort,
            "min_rating": min_rating,
            "active_tag": tag,
            "all_tags": all_tags,
        },
    )


@app.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    if vault_initialized():
        return _redirect("/login")
    return templates.TemplateResponse(request, "setup.html", {"result": None, "error": None})


@app.post("/setup", response_class=HTMLResponse)
def setup_post(request: Request, password: str = Form(...), confirm: str = Form(...)):
    if vault_initialized():
        return _redirect("/login")
    if password != confirm:
        return templates.TemplateResponse(
            request, "setup.html",
            {"result": None, "error": "Passwords do not match"},
            status_code=400,
        )
    try:
        secret, qr = auth.setup_vault(password)
    except HTTPException as e:
        return templates.TemplateResponse(
            request, "setup.html",
            {"result": None, "error": e.detail},
            status_code=e.status_code,
        )
    return templates.TemplateResponse(
        request, "setup.html",
        {"result": {"secret": secret, "qr": qr}, "error": None},
    )


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if not vault_initialized():
        return _redirect("/setup")
    if auth.read_session_key(request) is not None:
        return _redirect("/")
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_post(request: Request, password: str = Form(...), totp: str = Form(...)):
    if not vault_initialized():
        return _redirect("/setup")
    try:
        master_key = auth.authenticate(password, totp)
    except HTTPException as e:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": e.detail},
            status_code=e.status_code,
        )
    name, value = auth.make_session_cookie(master_key)
    resp = _redirect("/")
    resp.set_cookie(
        name, value,
        httponly=True, samesite="lax", secure=False, path="/",
    )
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.destroy_session(request)
    resp = _redirect("/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.post("/upload")
def upload(
    request: Request,
    files: list[UploadFile],
    session: _Session = Depends(auth.require_session),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    results: list[dict] = []
    for f in files:
        try:
            pid = photos.save_upload(session, f)
            results.append({"name": f.filename, "id": pid, "ok": True})
        except HTTPException as e:
            results.append({"name": f.filename, "ok": False, "error": e.detail})
        except Exception as e:  # noqa: BLE001
            results.append({"name": f.filename, "ok": False, "error": str(e)})

    if _wants_json(request):
        ok = sum(1 for r in results if r["ok"])
        return JSONResponse(
            {"results": results, "ok": ok, "failed": len(results) - ok}
        )
    return _redirect("/")


@app.get("/photo/{photo_id}")
def photo(photo_id: int, session: _Session = Depends(auth.require_session)):
    chunks, mime, size = photos.load_full_stream(session, photo_id)
    return StreamingResponse(
        chunks,
        media_type=mime,
        headers={"Content-Length": str(size), "Cache-Control": "private, no-store"},
    )


@app.get("/thumb/{photo_id}")
def thumb(photo_id: int, session: _Session = Depends(auth.require_session)):
    data, mime = photos.load_thumb(session, photo_id)
    return Response(
        content=data, media_type=mime,
        headers={"Cache-Control": "private, no-store"},
    )


@app.post("/photo/{photo_id}/delete")
def delete(photo_id: int, session: _Session = Depends(auth.require_session)):
    photos.delete_photo(session, photo_id)
    return _redirect("/")


@app.post("/photo/{photo_id}/rate")
def rate(
    request: Request,
    photo_id: int,
    rating: int = Form(...),
    session: _Session = Depends(auth.require_session),
):
    new_rating = photos.set_rating(photo_id, rating)
    if _wants_json(request):
        return {"id": photo_id, "rating": new_rating}
    return _redirect("/")


@app.post("/photo/{photo_id}/tag")
def tag_add(
    request: Request,
    photo_id: int,
    name: str = Form(...),
    session: _Session = Depends(auth.require_session),
):
    photo_tags = tagmod.add_tag_to_photo(session, photo_id, name)
    if _wants_json(request):
        return {"id": photo_id, "tags": photo_tags}
    return _redirect("/")


@app.post("/export")
def export(
    ids: list[int] = Form(default=[]),
    all: bool = Form(default=False),
    session: _Session = Depends(auth.require_session),
):
    if all:
        photo_ids = photos.all_photo_ids()
    else:
        photo_ids = ids

    if not photo_ids:
        raise HTTPException(status_code=400, detail="No photos selected")

    # Hard cap to keep request resource use bounded — adjust via env later
    # if a single export of >10 000 photos is a real workflow.
    if len(photo_ids) > 10_000:
        raise HTTPException(status_code=413, detail="Too many photos in one export")

    now = datetime.now(timezone.utc)
    perms = S_IFREG | 0o600

    def members():
        for filename, data in photos.iter_export(session, photo_ids):
            yield filename, now, perms, ZIP_64, (data,)

    fname = f"lock-export-{now.strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        stream_zip(members()),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/photo/{photo_id}/untag")
def tag_remove(
    request: Request,
    photo_id: int,
    tag_id: int = Form(...),
    session: _Session = Depends(auth.require_session),
):
    photo_tags = tagmod.remove_tag_from_photo(session, photo_id, tag_id)
    if _wants_json(request):
        return {"id": photo_id, "tags": photo_tags}
    return _redirect("/")
