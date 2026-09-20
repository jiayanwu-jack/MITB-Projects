@echo off
setlocal

echo Stopping Streamlit processes for this app...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$project = (Resolve-Path '%~dp0').Path.TrimEnd('\');" ^
  "$targets = Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -like '*streamlit*run*app.py*') -and (($_.CommandLine -like ('*' + $project + '*')) -or ($_.CommandLine -like '*streamlit run app.py*')) };" ^
  "if (-not $targets) { Write-Host 'No matching Streamlit app processes found.'; exit 0 };" ^
  "$targets | ForEach-Object { Write-Host ('Stopping PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Done.
