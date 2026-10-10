"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Layout of this file (top to bottom):
  1. Setup            - Mongo connection, constants, index creation
  2. Small helpers    - time zones, rounding, R1-R5 business rules, (de)serialising
  3. Request models   - what the API accepts (validation happens here -> 422)
  4. Pipelines        - the MongoDB aggregation pipelines (one builder per analytics endpoint)
  5. Endpoints
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional
import calendar

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does NOT override variables that are already set in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Setup
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back datetimes WITH a time zone (UTC). Without it you get
# "naive" datetimes and every conversion to/from epoch milliseconds becomes a guess.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
WEEKDAYS = [2, 3, 4, 5, 6]  # MongoDB $dayOfWeek: 1 = Sunday ... 7 = Saturday  -> Mon..Fri = 2..6
GRACE_SECONDS = 10 * 60
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000

_indexes_ready = False


def ensure_indexes() -> None:
    """Create every index we rely on. create_index is idempotent (safe to run on every start)."""
    global _indexes_ready
    emp, att = db.employees, db.attendance_logs
    # employees
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")  # unique emp_code (409 on duplicates)
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")  # list by dept, sorted
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")  # headcount (R9)
    emp.create_index([("joined_on", ASCENDING)], name="joined")  # headcount without a department filter
    # attendance_logs
    att.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")  # one record/day + monthly
    att.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")  # list order, month range scans
    att.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    att.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")  # punch-out lookup
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # Mongo not up yet: /health will answer 503 and retry
        log.warning("index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Helpers
# --------------------------------------------------------------------------- #
def bad_request(loc: list, msg: str):
    """Raise a 422 that looks exactly like FastAPI's own validation errors."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """datetime -> integer epoch milliseconds (pure integer maths, no float error)."""
    return None if dt is None else (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def whole_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation (and stored that way)."""
    return dt.replace(microsecond=0)


def now_utc_ms_precision() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)  # BSON dates only hold milliseconds


def round_half_up(value, places: int) -> Decimal:
    """R8: 'school' rounding (2.675 -> 2.68). Python's round() is banker's rounding, so we use Decimal."""
    q = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)


def hhmm(s: str) -> time_cls:
    h, m = s.split(":")
    return time_cls(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: the IST calendar date of the punch-in; for overnight shifts, an early-morning punch-in
    (before shift_end) belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        d -= timedelta(days=1)
    return d


def shift_start_dt(d: date_cls, shift_start: str) -> datetime:
    return datetime.combine(d, hhmm(shift_start), tzinfo=IST)


def shift_end_dt(d: date_cls, shift_start: str, shift_end: str) -> datetime:
    base = d + timedelta(days=1) if is_overnight(shift_start, shift_end) else d  # overnight ends next day
    return datetime.combine(base, hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, d: date_cls, shift_start: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since shift start."""
    secs = int((punch_in - shift_start_dt(d, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    secs = int((punch_out - punch_in).total_seconds())
    return float(round_half_up(Decimal(secs) / Decimal(3600), 2))


def compute_overtime(punch_out: datetime, d: date_cls, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only when >= 30."""
    secs = int((punch_out - shift_end_dt(d, shift_start, shift_end)).total_seconds())
    minutes = secs // 60
    return minutes if minutes >= 30 else 0


def derive(status: str, punch_in, punch_out, d_iso: str, shift_start: str, shift_end: str) -> dict:
    """All four derived fields for one record (used by punch-in/out and PATCH)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    d = date_cls.fromisoformat(d_iso)
    out["late_minutes"] = compute_late_minutes(punch_in, d, shift_start)
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, d, shift_start, shift_end)
        out["half_day"] = Decimal(str(wh)) < Decimal("4.50")  # R5 uses the ROUNDED hours
    return out


def parse_ymd(s: str, loc: list) -> date_cls:
    """Strict YYYY-MM-DD that must also be a real calendar day."""
    try:
        if len(s) != 10 or s[4] != "-" or s[7] != "-":
            raise ValueError
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        bad_request(loc, "must be a valid date in YYYY-MM-DD format")


def month_bounds(month: str) -> tuple:
    try:
        y, m = int(month[:4]), int(month[5:7])
        first = date_cls(y, m, 1)
        last = date_cls(y, m, calendar.monthrange(y, m)[1])
    except ValueError:
        bad_request(["query", "month"], "invalid month")
    return first, last


def count_working_days(first: date_cls, last: date_cls, joined_on: date_cls) -> int:
    """R7: Mon-Fri inside the month, counted from joined_on if they joined mid-month."""
    d, n = max(first, joined_on), 0
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d["name"], "email": d["email"], "department": d["department"],
        "shift_start": d.get("shift_start", "09:30"), "shift_end": d.get("shift_end", "18:30"),
        "joined_on": d["joined_on"], "created_at": to_ms(d.get("created_at")),
    }


def history_out(h: dict) -> dict:
    changes = {}
    for key, val in (h.get("changes") or {}).items():
        conv = to_ms if key in ("punch_in", "punch_out") else (lambda x: x)  # dates inside 'changes' too!
        changes[key] = {"from": conv(val.get("from")), "to": conv(val.get("to"))}
    return {"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def record_out(d: dict) -> dict:
    """Mongo document -> API shape. Missing legacy fields get their documented defaults. No _id."""
    return {
        "emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")), "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [history_out(h) for h in (d.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 3. Request models (validation => 422 automatically)
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict=True: refuses floats ("1.7e12"), strings ("1700000000000") and booleans -> 422
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        if len(v) != 10:
            raise ValueError("joined_on must be YYYY-MM-DD")
        datetime.strptime(v, "%Y-%m-%d")  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Aggregation pipelines
#    A pipeline is a list of stages; each stage transforms the stream of documents
#    (like chaining pandas steps, but executed INSIDE MongoDB).
# --------------------------------------------------------------------------- #
def lit(v):
    return {"$literal": v}


def _weekday_expr():
    """True when the record's 'date' string falls on Mon-Fri."""
    parsed = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
    return {"$in": [{"$dayOfWeek": parsed}, WEEKDAYS]}


def _present_expr():
    return {"$in": ["$status", PRESENCE]}


def _day_weight_expr():
    """1 for a present record, 0.5 for a present half-day, 0 for ABSENT/LEAVE (R5, R6)."""
    half = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    return {"$cond": [_present_expr(), {"$cond": [half, 0.5, 1]}, 0]}


def _late_min_expr():
    return {"$ifNull": ["$late_minutes", 0]}


def _late_flag_expr():
    return {"$cond": [{"$gt": [_late_min_expr(), 0]}, 1, 0]}


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [_weekday_expr(), _day_weight_expr(), 0]}},  # Mon-Fri only
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _late_flag_expr()},
            "total_late_minutes": {"$sum": _late_min_expr()},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def dept_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts from EMPLOYEES (so people with zero logs still count in headcount), then joins their logs."""
    match = {"joined_on": {"$lte": end}}  # R9
    if department:
        match["department"] = department
    has_hours = {"$and": [_present_expr(), {"$isNumber": "$work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {
                    "_id": 0,
                    "pd": {"$cond": [_weekday_expr(), _day_weight_expr(), 0]},
                    "lc": _late_flag_expr(), "lm": _late_min_expr(),
                    "leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                    "od": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                    "whn": {"$cond": [has_hours, 1, 0]},
                    # hours in hundredths -> exact integer sums -> exact half-up average later
                    "wh100": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$sum": "$logs.pd"}},
            "late_count": {"$sum": {"$sum": "$logs.lc"}},
            "total_late_minutes": {"$sum": {"$sum": "$logs.lm"}},
            "leave_count": {"$sum": {"$sum": "$logs.leave"}},
            "on_duty_count": {"$sum": {"$sum": "$logs.od"}},
            "hours_n": {"$sum": {"$sum": "$logs.whn"}},
            "hours_100": {"$sum": {"$sum": "$logs.wh100"}},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipe = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees have no 'emp' -> dropped here
    ]
    if department:
        pipe.append({"$match": {"emp.department": department}})  # rank only inside this department
    pipe += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties may exceed it
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def trend_pipeline(department: str, d_from: date_cls, d_to: date_cls) -> list:
    """One row per calendar day. Idea:
       - stream A: how many employees joined on each day (clamped to the range start) -> cumulative sum = headcount
       - stream B: present/late totals per day from the logs
       - a one-document 'seed' so $densify always has something to fill from
       - $densify adds the days that have no data; $setWindowFields does the running sums."""
    f, t = d_from.isoformat(), d_to.isoformat()
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    seed = [{"$match": {"department": department}}, {"$limit": 1}, {"$replaceWith": {"_id": lit(f)}}]
    logs_stream = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$project": {"_id": 0, "date": 1, "pw": _day_weight_expr(), "lc": _late_flag_expr()}},
            ],
            "as": "l",
        }},
        {"$unwind": "$l"},
        {"$group": {"_id": "$l.date", "present_count": {"$sum": "$l.pw"}, "late_count": {"$sum": "$l.lc"}}},
    ]
    r4_present2 = {"$multiply": [2, "$present_count"]}
    # half-up rounding to 4 decimals in exact integer arithmetic: floor((10000*2P + H) / (2H))
    r4 = {"$floor": {"$divide": [{"$add": [{"$multiply": [10000, r4_present2]}, "$headcount"]},
                                 {"$multiply": [2, "$headcount"]}]}}
    win7 = {"documents": [-6, "current"]}
    return [
        {"$match": {"department": department, "joined_on": {"$lte": t}}},
        {"$group": {"_id": {"$cond": [{"$lt": ["$joined_on", lit(f)]}, lit(f), "$joined_on"]}, "joins": {"$sum": 1}}},
        {"$unionWith": {"coll": "employees", "pipeline": seed}},
        {"$unionWith": {"coll": "employees", "pipeline": logs_stream}},
        {"$group": {"_id": "$_id", "joins": {"$sum": "$joins"},
                    "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$setWindowFields": {"sortBy": {"d": 1},
                              "output": {"headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]},
                  "wd": {"$in": [{"$dayOfWeek": "$d"}, WEEKDAYS]}}},
        {"$set": {"r4": {"$cond": [{"$and": ["$wd", {"$gt": ["$headcount", 0]}]}, r4, None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "s_w": {"$sum": "$r4", "window": win7},
            "n_w": {"$sum": {"$cond": [{"$ne": ["$r4", None]}, 1, 0]}, "window": win7},
        }}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "is_working_day": "$wd",
            "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$ne": ["$r4", None]}, {"$divide": ["$r4", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$n_w", 0]}, {"$divide": [
                {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$s_w"]}, "$n_w"]}, {"$multiply": [2, "$n_w"]}]}},
                10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]


def attendance_find_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """The ONE place that builds the list query - the endpoint and /admin/explain both use it."""
    flt = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        flt["date"] = rng
    if status:
        flt["status"] = status
    # with a single employee, emp_code is constant so sorting by date alone gives the same order
    sort = {"date": -1} if emp_code else {"date": -1, "emp_code": 1}
    return {"filter": flt, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size, "projection": {"_id": 0}}


def check_date_range(date_from, date_to):
    a = parse_ymd(date_from, ["query", "date_from"]) if date_from else None
    b = parse_ymd(date_to, ["query", "date_to"]) if date_to else None
    if a and b and a > b:
        bad_request(["query", "date_from"], "date_from must not be after date_to")


def check_trend_range(d_from: str, d_to: str) -> tuple:
    a = parse_ymd(d_from, ["query", "from"])
    b = parse_ymd(d_to, ["query", "to"])
    if b < a:
        bad_request(["query", "to"], "'to' must not be before 'from'")
    if (b - a).days + 1 > 92:
        bad_request(["query", "to"], "range must not exceed 92 days")
    return a, b


# --------------------------------------------------------------------------- #
# 5. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not available")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_ms_precision()
    try:
        db.employees.insert_one(doc)  # the UNIQUE INDEX decides who wins a race, not a find_one()
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = {"department": department} if department else {}
    total = db.employees.count_documents(flt)
    cur = db.employees.find(flt, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = whole_seconds(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    d = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": d.isoformat(), "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index -> exactly one 201 under a race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = whole_seconds(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    # most recent record whose punch_in is at/before this punch-out (works across midnight)
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = as_utc(rec["punch_in"])
    if ts <= p_in:
        bad_request(["body", "punched_at"], "punched_at must be after punch_in")
    if ts - p_in > timedelta(hours=24):
        bad_request(["body", "punched_at"], "punch_out must be within 24 hours of punch_in")
    fields = derive(rec["status"], p_in, ts, rec["date"], emp["shift_start"], emp["shift_end"])
    fields.pop("late_minutes")  # late minutes were fixed at punch-in
    # compare-and-set: only update if punch_out is STILL empty -> exactly one of two racing requests wins
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    check_date_range(date_from, date_to)
    spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"]) if spec["filter"] else db.attendance_logs.estimated_document_count()
    cur = (db.attendance_logs.find(spec["filter"], spec["projection"])
           .sort(list(spec["sort"].items())).skip(spec["skip"]).limit(spec["limit"]))
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_ymd(date, ["path", "date"])
    given = body.model_fields_set
    for f in ("status", "punch_in", "punch_out"):
        if f in given and getattr(body, f) is None:
            bad_request(["body", f], f"{f} must not be null")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    s_start, s_end = emp["shift_start"], emp["shift_end"]

    for _attempt in range(6):  # optimistic concurrency: re-read and retry if someone else changed the record
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for this date")
        old_in = as_utc(rec["punch_in"]) if rec.get("punch_in") else None
        old_out = as_utc(rec["punch_out"]) if rec.get("punch_out") else None

        status = body.status or rec["status"]
        if status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                bad_request(["body"], f"punch times cannot be supplied with status {status}")
            new_in = new_out = None
        else:
            new_in = whole_seconds(from_ms(body.punch_in)) if body.punch_in is not None else old_in
            new_out = whole_seconds(from_ms(body.punch_out)) if body.punch_out is not None else old_out
            if new_in is None:
                bad_request(["body", "punch_in"], "a presence status requires a punch_in")
            if body.punch_in is not None and attendance_date_for(new_in, s_start, s_end).isoformat() != date:
                bad_request(["body", "punch_in"], "punch_in must stay on the record's attendance date")
            if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
                bad_request(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")

        new = {"status": status, "punch_in": new_in, "punch_out": new_out,
               **derive(status, new_in, new_out, date, s_start, s_end)}
        old = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
               "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
               "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
        changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
        if not changes:
            bad_request(["body"], "the request changes nothing")

        entry = {"at": now_utc_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        hist = rec.get("history")
        guard = {"emp_code": emp_code, "date": date, "status": rec["status"],
                 "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
                 # nobody else appended to history since we read it:
                 "history": {"$size": len(hist)} if isinstance(hist, list) else None}
        updated = db.attendance_logs.find_one_and_update(
            guard,
            {"$set": {**new, "history": (hist if isinstance(hist, list) else []) + [entry]}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    r = rows[0] if rows else {}
    working = count_working_days(first, last, date_cls.fromisoformat(emp["joined_on"]))
    present = Decimal(str(r.get("present_days", 0)))
    pct = float(round_half_up(present * 100 / Decimal(working), 2)) if working else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working,
        "present_days": float(round_half_up(present, 2)), "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)), "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)), "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = db.employees.aggregate(dept_summary_pipeline(first.isoformat(), last.isoformat(), department), allowDiskUse=True)
    items = []
    for r in rows:
        n = int(r["hours_n"])
        avg = float(round_half_up(Decimal(int(r["hours_100"])) / Decimal(n) / 100, 2)) if n else None
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]),
            "present_days": float(round_half_up(r["present_days"], 2)), "avg_work_hours": avg,
            "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(
        leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department), allowDiskUse=True)
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
              "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])} for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    d_from, d_to = check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    rows = db.employees.aggregate(trend_pipeline(department, d_from, d_to), allowDiskUse=True)
    items = [{
        "date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
        "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
        "attendance_rate": r["attendance_rate"], "moving_avg_7d": r["moving_avg_7d"],
    } for r in rows]
    return {"department": department, "items": items}


ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _need(value, name):
    if value is None or value == "":
        bad_request(["query", name], f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Asks MongoDB HOW it would run the endpoint's main query (the query planner's report)."""
    if endpoint == "attendance_list":
        check_date_range(date_from, date_to)
        spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
        coll, cmd = "attendance_logs", {"find": "attendance_logs", **spec}
    else:
        if endpoint == "employee_monthly":
            first, last = month_bounds(_need(month, "month"))
            coll, pipe = "attendance_logs", monthly_pipeline(_need(emp_code, "emp_code"), first.isoformat(), last.isoformat())
        elif endpoint == "department_summary":
            first, last = month_bounds(_need(month, "month"))
            coll, pipe = "employees", dept_summary_pipeline(first.isoformat(), last.isoformat(), department)
        elif endpoint == "late_leaderboard":
            first, last = month_bounds(_need(month, "month"))
            coll, pipe = "attendance_logs", leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
        else:  # department_trend
            a, b = check_trend_range(_need(from_, "from"), _need(to, "to"))
            coll, pipe = "employees", trend_pipeline(_need(department, "department"), a, b)
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return JSONResponse({
        "endpoint": endpoint, "collection": coll,
        "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS)),
    })
