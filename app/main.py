"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import calendar
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi import Request
from pydantic import BaseModel
from pymongo import ReturnDocument, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup Event for Indexes (Concurrency & Performance Fix)
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def startup_db_client():
    # Indexes created idempotently at startup
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index("date")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_ist = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in_ist - start_ist).total_seconds() // 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out_utc: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    punch_out_ist = punch_out_utc.astimezone(IST)
    end_ist = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    minutes = max(0, int((punch_out_ist - end_ist).total_seconds() // 60))
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class CorrectionIn(BaseModel):
    # Required fields for the audit trail
    by: str
    reason: str
    
    # Optional fields that can be corrected
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    status: Optional[str] = None

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    ts_now = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = ts_now
    
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
        
    doc.pop("_id", None)
    doc["created_at"] = int(ts_now.timestamp() * 1000)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
        
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    
    for item in items:
        if "created_at" in item and isinstance(item["created_at"], datetime):
            item["created_at"] = int(item["created_at"].timestamp() * 1000)
            
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
        
    if body.punched_at:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
        
    ts_ist = ts.astimezone(IST)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    
    d_date = ts_ist.date()
    if shift_end <= shift_start:
        h_end, m_end = map(int, shift_end.split(":"))
        if ts_ist.hour < h_end or (ts_ist.hour == h_end and ts_ist.minute <= m_end):
            d_date = d_date - timedelta(days=1)
            
    d = d_date.isoformat()
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
        
    doc.pop("_id", None)
    doc["punch_in"] = int(ts.timestamp() * 1000)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
        
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    page_docs = list(db.attendance_logs.find(q, {"_id": 0}).sort("date", -1).skip(skip).limit(page_size))
    
    for d in page_docs:
        if d.get("punch_in"):
            d["punch_in"] = int(d["punch_in"].timestamp() * 1000)
        if d.get("punch_out"):
            d["punch_out"] = int(d["punch_out"].timestamp() * 1000)
        if d.get("history"):
            for h in d["history"]:
                if h.get("at"):
                    h["at"] = int(h["at"].timestamp() * 1000)
                    
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODOthe rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
        
    # Find the active record that hasn't been punched out yet
    log = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_out": None})
    if not log:
        raise HTTPException(404, "no active punch-in found")
        
    # Convert incoming timestamp to UTC BSON datetime, truncated to whole seconds
    if body.punched_at:
        ts_out = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)
    else:
        ts_out = datetime.now(timezone.utc).replace(microsecond=0)
        
    # Apply UTC timezone to the naive datetime retrieved from MongoDB
    punch_in_ts = log["punch_in"].replace(tzinfo=timezone.utc)
    
    # Calculate derived fields as per R3, R4, R5
    work_hours = compute_work_hours(punch_in_ts, ts_out)
    overtime = compute_overtime(ts_out, emp["shift_end"], log["date"])
    half_day = True if work_hours < 4.50 else False
    
    update_data = {
        "punch_out": ts_out,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day
    }
    
    # Atomic update to prevent race conditions during concurrent punch-outs
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"], "punch_out": None},
        {"$set": update_data},
        return_document=ReturnDocument.AFTER
    )
    
    if not updated_doc:
        raise HTTPException(409, "already punched out or record modified concurrently")
        
    updated_doc.pop("_id", None)
    
    # Convert datetimes back to integer epoch milliseconds for the response
    if updated_doc.get("punch_in"):
        updated_doc["punch_in"] = int(updated_doc["punch_in"].replace(tzinfo=timezone.utc).timestamp() * 1000)
    if updated_doc.get("punch_out"):
        updated_doc["punch_out"] = int(updated_doc["punch_out"].replace(tzinfo=timezone.utc).timestamp() * 1000)
        
    if updated_doc.get("history"):
        for h in updated_doc["history"]:
            if h.get("at"):
                h["at"] = int(h["at"].replace(tzinfo=timezone.utc).timestamp() * 1000)
                
    return updated_doc
@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(emp_code: str, date: str, body: CorrectionIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
        
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "attendance record not found")
        
    changes = {}
    updates = {}
    
    # Safely extract existing temporal values with UTC timezone
    current_in = log.get("punch_in")
    if current_in:
        current_in = current_in.replace(tzinfo=timezone.utc)
        
    current_out = log.get("punch_out")
    if current_out:
        current_out = current_out.replace(tzinfo=timezone.utc)
        
    new_in = current_in
    new_out = current_out
    
    # Evaluate punch_in modifications
    if body.punch_in is not None:
        candidate_in = datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc).replace(microsecond=0)
        if candidate_in != current_in:
            changes["punch_in"] = {
                "before": int(current_in.timestamp() * 1000) if current_in else None, 
                "after": body.punch_in
            }
            updates["punch_in"] = candidate_in
            new_in = candidate_in

    # Evaluate punch_out modifications
    if body.punch_out is not None:
        candidate_out = datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc).replace(microsecond=0)
        if candidate_out != current_out:
            changes["punch_out"] = {
                "before": int(current_out.timestamp() * 1000) if current_out else None, 
                "after": body.punch_out
            }
            updates["punch_out"] = candidate_out
            new_out = candidate_out
            
    # Evaluate status modifications
    if body.status is not None and body.status != log.get("status"):
        changes["status"] = {"before": log.get("status"), "after": body.status}
        updates["status"] = body.status
        
    # Recompute derived fields based on updated timestamps (late, work_hours, overtime, half_day)
    if "punch_in" in changes or "punch_out" in changes:
        if new_in:
            new_late = compute_late_minutes(new_in, emp["shift_start"])
            if new_late != log.get("late_minutes"):
                changes["late_minutes"] = {"before": log.get("late_minutes"), "after": new_late}
                updates["late_minutes"] = new_late
        
        if new_in and new_out:
            new_work_hours = compute_work_hours(new_in, new_out)
            new_overtime = compute_overtime(new_out, emp["shift_end"], date)
            new_half_day = True if new_work_hours < 4.50 else False
            
            if new_work_hours != log.get("work_hours"):
                changes["work_hours"] = {"before": log.get("work_hours"), "after": new_work_hours}
                updates["work_hours"] = new_work_hours
                
            if new_overtime != log.get("overtime_minutes"):
                changes["overtime_minutes"] = {"before": log.get("overtime_minutes"), "after": new_overtime}
                updates["overtime_minutes"] = new_overtime
                
            if new_half_day != log.get("half_day"):
                changes["half_day"] = {"before": log.get("half_day"), "after": new_half_day}
                updates["half_day"] = new_half_day

    # Prevent appending empty history entries if identical data is submitted
    if not changes:
        log.pop("_id", None)
        if log.get("punch_in"): 
            log["punch_in"] = int(log["punch_in"].replace(tzinfo=timezone.utc).timestamp() * 1000)
        if log.get("punch_out"): 
            log["punch_out"] = int(log["punch_out"].replace(tzinfo=timezone.utc).timestamp() * 1000)
        for h in log.get("history", []):
            if h.get("at") and isinstance(h["at"], datetime):
                h["at"] = int(h["at"].replace(tzinfo=timezone.utc).timestamp() * 1000)
        return log

    # Construct the audit trail entry
    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.by,
        "reason": body.reason,
        "changes": changes
    }
    
    # Execute an atomic update to safely apply field changes and append history
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"]},
        {"$set": updates, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER
    )
    
    # Format dates back to epoch milliseconds for the API response payload
    updated_doc.pop("_id", None)
    if updated_doc.get("punch_in"): 
        updated_doc["punch_in"] = int(updated_doc["punch_in"].replace(tzinfo=timezone.utc).timestamp() * 1000)
    if updated_doc.get("punch_out"): 
        updated_doc["punch_out"] = int(updated_doc["punch_out"].replace(tzinfo=timezone.utc).timestamp() * 1000)
    for h in updated_doc.get("history", []):
        if h.get("at") and isinstance(h["at"], datetime):
            h["at"] = int(h["at"].replace(tzinfo=timezone.utc).timestamp() * 1000)
            
    return updated_doc
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    pipeline = [
        # 1. Match employee aur month (date string starts with YYYY-MM)
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$regex": f"^{month}"}
            }
        },
        # 2. Date string ko BSON date mein convert karein taaki Day of Week nikal sakein
        {
            "$addFields": {
                "parsed_date": {"$dateFromString": {"dateString": "$date"}},
                "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
            }
        },
        {
            "$addFields": {
                # $dayOfWeek: Sunday=1, Monday=2, ..., Saturday=7
                "day_of_week": {"$dayOfWeek": "$parsed_date"}
            }
        },
        # 3. Calculate present day value (R5, R6, R7)
        {
            "$addFields": {
                "present_day_value": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}, # Sirf Monday - Friday
                                {"$eq": ["$is_present_status", True]}
                            ]
                        },
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, # Half day = 0.5
                        0
                    ]
                }
            }
        },
        # 4. Saare records ko sum up karein
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_day_value"},
                "late_minutes": {"$sum": "$late_minutes"},
                "overtime_minutes": {"$sum": "$overtime_minutes"},
                "work_hours": {"$sum": "$work_hours"}
            }
        },
        # 5. Output format aur rounding (R8)
        {
            "$project": {
                "_id": 0,
                "month": {"$literal": month},
                "present_days": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "work_hours": {"$round": ["$work_hours", 2]}
            }
        }
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    if not result:
        return {
            "month": month,
            "present_days": 0.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "work_hours": 0.0
        }
        
    return result[0]
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    date_from: str, 
    date_to: str, 
    limit: int = 10
):
    pipeline = [
        # 1. Sirf diye gaye date range ke records filter karein
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to}
            }
        },
        # 2. Har employee ke total late minutes sum karein
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"}
            }
        },
        # 3. MongoDB Window function se ranking assign karein
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1}, # Highest late minutes ko Rank 1 milega
                "output": {
                    "rank": {"$rank": {}} # $rank automatically ties mein skip karta hai (1, 2, 2, 4)
                }
            }
        },
        # 4. Sirf unhe rank karein jo actually late hain (0 late minutes wale exclude honge)
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        # 5. Ranks ke hisaab se sort karein, aur ties hone par emp_code alphabetical order mein aaye
        {
            "$sort": {
                "rank": 1,
                "_id": 1 
            }
        },
        # 6. Ranking aur sorting ke baad limit apply karein (jaisa rules mein likha hai)
        {
            "$limit": limit
        },
        # 7. Final output JSON format theek karein
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "total_late_minutes": 1,
                "rank": 1
            }
        }
    ]
    
    return list(db.attendance_logs.aggregate(pipeline))
#   GET   /analytics/departments/{department}/trend
@app.get("/analytics/departments/summary")
def department_summary(month: str):
    # Mahine ka aakhiri din nikalna (e.g., 2026-10-31) taaki R9 ke hisaab se headcount count ho
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    end_of_month = f"{month}-{last_day:02d}"

    pipeline = [
        # 1. Employees collection se start karein aur sirf unhe lein jinhone is mahine ya usse pehle join kiya
        {
            "$match": {
                "joined_on": {"$lte": end_of_month}
            }
        },
        # 2. attendance_logs collection ke sath left outer join karein
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}"}}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        # 3. Array ko unwind karein, par empty logs ko bhi rakhein taaki headcount mein ginti ho sake
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        # 4. Department ke hisaab se group karein
        {
            "$group": {
                "_id": "$department",
                "unique_emps": {"$addToSet": "$emp_code"},
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "total_work_hours": {"$sum": "$logs.work_hours"},
                # Sirf unhi records ko count karein jahan actual attendance log exist karta hai
                "log_count": {
                    "$sum": {"$cond": [{"$ifNull": ["$logs.date", False]}, 1, 0]}
                }
            }
        },
        # 5. Averages calculate karein aur R8 ke hisaab se 2 decimals tak round karein
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$unique_emps"},
                "average_late_minutes": {
                    "$cond": [
                        {"$eq": ["$log_count", 0]},
                        0,
                        {"$round": [{"$divide": ["$total_late_minutes", "$log_count"]}, 2]}
                    ]
                },
                "average_work_hours": {
                    "$cond": [
                        {"$eq": ["$log_count", 0]},
                        0,
                        {"$round": [{"$divide": ["$total_work_hours", "$log_count"]}, 2]}
                    ]
                }
            }
        },
        # Alphabetical order mein sort karein
        {
            "$sort": {"department": 1}
        }
    ]
    
    return list(db.employees.aggregate(pipeline))
@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str, date_to: str):
    # Retrieve all employees in the department to filter attendance logs efficiently
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "_id": 0}))
    emp_codes = [e["emp_code"] for e in emps]
    
    # Define bounding dates for $densify (upper bound is exclusive)
    start_date = datetime.strptime(date_from, "%Y-%m-%d")
    end_date = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
    
    pipeline = [
        # 1. Match records for these employees within the date range
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": date_from, "$lte": date_to}
            }
        },
        # 2. Group by date to calculate daily averages
        {
            "$group": {
                "_id": {"$dateFromString": {"dateString": "$date"}},
                "total_hours": {"$sum": "$work_hours"},
                "count": {
                    "$sum": {"$cond": [{"$ne": ["$work_hours", None]}, 1, 0]}
                }
            }
        },
        # 3. Fill in missing calendar days (weekends, zero-log days)
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_date, end_date]
                }
            }
        },
        # 4. Calculate daily average (0 if no logs exist for that day)
        {
            "$project": {
                "date": "$_id",
                "daily_avg": {
                    "$cond": [
                        {"$and": [{"$isNumber": "$count"}, {"$gt": ["$count", 0]}]},
                        {"$divide": ["$total_hours", "$count"]},
                        0
                    ]
                }
            }
        },
        # 5. Calculate 7-day moving average using window functions
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_average": {
                        "$avg": "$daily_avg",
                        # Current document and 6 preceding documents (7 days total)
                        "window": {"documents": [-6, 0]} 
                    }
                }
            }
        },
        # 6. Format dates back to string and round output to 2 decimals
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "moving_average": {"$round": ["$moving_average", 2]}
            }
        },
        # 7. Ensure chronological order
        {
            "$sort": {"date": 1}
        }
    ]
    
    return list(db.attendance_logs.aggregate(pipeline))
#   GET   /admin/explain/{endpoint}


@app.get("/admin/explain/{endpoint}")
def explain_query(endpoint: str, request: Request):
    params = dict(request.query_params)
    
    if endpoint == "attendance_list":
        q = {}
        if "emp_code" in params:
            q["emp_code"] = params["emp_code"]
        if "date_from" in params or "date_to" in params:
            q["date"] = {}
            if "date_from" in params:
                q["date"]["$gte"] = params["date_from"]
            if "date_to" in params:
                q["date"]["$lte"] = params["date_to"]
        if "status" in params:
            q["status"] = params["status"]
            
        # MongoDB ka explain command find query par 
        return db.command("explain", {"find": "attendance_logs", "filter": q}, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        date_from = params.get("date_from", "")
        date_to = params.get("date_to", "")
        
        pipeline = [
            {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            # Baaki logic skip kar sakte hain kyunki main indexing $match par lagti hai
        ]
        
        # MongoDB ka explain command aggregate pipeline par
        return db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        
    else:
        raise HTTPException(400, "Explain not implemented for this endpoint in starter kit")