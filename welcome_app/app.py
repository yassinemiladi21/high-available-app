from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import socket
from werkzeug.utils import secure_filename
import uuid
import time
from datetime import datetime

hostname = socket.gethostname()
app = Flask(__name__)
CORS(app)

# -------- Configuration --------
UPLOAD_FOLDER = '/mnt/shared/images'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# DB nodes (primary first is preferred for writes)
DB_CONFIGS = [
    {
        'dbname': 'welcome_app',
        'user': 'postgres',
        'password': 'postgres',
        'host': '192.168.104.31',  # Primary (preferred for writes)
        'port': 5432
    },
    {
        'dbname': 'welcome_app',
        'user': 'postgres',
        'password': 'postgres',
        'host': '192.168.104.32',  # Replica (read-only)
        'port': 5432
    }
]

# runtime state
current_db_index = 0
CONNECT_TIMEOUT = 3  # seconds
FAILOVER_RETRY_DELAY = 0.5  # seconds between attempts
MAX_FAILOVER_ATTEMPTS = 3

# -------- Utilities --------
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# -------- DB connection / failover logic --------
def _connect_to_node(index):
    """
    Try to connect to DB_CONFIGS[index]. Returns a psycopg2 connection or raises.
    """
    cfg = DB_CONFIGS[index].copy()
    cfg['connect_timeout'] = CONNECT_TIMEOUT
    # optional: set application_name to help logs
    cfg['application_name'] = 'welcome_app_flask'
    return psycopg2.connect(**cfg)

def is_read_only(conn):
    """
    Returns True if the connected server is a standby/replica (in recovery).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_is_in_recovery();")
            res = cur.fetchone()
            if res:
                return bool(res[0])
    except Exception:
        # Any error -- treat as unknown/unsafe to write
        return True
    return False

def get_db(readonly=False):
    """
    Get a fresh DB connection.

    - If readonly=True: allow connecting to any node (primary or replica).
    - If readonly=False: ensure we connect to a writable node (pg_is_in_recovery() == false).
    On failure, rotates current_db_index and tries other nodes (with small retry/backoff).
    """
    global current_db_index

    # try up to len(DB_CONFIGS) nodes, starting from current_db_index
    nodes_count = len(DB_CONFIGS)
    start_index = current_db_index

    for attempt in range(nodes_count):
        idx = (start_index + attempt) % nodes_count
        cfg = DB_CONFIGS[idx]
        try:
            conn = _connect_to_node(idx)
        except Exception as e:
            log(f"✗ Cannot connect to DB{idx+1} ({cfg['host']}): {e}")
            continue

        # If caller needs writable, check server state
        if not readonly:
            try:
                if is_read_only(conn):
                    log(f"✗ Connected to DB{idx+1} ({cfg['host']}) but it's read-only — skipping")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
            except Exception as e:
                log(f"✗ Error checking read-only on DB{idx+1} ({cfg['host']}): {e}")
                try:
                    conn.close()
                except Exception:
                    pass
                continue

        # success: update current_db_index to this node so subsequent calls prefer it
        current_db_index = idx
        log(f"✓ Connected to DB{idx+1} ({cfg['host']}) {'(read-only allowed)' if readonly else '(writable)'}")
        return conn

    # If we reach here, no suitable node found
    raise Exception("All database connection attempts failed or no writable node available")

# -------- DB initialization --------
def init_db():
    try:
        # Use writable connection to create table
        conn = get_db(readonly=False)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS content (
                            id SERIAL PRIMARY KEY,
                            quote TEXT NOT NULL,
                            image_filename TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
            log("✓ Database initialized (table ensured)")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        log(f"✗ Database initialization failed: {e}")

# -------- Upload folder handling --------
def ensure_upload_folder():
    if not os.path.exists(UPLOAD_FOLDER):
        try:
            os.makedirs(UPLOAD_FOLDER, mode=0o755, exist_ok=True)
            log(f"✓ Created upload folder: {UPLOAD_FOLDER}")
        except Exception as e:
            log(f"✗ Failed to create upload folder: {e}")
            # Fallback to local folder if NFS not mounted
            local = './uploads'
            app.config['UPLOAD_FOLDER'] = local
            os.makedirs(local, exist_ok=True)
            log(f"✓ Using local upload folder: {local}")

# -------- Flask routes --------
@app.route('/')
def welcome():
    return render_template('welcome.html', hostname=hostname)

@app.route('/admin')
def admin():
    return render_template('admin.html', hostname=hostname)

@app.route('/images/<filename>')
def serve_image(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/content', methods=['GET'])
def get_content():
    conn = None
    try:
        # For reads we allow replica
        conn = get_db(readonly=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT id, quote, image_filename, created_at FROM content ORDER BY created_at DESC')
            rows = cur.fetchall()
        # Close connection
        conn.close()
        # Add image URL and return
        for r in rows:
            r['image_url'] = f"/images/{r['image_filename']}"
        return jsonify(rows)
    except Exception as e:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return jsonify({'error': str(e)}), 500

@app.route('/api/content', methods=['POST'])
def add_content():
    conn = None
    try:
        # Basic validations
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400

        file = request.files['image']
        quote = request.form.get('quote')

        if not quote:
            return jsonify({'error': 'Quote is required'}), 400

        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if not allowed_file(file.filename):
            return jsonify({'error': 'Invalid file type. Only images allowed'}), 400

        # Save file
        ext = file.filename.rsplit('.', 1)[1].lower()
        unique_filename = f"{uuid.uuid4()}.{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(unique_filename))
        file.save(filepath)

        # Insert into DB (requires writable node)
        conn = get_db(readonly=False)

        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'INSERT INTO content (quote, image_filename) VALUES (%s, %s) RETURNING id',
                        (quote, unique_filename)
                    )
                    new_id = cur.fetchone()[0]
            conn.close()
        except Exception as db_exc:
            # If insert failed, delete saved file to avoid orphan
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            # Close conn if open
            try:
                conn.close()
            except Exception:
                pass
            raise db_exc

        return jsonify({
            'id': new_id,
            'message': 'Content added successfully',
            'image_url': f"/images/{unique_filename}"
        }), 201

    except Exception as e:
        # Return any error
        return jsonify({'error': str(e)}), 500

@app.route('/api/content/<int:item_id>', methods=['DELETE'])
def delete_content(item_id):
    conn = None
    try:
        # Need writable DB to delete row
        conn = get_db(readonly=False)
        filename = None
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT image_filename FROM content WHERE id = %s', (item_id,))
                    res = cur.fetchone()
                    if res:
                        filename = res[0]
                        cur.execute('DELETE FROM content WHERE id = %s', (item_id,))
            conn.close()
        except Exception as inner_e:
            try:
                conn.close()
            except Exception:
                pass
            raise inner_e

        # Delete file if exists
        if filename:
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception as e:
                    log(f"Warning: failed to remove file {filepath}: {e}")

        return jsonify({'message': 'Content deleted successfully'})

    except Exception as e:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return jsonify({'error': str(e)}), 500

@app.route('/api/time')
def get_time():
    now = datetime.now()
    return jsonify({
        'time': now.strftime('%H:%M:%S'),
        'date': now.strftime('%B %d, %Y'),
        'greeting': get_greeting(now.hour)
    })

@app.route('/api/health')
def health_check():
    status = {
        'status': 'unknown',
        'hostname': hostname,
        'database_index': None,
        'db_host': None,
        'content_count': None
    }

    conn = None
    try:
        # We try readonly True to avoid forcing failover for this check
        conn = get_db(readonly=True)
        status['database_index'] = current_db_index + 1
        status['db_host'] = DB_CONFIGS[current_db_index]['host']

        with conn.cursor() as cur:
            # Use a simple read
            cur.execute('SELECT COUNT(*) FROM content')
            cnt = cur.fetchone()[0]
            status['content_count'] = cnt
        status['status'] = 'healthy'
        conn.close()
    except Exception as e:
        status['status'] = 'degraded'
        status['error'] = str(e)
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return jsonify(status)

def get_greeting(hour):
    if 5 <= hour < 12:
        return 'Good Morning'
    elif 12 <= hour < 17:
        return 'Good Afternoon'
    elif 17 <= hour < 21:
        return 'Good Evening'
    else:
        return 'Good Night'

# -------- App startup --------
if __name__ == '__main__':
    print("=" * 60)
    print(f"Starting Flask App on {hostname}")
    print("=" * 60)

    ensure_upload_folder()

    # Try to initialize DB (tries to use a writable node)
    init_db()

    print(f"✓ App ready on http://0.0.0.0:5000")
    print("=" * 60)
    # NOTE: For production use a real WSGI server; debug True is for dev only
    app.run(host='0.0.0.0', port=5000, debug=True)
