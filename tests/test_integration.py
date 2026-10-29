"""Integration tests: run against a live server (CI boots uvicorn first).
Usage: BASE_URL=http://127.0.0.1:8000 pytest tests/test_integration.py
"""
import os
from pathlib import Path

import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL")
HERE = Path(__file__).parent
pytestmark = pytest.mark.skipif(not BASE_URL, reason="BASE_URL not set (no live server)")


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        yield c


def test_health(api):
    assert api.get("/").status_code == 200


def test_full_audit_flow(api):
    catalog = (HERE / "sample_catalog.html").read_bytes()
    r = api.post("/api/v1/admin/catalog/import", files={"file": ("c.html", catalog, "text/html")})
    assert r.status_code == 200

    transcript = (HERE / "student-example.html").read_bytes()
    r = api.post("/api/v1/students/ci1/history/import", files={"file": ("t.html", transcript, "text/html")})
    assert r.status_code == 201 and r.json()["past_courses_imported"] == 33

    # ITEC-3506 is cross-listed with COSC-3506, which this student completed in 26W
    plan = {"planned_courses": [{"course_code": "ITEC-3506", "term": "26F"},
                                {"course_code": "COSC-2007", "term": "23F"}]}
    assert api.post("/api/v1/students/ci1/plan", json=plan).status_code == 200

    report = api.get("/api/v1/students/ci1/audit-report", params={"strict": "true"}).json()
    assert report["status"] == "failed"
    assert report["cross_list_violations"][0]["type"] == "CROSS_LIST_CONFLICT"
    # COSC-2006 was completed in 24W, which is not before 23F
    assert report["timeline_validation"][0]["term"] == "23F"
    assert report["timeline_validation"][0]["errors"][0]["type"] == "MISSING_PREREQUISITE"
    assert report["credit_summary"]["total_planned"] == 6


def test_unknown_student(api):
    assert api.get("/api/v1/students/nobody/audit-report").status_code == 404
