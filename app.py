from flask import Flask, request, render_template, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import gspread
from google.oauth2.service_account import Credentials
from threading import Thread
from datetime import datetime, date, timedelta
from calendar import monthrange


app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkey'  # Required for session management
# --- Google Sheets Setup ---
# Define the scope and load your service account credentials
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
client = gspread.authorize(creds)

# Open your spreadsheet by name (or by key)
spreadsheet = client.open("Team Work Tracker - Money Mediia")
spreadsheet_tasks = client.open("Core Tracker - Money Mediia")
# Worksheets for users and attendance
users_sheet = spreadsheet.worksheet("Users")
attendance_sheet = spreadsheet.worksheet("Attendance")

# --- Helper Functions ---

def _parse_date_str(s: str):
    """Parse to date() from common formats; returns None if not parseable."""
    if not s:
        return None
    s = str(s).strip()
    # Remove trailing '(Mon)' etc if ever present
    if "(" in s and s.endswith(")"):
        s = s[:s.rfind("(")].strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None

def _month_working_dates(year: int, month: int, mon_sat=True, holidays=None, cap_today=True):
    wd = set()
    days = monthrange(year, month)[1]
    today = date.today()
    allowed = {0,1,2,3,4,5} if mon_sat else {0,1,2,3,4}  # Mon=0
    holidays = holidays or set()
    for d in range(1, days + 1):
        dt = date(year, month, d)
        if cap_today and dt > today and year == today.year and month == today.month:
            break
        if dt.weekday() in allowed and dt not in holidays:
            wd.add(dt)
    return wd

def compute_monthly_attendance_summary(
    attendance_rows,
    users_rows,
    year: int,
    month: int,
    *,
    mon_sat=True,                 # set False for Mon–Fri
    holidays_dates=None,          # list of 'YYYY-MM-DD' or date()
    leaves_rows=None,             # pass Leaves if you want approved leave to reduce absences
    count_approved_leave_as_present=True
):
    # Holidays -> date() set
    holidays = set()
    if holidays_dates:
        for h in holidays_dates:
            if isinstance(h, date):
                holidays.add(h)
            else:
                d = _parse_date_str(h)
                if d:
                    holidays.add(d)

    working_days = _month_working_dates(year, month, mon_sat=mon_sat, holidays=holidays, cap_today=True)

    # Users map + (optional) joining_date
    users_by_id, join_date_by_id = {}, {}
    for u in users_rows:
        eid = str(u.get("employee_id", "")).strip()
        if not eid:
            continue
        users_by_id[eid] = u
        jd = _parse_date_str(u.get("joining_date") or u.get("Joining Date") or "")
        if jd:
            join_date_by_id[eid] = jd

    month_start = date(year, month, 1)
    month_end   = date(year, month, monthrange(year, month)[1])

    # Present dates & durations
    present = {eid: set() for eid in users_by_id}
    durations = {eid: [] for eid in users_by_id}

    for r in attendance_rows:
        eid = str(r.get("employee_id", "")).strip()
        if eid not in users_by_id:
            continue
        d = _parse_date_str(r.get("date"))
        if not d or not (month_start <= d <= month_end):
            continue

        ci = r.get("check_in_time")
        co = r.get("check_out_time")
        if ci:
            present[eid].add(d)
        try:
            if ci and co:
                ci_dt = datetime.strptime(ci.strip(), "%Y-%m-%d %H:%M:%S")
                co_dt = datetime.strptime(co.strip(), "%Y-%m-%d %H:%M:%S")
                if co_dt >= ci_dt:
                    durations[eid].append(co_dt - ci_dt)
        except Exception:
            pass

    # Approved leave days (optional)
    leave_days = {eid: set() for eid in users_by_id}
    if leaves_rows and count_approved_leave_as_present:
        for r in leaves_rows:
            if str(r.get("status", "")).strip().lower() != "approved":
                continue
            eid = str(r.get("employee_id", "")).strip()
            if eid not in users_by_id:
                continue
            ld = _parse_date_str(r.get("leave_date"))
            if not ld or not (month_start <= ld <= month_end):
                continue
            if ld in working_days:
                leave_days[eid].add(ld)

    # Build summary
    rows = []
    for eid, u in users_by_id.items():
        # Filter working days by join date
        jd = join_date_by_id.get(eid)
        eff_working = {d for d in working_days if (jd is None or d >= jd)}

        present_days   = len(present[eid] & eff_working)
        approved_leave = len(leave_days[eid] & eff_working) if count_approved_leave_as_present else 0
        total_working  = len(eff_working)
        absent_days    = max(total_working - present_days - approved_leave, 0)

        if durations[eid]:
            avg_secs  = sum(td.total_seconds() for td in durations[eid]) / len(durations[eid])
            avg_hours = round(avg_secs / 3600.0, 2)
        else:
            avg_hours = 0.0

        rows.append({
            "employee_id":  eid,
            "name":         f"{u.get('first_name','')} {u.get('last_name','')}".strip(),
            "present_days": present_days,
            "leave_days":   approved_leave,   # <<< NEW
            "absent_days":  absent_days,
            "avg_hours":    avg_hours,
        })

    rows.sort(key=lambda x: x["name"].lower())
    return rows

def fy_label_for_date(dt: date) -> str:
    """Return FY label like 'FY25' for a given date (Apr–Mar fiscal)."""
    end_year = dt.year + 1 if dt.month >= 4 else dt.year
    return f"FY{str(end_year)[-2:]}"

def current_fy_label() -> str:
    return fy_label_for_date(date.today())

def fy_options_list(n=3):
    """
    Return list of FY labels (current, previous, ...).
    n=3 -> [current, prev1, prev2]
    """
    today = date.today()
    # Fiscal year end (calendar year) for today
    end_year = today.year + 1 if today.month >= 4 else today.year
    return [f"FY{str(end_year - i)[-2:]}" for i in range(n)]

def compute_leave_summary(leaves_rows, users_rows, fy_label: str):
    """
    Robust per-active-employee leave usage for FY label (e.g. 'FY25').
    - Only counts status == 'approved' (case-insensitive).
    - Half-day detection: if 'half' or '0.5' appears in leave_type, counts 0.5.
    - Recognizes types by keywords: sick/sl, casual/cl, privilege/privileged/pl.
    - Matches employees by trimmed employee_id.
    - FY: uses 'Financial Year' if present; otherwise computes from leave_date.
    - Supports leave_date formats:
        'Month DD, YYYY (ddd)', 'Month DD, YYYY', 'YYYY-MM-DD', 'DD/MM/YYYY', 'DD-MM-YYYY'
    """

    # ---------- helpers ----------
    def norm(s): return str(s or "").strip()
    def lower(s): return norm(s).lower()

    def fy_from_date(ld: str) -> str | None:
        s = norm(ld)
        if not s:
            return None
        # remove trailing "(Wed)" etc if present
        if "(" in s and s.endswith(")"):
            s = s[:s.rfind("(")].strip()
        # try multiple formats
        for fmt in ("%B %d, %Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                dt = datetime.strptime(s, fmt)
                end_year = dt.year + 1 if dt.month >= 4 else dt.year
                return f"FY{str(end_year)[-2:]}"
            except Exception:
                pass
        return None

    def classify_leave(leave_type_raw: str):
        """Return ('sick'|'casual'|'privilege'|None, is_half: bool)."""
        t = lower(leave_type_raw)
        is_half = ("half" in t) or ("0.5" in t)

        base = None
        if "casual" in t or t == "cl":
            base = "casual"
        elif "privilege" in t or "privileged" in t or t == "pl":
            base = "privilege"
        elif "sick" in t or t == "sl":
            base = "sick"
        return base, is_half

    want_fy = norm(fy_label)

    # active users by trimmed id
    users_by_id = {}
    for u in users_rows:
        eid = norm(u.get("employee_id"))
        if eid:
            users_by_id[eid] = u

    # counters
    used = {eid: {"sick": 0.0, "casual": 0.0, "privilege": 0.0} for eid in users_by_id}

    # ---------- iterate leaves ----------
    # (Optional debug toggles)
    DEBUG = False
    dbg_unmatched_fy = []
    dbg_unknown_type = []
    dbg_unknown_emp = []

    for r in leaves_rows:
        if lower(r.get("status")) != "approved":
            continue

        # resolve FY
        fy_cell = norm(r.get("Financial Year"))
        if not fy_cell:
            fy_cell = fy_from_date(r.get("leave_date"))
        if norm(fy_cell) != want_fy:
            if DEBUG: dbg_unmatched_fy.append(r)
            continue

        eid = norm(r.get("employee_id"))
        if eid not in users_by_id:
            if DEBUG: dbg_unknown_emp.append(r)
            continue

        base, is_half = classify_leave(r.get("leave_type"))
        if not base:
            # not one of our three tracked types
            if DEBUG: dbg_unknown_type.append(r)
            continue

        inc = 0.5 if is_half else 1.0
        used[eid][base] += inc

    # build rows
    rows = []
    for eid, u in users_by_id.items():
        sick = used[eid]["sick"]
        casual = used[eid]["casual"]
        privilege = used[eid]["privilege"]
        total = round(sick + casual + privilege, 2)
        rows.append({
            "employee_id": eid,
            "name": f"{norm(u.get('first_name'))} {norm(u.get('last_name'))}".strip(),
            "sick": sick,
            "casual": casual,
            "privilege": privilege,
            "total": total,
        })

    rows.sort(key=lambda x: x["name"].lower())

    if DEBUG:
        print("[LEAVES] FY unmatched (skipped):", len(dbg_unmatched_fy))
        print("[LEAVES] Unknown employee_id (skipped):", len(dbg_unknown_emp))
        print("[LEAVES] Unknown type (skipped):", len(dbg_unknown_type))

    return rows


def get_active_users():
    rows = users_sheet.get_all_records()
    active = []
    for r in rows:
        status = str(r.get("Status", "")).strip().lower()
        if status == "active":
            r["employee_id"] = str(r.get("employee_id", "")).strip()
            active.append(r)
    return active

def append_checkin_in_background(employee_id, today_str, now_str):
    new_id = get_next_attendance_id()
    attendance_sheet.append_row([
        new_id,
        employee_id,
        today_str,
        now_str,
        ""  # Empty checkout time
    ])

def update_checkout_in_background(attendance_id, now_str):
    update_attendance_checkout(attendance_id, now_str)

def get_pending_work_for_user(user_full_name):
    """
    Fetches pending tasks from the "TRACKER (NEW)" sheet for the given user.
    A task is pending if its Video Status is not "Completed" and its Start Date is <= today.
    The task is assigned if the user is listed as either Video Editor or Storyboarder.
    """
    tracker_sheet = spreadsheet_tasks.worksheet("Master Work Tracker (Monthly)")
    all_tasks = tracker_sheet.get_all_records()
    today_date = date.today()
    pending = []
    
    for task in all_tasks:
        # Skip if task is already marked as Completed (case-insensitive)
        status = task.get("Delivery Status", "").strip().lower()

        # Skip tasks that are either Completed or in Client Review
        if status in ("Done"):
            continue
        
        # Check if user is involved (either Video Editor or Storyboarder)
        video_editor = task.get("Video Editor", "").strip()
        storyboarder = task.get("Storyboarder", "").strip()

        if user_full_name not in (video_editor, storyboarder):
            continue
        
        # Convert "Start Date" to a date object and check if it is before or equal to today
        start_date_str = task.get("Start Date", "").strip()
        if not start_date_str:
            continue
        # Extract the date portion before any whitespace
        start_date_str = start_date_str.split()[0]
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except Exception as e:
            continue
        
        if start_date <= today_date:
            pending.append(task)

        # ←─ newest work first
        pending.sort(
            key=lambda t: datetime.strptime(t["Start Date"].split()[0], "%Y-%m-%d"),
            reverse=True
        )
    
    return pending

def get_all_team_tasks(member_name=None):

    tracker_sheet = spreadsheet_tasks.worksheet("Master Work Tracker (Monthly)")
    all_tasks = tracker_sheet.get_all_records()
    today = date.today()

    # Filter out completed tasks
    filtered_tasks = []
    for task in all_tasks:
        status = task.get("Delivery Status", "").strip().lower()
        if status == "Done":
            continue
        
        assigned_to = [
            task.get("Video Editor", "").strip(),
            task.get("Storyboarder", "").strip()
        ]
        
        if member_name and member_name not in assigned_to:
            continue
        
        filtered_tasks.append(task)
        print(filtered_tasks)
    return filtered_tasks


def get_all_users():
    """Return a list of user dictionaries from the Users sheet."""
    return users_sheet.get_all_records()

def find_user_by(field, value):
    """Find a user by a given field (e.g., username or employee_id)."""
    for user in get_all_users():
        if user.get(field) == value:
            return user
    return None

def get_all_attendance():
    """Return a list of attendance dictionaries from the Attendance sheet."""
    return attendance_sheet.get_all_records()

def find_attendance(employee_id, date_str):
    """Find today’s attendance record for an employee."""
    for record in get_all_attendance():
        if record.get('employee_id') == employee_id and record.get('date') == date_str:
            return record
    return None

def get_next_attendance_id():
    """Generate a new ID based on the number of records.
       (Assumes the first row is headers.)
    """
    records = attendance_sheet.get_all_values()
    # Subtract one for header row.
    return len(records)

def update_attendance_checkout(attendance_id, new_checkout_time):
    """Find and update the checkout time for a given attendance id."""
    # Get all values with row numbers (1-indexed)
    data = attendance_sheet.get_all_values()
    header = data[0]
    id_index = header.index("id")
    checkout_index = header.index("check_out_time") + 1  # gspread is 1-indexed for columns

    for row_idx, row in enumerate(data[1:], start=2):
        if row[id_index] == str(attendance_id):
            attendance_sheet.update_cell(row_idx, checkout_index, new_checkout_time)
            break

def get_next_leave_id():
    leaves_sheet = spreadsheet.worksheet("Leaves")
    data = leaves_sheet.get_all_values()
    # Assuming the first row is headers; use row count as the next id.
    return len(data)  # e.g. if 5 rows (including headers), new id will be 5

# --- Routes ---

@app.route('/', methods=['GET', 'POST'])
def home():
    if not session.get('logged_in'):
        return render_template('index.html')

    employee_id = session['employee_id']
    today_obj = date.today()
    today_str = today_obj.strftime("%Y-%m-%d")

    attendance = find_attendance(employee_id, today_str)
    has_checked_in = attendance is not None
    # If a checkout time exists (non-empty), assume checked out.
    has_checked_out = attendance and attendance.get('check_out_time') != ''
    checkin_time = attendance.get('check_in_time') if attendance else None
    checkout_time = attendance.get('check_out_time') if attendance else None

    if request.method == 'POST':
# Inside your route handler for checkin:
        if request.form['action'] == 'checkin' and not has_checked_in:
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            Thread(target=append_checkin_in_background, args=(employee_id, today_str, now_str)).start()
            flash(f'Check-in successful at {now.strftime("%H:%M:%S")}!', 'success')
            has_checked_in = True
            checkin_time = now.strftime("%H:%M:%S")

        # In your checkout block:
        elif request.form['action'] == 'checkout' and has_checked_in:
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            
            if has_checked_out:
                prev_checkout_dt = datetime.strptime(checkout_time, "%Y-%m-%d %H:%M:%S")
                if now > prev_checkout_dt:
                    Thread(target=update_checkout_in_background, args=(attendance.get('id'), now_str)).start()
                    flash(f'Check-out updated to a later time: {now_str}', 'success')
                else:
                    flash("New check-out time is not later than the existing one.", "warning")
            else:
                Thread(target=update_checkout_in_background, args=(attendance.get('id'), now_str)).start()
                flash(f'Check-out updated at {now_str}', 'success')
            
            checkout_time = now_str
    
        # Fetch pending work from the "TRACKER (NEW)" sheet for the logged-in user

    user_full_name = f"{session['first_name']} {session['last_name']}"
    pending_work   = get_pending_work_for_user(user_full_name)

    return render_template(
        'index.html',
        logged_in=True,
        first_name=session['first_name'],
        has_checked_in=has_checked_in,
        checkin_time=checkin_time,
        checkout_time=checkout_time,
        pending_work=pending_work  # Pass the pending tasks to the template
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        employee_id = request.form['employee_id']
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        username = request.form['username']
        password = request.form['password']
        admin_password = request.form['admin_password']

        # Check admin password
        if admin_password != "Learnapp@1234":
            flash('Invalid Admin Password. Please try again.', 'error')
            return redirect(url_for('register'))

        # Check if user already exists
        if find_user_by('username', username):
            flash('Username already exists. Please choose another.', 'error')
            return redirect(url_for('register'))
        if find_user_by('employee_id', employee_id):
            flash('Employee ID already exists. Please use another.', 'error')
            return redirect(url_for('register'))

        # Create user with hashed password
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        # Append the new user row to the Users sheet
        users_sheet.append_row([employee_id, first_name, last_name, username, hashed_password])
        flash('Registration successful. Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = find_user_by('username', username)
        if not user or not check_password_hash(user.get('password'), password):
            flash('Invalid credentials. Please try again.', 'error')
            return redirect(url_for('login'))

        # Set session variables
        session['logged_in'] = True
        session['employee_id'] = user.get('employee_id')
        session['username'] = user.get('username')
        session['first_name'] = user.get('first_name')
        session['last_name'] = user.get('last_name')

        return redirect(url_for('home'))

    return render_template('login.html')


@app.route('/admin', methods=['GET'])
def admin():
    today_obj = date.today()
    today_str = today_obj.strftime("%Y-%m-%d")

    # --- Attendance & Users ---
    attendance_data = get_all_attendance()
    users_active = get_active_users()  # Only Active users

    # Map active users by ID
    users_dict = {u.get('employee_id'): u for u in users_active if u.get('employee_id')}

    # --- TODAY's Attendance ---
    processed_records = []
    for record in attendance_data:
        if record.get('date') != today_str:
            continue
        emp_id = record.get('employee_id')
        if emp_id not in users_dict:
            continue
        user = users_dict[emp_id]

        check_in_str = record.get('check_in_time')
        check_out_str = record.get('check_out_time')
        check_in_obj, check_out_obj, late_status, hours_worked = None, None, "On Time", None

        if check_in_str:
            check_in_obj = datetime.strptime(check_in_str, "%Y-%m-%d %H:%M:%S")
            if check_in_obj.time() > datetime.strptime("10:00:00", "%H:%M:%S").time():
                late_status = "Late"

        if check_in_obj and check_out_str:
            check_out_obj = datetime.strptime(check_out_str, "%Y-%m-%d %H:%M:%S")
            if check_out_obj >= check_in_obj:
                worked_duration = check_out_obj - check_in_obj
                hours_worked = round(worked_duration.total_seconds() / 3600, 2)

        processed_records.append({
            "employee_id": emp_id,
            "first_name": user.get('first_name', ''),
            "last_name":  user.get('last_name', ''),
            "check_in_time": check_in_obj,
            "check_out_time": check_out_obj,
            "late_status": late_status,
            "hours_worked": hours_worked
        })

    # --- Team Work Tracker ---
    tracker_sheet = spreadsheet_tasks.worksheet("Master Work Tracker (Monthly)")
    all_tasks = tracker_sheet.get_all_records()

    member_names = set()
    for task in all_tasks:
        for role in ["Video Editor", "Storyboarder"]:
            name = task.get(role, "").strip()
            if name:
                member_names.add(name)
    member_names = list(member_names)

    selected_member = request.args.get("member", "").strip()
    filtered_tasks = []
    for task in all_tasks:
        status = task.get("Delivery Status", "").strip().lower()
        if status == "done":
            continue
        if selected_member:
            if selected_member not in (task.get("Video Editor", ""), task.get("Storyboarder", "")):
                continue
        filtered_tasks.append(task)

    # --- Leaves Sheet (reuse for monthly + FY summaries) ---
    leaves_sheet = spreadsheet.worksheet("Leaves")
    leaves_rows = leaves_sheet.get_all_records()

    # --- Monthly Attendance Summary ---
    month_param = request.args.get("month", "")
    if month_param:
        try:
            year = int(month_param.split("-")[0])
            month = int(month_param.split("-")[1])
        except Exception:
            year, month = today_obj.year, today_obj.month
    else:
        year, month = today_obj.year, today_obj.month

    monthly_summary = compute_monthly_attendance_summary(
        attendance_rows=attendance_data,
        users_rows=users_active,
        year=year,
        month=month,
        leaves_rows=leaves_rows,                   # include approved leaves
        count_approved_leave_as_present=True
    )
    selected_month_value = f"{year:04d}-{month:02d}"

    # --- Leave Summary by Financial Year ---
    fy_param = request.args.get("fy", "").strip()
    selected_fy = fy_param if fy_param else current_fy_label()
    fy_options = fy_options_list(3)  # current + 2 previous

    leave_summary = compute_leave_summary(
        leaves_rows=leaves_rows,
        users_rows=users_active,
        fy_label=selected_fy
    )

    return render_template(
        "admin.html",
        records=processed_records,
        today=today_str,
        all_members=sorted(member_names),
        selected_member=selected_member,
        team_tasks=filtered_tasks,
        monthly_summary=monthly_summary,
        selected_month=selected_month_value,
        leave_summary=leave_summary,
        fy_options=fy_options,
        selected_fy=selected_fy
    )


@app.route('/leaves', methods=['GET', 'POST'])
def leaves():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
        
    employee_id = session['employee_id']
    full_name = session.get('first_name') + " " + session.get('last_name')
    leaves_sheet = spreadsheet.worksheet("Leaves")
    
    if request.method == 'POST':
        leave_date = request.form.get("leave_date")  # Expected format: YYYY-MM-DD
        leave_type = request.form.get("leave_type")
        reason = request.form.get("reason")
        status = "Pending"
        application_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        leave_id = get_next_leave_id()  # Ensure get_next_leave_id() returns a proper serializable id
        
        # Append a new row with the leave type included
        leaves_sheet.append_row([
            leave_id,
            employee_id,
            full_name,
            leave_date,
            leave_type,
            reason,
            status,
            application_date
        ])
        flash("Leave application submitted successfully!", "success")
        return redirect(url_for('leaves'))
    
    # For GET, fetch all leave records for the logged-in user
    all_leaves = leaves_sheet.get_all_records()
    user_leaves = [leave for leave in all_leaves if leave.get("employee_id") == employee_id]
    
    # Total leave allocations
    total_leaves = {
        "Sick Leave": 6,
        "Casual Leave": 8,
        "Privilege Leave": 12
    }
    
    used_leaves = {
        "Sick Leave": 0,
        "Casual Leave": 0,
        "Privilege Leave": 0
    }

    for leave in user_leaves:
        lt = leave.get("leave_type")
        if leave.get("status") == "Approved":
            if lt == "Half Casual Leave":
                used_leaves["Casual Leave"] += 0.5
            elif lt in used_leaves:
                used_leaves[lt] += 1

    remaining_leaves = { lt: total_leaves[lt] - used_leaves[lt] for lt in total_leaves }
    
    return render_template("leaves.html", 
                           leaves=user_leaves, 
                           total_leaves=total_leaves, 
                           used_leaves=used_leaves, 
                           remaining_leaves=remaining_leaves)


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

if __name__ == '__main__':
    #app.run(debug=True, host='127.0.0.1', port=5002)
    app.run(debug=False, host='0.0.0.0', port=8000) # for production