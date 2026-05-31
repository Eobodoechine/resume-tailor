FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
#
# LibreOffice headless  — DOCX → PDF via the libreoffice engine (default).
# lxml deps             — libxml2/libxslt for fast XML parsing in fde_docx.py.
# fonts-liberation      — Liberation Sans TTF files embedded in HTML template
#                         as base64 @font-face so Playwright Chrome renders
#                         identically to the DOCX template on any server.
# Playwright Chromium deps — required for headless Chrome on Debian/Ubuntu.
#   (Installed here as system packages; playwright installs the browser binary
#    separately via `playwright install chromium` in the pip layer below.)
#
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libreoffice-java-common \
    default-jre-headless \
    libxml2 \
    libxslt1.1 \
    fonts-liberation \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser binary (Chromium only — keeps image size down).
# Must run after pip install so the playwright CLI is available.
RUN playwright install chromium

# Copy backend and frontend
COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
