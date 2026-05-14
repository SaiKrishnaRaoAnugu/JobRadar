import sys
import os
import traceback

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from sqlalchemy.orm import Session
import io
import json
import boto3
from botocore.config import Config as BotoConfig
import pdfplumber
from docx import Document as DocxDocument
from groq import Groq

from database import engine, get_db, Base
from models import User, Application, Resume
from auth import (
    create_token, require_user, exchange_code_for_user,
    google_auth_url, GOOGLE_CLIENT_ID,
)
from multi_source_search import (
    search_adzuna, search_adzuna_all_europe, search_remotive,
    search_arbeitnow, search_remoteok, search_jobicy,
    search_hackernews, search_weworkremotely, search_graphql_jobs,
    search_workingnomads, filter_by_experience, filter_by_work_mode,
    filter_by_date, deduplicate_jobs, calculate_match_score, EUROPEAN_COUNTRIES,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="JobRadar API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)


# ── Pydantic schemas ────────────────────────────────────────────────────────

class SearchParams(BaseModel):
    job_title: str = ""
    country: str = ""
    location: str = ""
    days_old: int = 7
    experience_level: str = ""
    work_mode: str = ""
    max_results_per_source: int = 20


class ApplicationCreate(BaseModel):
    job_title:  str
    company:    str = ""
    url:        str
    location:   str = ""
    source:     str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    notes:      str = ""
    status:     str = "applied"


class ApplicationUpdate(BaseModel):
    status: Optional[str] = None
    notes:  Optional[str] = None


class ATSRequest(BaseModel):
    job_title:       str
    company:         str = ""
    job_description: str


# ── Resume helpers ───────────────────────────────────────────────────────────

def _extract_text(file_bytes: bytes, filename: str) -> str:
    if filename.lower().endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    else:
        doc = DocxDocument(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs)


def _upload_to_r2(file_bytes: bytes, key: str, content_type: str) -> bool:
    account_id = os.getenv("R2_ACCOUNT_ID", "").strip()
    access_key = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    bucket     = os.getenv("R2_BUCKET_NAME", "jobradar-resumes").strip()
    if not all([account_id, access_key, secret_key]):
        return False
    try:
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(signature_version="s3v4"),
            region_name="auto",
        )
        client.put_object(Bucket=bucket, Key=key, Body=file_bytes, ContentType=content_type)
        return True
    except Exception:
        return False


# ── Static files & frontend ─────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def serve_frontend():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# ── Google OAuth ─────────────────────────────────────────────────────────────

@app.get("/auth/debug-oauth")
def debug_oauth():
    return {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI"),
        "oauth_url": google_auth_url(),
    }

@app.get("/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google OAuth not configured — add GOOGLE_CLIENT_ID to .env")
    return RedirectResponse(google_auth_url())


@app.get("/auth/google/callback")
async def google_callback(code: str = None, error: str = None, db: Session = Depends(get_db)):
    if error or not code:
        return RedirectResponse("/?auth_error=access_denied")

    try:
        guser = await exchange_code_for_user(code)
    except Exception:
        return RedirectResponse("/?auth_error=google_failed")

    google_id = guser.get("id")
    email     = guser.get("email", "")
    name      = guser.get("name", "")
    picture   = guser.get("picture", "")

    user = db.query(User).filter(User.google_id == google_id).first()
    if not user:
        user = User(google_id=google_id, email=email, name=name, picture=picture)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.name    = name
        user.picture = picture
        db.commit()

    token = create_token(user.id, user.email)
    return RedirectResponse(f"/?token={token}")


@app.get("/auth/me")
def get_me(current_user: dict = Depends(require_user), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == current_user["user_id"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id":      user.id,
        "email":   user.email,
        "name":    user.name,
        "picture": user.picture,
    }


# ── Job search (public) ──────────────────────────────────────────────────────

@app.post("/api/search")
async def search_jobs(params: SearchParams):
    try:
        all_jobs = []
        mode = (params.work_mode or '').lower()

        # Adzuna: location-based, country-specific — always run except for remote-only
        if mode in ('', 'onsite', 'hybrid'):
            if params.country:
                all_jobs.extend(search_adzuna(
                    params.job_title, params.country, params.location,
                    params.days_old, params.max_results_per_source,
                ))
            else:
                all_jobs.extend(search_adzuna_all_europe(
                    params.job_title, params.location,
                    params.days_old, params.max_results_per_source,
                ))

        # Remote-first sources — skip when onsite is selected
        if mode in ('', 'remote', 'hybrid'):
            all_jobs.extend(search_remotive(params.job_title, params.max_results_per_source, params.days_old))
            all_jobs.extend(search_remoteok(params.job_title, params.max_results_per_source))
            all_jobs.extend(search_jobicy(params.job_title, params.max_results_per_source))
            all_jobs.extend(search_weworkremotely(params.job_title, params.max_results_per_source))
            all_jobs.extend(search_workingnomads(params.job_title, params.max_results_per_source))

        # Arbeitnow: Europe-focused, includes on-site jobs — runs for all modes
        all_jobs.extend(search_arbeitnow(params.job_title, params.location, params.max_results_per_source))

        # Remote/global sources — skip when onsite is selected
        if mode in ('', 'remote', 'hybrid'):
            all_jobs.extend(search_hackernews(params.job_title, params.max_results_per_source))
            all_jobs.extend(search_graphql_jobs(params.job_title, params.max_results_per_source))

        # Enforce date cutoff on all sources (Adzuna filters server-side, others don't)
        all_jobs = filter_by_date(all_jobs, params.days_old)

        # Post-filter all results by title relevance (catches Adzuna loose matches too)
        if params.job_title:
            from multi_source_search import matches_query
            all_jobs = [j for j in all_jobs if matches_query(j.get('title', ''), j.get('description', ''), params.job_title)]

        if params.experience_level:
            all_jobs = filter_by_experience(all_jobs, params.experience_level)

        # For hybrid, post-filter to only jobs that mention hybrid
        if mode == 'hybrid':
            all_jobs = filter_by_work_mode(all_jobs, 'hybrid')

        total_before = len(all_jobs)
        unique_jobs  = deduplicate_jobs(all_jobs)

        for job in unique_jobs:
            job["match_score"] = calculate_match_score(job, params.job_title)
        unique_jobs.sort(key=lambda x: x["match_score"], reverse=True)

        source_counts: dict[str, int] = {}
        for job in unique_jobs:
            src = job.get("source", "Unknown")
            source_counts[src] = source_counts.get(src, 0) + 1

        return {
            "jobs":               unique_jobs,
            "total":              total_before,
            "unique":             len(unique_jobs),
            "duplicates_removed": total_before - len(unique_jobs),
            "source_counts":      source_counts,
        }

    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/countries")
def get_countries():
    return EUROPEAN_COUNTRIES


# ── Applications CRUD (auth required) ───────────────────────────────────────

@app.post("/api/applications", status_code=201)
def save_application(
    payload:      ApplicationCreate,
    current_user: dict    = Depends(require_user),
    db:           Session = Depends(get_db),
):
    existing = db.query(Application).filter(
        Application.user_id == current_user["user_id"],
        Application.url     == payload.url,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already saved")

    obj = Application(
        user_id    = current_user["user_id"],
        job_title  = payload.job_title,
        company    = payload.company,
        url        = payload.url,
        location   = payload.location,
        source     = payload.source,
        salary_min = payload.salary_min,
        salary_max = payload.salary_max,
        notes      = payload.notes,
        status     = payload.status,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _serialize(obj)


@app.get("/api/applications")
def list_applications(
    current_user: dict    = Depends(require_user),
    db:           Session = Depends(get_db),
):
    rows = (
        db.query(Application)
        .filter(Application.user_id == current_user["user_id"])
        .order_by(Application.applied_date.desc())
        .all()
    )
    return [_serialize(r) for r in rows]


@app.patch("/api/applications/{app_id}")
def update_application(
    app_id:       int,
    payload:      ApplicationUpdate,
    current_user: dict    = Depends(require_user),
    db:           Session = Depends(get_db),
):
    row = db.query(Application).filter(
        Application.id      == app_id,
        Application.user_id == current_user["user_id"],
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if payload.status is not None:
        row.status = payload.status
    if payload.notes is not None:
        row.notes = payload.notes
    db.commit()
    db.refresh(row)
    return _serialize(row)


@app.delete("/api/applications/{app_id}", status_code=204)
def delete_application(
    app_id:       int,
    current_user: dict    = Depends(require_user),
    db:           Session = Depends(get_db),
):
    row = db.query(Application).filter(
        Application.id      == app_id,
        Application.user_id == current_user["user_id"],
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(row)
    db.commit()


# ── Resume endpoints ─────────────────────────────────────────────────────────

@app.post("/api/resume", status_code=201)
async def upload_resume(
    file:         UploadFile = File(...),
    current_user: dict       = Depends(require_user),
    db:           Session    = Depends(get_db),
):
    if not file.filename.lower().endswith((".pdf", ".docx")):
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are supported")

    file_bytes = await file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large — max 5 MB")

    text = _extract_text(file_bytes, file.filename)
    if not text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from file")

    r2_key = f"resumes/{current_user['user_id']}/{file.filename}"
    _upload_to_r2(file_bytes, r2_key, file.content_type or "application/octet-stream")

    existing = db.query(Resume).filter(Resume.user_id == current_user["user_id"]).first()
    if existing:
        existing.filename       = file.filename
        existing.r2_key         = r2_key
        existing.extracted_text = text
        existing.uploaded_at    = datetime.utcnow()
    else:
        db.add(Resume(
            user_id=current_user["user_id"],
            filename=file.filename,
            r2_key=r2_key,
            extracted_text=text,
        ))
    db.commit()
    return {"filename": file.filename}


@app.get("/api/resume")
def get_resume(current_user: dict = Depends(require_user), db: Session = Depends(get_db)):
    row = db.query(Resume).filter(Resume.user_id == current_user["user_id"]).first()
    if not row:
        return None
    return {"id": row.id, "filename": row.filename, "uploaded_at": row.uploaded_at.isoformat()}


@app.delete("/api/resume", status_code=204)
def delete_resume(current_user: dict = Depends(require_user), db: Session = Depends(get_db)):
    row = db.query(Resume).filter(Resume.user_id == current_user["user_id"]).first()
    if row:
        db.delete(row)
        db.commit()


# ── ATS score ────────────────────────────────────────────────────────────────

@app.post("/api/ats-score")
async def ats_score(
    payload:      ATSRequest,
    current_user: dict    = Depends(require_user),
    db:           Session = Depends(get_db),
):
    row = db.query(Resume).filter(Resume.user_id == current_user["user_id"]).first()
    if not row:
        raise HTTPException(status_code=400, detail="No resume uploaded")

    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")

    prompt = f"""You are an expert ATS (Applicant Tracking System) analyzer.
Compare the resume against the job description and return ONLY a JSON object — no markdown, no explanation.

Job Title: {payload.job_title}
Company: {payload.company}

Job Description:
{payload.job_description[:2500]}

Resume:
{row.extracted_text[:2500]}

Return exactly this JSON (no extra text):
{{"score": 75, "matching_skills": ["Python", "FastAPI"], "missing_keywords": ["Docker", "Kubernetes"], "recommendation": "Add more cloud experience to your resume."}}"""

    try:
        groq_client = Groq(api_key=groq_key)

        # dynamically pick first available text model
        try:
            available = groq_client.models.list()
            model_ids = [m.id for m in available.data if "whisper" not in m.id.lower()]
        except Exception:
            model_ids = []

        # fallback list in case models.list() fails
        fallback_models = [
            "llama-3.1-8b-instant",
            "llama-3.3-70b-versatile",
            "llama3-8b-8192",
            "gemma2-9b-it",
            "gemma-7b-it",
            "mixtral-8x7b-32768",
        ]
        models_to_try = model_ids if model_ids else fallback_models

        resp = None
        for model in models_to_try:
            try:
                resp = groq_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=600,
                )
                break
            except Exception:
                continue

        if resp is None:
            raise HTTPException(status_code=500, detail="No Groq model available — check GROQ_API_KEY")

        raw = resp.choices[0].message.content.strip()

        # strip markdown fences
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break

        # extract JSON object with regex fallback
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            raw = match.group(0)

        result = json.loads(raw)
        result["score"] = max(0, min(100, int(result.get("score", 0))))
        return result

    except HTTPException:
        raise
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Could not parse AI response: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _serialize(row: Application) -> dict:
    return {
        "id":           row.id,
        "job_title":    row.job_title,
        "company":      row.company,
        "url":          row.url,
        "location":     row.location,
        "source":       row.source,
        "salary_min":   row.salary_min,
        "salary_max":   row.salary_max,
        "status":       row.status,
        "notes":        row.notes,
        "applied_date": row.applied_date.isoformat() if row.applied_date else None,
    }
