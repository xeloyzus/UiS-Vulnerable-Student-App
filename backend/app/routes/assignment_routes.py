from contextlib import closing
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from app.database import get_conn, run_query
from app.auth import get_user_from_session

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
LEGACY_UPLOAD_DIR = Path("app/uploads")
SUBMISSION_DIR = LEGACY_UPLOAD_DIR / "submissions"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
SUBMISSION_TYPES = {".pdf", ".docx"}


def valid_document(suffix: str, content: bytes) -> bool:
    if suffix == ".pdf":
        return content.startswith(b"%PDF-") and content.rstrip().endswith(b"%%EOF")
    if suffix != ".docx" or not content.startswith(b"PK\x03\x04"):
        return False
    try:
        with ZipFile(BytesIO(content)) as archive:
            manifest = archive.getinfo("[Content_Types].xml")
            if manifest.file_size > 64 * 1024 or "word/document.xml" not in archive.namelist():
                return False
            types = ElementTree.fromstring(archive.read(manifest))
            return any(part.get("PartName") == "/word/document.xml"
                       and part.get("ContentType") == DOCX_CONTENT_TYPE for part in types)
    except (BadZipFile, KeyError, RuntimeError, NotImplementedError, ElementTree.ParseError):
        return False


def existing_submission_path(stored_name: str | None) -> Path | None:
    if not stored_name or "/" in stored_name or "\\" in stored_name:
        return None
    for directory in (SUBMISSION_DIR, LEGACY_UPLOAD_DIR):
        path = (directory / stored_name).resolve()
        if path.parent == directory.resolve() and path.is_file():
            return path
    return None


@router.get("/assignments", response_class=HTMLResponse)
def assignments(request: Request):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    assignment_rows = run_query("SELECT * FROM assignments ORDER BY id", fetchall=True)
    submissions = run_query(
        "SELECT * FROM submissions WHERE user_id=%s ORDER BY submitted_at DESC",
        (user["id"],),
        fetchall=True,
    )
    return templates.TemplateResponse(
        "assignments.html",
        {"request": request, "user": user, "assignments": assignment_rows, "submissions": submissions},
    )


@router.post("/assignments/upload")
def upload_assignment(
    request: Request,
    assignment_id: int = Form(..., ge=1, le=2147483647),
    file: UploadFile = File(...),
):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    filename = file.filename or ""
    if len(filename) > 255 or "/" in filename or "\\" in filename or not filename.isprintable():
        raise HTTPException(status_code=400, detail="Invalid filename")
    suffix = Path(filename).suffix.lower()
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Maximum upload size is 10 MiB")
    if not valid_document(suffix, content):
        raise HTTPException(status_code=415, detail="Upload a valid PDF or DOCX document")

    digest = sha256(content).hexdigest()
    stored_filename = f"{digest}_{uuid4().hex}{suffix}"
    path = None
    try:
        with closing(get_conn()) as conn, conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM assignments WHERE id=%s", (assignment_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Assignment not found")

            # Serialize the replay check and insert, including across workers.
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (user["id"], assignment_id))
            cur.execute(
                "SELECT file_path FROM submissions WHERE user_id=%s AND assignment_id=%s AND filename=%s",
                (user["id"], assignment_id, filename),
            )
            for (stored_name,) in cur.fetchall():
                if stored_name and stored_name.startswith(f"{digest}_"):
                    raise HTTPException(status_code=409, detail="This submission already exists")
                previous = existing_submission_path(stored_name)
                if previous:
                    with previous.open("rb") as submitted:
                        if submitted.read(MAX_UPLOAD_BYTES + 1) == content:
                            raise HTTPException(status_code=409, detail="This submission already exists")

            SUBMISSION_DIR.mkdir(parents=True, exist_ok=True)
            destination = SUBMISSION_DIR / stored_filename
            with destination.open("xb") as out:
                path = destination
                out.write(content)
            cur.execute(
                """
                INSERT INTO submissions (assignment_id, user_id, filename, file_path, notes)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (assignment_id, user["id"], filename, stored_filename, "Uploaded through portal"),
            )
    except Exception:
        if path:
            path.unlink(missing_ok=True)
        raise

    return RedirectResponse("/assignments", status_code=302)


@router.get("/assignments/submissions/{submission_id}/download")
def download_submission(request: Request, submission_id: int):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    submission = run_query(
        "SELECT filename, file_path FROM submissions WHERE id=%s AND user_id=%s",
        (submission_id, user["id"]),
        fetchone=True,
    )
    if submission and Path(submission["filename"] or "").suffix.lower() in SUBMISSION_TYPES:
        path = existing_submission_path(submission["file_path"])
        if path:
            return FileResponse(
                path,
                filename=submission["filename"],
                media_type="application/octet-stream",
                headers={"X-Content-Type-Options": "nosniff"},
            )
    raise HTTPException(status_code=404, detail="Submission file not found")
