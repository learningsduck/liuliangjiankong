@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r requirements.txt

pyinstaller --noconfirm --clean ^
  --onefile --windowed --name "VPS流量汇总" ^
  --collect-all paramiko ^
  --collect-all cryptography ^
  app_gui.py

echo.
echo 输出目录：dist\VPS流量汇总.exe
echo 请将 servers.yaml 与 exe 放在同一目录（可复制 servers.example.yaml）。
pause
