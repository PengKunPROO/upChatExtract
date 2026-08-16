@echo off
rem build.bat - one-click build of up-chat-monitor.exe (requires Python 3.10+)
rem Build output: dist\up-chat-monitor.exe
setlocal

echo [1/2] Installing dependencies...
pip install -r requirements.txt pyinstaller || goto :err

echo [2/2] Building exe...
python -m PyInstaller --noconfirm --clean --onefile --console --name up-chat-monitor monitor.py || goto :err
echo.
echo Build complete: dist\up-chat-monitor.exe
exit /b 0

:err
echo Build failed. See error messages above.
exit /b 1
