# 버그 이슈 감시 (설계 §33 제보 처리 "깨우기" B안). 1분마다 GitHub 에서 열린 `버그` 이슈를 보고(토큰 사용 없음),
# 아직 처리하지 않은 이슈가 있으면 이 저장소 폴더에서 Claude Code 를 한 번 실행해 docs/운영-제보처리.md 절차대로 처리하게 한다.
# A안(Claude Code 디스코드 채널 플러그인)은 플러그인이 봇·웹훅이 쓴 메시지를 전부 버려서(server.ts: if (msg.author.bot) return) 쓸 수 없다.
#
# 사용: powershell -ExecutionPolicy Bypass -File tools\issue_watch.ps1
#   -Model           처리 세션 모델. 기본 claude-opus-5-5 (비용을 아끼려면 -Model sonnet)
#   -MaxTurns        처리 세션 한 번의 최대 턴 (무한 루프 방지)
#   -MaxRunsPer5h    5시간 동안 최대 실행 수. 넘으면 그 5시간이 지날 때까지 새 이슈는 건드리지 않는다(이슈는 열린 채 남음)
#   -ClaudeArgs      claude 에 더 넘길 인자. 명령 허용은 저장소 .claude/settings.json 의 permissions 를 따른다
#   -Once            한 번만 확인하고 끝 (시험용)
# 끄기: tools\.issue_watch.off 파일이 있으면 자동 처리를 하지 않는다(봇 접수·목록은 그대로).
# 플랜 사용량(%)은 CLI·로컬 파일로 읽을 방법이 없어(2026-10-20 확인) 실행 수 상한과 끄기 파일로 대신한다.
# 상태(처리한 번호·실패 횟수·실행 시각)는 tools\.issue_watch.json, 로그는 tools\logs\issue-<번호>.log (둘 다 깃 미포함)
# 이 파일은 한글 때문에 UTF-8(BOM) 으로 저장한다.
param(
	[string]$Repo = '94hyuns/minetotalwar-bot',
	[int]$IntervalSeconds = 60,
	[string]$Model = 'claude-opus-5-5',
	[int]$MaxTurns = 80,
	[int]$MaxRunsPer5h = 4,
	[string[]]$ClaudeArgs = @('--permission-mode', 'acceptEdits'),
	[switch]$Once
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $PSScriptRoot '.issue_watch.json'
$offFile = Join-Path $PSScriptRoot '.issue_watch.off'
$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
[Console]::OutputEncoding = [Text.Encoding]::UTF8
# 처리가 끝났다고 보는 라벨 (이 중 하나가 붙었거나 이슈가 닫혔으면 성공)
$handledLabels = @('확인중', '운영조치', '보류')
$retryMinutes = 30
$limitRetryMinutes = 60
$maxFails = 3

function Say([string]$text) { Write-Host "$(Get-Date -Format 'MM-dd HH:mm') $text" }

function Load-State {
	$state = @{ done = @(); fails = @{}; runs = @() }
	if (Test-Path $stateFile) {
		$saved = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
		if ($saved.done) { $state.done = @($saved.done) }
		if ($saved.runs) { $state.runs = @($saved.runs) }
		if ($saved.fails) { $saved.fails.PSObject.Properties | ForEach-Object { $state.fails[$_.Name] = @{ count = [int]$_.Value.count; next = [string]$_.Value.next } } }
	}
	return $state
}

function Save-State($state) {
	[IO.File]::WriteAllText($stateFile, ($state | ConvertTo-Json -Depth 5 -Compress), (New-Object Text.UTF8Encoding $false))
}

function Issue-Handled([int]$number) {
	$issue = gh issue view $number --repo $Repo --json state,labels | ConvertFrom-Json
	if ($issue.state -ne 'OPEN') { return $true }
	foreach ($label in $issue.labels) { if ($handledLabels -contains $label.name) { return $true } }
	return $false
}

while ($true) {
	try {
		if (Test-Path $offFile) {
			if ($Once) { Say '끄기 파일이 있어 자동 처리를 하지 않습니다.' }
		} else {
			$state = Load-State
			$now = Get-Date
			$state.runs = @($state.runs | Where-Object { [datetime]$_ -gt $now.AddHours(-5) })
			$issues = gh issue list --repo $Repo --label '버그' --state open --json number,title,labels --limit 50 | ConvertFrom-Json
			$target = $null
			foreach ($issue in ($issues | Sort-Object number)) {
				$labels = @($issue.labels | ForEach-Object { $_.name })
				if ($state.done -contains $issue.number) { continue }
				if (@($labels | Where-Object { $handledLabels -contains $_ }).Count -gt 0) { continue }
				$fail = $state.fails["$($issue.number)"]
				if ($fail -and [datetime]$fail.next -gt $now) { continue }
				$target = $issue
				break
			}
			if ($target -and $state.runs.Count -ge $MaxRunsPer5h) {
				if ($Once) { Say "5시간 실행 상한($MaxRunsPer5h)에 닿아 이슈 #$($target.number) 은 나중에 합니다." }
			} elseif ($target) {
				$number = [int]$target.number
				$log = Join-Path $logDir "issue-$number.log"
				$state.runs += $now.ToString('o')
				Save-State $state
				Say "이슈 #$number 처리 시작: $($target.title)"
				$prompt = "GitHub 이슈 #$number ($Repo) 를 처리해. 먼저 docs/운영-제보처리.md 를 끝까지 읽고 그 절차와 배포 보호장치를 그대로 따라. 이전 실행이 중간에 끊겼을 수 있으니 이슈 댓글·라벨·git log 로 어디까지 했는지 먼저 확인하고 이어서 해. 설계 판단이 필요하거나 재현이 안 되거나 명령이 거부되면 라벨 보류 + 질문 댓글로 멈춰."
				Push-Location $root
				try {
					$output = & claude -p $prompt --model $Model --max-turns $MaxTurns --output-format json @ClaudeArgs 2>&1 | Out-String
					$exitCode = $LASTEXITCODE
				} finally {
					Pop-Location
				}
				[IO.File]::AppendAllText($log, "==== $(Get-Date -Format o) (exit $exitCode)`n$output`n", (New-Object Text.UTF8Encoding $false))
				$result = $null
				try { $result = ($output.Trim() -split "`n")[-1] | ConvertFrom-Json } catch { }
				$errorText = if ($result -and $result.result) { [string]$result.result } else { ($output.Trim() -split "`n")[-1] }
				$failed = $exitCode -ne 0 -or ($result -and $result.is_error)
				$state = Load-State
				if (-not $failed -and (Issue-Handled $number)) {
					$state.done += $number
					$state.fails.Remove("$number")
					Say "이슈 #$number 처리 끝"
				} elseif ($result -and ($result.api_error_status -eq 429 -or $errorText -match '(?i)rate.?limit|usage.?limit|limit reached|5-hour')) {
					# 사용량 한도: 실패로 세지 않고 한 시간 뒤 다시
					$count = if ($state.fails["$number"]) { $state.fails["$number"].count } else { 0 }
					$state.fails["$number"] = @{ count = $count; next = (Get-Date).AddMinutes($limitRetryMinutes).ToString('o') }
					Say "이슈 #$number 사용량 한도 — $limitRetryMinutes 분 뒤 다시: $errorText"
				} else {
					$count = 1 + $(if ($state.fails["$number"]) { $state.fails["$number"].count } else { 0 })
					$state.fails["$number"] = @{ count = $count; next = (Get-Date).AddMinutes($retryMinutes).ToString('o') }
					$reason = if ($failed) { $errorText } else { '세션이 끝났지만 이슈 라벨·상태가 바뀌지 않음' }
					$line = ($reason -split "`n")[0]
					if ($line.Length -gt 200) { $line = $line.Substring(0, 200) }
					Say "이슈 #$number 실패 $count/$maxFails : $line"
					if ($count -ge $maxFails) {
						gh issue edit $number --repo $Repo --add-label '보류' | Out-Null
						gh issue comment $number --repo $Repo --body "자동 처리 실패: $line" | Out-Null
						$state.done += $number
						Say "이슈 #$number 보류로 넘김"
					}
				}
				Save-State $state
			}
		}
	} catch {
		Say "확인 실패: $_"
	}
	if ($Once) { break }
	Start-Sleep -Seconds $IntervalSeconds
}
