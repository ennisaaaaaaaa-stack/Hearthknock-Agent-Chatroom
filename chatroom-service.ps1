# chatroom-service.ps1 - idempotent launcher/watchdog for the family chatroom.
# Starts server.py (port 7788) and doorman.py if they are not already running.
# Safe to run any number of times; scheduled task calls it at logon + every 5 min.
# KEEP THIS FILE PURE ASCII. PS5.1 reads BOM-less ps1 as GBK; CJK bytes can form
# a backtick that silently eats the next line. The family has been bitten. Twice.

$ErrorActionPreference = "Continue"
$root = "C:\Users\<winuser>\Documents\kimi\workspace\chatroom"
$pyw  = "C:\Users\<winuser>\AppData\Local\Programs\Python\Python314\pythonw.exe"
$logFile = Join-Path $root "chatroom-service.log"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

# --- server.py: probe TCP 7788 ---
$serverUp = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $iar = $client.BeginConnect("127.0.0.1", 7788, $null, $null)
    $serverUp = ($iar.AsyncWaitHandle.WaitOne(1500) -and $client.Connected)
    $client.Close()
} catch { $serverUp = $false }

if ($serverUp) {
    Log "server already up on 7788, skip"
} else {
    Start-Process -FilePath $pyw -ArgumentList "server.py --port 7788" -WorkingDirectory $root -WindowStyle Hidden
    Log "server.py started"
}

# --- doorman.py: heartbeat-based liveness ---
# A live process with a dead patrol loop (zombie) has happened. Process existence
# proves nothing; the heartbeat file does. Stale or missing heartbeat -> kill + restart.
$hb = Join-Path $root "doorman.heartbeat"
$proc = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'doorman\.py' })
$alive = $false
if ($proc.Count -gt 0 -and (Test-Path $hb)) {
    $ageMin = ((Get-Date) - (Get-Item $hb).LastWriteTime).TotalMinutes
    if ($ageMin -lt 4) { $alive = $true }
}

if ($alive) {
    Log "doorman alive (heartbeat fresh), skip"
} else {
    # v7.2 P0 migration gate: never start the doorman while the server reports
    # migration_ready=false or cursor coverage gaps. A doorman started during the
    # cutover window would write state/heartbeat and could erase the very cursors
    # migrate_cursors_v7.py still needs to migrate. Same gate as doorman.py itself.
    $gateOk = $false
    try {
        $st = Invoke-RestMethod -Uri "http://localhost:7788/api/cursors/status" -TimeoutSec 5
        $gateOk = [bool]$st.ready
    } catch { $gateOk = $false }
    if (-not $gateOk) {
        Log "migration gate NOT passed (status unreachable or ready=false) - doorman NOT started; run migrate_cursors_v7.py first"
    } else {
        if ($proc.Count -gt 0) {
            $proc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Log "doorman zombie killed (heartbeat stale or missing)"
        }
        Start-Process -FilePath $pyw -ArgumentList "doorman.py" -WorkingDirectory $root -WindowStyle Hidden
        Log "doorman.py started"
    }
}

# --- corpse-watch.py: session coroner ---
# Finds violently-killed kimi-code sessions (no obituary + wire ends mid-turn +
# wire silent) and posts one death notice per corpse to the chatroom. Idempotent:
# reported session ids are recorded in ~/.kimi-code/tideline-state/session-ledger.jsonl.
Start-Process -FilePath $pyw -ArgumentList "corpse-watch.py" -WorkingDirectory $root -WindowStyle Hidden
Log "corpse-watch dispatched"
