"""Quick self-checks that run WITHOUT a real MongoDB (uses mongomock). Aggregations are not covered here."""
import mongomock, pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import app.main as m

IST = m.IST

def ms(y, mo, d, h, mi, s=0, ms_=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000) + ms_

@pytest.fixture()
def api(monkeypatch):
    c = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(m, "client", c)
    monkeypatch.setattr(m, "db", c["t"])
    m.ensure_indexes()
    return TestClient(m.app)

def emp(api, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "A", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-05", **kw}
    return api.post("/employees", json=body)

# ---- pure rules -----------------------------------------------------------
def test_late_rule():
    d = datetime(2026, 7, 6).date()
    f = lambda h, mi, s: m.compute_late_minutes(datetime(2026, 7, 6, h, mi, s, tzinfo=IST), d, "09:30")
    assert f(9, 40, 0) == 0 and f(9, 40, 1) == 10 and f(10, 15, 59) == 45 and f(9, 0, 0) == 0

def test_overtime_and_hours():
    d = datetime(2026, 7, 6).date()
    out = lambda h, mi: datetime(2026, 7, 6, h, mi, tzinfo=IST)
    assert m.compute_overtime(out(19, 0), d, "09:30", "18:30") == 30
    assert m.compute_overtime(out(18, 59), d, "09:30", "18:30") == 0
    n = datetime(2026, 7, 6, 22, 0, tzinfo=IST)
    assert m.compute_overtime(datetime(2026, 7, 7, 6, 40, tzinfo=IST), d, "22:00", "06:00") == 40
    assert m.compute_work_hours(out(9, 0), out(13, 30)) == 4.5
    # 2.675 hours = 9630 s -> half-up 2.68 (banker's/float round gives 2.67/2.68 unpredictably)
    assert m.compute_work_hours(out(9, 0), out(9, 0).replace(hour=11, minute=40, second=30)) == 2.68

def test_overnight_date():
    ts = datetime(2026, 7, 7, 1, 0, tzinfo=IST)
    assert m.attendance_date_for(ts, "22:00", "06:00").isoformat() == "2026-07-06"
    assert m.attendance_date_for(ts, "09:30", "18:30").isoformat() == "2026-07-07"

# ---- endpoints ------------------------------------------------------------
def test_employee_flow(api):
    assert emp(api).status_code == 201
    assert emp(api).status_code == 409
    assert emp(api, "BAD1").status_code == 422
    assert emp(api, "EMP0002", shift_start="09:00", shift_end="09:00").status_code == 422
    assert emp(api, "EMP0003", joined_on="2026-02-30").status_code == 422
    r = api.get("/employees", params={"page": 1, "page_size": 1}).json()
    assert r["total"] == 1 and "_id" not in r["items"][0] and isinstance(r["items"][0]["created_at"], int)
    assert api.get("/employees", params={"page": 0}).status_code == 422

def test_punch_in_out(api):
    emp(api)
    assert api.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1.7e12}).status_code == 422
    t_in = ms(2026, 7, 6, 10, 5, 0, 900)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 35 and j["punch_in"] == t_in - 900
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in}).status_code == 409
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_in - 5000}).status_code == 404
    out = ms(2026, 7, 6, 19, 10)
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out})
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 40 and r.json()["half_day"] is False
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}).status_code == 409

def test_patch(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)})
    base = {"reason": "biometric glitch", "regularized_by": "hr"}
    r = api.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ms(2026, 7, 6, 9, 28)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["late_minutes"] == 0 and len(j["history"]) == 1
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"} and isinstance(ch["punch_in"]["from"], int)
    assert api.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ms(2026, 7, 6, 9, 28)}).status_code == 422  # no-op
    assert api.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "LEAVE", "punch_in": ms(2026, 7, 6, 9, 0)}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422  # wrong date
    assert api.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    r = api.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "LEAVE"})
    j = r.json()
    assert r.status_code == 200 and j["punch_in"] is None and j["work_hours"] is None and len(j["history"]) == 2
    assert api.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "WFH"}).status_code == 422  # needs punch_in

def test_list_attendance(api):
    emp(api); emp(api, "EMP0002")
    for c in ("EMP0001", "EMP0002"):
        for d in (6, 7):
            api.post("/attendance/punch-in", json={"emp_code": c, "punched_at": ms(2026, 7, d, 9, 30)})
    r = api.get("/attendance", params={"page_size": 3}).json()
    assert r["total"] == 4 and [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001")]
    assert api.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"status": "BOGUS"}).status_code == 422
    assert api.get("/attendance", params={"emp_code": "EMP0001", "status": "PRESENT"}).json()["total"] == 2

def test_validation_shapes(api):
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-13"}).status_code == 422
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-01-01"}).status_code == 422
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-12-01"}).status_code == 422
    assert api.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
