FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY frontend/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

EXPOSE 3000

ENV PORT=3000
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python3", "frontend/server.py"]
