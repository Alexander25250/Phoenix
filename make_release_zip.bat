@echo off
cd /d "%~dp0"
if not exist "dist\PHOENIX\PHOENIX.exe" (
  echo Не найдена dist\PHOENIX\PHOENIX.exe. Сначала запусти build_exe.bat
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path 'PHOENIX-win-x64.zip') { Remove-Item 'PHOENIX-win-x64.zip' -Force }; Compress-Archive -Path 'dist\PHOENIX\*' -DestinationPath 'PHOENIX-win-x64.zip' -CompressionLevel Optimal"
if errorlevel 1 (
  echo Не удалось создать архив обновления.
  pause
  exit /b 1
)
echo.
echo Готово: PHOENIX-win-x64.zip
echo Загружай этот ZIP в Assets каждого GitHub Release.
pause
