@echo off
setlocal
set "UV_PYW=%APPDATA%\uv\tools\google-colab-cli\Scripts\pythonw.exe"
if exist "%UV_PYW%" (
    start "" "%UV_PYW%" "%~dp0colab_accounts.py"
) else (
    start "" pythonw "%~dp0colab_accounts.py"
)
