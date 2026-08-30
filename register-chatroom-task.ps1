# register-chatroom-task.ps1 - register the chatroom watchdog in Task Scheduler.
# Fires at user logon and then every 5 minutes; chatroom-service.ps1 is
# idempotent so repeat fires are cheap no-ops. Same pattern as KimiCode-Autostart.
# Run once as the user (no admin needed). KEEP THIS FILE PURE ASCII.

$ps = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
$launcher = "C:\Users\<winuser>\Documents\kimi\workspace\chatroom\chatroom-service.ps1"

$action = New-ScheduledTaskAction -Execute $ps `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""

$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Chatroom-Autostart" -Action $action -Trigger @($t1, $t2) `
    -Principal $principal -Description "family chatroom server + doorman watchdog (logon + 5min)" -Force | Out-Null

Write-Output "registered: Chatroom-Autostart (at logon of $env:USERNAME, then every 5 min)"
