"""File storage backend: FastAPI + PostgreSQL + MinIO + Redis + JWT. Everything in one file."""
import os, json, time, hashlib, secrets, datetime as dt
from contextlib import asynccontextmanager

import jwt, redis
from minio import Minio
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File as FormFile, Form, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------- 1. CONFIG (all values come from docker-compose env vars) ----------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@db:5432/filestore")
REDIS_URL    = os.getenv("REDIS_URL", "redis://redis:6379/0")
MINIO_HOST   = os.getenv("MINIO_HOST", "minio:9000")
MINIO_USER   = os.getenv("MINIO_USER", "minioadmin")
MINIO_PASS   = os.getenv("MINIO_PASS", "minioadmin")
BUCKET       = "files"
JWT_SECRET   = os.getenv("JWT_SECRET", "change-me")

# ---------- 2. CONNECTIONS ----------
engine  = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
cache   = redis.from_url(REDIS_URL, decode_responses=True)
store   = Minio(MINIO_HOST, MINIO_USER, MINIO_PASS, secure=False)

# ---------- 3. DATABASE TABLES (metadata + access control via owner_id) ----------
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)

class Folder(Base):
    __tablename__ = "folders"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    parent_id = Column(Integer, ForeignKey("folders.id"), nullable=True)  # null = root
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)

class File(Base):
    __tablename__ = "files"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    size = Column(BigInteger)
    content_type = Column(String)
    checksum = Column(String)                       # filled in later by a background task
    object_key = Column(String, nullable=False)     # where the bytes live in MinIO
    folder_id = Column(Integer, ForeignKey("folders.id"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

def file_dict(f):
    return {"id": f.id, "name": f.name, "size": f.size, "content_type": f.content_type,
            "checksum": f.checksum, "folder_id": f.folder_id, "created_at": f.created_at.isoformat()}

def folder_dict(f):
    return {"id": f.id, "name": f.name, "parent_id": f.parent_id}

# ---------- 4. STARTUP: wait for DB, create tables, create bucket ----------
@asynccontextmanager
async def lifespan(app):
    for _ in range(30):                              # DB may take a few seconds to boot
        try:
            Base.metadata.create_all(engine); break
        except Exception:
            time.sleep(1)
    for _ in range(30):                              # same for MinIO
        try:
            if not store.bucket_exists(BUCKET):
                store.make_bucket(BUCKET)
            break
        except Exception:
            time.sleep(1)
    yield

app = FastAPI(title="File Storage", lifespan=lifespan)

def get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()

# ---------- 5. AUTH: passwords + JWT ----------
oauth2 = OAuth2PasswordBearer(tokenUrl="login")

def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()

def check_password(pw, stored):
    return hash_password(pw, stored.split("$")[0]) == stored

def current_user(token: str = Depends(oauth2), db=Depends(get_db)):
    """Runs on EVERY protected endpoint: validates the JWT and returns the user."""
    try:
        user_id = int(jwt.decode(token, JWT_SECRET, algorithms=["HS256"])["sub"])
    except Exception:
        raise HTTPException(401, "Invalid or expired token")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(401, "User not found")
    return user

def get_owned(db, Model, obj_id, user):
    """Access control: only returns the object if it belongs to this user."""
    obj = db.get(Model, obj_id)
    if not obj or obj.owner_id != user.id:
        raise HTTPException(404, "Not found")        # 404 (not 403) so we don't leak existence
    return obj

class Credentials(BaseModel):
    username: str
    password: str

@app.post("/register")
def register(c: Credentials, db=Depends(get_db)):
    if db.query(User).filter_by(username=c.username).first():
        raise HTTPException(400, "Username taken")
    db.add(User(username=c.username, password_hash=hash_password(c.password)))
    db.commit()
    return {"message": "registered"}

@app.post("/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    user = db.query(User).filter_by(username=form.username).first()
    if not user or not check_password(form.password, user.password_hash):
        raise HTTPException(401, "Wrong username or password")
    exp = dt.datetime.utcnow() + dt.timedelta(hours=1)
    token = jwt.encode({"sub": str(user.id), "exp": exp}, JWT_SECRET, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer"}

# ---------- 6. REDIS CACHE helpers ----------
def cached(key):
    v = cache.get(key)
    return json.loads(v) if v else None

def save_cache(key, data, ttl=60):
    cache.setex(key, ttl, json.dumps(data))

def clear_cache(user_id):
    """Any change wipes this user's cached reads (simple and always correct)."""
    for k in cache.scan_iter(f"user:{user_id}:*"):
        cache.delete(k)

# ---------- 7. BACKGROUND TASKS (run after the response is already sent) ----------
def compute_checksum(file_id):
    db = Session()
    f = db.get(File, file_id)
    obj = store.get_object(BUCKET, f.object_key)
    h = hashlib.sha256()
    for chunk in obj.stream(64 * 1024):
        h.update(chunk)
    obj.close(); obj.release_conn()
    f.checksum = h.hexdigest()
    db.commit(); db.close()
    clear_cache(f.owner_id)

def delete_object(object_key):
    store.remove_object(BUCKET, object_key)

# ---------- 8. FOLDERS ----------
class FolderIn(BaseModel):
    name: str
    parent_id: int | None = None

@app.post("/folders")
def create_folder(body: FolderIn, user=Depends(current_user), db=Depends(get_db)):
    if body.parent_id:
        get_owned(db, Folder, body.parent_id, user)
    folder = Folder(name=body.name, parent_id=body.parent_id, owner_id=user.id)
    db.add(folder); db.commit()
    clear_cache(user.id)
    return folder_dict(folder)

@app.delete("/folders/{folder_id}")
def delete_folder(folder_id: int, user=Depends(current_user), db=Depends(get_db)):
    folder = get_owned(db, Folder, folder_id, user)
    if db.query(File).filter_by(folder_id=folder_id).first() or db.query(Folder).filter_by(parent_id=folder_id).first():
        raise HTTPException(400, "Folder is not empty")
    db.delete(folder); db.commit()
    clear_cache(user.id)
    return {"message": "deleted"}

@app.get("/contents")
def list_contents(folder_id: int | None = None, user=Depends(current_user), db=Depends(get_db)):
    """List the folders and files inside a folder (omit folder_id for the root). Cached."""
    key = f"user:{user.id}:contents:{folder_id}"
    if (hit := cached(key)):
        return hit
    if folder_id:
        get_owned(db, Folder, folder_id, user)
    folders = db.query(Folder).filter_by(owner_id=user.id, parent_id=folder_id).all()
    files   = db.query(File).filter_by(owner_id=user.id, folder_id=folder_id).all()
    data = {"folders": [folder_dict(f) for f in folders], "files": [file_dict(f) for f in files]}
    save_cache(key, data)
    return data

# ---------- 9. FILES ----------
@app.post("/files")
def upload(bg: BackgroundTasks, file: UploadFile = FormFile(...), folder_id: int | None = Form(None),
           user=Depends(current_user), db=Depends(get_db)):
    if folder_id:
        get_owned(db, Folder, folder_id, user)
    file.file.seek(0, 2); size = file.file.tell(); file.file.seek(0)
    key = f"{user.id}/{secrets.token_hex(16)}"                                   # unique name in MinIO
    store.put_object(BUCKET, key, file.file, length=size, content_type=file.content_type)  # bytes -> MinIO
    row = File(name=file.filename, size=size, content_type=file.content_type,
               object_key=key, folder_id=folder_id, owner_id=user.id)            # metadata -> Postgres
    db.add(row); db.commit()
    clear_cache(user.id)
    bg.add_task(compute_checksum, row.id)                                        # don't make the user wait
    return file_dict(row)

@app.get("/files/{file_id}")
def file_metadata(file_id: int, user=Depends(current_user), db=Depends(get_db)):
    key = f"user:{user.id}:file:{file_id}"
    if (hit := cached(key)):
        return hit
    data = file_dict(get_owned(db, File, file_id, user))
    save_cache(key, data)
    return data

@app.get("/files/{file_id}/download")
def download(file_id: int, user=Depends(current_user), db=Depends(get_db)):
    f = get_owned(db, File, file_id, user)
    obj = store.get_object(BUCKET, f.object_key)
    def stream():
        try:
            yield from obj.stream(64 * 1024)
        finally:
            obj.close(); obj.release_conn()
    return StreamingResponse(stream(), media_type=f.content_type or "application/octet-stream",
                             headers={"Content-Disposition": f'attachment; filename="{f.name}"'})

@app.put("/files/{file_id}/move")
def move_file(file_id: int, folder_id: int | None = None, user=Depends(current_user), db=Depends(get_db)):
    f = get_owned(db, File, file_id, user)
    if folder_id:
        get_owned(db, Folder, folder_id, user)
    f.folder_id = folder_id
    db.commit()
    clear_cache(user.id)
    return file_dict(f)

@app.delete("/files/{file_id}")
def delete_file(file_id: int, bg: BackgroundTasks, user=Depends(current_user), db=Depends(get_db)):
    f = get_owned(db, File, file_id, user)
    key = f.object_key
    db.delete(f); db.commit()
    clear_cache(user.id)
    bg.add_task(delete_object, key)                  # remove bytes from MinIO after responding
    return {"message": "deleted"}
