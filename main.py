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

import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

app = FastAPI(title="Course Registration API", version="1.0.0")

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
async def import_history(student_id: str, file: UploadFile = File(...)):
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


@app.get("/api/v1/students/{student_id}/profile")
def get_profile(student_id: str):
    student = require_student(student_id)
    return {"student_id": student_id, "history": student["history"], "plan": student["plan"]}
