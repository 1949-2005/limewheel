# Runs one operation per library inside a packaged build to prove nothing is missing from the bundle.
#   powershell -File tests\frozen_smoke.ps1 -Exe dist\<App>\<App>.exe -Samples <folder with test samples>
param([Parameter(Mandatory)][string]$Exe, [Parameter(Mandatory)][string]$Samples)
$ErrorActionPreference = "Stop"
$work = Join-Path $env:TEMP "frozen-smoke"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $work | Out-Null
$cases = @(
  @("img.to.heic", "photo.jpg", ""), @("img.to.avif", "photo.jpg", ""), @("img.to.png", "logo.svg", ""), @("img.to.ico", "flower.jpg", ""), @("img.to.icosmall", "flower.jpg", ""), @("img.to.pdf", "flower.jpg", ""),
  @("img.compress", "flower.jpg", ""), @("img.compress", "logo_big.png", ""), @("img.rotate", "photo.jpg", ""),
  @("pdf.md", "doc.pdf", ""), @("pdf.compress", "big.pdf", ""), @("vid.to.mov", "clip.mp4", ""),
  @("aud.normalize", "tone.wav", ""), @("md.docx", "notes.md", ""),
  @("img.tosize", "flower.jpg", '{"bytes": 25000}'), @("img.to.svg", "photo.jpg", ""), @("img.qr", "qr.png", ""),
  @("img.meta", "photo.jpg", '{"fields": {"title": "T"}, "checks": {}, "strip": false}'),
  @("pdf.organize", "doc.pdf", '{"order": [[1, 90], [0, 0]]}'),
  @("pdf.meta", "doc.pdf", '{"fields": {"title": "D"}, "strip": false}'), @("aud.tosize", "tone.wav", '{"bytes": 20000}'),
  @("img.edit", "photo.jpg", '{"values": {"Exposure": 40, "Saturation": 20}}'),
  @("img.background", "sample.png", '{"color": "#FFFFFF", "pad": 0.1, "rounded": true, "shadow": true, "square": false}'),
  @("vid.to.webm", "clip.mp4", ""), @("vid.speed", "clip.mp4", '{"speed": 2.0, "pitch": true, "mute": false}'),
  @("aud.bleep", "tone.wav", '{"ranges": [[0.5, 1.0]], "sound": "tone"}'), @("aud.channels", "tone.wav", '{"mode": "Mono", "left": 1, "right": 1}'),
  @("pdf.docx", "doc.pdf", ""), @("pdf.epub", "doc.pdf", ""), @("pdf.pptx", "doc.pdf", ""), @("pdf.png", "doc.pdf", ""),
  @("docx.epub", "report.docx", ""), @("docx.pdf.basic", "report.docx", ""), @("md.docx", "notes.md", ""),
  @("epub.pdf", "novel.epub", ""), @("epub.docx", "novel.epub", ""),
  @("sheet.ods", "table.csv", ""), @("sheet.xlsx", "table.ods", ""), @("sheet.csv", "book.xlsx", ""),
  @("rtf.docx", "letter.rtf", ""), @("rtf.pdf", "letter.rtf", ""),
  @("arc.tar", "notes.md", ""), @("arc.gz", "notes.md", ""), @("arc.extract", "bundle.zip", ""),
  @("img.preset", "photo.jpg", '{"preset": "ig-post"}'), @("img.preset", "flower.jpg", '{"preset": "email"}'),
  @("vid.preset", "clip.mp4", '{"preset": "reel"}')
)
$pass = 0
$n = 0
foreach ($c in $cases) {
  $id, $file, $opts = $c
  $n++; $dir = Join-Path $work "$n-$id"; New-Item -ItemType Directory $dir | Out-Null
  Copy-Item (Join-Path $Samples $file) $dir
  $res = Join-Path $dir "result.json"
  $args = @("--run-op", $id, (Join-Path $dir $file), $res)
  if ($opts) { $of = Join-Path $dir "opts.json"; Set-Content $of $opts -Encoding utf8; $args += $of }
  $p = Start-Process $Exe -ArgumentList $args -PassThru -WindowStyle Hidden
  if (-not $p.WaitForExit(120000)) { Stop-Process -Id $p.Id -Force }
  $r = if (Test-Path $res) { Get-Content $res -Raw | ConvertFrom-Json } else { [pscustomobject]@{ ok = $false; error = "no result (exit $($p.ExitCode))" } }
  if ($r.ok) { $pass++; "OK    {0,-16} {1}" -f $id, (($r.outputs | ForEach-Object { Split-Path $_ -Leaf }) -join ", ") }
  else { "FAIL  {0,-16} {1}" -f $id, $r.error }
}
"`n$pass/$($cases.Count) passed in the packaged build"
