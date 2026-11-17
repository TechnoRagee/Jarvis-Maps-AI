@echo off
title Jarvis Maps AI — Web Portal
echo Starting local web server on port 8080...
start "" "http://localhost:8080/#home"
python -m http.server 8080 --directory "%~dp0"
pause
