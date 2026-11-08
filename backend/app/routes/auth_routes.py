import time
from collections import OrderedDict
from math import ceil
from threading import Lock
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from app.auth import hash_password, verify_password, create_session, clear_session, get_user_from_session
from app.database import run_query
from app.email_service import send_reset_email

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
LOGIN_WINDOW_SECONDS = 60
login_windows = OrderedDict()
login_windows_lock = Lock()


def login_retry_after(email: str, source: str) -> int:
    limits = ((("account", email.strip().casefold()), 5), (("source", source), 30))
    with login_windows_lock:
        now = time.monotonic()
        while login_windows and next(iter(login_windows.values()))[0] <= now:
            login_windows.popitem(last=False)

        windows = [(key, limit, *login_windows.get(key, (now + LOGIN_WINDOW_SECONDS, 0)))
                   for key, limit in limits]
        retry_after = max((ceil(expires - now) for _, limit, expires, count in windows
                           if count >= limit), default=0)
        if retry_after:
            return retry_after
        for key, _, expires, count in windows:
            login_windows[key] = (expires, count + 1)
        return 0


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = get_user_from_session(request)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    retry_after = login_retry_after(email, request.client.host if request.client else "")
    if retry_after:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many login attempts. Try again shortly."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    user = run_query("SELECT * FROM users WHERE email=%s", (email,), fetchone=True)
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid email or password."}
        )

    token = create_session(user["id"])
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("session_token", token, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return resp


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@router.post("/register", response_class=HTMLResponse)
def register(request: Request, email: str = Form(...), full_name: str = Form(...), password: str = Form(...)):
    existing = run_query("SELECT id FROM users WHERE email=%s", (email,), fetchone=True)
    if existing:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Email already exists."}
        )
    run_query(
        """
        INSERT INTO users (email, full_name, password_hash, role)
        VALUES (%s, %s, %s, 'student')
        """,
        (email, full_name, hash_password(password)),
        commit=True,
    )
    return RedirectResponse("/login", status_code=302)


@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        clear_session(token)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("session_token")
    return resp


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request):
    return templates.TemplateResponse("reset_password.html", {"request": request, "mode": "request", "message": None})


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_request(request: Request, email: str = Form(...)):
    user = run_query("SELECT * FROM users WHERE email=%s", (email,), fetchone=True)
    if not user:
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "mode": "request", "message": "No user found with that email."},
        )

    token = str(int(time.time()))[-6:]
    run_query(
        "INSERT INTO password_reset_tokens (user_id, token) VALUES (%s, %s)",
        (user["id"], token),
        commit=True,
    )
    send_reset_email(email, token)
    return templates.TemplateResponse(
        "reset_password.html",
        {"request": request, "mode": "request", "message": f"Reset email sent to {email}."},
    )


@router.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_token_page(request: Request, token: str):
    return templates.TemplateResponse("reset_password.html", {"request": request, "mode": "set", "token": token, "message": None})


@router.post("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_token(request: Request, token: str, new_password: str = Form(...)):
    token_row = run_query(
        "SELECT * FROM password_reset_tokens WHERE token=%s ORDER BY created_at DESC LIMIT 1",
        (token,),
        fetchone=True,
    )
    if not token_row:
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "mode": "set", "token": token, "message": "Invalid token"},
        )

    run_query(
        "UPDATE users SET password_hash=%s WHERE id=%s",
        (hash_password(new_password), token_row["user_id"]),
        commit=True,
    )
    return RedirectResponse("/login", status_code=302)
