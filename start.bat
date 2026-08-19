@echo off
cd /d "C:\Users\ADMIN\Documents\code\veda-dashboard"
start "" http://localhost:8765
python -m uvicorn main:app --host 0.0.0.0 --port 8765
