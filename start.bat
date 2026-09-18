@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   NoADPornSite  -  本地视频直链播放器
echo ============================================================
echo.

set "VENV=.venv"
set "VPY=%VENV%\Scripts\python.exe"

REM ==========================================================================
REM 依赖装进项目自己的 .venv，不碰全局 site-packages。
REM   理由：全局只有一份，两个项目要同一个库的不同版本时只能有一个赢；
REM         而且以后卸载时分不清哪些包是自己要的、哪些是某个项目带进来的。
REM   代价：磁盘上多占约 60 MB。**不会额外下载** —— pip 的 wheel 缓存会命中。
REM

REM ---------- 1. 优先复用已有的虚拟环境 ----------
REM 顺序很重要：已有的 venv 只要能跑，就不该因为系统 python 太旧而报错退出。
REM （这个坑是 start.sh 的作者踩出来的：明明 .venv 已经建好了，却因为系统
REM   只有 python 3.9 而被判死。）
if exist "%VPY%" (
    "%VPY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        echo [venv] 复用已有虚拟环境 %VENV%
        goto :deps
    )
    echo [venv] 已有的 %VENV% 不可用（python 低于 3.10 或已损坏），重建...
    rmdir /s /q "%VENV%"
)

REM ---------- 2. 没有可复用的 venv，才要求系统 python ----------
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 找不到 python，也没有可复用的 %VENV%。
    echo         请安装 Python 3.10 或更新，安装时**勾选** "Add python.exe to PATH"。
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

REM fastapi / uvicorn / starlette / anyio / click / yt-dlp 的 Requires-Python
REM 全都声明了 >=3.10。版本太低时 pip 只会丢一堆看不懂的解析错误，这里提前拦下来。
python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [错误] Python 版本过低，需要 3.10 或更新。当前版本：
    python --version
    pause
    exit /b 1
)

REM ---------- 3. 创建虚拟环境 ----------
echo [venv] 创建虚拟环境 %VENV%（只需一次，以后直接复用）...
python -m venv "%VENV%"
if errorlevel 1 (
    echo [错误] 创建虚拟环境失败。
    pause
    exit /b 1
)

:deps
REM ---------- 4. 依赖（装进 %VENV%，不动全局） ----------
REM 注意要连 cryptography 一起试探：它是**懒加载**的（只有 hanime 握手时才 import），
REM 只检查前四个的话会漏装它，表现为「启动一切正常、一播 hanime 就崩」。
"%VPY%" -c "import fastapi,uvicorn,httpx,yt_dlp,cryptography" >nul 2>&1
if errorlevel 1 (
    echo [依赖] 正在装到 %VENV%（首次约几十 MB，需要能连 pypi）...
    "%VPY%" -m pip install -q --upgrade pip >nul 2>&1
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。如果是网络问题，可以换国内镜像重试：
        echo        "%VPY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
    echo.
) else (
    echo [依赖] 已就绪
)

REM ---------- 5. Node / ffmpeg：都只是提示，缺了不拦 ----------
node --version >nul 2>&1
if errorlevel 1 (
    echo [node]   没找到 —— RedTube / PornHub 照常可用，
    echo          但 hanime.tv 取流需要它（WASM 签名助手）。建议装 Node 18+：
    echo          https://nodejs.org/
    echo.
) else (
    echo [node]   已就绪
)

ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [ffmpeg] 没找到 —— 播放、以及 RedTube / PornHub 的下载都不受影响，
    echo          但 hanime 的视频「另存为」需要它（要把上百个加密分片解密再封装）。
    echo          下载后把 bin 目录加进 PATH：https://www.gyan.dev/ffmpeg/builds/
    echo.
) else (
    echo [ffmpeg] 已就绪
)

echo 启动中... 浏览器打开 http://127.0.0.1:8000
echo 按 Ctrl+C 停止
echo.
"%VPY%" app.py

pause
