@echo off
rem Build dist\VKPhotoExporter.exe (single file, no console window).
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
)
.venv\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller || goto :error

.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed ^
    --name VKPhotoExporter ^
    --exclude-module pytest --exclude-module _pytest ^
    gui.py || goto :error

echo.
echo Done: dist\VKPhotoExporter.exe
exit /b 0

:error
echo Build failed.
exit /b 1