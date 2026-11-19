"""
Course Registration API - Phase 1: Catalog Ingestion

Endpoints
  POST /api/v1/admin/catalog/import        multipart/form-data upload of an HTML catalog
  GET  /api/v1/catalog/courses/{code}      one course as structured JSON
  GET  /api/v1/catalog/courses             all courses (handy for debugging)
  GET  /                                   health check (use this to wake Render up)

The parser reads the STRUCTURE of the HTML (table header -> column index),
never specific course codes or titles.
"""

import os
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

import bcrypt
import jwt
from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

app = FastAPI(title="Course Registration API", version="1.0.0")

# ---------------------------------------------------------------------------
# Phase 4: identity (JWT + bcrypt), BOLA / RBAC helpers, rate limiting
# ---------------------------------------------------------------------------

# Set JWT_SECRET in the environment for stable tokens across restarts.
JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 3600
BCRYPT_ROUNDS = 10  # keeps hashing fast enough on a 0.1-CPU free instance

# username -> {"password_hash": str, "role": "student" | "admin"}; never plain-text passwords
USERS: Dict[str, dict] = {}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def seed_admin() -> None:
    USERS["admin"] = {"password_hash": hash_password("admin"), "role": "admin"}


seed_admin()

bearer_scheme = HTTPBearer(auto_error=False)


def unauthorized(detail: str = "Unauthorized") -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def decode_token(token: str) -> dict:
    """Verify signature + expiry; raise 401 on any problem."""
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
                            options={"require": ["sub", "exp"]})
    except jwt.PyJWTError:
        raise unauthorized("Invalid or expired token")
    if claims["sub"] not in USERS:
        raise unauthorized("Unknown user")
    return claims


def current_claims(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)) -> dict:
    if creds is None:
        raise unauthorized("Missing bearer token")
    return decode_token(creds.credentials)


def require_owner(student_id: str, claims: dict) -> None:
    """BOLA lock: the token's subject must be exactly this student."""
    if claims["sub"] != student_id:
        raise unauthorized("Token does not belong to this student")


def require_owner_or_admin(student_id: str, claims: dict) -> None:
    """RBAC: the owner, or any token with role=admin."""
    if claims["sub"] != student_id and claims.get("role") != "admin":
        raise unauthorized("Not allowed to access this student")


RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60.0
_rate_hits: Dict[str, Deque[float]] = defaultdict(deque)


def rate_limit(request: Request,
               creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)) -> None:
    """Sliding window: at most RATE_LIMIT requests per key in any RATE_WINDOW_SECONDS."""
    key = None
    if creds is not None:
        try:
            key = "sub:" + decode_token(creds.credentials)["sub"]
        except HTTPException:
            key = None
    if key is None:
        key = "ip:" + (request.client.host if request.client else "unknown")

    now = time.monotonic()
    hits = _rate_hits[key]
    while hits and now - hits[0] >= RATE_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= RATE_LIMIT:
        retry_after = int(RATE_WINDOW_SECONDS - (now - hits[0])) + 1
        raise HTTPException(status_code=429, detail="Too Many Requests",
                            headers={"Retry-After": str(retry_after)})
    hits.append(now)


class Credentials(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


@app.post("/api/v1/auth/register", status_code=201)
def register(body: Credentials):
    if body.username in USERS:
        raise HTTPException(status_code=409, detail="Username already exists")
    USERS[body.username] = {"password_hash": hash_password(body.password), "role": "student"}
    return {"status": "registered"}


@app.post("/api/v1/auth/login")
def login(body: Credentials):
    user = USERS.get(body.username)
    if user is None or not bcrypt.checkpw(body.password.encode("utf-8"),
                                          user["password_hash"].encode("utf-8")):
        raise unauthorized("Invalid username or password")
    now = int(time.time())
    claims = {"sub": body.username, "role": user["role"], "iat": now, "exp": now + TOKEN_TTL_SECONDS}
    return {"access_token": jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGORITHM),
            "token_type": "bearer"}


# In-memory "database": normalized course code -> course dict
CATALOG: Dict[str, dict] = {}

# A course code = 2-5 letters, optional space/dash, 3-4 digits, optional letter.
# Matches "COSC 3506", "COSC3506", "cs-101", "MATH 1036A", etc.
COURSE_CODE_RE = re.compile(r"\b([A-Za-z]{2,5})[\s\-]*(\d{3,4}[A-Za-z]?)\b")

# Header keywords -> our field names (order independent, case insensitive)
HEADER_MAP = {
    "code": "course_code",
    "title": "title",
    "name": "title",
    "credit": "credits",
    "prereq": "prerequisites",
    "cross": "cross_listed",
}


def normalize_code(raw: str) -> str:
    """'cosc 3506' / 'COSC-3506' / 'COSC3506' -> 'COSC3506'."""
    return re.sub(r"[\s\-]+", "", raw or "").upper()


def extract_codes(text: str) -> List[str]:
    """Pull every course code out of free text; 'None'/blank -> []."""
    if not text:
        return []
    codes: List[str] = []
    for subject, number in COURSE_CODE_RE.findall(text):
        code = f"{subject}{number}".upper()
        if code not in codes:  # keep order, drop duplicates
            codes.append(code)
    return codes


def parse_credits(text: str):
    """'3' -> 3, '1.5' -> 1.5, anything unparseable -> None."""
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    if not match:
        return None
    value = float(match.group())
    return int(value) if value.is_integer() else value


def clean(text: str) -> str:
    """Collapse whitespace / non-breaking spaces."""
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def map_header(cells: List[str]) -> Optional[Dict[str, int]]:
    """Return {field_name: column_index} if this row looks like a header row."""
    mapping: Dict[str, int] = {}
    for idx, cell in enumerate(cells):
        lower = cell.lower()
        for keyword, field in HEADER_MAP.items():
            if keyword in lower and field not in mapping:
                mapping[field] = idx
                break
    return mapping if "course_code" in mapping else None


def parse_catalog(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    courses: List[dict] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        columns: Optional[Dict[str, int]] = None

        for row in rows:
            cells = [clean(c.get_text(" ")) for c in row.find_all(["td", "th"])]
            if not cells:
                continue

            # First row that names a "Course Code" column is the header
            if columns is None:
                columns = map_header(cells)
                if columns is None:
                    # No header found: fall back to the standard column order
                    columns = {
                        "course_code": 0, "title": 1, "credits": 2,
                        "prerequisites": 3, "cross_listed": 4,
                    }
                else:
                    continue  # header row consumed

            def cell(field: str) -> str:
                i = columns.get(field)
                return cells[i] if i is not None and i < len(cells) else ""

            code = normalize_code(cell("course_code"))
            if not COURSE_CODE_RE.fullmatch(code):
                continue  # skip blank/junk rows

            courses.append({
                "course_code": code,
                "title": cell("title"),
                "credits": parse_credits(cell("credits")),
                "prerequisites": extract_codes(cell("prerequisites")),
                "cross_listed": [c for c in extract_codes(cell("cross_listed")) if c != code],
            })

    return courses


@app.get("/")
def health():
    return {"status": "ok", "courses_loaded": len(CATALOG)}


@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    html = raw.decode("utf-8", errors="replace")
    courses = parse_catalog(html)
    if not courses:
        raise HTTPException(status_code=400, detail="No courses found in the uploaded catalog")

    # A new import replaces the old catalog
    CATALOG.clear()
    for course in courses:
        CATALOG[course["course_code"]] = course

    return {
        "message": "Catalog imported successfully",
        "courses_imported": len(CATALOG),
        "course_codes": list(CATALOG.keys()),
    }


@app.get("/api/v1/catalog/courses")
def list_courses():
    return list(CATALOG.values())


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    course = CATALOG.get(normalize_code(course_code))
    if course is None:
        raise HTTPException(status_code=404, detail=f"Course '{course_code}' not found")
    return course


# ---------------------------------------------------------------------------
# Phase 2: student academic history + plan
# ---------------------------------------------------------------------------

# Per-student state: student_id -> {"history": [...], "plan": [...]}
STUDENTS: Dict[str, dict] = {}

PAST_STATUSES = {"Completed", "In-Progress", "Attempted"}


class HistoryEntry(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryBody(BaseModel):
    history: List[HistoryEntry]


class PlanEntry(BaseModel):
    course_code: str
    term: str


class PlanBody(BaseModel):
    planned_courses: List[PlanEntry]


def grade_rank(grade: str) -> tuple:
    """Numeric grade (higher wins) > letter grade A-F(+/-) > P/blank/other."""
    g = (grade or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", g):
        return (2, float(g))
    if re.fullmatch(r"[A-EF][+-]?", g, re.I):
        return (1, 0.0)
    return (0, 0.0)


def parse_int_credits(text: str) -> int:
    """Credits cell as an integer; blank or non-integer text -> 0."""
    try:
        return int(text)
    except ValueError:
        return 0


def parse_transcript(html: str) -> List[dict]:
    """Canonical rule: tables whose header row is
    Status | Course | (title) | Grade | Term | Credits."""
    soup = BeautifulSoup(html, "html.parser")
    best: Dict[tuple, tuple] = {}  # (course_code, term) -> (score, record)

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not ("Status" in headers and "Course" in headers and "Credits" in headers):
            continue
        for row in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
            if len(cells) < 6 or not cells[1]:
                continue
            status, course, grade, term, credits_text = cells[0], cells[1], cells[3], cells[4], cells[5]
            if status not in PAST_STATUSES or not term:
                continue  # drops Fulfilled / Planned placeholder rows

            credits = parse_int_credits(credits_text)
            record = {"course_code": course, "term": term,
                      "credits_earned": credits, "status": status}
            score = (grade_rank(grade), credits)
            key = (course, term)
            if key not in best or score > best[key][0]:
                best[key] = (score, record)

    return [record for _, record in best.values()]


def require_student(student_id: str) -> dict:
    student = STUDENTS.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found")
    return student


@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(student_id: str, file: UploadFile = File(...),
                         claims: dict = Depends(current_claims)):
    require_owner(student_id, claims)
    raw = await file.read()
    history = parse_transcript(raw.decode("utf-8", errors="replace"))
    student = STUDENTS.setdefault(student_id, {"history": [], "plan": []})
    student["history"] = history
    return {"status": "success", "past_courses_imported": len(history)}


@app.put("/api/v1/students/{student_id}/history")
def replace_history(student_id: str, body: HistoryBody):
    student = require_student(student_id)
    student["history"] = [e.model_dump() for e in body.history]
    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history")
def clear_history(student_id: str):
    require_student(student_id)["history"] = []
    return {"status": "success", "message": "Academic history cleared"}


@app.post("/api/v1/students/{student_id}/plan")
def save_plan(student_id: str, body: PlanBody):
    student = require_student(student_id)
    student["plan"] = [e.model_dump() for e in body.planned_courses]
    return {"status": "success", "planned_courses_saved": len(student["plan"])}


@app.put("/api/v1/students/{student_id}/plan")
def replace_plan(student_id: str, body: PlanBody):
    student = require_student(student_id)
    student["plan"] = [e.model_dump() for e in body.planned_courses]
    return {"status": "success", "planned_courses_saved": len(student["plan"])}


@app.delete("/api/v1/students/{student_id}/plan")
def clear_plan(student_id: str):
    require_student(student_id)["plan"] = []
    return {"status": "success", "message": "Plan cleared"}


@app.get("/api/v1/students/{student_id}/plan")
def get_plan(student_id: str, claims: dict = Depends(current_claims)):
    require_owner_or_admin(student_id, claims)
    student = require_student(student_id)
    return {"student_id": student_id, "plan": student["plan"]}


@app.get("/api/v1/students/{student_id}/profile")
def get_profile(student_id: str, claims: dict = Depends(current_claims)):
    require_owner_or_admin(student_id, claims)
    student = require_student(student_id)
    return {"student_id": student_id, "history": student["history"], "plan": student["plan"]}


# ---------------------------------------------------------------------------
# Phase 3: degree audit engine
# ---------------------------------------------------------------------------

GRADUATION_CREDITS = 120
SEASON_ORDER = {"W": 0, "SP": 1, "S": 2, "F": 3}
TERM_RE = re.compile(r"^(\d{2})(SP|W|S|F)$")


def term_key(term: str) -> tuple:
    """'24W' -> (24, 0); '26SP' -> (26, 1). Unknown formats sort last."""
    m = TERM_RE.match((term or "").strip().upper())
    if not m:
        return (999, 9)
    return (int(m.group(1)), SEASON_ORDER[m.group(2)])


def display_code(code: str) -> str:
    """'COSC3127' -> 'COSC-3127' for readable messages."""
    m = re.match(r"^([A-Z]+)(\d.*)$", code)
    return f"{m.group(1)}-{m.group(2)}" if m else code


def build_audit(student: dict) -> dict:
    # Normalized code -> terms in which it was Completed
    completed: Dict[str, List[str]] = {}
    earned_by_course: Dict[str, int] = {}
    for entry in student["history"]:
        if entry["status"] != "Completed":
            continue  # non-completed attempts contribute 0
        code = normalize_code(entry["course_code"])
        completed.setdefault(code, []).append(entry["term"])
        # Retakes: count each completed course once
        earned_by_course[code] = max(earned_by_course.get(code, 0), entry["credits_earned"])

    errors_by_term: Dict[str, List[dict]] = {}
    cross_list_violations: List[dict] = []
    total_planned = 0

    for planned in student["plan"]:
        code = normalize_code(planned["course_code"])
        term = planned["term"]
        course = CATALOG.get(code) or {}
        total_planned += course.get("credits") or 0

        for prereq in course.get("prerequisites", []):
            if not any(term_key(t) < term_key(term) for t in completed.get(prereq, [])):
                errors_by_term.setdefault(term, []).append({
                    "course_code": planned["course_code"],
                    "type": "MISSING_PREREQUISITE",
                    "message": f"Missing prerequisite: {display_code(prereq)}",
                })

        # Cross-listing may be declared on either course in the catalog
        partners = set(course.get("cross_listed", []))
        partners |= {c for c, info in CATALOG.items() if code in info.get("cross_listed", [])}
        for partner in sorted(partners - {code}):
            if partner in completed:
                cross_list_violations.append({
                    "course_code": planned["course_code"],
                    "type": "CROSS_LIST_CONFLICT",
                    "message": f"Cross-listed with completed course {display_code(partner)}",
                })

    total_earned = sum(earned_by_course.values())
    return {
        "timeline_validation": [
            {"term": t, "errors": errors_by_term[t]} for t in sorted(errors_by_term, key=term_key)
        ],
        "cross_list_violations": cross_list_violations,
        "credit_summary": {
            "total_earned": total_earned,
            "total_planned": total_planned,
            "total_remaining_for_graduation": max(0, GRADUATION_CREDITS - total_earned - total_planned),
        },
    }


@app.get("/api/v1/students/{student_id}/audit-report", dependencies=[Depends(rate_limit)])
def audit_report(student_id: str, strict: bool = False):
    audit = build_audit(require_student(student_id))
    has_issues = bool(audit["timeline_validation"] or audit["cross_list_violations"])
    status = ("failed" if strict else "warning") if has_issues else "ok"
    return {"student_id": student_id, "status": status, **audit}


# ---------------------------------------------------------------------------
# Phase 4: recommendations (Kahn's topological sort, level by level)
# ---------------------------------------------------------------------------

DEFAULT_START_TERM = "26F"


def next_term(term: str) -> str:
    """Fall -> next year's Winter; any other season -> same year's Fall."""
    year, season = term_key(term)
    return f"{year + 1:02d}W" if season == SEASON_ORDER["F"] else f"{year:02d}F"


def course_label(code: str) -> str:
    """'COSC2006' -> 'COSC 2006' (catalog style)."""
    m = re.match(r"^([A-Z]+)(\d.*)$", code)
    return f"{m.group(1)} {m.group(2)}" if m else code


def build_pathway(student: dict) -> List[dict]:
    completed = {normalize_code(e["course_code"]) for e in student["history"] if e["status"] == "Completed"}
    remaining = set(CATALOG) - completed

    # DAG over remaining courses; a prerequisite that is neither completed nor
    # offered keeps its dependents blocked forever (indegree never reaches 0).
    indegree: Dict[str, int] = {}
    dependents: Dict[str, List[str]] = defaultdict(list)
    for code in remaining:
        prereqs = set(CATALOG[code].get("prerequisites", [])) - completed - {code}
        indegree[code] = len(prereqs)
        for p in prereqs:
            dependents[p].append(code)

    known_terms = [e["term"] for e in student["history"] if term_key(e["term"])[0] != 999]
    term = next_term(max(known_terms, key=term_key)) if known_terms else DEFAULT_START_TERM

    pathway: List[dict] = []
    frontier = sorted(c for c, d in indegree.items() if d == 0)
    while frontier:
        pathway.append({"term": term, "courses": [course_label(c) for c in frontier]})
        unlocked = []
        for code in frontier:
            for dep in dependents[code]:
                indegree[dep] -= 1
                if indegree[dep] == 0:
                    unlocked.append(dep)
        frontier = sorted(unlocked)
        term = next_term(term)
    return pathway


@app.get("/api/v1/students/{student_id}/recommendations")
def recommendations(student_id: str, claims: dict = Depends(current_claims)):
    require_owner_or_admin(student_id, claims)
    student = require_student(student_id)
    return {"student_id": student_id, "recommended_pathway": build_pathway(student)}
