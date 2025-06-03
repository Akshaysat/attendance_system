from flask import Flask, request, render_template, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import gspread
from google.oauth2.service_account import Credentials
from threading import Thread
from datetime import datetime, date



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
# Worksheets for users and attendance
users_sheet = spreadsheet.worksheet("Users")
attendance_sheet = spreadsheet.worksheet("Attendance")

# --- Helper Functions ---

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
    tracker_sheet = spreadsheet.worksheet("TRACKER (NEW)")
    all_tasks = tracker_sheet.get_all_records()
    today_date = date.today()
    pending = []
    
    for task in all_tasks:
        # Skip if task is already marked as Completed (case-insensitive)
        status = task.get("Video Status", "").strip().lower()

        # Skip tasks that are either Completed or in Client Review
        if status in ("completed", "client review"):
            continue
        
        # Check if user is involved (either Video Editor or Storyboarder)
        video_editor = task.get("Video Editor", "").strip()
        storyboarder = task.get("Storyboarder", "").strip()
        graphic_designer = task.get("Graphic Designer", "").strip()

        if user_full_name not in (video_editor, storyboarder, graphic_designer):
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
    tracker_sheet = spreadsheet.worksheet("TRACKER (NEW)")
    all_tasks = tracker_sheet.get_all_records()
    today = date.today()

    # Filter out completed tasks
    filtered_tasks = []
    for task in all_tasks:
        status = task.get("Video Status", "").strip().lower()
        if status == "completed":
            continue
        
        assigned_to = [
            task.get("Video Editor", "").strip(),
            task.get("Storyboarder", "").strip(),
            task.get("Graphic Designer", "").strip()
        ]
        
        if member_name and member_name not in assigned_to:
            continue
        
        filtered_tasks.append(task)
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

    attendance_data = get_all_attendance()
    users_data = get_all_users()

    # --- Attendance Records ---
    processed_records = []
    users_dict = {user['employee_id']: user for user in users_data}

    for record in attendance_data:
        if record.get('date') != today_str:
            continue

        emp_id = record.get('employee_id')
        user = users_dict.get(emp_id, {})

        check_in_str = record.get('check_in_time')
        check_out_str = record.get('check_out_time')
        check_in_obj = None
        check_out_obj = None
        late_status = "On Time"
        hours_worked = None

        if check_in_str:
            check_in_obj = datetime.strptime(check_in_str, "%Y-%m-%d %H:%M:%S")
            if check_in_obj.time() > datetime.strptime("10:00:00", "%H:%M:%S").time():
                late_status = "Late"

        if check_in_obj and check_out_str:
            check_out_obj = datetime.strptime(check_out_str, "%Y-%m-%d %H:%M:%S")
            worked_duration = check_out_obj - check_in_obj
            hours_worked = round(worked_duration.total_seconds() / 3600, 2)

        processed_records.append({
            "employee_id": emp_id,
            "first_name": user.get('first_name', ''),
            "last_name": user.get('last_name', ''),
            "check_in_time": check_in_obj,
            "check_out_time": check_out_obj,
            "late_status": late_status,
            "hours_worked": hours_worked
        })

    # --- Team Work Tracker ---
    tracker_sheet = spreadsheet.worksheet("TRACKER (NEW)")
    all_tasks = tracker_sheet.get_all_records()

    # Build unique member name list
    member_names = set()
    for task in all_tasks:
        for role in ["Video Editor", "Storyboarder", "Graphic Designer"]:
            name = task.get(role, "").strip()
            if name:
                member_names.add(name)
    member_names = list(member_names)

    # Filter by member
    selected_member = request.args.get("member", "").strip()
    filtered_tasks = []
    for task in all_tasks:
        if task.get("Video Status", "").strip().lower() == "completed":
            continue
        if selected_member:
            if selected_member not in (
                task.get("Video Editor", ""),
                task.get("Storyboarder", ""),
                task.get("Graphic Designer", "")
            ):
                continue
        filtered_tasks.append(task)

    return render_template(
        "admin.html",
        records=processed_records,
        today=today_str,
        all_members=sorted(member_names),
        selected_member=selected_member,
        team_tasks=filtered_tasks
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