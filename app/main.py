from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, photos
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request, page: int = 1):
    if not vault_initialized():
        return _redirect("/setup")
    session = auth.read_session(request)
    if session is None:
        return _redirect("/login")

    if page < 1:
        page = 1
    total = photos.count_photos()
    pages = max(1, (total + GALLERY_PAGE_SIZE - 1) // GALLERY_PAGE_SIZE)
    if page > pages:
        page = pages
    offset = (page - 1) * GALLERY_PAGE_SIZE
    items = photos.list_photos_page(session, limit=GALLERY_PAGE_SIZE, offset=offset)

    return templates.TemplateResponse(
        request, "gallery.html",
        {
            "photos": items,
            "page": page,
            "pages": pages,
            "total": total,
            "page_size": GALLERY_PAGE_SIZE,
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
        except Exception as e:  # noqa: BLE001 — never let one bad file kill the batch
            results.append({"name": f.filename, "ok": False, "error": str(e)})

    wants_json = "application/json" in (request.headers.get("accept") or "")
    if wants_json:
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
    # Browser may keep thumbnails in its memory cache for the duration of
    # the session, but we never want them written to its disk cache.
    return Response(
        content=data, media_type=mime,
        headers={"Cache-Control": "private, no-store"},
    )


@app.post("/photo/{photo_id}/delete")
def delete(photo_id: int, session: _Session = Depends(auth.require_session)):
    photos.delete_photo(session, photo_id)
    return _redirect("/")
