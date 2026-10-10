# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local: docker run -p 27017:27017 mongo:7)
uvicorn app.main:app --port 8000 --reload
```
MongoDB 6.0+ is required (uses `$setWindowFields`, `$densify`, `$unionWith`). Open `http://localhost:8000/docs` to try every endpoint.
Indexes are created automatically at startup.

## Notes
- Times are stored as UTC datetimes and exposed as epoch milliseconds; dates are IST `YYYY-MM-DD` strings.
- Race safety comes from database guarantees: unique indexes (employee, one record per employee per day) and compare-and-set updates (punch-out, PATCH).
- Analytics are MongoDB aggregation pipelines; `/admin/explain/{endpoint}` shows the query plan for each.
- Quick checks without a database: `pip install pytest mongomock httpx && pytest tests`.
