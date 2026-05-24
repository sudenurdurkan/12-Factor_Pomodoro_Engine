import os
import time
import math
from flask import Flask, jsonify, request, Response, render_template_string
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import redis
from werkzeug.security import generate_password_hash, check_password_hash
from prometheus_client import generate_latest, Counter, CONTENT_TYPE_LATEST

load_dotenv()

app = Flask(__name__)

# PostgreSQL Settings
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Redis Settings
redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'localhost'),
    port=int(os.getenv('REDIS_PORT', 6379)),
    password = os.getenv('REDIS_PASSWORD', None),
    decode_responses=True
)

# Prometheus Metrics
HTTP_REQUESTS_TOTAL = Counter('http_requests_total', 'Total HTTP Requests', ['method', 'endpoint'])
COMPLETED_POMODOROS_TOTAL = Counter('completed_pomodoros_total', 'Total successfully completed pomodoros')

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

class PomodoroSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False, default=25) # Duration of a session
    completed_at = db.Column(db.Float, nullable=False)

@app.route('/')
def home():
    HTTP_REQUESTS_TOTAL.labels(method='GET', endpoint='/').inc()
    try:
        static_dir = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(static_dir, 'static', 'index.html'), 'r', encoding='utf-8') as f:
            html_content = f.read()
        return render_template_string(html_content)
    except FileNotFoundError:
        return "Error: 'static/index.html' not found!", 404

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({"status": "error", "message": "Missing info!"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"status": "error", "message": "This username was already taken!"}), 400
    
    new_user = User(username=username, password_hash=generate_password_hash(password))
    db.session.add(new_user)
    db.session.commit()
    return jsonify({"status": "success"})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    user = User.query.filter_by(username=username).first()
    if user and check_password_hash(user.password_hash, password):
        return jsonify({"status": "success", "username": user.username})
    return jsonify({"status": "error", "message": "Wrong username or password!"}), 401


# --- POMODORO ENGINE ---

@app.route('/start', methods=['POST'])
def start_pomodoro():
    HTTP_REQUESTS_TOTAL.labels(method='POST', endpoint='/start').inc()
    try:
        user_id = request.json.get('user_id', 'default_user')
        # User selects the focus duration (default 25 dk)
        duration_minutes = int(request.json.get('duration_minutes', 25))
        
        if duration_minutes < 1:
            return jsonify({"status": "error", "message": "Invalid focus time entered!"}), 400
        
        end_time = time.time() + (duration_minutes * 60)
        
        redis_client.set(f"pomodoro:{user_id}", end_time)
        redis_client.set(f"pomodoro_duration:{user_id}", duration_minutes) # Save the selected time to Redis temporarily
        
        return jsonify({"status": "started", "user_id": user_id})
    except redis.ConnectionError:
        return jsonify({"status": "error", "message": "Redis disconnected!"}), 500
    
# Stopping Pomodoro and Calculating Time Focused
@app.route('/stop', methods=['POST'])
def stop_pomodoro():
    HTTP_REQUESTS_TOTAL.labels(method='POST', endpoint='/stop').inc()
    try:
        user_id = request.json.get('user_id', 'default_user')
        
        end_time_raw = redis_client.get(f"pomodoro:{user_id}")
        duration_raw = redis_client.get(f"pomodoro_duration:{user_id}")
        
        if not end_time_raw or not duration_raw:
            return jsonify({"status": "error", "message": "No active session found!"}), 400
            
        end_time = float(end_time_raw)
        total_duration_minutes = int(duration_raw)
        
        # Pomodoro Time - Remaining Time = Time Focused
        remaining_seconds = end_time - time.time()
        elapsed_seconds = (total_duration_minutes * 60) - remaining_seconds
        
        elapsed_minutes = math.ceil(elapsed_seconds / 60) # Rounding up to a whole minute to save (if focus time was less than a minute it's saved as 1 minute)

        # Focus time cannot be negative or surpass the started session time
        if elapsed_minutes > total_duration_minutes:
            elapsed_minutes = total_duration_minutes
        if elapsed_minutes < 1:
            elapsed_minutes = 1
        
        # Cleaning
        redis_client.delete(f"pomodoro:{user_id}")
        redis_client.delete(f"pomodoro_duration:{user_id}")
        
        # If Pomodoro was focused for at least 1 minute, save it to Postgres
        new_session = PomodoroSession(user_id=user_id, duration_minutes=elapsed_minutes, completed_at=time.time())
        db.session.add(new_session)
        db.session.commit()
        return jsonify({"status": "stopped", "saved": True, "elapsed_minutes": elapsed_minutes})
        
    except redis.ConnectionError:
        return jsonify({"status": "error", "message": "Redis disconnected!"}), 500

@app.route('/status', methods=['GET'])
def get_status():
    HTTP_REQUESTS_TOTAL.labels(method='GET', endpoint='/status').inc()
    user_id = request.args.get('user_id', 'default_user')
    end_time_raw = redis_client.get(f"pomodoro:{user_id}")
    
    if not end_time_raw:
        return jsonify({"status": "idle", "remaining_seconds": 0})
    
    remaining = float(end_time_raw) - time.time()
    
    if remaining <= 0:
        redis_client.delete(f"pomodoro:{user_id}")
        # Read from Redis how long the session was started to be
        duration_minutes = int(redis_client.get(f"pomodoro_duration:{user_id}") or 25)
        redis_client.delete(f"pomodoro_duration:{user_id}")
        
        # Save to PostgreSQL how much time was focused
        new_session = PomodoroSession(user_id=user_id, duration_minutes=duration_minutes, completed_at=time.time())
        db.session.add(new_session)
        db.session.commit()
        
        COMPLETED_POMODOROS_TOTAL.inc()
        return jsonify({"status": "completed_and_saved_to_db", "remaining_seconds": 0})
        
    return jsonify({"status": "active", "user_id": user_id, "remaining_seconds": int(remaining)})

# Detailed Day/Minutes Focused Statistics EndPoint
@app.route('/stats', methods=['GET'])
def get_detailed_stats():
    user_id = request.args.get('user_id', 'default_user')
    sessions = PomodoroSession.query.filter_by(user_id=user_id).all()
    
    # Aggregating daily focus data
    daily_aggregation = {}
    for session in sessions:
        # Date Format: YYYY-MM-DD
        date_str = time.strftime('%Y-%m-%d', time.localtime(session.completed_at))
        daily_aggregation[date_str] = daily_aggregation.get(date_str, 0) + session.duration_minutes
        
    # Formatting stats to a sorted array suitable for graphing
    formatted_stats = [{"date": k, "total_minutes": v} for k, v in sorted(daily_aggregation.items())]
    return jsonify(formatted_stats)

@app.route('/history', methods=['GET'])
def get_history():
    user_id = request.args.get('user_id', 'default_user')
    sessions = PomodoroSession.query.filter_by(user_id=user_id).all()
    return jsonify({
        "user_id": user_id,
        "total_completed_pomodoros": len(sessions)
    })

@app.route('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.getenv('FLASK_PORT', 5000))
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() in ['true', '1']
    app.run(host='0.0.0.0', port=port, debug=debug_mode)