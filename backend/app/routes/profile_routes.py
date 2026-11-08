from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from app.database import run_query
from app.auth import get_user_from_session

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
UPLOAD_DIR = Path("app/uploads")
MAX_AVATAR_BYTES = 10 * 1024 * 1024


def avatar_type(header: bytes) -> tuple[str, str] | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    courses = run_query(
        """
        SELECT c.* FROM courses c
        JOIN course_registrations cr ON cr.course_id = c.id
        WHERE cr.user_id = %s
        """,
        (user["id"],),
        fetchall=True,
    )
    messages = run_query(
        "SELECT * FROM messages WHERE recipient_id=%s ORDER BY created_at DESC LIMIT 5",
        (user["id"],),
        fetchall=True,
    )
    grades = run_query(
        "SELECT * FROM grades WHERE student_id=%s ORDER BY updated_at DESC LIMIT 5",
        (user["id"],),
        fetchall=True,
    )
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "courses": courses, "messages": messages, "grades": grades},
    )


@router.get("/profile", response_class=HTMLResponse)
def my_profile(request: Request):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "profile": user})


@router.get("/profile/{user_id}", response_class=HTMLResponse)
def profile_by_id(request: Request, user_id: int):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user_id != user["id"]:
        raise HTTPException(status_code=403, detail="You can only view your own profile")
    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "profile": user})


@router.post("/profile/update")
def update_profile(
    request: Request,
    full_name: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    bio: str = Form(""),
):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    run_query(
        """
        UPDATE users SET full_name=%s, phone=%s, address=%s, bio=%s
        WHERE id=%s
        """,
        (full_name, phone, address, bio, user["id"]),
        commit=True,
    )
    return RedirectResponse(f"/profile/{user['id']}", status_code=302)


@router.post("/profile/avatar")
def upload_avatar(request: Request, avatar: UploadFile = File(...)):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    content = avatar.file.read(MAX_AVATAR_BYTES + 1)
    if len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="Maximum avatar size is 10 MiB")
    image_type = avatar_type(content[:12])
    if not image_type:
        raise HTTPException(status_code=415, detail="Upload a PNG, JPEG, GIF, or WebP image")
    suffix, _ = image_type
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"avatar_{user['id']}_{uuid4().hex}{suffix}"
    path = UPLOAD_DIR / filename
    with path.open("xb") as out:
        out.write(content)

    run_query("UPDATE users SET avatar_path=%s WHERE id=%s", (filename, user["id"]), commit=True)
    return RedirectResponse(f"/profile/{user['id']}", status_code=302)


@router.get("/uploads/{filename}")
def download_avatar(request: Request, filename: str):
    user = get_user_from_session(request)
    if not user or user["avatar_path"] != filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="Avatar not found")
    path = (UPLOAD_DIR / filename).resolve()
    if path.parent != UPLOAD_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="Avatar not found")
    with path.open("rb") as avatar:
        image_type = avatar_type(avatar.read(12))
    if not image_type:
        raise HTTPException(status_code=404, detail="Avatar not found")
    _, media_type = image_type
    return FileResponse(path, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})
