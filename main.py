import os
import sqlite3
import uuid
import shutil
import requests
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import face_recognition
import cloudinary
import cloudinary.api
import cloudinary.uploader
import cloudinary.utils

app = FastAPI(title="Todo Studio Premium B2B SaaS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "todostudio_v2.db"
TEMP_FOLDER = "temp_processing"

if not os.path.exists(TEMP_FOLDER):
    os.makedirs(TEMP_FOLDER)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS studios (
            id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT UNIQUE,
            password TEXT,
            cloud_name TEXT,
            api_key TEXT,
            api_secret TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            studio_id TEXT,
            name TEXT,
            client_email TEXT UNIQUE,
            client_password TEXT,
            cloudinary_prefix TEXT,
            FOREIGN KEY(studio_id) REFERENCES studios(id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.post("/api/studio/register")
async def register_studio(
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    cloud_name: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(...)
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    studio_id = f"studio_{uuid.uuid4().hex[:8]}"
    try:
        cursor.execute(
            "INSERT INTO studios (id, name, email, password, cloud_name, api_key, api_secret) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (studio_id, name, email, password, cloud_name, api_key, api_secret)
        )
        conn.commit()
        return {"status": "success", "studio_id": studio_id}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Email already exists in the platform")
    finally:
        conn.close()

@app.post("/api/studio/login")
async def studio_login(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM studios WHERE email = ? AND password = ?", (email, password))
    studio = cursor.fetchone()
    conn.close()
    if not studio:
        raise HTTPException(status_code=401, detail="Invalid studio credentials")
    return {"status": "success", "studio_id": studio[0], "studio_name": studio[1]}

@app.get("/api/studio/events/{studio_id}")
async def get_studio_events(studio_id: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, client_email FROM events WHERE studio_id = ?",
        (studio_id,)
    )
    events = cursor.fetchall()
    conn.close()
    return {
        "status": "success",
        "events": [{"id": e[0], "name": e[1], "client_email": e[2]} for e in events]
    }

@app.post("/api/studio/create-event")
async def create_event(
    studio_id: str = Form(...),
    event_name: str = Form(...),
    client_email: str = Form(...),
    client_password: str = Form(...)
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    event_id = f"event_{uuid.uuid4().hex[:8]}"
    cloudinary_prefix = f"todostudio_events/{event_id}"
    try:
        cursor.execute(
            "INSERT INTO events (id, studio_id, name, client_email, client_password, cloudinary_prefix) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, studio_id, event_name, client_email, client_password, cloudinary_prefix)
        )
        conn.commit()
        return {"status": "success", "event_id": event_id}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Client email already tied to another event")
    finally:
        conn.close()

@app.post("/api/studio/upload-photo")
async def upload_photo(event_id: str = Form(...), file: UploadFile = File(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT e.cloudinary_prefix, s.cloud_name, s.api_key, s.api_secret
        FROM events e JOIN studios s ON e.studio_id = s.id WHERE e.id = ?
    ''', (event_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    prefix, c_name, a_key, a_secret = row
    try:
        result = cloudinary.uploader.upload(
            file.file,
            folder=prefix,
            cloud_name=c_name,
            api_key=a_key,
            api_secret=a_secret
        )
        return {"status": "success", "url": result.get("secure_url")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/client/login")
@app.post("/api/client/login")
async def client_login(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, cloudinary_prefix FROM events WHERE client_email = ? AND client_password = ?",
        (email, password)
    )
    event = cursor.fetchone()
    conn.close()
    if not event:
        raise HTTPException(status_code=401, detail="Invalid client credentials")
    return {"status": "success", "event_id": event[0], "event_name": event[1]}


@app.get("/api/client/all-photos/{event_id}")
async def get_all_photos(event_id: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT e.cloudinary_prefix, s.cloud_name, s.api_key, s.api_secret
        FROM events e JOIN studios s ON e.studio_id = s.id WHERE e.id = ?
    ''', (event_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    prefix, c_name, a_key, a_secret = row
    cloudinary.config(cloud_name=c_name, api_key=a_key, api_secret=a_secret)
    resources = cloudinary.api.resources(type="upload", prefix=prefix, max_results=100)
    photos = [r["secure_url"] for r in resources.get("resources", [])]
    return {"status": "success", "photos": photos}
