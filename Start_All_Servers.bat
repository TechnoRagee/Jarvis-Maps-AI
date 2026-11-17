@echo off
title Jarvis Maps AI — Server Controller
echo ============================================================
echo   Jarvis Maps AI — Starting Servers
echo ============================================================
echo.

echo [1/2] Launching License and Security Gateway on port 8081...
start "Jarvis License Gateway (Port 8081)" cmd /k "python server/license_server.py 8081"

timeout /t 2 /nobreak >nul

echo [2/2] Launching Website Portal on port 8080 (0.0.0.0 LAN)...
start "Jarvis Website Portal (Port 8080)" cmd /k "python -m http.server 8080 --bind 0.0.0.0 --directory Website"

timeout /t 2 /nobreak >nul

echo.
echo Opening Admin Panel in default browser...
start "" "http://localhost:8080/admin.html"

echo.
echo ============================================================
echo   All servers are active!
echo   - Local Access:     http://localhost:8080
echo   - Admin Panel:      http://localhost:8080/admin.html
echo   - LAN (Other PCs):  http://192.168.1.244:8080
echo   - License Gateway:  http://192.168.1.244:8081
echo ============================================================
echo.
pause
