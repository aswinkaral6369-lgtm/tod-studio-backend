import os
import uuid
import requests
import io
import time
from PIL import Image
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import cloudinary
import cloudinary.api
import cloudinary.uploader
import psycopg2
from psycopg2 import IntegrityError

app = FastAPI(title="WinLens Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FACEPP_API_KEY = os.environ.get("FACEPP_API_KEY", "")
FACEPP_API_SECRET = os.environ.get("FACEPP_API_SECRET", "")
FACEPP_BASE = "https://api-us.faceplusplus.com/facepp/v3"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL: return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS studios (id TEXT PRIMARY KEY, name TEXT, email TEXT UNIQUE, password TEXT, cloud_name TEXT, api_key TEXT, api_secret TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, studio_id TEXT, name TEXT, client_email TEXT UNIQUE, client_password TEXT, cloudinary_prefix TEXT, faceset_token TEXT, FOREIGN KEY(studio_id) REFERENCES studios(id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS photo_faces (id TEXT PRIMARY KEY, event_id TEXT, photo_url TEXT, face_token TEXT, FOREIGN KEY(event_id) REFERENCES events(id))')
    conn.commit()
    conn.close()

init_db()

# --- Face++ Helper Functions ---
def compress_image(file_bytes: bytes, max_bytes: int = 500000) -> bytes:
    if len(file_bytes) <= max_bytes: return file_bytes
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    img.thumbnail((1920, 1920), Image.LANCZOS)
    quality = 85
    while quality >= 20:
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality)
        if output.tell() <= max_bytes: return output.getvalue()
        quality -= 10
    return file_bytes

def facepp_create_faceset(event_id: str) -> str:
    res = requests.post(f"{FACEPP_BASE}/faceset/create", data={"api_key": FACEPP_API_KEY, "api_secret": FACEPP_API_SECRET, "outer_id": event_id})
    return res.json().get("faceset_token")

def facepp_add_to_faceset(faceset_token: str, face_tokens: list):
    for attempt in range(3):
        res = requests.post(f"{FACEPP_BASE}/faceset/addface", data={"api_key": FACEPP_API_KEY, "api_secret": FACEPP_API_SECRET, "faceset_token": faceset_token, "face_tokens": ",".join(face_tokens)})
        if "error_message" not in res.json(): return
        time.sleep(2)

# --- Routes ---
@app.post("/api/studio/register")
async def register(name: str=Form(...), email: str=Form(...), password: str=Form(...), cloud_name: str=Form(...), api_key: str=Form(...), api_secret: str=Form(...)):
    conn = get_db_connection()
    try:
        conn.cursor().execute("INSERT INTO studios VALUES (%s, %s, %s, %s, %s, %s, %s)", (f"studio_{uuid.uuid4().hex[:8]}", name, email, password, cloud_name, api_key, api_secret))
        conn.commit()
        return {"status": "success"}
    finally: conn.close()

@app.post("/api/studio/create-event")
async def create_event(studio_id: str=Form(...), event_name: str=Form(...), client_email: str=Form(...), client_password: str=Form(...)):
    event_id = f"event_{uuid.uuid4().hex[:8]}"
    token = facepp_create_faceset(event_id)
    conn = get_db_connection()
    conn.cursor().execute("INSERT INTO events VALUES (%s, %s, %s, %s, %s, %s, %s)", (event_id, studio_id, event_name, client_email, client_password, f"todostudio/{event_id}", token))
    conn.commit()
    conn.close()
    return {"status": "success", "event_id": event_id}

@app.post("/api/studio/upload-photo")
async def upload_photo(event_id: str=Form(...), file: UploadFile=File(...)):
    # Cloudinary upload + Face Detection logic here
    return {"status": "success"}

@app.delete("/api/studio/events/{event_id}")
async def delete_event(event_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM photo_faces WHERE event_id=%s", (event_id,))
    cursor.execute("DELETE FROM events WHERE id=%s", (event_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/guest/search")
async def search(event_id: str=Form(...), selfie: UploadFile=File(...)):
    # Compression + Face++ Search Logic
    return {"status": "success", "photos": []}


@app.get("/api/studio/events/{studio_id}")
async def get_studio_events(studio_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, client_email FROM events WHERE studio_id=%s", (studio_id,))
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
    event_id = f"event_{uuid.uuid4().hex[:8]}"
    cloudinary_prefix = f"todostudio_events/{event_id}"
    faceset_token = facepp_create_faceset(event_id)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO events (id, studio_id, name, client_email, client_password, cloudinary_prefix, faceset_token) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (event_id, studio_id, event_name, client_email, client_password, cloudinary_prefix, faceset_token)
        )
        conn.commit()
        return {"status": "success", "event_id": event_id}
    except IntegrityError:
        raise HTTPException(status_code=400, detail="Client email already used")
    finally:
        conn.close()


@app.post("/api/studio/upload-photo")
async def upload_photo(event_id: str = Form(...), file: UploadFile = File(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT e.cloudinary_prefix, e.faceset_token,
               s.cloud_name, s.api_key, s.api_secret
        FROM events e JOIN studios s ON e.studio_id = s.id
        WHERE e.id = %s
    ''', (event_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")

    prefix, faceset_token, c_name, a_key, a_secret = row

    try:
        file_bytes = await file.read()
        result = cloudinary.uploader.upload(
            file_bytes,
            folder=prefix,
            cloud_name=c_name,
            api_key=a_key,
            api_secret=a_secret
        )
        photo_url = result.get("secure_url")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {e}")

    faces_count = 0
    try:
        face_tokens = facepp_detect_faces(photo_url)
        faces_count = len(face_tokens)
        if face_tokens:
            facepp_add_to_faceset(faceset_token, face_tokens)
            conn = get_db_connection()
            cursor = conn.cursor()
            for ft in face_tokens:
                cursor.execute(
                    "INSERT INTO photo_faces (id, event_id, photo_url, face_token) VALUES (%s, %s, %s, %s)",
                    (uuid.uuid4().hex, event_id, photo_url, ft)
                )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"Face detection warning: {e}")

    return {"status": "success", "url": photo_url, "faces_detected": faces_count}


@app.delete("/api/studio/events/{event_id}")
async def delete_event(event_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM photo_faces WHERE event_id=%s", (event_id,))
        cursor.execute("DELETE FROM events WHERE id=%s", (event_id,))
        conn.commit()
        return {"status": "success", "message": "Event deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ==========================================
# CLIENT & GUEST API ROUTES
# ==========================================

@app.post("/api/client/login")
async def client_login(email: str = Form(...), password: str = Form(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name FROM events WHERE client_email=%s AND client_password=%s",
        (email, password)
    )
    event = cursor.fetchone()
    conn.close()
    if not event:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"status": "success", "event_id": event[0], "event_name": event[1]}


@app.get("/api/client/all-photos/{event_id}")
async def get_all_photos(event_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT e.cloudinary_prefix, s.cloud_name, s.api_key, s.api_secret
        FROM events e JOIN studios s ON e.studio_id = s.id WHERE e.id = %s
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


@app.post("/api/guest/search")
async def guest_search(event_id: str = Form(...), selfie: UploadFile = File(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT faceset_token FROM events WHERE id=%s", (event_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Event or FaceSet not found")
    faceset_token = row[0]

    selfie_bytes = await selfie.read()

    try:
        matched_face_tokens = facepp_search(faceset_token, selfie_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Face search failed: {e}")

    if not matched_face_tokens:
        return {"status": "success", "photos": []}

    conn = get_db_connection()
    cursor = conn.cursor()
    placeholders = ",".join(["%s"] * len(matched_face_tokens))
    cursor.execute(
        f"SELECT DISTINCT photo_url FROM photo_faces WHERE event_id=%s AND face_token IN ({placeholders})",
        [event_id] + matched_face_tokens
    )
    photo_urls = [r[0] for r in cursor.fetchall()]
    conn.close()

    photos = []
    for url in photo_urls:
        download_url = url.replace("/upload/", "/upload/fl_attachment/")
        photos.append({"preview": url, "download": download_url})

    return {"status": "success", "photos": photos}
