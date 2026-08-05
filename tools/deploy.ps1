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
Pop-Location
if (-not $test -or -not $lint) {
    Write-Host "테스트 또는 룰셋 린트가 통과하지 않았습니다. 배포를 중단합니다." -ForegroundColor Red
    exit 1
}
Write-Host "  테스트: $($test.Line.Trim())"
Write-Host "  룰셋:   통과"

# ── 2. 비밀·개인정보 스캔 ──────────────────────────────────────────
# 인증키는 환경변수로만 쓰지만, 실수로 붙여넣은 흔적을 매번 확인한다.
# 자택 IP도 공개 저장소에 두지 않는다.
$tree = git rev-parse "HEAD:$Prefix"
$patterns = "[REDACTED-KEY]", "[REDACTED-KEY]", "[REDACTED-IP]", "[REDACTED-ACCOUNT]"
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
