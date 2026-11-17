# DJ Grab for Windows: one-time setup. Run by "Install DJ Grab.cmd" (no admin rights needed).
# Everything goes into %LOCALAPPDATA%\DJ Grab: its own Python, the download tools, ffmpeg, and DJ Grab itself.
# Then a Start menu and desktop shortcut are made and DJ Grab opens.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$S = Join-Path $env:LOCALAPPDATA "DJ Grab"
$Repo = "vinnyricott/DJGrab"
New-Item -ItemType Directory -Force -Path "$S\bin", "$S\uv" | Out-Null
$env:UV_INSTALL_DIR = "$S\uv"; $env:UV_NO_MODIFY_PATH = "1"; $env:UV_PYTHON_INSTALL_DIR = "$S\python"
$UV = "$S\uv\uv.exe"

function Step($pct, $text) { Write-Host ("[{0,3}%] {1}" -f $pct, $text) }

Step 5 "Getting the Python installer"
if (-not (Test-Path $UV)) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null }

Step 20 "Installing Python (one time, about 40 MB)"
& $UV python install 3.12 | Out-Null
if (-not (Test-Path "$S\venv\Scripts\python.exe")) { & $UV venv --seed --python 3.12 "$S\venv" | Out-Null }
$PY = "$S\venv\Scripts\python.exe"

Step 40 "Installing the download tools and the window"
& $UV pip install --python $PY -q --upgrade "yt-dlp[default]" mutagen static-ffmpeg deno PySide6 numpy

Step 70 "Getting ffmpeg (audio converter)"
& $PY -c @"
import os, shutil, static_ffmpeg.run as r
ff, fp = r.get_or_fetch_platform_executables_else_raise()
b = os.path.join(os.environ['LOCALAPPDATA'], 'DJ Grab', 'bin')
shutil.copy2(ff, os.path.join(b, 'ffmpeg.exe')); shutil.copy2(fp, os.path.join(b, 'ffprobe.exe'))
"@
if (Test-Path "$S\venv\Scripts\deno.exe") { Copy-Item "$S\venv\Scripts\deno.exe" "$S\bin\deno.exe" -Force }

Step 85 "Downloading DJ Grab"
$tmp = Join-Path $env:TEMP ("djgrab-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile "$tmp\dj.zip"
Expand-Archive "$tmp\dj.zip" -DestinationPath $tmp
$src = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like "DJGrab-*" } | Select-Object -First 1
if (Test-Path "$S\engine") { Remove-Item "$S\engine" -Recurse -Force }
Copy-Item $src.FullName "$S\engine" -Recurse
Remove-Item $tmp -Recurse -Force

Step 95 "Making the shortcuts"
$sh = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
  $lnk = $sh.CreateShortcut((Join-Path $dir "DJ Grab.lnk"))
  $lnk.TargetPath = "$S\venv\Scripts\pythonw.exe"
  $lnk.Arguments = "`"$S\engine\win\DJGrab.pyw`""
  $lnk.WorkingDirectory = "$S\engine"
  if (Test-Path "$S\engine\win\DJGrab.ico") { $lnk.IconLocation = "$S\engine\win\DJGrab.ico" }
  $lnk.Save()
}

Step 100 "Ready. Opening DJ Grab"
Start-Process "$S\venv\Scripts\pythonw.exe" -ArgumentList "`"$S\engine\win\DJGrab.pyw`"" -WorkingDirectory "$S\engine"
