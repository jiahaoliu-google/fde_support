# Stage 1: Build Vite + React + TypeScript + Tailwind static bundle
FROM node:20-slim AS ui-builder

WORKDIR /ui
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

# Stage 2: FastAPI BFF + Static Asset Server
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

# Copy compiled Vite bundle from Stage 1
COPY --from=ui-builder /ui/dist /app/frontend/dist

EXPOSE 3000

ENV PORT=3000
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python3", "frontend/server.py"]
