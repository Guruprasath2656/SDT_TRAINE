# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware=True`, so every datetime read back is naive (no time zone). | Subtracting a stored time from an IST time raises `TypeError`; epoch conversions are silently off. | `tz_aware=True`; all conversion to/from epoch ms is in `to_ms` / `from_ms`. |
| 2 | `compute_late_minutes` | Builds shift start with `punch_in.replace(...)`, i.e. in the punch's own zone (UTC/server), not IST, and on the punch's calendar day (wrong for overnight shifts). | Shift 09:30, punch 09:31 IST gives hundreds of minutes late. | Shift start is built on the attendance date in IST (`shift_start_dt`). |
| 3 | `compute_late_minutes` | `int(seconds/60) > 10`: 09:40:01 floors to 10, which is not > 10, so returns 0. R2 says strictly more than 10:00 in time. | Punch 09:40:01, expect 10, got 0. | Compare seconds `> 600`, then floor to minutes. |
| 4 | `compute_late_minutes`, `punch_in` | Sub-second part never truncated (R1). | Punch 09:40:00.900 treated as late-by-a-bit. | `whole_seconds()` before any calculation, and the truncated value is stored. |
| 5 | `compute_work_hours` | `round()` is banker's/float rounding, not half-up (R4). | 2.675 h may give 2.67. | `Decimal` with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | No 30-minute minimum (R3); overnight shift end not moved to next day; mixes naive and aware datetimes. | Out at 18:35 returns 5 instead of 0; overnight gives 0 or negative; `TypeError`. | Rewritten with `shift_end_dt` (overnight aware) and `>= 30` rule. |
| 7 | `/health` | Always answers ok without touching MongoDB. | Stop MongoDB: still 200, so the grader starts testing too early. | Pings MongoDB; 503 if it fails. |
| 8 | startup | No indexes are created at all. | `/admin/explain` shows COLLSCAN; duplicates possible. | `ensure_indexes()` in the lifespan hook (idempotent). |
| 9 | `create_employee` | `find_one` then `insert_one` is a race; no unique index. | Two parallel POSTs both return 201. | Unique index; catch `DuplicateKeyError` -> 409. |
| 10 | `EmployeeIn` | No validation (emp_code format, email, lengths, real `joined_on`, shift_start == shift_end). | `emp_code: "x"` returns 201. | Pydantic constraints and validators -> 422. |
| 11 | `create_employee` | `created_at = datetime.now()` is naive local time and is returned as a datetime, not epoch ms. | Response has a string, not an integer. | UTC, millisecond precision, returned via `employee_out` as epoch ms. |
| 12 | `list_employees` | `skip = page * page_size` skips the whole first page. | Page 1 starts at employee 21. | `(page - 1) * page_size`. |
| 13 | `list_employees` | `total` counts the whole collection, ignoring the department filter (R10). | Filter by department: `total` too big. | `count_documents(filter)`. |
| 14 | `list_employees` | No sort, and no limits on `page` / `page_size` (page=0 -> negative skip -> 500). | Order changes; `page=0` crashes. | Sort by `emp_code`; `page >= 1`, `1 <= page_size <= 100` -> 422. |
| 15 | `punch_in` | Unknown employee: `emp` is `None`, then `emp["shift_start"]` raises `TypeError` (500). | Punch in with a bad code. | Explicit 404. |
| 16 | `punch_in` | Check-then-insert race, no unique index. | Two simultaneous punch-ins both get 201. | Insert directly; unique `(emp_code, date)` index -> 409. |
| 17 | `PunchInIn` / `punch_in` | `if body.punched_at` treats 0 as missing; accepts floats, strings and seconds; `fromtimestamp(ms/1000)` uses server local time (naive) and float maths. | Send `1783312500` (seconds): accepted as 1970. | Strict int in range 1e11..4.1e12 -> 422; integer-ms conversion to UTC. |
| 18 | `punch_in` | `date` is the server-local `ts.date()`, not the IST attendance date; overnight shifts (R1) ignored. | 01:00 IST punch of a 22:00-06:00 shift lands on the wrong day. | `attendance_date_for()`. |
| 19 | `PunchInIn.status` | Any string accepted (even `ABSENT`, or garbage). | `status: "xyz"` returns 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 20 | `punch_in` response | Returns `id` (ObjectId string) and raw datetimes. Contract: no ids, instants in epoch ms. | Response contains `id`. | `record_out()`. |
| 21 | `list_attendance` | Loads the whole collection into memory and sorts in Python; `total = len(docs)`. | Very slow on ~100k records. | `find` + `sort/skip/limit` in MongoDB with indexes; `count_documents`. |
| 22 | `list_attendance` | Sort has no `emp_code` tie-break; `page_size` unbounded, `page=0` gives odd slices; `status` and dates not validated; `date_from > date_to` not rejected. | Rows jump between pages; bad dates silently match nothing. | Contract sort, validated params -> 422. |
| 23 | `list_attendance` | Legacy rows without `half_day` / `history` are returned as-is; ids leak; datetimes not ms. | Rows from `DATA_MODEL.md` example 9. | `record_out()` applies defaults. |

**Looked at and decided NOT a defect:** `load_dotenv()` (it does not override real environment variables, as required); the `localhost` fallback for `MONGO_URI` (only a dev default, real env wins); `status_code=201` on create; `doc.pop("_id")` after `insert_one` (harmless, now replaced by explicit output shaping); shift defaults `09:30` / `18:30` (match the contract).
