# 버그 이슈 감시 (설계 §33 제보 처리 "깨우기" B안). 1분마다 GitHub 에서 열린 `버그` 이슈를 보고(토큰 사용 없음),
# 아직 손대지 않은 새 이슈가 있으면 이 저장소 폴더에서 Claude Code 를 한 번 실행해 docs/운영-제보처리.md 절차대로 처리하게 한다.
# A안(Claude Code 디스코드 채널 플러그인)은 플러그인이 봇·웹훅이 쓴 메시지를 전부 버려서(server.ts: if (msg.author.bot) return) 쓸 수 없다.
#
# 사용: powershell -ExecutionPolicy Bypass -File tools\issue_watch.ps1
#   -ClaudeArgs  claude 에 더 넘길 인자. 기본은 편집만 자동 허용이라 빌드·ssh·배포 명령에서 멈춘다 —
#                무엇을 자동 허용할지는 운영자가 정한다 (예: -ClaudeArgs '--allowedTools', 'Bash(./gradlew *)' ...).
#   -Once        한 번만 확인하고 끝 (시험용)
# 필요: gh(로그인), claude(Claude Code CLI). 처리한 이슈 번호는 tools\.issue_watch.json, 로그는 tools\logs\issue-<번호>.log (둘 다 깃 미포함)
# 이 파일은 한글 때문에 UTF-8(BOM) 으로 저장한다.
param(
	[string]$Repo = '94hyuns/minetotalwar-bot',
	[int]$IntervalSeconds = 60,
	[string[]]$ClaudeArgs = @('--permission-mode', 'acceptEdits'),
	[switch]$Once
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $PSScriptRoot '.issue_watch.json'
$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Load-Done {
	if (Test-Path $stateFile) { return @((Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json).done) }
	return @()
}

function Save-Done([int[]]$done) {
	[IO.File]::WriteAllText($stateFile, (@{ done = $done } | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding $false))
}

while ($true) {
	try {
		$issues = gh issue list --repo $Repo --label '버그' --state open --json number,title,labels --limit 50 | ConvertFrom-Json
		$done = Load-Done
		foreach ($issue in ($issues | Sort-Object number)) {
			$labels = @($issue.labels | ForEach-Object { $_.name })
			if ($done -contains $issue.number -or $labels -contains '확인중' -or $labels -contains '보류') { continue }
			# 먼저 기록해 두어 실행이 실패해도 같은 이슈로 계속 깨우지 않는다 (다시 하려면 .issue_watch.json 에서 번호를 지운다)
			$done += $issue.number
			Save-Done $done
			$log = Join-Path $logDir "issue-$($issue.number).log"
			Write-Host "$(Get-Date -Format 'MM-dd HH:mm') 이슈 #$($issue.number) 처리 시작: $($issue.title)"
			$prompt = "GitHub 이슈 #$($issue.number) ($Repo) 를 처리해. 먼저 docs/운영-제보처리.md 를 끝까지 읽고 그 절차와 배포 보호장치를 그대로 따라. 설계 판단이 필요하거나 재현이 안 되면 라벨 보류 + 질문 댓글로 멈춰."
			Push-Location $root
			try {
				& claude -p $prompt @ClaudeArgs *> $log
			} finally {
				Pop-Location
			}
			Write-Host "$(Get-Date -Format 'MM-dd HH:mm') 이슈 #$($issue.number) 끝 (로그: $log)"
		}
	} catch {
		Write-Host "$(Get-Date -Format 'MM-dd HH:mm') 확인 실패: $_"
	}
	if ($Once) { break }
	Start-Sleep -Seconds $IntervalSeconds
}
