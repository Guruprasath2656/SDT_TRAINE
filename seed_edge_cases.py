"""
DEV TOOL (not part of the submission).  Seeds a tiny database full of awkward cases and prints what the
analytics endpoints SHOULD return, computed with plain Python (no MongoDB). Compare with curl output.

    python dev/seed_edge_cases.py --yes            # wipe + seed the edge cases, print expected answers
    python dev/seed_edge_cases.py --yes --bulk     # additionally add ~100k records to check /admin/explain speed

WARNING: it DELETES both collections in $MONGO_DB. Use a throw-away database name.
"""
import json, os, random, sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
UTC, IST = timezone.utc, timezone(timedelta(hours=5, minutes=30))
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}

EMPLOYEES = [  # emp_code, department, shift_start, shift_end, joined_on
    ("EMP0001", "Engineering", "09:30", "18:30", "2026-01-05"),
    ("EMP0002", "Engineering", "09:30", "18:30", "2026-01-05"),
    ("EMP0003", "Engineering", "09:30", "18:30", "2026-07-15"),   # joins mid-month
    ("EMP0004", "Engineering", "09:30", "18:30", "2026-01-05"),   # has NO logs at all
    ("EMP0005", "Sales", "09:30", "18:30", "2026-01-05"),
    ("EMP0006", "Sales", "09:30", "18:30", "2026-01-05"),
    ("EMP0007", "Support", "22:00", "06:00", "2026-03-10"),       # overnight shift
]
# emp_code, date, status, late, overtime, work_hours, half_day   (stored values are authoritative)
LOGS = [
    ("EMP0001", "2026-07-06", "PRESENT", 0, 0, 9.0, False),
    ("EMP0001", "2026-07-07", "PRESENT", 35, 40, 9.5, False),
    ("EMP0001", "2026-07-08", "LEAVE", 0, 0, None, False),
    ("EMP0001", "2026-07-09", "WFH", 0, 0, 3.5, True),            # half day = 0.5
    ("EMP0001", "2026-07-11", "PRESENT", 20, 0, 8.0, False),      # Saturday: late counts, present_days does not
    ("EMP0002", "2026-07-06", "PRESENT", 35, 0, 8.5, False),
    ("EMP0002", "2026-07-07", "PRESENT", 35, 0, 8.5, False),      # total 70
    ("EMP0003", "2026-07-16", "PRESENT", 0, 0, 9.0, False),
    ("EMP0005", "2026-07-06", "PRESENT", 40, 0, 8.0, False),
    ("EMP0005", "2026-07-08", "PRESENT", 30, 0, 8.0, False),      # total 70 -> ties with EMP0002 (rank 1,1)
    ("EMP0006", "2026-07-06", "ABSENT", 0, 0, None, False),
    ("EMP0006", "2026-07-07", "ON_DUTY", 0, 0, 8.0, False),
    ("EMP0007", "2026-07-06", "PRESENT", 55, 0, 8.75, False),     # total 55
    ("GHOST01", "2026-07-06", "PRESENT", 99, 0, 8.0, False),      # unknown employee -> must be ignored
]

def rhu(x, p):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))

def is_wd(d): return date.fromisoformat(d).weekday() < 5
def weight(r): return (0.5 if r[6] else 1) if r[2] in PRESENT else 0
def in_month(d, month): return d.startswith(month)
def emp_map(): return {e[0]: e for e in EMPLOYEES}

def expected_leaderboard(month, limit, dept=None):
    em, tot = emp_map(), {}
    for r in LOGS:
        if in_month(r[1], month) and r[0] in em and r[3] > 0 and (not dept or em[r[0]][1] == dept):
            t = tot.setdefault(r[0], [0, 0]); t[0] += r[3]; t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for i, (c, (m, n)) in enumerate(rows):
        rank = 1 + sum(1 for _, (m2, _) in rows if m2 > m)   # standard competition rank
        out.append({"rank": rank, "emp_code": c, "total_late_minutes": m, "late_count": n})
    return [o for o in out if o["rank"] <= limit]

def expected_summary(month):
    first = date.fromisoformat(month + "-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    res = {}
    for code, dept, *_rest, joined in EMPLOYEES:
        if date.fromisoformat(joined) > last: continue
        d = res.setdefault(dept, dict(department=dept, headcount=0, present_days=0.0, hrs=[], late_count=0,
                                      total_late_minutes=0, leave_count=0, on_duty_count=0))
        d["headcount"] += 1
        for r in LOGS:
            if r[0] != code or not in_month(r[1], month): continue
            if is_wd(r[1]): d["present_days"] += weight(r)
            if r[2] in PRESENT and r[5] is not None: d["hrs"].append(Decimal(str(r[5])))
            if r[3] > 0: d["late_count"] += 1; d["total_late_minutes"] += r[3]
            d["leave_count"] += r[2] == "LEAVE"; d["on_duty_count"] += r[2] == "ON_DUTY"
    items = []
    for dept in sorted(res):
        d = res.pop(dept); h = d.pop("hrs")
        d["avg_work_hours"] = rhu(sum(h) / len(h), 2) if h else None
        items.append(d)
    return items

def expected_monthly(code, month):
    e = emp_map()[code]
    first = date.fromisoformat(month + "-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    start, wd = max(first, date.fromisoformat(e[4])), 0
    while start <= last:
        wd += start.weekday() < 5; start += timedelta(days=1)
    mine = [r for r in LOGS if r[0] == code and in_month(r[1], month)]
    present = sum(weight(r) for r in mine if is_wd(r[1]))
    return {"working_days": wd, "present_days": present, "leave_days": sum(r[2] == "LEAVE" for r in mine),
            "late_count": sum(r[3] > 0 for r in mine), "total_late_minutes": sum(r[3] for r in mine),
            "attendance_pct": rhu(Decimal(str(present)) * 100 / wd, 2) if wd else None}

def expected_trend(dept, d_from, d_to):
    codes = {e[0]: e for e in EMPLOYEES if e[1] == dept}
    day, rows, rates = date.fromisoformat(d_from), [], []
    while day <= date.fromisoformat(d_to):
        iso = day.isoformat()
        hc = sum(date.fromisoformat(e[4]) <= day for e in codes.values())
        day_logs = [r for r in LOGS if r[0] in codes and r[1] == iso]
        present = sum(weight(r) for r in day_logs)
        late = sum(r[3] > 0 for r in day_logs)
        wd = day.weekday() < 5
        rate = rhu(Decimal(str(present)) / hc, 4) if (wd and hc > 0) else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        rows.append({"date": iso, "is_working_day": wd, "headcount": hc, "present_count": present, "late_count": late,
                     "attendance_rate": rate, "moving_avg_7d": rhu(sum(Decimal(str(x)) for x in window) / len(window), 4) if window else None})
        day += timedelta(days=1)
    return rows

def seed(db):
    db.employees.delete_many({}); db.attendance_logs.delete_many({})
    db.employees.insert_many([{"emp_code": c, "name": f"Name {c[-1]}", "email": f"{c.lower()}@example.com", "department": d,
                               "shift_start": s, "shift_end": e, "joined_on": j,
                               "created_at": datetime(2026, 1, 1, tzinfo=UTC)} for c, d, s, e, j in EMPLOYEES])
    docs = []
    for c, d, st, late, ot, wh, half in LOGS:
        pin = pout = None
        if st in PRESENT:
            pin = datetime.fromisoformat(d).replace(hour=4, minute=0, tzinfo=UTC)
            pout = pin + timedelta(hours=wh) if wh else None
        docs.append({"emp_code": c, "date": d, "status": st, "punch_in": pin, "punch_out": pout, "work_hours": wh,
                     "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []})
    db.attendance_logs.insert_many(docs)

def bulk(db, n=100_000):
    codes = [f"EMP{i:05d}" for i in range(1, 501)]
    depts = ["Engineering", "Sales", "Support", "HR", "Finance"]
    db.employees.insert_many([{"emp_code": c, "name": c, "email": f"{c}@x.com", "department": depts[i % 5], "shift_start": "09:30",
                               "shift_end": "18:30", "joined_on": "2026-01-05", "created_at": datetime.now(UTC)}
                              for i, c in enumerate(codes)])
    docs, start = [], date(2026, 1, 1)
    for i in range(n):
        c, d = codes[i % 500], (start + timedelta(days=i // 500)).isoformat()
        late = random.choice([0, 0, 0, 12, 35])
        docs.append({"emp_code": c, "date": d, "status": "PRESENT", "punch_in": datetime.now(UTC), "punch_out": None,
                     "work_hours": 8.5, "late_minutes": late, "overtime_minutes": 0, "half_day": False, "history": []})
    db.attendance_logs.insert_many(docs, ordered=False)

if __name__ == "__main__":
    if "--yes" not in sys.argv:
        sys.exit(__doc__)
    db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]
    seed(db)
    if "--bulk" in sys.argv:
        bulk(db); print("bulk data added")
    print("seeded. EXPECTED answers (July 2026):\n")
    print("leaderboard limit=1 :", json.dumps(expected_leaderboard("2026-07", 1)))
    print("leaderboard limit=3 :", json.dumps(expected_leaderboard("2026-07", 3)))
    print("leaderboard Sales   :", json.dumps(expected_leaderboard("2026-07", 10, "Sales")))
    print("dept summary        :", json.dumps(expected_summary("2026-07"), indent=1))
    print("monthly EMP0001     :", expected_monthly("EMP0001", "2026-07"))
    print("monthly EMP0003     :", expected_monthly("EMP0003", "2026-07"))
    print("trend Engineering 07-05..07-19:", json.dumps(expected_trend("Engineering", "2026-07-05", "2026-07-19"), indent=0))
