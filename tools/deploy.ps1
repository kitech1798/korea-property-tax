# 배포 — 이 저장소를 공개 저장소(main)로 밀어 올린다.
#
# 2026-08-13 전면 개정. 이전 판은 개인 모노레포(`교육`) 안에서 이 폴더의 트리만
# 떼어 합성 커밋으로 올렸다. 저장소가 분리되면서 그 우회로가 필요 없어졌다.
#
# ⚠️ 이 파일을 고치게 만든 사고 (2026-08-13)
#     `_RESUME.md`에 juso 키 발급 기준으로 자택 IP를 메모해 두었는데, 그 커밋이
#     공개 저장소로 나갔다. 나중에 파일에서 지웠지만 **과거 커밋에는 그대로 남았다.**
#     이전 스캐너가 못 잡은 이유가 두 가지다.
#       ① 현재 트리만 봤다. 과거 커밋은 범위 밖이었다.
#       ② 패턴이 정규식이라 점을 이스케이프한 형태를 못 잡았다.
#          사람 눈에는 그대로 읽히는 값인데 스캐너만 못 봤다.
#     그래서 지금은 **이력 전체**를 훑고, 구분자를 지운 **정규화 문자열**로 찾는다.
#
# ⚠️ 탐지 문자열을 이 파일에 적지 않는다
#     이전 판은 조각을 이어 붙여("U01T" + "…") 리터럴을 피하려 했지만, 사람이 읽으면
#     그대로 보인다. 사고를 설명하는 주석에 실제 IP를 적었다가 이 스캐너에 걸리기도 했다.
#     지금은 `tools/.secret-needles.txt`(gitignore 대상)에서 읽는다.
#     **파일이 없으면 배포를 멈춘다** — 스캔을 건너뛰는 것이 가장 나쁜 실패다.
#
# 실행
#     pwsh -File tools/deploy.ps1              # 검사 후 푸시
#     pwsh -File tools/deploy.ps1 -DryRun      # 검사만

param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Remote = "origin"
$Branch = "main"

Set-Location $Repo

# ── 0. 커밋되지 않은 변경이 있으면 멈춘다 ──────────────────────────
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "커밋되지 않은 변경이 있습니다. 먼저 커밋하세요:" -ForegroundColor Yellow
    $dirty | Select-Object -First 10
    exit 1
}

# ── 1. 테스트 게이트 ───────────────────────────────────────────────
$test = python -m pytest tests/ -q --tb=no 2>&1 | Select-String "passed"
$lint = python -m realestate_tax.rules.lint 2>&1 | Select-String "이상 없음"
# 상황 시뮬레이션도 게이트에 건다. 하네스는 **자동으로 돌지 않으면 아무 의미가 없다** —
# 손으로 돌리는 검증은 바쁠 때 가장 먼저 생략되고, 바쁠 때가 사고가 나는 때다.
$sim = python -m sim.run --fail-on-violation 2>&1 | Select-String "^시나리오"
$simOk = $LASTEXITCODE -eq 0
if (-not $test -or -not $lint) {
    Write-Host "테스트 또는 룰셋 린트가 통과하지 않았습니다. 배포를 중단합니다." -ForegroundColor Red
    exit 1
}
if (-not $simOk) {
    Write-Host "상황 시뮬레이션에서 불변식 위반이 있습니다. 배포를 중단합니다." -ForegroundColor Red
    exit 1
}
Write-Host "  테스트: $($test.Line.Trim())"
Write-Host "  룰셋:   통과"
Write-Host "  시뮬:   $($sim.Line.Trim())"

# ── 2. 비밀·개인정보 스캔 (이력 전체) ──────────────────────────────
# 공개 저장소는 **현재 파일이 아니라 이력 전체**가 공개된다. 지운 것도 과거에 남는다.
#
$needleFile = Join-Path $PSScriptRoot ".secret-needles.txt"
if (-not (Test-Path $needleFile)) {
    Write-Host "탐지 문자열 파일이 없습니다: $needleFile" -ForegroundColor Red
    Write-Host "  한 줄에 하나씩, 이력에 있어서는 안 되는 값을 적으세요" -ForegroundColor Yellow
    Write-Host "  (인증키 접두·자택 IP·개인 계정 등). 이 파일은 gitignore 대상입니다." -ForegroundColor Yellow
    Write-Host "  스캔을 건너뛰고 배포하지 않습니다." -ForegroundColor Red
    exit 1
}
# ★ 정규화는 **영숫자만 남긴다.**
#
#   2026-08-13, 세 번째 사고. 이전 판은 역슬래시와 공백만 지웠는데, 정작 저장소에
#   남아 있던 형태는 조각을 이어 붙인 것이었다.
#       ("119" + "[REDACTED-IP]")     ("U01T" + "[REDACTED-KEY]")
#   따옴표와 +를 안 지우니 통과했다. 이 회피법은 원래 이 스크립트가 **자기검출을
#   피하려고** 쓰던 기법이라, 스캐너가 자기 기법에 당한 셈이다.
#
#   구분자를 골라 지우는 방식은 언제나 다음 변형에 진다. 그래서 영숫자만 남긴다.
#   오탐이 늘 수 있지만, 배포 게이트에서 오탐은 멈추면 그만이고 미탐은 유출이다.
$needles = Get-Content $needleFile -Encoding UTF8 |
    Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith("#") } |
    ForEach-Object { ($_ -replace '[^0-9A-Za-z]', '') } |
    Where-Object { $_ }
if (-not $needles) {
    Write-Host "탐지 문자열이 비어 있습니다. 배포를 중단합니다." -ForegroundColor Red
    exit 1
}

$revs = git rev-list --all
Write-Host "  비밀 스캔: 커밋 $($revs.Count)개 · 이력 전체"
$blobs = git rev-list --objects --all | ForEach-Object { ($_ -split " ", 2)[0] } | Select-Object -Unique

$leaks = @()
foreach ($b in $blobs) {
    if ((git cat-file -t $b 2>$null) -ne "blob") { continue }
    $raw = git cat-file -p $b 2>$null
    if (-not $raw) { continue }
    # ★ 본문도 같은 규칙으로 정규화한다 — 영숫자만 남긴다.
    #   이스케이프(`\.`)·따옴표·+·공백·줄바꿈이 사이에 끼어도 잡힌다.
    $flat = ($raw -join "`n") -replace '[^0-9A-Za-z]', ''
    foreach ($n in $needles) {
        # 어떤 값이 걸렸는지는 **앞 4자만** 남긴다. 로그가 새 유출 경로가 되면 안 된다.
        if ($flat.Contains($n)) { $leaks += "$b : $($n.Substring(0, [Math]::Min(4, $n.Length)))…" }
    }
}
if ($leaks) {
    Write-Host "비밀·개인정보가 이력에 있습니다. 배포를 중단합니다." -ForegroundColor Red
    $leaks | Select-Object -First 10
    Write-Host "  이력에서 지우려면 git filter-repo --replace-text 를 쓰세요." -ForegroundColor Yellow
    exit 1
}
Write-Host "  비밀 스캔: 깨끗함"

if ($DryRun) { Write-Host "DryRun — 푸시하지 않습니다."; exit 0 }

# ── 3. 푸시 ────────────────────────────────────────────────────────
# ⚠️ git은 정상 진행 상황도 stderr에 쓴다("To https://…"). 스크립트 맨 위의
#    $ErrorActionPreference = "Stop"과 만나면 **성공한 푸시가 실패로 읽힌다.**
#    실제로 2026-08-05에 그렇게 멈췄고, 원격은 멀쩡히 갱신돼 있었다.
#    네이티브 명령의 성공 여부는 stderr가 아니라 **종료코드**로만 판단한다.
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
git push $Remote $Branch
$pushed = $LASTEXITCODE -eq 0
$ErrorActionPreference = $prev
if (-not $pushed) {
    Write-Host "푸시에 실패했습니다 (종료코드 $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}

# 원격이 정말 이 커밋을 가리키는지 확인한다. "푸시했다"와 "반영됐다"는 다르다.
$local = git rev-parse HEAD
$remoteHead = (git ls-remote $Remote $Branch) -split "\s+" | Select-Object -First 1
if ($remoteHead -ne $local) {
    Write-Host "원격이 $remoteHead 로 다릅니다. 배포를 확인하세요." -ForegroundColor Red
    exit 1
}
Write-Host "푸시 완료 — $local" -ForegroundColor Green
Write-Host ""
# ⚠️ 2026-08-13 — Streamlit Cloud가 푸시를 **자동으로 물어오지 않는다.** 하루에 네 번
#    같은 자리에서 헤맸다(ImportError·MissingRule·옛 화면 문구·캐시 오류). 전부
#    "코드가 틀렸나"를 의심하다 배포가 안 됐던 것으로 끝났다.
#    푸시는 배포가 아니다. 그 사실을 매번 화면에 적는다.
Write-Host "  ⚠️ 푸시는 배포가 아닙니다. Streamlit Cloud는 새 커밋을 자동으로 안 가져옵니다." -ForegroundColor Yellow
Write-Host "     https://share.streamlit.io -> korea-property-tax -> [...] -> Reboot app" -ForegroundColor Yellow
Write-Host "     리붓 뒤 사이드바의 '빌드' 값이 $($local.Substring(0,7)) 인지 확인하세요." -ForegroundColor Yellow
