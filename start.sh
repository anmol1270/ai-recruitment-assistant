#!/bin/bash
# Start script for Railway deployment
# Handles Railway's dynamic PORT assignment

echo "🚀 Starting AI Recruitment Caller..."
echo "PORT: ${PORT:-8000}"
echo "HOST: ${HOST:-0.0.0.0}"

# Use Railway's PORT or default to 8000
export PORT=${PORT:-8000}
export HOST=${HOST:-0.0.0.0}

exec uvicorn app.server:app --host $HOST --port $PORT
