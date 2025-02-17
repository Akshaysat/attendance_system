from flask import Flask, request, render_template, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
from sqlalchemy import func


app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkey'  # Required for session management

# MySQL Database Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://straddle_user:straddle_user%40jktvs@localhost/attendance_system'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# User model
class USERS(db.Model):
    employee_id = db.Column(db.String(20), primary_key=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)

# Attendance model
class ATTENDANCE(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(20), db.ForeignKey('users.employee_id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    check_in_time = db.Column(db.DateTime, nullable=False)
    check_out_time = db.Column(db.DateTime, nullable=True)

# Initialize database
with app.app_context():
    db.create_all()

# Routes
@app.route('/', methods=['GET', 'POST'])
def home():
    if not session.get('logged_in'):
        # Render the default homepage for non-logged-in users
        return render_template('index.html')

    # Get user details
    employee_id = session['employee_id']
    today = date.today()

    # Fetch today's attendance record for the user
    attendance = ATTENDANCE.query.filter_by(employee_id=employee_id, date=today).first()

    # Check if the user has checked in today
    has_checked_in = attendance is not None
    # Check if the user has already checked out today
    has_checked_out = attendance.check_out_time is not None if attendance else False

    checkin_time = attendance.check_in_time.strftime("%H:%M:%S") if attendance and attendance.check_in_time else None
    checkout_time = attendance.check_out_time.strftime("%H:%M:%S") if attendance and attendance.check_out_time else None

    # Handle check-in and check-out requests
    if request.method == 'POST':
        if request.form['action'] == 'checkin' and not has_checked_in:
            # Handle Check-In
            now = datetime.now()
            new_attendance = ATTENDANCE(
                employee_id=employee_id,
                date=today,
                check_in_time=now,
                check_out_time=None
            )
            db.session.add(new_attendance)
            db.session.commit()
            flash(f'Check-in successful at {now.strftime("%H:%M:%S")}!', 'success')
            has_checked_in = True
            checkin_time = now.strftime("%H:%M:%S")

        elif request.form['action'] == 'checkout' and has_checked_in:
            # Handle Check-Out
            now = datetime.now()
            attendance.check_out_time = now
            db.session.commit()
            flash(f'Check-out updated at {now.strftime("%H:%M:%S")}!', 'success')
            checkout_time = now.strftime("%H:%M:%S")

    return render_template(
        'index.html',
        logged_in=True,
        first_name=session['first_name'],
        has_checked_in=has_checked_in,
        checkin_time=checkin_time,
        checkout_time=checkout_time
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        employee_id = request.form['employee_id']
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        username = request.form['username']
        password = request.form['password']
        admin_password = request.form['admin_password']  # Get admin password from the form

        # Check if the provided admin password is correct
        if admin_password != "Learnapp@1234":
            flash('Invalid Admin Password. Please try again.', 'error')
            return redirect(url_for('register'))

        # Check if user already exists
        if USERS.query.filter_by(username=username).first():
            flash('Username already exists. Please choose another.', 'error')
            return redirect(url_for('register'))

        if USERS.query.filter_by(employee_id=employee_id).first():
            flash('Employee ID already exists. Please use another.', 'error')
            return redirect(url_for('register'))

        # Add new user
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = USERS(
            employee_id=employee_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
            password=hashed_password
        )
        db.session.add(new_user)
        db.session.commit()
        flash('Registration successful. Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = USERS.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password, password):
            flash('Invalid credentials. Please try again.', 'error')
            return redirect(url_for('login'))

        # Set session variables to maintain login state
        session['logged_in'] = True
        session['employee_id'] = user.employee_id
        session['username'] = user.username
        session['first_name'] = user.first_name
        session['last_name'] = user.last_name

        return redirect(url_for('home'))

    return render_template('login.html')


@app.route('/admin', methods=['GET'])
def admin():

    # Fetch all attendance records for today with user details
    today = date.today()
    records = db.session.query(
        ATTENDANCE.employee_id,
        ATTENDANCE.check_in_time,
        ATTENDANCE.check_out_time,
        USERS.first_name,
        USERS.last_name
    ).join(USERS, ATTENDANCE.employee_id == USERS.employee_id).filter(ATTENDANCE.date == today).all()

    # Process records to add "Late" status and hours worked
    processed_records = []
    for record in records:
        # Check for late arrival
        late_status = "On Time"
        if record.check_in_time and record.check_in_time.time() > datetime.strptime("10:00:00", "%H:%M:%S").time():
            late_status = "Late"

        # Calculate hours worked
        hours_worked = None
        if record.check_in_time and record.check_out_time:
            worked_duration = record.check_out_time - record.check_in_time
            hours_worked = round(worked_duration.total_seconds() / 3600, 2)  # Convert seconds to hours

        # Append processed data
        processed_records.append({
            "employee_id": record.employee_id,
            "first_name": record.first_name,
            "last_name": record.last_name,
            "check_in_time": record.check_in_time,
            "check_out_time": record.check_out_time,
            "late_status": late_status,
            "hours_worked": hours_worked
        })

    # Pass the processed records to the template
    return render_template('admin.html', records=processed_records, today=today)

@app.route('/logout')
def logout():
    session.clear()  # Clear all session variables
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5002)
    #app.run(debug=True, port=5002)