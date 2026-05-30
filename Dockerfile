FROM python:3.11-slim

# ── System deps ──────────────────────────────────────────────────────
# curl is required for the docker-compose health-check:
#   test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    gcc \
    g++ \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Ensure PYTHONPATH includes /app so all local packages resolve ────
ENV PYTHONPATH=/app

# ── Python deps ──────────────────────────────────────────────────────
# Install torch CPU-only separately first — large download, own cache layer
RUN pip install --no-cache-dir \
    torch==2.3.0 \
    torchvision==0.18.0 \
    --index-url https://download.pytorch.org/whl/cpu

# Install torchreid (OSNet person-ReID model zoo) from GitHub.
# Install torchreid (OSNet person-ReID model zoo) from GitHub.
# scipy, cv2, gdown, and tensorboard must be installed BEFORE torchreid — its setup.py imports them at metadata time.
RUN pip install --no-cache-dir \
    Cython \
    h5py \
    scipy \
    opencv-python-headless \
    gdown \
    tensorboard \
    && pip install --no-cache-dir \
    git+https://github.com/KaiyangZhou/deep-person-reid.git

# Copy and install all project dependencies from requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────────
COPY . .

# Ensure runtime directories exist
RUN mkdir -p data footage

# ── Runtime ──────────────────────────────────────────────────────────
EXPOSE 8000

# Healthcheck at the Dockerfile level (docker-compose overrides for interval/retries)
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Start the Store Intelligence API via uvicorn
# --factory tells uvicorn that create_app() returns a FastAPI instance
CMD ["python", "-m", "uvicorn", "api.server:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--log-level", "info", \
     "--no-access-log"]
