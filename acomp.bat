@echo off
REM acomp 便捷入口：用项目自带 venv 跑，避免污染全局 Python
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
    "%ROOT%.venv\Scripts\python.exe" "%ROOT%acomp.py" %*
) else (
    python "%ROOT%acomp.py" %*
)
endlocal & exit /b %ERRORLEVEL%
