# REVIEW.md
# Code Review

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` endpoint | The health check is fake and just returns a static string. | Calling `/health` returns "ok" even if the MongoDB database is completely disconnected. | Updated the endpoint to ping the MongoDB database directly to verify the connection status. |
| 2 | `list_attendance` | Pagination is unpredictable due to lack of sorting. | Calling page 1 and page 2 might show overlapping or skipped records if the database rearranges items internally. | Added a `.sort()` operation before applying skip and limit to ensure consistent pagination. |
| 3 | `punch_out` endpoint | Race condition during concurrent requests. | If an employee clicks "Punch Out" twice very fast, it might log both or overwrite data improperly. | Replaced standard find/update with MongoDB's atomic `find_one_and_update`. |
| 4 | JSON Responses | Datetime objects don't match OpenAPI schema. | The Swagger UI throws validation errors because it expects epoch integer milliseconds, but gets string ISO dates. | Formatted the BSON/Datetime outputs to integer timestamps before returning them. |

**Not a defect:**
I looked at the `shift_start` and `shift_end` fields being stored as simple strings (e.g., "09:30") instead of proper datetime objects. I decided this is **not** a defect because they represent recurring daily time boundaries, and string parsing for these daily offsets is fully sufficient for the business logic.