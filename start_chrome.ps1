# Запускает Chrome с удалённой отладкой (CDP), чтобы Python читал уже открытую
# вкладку. Профиль сохраняется в .chrome-profile — вход на сайт делается один раз.
#
# Использование:
#   .\start_chrome.ps1
#   .\start_chrome.ps1 -Browser edge
#   .\start_chrome.ps1 -Browser yandex
#   .\start_chrome.ps1 -Port 9223  # отдельный профиль для второго экземпляра
#   .\start_chrome.ps1 -Force   # закрыть chrome.exe и запустить заново

param(
    [ValidateSet("chrome", "edge", "yandex")]
    [string]$Browser = "chrome",
    [int]$Port = 9222,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$profileName = if ($Port -eq 9222) { ".chrome-profile" } else { ".chrome-profile-$Port" }
$profileDir = Join-Path $projectRoot $profileName

function Test-CdpReady {
    param([int]$CheckPort)
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$CheckPort/json/version" -UseBasicParsing -TimeoutSec 2
        return ($resp.StatusCode -eq 200 -and $resp.Content -match '"Browser"')
    } catch {
        return $false
    }
}

function Test-PortOpen {
    param([int]$CheckPort)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect("127.0.0.1", $CheckPort, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(400)
        $client.Close()
        return $ok
    } catch {
        return $false
    }
}

function Find-BrowserPath {
    param([string]$Name)

    $candidates = @()
    switch ($Name) {
        "chrome" {
            $candidates = @(
                "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
            )
        }
        "edge" {
            $candidates = @(
                "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
                "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
            )
        }
        "yandex" {
            $candidates = @(
                "$env:LOCALAPPDATA\Yandex\YandexBrowser\Application\browser.exe",
                "$env:ProgramFiles\Yandex\YandexBrowser\Application\browser.exe"
            )
        }
    }

    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) {
            return $path
        }
    }
    return $null
}

function Stop-ChromeProcesses {
    Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

if (Test-CdpReady -CheckPort $Port) {
    Write-Host "CDP уже работает: http://127.0.0.1:$Port/json/version"
    Write-Host "Откройте нужный сайт и запустите: python main.py inspect"
    exit 0
}

if ((Test-PortOpen -CheckPort $Port) -and -not (Test-CdpReady -CheckPort $Port)) {
    Write-Host "Внимание: порт $Port занят, но CDP не отвечает."
    Write-Host "Скорее всего Chrome запущен БЕЗ --user-data-dir (обычный профиль)."
    if (-not $Force) {
        Write-Host "Повторите с -Force, чтобы закрыть chrome.exe и запустить отладочный Chrome:"
        Write-Host "  .\start_chrome.ps1 -Force"
        exit 1
    }
    Stop-ChromeProcesses
}

if ($Force) {
    Stop-ChromeProcesses
}

$exe = Find-BrowserPath -Name $Browser
if (-not $exe) {
    Write-Error "Не найден исполняемый файл браузера '$Browser'."
}

New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

Write-Host "Запуск: $exe"
Write-Host "Профиль: $profileDir"
Write-Host "CDP:     http://127.0.0.1:$Port"
Write-Host ""
Write-Host "Важно: нужны ОБА параметра --remote-debugging-port и --user-data-dir"
Write-Host ""
Write-Host "1) Откройте нужный сайт и авторизуйтесь (один раз)."
Write-Host "2) python main.py check   — проверить подключение"
Write-Host "3) python main.py web     — интерфейс мониторинга"

$browserArgs = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=$profileDir",
    "--no-first-run",
    "--no-default-browser-check"
)

Start-Process -FilePath $exe -ArgumentList $browserArgs
Start-Sleep -Seconds 2

if (Test-CdpReady -CheckPort $Port) {
    Write-Host "CDP готов: http://127.0.0.1:$Port/json/version"
} else {
    Write-Host "CDP пока не отвечает. Закройте все окна Chrome и запустите: .\start_chrome.ps1 -Force"
}
