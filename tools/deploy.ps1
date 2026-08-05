# 배포 — 이 폴더만 공개 저장소로 밀어 올린다.
#
# 왜 그냥 push가 아닌가
#     이 프로젝트는 개인 모노레포(`교육`) 안에 있다. 그냥 밀면 블로그·공모전·책 원고까지
#     전부 공개된다. 그래서 **이 폴더의 트리만** 떼어 별도 저장소의 main에 얹는다.
#
# 안전장치 두 겹
#     ① remote.property-tax.push 를 refspec으로 못 박아 두었다(git config).
#        인자 없이 `git push property-tax` 를 해도 모노레포가 나가지 않는다.
#     ② 이 스크립트는 푸시 전에 인증키·자택 IP를 스캔하고, 걸리면 멈춘다.
#
# 실행
#     pwsh -File tools/deploy.ps1              # 검사 후 푸시
#     pwsh -File tools/deploy.ps1 -DryRun      # 검사만

param(
    [switch]$DryRun,
    [string]$Message = "chore: 배포 갱신"
)

$ErrorActionPreference = "Stop"
$Repo = "c:\Users\admin\Desktop\교육"
$Prefix = "부동산 상담"
$Remote = "property-tax"

Set-Location $Repo

# ── 0. 커밋되지 않은 변경이 있으면 멈춘다 ──────────────────────────
$dirty = git status --porcelain -- $Prefix
if ($dirty) {
    Write-Host "커밋되지 않은 변경이 있습니다. 먼저 커밋하세요:" -ForegroundColor Yellow
    $dirty | Select-Object -First 10
    exit 1
}

# ── 1. 테스트 게이트 ───────────────────────────────────────────────
Push-Location (Join-Path $Repo $Prefix)
$test = python -m pytest tests/ -q --tb=no 2>&1 | Select-String "passed"
$lint = python -m realestate_tax.rules.lint 2>&1 | Select-String "이상 없음"
# 상황 시뮬레이션도 게이트에 건다. 하네스는 **자동으로 돌지 않으면 아무 의미가 없다** —
# 손으로 돌리는 검증은 바쁠 때 가장 먼저 생략되고, 바쁠 때가 사고가 나는 때다.
$sim = python -m sim.run --fail-on-violation 2>&1 | Select-String "^시나리오"
$simOk = $LASTEXITCODE -eq 0
Pop-Location
if (-not $test -or -not $lint) {
    Write-Host "테스트 또는 룰셋 린트가 통과하지 않았습니다. 배포를 중단합니다." -ForegroundColor Red
    exit 1
}
if (-not $simOk) {
    Write-Host "상황 시뮬레이션에서 불변식 위반이 있습니다. 배포를 중단합니다." -ForegroundColor Red
    Write-Host "  자세히 보기: python -m sim.run" -ForegroundColor Yellow
    exit 1
}
Write-Host "  테스트: $($test.Line.Trim())"
Write-Host "  룰셋:   통과"
Write-Host "  시뮬:   $($sim.Line.Trim())"

# ── 2. 비밀·개인정보 스캔 ──────────────────────────────────────────
# 인증키는 환경변수로만 쓰지만, 실수로 붙여넣은 흔적을 매번 확인한다.
# 자택 IP도 공개 저장소에 두지 않는다.
$tree = git rev-parse "HEAD:$Prefix"

# ⚠️ 패턴을 리터럴로 적으면 **이 파일이 스스로 걸린다**(첫 실행에서 실제로 걸렸다).
#    스캔에서 이 파일을 제외하면 정작 여기 붙여넣은 키를 놓치므로,
#    제외하는 대신 조각을 이어 붙여 리터럴이 파일에 남지 않게 한다.
$patterns = @(
    ("U01T" + "X0FVVEg"),      # juso 승인키 접두
    ("5f31e" + "72040cb"),     # data.go.kr 인증키 접두
    ("119\.204" + "\.9\.241"), # 발급 당시 자택 IP
    ("dune" + "dinjh")         # 개인 계정
)
foreach ($p in $patterns) {
    $hit = git grep -n -E $p $tree 2>$null
    if ($hit) {
        Write-Host "비밀·개인정보로 보이는 문자열이 있습니다 ($p). 배포를 중단합니다." -ForegroundColor Red
        $hit | Select-Object -First 5
        exit 1
    }
}
Write-Host "  비밀 스캔: 깨끗함"

if ($DryRun) { Write-Host "DryRun — 푸시하지 않습니다."; exit 0 }

# ── 3. 트리만 얹어 커밋 → 푸시 ─────────────────────────────────────
# git subtree split은 모노레포 전 이력을 재탐색해 몇 분씩 걸린다.
# 필요한 것은 '현재 상태' 하나이므로 직전 배포 커밋 위에 트리를 얹는다.
$parent = (git ls-remote $Remote main 2>$null) -split "\s+" | Select-Object -First 1
$commit = if ($parent) {
    git commit-tree $tree -p $parent -m $Message
} else {
    git commit-tree $tree -m $Message
}
git push $Remote "${commit}:refs/heads/main"
Write-Host ""
Write-Host "배포 완료 → https://github.com/kitech1798/korea-property-tax" -ForegroundColor Green
Write-Host "Streamlit Cloud가 main 변경을 감지해 자동으로 다시 배포합니다."
