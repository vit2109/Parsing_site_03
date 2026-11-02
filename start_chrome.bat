@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem Запуск Chrome с CDP. PowerShell не нужен.
rem   start_chrome.bat
rem   start_chrome.bat 9223  (отдельный профиль второго экземпляра)
rem   start_chrome.bat force

set "PORT=%~1"
if not defined PORT set "PORT=9222"
if /I "%PORT%"=="force" set "PORT=9222"
if /I "%PORT%"=="-force" set "PORT=9222"
set "ROOT=%~dp0"
set "PROFILE=%ROOT%.chrome-profile"
if not "%PORT%"=="9222" set "PROFILE=%ROOT%.chrome-profile-%PORT%"

if /I "%~1"=="force" goto force
if /I "%~1"=="-force" goto force

curl -s -o nul -w "%%{http_code}" "http://127.0.0.1:%PORT%/json/version" 2>nul | findstr "200" >nul
if not errorlevel 1 (
    echo CDP уже работает: http://127.0.0.1:%PORT%/json/version
    echo Откройте сайт и запустите: python main.py check
    exit /b 0
)

goto launch

:force
echo Закрываю chrome.exe...
taskkill /F /IM chrome.exe >nul 2>&1
timeout /t 2 /nobreak >nul

:launch
set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if not defined CHROME (
    echo Не найден chrome.exe
    exit /b 1
)

if not exist "%PROFILE%" mkdir "%PROFILE%"

echo Запуск: %CHROME%
echo Профиль: %PROFILE%
echo CDP: http://127.0.0.1:%PORT%
echo.
echo Нужны оба параметра: --remote-debugging-port и --user-data-dir
echo.

start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check

timeout /t 2 /nobreak >nul
echo Проверка: http://127.0.0.1:%PORT%/json/version
echo Далее: python main.py check

endlocal
