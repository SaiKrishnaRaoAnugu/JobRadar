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
import re
import requests as http_requests
from bs4 import BeautifulSoup
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
    filter_by_date, filter_by_country, deduplicate_jobs, calculate_match_score, EUROPEAN_COUNTRIES,
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


class FetchJDRequest(BaseModel):
    url:    str
    source: str = ""


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

        # Enforce date cutoff on all sources
        all_jobs = filter_by_date(all_jobs, params.days_old)

        # Country filter — applied to ALL sources after collection
        # Adzuna is already country-filtered server-side; this catches Arbeitnow + others
        if params.country:
            all_jobs = filter_by_country(all_jobs, params.country)

        # Post-filter by title relevance
        if params.job_title:
            from multi_source_search import matches_query
            all_jobs = [j for j in all_jobs if matches_query(j.get('title', ''), j.get('description', ''), params.job_title)]

        # Experience level filter
        if params.experience_level:
            all_jobs = filter_by_experience(all_jobs, params.experience_level)

        # Work mode post-filter (hybrid needs keyword check)
        if mode == 'hybrid':
            all_jobs = filter_by_work_mode(all_jobs, 'hybrid')
        elif mode == 'onsite':
            all_jobs = filter_by_work_mode(all_jobs, 'onsite')

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


# ── Fetch full JD ────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# CSS selectors for the main JD container (tried in order, most specific first)
JD_SELECTORS = [
    "[class*='job-description']",
    "[class*='jobDescription']",
    "[class*='job_description']",
    "[id*='job-description']",
    "[id*='jobDescription']",
    "[class*='job-detail']",
    "[class*='jobDetail']",
    "[class*='vacancy-description']",
    "[class*='listing-description']",
    "[class*='job-content']",
    "[class*='jobContent']",
    "[class*='offer-description']",
    "[class*='advert-detail']",
    "article",
    "[role='main']",
    "main",
]

# Only remove elements whose class/id is clearly UI chrome — NOT content areas
_NOISE_CLASSES = [
    'cookie', 'gdpr', 'consent', 'newsletter', 'email-alert',
    'job-alert', 'subscribe', 'sign-up', 'signup',
    'recommendation', 'similar-jobs', 'related-jobs',
    'popup', 'modal', 'overlay', 'breadcrumb',
]

# Text patterns that mark the END of actual JD — stop collecting here
_CUTOFF_LINES = [
    'ähnliche jobs', 'similar jobs', 'you might also like',
    'related jobs', 'more jobs like this', 'other opportunities',
    'job-e-mail', 'jetzt ähnliche', 'nein, danke',
    'mit dem klick auf', 'datenschutzbestimmungen',
    'häufige suchvorgänge', 'zurück zur letzten suche',
    'cookies zustimmen', 'impressum', 'privacy policy',
    'get email alerts', 'sign up for job alerts',
    'create a job alert', 'set up an alert',
    'subscribe to', 'agbs', 'terms of service',
    'jetzt ähnliche jobs', 'job-e-mail bestellen',
]

# Exact noisy lines to skip (navigation, buttons, etc.)
_NOISE_LINES = [
    'schnellbewerbung', 'auf diesen job bewerben', 'jetzt bewerben',
    'apply now', 'apply for this job', 'quick apply',
    'back to search', 'back to results', '❮', '❯', 'neu', 'new',
    'save job', 'share job', 'print job',
]


def _clean_jd_text(raw: str) -> str:
    """Truncate at end-of-JD markers and remove obvious UI-only lines."""
    lines = raw.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()

        # Stop at section-end markers
        if any(cutoff in low for cutoff in _CUTOFF_LINES):
            break

        # Skip exact UI-noise lines
        if any(low == noise or low.startswith(noise) for noise in _NOISE_LINES):
            continue

        # Skip navigation arrows / single characters
        if len(stripped) <= 2:
            continue

        cleaned.append(stripped)

    return "\n".join(cleaned)


def _scrape_jd(url: str) -> str:
    try:
        resp = http_requests.get(url, headers=HEADERS, timeout=15,
                                 allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Remove tags that never contain JD content
        for tag in soup(["script", "style", "noscript", "iframe",
                         "svg", "img", "video", "audio"]):
            tag.decompose()

        # Remove only clearly-UI chrome elements (conservative list)
        for el in soup.find_all(True):
            cls = " ".join(el.get("class", [])).lower()
            eid = (el.get("id") or "").lower()
            if any(n in cls or n in eid for n in _NOISE_CLASSES):
                el.decompose()

        # Strategy 1: targeted CSS selectors
        for selector in JD_SELECTORS:
            el = soup.select_one(selector)
            if el:
                raw = el.get_text(separator="\n", strip=True)
                text = _clean_jd_text(raw)
                if len(text) > 300:
                    return text

        # Strategy 2: largest text block (find the div with most paragraph text)
        best, best_len = "", 0
        for div in soup.find_all(["div", "section"]):
            t = div.get_text(separator="\n", strip=True)
            if len(t) > best_len and len(t) < 50000:
                best, best_len = t, len(t)
        if best_len > 500:
            text = _clean_jd_text(best)
            if len(text) > 300:
                return text

        # Strategy 3: all paragraphs + list items
        parts = [tag.get_text(strip=True)
                 for tag in soup.find_all(["p", "li", "h1", "h2", "h3"])
                 if len(tag.get_text(strip=True)) > 15]
        if parts:
            text = _clean_jd_text("\n".join(parts))
            if len(text) > 300:
                return text

        return ""
    except Exception:
        return ""


@app.post("/api/fetch-jd")
async def fetch_jd(payload: FetchJDRequest):
    text = _scrape_jd(payload.url)
    if not text:
        raise HTTPException(status_code=422, detail="Could not extract full description from this job board.")
    return {"description": text}


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

    resume_text = row.extracted_text[:3000]
    jd_text     = payload.job_description[:3000]

    prompt = f"""You are a precise ATS (Applicant Tracking System) analyzer.

Your task: compare the RESUME against the JOB DESCRIPTION and return a JSON ATS score.

STRICT RULES:
1. Only list a skill in "matching_skills" if it clearly appears in BOTH the resume AND the job description.
2. Only list a keyword in "missing_keywords" if it appears in the job description but is genuinely ABSENT from the resume. Search carefully — check for abbreviations and variations (e.g. "K8s" = "Kubernetes", "JS" = "JavaScript").
3. Do NOT hallucinate. Do NOT use example data. Base everything strictly on the texts provided.
4. Score = percentage of JD requirements covered by the resume (0-100).

JOB TITLE: {payload.job_title}
COMPANY: {payload.company}

--- JOB DESCRIPTION ---
{jd_text}

--- RESUME ---
{resume_text}

Return ONLY this JSON (no markdown, no explanation, no extra text):
{{"score": <integer 0-100>, "matching_skills": [<skills found in BOTH texts>], "missing_keywords": [<important JD keywords NOT found in resume>], "recommendation": "<one specific sentence to improve the resume>"}}"""

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
