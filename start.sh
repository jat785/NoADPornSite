#!/usr/bin/env bash
#
# macOS / Linux 启动脚本，对应 Windows 的 start.bat。
#
#   启动：   ./start.sh                        自动判断出口（推荐）
#   换代理： ADSKIPER_PROXY=http://127.0.0.1:1087 ./start.sh
#   强制直连：ADSKIPER_PROXY= ./start.sh        （Shadowrocket 等系统级 VPN）
#   换端口： ADSKIPER_PORT=9000 ./start.sh
#
# 依次做六件事：解释器 → venv → 依赖 → Node → ffmpeg → 代理出口，最后启动 app.py。

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
DEFAULT_PROXY="http://127.0.0.1:7890"

# 代理出口的三种写法：
#   什么都不设            → 自动：7890 通就用它，不通就按直连跑
#                           （Shadowrocket / Surge 这类系统级 VPN 走这条）
#   ADSKIPER_PROXY=       → 强制直连
#   ADSKIPER_PROXY=<url>  → 强制使用该代理
PROXY_IS_SET="${ADSKIPER_PROXY+1}"
PROXY="${ADSKIPER_PROXY-}"

info() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }

echo "============================================================"
echo "  NoADPornSite  -  本地视频直链播放器"
echo "============================================================"
echo

# ---------- 0. 补 PATH ----------
# uv / pipx 装的 python 在 ~/.local/bin，而这个目录通常只写在 ~/.zshrc 里。
# 交互式终端没问题，但 IDE、双击运行、cron 这类非交互环境读不到，
# 会掉回系统自带的 Python 3.9（低于硬下限 3.10）。这里显式补上。
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH" ;;
esac

VPY="$VENV/bin/python"

# ---------- 1. 解释器：优先复用已有 venv ----------
# 注意顺序：已有的 venv 本身就够用了，不该因为系统 python 太旧就报错退出。
if [ -x "$VPY" ] && "$VPY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    ok "[1/6] Python: $("$VPY" --version 2>&1)"
    ok "[2/6] 复用已有虚拟环境：$VENV"
else
    # 依次试，优先高版本；uv 装的 python3.12 也在 PATH 里，会一起被找到。
    PY=""
    for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    done

    if [ -z "$PY" ]; then
        err "[错误] 找不到 Python 3.10 或更新的版本。"
        if command -v python3 >/dev/null 2>&1; then
            err "        当前 python3 是 $(python3 --version 2>&1)（路径 $(command -v python3)）"
        fi
        echo  "        用 uv 装一个：  uv python install 3.12"
        echo  "        或用 Homebrew： brew install python@3.12"
        exit 1
    fi
    ok "[1/6] Python: $("$PY" --version 2>&1)  ($(command -v "$PY"))"

    # ---------- 2. 创建虚拟环境 ----------
    # macOS / Linux 直接用系统 Python 装依赖会被 PEP 668 拦下来
    # (externally-managed-environment)，所以必须走 venv。
    #
    # --seed 不能省：uv 默认**不往 venv 里装 pip**（它的设计取向是让用户用
    # `uv pip install`），少了这个参数，下面第 3 步的 `python -m pip` 会直接
    # 报 "No module named pip"。venv 里带上 pip 还有个好处：方式二里
    # `source .venv/bin/activate` 之后手动跑 pip 也是通的。
    info "[2/6] 创建虚拟环境 $VENV ..."
    if command -v uv >/dev/null 2>&1; then
        # uv 会把需要的 Python 版本下到自己的目录，不依赖系统 python 够不够新
        uv venv --seed --python 3.12 "$VENV" \
            || { warn "      uv venv 失败，改用 python -m venv"; "$PY" -m venv "$VENV"; }
    else
        "$PY" -m venv "$VENV"
    fi
    VPY="$VENV/bin/python"
fi

# ---------- 3. 依赖 ----------
# 先定安装器。venv 里没有 pip 是真实会出现的情况：uv 建的 venv 就没有
# （见上面的 --seed），而第 1 步的复用分支会原样接手这种 venv。
# 顺序：pip → ensurepip 现补一个 → uv pip。三条都不通才报错，
# 而不是让 `python -m pip` 的报错把用户带去折腾镜像。
INSTALLER=""
if "$VPY" -m pip --version >/dev/null 2>&1; then
    INSTALLER="pip"
elif "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 && "$VPY" -m pip --version >/dev/null 2>&1; then
    INSTALLER="pip"
    info "      这个 venv 里没有 pip，已用 ensurepip 补上"
elif command -v uv >/dev/null 2>&1; then
    INSTALLER="uv"
    info "      这个 venv 里没有 pip，改用 uv pip 装依赖"
fi

if [ -z "$INSTALLER" ]; then
    err "[错误] 这个 venv 里没有 pip，ensurepip 也补不上，同时找不到 uv。"
    err "        常见于复用了 uv 建的 venv、而 uv 已不在 PATH 里。"
    err "        删掉重来即可：rm -rf $VENV && ./start.sh"
    exit 1
fi

# cryptography 必须一起试探：它是懒加载的（只有 hanime 握手时才 import），
# 只检查前四个会漏装，表现为「启动一切正常、一播 hanime 就崩」。
if "$VPY" -c 'import fastapi, uvicorn, httpx, yt_dlp, cryptography' >/dev/null 2>&1; then
    ok "[3/6] 依赖已就绪"
else
    info "[3/6] 安装依赖（首次约几十 MB，需要能连 pypi）..."
    install_ok=1
    if [ "$INSTALLER" = "pip" ]; then
        "$VPY" -m pip install -q --upgrade pip >/dev/null 2>&1 || warn "      pip 自升级失败，继续。"
        "$VPY" -m pip install -r requirements.txt || install_ok=0
    else
        uv pip install --python "$VPY" -r requirements.txt || install_ok=0
    fi
    if [ "$install_ok" = "0" ]; then
        # 能走到这里说明安装器是好的，所以失败大概率真的在网络/解析上。
        err "[错误] 依赖安装失败。网络问题可以换国内镜像重试："
        if [ "$INSTALLER" = "pip" ]; then
            err "       $VPY -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
        else
            err "       uv pip install --python $VPY -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple"
        fi
        exit 1
    fi
    ok "      依赖安装完成"
fi

# ---------- 4. Node：只有 hanime 取流需要，缺了不拦 ----------
# 认 ADSKIPER_NODE_BIN（和 hanime.py 读同一个变量）：node 是 nvm / Homebrew
# 装的、又不在当前 PATH 里时，用户本来就要靠它指路，脚本这边也得跟着认，
# 否则会在「用户已经按 README 配好了」的情况下报「没找到 node」。
NODE_BIN="${ADSKIPER_NODE_BIN:-node}"
if command -v "$NODE_BIN" >/dev/null 2>&1; then
    NODE_VER="$("$NODE_BIN" --version 2>&1)"
    NODE_MAJOR="${NODE_VER#v}"
    NODE_MAJOR="${NODE_MAJOR%%.*}"
    if [ "${NODE_MAJOR:-0}" -ge 18 ] 2>/dev/null; then
        ok "[4/6] Node: $NODE_VER  ($(command -v "$NODE_BIN"))"
    else
        warn "[4/6] Node 版本过低：$NODE_VER（需要 18+）"
        warn "      RedTube / PornHub 照常可用，hanime.tv 取流会失败。"
    fi
else
    warn "[4/6] 没找到 node（$NODE_BIN）—— RedTube / PornHub 照常可用，"
    warn "      但 hanime.tv 取流需要它（WASM 签名助手），建议装 Node 18+。"
    warn "      node 是 nvm / Homebrew 装的、不在 PATH 里时："
    warn "        ADSKIPER_NODE_BIN=/opt/homebrew/bin/node ./start.sh"
fi

# ---------- 5. ffmpeg：只有 hanime 的「下载」需要，缺了不拦 ----------
# 同样认 ADSKIPER_FFMPEG（和 app.py 读同一个变量）。
FFMPEG_BIN="${ADSKIPER_FFMPEG:-ffmpeg}"
if command -v "$FFMPEG_BIN" >/dev/null 2>&1; then
    ok "[5/6] ffmpeg: $(command -v "$FFMPEG_BIN")"
else
    warn "[5/6] 没找到 ffmpeg（$FFMPEG_BIN）—— 播放、以及 RedTube / PornHub 的下载"
    warn "      都不受影响，但 hanime 的「另存为」需要它（上百个加密分片要解密再封装）。"
    warn "      macOS: brew install ffmpeg    Linux: sudo apt install ffmpeg"
    warn "      不在 PATH 里时：ADSKIPER_FFMPEG=/path/to/ffmpeg ./start.sh"
fi

# ---------- 6. 代理出口 ----------
# 站点和 CDN 都在墙外。两种常见方案，**互斥**：
#   A. 本地 HTTP 代理（Clash / mihomo 的 127.0.0.1:7890）—— 要显式告诉本程序
#   B. 系统级 VPN（Shadowrocket / Surge 增强模式）—— 在 IP 层接管所有流量，
#      本程序也在内，所以必须**直连**；这时设了代理反而连不上
proxy_reachable() {
    "$VPY" - "$1" <<'PY' >/dev/null 2>&1
import socket, sys, urllib.parse as u

raw = sys.argv[1]
p = u.urlparse(raw if "//" in raw else "http://" + raw)
s = socket.socket()
s.settimeout(2)
try:
    s.connect((p.hostname or "127.0.0.1", p.port or 7890))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

if [ -n "$PROXY" ]; then
    if proxy_reachable "$PROXY"; then
        ok "[6/6] 代理可达：$PROXY"
    else
        warn "[6/6] ⚠️  代理不可达：$PROXY"
        warn "      搜索和播放会失败（HTTP 502）。检查代理客户端是否在跑，或换一个出口："
        warn "        ADSKIPER_PROXY=http://127.0.0.1:1087 ./start.sh"
        echo
    fi
elif [ -n "$PROXY_IS_SET" ]; then
    ok "[6/6] 直连模式（ADSKIPER_PROXY 已显式置空）"
else
    # 没显式配置：探一下最常见的 7890。通就用它，不通就按 VPN 方案直连。
    if proxy_reachable "$DEFAULT_PROXY"; then
        PROXY="$DEFAULT_PROXY"
        ok "[6/6] 自动检测到本地代理：$PROXY"
    else
        ok "[6/6] 7890 没有代理，按直连模式运行（Shadowrocket 等 VPN 方案走这条）"
        warn "      注意：VPN 没连上时，站点依然连不上 —— 先打开 Shadowrocket 并连接。"
    fi
fi

# app.py 里 os.environ.get("ADSKIPER_PROXY", "http://127.0.0.1:7890") 带默认值，
# 所以自动判定为直连时必须显式导出空串，否则程序会自己又去连 7890。
export ADSKIPER_PROXY="$PROXY"

echo
echo "启动中... 浏览器打开 http://127.0.0.1:${ADSKIPER_PORT:-8000}"
echo "按 Ctrl+C 停止"
echo
exec "$VPY" app.py
