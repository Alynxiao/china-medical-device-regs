@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title 中国医疗器械法规库
color 0F

echo.
echo  ==========================================================
echo    中国医疗器械法规库
echo    第一次运行会自动准备环境（约 5-10 分钟，看网速）
echo    以后再双击就是直接更新，通常 1 分钟内
echo  ==========================================================
echo.

REM ============================================================
REM  第 1 步：找一个能用的 Python
REM ============================================================
set "PYEXE="
set "PYARG="

if exist "%~dp0runtime\python.exe" (
    set "PYEXE=%~dp0runtime\python.exe"
    echo  [1/5] 环境：使用自带的 Python
    goto :check_pip
)

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARG=-3"
    echo  [1/5] 环境：使用系统 Python ^(py -3^)
    goto :check_pip
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python"
    echo  [1/5] 环境：使用系统 Python ^(python^)
    goto :check_pip
)

echo  [1/5] 没找到 Python，正在下载便携版（约 11MB，只这一次）...
echo.
set "PYZIP=%TEMP%\mdregs_py.zip"
set "PYURL1=https://registry.npmmirror.com/-/binary/python/3.11.9/python-3.11.9-embed-amd64.zip"
set "PYURL2=https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"

echo       尝试国内镜像...
curl -L --fail --progress-bar --max-time 300 -o "%PYZIP%" "%PYURL1%"
if not errorlevel 1 goto :pydl_ok
echo       镜像不通，改试官方源...
curl -L --fail --progress-bar --max-time 300 -o "%PYZIP%" "%PYURL2%"
if not errorlevel 1 goto :pydl_ok

echo.
echo  [×] 下载失败。检查网络后重试；也可以自己装一个 Python
echo      官网 https://www.python.org/downloads/  ^(安装时勾选 Add to PATH^)
goto :fail

:pydl_ok
if not exist "%~dp0runtime" mkdir "%~dp0runtime"
powershell -NoProfile -Command "Expand-Archive -Path '%PYZIP%' -DestinationPath '%~dp0runtime' -Force"
if errorlevel 1 (
    echo  [×] 解压失败
    goto :fail
)
del "%PYZIP%" >nul 2>nul

REM 让便携版能加载 pip 装的包
powershell -NoProfile -Command "(Get-Content '%~dp0runtime\python311._pth') -replace '#import site','import site' | Set-Content '%~dp0runtime\python311._pth'"
if not exist "%~dp0runtime\Lib\site-packages" mkdir "%~dp0runtime\Lib\site-packages"

set "PYEXE=%~dp0runtime\python.exe"
echo  [1/5] 便携版 Python 就绪

REM ============================================================
REM  第 2 步：确保有 pip
REM ============================================================
:check_pip
"%PYEXE%" %PYARG% -m pip --version >nul 2>nul
if not errorlevel 1 goto :check_deps

echo  [2/5] 正在准备 pip...
curl -L --fail -o "%~dp0runtime\get-pip.py" "https://bootstrap.pypa.io/get-pip.py"
if errorlevel 1 (
    echo  [×] pip 下载失败，检查网络
    goto :fail
)
"%PYEXE%" %PYARG% "%~dp0runtime\get-pip.py" --no-warn-script-location
if errorlevel 1 (
    echo  [×] pip 安装失败
    goto :fail
)
echo  [2/5] pip 就绪
goto :check_deps

REM ============================================================
REM  第 3 步：装抓取依赖（已经装过就跳过）
REM ============================================================
:check_deps
"%PYEXE%" %PYARG% -c "import scrapling" >nul 2>nul
if not errorlevel 1 (
    echo  [3/5] 依赖：已装好，跳过
    goto :check_browser
)

echo  [3/5] 正在安装抓取组件（约 80MB，只这一次）...
"%PYEXE%" %PYARG% -m pip install --upgrade pip --no-warn-script-location -i https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>nul
"%PYEXE%" %PYARG% -m pip install "scrapling[fetchers]" --no-warn-script-location -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo       镜像源装不上，改试官方源...
    "%PYEXE%" %PYARG% -m pip install "scrapling[fetchers]" --no-warn-script-location
)
if errorlevel 1 (
    echo.
    echo  [×] 依赖安装失败。常见原因：
    echo      - 网络不稳（公司网/防火墙），换个网络再试
    echo      - 加速器/代理干扰，先关掉再试
    goto :fail
)
echo  [3/5] 抓取组件就绪

REM ============================================================
REM  第 4 步：浏览器内核（已经装过就跳过）
REM ============================================================
:check_browser
set "HAS_BROWSER="
for /d %%D in ("%LOCALAPPDATA%\ms-playwright\chromium-*") do set "HAS_BROWSER=1"
if defined HAS_BROWSER (
    echo  [4/5] 浏览器内核：已装好，跳过
    goto :run
)

echo  [4/5] 正在下载浏览器内核（约 130MB，只这一次）...
"%PYEXE%" %PYARG% -m playwright install chromium
if errorlevel 1 (
    echo.
    echo  [×] 浏览器内核下载失败，检查网络后重试
    goto :fail
)
echo  [4/5] 浏览器内核就绪

REM ============================================================
REM  第 5 步：抓数据 + 生成页面
REM ============================================================
:run
echo.
echo  [5/5] 开始抓取官方最新数据...
echo        第一次约 1 分钟（包内已带历史数据，只补新增）
echo        以后同样，只抓新增
echo.
REM 跑 run.py：法规 + 监管动态(30个源) + 生成页面，四步一体
"%PYEXE%" %PYARG% "%~dp0run.py"
if errorlevel 1 (
    echo.
    echo  [×] 出错了，上面的报错信息可以发给维护人
    goto :fail
)

echo.
echo  ==========================================================
echo    完成。正在打开页面...
echo    数据文件：%~dp0data\cn_regs.jsonl
echo  ==========================================================
REM 页面文件名带数据版本，取最新的那个
set "LATEST="
for /f "delims=" %%F in ('dir /b /o-d "%~dp0中国医械法规库_*.html" 2^>nul') do (
    if not defined LATEST set "LATEST=%%F"
)
if defined LATEST (
    echo    页面文件：%~dp0!LATEST!
    start "" "%~dp0!LATEST!"
) else (
    echo    页面文件：%~dp0中国医械法规库.html
    start "" "%~dp0中国医械法规库.html"
)
timeout /t 3 >nul
exit /b 0

:fail
echo.
echo  按任意键关闭...
pause >nul
exit /b 1
