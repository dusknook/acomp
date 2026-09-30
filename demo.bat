@echo off
REM acomp 一键演示（双击即可）。
REM 依赖缺失时 demo.py 会自动建 .venv 并安装，无需预先配置。
setlocal
set "ROOT=%~dp0"
set "PY="

where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )

if not defined PY (
    echo.
    echo 找不到 Python。请先安装 Python 3.10+：https://www.python.org/downloads/
    echo 安装时记得勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

%PY% "%ROOT%demo.py" %*
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (echo 演示完成。) else (echo 演示中断，退出码 %CODE%。)
echo.
pause
endlocal & exit /b %CODE%
