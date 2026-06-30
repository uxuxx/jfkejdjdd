from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import sqlite3, hashlib, os, shutil, subprocess
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

app = FastAPI()
DB_PATH = "database.db"
UPLOAD_DIR = "uploads"
AVATAR_DIR = "avatars"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(AVATAR_DIR, exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE,
        password TEXT,
        nickname TEXT,
        avatar TEXT,
        bio TEXT,
        last_seen TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        from_user INTEGER,
        to_user INTEGER,
        text TEXT,
        file_path TEXT,
        timestamp TEXT,
        read INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS typing (
        user_id INTEGER,
        to_user INTEGER,
        last_typing TEXT,
        PRIMARY KEY (user_id, to_user)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS blocks (
        user_id INTEGER,
        blocked_user_id INTEGER,
        PRIMARY KEY (user_id, blocked_user_id)
    )''')
    conn.commit()
    conn.close()
init_db()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO users (username, password, nickname, last_seen) VALUES (?,?,?,?)",
                     (username, hash_pw(password), username, datetime.utcnow().isoformat()))
        conn.commit()
        return {"ok": True}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "user exists")
    finally:
        conn.close()

@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id, password FROM users WHERE username=?", (username,)).fetchone()
    if row and row[1] == hash_pw(password):
        conn.execute("UPDATE users SET last_seen=? WHERE id=?", (datetime.utcnow().isoformat(), row[0]))
        conn.commit()
        conn.close()
        return {"user_id": row[0]}
    conn.close()
    raise HTTPException(401, "bad creds")

@app.get("/users")
async def search_users(q: str, me: int):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT id, username, nickname, avatar, last_seen FROM users
        WHERE username LIKE ? AND id != ?
        AND id NOT IN (SELECT blocked_user_id FROM blocks WHERE user_id = ?)
        AND id NOT IN (SELECT user_id FROM blocks WHERE blocked_user_id = ?)
        LIMIT 20
    ''', (f"%{q}%", me, me, me)).fetchall()
    conn.close()
    return [{"id": r[0], "username": r[1], "nickname": r[2], "avatar": r[3], "online": (datetime.utcnow() - datetime.fromisoformat(r[4])).seconds < 60} for r in rows]

@app.get("/profile/{user_id}")
async def get_profile(user_id: int, me: int = None):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT username, nickname, avatar, bio, last_seen FROM users WHERE id=?", (user_id,)).fetchone()
    if not row: raise HTTPException(404, "no user")
    blocked = False
    if me:
        blocked = conn.execute("SELECT 1 FROM blocks WHERE user_id=? AND blocked_user_id=?", (me, user_id)).fetchone() is not None
    conn.close()
    return {"username": row[0], "nickname": row[1], "avatar": row[2], "bio": row[3], "online": (datetime.utcnow() - datetime.fromisoformat(row[4])).seconds < 60, "blocked": blocked}

@app.put("/profile")
async def update_profile(
    user_id: int = Form(...),
    nickname: str = Form(None),
    bio: str = Form(None),
    avatar: UploadFile = File(None)
):
    conn = sqlite3.connect(DB_PATH)
    updates = []
    params = []
    if nickname is not None:
        updates.append("nickname=?")
        params.append(nickname)
    if bio is not None:
        updates.append("bio=?")
        params.append(bio)
    if avatar:
        ext = os.path.splitext(avatar.filename)[1]
        fname = f"{user_id}_{int(datetime.utcnow().timestamp())}{ext}"
        path = os.path.join(AVATAR_DIR, fname)
        with open(path, "wb") as f:
            shutil.copyfileobj(avatar.file, f)
        old = conn.execute("SELECT avatar FROM users WHERE id=?", (user_id,)).fetchone()
        if old and old[0]:
            try: os.remove(os.path.join(AVATAR_DIR, old[0]))
            except: pass
        updates.append("avatar=?")
        params.append(fname)
    if updates:
        conn.execute(f"UPDATE users SET {','.join(updates)} WHERE id=?", params + [user_id])
        conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/block")
async def block_user(user_id: int = Form(...), target_id: int = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO blocks (user_id, blocked_user_id) VALUES (?,?)", (user_id, target_id))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return {"ok": True}

@app.delete("/block")
async def unblock_user(user_id: int = Form(...), target_id: int = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM blocks WHERE user_id=? AND blocked_user_id=?", (user_id, target_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/chats/{user_id}")
async def get_chats(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT 
            CASE WHEN from_user = ? THEN to_user ELSE from_user END as other,
            (SELECT username FROM users WHERE id = other),
            (SELECT nickname FROM users WHERE id = other),
            (SELECT avatar FROM users WHERE id = other),
            (SELECT last_seen FROM users WHERE id = other),
            (SELECT text FROM messages WHERE (from_user = ? AND to_user = other) OR (from_user = other AND to_user = ?) ORDER BY timestamp DESC LIMIT 1),
            (SELECT timestamp FROM messages WHERE (from_user = ? AND to_user = other) OR (from_user = other AND to_user = ?) ORDER BY timestamp DESC LIMIT 1),
            (SELECT COUNT(*) FROM messages WHERE from_user = other AND to_user = ? AND read = 0)
        FROM messages
        WHERE (from_user = ? OR to_user = ?)
        AND other NOT IN (SELECT blocked_user_id FROM blocks WHERE user_id = ?)
        AND other NOT IN (SELECT user_id FROM blocks WHERE blocked_user_id = ?)
        GROUP BY other
    ''', (user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id))
    result = []
    for r in rows:
        other, uname, nn, av, ls, last_text, last_ts, unread = r
        online = (datetime.utcnow() - datetime.fromisoformat(ls)).seconds < 60 if ls else False
        result.append({
            "user_id": other,
            "username": uname,
            "nickname": nn,
            "avatar": av,
            "online": online,
            "last_message": last_text or "",
            "last_time": last_ts,
            "unread": unread
        })
    conn.close()
    return result

@app.get("/messages/{user1}/{user2}")
async def get_messages(user1: int, user2: int):
    conn = sqlite3.connect(DB_PATH)
    if conn.execute("SELECT 1 FROM blocks WHERE (user_id=? AND blocked_user_id=?) OR (user_id=? AND blocked_user_id=?)", (user1, user2, user2, user1)).fetchone():
        conn.close()
        return {"messages": [], "typing": False, "blocked": True}
    conn.execute("UPDATE messages SET read = 1 WHERE from_user = ? AND to_user = ?", (user2, user1))
    conn.commit()
    rows = conn.execute('''
        SELECT from_user, text, file_path, timestamp FROM messages
        WHERE (from_user = ? AND to_user = ?) OR (from_user = ? AND to_user = ?)
        ORDER BY timestamp ASC LIMIT 200
    ''', (user1, user2, user2, user1)).fetchall()
    typing_row = conn.execute("SELECT last_typing FROM typing WHERE user_id = ? AND to_user = ?", (user2, user1)).fetchone()
    conn.close()
    is_typing = False
    if typing_row:
        try:
            is_typing = (datetime.utcnow() - datetime.fromisoformat(typing_row[0])).seconds < 5
        except: pass
    return {"messages": [{"from": r[0], "text": r[1], "file": r[2], "time": r[3]} for r in rows], "typing": is_typing, "blocked": False}

@app.post("/message")
async def send_message(
    from_user: int = Form(...),
    to_user: int = Form(...),
    text: str = Form(""),
    file: UploadFile = File(None)
):
    conn = sqlite3.connect(DB_PATH)
    if conn.execute("SELECT 1 FROM blocks WHERE (user_id=? AND blocked_user_id=?) OR (user_id=? AND blocked_user_id=?)", (from_user, to_user, to_user, from_user)).fetchone():
        conn.close()
        raise HTTPException(403, "blocked")
    if file and file.size > 5 * 1024 * 1024:
        raise HTTPException(400, "file >5mb")
    fpath = None
    if file:
        ext = os.path.splitext(file.filename)[1]
        fname = f"{from_user}_{to_user}_{int(datetime.utcnow().timestamp())}{ext}"
        path = os.path.join(UPLOAD_DIR, fname)
        with open(path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        fpath = fname
    conn.execute("INSERT INTO messages (from_user, to_user, text, file_path, timestamp, read) VALUES (?,?,?,?,?,?)",
                 (from_user, to_user, text or "", fpath, datetime.utcnow().isoformat(), 0))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/typing")
async def set_typing(user_id: int = Form(...), to_user: int = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO typing (user_id, to_user, last_typing) VALUES (?,?,?)",
                 (user_id, to_user, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

app.mount("/avatars", StaticFiles(directory=AVATAR_DIR), name="avatars")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

def backup():
    try:
        subprocess.run(["git", "add", DB_PATH], check=True, capture_output=True)
        subprocess.run(["git", "commit", "--amend", "--no-edit", "--allow-empty"], check=True, capture_output=True)
        subprocess.run(["git", "push", "--force", "origin", "main"], check=True, capture_output=True)
        print("backup ok")
    except Exception as e:
        print("backup fail", e)

sched = BackgroundScheduler()
sched.add_job(backup, trigger=IntervalTrigger(minutes=5))
sched.start()
