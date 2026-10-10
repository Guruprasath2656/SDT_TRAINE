# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` and unique `(emp_code, date)` on logs: they stop duplicates and serve the monthly query. `(date desc, emp_code)` serves the list order and month-range scans. `(status, date, emp_code)` serves the status filter. `(emp_code, punch_in)` finds the record to punch out. `(department, joined_on)` serves headcount. Rejected: an index on `late_minutes` alone; it is not selective and would slow every write.
2. **Punch-in race.** Both requests call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets `DuplicateKeyError`, which I turn into 409. I never check first.
3. **Ties.** Ranking happens first (`$rank`, so 1, 2, 2, 4), then I keep every row with rank <= `limit`. Tied people at the cutoff are all returned, so the list can be longer than `limit`.
4. **Headcount.** The department summary starts from `employees` and joins logs onto them, so someone with zero logs still counts as one person.
5. **At 100x data** I would stop scanning raw logs for monthly analytics and keep a small pre-computed monthly total per employee, updated on each write.
