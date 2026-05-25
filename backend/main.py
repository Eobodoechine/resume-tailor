from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import os

from routes import auth, profile, resumes, master, tailor, admin

app = FastAPI(title="Resume Tailor", version="1.0.0")

# CORS — allow_origins=["*"] with allow_credentials=True is invalid per spec.
# Restrict to the production domain and local dev.
ALLOWED_ORIGINS = [
    "https://resume-tailor-ogop.onrender.com",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routes
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(resumes.router)
app.include_router(master.router)
app.include_router(tailor.router)
app.include_router(admin.router)

# Serve frontend static files
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

@app.get("/dashboard")
def serve_dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "dashboard.html"))

@app.get("/profile")
def serve_profile():
    return FileResponse(os.path.join(FRONTEND_DIR, "profile.html"))

@app.get("/tailor")
def serve_tailor():
    return FileResponse(os.path.join(FRONTEND_DIR, "tailor.html"))

@app.get("/history")
def serve_history():
    return FileResponse(os.path.join(FRONTEND_DIR, "history.html"))

@app.get("/improve")
def serve_improve():
    return FileResponse(os.path.join(FRONTEND_DIR, "improve.html"))

@app.get("/admin-panel")
def serve_admin():
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))

@app.get("/health")
def health():
    return {"status": "ok"}
