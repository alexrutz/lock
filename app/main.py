from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, photos
from .config import SESSION_COOKIE
from .db import init_db, vault_initialized

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
def index(request: Request):
    if not vault_initialized():
        return _redirect("/setup")
    key = auth.read_session_key(request)
    if key is None:
        return _redirect("/login")
    items = photos.list_photos(key)
    return templates.TemplateResponse(
        request, "gallery.html", {"photos": items}
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
async def upload(request: Request, file: UploadFile, key: bytes = Depends(auth.require_key)):
    photos.save_upload(key, file)
    return _redirect("/")


@app.get("/photo/{photo_id}")
def photo(photo_id: int, key: bytes = Depends(auth.require_key)):
    data, mime = photos.load_full(key, photo_id)
    return Response(content=data, media_type=mime)


@app.get("/thumb/{photo_id}")
def thumb(photo_id: int, key: bytes = Depends(auth.require_key)):
    data, mime = photos.load_thumb(key, photo_id)
    return Response(content=data, media_type=mime)


@app.post("/photo/{photo_id}/delete")
def delete(photo_id: int, key: bytes = Depends(auth.require_key)):
    photos.delete_photo(photo_id)
    return _redirect("/")
