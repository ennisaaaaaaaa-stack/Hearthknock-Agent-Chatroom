# register-chatroom-task.ps1 - register the chatroom watchdog in Task Scheduler.
# Fires at user logon and then every 5 minutes; chatroom-service.ps1 is
# idempotent so repeat fires are cheap no-ops. Same pattern as KimiCode-Autostart.
# Run once as the user (no admin needed). KEEP THIS FILE PURE ASCII.

$ps = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
$wscript = "C:\Windows\System32\wscript.exe"
$vbs = "C:\Users\<winuser>\Documents\kimi\workspace\tideline-memory\kimi-code\run-hidden.vbs"
$launcher = "C:\Users\<winuser>\Documents\kimi\workspace\chatroom\chatroom-service.ps1"

# Route through wscript + run-hidden.vbs: launching powershell.exe directly from
# an interactive-task action flashes a console window every 5 minutes.
$action = New-ScheduledTaskAction -Execute $wscript `
    -Argument "`"$vbs`" `"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$launcher`""

$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Chatroom-Autostart" -Action $action -Trigger @($t1, $t2) `
    -Principal $principal -Description "family chatroom server + doorman watchdog (logon + 5min)" -Force | Out-Null

Write-Output "registered: Chatroom-Autostart (at logon of $env:USERNAME, then every 5 min)"
