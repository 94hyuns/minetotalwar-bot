# 버그 이슈 감시(tools\issue_watch.ps1)를 Windows 작업 스케줄러에 등록/해제 (설계 §33 깨우기 B안).
# 로그온하면 숨김 창으로 시작하고, 출력은 tools\logs\issue_watch.log 에 쌓인다.
#   등록: powershell -ExecutionPolicy Bypass -File tools\install_issue_watch.ps1
#   해제: powershell -ExecutionPolicy Bypass -File tools\install_issue_watch.ps1 -Uninstall
# 이 파일은 한글 때문에 UTF-8(BOM) 으로 저장한다.
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$taskName = 'MineTotalWar IssueWatch'
$root = Split-Path -Parent $PSScriptRoot
$watch = Join-Path $PSScriptRoot 'issue_watch.ps1'
$log = Join-Path $PSScriptRoot 'logs\issue_watch.log'

if ($Uninstall) {
	Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
	Write-Host "작업을 해제했습니다: $taskName"
	return
}

New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
$command = "& '$watch' *>> '$log'"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $root `
	-Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$command`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# 배터리·유휴 조건에 멈추지 않고, 시간 제한 없이 돈다
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description '영지전 버그 이슈 감시 (tools\issue_watch.ps1)' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "등록하고 지금 시작했습니다: $taskName (로그: $log)"
