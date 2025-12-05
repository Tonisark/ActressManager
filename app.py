"""
Flask Actress Manager - Full project with safe migration and flexible CSV import.

Usage:
- Place this file in your project root.
- Put templates/ and static/ alongside it (or copy contents from below).
- Install requirements and run: python app.py
- Set MEDIA_ROOT env var if you keep media on another drive.
"""

from flask import Flask, render_template, request, redirect, url_for, send_from_directory, flash, Response, jsonify
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from wtforms import StringField, IntegerField, SelectField, TextAreaField, BooleanField, validators
import sqlite3, os, csv, io, shutil, json, zipfile, tempfile
from werkzeug.utils import secure_filename
from datetime import datetime
from fuzzywuzzy import fuzz  # pip install fuzzywuzzy python-levenshtein
import requests
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Image, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from collections import Counter
from datetime import datetime
# Optional: Try to import APScheduler for automated backups
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    print("APScheduler not installed. Automated backups disabled. Install with: pip install apscheduler")
  # Should be already
# --------------------------
# Config
# --------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_ROOT = os.getenv('MEDIA_ROOT', os.path.join(BASE_DIR, 'media'))
DB_PATH = os.getenv('DB_PATH', os.path.join(BASE_DIR, 'actresses.db'))
TWITTER_BEARER = os.getenv('TWITTER_BEARER')  # For Twitter API v2
ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif'}
DELETE_MEDIA_ON_REMOVE = os.getenv('DELETE_MEDIA_ON_REMOVE', 'false').lower() in ('1','true','yes')
RECYCLE_BIN = os.path.join(MEDIA_ROOT, 'recycle_bin')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')

os.makedirs(MEDIA_ROOT, exist_ok=True)
os.makedirs(RECYCLE_BIN, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET', 'b3f2830d075ba2c3f090cad51d29c6659bde518b9fc8276c4ee3bc175a579ead')
app.config['UPLOAD_FOLDER'] = MEDIA_ROOT
app.config['WTF_CSRF_ENABLED'] = True

# Initialize CSRF protection
csrf = CSRFProtect(app)

# --------------------------
# Dropdown options (customize if you want)
# --------------------------
MARITAL_STATUS_OPTIONS = ['Single', 'Married', 'Divorced', 'Widowed', 'In Relationship', 'Complicated']
CHILDREN_OPTIONS = ['No', 'Yes']
RELIGION_OPTIONS = [
    'Christianity', 'Islam', 'Hinduism', 'Buddhism', 'Judaism', 'Sikhism',
    'Atheist', 'Agnostic', 'Other'
]
ETHNICITY_OPTIONS = [
    'Asian', 'Black', 'Caucasian', 'Hispanic / Latino', 'Middle Eastern',
    'Native American', 'Mixed', 'Other'
]
EYE_COLOR_OPTIONS = ['Brown','Blue','Green','Hazel','Gray','Amber']
HAIR_COLOR_OPTIONS = ['Black','Brown','Blonde','Red','Dyed','Mixed']
OCCUPATION_CATEGORY_OPTIONS = [
    'Model','Actress','Singer','Influencer','Adult Performer','Fitness Model','Beauty Pageant','TikTok Creator'
]
STATUS_OPTIONS = ['Active','Inactive','Retired']

# --------------------------
# DB helpers
# --------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# list of columns we want in the final schema (name + many fields)
DESIRED_COLUMNS = [
    ('id','INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('name','TEXT NOT NULL'),
    ('aka','TEXT'),
    ('profession','TEXT'),
    ('occupation_category','TEXT'),
    ('age','INTEGER'),
    ('dob','TEXT'),
    ('birthplace','TEXT'),
    ('hometown','TEXT'),
    ('marital_status','TEXT'),
    ('children','TEXT'),
    ('nationality','TEXT'),
    ('religion','TEXT'),
    ('ethnicity','TEXT'),
    ('height','TEXT'),
    ('weight','TEXT'),
    ('measurements','TEXT'),
    ('eye_color','TEXT'),
    ('hair_color','TEXT'),
    ('instagram','TEXT'),
    ('tiktok','TEXT'),
    ('twitter','TEXT'),
    ('onlyfans','TEXT'),
    ('languages','TEXT'),
    ('tags','TEXT'),
    ('specialties','TEXT'),
    ('birthday','TEXT'),
    ('country','TEXT'),
    ('piercings','TEXT'),
    ('tattoo','TEXT'),
    ('status','TEXT'),
    ('has_videos','INTEGER DEFAULT 0'),
    ('has_pictures','INTEGER DEFAULT 0'),
    ('sexual_orientation','TEXT'),
    ('bdsm_orientation','TEXT'),
    ('description','TEXT'),
    ('folder_name','TEXT')
]

def ensure_schema():
    """
    Creates table if missing and adds missing columns using ALTER TABLE.
    This preserves existing data.
    """
    conn = get_conn(); cur = conn.cursor()
    # create table if not exists with minimal columns (id,name) - we'll add others later
    cur.execute('''
    CREATE TABLE IF NOT EXISTS actresses (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL
    );
    ''')
    conn.commit()

    # find existing columns
    cur.execute("PRAGMA table_info('actresses')")
    existing = [r['name'] for r in cur.fetchall()]

    # iterate desired columns, add if missing
    for col, coldef in DESIRED_COLUMNS:
        if col not in existing:
            if col == 'id' or col == 'name':
                continue  # already created by initial CREATE
            try:
                sql = f"ALTER TABLE actresses ADD COLUMN {col} {coldef}"
                cur.execute(sql)
                conn.commit()
                print("Added column:", col)
            except Exception as e:
                print("Failed to add column", col, ":", e)

    # FTS5 virtual table for search - enhanced with more fields
    # Check if FTS exists and has all required columns
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='actresses_fts'")
    fts_exists = cur.fetchone() is not None
    if fts_exists:
        cur.execute("PRAGMA table_info('actresses_fts')")
        fts_columns = [r['name'] for r in cur.fetchall()]
        if 'profession' not in fts_columns or 'specialties' not in fts_columns:
            print("FTS table missing columns, recreating...")
            cur.execute('DROP TABLE IF EXISTS actresses_fts')
            fts_exists = False

    if not fts_exists:
        cur.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS actresses_fts USING fts5(
            name, aka, description, tags, profession, specialties, content='actresses', content_rowid='id'
        );
        ''')

    # Populate/Sync FTS
    cur.execute('INSERT OR REPLACE INTO actresses_fts(rowid, name, aka, description, tags, profession, specialties) SELECT id, name, aka, description, tags, profession, specialties FROM actresses')
    conn.commit()
    conn.close()

# run migration on startup
ensure_schema()

# --------------------------
# Forms for validation (Feature 8)
# --------------------------
class ActressForm(FlaskForm):
    name = StringField('Name', [validators.DataRequired(), validators.Length(max=100)])
    aka = StringField('AKA', [validators.Length(max=200)])
    age = IntegerField('Age', [validators.Optional(), validators.NumberRange(min=18, max=100)])
    dob = StringField('DOB')
    birthplace = StringField('Birthplace')
    hometown = StringField('Hometown')
    marital_status = SelectField('Marital Status', choices=[('', 'Select')] + [(s, s) for s in MARITAL_STATUS_OPTIONS])
    children = SelectField('Children', choices=[('', 'Select')] + [(c, c) for c in CHILDREN_OPTIONS])
    nationality = StringField('Nationality')
    religion = SelectField('Religion', choices=[('', 'Select')] + [(r, r) for r in RELIGION_OPTIONS])
    ethnicity = SelectField('Ethnicity', choices=[('', 'Select')] + [(e, e) for e in ETHNICITY_OPTIONS])
    height = StringField('Height')
    weight = StringField('Weight')
    measurements = StringField('Measurements')
    eye_color = SelectField('Eye Color', choices=[('', 'Select')] + [(ec, ec) for ec in EYE_COLOR_OPTIONS])
    hair_color = SelectField('Hair Color', choices=[('', 'Select')] + [(hc, hc) for hc in HAIR_COLOR_OPTIONS])
    instagram = StringField('Instagram')
    tiktok = StringField('TikTok')
    twitter = StringField('Twitter')
    onlyfans = StringField('OnlyFans')
    languages = StringField('Languages')
    tags = StringField('Tags')
    specialties = StringField('Specialties')
    birthday = StringField('Birthday')
    country = StringField('Country')
    piercings = StringField('Piercings')
    tattoo = StringField('Tattoo')
    status = SelectField('Status', choices=[('', 'Select')] + [(s, s) for s in STATUS_OPTIONS])
    has_videos = BooleanField('Has Videos')
    has_pictures = BooleanField('Has Pictures')
    sexual_orientation = StringField('Sexual Orientation')
    bdsm_orientation = StringField('BDSM Orientation')
    description = TextAreaField('Description', [validators.Length(max=5000)])
    folder_name = StringField('Folder Name')
    profession = StringField('Profession')
    occupation_category = SelectField('Occupation Category', choices=[('', 'Select')] + [(oc, oc) for oc in OCCUPATION_CATEGORY_OPTIONS])

# --------------------------
# Utilities
# --------------------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXT

def safe_folder_name(name: str) -> str:
    return secure_filename(name).strip() or 'unknown'

def get_thumbnail_path(folder_name: str):
    if not folder_name: return None
    folder = os.path.join(MEDIA_ROOT, folder_name)
    if not os.path.isdir(folder): return None
    for candidate in os.listdir(folder):
        low = candidate.lower()
        if low.startswith('thumbnail') and low.split('.')[-1] in ALLOWED_IMAGE_EXT:
            return os.path.join(folder, candidate)
    for candidate in os.listdir(folder):
        if candidate.rsplit('.',1)[-1].lower() in ALLOWED_IMAGE_EXT:
            return os.path.join(folder, candidate)
    return None

def build_filter_sql(q, status_filter, ethnicity_filter, occupation_filter, tag_filter, sort_by, age_min=None, age_max=None, height_min=None, height_max=None, page=1, per_page=20):
    where_clauses = []
    params = []
    base_sql = 'SELECT * FROM actresses'
    if q:
        base_sql = 'SELECT a.* FROM actresses a JOIN actresses_fts f ON a.id = f.rowid'
        where_clauses.append('f MATCH ?')
        params.append(q + '*')  # Simple prefix search
    if status_filter:
        where_clauses.append('status = ?')
        params.append(status_filter)
    if ethnicity_filter:
        where_clauses.append('ethnicity = ?')
        params.append(ethnicity_filter)
    if occupation_filter:
        where_clauses.append('occupation_category = ?')
        params.append(occupation_filter)
    if tag_filter:
        where_clauses.append('tags LIKE ?')
        params.append(f'%{tag_filter}%')
    if age_min is not None:
        where_clauses.append('age IS NOT NULL AND age >= ?')
        params.append(age_min)
    if age_max is not None:
        where_clauses.append('age IS NOT NULL AND age <= ?')
        params.append(age_max)
    # Height parsing (simple assume inches, rough)
    if height_min is not None:
        where_clauses.append('height IS NOT NULL AND CAST(REPLACE(REPLACE(height, "\'", ""), "\"", "") AS INTEGER) >= ?')
        params.append(height_min)
    if height_max is not None:
        where_clauses.append('height IS NOT NULL AND CAST(REPLACE(REPLACE(height, "\'", ""), "\"", "") AS INTEGER) <= ?')
        params.append(height_max)
    if where_clauses:
        sql = base_sql + ' WHERE ' + ' AND '.join(where_clauses)
    else:
        sql = base_sql
    if sort_by not in ('name','age','country'):
        sort_by = 'name'
    sql += f' ORDER BY {sort_by} COLLATE NOCASE'
    # Pagination (Feature 3)
    offset = (page - 1) * per_page
    sql += f' LIMIT ? OFFSET ?'
    params.extend([per_page, offset])
    return sql, params

# Tag cloud (Feature 1)
def get_tag_cloud():
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT tags FROM actresses WHERE tags IS NOT NULL AND tags != ""')
    rows = cur.fetchall()
    tags_text = ' '.join([r['tags'] for r in rows])
    words = [w.strip('#, .') for w in tags_text.split() if w.startswith('#') and len(w) > 1]
    common = Counter(words).most_common(20)
    conn.close()
    return common

# Social Media Sync Utilities
def sync_twitter(username):
    if not TWITTER_BEARER:
        return {'error': 'Twitter bearer token not set'}
    headers = {'Authorization': f'Bearer {TWITTER_BEARER}'}
    url = f'https://api.twitter.com/2/users/by/username/{username}?user.fields=public_metrics,description'
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()['data']
        return {
            'followers_count': data.get('public_metrics', {}).get('followers_count', 0),
            'description': data.get('description', ''),
            'verified': data.get('verified', False)
        }
    return {'error': resp.text}

def sync_instagram(username):
    url = f'https://www.instagram.com/{username}/'
    resp = requests.get(url)
    if resp.status_code == 200:
        soup = BeautifulSoup(resp.text, 'html.parser')
        script = soup.find('script', text=lambda t: 'window._sharedData' in t)
        if script:
            # Note: Instagram structure may change; this is a basic scrape
            data = json.loads(script.string.split('window._sharedData = ')[1].rstrip(';'))
            user = data['entry_data']['ProfilePage'][0]['graphql']['user']
            return {
                'followers_count': user['edge_followed_by']['count'],
                'biography': user['biography'],
                'is_verified': user['is_verified']
            }
    return {'error': 'Failed to fetch'}

# Backup Utility
def backup_database(automated=False):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f'backup_{timestamp}.zip'
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    with zipfile.ZipFile(backup_path, 'w') as zf:
        # Backup DB
        conn = get_conn()
        with zf.open('actresses.sql', 'w') as f:
            for line in conn.iterdump():
                f.write((f"{line}\n").encode('utf-8'))
        conn.close()
        # Backup media if not automated
        if not automated:
            for root, dirs, files in os.walk(MEDIA_ROOT):
                for file in files:
                    arcname = os.path.join('media', os.path.relpath(os.path.join(root, file), MEDIA_ROOT))
                    zf.write(os.path.join(root, file), arcname)
    if not automated:
        return jsonify({'backup': backup_name})
    return f'Automated backup created: {backup_name}'

# --------------------------
# Routes
# --------------------------
@app.route('/')
def index():
    q = request.args.get('q','').strip()
    # Remove view param - always list
    view = 'list'  # Hardcode to list
    status_filter = request.args.get('status','')
    ethnicity_filter = request.args.get('ethnicity','')
    occupation_filter = request.args.get('occupation_category','')
    tag_filter = request.args.get('tags','')
    sort_by = request.args.get('sort','name')
    age_min = request.args.get('age_min', type=int)
    age_max = request.args.get('age_max', type=int)
    height_min = request.args.get('height_min', type=int)
    height_max = request.args.get('height_max', type=int)
    page = int(request.args.get('page', 1))
    sql, params = build_filter_sql(q, status_filter, ethnicity_filter, occupation_filter, tag_filter, sort_by, age_min, age_max, height_min, height_max, page)
    conn = get_conn(); cur = conn.cursor(); cur.execute(sql, params); rows = cur.fetchall()
    # Total count for pagination
    count_base = sql.rsplit(' ORDER BY ', 1)[0].rsplit(' LIMIT ? OFFSET ? ', 1)[0]
    if 'JOIN actresses_fts' in count_base:
        count_sql = count_base.replace('SELECT a.*', 'SELECT COUNT(*)')
    else:
        count_sql = count_base.replace('SELECT *', 'SELECT COUNT(*)')
    count_params = params[:-2] if len(params) >= 2 else params  # Exclude LIMIT/OFFSET
    cur.execute(count_sql, count_params); total = cur.fetchone()[0]
    conn.close()

    # Pagination info
    per_page = 20
    total_pages = (total + per_page - 1) // per_page

    missing = []
    for r in rows:
        if not r['folder_name'] or not get_thumbnail_path(r['folder_name']):
            missing.append({'id': r['id'], 'name': r['name'], 'folder': r['folder_name']})

    tags = get_tag_cloud()  # Feature 1

    return render_template('index.html',
        actresses=rows, view=view, q=q, page=page, total_pages=total_pages, per_page=per_page,
        MARITAL_STATUS_OPTIONS=MARITAL_STATUS_OPTIONS,
        CHILDREN_OPTIONS=CHILDREN_OPTIONS,
        RELIGION_OPTIONS=RELIGION_OPTIONS,
        ETHNICITY_OPTIONS=ETHNICITY_OPTIONS,
        EYE_COLOR_OPTIONS=EYE_COLOR_OPTIONS,
        HAIR_COLOR_OPTIONS=HAIR_COLOR_OPTIONS,
        OCCUPATION_CATEGORY_OPTIONS=OCCUPATION_CATEGORY_OPTIONS,
        STATUS_OPTIONS=STATUS_OPTIONS,
        missing_thumbs=missing, sort_by=sort_by,
        tags=tags, age_min=age_min, age_max=age_max, height_min=height_min, height_max=height_max,
        occupation_filter=occupation_filter, tag_filter=tag_filter)

@app.route('/media/<path:filename>')
def media(filename):
    return send_from_directory(MEDIA_ROOT, filename)

@app.route('/add', methods=['GET','POST'])
def add_actress():
    form = ActressForm()
    if form.validate_on_submit():
        data = {k: v.data for k,v in form._fields.items() if k != 'csrf_token'}
        # Enhanced dupe check with fuzzy matching
        conn = get_conn(); cur = conn.cursor()
        cur.execute('SELECT id, name FROM actresses WHERE lower(name) LIKE lower(?)', (f"%{data['name']}%",))
        existing = cur.fetchall()
        if existing:
            # Exact match first
            exact = next((r for r in existing if fuzz.ratio(r['name'], data['name']) == 100), None)
            if exact:
                flash(f'Exact duplicate "{data["name"]}" already exists (ID: {exact["id"]})', 'error')
            else:
                # Fuzzy near-matches
                similars = [r for r in existing if fuzz.ratio(r['name'], data['name']) > 85]
                if similars:
                    flash(f'Potential duplicates found: {", ".join([s["name"] for s in similars[:3]])} (similar to "{data["name"]}"). Proceed?', 'warning')
                else:
                    # No issue, proceed
                    pass
            conn.close()
            thumbnail_path = None  # No actress yet
            return render_template('form.html',
                form=form, actress={}, thumbnail_path=thumbnail_path,
                MARITAL_STATUS_OPTIONS=MARITAL_STATUS_OPTIONS, CHILDREN_OPTIONS=CHILDREN_OPTIONS,
                RELIGION_OPTIONS=RELIGION_OPTIONS, ETHNICITY_OPTIONS=ETHNICITY_OPTIONS,
                EYE_COLOR_OPTIONS=EYE_COLOR_OPTIONS, HAIR_COLOR_OPTIONS=HAIR_COLOR_OPTIONS,
                OCCUPATION_CATEGORY_OPTIONS=OCCUPATION_CATEGORY_OPTIONS, STATUS_OPTIONS=STATUS_OPTIONS)
        # Proceed with add
        folder_name = data.get('folder_name') or safe_folder_name(data.get('name') or '')
        folder_path = os.path.join(MEDIA_ROOT, folder_name); os.makedirs(folder_path, exist_ok=True)
        thumb = request.files.get('thumbnail')
        if thumb and allowed_file(thumb.filename):
            ext = os.path.splitext(secure_filename(thumb.filename))[1]
            save_path = os.path.join(folder_path, 'thumbnail' + ext); thumb.save(save_path); data['has_pictures'] = 1
        data['folder_name'] = folder_name; _insert_actress(data); 
        conn.close()
        flash('Actress added successfully', 'success'); return redirect(url_for('index'))
    # GET: Render empty form
    thumbnail_path = None
    return render_template('form.html',
        form=form, actress={}, thumbnail_path=thumbnail_path,
        MARITAL_STATUS_OPTIONS=MARITAL_STATUS_OPTIONS, CHILDREN_OPTIONS=CHILDREN_OPTIONS,
        RELIGION_OPTIONS=RELIGION_OPTIONS, ETHNICITY_OPTIONS=ETHNICITY_OPTIONS,
        EYE_COLOR_OPTIONS=EYE_COLOR_OPTIONS, HAIR_COLOR_OPTIONS=HAIR_COLOR_OPTIONS,
        OCCUPATION_CATEGORY_OPTIONS=OCCUPATION_CATEGORY_OPTIONS, STATUS_OPTIONS=STATUS_OPTIONS)

@app.route('/edit/<int:actress_id>', methods=['GET','POST'])
def edit_actress(actress_id):
    conn = get_conn(); cur = conn.cursor(); cur.execute('SELECT * FROM actresses WHERE id=?', (actress_id,)); actress = cur.fetchone(); conn.close()
    if not actress:
        flash('Not found', 'error'); return redirect(url_for('index'))
    form = ActressForm(obj=actress)
    if form.validate_on_submit():
        data = {k: v.data for k,v in form._fields.items() if k != 'csrf_token'}
        # Dupe check (exclude self)
        conn = get_conn(); cur = conn.cursor()
        cur.execute('SELECT id FROM actresses WHERE lower(name)=lower(?) AND id != ?', (data['name'], actress_id))
        existing = cur.fetchone()
        if existing:
            flash('Duplicate name detected', 'error')
            conn.close()
            return render_template('form.html', form=form, actress=actress)
        new_folder = data.get('folder_name') or safe_folder_name(data.get('name') or actress['name'])
        old_folder = actress['folder_name'] or ''
        if old_folder and new_folder and old_folder != new_folder:
            old_path = os.path.join(MEDIA_ROOT, old_folder); new_path = os.path.join(MEDIA_ROOT, new_folder)
            try:
                if os.path.exists(old_path): shutil.move(old_path, new_path)
                else: os.makedirs(new_path, exist_ok=True)
            except Exception as e:
                flash(f'Folder rename failed: {e}', 'error')
        else:
            os.makedirs(os.path.join(MEDIA_ROOT, new_folder), exist_ok=True)
        thumb = request.files.get('thumbnail')
        if thumb and allowed_file(thumb.filename):
            ext = os.path.splitext(secure_filename(thumb.filename))[1]
            save_path = os.path.join(MEDIA_ROOT, new_folder, 'thumbnail' + ext); thumb.save(save_path); data['has_pictures'] = 1
        data['folder_name'] = new_folder; _update_actress(actress_id, data); 
        conn.close()
        flash('Actress updated', 'success'); return redirect(url_for('index'))
    thumbnail_path = get_thumbnail_path(actress['folder_name']) if actress['folder_name'] else None
    return render_template('form.html', form=form, actress=actress,
        MARITAL_STATUS_OPTIONS=MARITAL_STATUS_OPTIONS, CHILDREN_OPTIONS=CHILDREN_OPTIONS,
        RELIGION_OPTIONS=RELIGION_OPTIONS, ETHNICITY_OPTIONS=ETHNICITY_OPTIONS,
        EYE_COLOR_OPTIONS=EYE_COLOR_OPTIONS, HAIR_COLOR_OPTIONS=HAIR_COLOR_OPTIONS,
        OCCUPATION_CATEGORY_OPTIONS=OCCUPATION_CATEGORY_OPTIONS, STATUS_OPTIONS=STATUS_OPTIONS)

@app.route('/delete/<int:actress_id>', methods=['POST'])
def delete_actress(actress_id):
    recycle = False
    if request.is_json:
        payload = request.get_json(); recycle = payload.get('recycle', False)
    else:
        recycle = request.form.get('recycle', 'false') in ('1','true','yes','on')
    conn = get_conn(); cur = conn.cursor(); cur.execute('SELECT folder_name FROM actresses WHERE id=?', (actress_id,)); row = cur.fetchone(); folder = row['folder_name'] if row else None
    cur.execute('DELETE FROM actresses WHERE id=?', (actress_id,)); 
    # Delete from FTS
    cur.execute('DELETE FROM actresses_fts WHERE rowid=?', (actress_id,))
    conn.commit(); conn.close()
    if folder:
        folder_path = os.path.join(MEDIA_ROOT, folder)
        try:
            if recycle:
                if os.path.isdir(folder_path):
                    dest = os.path.join(RECYCLE_BIN, f"{folder}_{int(datetime.utcnow().timestamp())}")
                    shutil.move(folder_path, dest)
            elif DELETE_MEDIA_ON_REMOVE:
                if os.path.isdir(folder_path): shutil.rmtree(folder_path)
        except Exception as e:
            flash(f'Failed to handle media folder: {e}', 'error')
    flash('Deleted', 'info')
    return jsonify({'ok': True}) if request.is_json else redirect(url_for('index'))

@app.route('/scan_missing')
def scan_missing():
    conn = get_conn(); cur = conn.cursor(); cur.execute('SELECT id, name, folder_name FROM actresses'); rows = cur.fetchall(); conn.close()
    missing = []
    for r in rows:
        if not r['folder_name'] or not get_thumbnail_path(r['folder_name']):
            missing.append({'id': r['id'], 'name': r['name'], 'folder': r['folder_name']})
    return jsonify({'missing': missing})

# --------------------------
# New Routes for Features
# --------------------------
@app.route('/scrape/<name>')
def scrape_name(name):
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{name.replace(' ', '_')}"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        bio = data.get('extract', '')[:500]
        return jsonify({'description': bio})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/bulk', methods=['POST'])
def bulk_ops():
    action = request.form.get('action')
    ids = request.form.getlist('selected_ids')
    if not ids:
        flash('No items selected', 'error')
        return redirect(url_for('index'))
    conn = get_conn(); cur = conn.cursor()
    if action == 'delete':
        for iid in ids:
            cur.execute('DELETE FROM actresses WHERE id=?', (iid,))
            cur.execute('DELETE FROM actresses_fts WHERE rowid=?', (iid,))
        conn.commit()
    elif action == 'update_status':
        new_status = request.form.get('new_status')
        cur.executemany('UPDATE actresses SET status=? WHERE id=?', [(new_status, iid) for iid in ids])
        cur.executemany('INSERT OR REPLACE INTO actresses_fts(rowid, name, aka, description, tags, profession, specialties) SELECT id, name, aka, description, tags, profession, specialties FROM actresses WHERE id=?', [(iid,) for iid in ids])
        conn.commit()
    conn.close()
    flash(f'Bulk {action} on {len(ids)} items', 'success')
    return redirect(url_for('index'))

# Bulk Expansion: Merge Duplicates
@app.route('/merge_candidates')
def merge_candidates():
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT id, name FROM actresses ORDER BY name')
    rows = cur.fetchall()
    candidates = []
    for i, r1 in enumerate(rows):
        for r2 in rows[i+1:]:
            if fuzz.ratio(r1['name'], r2['name']) > 80:
                candidates.append({'id1': r1['id'], 'name1': r1['name'], 'id2': r2['id'], 'name2': r2['name'], 'score': fuzz.ratio(r1['name'], r2['name'])})
    conn.close()
    return render_template('merge.html', candidates=candidates)  # Assume merge.html exists or add

@app.route('/merge/<int:id1>/<int:id2>', methods=['POST'])
def merge_actresses(id1, id2):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM actresses WHERE id=?', (id1,)); act1 = dict(cur.fetchone())
    cur.execute('SELECT * FROM actresses WHERE id=?', (id2,)); act2 = dict(cur.fetchone())
    # Merge: prefer non-empty fields from act1, fallback to act2
    for key in act1:
        if not act1[key] and act2.get(key):
            act1[key] = act2[key]
    _update_actress(id1, act1)
    # Delete act2
    cur.execute('DELETE FROM actresses WHERE id=?', (id2,))
    cur.execute('DELETE FROM actresses_fts WHERE rowid=?', (id2,))
    # Move media if different
    if act1['folder_name'] != act2['folder_name']:
        old_path = os.path.join(MEDIA_ROOT, act2['folder_name'])
        if os.path.exists(old_path):
            shutil.move(old_path, os.path.join(MEDIA_ROOT, act1['folder_name']))
    conn.commit(); conn.close()
    flash('Merged successfully', 'success')
    return redirect(url_for('index'))

@app.route('/gallery/<int:actress_id>')
def gallery(actress_id):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT folder_name FROM actresses WHERE id=?', (actress_id,))
    row = cur.fetchone()
    folder = row['folder_name'] if row else None
    conn.close()
    if not folder:
        return 'Not found', 404
    folder_path = os.path.join(MEDIA_ROOT, folder)
    if not os.path.isdir(folder_path):
        return 'Folder not found', 404
    files = [f for f in os.listdir(folder_path) if f.rsplit('.',1)[1].lower() in ALLOWED_IMAGE_EXT]
    try:
        return render_template('gallery.html', files=files, folder=folder, actress_id=actress_id)
    except ImportError:
        # Fallback if Jinja2 not fully available, but unlikely
        pass
    except Exception as e:
        if "TemplateNotFound" in str(e):
            # Simple fallback HTML without template engine dependency
            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Gallery - ID {actress_id}</title>
    <style>img {{ max-width: 300px; margin: 10px; }} ul {{ list-style: none; display: flex; flex-wrap: wrap; }}</style>
</head>
<body>
    <h1>Gallery for Folder: {folder}</h1>
    <p>Actress ID: {actress_id} | <a href="/">Back to Home</a></p>
    <ul>
"""
            for f in files:
                html += f'        <li><img src="/media/{folder}/{f}" alt="{f}" loading="lazy"></li>\n'
            html += """    </ul>
    <p>Total images: {len(files)}</p>
</body>
</html>"""
            return html, 200, {'Content-Type': 'text/html'}
        else:
            raise e

@app.route('/dashboard')
def dashboard():
    conn = get_conn(); cur = conn.cursor()
    # Age dist
    cur.execute('SELECT age, COUNT(*) as count FROM actresses WHERE age IS NOT NULL GROUP BY age ORDER BY age')
    age_data = [{'age': r['age'], 'count': r['count']} for r in cur.fetchall()]
    # Ethnicity pie
    cur.execute('SELECT ethnicity, COUNT(*) as count FROM actresses WHERE ethnicity IS NOT NULL GROUP BY ethnicity')
    eth_data = [{'ethnicity': r['ethnicity'], 'count': r['count']} for r in cur.fetchall()]
    # Occupation dist
    cur.execute('SELECT occupation_category, COUNT(*) as count FROM actresses WHERE occupation_category IS NOT NULL GROUP BY occupation_category')
    occ_data = [{'category': r['occupation_category'], 'count': r['count']} for r in cur.fetchall()]
    # Status dist
    cur.execute('SELECT status, COUNT(*) as count FROM actresses WHERE status IS NOT NULL GROUP BY status')
    status_data = [{'status': r['status'], 'count': r['count']} for r in cur.fetchall()]
    conn.close()
    
    # Format current date
    current_date = datetime.now().strftime('%B %d, %Y')
    
    return render_template('dashboard.html', 
        age_data=json.dumps(age_data), 
        eth_data=json.dumps(eth_data),
        occ_data=json.dumps(occ_data),
        status_data=json.dumps(status_data),
        current_date=current_date)  # Add this line

# Social Media Sync
@app.route('/sync/<int:actress_id>')
def sync_social(actress_id):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT twitter, instagram, description FROM actresses WHERE id=?', (actress_id,))
    row = cur.fetchone()
    if not row:
        return 'Not found', 404
    updates = {}
    if row['twitter']:
        twitter_data = sync_twitter(row['twitter'].lstrip('@'))
        if 'error' not in twitter_data:
            updates['twitter_followers'] = twitter_data['followers_count']
    if row['instagram']:
        ig_data = sync_instagram(row['instagram'].lstrip('@'))
        if 'error' not in ig_data:
            updates['ig_followers'] = ig_data['followers_count']
    # Update DB if updates
    if updates:
        updates_str = f"\n\nSocial Sync {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}: {json.dumps(updates)}"
        cur.execute('UPDATE actresses SET description = COALESCE(description, "") || ? WHERE id=?', (updates_str, actress_id))
        cur.execute('INSERT OR REPLACE INTO actresses_fts(rowid, name, aka, description, tags, profession, specialties) SELECT id, name, aka, description, tags, profession, specialties FROM actresses WHERE id=?', (actress_id,))
        conn.commit()
    conn.close()
    return jsonify(updates or {'message': 'No updates'})

@app.route('/pdf/<int:actress_id>')
def pdf_profile(actress_id):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM actresses WHERE id=?', (actress_id,))
    actress_row = cur.fetchone()
    actress = dict(actress_row) if actress_row else None
    conn.close()
    if not actress:
        return 'Not found', 404
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    story = []
    styles = getSampleStyleSheet()
    story.append(Paragraph(actress['name'], styles['Title']))
    if actress['folder_name']:
        thumb_path = get_thumbnail_path(actress['folder_name'])
        if thumb_path and os.path.exists(thumb_path):
            img = Image(thumb_path, width=2*inch, height=2*inch)
            story.append(img)
    story.append(Spacer(1, 12))
    for key, value in actress.items():
        if value and key not in ('id', 'folder_name'):
            story.append(Paragraph(f"<b>{key.replace('_', ' ').title()}:</b> {value}", styles['Normal']))
    doc.build(story)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype='application/pdf', headers={'Content-Disposition': f'attachment;filename={actress["name"].replace(" ", "_")}.pdf'})

# --------------------------
# Flexible CSV import
# --------------------------
# Accept any file as long as "name" (case-insensitive) exists.
# Map common header synonyms automatically.
HEADER_SYNONYMS = {
    'name': ['name','full name','model name','actor','actress','displayname'],
    'aka': ['aka','also known as','also_known_as','other names'],
    'profession': ['profession','job','occupation'],
    'age': ['age'],
    'dob': ['dob','date of birth','birthdate','birthday'],
    'birthplace': ['birthplace','place of birth'],
    'hometown': ['hometown','residence','lives in'],
    'marital_status': ['marital status','marital_status','married'],
    'children': ['children','has children'],
    'nationality': ['nationality','country'],
    'religion': ['religion'],
    'ethnicity': ['ethnicity'],
    'folder_name': ['folder','folder_name','media folder','media_folder'],
    # add more if you like...
}

# flatten mapping: header_lower -> canonical_field
def build_header_map(fieldnames):
    fm = {}
    lower_names = [c.strip().lower() for c in (fieldnames or [])]
    for canonical, syns in HEADER_SYNONYMS.items():
        for s in syns:
            if s in lower_names:
                fm[canonical] = lower_names.index(s)
                break
    # Also try exact column names mapping if present
    for i, col in enumerate(lower_names):
        if col in HEADER_SYNONYMS:
            fm[col] = i
    return fm, lower_names

@app.route('/export_csv')
def export_csv():
    q = request.args.get('q','').strip(); status_filter = request.args.get('status',''); ethnicity_filter = request.args.get('ethnicity',''); occupation_filter = request.args.get('occupation_category',''); tag_filter = request.args.get('tags',''); sort_by = request.args.get('sort','name')
    age_min = request.args.get('age_min', type=int)
    age_max = request.args.get('age_max', type=int)
    height_min = request.args.get('height_min', type=int)
    height_max = request.args.get('height_max', type=int)
    sql, params = build_filter_sql(q, status_filter, ethnicity_filter, occupation_filter, tag_filter, sort_by, age_min, age_max, height_min, height_max)
    conn = get_conn(); cur = conn.cursor(); cur.execute(sql, params); rows = cur.fetchall(); conn.close()
    si = io.StringIO(); cw = csv.writer(si)
    header = [
        'Name','AKA','Profession','OccupationCategory','Age','DOB','Birthplace','Hometown',
        'MaritalStatus','Children','Nationality','Religion','Ethnicity','Height','Weight','Measurements',
        'EyeColor','HairColor','Instagram','TikTok','Twitter','OnlyFans','Languages','Tags',
        'Specialties','Birthday','Country','Piercings','Tattoo','Status','HasVideos','HasPictures',
        'SexualOrientation','BDSMOrientation','Description','FolderName'
    ]
    cw.writerow(header)
    for r in rows:
        cw.writerow([
            r.get('name'), r.get('aka'), r.get('profession'), r.get('occupation_category'), r.get('age'), r.get('dob'), r.get('birthplace'), r.get('hometown'),
            r.get('marital_status'), r.get('children'), r.get('nationality'), r.get('religion'), r.get('ethnicity'), r.get('height'), r.get('weight'), r.get('measurements'),
            r.get('eye_color'), r.get('hair_color'), r.get('instagram'), r.get('tiktok'), r.get('twitter'), r.get('onlyfans'), r.get('languages'), r.get('tags'),
            r.get('specialties'), r.get('birthday'), r.get('country'), r.get('piercings'), r.get('tattoo'), r.get('status'), r.get('has_videos'), r.get('has_pictures'),
            r.get('sexual_orientation'), r.get('bdsm_orientation'), r.get('description'), r.get('folder_name')
        ])
    return Response(si.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment;filename=actresses_export.csv'})

@app.route('/export_json')
def export_json():
    q = request.args.get('q','').strip(); status_filter = request.args.get('status',''); ethnicity_filter = request.args.get('ethnicity',''); occupation_filter = request.args.get('occupation_category',''); tag_filter = request.args.get('tags',''); sort_by = request.args.get('sort','name')
    age_min = request.args.get('age_min', type=int)
    age_max = request.args.get('age_max', type=int)
    height_min = request.args.get('height_min', type=int)
    height_max = request.args.get('height_max', type=int)
    sql, params = build_filter_sql(q, status_filter, ethnicity_filter, occupation_filter, tag_filter, sort_by, age_min, age_max, height_min, height_max)
    conn = get_conn(); cur = conn.cursor(); cur.execute(sql, params); rows = cur.fetchall(); conn.close()
    data = [dict(r) for r in rows]
    return Response(json.dumps(data, indent=2), mimetype='application/json', headers={'Content-Disposition':'attachment;filename=actresses_export.json'})

# Bulk Expansion: JSON Import
@app.route('/import_json', methods=['GET', 'POST'])
def import_json():
    if request.method == 'POST':
        f = request.files.get('jsonfile')
        if not f:
            flash('No file uploaded', 'error'); return redirect(url_for('import_json'))
        try:
            data = json.load(f)
        except Exception as e:
            flash(f'Failed to read JSON: {e}', 'error'); return redirect(url_for('import_json'))

        upserted = 0; skipped = 0
        conn = get_conn(); cur = conn.cursor()

        for item in data:
            name_val = item.get('name')
            if not name_val:
                skipped += 1; continue

            data_dict = {col[0]: item.get(col[0]) for col in DESIRED_COLUMNS if col[0] != 'id'}
            data_dict['age'] = int(data_dict.get('age', 0)) if data_dict.get('age') else None
            data_dict['folder_name'] = data_dict.get('folder_name') or safe_folder_name(name_val)

            # check existing
            cur.execute('SELECT name, id FROM actresses WHERE lower(name)=lower(?)', (name_val,))
            existing = cur.fetchone()
            if existing:
                skipped += 1  # or update logic
            else:
                columns = ', '.join(data_dict.keys())
                placeholders = ', '.join(['?' for _ in data_dict])
                cur.execute(f'INSERT INTO actresses ({columns}) VALUES ({placeholders})', list(data_dict.values()))
                new_id = cur.lastrowid
                cur.execute('INSERT OR REPLACE INTO actresses_fts(rowid, name, aka, description, tags, profession, specialties) VALUES (?, ?, ?, ?, ?, ?, ?)', 
                            (new_id, data_dict['name'], data_dict.get('aka'), data_dict.get('description'), data_dict.get('tags'), data_dict.get('profession'), data_dict.get('specialties')))
                upserted += 1

        conn.commit(); conn.close()
        flash(f'JSON import complete. Upserted: {upserted}, Skipped: {skipped}', 'success')
        return redirect(url_for('index'))

    return render_template('import_json.html', title='Import JSON')  # Assume template similar to import_csv.html

@app.route('/import_csv', methods=['GET','POST'])
def import_csv():
    if request.method == 'POST':
        mode = request.form.get('mode','skip')
        f = request.files.get('csvfile')
        if not f:
            flash('No file uploaded', 'error'); return redirect(url_for('import_csv'))
        try:
            stream = io.StringIO(f.stream.read().decode('utf-8'))
            reader = csv.DictReader(stream)
        except Exception as e:
            flash(f'Failed to read CSV: {e}', 'error'); return redirect(url_for('import_csv'))

        # build header map
        fieldnames = reader.fieldnames or []
        header_map, lower_names = build_header_map(fieldnames)
        # require name
        if 'name' not in header_map:
            # try if some column literally called 'name'
            if 'name' in lower_names:
                header_map['name'] = lower_names.index('name')
            else:
                flash('CSV must include a Name column (header like: Name, name, Model Name etc.)', 'error')
                return redirect(url_for('import_csv'))

        upserted = 0; skipped = 0
        conn = get_conn(); cur = conn.cursor()

        for row in reader:
            # case-insensitive column access via row with lowered keys:
            row_lower = {k.strip().lower(): (v or '').strip() for k,v in (row.items())}
            name_val = None
            # find name by synonyms
            for syn in HEADER_SYNONYMS['name']:
                if syn in row_lower and row_lower[syn]:
                    name_val = row_lower[syn]; break
            if not name_val:
                # fallback: first non-empty column
                for v in row.values():
                    if v and v.strip():
                        name_val = v.strip(); break
            if not name_val:
                skipped += 1; continue

            # map fields opportunistically
            def value_for(cands):
                for c in cands:
                    if c in row_lower and row_lower[c]:
                        return row_lower[c]
                return ''

            data = {
                'name': name_val,
                'aka': value_for(HEADER_SYNONYMS.get('aka', [])),
                'profession': value_for(HEADER_SYNONYMS.get('profession', [])),
                'age': (value_for(['age']) or None),
                'dob': value_for(HEADER_SYNONYMS.get('dob', [])),
                'birthplace': value_for(HEADER_SYNONYMS.get('birthplace', [])),
                'hometown': value_for(HEADER_SYNONYMS.get('hometown', [])),
                'marital_status': value_for(HEADER_SYNONYMS.get('marital_status', [])),
                'children': value_for(HEADER_SYNONYMS.get('children', [])),
                'nationality': value_for(HEADER_SYNONYMS.get('nationality', [])),
                'religion': value_for(HEADER_SYNONYMS.get('religion', [])),
                'ethnicity': value_for(HEADER_SYNONYMS.get('ethnicity', [])),
                'folder_name': value_for(HEADER_SYNONYMS.get('folder_name', [])) or safe_folder_name(name_val)
            }

            # normalize age -> int if possible
            try:
                data['age'] = int(data['age']) if data['age'] not in (None, '') else None
            except:
                data['age'] = None

            # check existing (case-insensitive name match with fuzzy)
            cur.execute('SELECT name, id FROM actresses WHERE lower(name)=lower(?)', (data['name'],))
            existing = cur.fetchone()
            if existing:
                if mode == 'skip':
                    skipped += 1
                else:
                    # Fuzzy check for merge/skip
                    if fuzz.ratio(existing['name'], data['name']) < 80:
                        skipped += 1
                        continue
                    # update only mapped fields present - safe update with many columns
                    update_cols = ['aka','profession','age','dob','birthplace','hometown','marital_status','children','nationality','religion','ethnicity','folder_name']
                    set_vals = [data.get(c) for c in update_cols] + [existing['id']]
                    cur.execute(f"UPDATE actresses SET {', '.join([c+'=?' for c in update_cols])} WHERE id=?", set_vals)
                    # Update FTS
                    cur.execute('INSERT OR REPLACE INTO actresses_fts(rowid, name, aka, description, tags, profession, specialties) SELECT id, name, aka, description, tags, profession, specialties FROM actresses WHERE id=?', (existing['id'],))
                    upserted += 1
            else:
                cur.execute('INSERT INTO actresses (name, aka, profession, age, dob, birthplace, hometown, marital_status, children, nationality, religion, ethnicity, folder_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    data['name'], data['aka'], data['profession'], data['age'], data['dob'], data['birthplace'], data['hometown'],
                    data['marital_status'], data['children'], data['nationality'], data['religion'], data['ethnicity'], data['folder_name']
                ))
                new_id = cur.lastrowid
                # Insert to FTS
                cur.execute('INSERT OR REPLACE INTO actresses_fts(rowid, name, aka, description, tags, profession, specialties) VALUES (?, ?, ?, ?, ?, ?, ?)', 
                            (new_id, data['name'], data['aka'], '', '', data['profession'], ''))
                upserted += 1

        conn.commit(); conn.close()
        flash(f'CSV import complete. Upserted: {upserted}, Skipped: {skipped}', 'success')
        return redirect(url_for('index'))

    return render_template('import_csv.html',
        MARITAL_STATUS_OPTIONS=MARITAL_STATUS_OPTIONS,
        CHILDREN_OPTIONS=CHILDREN_OPTIONS,
        RELIGION_OPTIONS=RELIGION_OPTIONS,
        ETHNICITY_OPTIONS=ETHNICITY_OPTIONS
    )

# Backup and Recovery
@app.route('/backup')
def backup():
    backup_name = backup_database(automated=False)
    flash(f'Backup created: {backup_name}', 'success')
    return redirect(url_for('index'))

@app.route('/restore', methods=['POST'])
def restore():
    f = request.files.get('backupfile')
    if not f or not f.filename.endswith('.zip'):
        flash('Invalid backup file', 'error')
        return redirect(url_for('index'))
    try:
        with tempfile.TemporaryDirectory() as tempdir:
            with zipfile.ZipFile(f) as zf:
                zf.extractall(tempdir)
            # Restore DB
            sql_path = os.path.join(tempdir, 'actresses.sql')
            if os.path.exists(sql_path):
                with open(sql_path, 'r', encoding='utf-8') as sqlfile:
                    sql_script = sqlfile.read()
                if os.path.exists(DB_PATH):
                    os.remove(DB_PATH)
                conn = sqlite3.connect(DB_PATH)
                conn.executescript(sql_script)
                conn.close()
            # Restore media
            media_src = os.path.join(tempdir, 'media')
            if os.path.exists(MEDIA_ROOT):
                shutil.rmtree(MEDIA_ROOT)
            if os.path.exists(media_src):
                shutil.copytree(media_src, MEDIA_ROOT)
        ensure_schema()
        flash('Restore successful', 'success')
    except Exception as e:
        flash(f'Restore failed: {e}', 'error')
    return redirect(url_for('index'))

# --------------------------
# DB insert/update helpers
# --------------------------
def _insert_actress(data):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('''
        INSERT INTO actresses (
            name, aka, profession, occupation_category, age, dob, birthplace, hometown, marital_status, children,
            nationality, religion, ethnicity, height, weight, measurements, eye_color, hair_color, instagram, tiktok,
            twitter, onlyfans, languages, tags, specialties, birthday, country, piercings, tattoo, status, has_videos,
            has_pictures, sexual_orientation, bdsm_orientation, description, folder_name
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        data['name'], data['aka'], data['profession'], data['occupation_category'], data['age'], data['dob'], data['birthplace'],
        data['hometown'], data['marital_status'], data['children'], data['nationality'], data['religion'], data['ethnicity'],
        data['height'], data['weight'], data['measurements'], data['eye_color'], data['hair_color'], data['instagram'],
        data['tiktok'], data['twitter'], data['onlyfans'], data['languages'], data['tags'], data['specialties'],
        data['birthday'], data['country'], data['piercings'], data['tattoo'], data['status'], data['has_videos'],
        data['has_pictures'], data['sexual_orientation'], data['bdsm_orientation'], data['description'], data['folder_name']
    ))
    new_id = cur.lastrowid
    cur.execute('INSERT OR REPLACE INTO actresses_fts(rowid, name, aka, description, tags, profession, specialties) VALUES (?, ?, ?, ?, ?, ?, ?)', 
                (new_id, data['name'], data['aka'], data['description'], data['tags'] or '', data['profession'] or '', data['specialties'] or ''))
    conn.commit(); conn.close()

def _update_actress(actress_id, data):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('''
        UPDATE actresses SET
            name=?, aka=?, profession=?, occupation_category=?, age=?, dob=?, birthplace=?, hometown=?, marital_status=?, children=?,
            nationality=?, religion=?, ethnicity=?, height=?, weight=?, measurements=?, eye_color=?, hair_color=?, instagram=?, tiktok=?,
            twitter=?, onlyfans=?, languages=?, tags=?, specialties=?, birthday=?, country=?, piercings=?, tattoo=?, status=?, has_videos=?,
            has_pictures=?, sexual_orientation=?, bdsm_orientation=?, description=?, folder_name=?
        WHERE id=?
    ''', (
        data['name'], data['aka'], data['profession'], data['occupation_category'], data['age'], data['dob'], data['birthplace'],
        data['hometown'], data['marital_status'], data['children'], data['nationality'], data['religion'], data['ethnicity'],
        data['height'], data['weight'], data['measurements'], data['eye_color'], data['hair_color'], data['instagram'],
        data['tiktok'], data['twitter'], data['onlyfans'], data['languages'], data['tags'], data['specialties'],
        data['birthday'], data['country'], data['piercings'], data['tattoo'], data['status'], data['has_videos'],
        data['has_pictures'], data['sexual_orientation'], data['bdsm_orientation'], data['description'], data['folder_name'],
        actress_id
    ))
    cur.execute('INSERT OR REPLACE INTO actresses_fts(rowid, name, aka, description, tags, profession, specialties) VALUES (?, ?, ?, ?, ?, ?, ?)', 
                (actress_id, data['name'], data['aka'], data['description'], data['tags'] or '', data['profession'] or '', data['specialties'] or ''))
    conn.commit(); conn.close()

# Scheduler for automated backups (every day at midnight) - optional
scheduler = None
if SCHEDULER_AVAILABLE:
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=lambda: backup_database(automated=True), trigger="cron", hour=0, minute=0)
    scheduler.start()

# --------------------------
# Run
# --------------------------
if __name__ == '__main__':
    print('Using MEDIA_ROOT =', MEDIA_ROOT)
    print('Using DB_PATH =', DB_PATH)
    print('RECYCLE_BIN =', RECYCLE_BIN)
    print('BACKUP_DIR =', BACKUP_DIR)
    if SCHEDULER_AVAILABLE:
        print('Automated backups enabled.')
    else:
        print('Automated backups disabled. Install APScheduler for automation.')
    app.run(debug=True)