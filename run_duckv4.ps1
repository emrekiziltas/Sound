param(
    [Parameter(Mandatory=$true)][ValidateSet("run","analyze")][string]$mode,
    [Parameter(Mandatory=$true)][string]$folder,
    [Parameter(Mandatory=$true)][string]$file,
    [double]$on = 35,
    [double]$off = 30,
    [double]$floorMargin = 30,
    [double]$duckDb = -10,
    [double]$audDuckDb = -40,
    [double]$teachHold = 1.5,
    [switch]$denoise,
    [switch]$noAlign
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# ---------------- LOG ----------------
$logDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("duck_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HH-mm-ss"))

function Write-Log {
    param([string]$msg, [string]$level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    switch ($level) {
        "ERROR" { Write-Host $line -ForegroundColor Red }
        "WARN"  { Write-Host $line -ForegroundColor Yellow }
        default { Write-Host $line }
    }
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}

# Ondalik sayilari her zaman noktayla gonder
function F([double]$v) { $v.ToString([Globalization.CultureInfo]::InvariantCulture) }

$sw = [System.Diagnostics.Stopwatch]::StartNew()
Write-Log "Log dosyasi: $logFile"
Write-Log ("Parametreler: mode={0} folder={1} file={2} on={3} off={4} floorMargin={5} duckDb={6} audDuckDb={7} teachHold={8} denoise={9} noAlign={10}" -f `
    $mode, $folder, $file, $on, $off, $floorMargin, $duckDb, $audDuckDb, $teachHold, $denoise.IsPresent, $noAlign.IsPresent)

try {
    $base = "C:\Users\ek675\OneDrive - University of Cambridge\Documents\Emre\Video Area\Video"
    $dir  = Join-Path $base $folder
    Write-Log "Klasor: $dir"

    if (-not (Test-Path -LiteralPath $dir)) {
        Write-Log "Klasor bulunamadi: $dir" "ERROR"
        exit 1
    }

    # Uzanti verilmisse kirp, sonra mp4/mkv/mov sirayla ara
    $name = [IO.Path]::GetFileNameWithoutExtension($file)
    $inputFile = $null
    foreach ($ext in "mp4","mkv","mov") {
        $candidate = Join-Path $dir "$name.$ext"
        if (Test-Path -LiteralPath $candidate) { $inputFile = $candidate; break }
    }
    if (-not $inputFile) {
        Write-Log "Dosya bulunamadi: $name (.mp4/.mkv/.mov) -> $dir" "ERROR"
        $prefix = $name.Substring(0, [Math]::Min(10, $name.Length))
        Write-Log "Benzer dosyalar ($prefix*):" "WARN"
        Get-ChildItem -LiteralPath $dir -Filter "$prefix*" | ForEach-Object { Write-Log "  $($_.Name)" "WARN" }
        exit 1
    }
    Write-Log "Girdi: $inputFile"

    $outputFile = Join-Path $dir "${name}_processed4.mp4"
    if ($mode -eq "run") { Write-Log "Cikti: $outputFile" }

    $script = Join-Path $PSScriptRoot "duck_lecture_v4.py"
    if (-not (Test-Path -LiteralPath $script)) {
        Write-Log "Python scripti yok: $script" "ERROR"
        exit 1
    }

    # Python: aktif venv -> Sound\.venv -> ust klasordeki .venv -> sistem python
    $py = $null
    $candidates = @()
    if ($env:VIRTUAL_ENV) { $candidates += (Join-Path $env:VIRTUAL_ENV "Scripts\python.exe") }
    $candidates += (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")
    $candidates += (Join-Path (Split-Path $PSScriptRoot -Parent) ".venv\Scripts\python.exe")
    foreach ($c in $candidates) { if (Test-Path -LiteralPath $c) { $py = $c; break } }
    if (-not $py) { $py = "python"; Write-Log "venv bulunamadi, sistem python kullaniliyor" "WARN" }
    Write-Log "Python: $py"
    Write-Log ("Python surumu: " + (& $py --version 2>&1))

    # ffmpeg: script 'ffmpeg' diye PATH'ten cagiriyor
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ff) {
        Write-Log "ffmpeg: $($ff.Source)"
    }
    elseif (Test-Path "C:\ffmpeg\bin\ffmpeg.exe") {
        $env:Path = "C:\ffmpeg\bin;$env:Path"
        Write-Log "ffmpeg PATH'te yoktu, bu oturum icin C:\ffmpeg\bin eklendi" "WARN"
    }
    else {
        Write-Log "ffmpeg bulunamadi (PATH'te de C:\ffmpeg\bin'de de yok)" "ERROR"
        exit 1
    }

    # Paket kontrolu
    $depOut = & $py -c "import numpy, scipy, soundfile; print('paketler tamam')" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "Eksik paket: $depOut" "ERROR"
        Write-Log "Kur: `"$py`" -m pip install numpy scipy soundfile" "ERROR"
        exit 1
    }
    Write-Log "$depOut"

    # duck_lecture v2 argumanlari: <analyze|run> input [output] --on ... (ortak ayarlar subcommand'dan sonra)
    $pyArgs = @("-u", $script, $mode, $inputFile)
    if ($mode -eq "run") { $pyArgs += $outputFile }
    $pyArgs += @("--on", (F $on), "--off", (F $off),
                 "--floor-margin", (F $floorMargin),
                 "--teach-hold", (F $teachHold))
    if ($mode -eq "run") {
        $pyArgs += @("--duck-db", (F $duckDb), "--aud-duck-db", (F $audDuckDb))
    }
    if ($denoise) { $pyArgs += "--denoise" }
    if ($noAlign) { $pyArgs += "--no-align" }

    Write-Log ("Komut: $py " + (($pyArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "))

    $env:PYTHONIOENCODING = "utf-8"
    & $py @pyArgs 2>&1 | ForEach-Object { Write-Log "$_" "PY" }
    $code = $LASTEXITCODE

    if ($code -ne 0) {
        Write-Log "Python hata kodu ile bitti: $code" "ERROR"
        exit $code
    }
    if ($mode -eq "run" -and -not (Test-Path -LiteralPath $outputFile)) {
        Write-Log "Cikti dosyasi olusmadi: $outputFile" "ERROR"
        exit 1
    }
    Write-Log ("Tamamlandi. Sure: {0:N1} sn" -f $sw.Elapsed.TotalSeconds)
}
catch {
    Write-Log ("Beklenmeyen hata: " + $_.Exception.Message) "ERROR"
    Write-Log ("Konum: " + $_.InvocationInfo.PositionMessage) "ERROR"
    exit 1
}