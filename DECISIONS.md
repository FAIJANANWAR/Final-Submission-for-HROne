# DECISIONS.md  (about 150-200 words in total - plain language, your own words)
# Technical Decisions

1. **Indexes:** I created a unique index on `emp_code` in the `employees` collection to ensure no duplicate profiles are created. I also added a compound unique index on `(emp_code, date)` in `attendance_logs`. I considered adding a heavy text index for names but rejected it to keep insert speeds fast, as text search wasn't a strict requirement.
2. **Punch-in race:** If two identical punch-in requests arrive at the exact same millisecond, the first request will succeed. The second request will hit the unique compound index on `(emp_code, date)` and MongoDB will throw a DuplicateKeyError. The app catches this and returns a 409 Conflict. 
3. **Ties:** If two employees are tied on late minutes at the cutoff of `limit`, the leaderboard sorts them alphabetically by their `emp_code` in ascending order. This guarantees deterministic results so the leaderboard doesn't shuffle on page reloads.
4. **Headcount:** The department summary aggregation uses a `$lookup` to join attendance logs, followed by an `$unwind` with `preserveNullAndEmptyArrays: true`. This performs a left outer join, ensuring employees with zero logs are still kept in the pipeline and counted in the final grouping.
5. **Scaling to 100x:** If this had to serve 100x the data, I would implement Redis caching for the daily leaderboard and department summaries, as these are heavily read but infrequently updated. I would also move report generation to asynchronous background tasks.
