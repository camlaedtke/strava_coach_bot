FROM python:3.11-slim

WORKDIR /app

# Copy requirements first so this layer is cached across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8080

# Use sh -c so ${PORT:-8080} shell expansion works at runtime.
# Cloud Run injects $PORT; local runs fall back to 8080.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
