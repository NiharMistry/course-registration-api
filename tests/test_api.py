"""Unit tests for the Course Registration API (Phases 1-3)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main

HERE = Path(__file__).parent
client = TestClient(main.app)

CATALOG_HTML = """
<table>
<tr><th>Course Code</th><th>Title</th><th>Credits</th><th>Prerequisites</th><th>Cross-listed</th></tr>
<tr><td>COSC 1000</td><td>Intro</td><td>3</td><td>None</td><td></td></tr>
<tr><td>COSC 2000</td><td>Middle</td><td>3</td><td>COSC 1000</td><td></td></tr>
<tr><td>COSC 3000</td><td>Upper</td><td>6</td><td>COSC 2000 and MATH 1000</td><td>ITEC 3000</td></tr>
<tr><td>ITEC 3000</td><td>Upper (ITEC)</td><td>6</td><td>COSC 2000</td><td></td></tr>
<tr><td>MATH 1000</td><td>Calc</td><td>3</td><td>None</td><td></td></tr>
</table>
"""


def transcript(rows):
    body = "".join(
        f"<tr><td>{s}</td><td>{c}</td><td>t</td><td>{g}</td><td>{t}</td><td>{cr}</td></tr>"
        for s, c, g, t, cr in rows
    )
    return ("<table><tr><th>Status</th><th>Course</th><th></th><th>Grade</th>"
            f"<th>Term</th><th>Credits</th></tr>{body}</table>")


@pytest.fixture(autouse=True)
def fresh_state():
    main.CATALOG.clear()
    main.STUDENTS.clear()
    r = client.post("/api/v1/admin/catalog/import",
                    files={"file": ("c.html", CATALOG_HTML, "text/html")})
    assert r.status_code == 200


def setup_student(sid, rows, plan):
    r = client.post(f"/api/v1/students/{sid}/history/import",
                    files={"file": ("t.html", transcript(rows), "text/html")})
    assert r.status_code == 201
    r = client.post(f"/api/v1/students/{sid}/plan", json={"planned_courses": plan})
    assert r.status_code == 200


def audit(sid, strict=None):
    url = f"/api/v1/students/{sid}/audit-report"
    if strict is not None:
        url += f"?strict={'true' if strict else 'false'}"
    r = client.get(url)
    assert r.status_code == 200
    return r.json()


# ---------------- Phase 1 ----------------

def test_health():
    assert client.get("/").json()["status"] == "ok"


def test_catalog_lookup_is_format_insensitive():
    course = client.get("/api/v1/catalog/courses/cosc-3000").json()
    assert course["prerequisites"] == ["COSC2000", "MATH1000"]
    assert course["cross_listed"] == ["ITEC3000"]
    assert course["credits"] == 6
    assert len(client.get("/api/v1/catalog/courses").json()) == 5


def test_catalog_errors():
    assert client.get("/api/v1/catalog/courses/NOPE9999").status_code == 404
    empty = client.post("/api/v1/admin/catalog/import", files={"file": ("e.html", b"", "text/html")})
    assert empty.status_code == 400
    junk = client.post("/api/v1/admin/catalog/import", files={"file": ("j.html", b"<p>hi</p>", "text/html")})
    assert junk.status_code == 400


def test_sample_catalog_file():
    html = (HERE / "sample_catalog.html").read_bytes()
    r = client.post("/api/v1/admin/catalog/import", files={"file": ("s.html", html, "text/html")})
    assert r.json()["courses_imported"] == 5
    assert client.get("/api/v1/catalog/courses/COSC3506").json()["prerequisites"] == ["COSC2007"]


def test_catalog_without_header_row_uses_default_columns():
    html = "<table><tr><td>BIOL 1000</td><td>Bio</td><td>1.5</td><td>None</td><td></td></tr></table>"
    client.post("/api/v1/admin/catalog/import", files={"file": ("n.html", html, "text/html")})
    assert client.get("/api/v1/catalog/courses/BIOL1000").json()["credits"] == 1.5


# ---------------- Phase 2 ----------------

def test_student_example_parsing_and_lifecycle():
    html = (HERE / "student-example.html").read_bytes()
    r = client.post("/api/v1/students/s1/history/import", files={"file": ("x.html", html, "text/html")})
    assert r.status_code == 201 and r.json()["past_courses_imported"] == 33

    new_hist = {"history": [{"course_code": "COSC-1000", "term": "23F",
                             "credits_earned": 3, "status": "Completed"}]}
    assert client.put("/api/v1/students/s1/history", json=new_hist).status_code == 200
    assert len(client.get("/api/v1/students/s1/profile").json()["history"]) == 1
    assert client.delete("/api/v1/students/s1/history").status_code == 200

    plan = {"planned_courses": [{"course_code": "COSC-3000", "term": "26F"}]}
    assert client.put("/api/v1/students/s1/plan", json=plan).json()["planned_courses_saved"] == 1
    assert client.delete("/api/v1/students/s1/plan").status_code == 200
    profile = client.get("/api/v1/students/s1/profile").json()
    assert set(profile) == {"student_id", "history", "plan"}
    assert profile["history"] == [] and profile["plan"] == []


def test_transcript_dedup_prefers_numeric_grade():
    rows = [("Completed", "COSC-1000", "B", "23F", "3"),
            ("Completed", "COSC-1000", "85", "23F", "3"),
            ("Planned", "COSC-2000", "", "", "3"),
            ("Attempted", "MATH-1000", "F", "24W", "")]
    hist = main.parse_transcript(transcript(rows))
    assert len(hist) == 2
    assert {"course_code": "MATH-1000", "term": "24W", "credits_earned": 0, "status": "Attempted"} in hist


def test_unknown_student_404s():
    for method, path in [("get", "profile"), ("get", "audit-report"), ("delete", "history"),
                         ("delete", "plan")]:
        assert getattr(client, method)(f"/api/v1/students/ghost/{path}").status_code == 404
    assert client.post("/api/v1/students/ghost/plan", json={"planned_courses": []}).status_code == 404


# ---------------- Phase 3 ----------------

def test_term_ordering():
    terms = ["26F", "24W", "26SP", "23F", "24F", "26S", "26W"]
    assert sorted(terms, key=main.term_key) == ["23F", "24W", "24F", "26W", "26SP", "26S", "26F"]
    assert main.term_key("bogus") > main.term_key("99F")


def test_clean_student_is_ok():
    setup_student("ok1", [("Completed", "COSC 1000", "A", "24F", "3")],
                  [{"course_code": "cosc2000", "term": "25W"}])
    rep = audit("ok1", strict=True)
    assert rep["status"] == "ok"
    assert rep["timeline_validation"] == [] and rep["cross_list_violations"] == []
    assert set(rep) == {"student_id", "status", "timeline_validation",
                        "cross_list_violations", "credit_summary"}


def test_missing_prerequisite_grouped_by_term_chronologically():
    setup_student("p1", [("Completed", "COSC-1000", "A", "24F", "3")],
                  [{"course_code": "COSC-3000", "term": "26F"},
                   {"course_code": "COSC-2000", "term": "24F"}])  # same term as prereq -> not prior
    rep = audit("p1")
    assert rep["status"] == "warning"
    assert [g["term"] for g in rep["timeline_validation"]] == ["24F", "26F"]
    errs_26f = rep["timeline_validation"][1]["errors"]
    expected = {"Missing prerequisite: COSC-2000", "Missing prerequisite: MATH-1000"}
    assert {e["message"] for e in errs_26f} == expected
    assert all(e["type"] == "MISSING_PREREQUISITE" and e["course_code"] == "COSC-3000"
               for e in errs_26f)
    assert audit("p1", strict=True)["status"] == "failed"


def test_in_progress_prerequisite_does_not_count():
    setup_student("p2", [("In-Progress", "COSC-1000", "", "24F", "3")],
                  [{"course_code": "COSC-2000", "term": "25W"}])
    assert audit("p2")["timeline_validation"][0]["errors"][0]["message"] == \
        "Missing prerequisite: COSC-1000"


def test_cross_list_both_directions():
    rows = [("Completed", "COSC-1000", "A", "23F", "3"), ("Completed", "COSC-2000", "A", "24W", "3"),
            ("Completed", "MATH-1000", "A", "24W", "3"), ("Completed", "COSC-3000", "A", "25W", "6")]
    setup_student("x1", rows, [{"course_code": "ITEC 3000", "term": "26F"}])
    viol = audit("x1")["cross_list_violations"]
    assert viol == [{"course_code": "ITEC 3000", "type": "CROSS_LIST_CONFLICT",
                     "message": "Cross-listed with completed course COSC-3000"}]

    rows[-1] = ("Completed", "ITEC-3000", "A", "25W", "6")
    setup_student("x2", rows, [{"course_code": "COSC-3000", "term": "26F"}])
    assert audit("x2")["cross_list_violations"][0]["message"].endswith("ITEC-3000")


def test_retakes_and_credit_summary():
    rows = [("Attempted", "COSC-1000", "F", "23F", "0"),
            ("Completed", "COSC-1000", "70", "24W", "3"),
            ("Completed", "COSC-1000", "90", "24F", "3"),
            ("Completed", "MATH-1000", "B", "24F", "3")]
    setup_student("r1", rows, [{"course_code": "COSC-2000", "term": "25W"},
                               {"course_code": "COSC-3000", "term": "25F"},
                               {"course_code": "UNKNOWN-9999", "term": "25F"}])
    cs = audit("r1")["credit_summary"]
    assert cs == {"total_earned": 6, "total_planned": 9, "total_remaining_for_graduation": 105}


def test_remaining_never_negative():
    main.STUDENTS["big"] = {"history": [{"course_code": f"X-{i}", "term": "23F",
                                         "credits_earned": 3, "status": "Completed"}
                                        for i in range(50)], "plan": []}
    assert audit("big")["credit_summary"]["total_remaining_for_graduation"] == 0


def test_display_code_passthrough():
    assert main.display_code("COSC3127") == "COSC-3127"
    assert main.display_code("weird") == "weird"
