@echo off
:: Check for Administrator permissions
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges to configure Windows Firewall...
    powershell -Command "Start-Process cmd -ArgumentList '/k \"%~fn0\"' -Verb RunAs"
    exit /b
)

echo ============================================================
echo   Jarvis Maps AI — Windows Firewall Port Configuration
echo ============================================================
echo.

echo [1/4] Allowing Python 3.11 through Firewall...
netsh advfirewall firewall delete rule name="Jarvis Python 3.11" >nul 2>&1
netsh advfirewall firewall add rule name="Jarvis Python 3.11" dir=in action=allow program="C:\Users\User\AppData\Local\Programs\Python\Python311\python.exe" enable=yes profile=any

echo [2/4] Opening Inbound Port 8080 (Website Portal)...
netsh advfirewall firewall delete rule name="Jarvis Maps AI Web Server (Port 8080)" >nul 2>&1
netsh advfirewall firewall add rule name="Jarvis Maps AI Web Server (Port 8080)" dir=in action=allow protocol=TCP localport=8080 enable=yes profile=any

echo [3/4] Opening Inbound Port 8081 (License Gateway)...
netsh advfirewall firewall delete rule name="Jarvis Maps AI License Server (Port 8081)" >nul 2>&1
netsh advfirewall firewall add rule name="Jarvis Maps AI License Server (Port 8081)" dir=in action=allow protocol=TCP localport=8081 enable=yes profile=any

echo [4/4] Setting Wi-Fi network to Private mode...
powershell -Command "Set-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -NetworkCategory Private -ErrorAction SilentlyContinue"

echo.
echo ============================================================
echo   SUCCESS! Windows Firewall has been configured.
echo.
echo   You can now access from any laptop on your Wi-Fi:
echo   - Website Portal:  http://192.168.1.244:8080
echo   - Admin Panel:     http://192.168.1.244:8080/admin.html
echo   - License Gateway: http://192.168.1.244:8081
echo ============================================================
echo.
pause
