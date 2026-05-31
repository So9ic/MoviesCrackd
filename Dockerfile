FROM python:3.10-slim

# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CLOUD_MODE=true
ENV PORT=7860

# Create a non-root user with UID 1000
RUN useradd -m -u 1000 user

WORKDIR /app

# Install system dependencies if any are needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and set ownership to non-root user
COPY --chown=user:user . .

# Switch to the non-root user
USER user

# Hugging Face Spaces runs on port 7860 by default
EXPOSE 7860

# Command to run our python server
CMD ["python", "web_downloader.py"]
