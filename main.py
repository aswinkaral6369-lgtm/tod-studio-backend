import os
import sqlite3
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

app = FastAPI(title="WinLens Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "todostudio_v2.db"
TEMP_FOLDER = "temp_processing"
FACEPP_API_KEY = os.environ.get("FACEPP_API_KEY", "")
FACEPP_API_SECRET = os.environ.get("FACEPP_API_SECRET", "")
FACEPP_BASE = "https://api-us.faceplusplus.com/facepp/v3"

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
            faceset_token TEXT,
            FOREIGN KEY(studio_id) REFERENCES studios(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS photo_faces (
            id TEXT PRIMARY KEY,
            event_id TEXT,
            photo_url TEXT,
            face_token TEXT,
            FOREIGN KEY(event_id) REFERENCES events(id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()


def compress_image(file_bytes: bytes, max_bytes: int = 500000) -> bytes:
    """Properly compress image using Pillow to stay within Face++ 2MB limit."""
    if len(file_bytes) <= max_bytes:
        return file_bytes

    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = img.convert("RGB")  # Remove alpha channel if any (PNG, WEBP etc.)

        # Resize if dimensions are too large
        max_size = (1920, 1920)
        img.thumbnail(max_size, Image.LANCZOS)

        # Reduce quality until under limit
        quality = 85
        while quality >= 20:
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=quality)
            compressed = output.getvalue()
            print(f"Compressed at quality={quality}: {len(compressed)} bytes")
            if len(compressed) <= max_bytes:
                return compressed
            quality -= 10

        # Last resort: resize further
        img.thumbnail((800, 800), Image.LANCZOS)
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=60)
        return output.getvalue()

    except Exception as e:
        print(f"Image compression error: {e}")
        return file_bytes  # Return original if compression fails


def facepp_create_faceset(event_id: str) -> str:
    print(f"Creating FaceSet for event: {event_id}")
    res = requests.post(f"{FACEPP_BASE}/faceset/create", data={
        "api_key": FACEPP_API_KEY,
        "api_secret": FACEPP_API_SECRET,
        "outer_id": event_id,
        "display_name": event_id
    })
    data = res.json()
    print(f"FaceSet create response: {data}")
    if "faceset_token" not in data:
        raise HTTPException(status_code=500, detail=f"FaceSet create failed: {data}")
    return data["faceset_token"]


def facepp_detect_faces(image_url: str) -> list:
    print(f"Detecting faces in: {image_url}")
    print(f"Using API key: {FACEPP_API_KEY[:8]}..." if FACEPP_API_KEY else "NO API KEY!")
    res = requests.post(f"{FACEPP_BASE}/detect", data={
        "api_key": FACEPP_API_KEY,
        "api_secret": FACEPP_API_SECRET,
        "image_url": image_url
    })
    data = res.json()
    print(f"FACEPP DETECT RESPONSE: {data}")
    return [f["face_token"] for f in data.get("faces", [])]


def facepp_add_to_faceset(faceset_token: str, face_tokens: list, retries=3):
    if not face_tokens:
        return
    for attempt in range(retries):
        res = requests.post(f"{FACEPP_BASE}/faceset/addface", data={
            "api_key": FACEPP_API_KEY,
            "api_secret": FACEPP_API_SECRET,
            "faceset_token": faceset_token,
            "face_tokens": ",".join(face_tokens)
        })
        data = res.json()
        print(f"AddFace response: {data}")
        if "error_message" not in data:
            return data
        if data.get("error_message") == "CONCURRENCY_LIMIT_EXCEEDED":
            time.sleep(2)
            continue
        return data
    return data


def facepp_search(faceset_token: str, file_bytes: bytes) -> list:
    print(f"Searching faceset: {faceset_token}, original image size: {len(file_bytes)} bytes")

    # Properly compress instead of raw truncation
    file_bytes = compress_image(file_bytes)
    print(f"Final image size for Face++ search: {len(file_bytes)} bytes")

    res = requests.post(
        f"{FACEPP_BASE}/search",
        data={
            "api_key": FACEPP_API_KEY,
            "api_secret": FACEPP_API_SECRET,
            "faceset_token": faceset_token,
            "return_result_count": 5
        },
        files={"image_file": ("selfie.jpg", file_bytes, "image/jpeg")}
    )
    data = res.json()
    print(f"FACEPP SEARCH RESPONSE: {data}")
    results = data.get("results", [])
    matched = [r["face_token"] for r in results if r.get("confidence", 0) >= 60]
    return matched

# ==========================================
# STUDIO API ROUTES
# ==========================================

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
        raise HTTPException(status_code=400, detail="Email already exists")
    finally:
        conn.close()


@app.post("/api/studio/login")
async def studio_login(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM studios WHERE email=? AND password=?", (email, password))
    studio = cursor.fetchone()
    conn.close()
    if not studio:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"status": "success", "studio_id": studio[0], "studio_name": studio[1]}


@app.get("/api/studio/events/{studio_id}")
async def get_studio_events(studio_id: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, client_email FROM events WHERE studio_id=?", (studio_id,))
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

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO events (id, studio_id, name, client_email, client_password, cloudinary_prefix, faceset_token) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, studio_id, event_name, client_email, client_password, cloudinary_prefix, faceset_token)
        )
        conn.commit()
        return {"status": "success", "event_id": event_id}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Client email already used")
    finally:
        conn.close()


@app.post("/api/studio/upload-photo")
async def upload_photo(event_id: str = Form(...), file: UploadFile = File(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT e.cloudinary_prefix, e.faceset_token,
               s.cloud_name, s.api_key, s.api_secret
        FROM events e JOIN studios s ON e.studio_id = s.id
        WHERE e.id = ?
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
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            for ft in face_tokens:
                cursor.execute(
                    "INSERT INTO photo_faces (id, event_id, photo_url, face_token) VALUES (?, ?, ?, ?)",
                    (uuid.uuid4().hex, event_id, photo_url, ft)
                )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"Face detection warning: {e}")

    return {"status": "success", "url": photo_url, "faces_detected": faces_count}


@app.delete("/api/studio/events/{event_id}")
async def delete_event(event_id: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        # First, delete associated face data (foreign key constraint)
        cursor.execute("DELETE FROM photo_faces WHERE event_id=?", (event_id,))
        # Then, delete the main event
        cursor.execute("DELETE FROM events WHERE id=?", (event_id,))
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
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name FROM events WHERE client_email=? AND client_password=?",
        (email, password)
    )
    event = cursor.fetchone()
    conn.close()
    if not event:
        raise HTTPException(status_code=401, detail="Invalid credentials")
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


@app.post("/api/guest/search")
async def guest_search(event_id: str = Form(...), selfie: UploadFile = File(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT faceset_token FROM events WHERE id=?", (event_id,))
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

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(matched_face_tokens))
    cursor.execute(
        f"SELECT DISTINCT photo_url FROM photo_faces WHERE event_id=? AND face_token IN ({placeholders})",
        [event_id] + matched_face_tokens
    )
    photo_urls = [r[0] for r in cursor.fetchall()]
    conn.close()

    photos = []
    for url in photo_urls:
        download_url = url.replace("/upload/", "/upload/fl_attachment/")
        photos.append({"preview": url, "download": download_url})

    return {"status": "success", "photos": photos}
