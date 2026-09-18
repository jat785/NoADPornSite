#!/usr/bin/env python3
"""
站点连通性诊断 —— 一次性把「谁的代理、哪个出口、站点回了什么」全 dump 出来。

为什么要有这个：站点报错时，「打不开」可能是五六个完全不同的原因
（出口 IP 被拉黑 / 本机代理在拦 / DNS 被改 / 站点地区策略 / 上游风控），
而每一种的应对方式都不一样。过去靠一条条问、一个个试，来回十几轮还不一定定位。
这里一条命令全部打印出来。

    python tools/site_probe.py                 # 默认测 PornHub
    python tools/site_probe.py redtube
    python tools/site_probe.py hanime
    python tools/site_probe.py https://www.example.com/path

最关键的其实是第 2 步：**软件和浏览器走的是不是同一个代理**。
浏览器一般走「系统代理」，而本程序走 ADSKIPER_PROXY —— 这两个如果指向不同的
出口 IP，那"浏览器能打开、程序打不开"就有了最直接的解释。

退出码：0 = 一切正常；1 = 发现了可疑之处（看最后的结论）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# Windows 控制台默认不是 UTF-8，中文会变成乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ------------------------------------------------------------------ #
# 尽量复用主程序里的常量，保证探测发出去的请求和后端**完全一样**
# ------------------------------------------------------------------ #
FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
try:
    import app as A  # noqa: N812

    UA = getattr(A, "UA", FALLBACK_UA)
    ADAPTERS = getattr(A, "SITES", {})
    _IMPORTED = True
except Exception as exc:  # noqa: BLE001
    UA = FALLBACK_UA
    ADAPTERS = {}
    _IMPORTED = False
    _IMPORT_ERR = f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ #
# 输出
# ------------------------------------------------------------------ #
def step(n: int, total: int, title: str) -> None:
    print(f"\n{'─' * 70}\n[{n}/{total}] {title}\n{'─' * 70}")


def _disp_width(s: Any) -> int:
    """终端显示宽度：CJK 全角算 2 格，其余算 1 格。

    不能用 str.ljust/`:<18` —— 那是按**字符数**补的，中文标签会被少补一半空格，
    输出的冒号就全歪了。
    """
    w = 0
    for ch in str(s):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def say(label: str, value: Any = "", indent: int = 2) -> None:
    pad = " " * indent
    if value == "":
        print(f"{pad}{label}")
    else:
        gap = " " * max(1, 22 - _disp_width(label))
        print(f"{pad}{label}{gap}{value}")


def verdict(text: str, bad: bool = False) -> None:
    print(f"  {'✗' if bad else '★'} {text}")


# ------------------------------------------------------------------ #
# 1. 代理配置：app 用哪个、浏览器用哪个
# ------------------------------------------------------------------ #
DEFAULT_PROXY = "http://127.0.0.1:7890"


def normalize_proxy(raw: str | None) -> str | None:
    """把各种写法的代理串统一成 http://host:port，空串/None -> None（= 直连）。"""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if "//" not in raw:
        raw = "http://" + raw
    return raw


def app_proxy() -> tuple[str | None, str]:
    """本程序会用的代理。逻辑和 app.py 的 PROXY 一致。"""
    if "ADSKIPER_PROXY" in os.environ:
        return normalize_proxy(os.environ["ADSKIPER_PROXY"]), "环境变量 ADSKIPER_PROXY"
    return DEFAULT_PROXY, "默认值（没设 ADSKIPER_PROXY）"


def env_proxies() -> dict[str, str]:
    out = {}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if v := os.environ.get(k):
            out[k] = v
    return out


def system_proxy() -> tuple[str | None, str]:
    """浏览器的系统代理设置（Windows 注册表 / macOS scutil / Linux gsettings）。

    浏览器默认走的就是这个 —— 所以它决定了"浏览器从哪个 IP 出去"。
    """
    sysname = platform.system()

    if sysname == "Windows":
        try:
            import winreg  # type: ignore[import-not-found]

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except FileNotFoundError:
                enabled = 0
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError:
                server = ""
            try:
                pac, _ = winreg.QueryValueEx(key, "AutoConfigURL")
            except FileNotFoundError:
                pac = ""
            finally:
                winreg.CloseKey(key)

            if pac:
                return None, f"PAC 自动配置脚本：{pac}"
            if not enabled:
                return None, "未启用（浏览器走直连）"
            if not server:
                return None, "启用了但没有服务器地址"
            # 可能是 "http=1.2.3.4:80;https=1.2.3.4:81" 这种分流写法
            if "=" in server:
                parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
                server = parts.get("https") or parts.get("http") or server
            return normalize_proxy(server), f"已启用 -> {server}"
        except Exception as exc:  # noqa: BLE001
            return None, f"读取失败（{type(exc).__name__}）"

    if sysname == "Darwin":
        try:
            out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=10).stdout
            https_on = re.search(r"HTTPSEnable\s*:\s*1", out)
            host = re.search(r"HTTPSProxy\s*:\s*(\S+)", out)
            port = re.search(r"HTTPSPort\s*:\s*(\d+)", out)
            if https_on and host and port:
                return normalize_proxy(f"{host.group(1)}:{port.group(1)}"), "scutil --proxy（HTTPS）"
            http_on = re.search(r"HTTPEnable\s*:\s*1", out)
            h2 = re.search(r"HTTPProxy\s*:\s*(\S+)", out)
            p2 = re.search(r"HTTPPort\s*:\s*(\d+)", out)
            if http_on and h2 and p2:
                return normalize_proxy(f"{h2.group(1)}:{p2.group(1)}"), "scutil --proxy（HTTP）"
            return None, "未启用"
        except Exception as exc:  # noqa: BLE001
            return None, f"读取失败（{type(exc).__name__}）"

    # Linux 及其它
    try:
        mode = subprocess.run(
            ["gsettings", "get", "org.gnome.system.proxy", "mode"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().strip("'")
        if mode == "manual":
            host = subprocess.run(
                ["gsettings", "get", "org.gnome.system.proxy.http", "host"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip().strip("'")
            port = subprocess.run(
                ["gsettings", "get", "org.gnome.system.proxy.http", "port"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            if host:
                return normalize_proxy(f"{host}:{port}"), "gsettings（GNOME 系统代理）"
        return None, f"未检测到（gsettings mode={mode or '未知'}）"
    except Exception:  # noqa: BLE001
        return None, "未检测到（非 GNOME 桌面，通常靠环境变量）"


# ------------------------------------------------------------------ #
# 2. 出口 IP
# ------------------------------------------------------------------ #
def whoami(proxy: str | None, timeout: float = 15.0) -> dict[str, Any]:
    """经指定代理（或直连）查出口 IP / 地区。"""
    url = "http://ip-api.com/json/?fields=status,message,country,regionName,city,isp,as,query"
    label = proxy or "直连"
    try:
        kw: dict[str, Any] = {"timeout": timeout, "trust_env": False, "follow_redirects": True}
        if proxy:
            kw["proxy"] = proxy
        with httpx.Client(**kw) as c:
            r = c.get(url)
            j = r.json()
        if j.get("status") != "success":
            return {"label": label, "error": j.get("message") or f"HTTP {r.status_code}"}
        desc = f"{j.get('country')} / {j.get('regionName')} / {j.get('city')}"
        org = (j.get("as") or "").split(" ", 1)[-1] or j.get("isp") or ""
        return {"label": label, "ip": j.get("query"), "region": desc, "org": org}
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "error": f"{type(exc).__name__}: {str(exc)[:70]}"}


def fmt_whoami(d: dict[str, Any]) -> str:
    if d.get("error"):
        return f"失败 —— {d['error']}"
    return f"{d.get('ip')}   {d.get('region')}   {d.get('org')}"


# ------------------------------------------------------------------ #
# 3. DNS
# ------------------------------------------------------------------ #
def resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return sorted({i[4][0] for i in infos})
    except Exception as exc:  # noqa: BLE001
        return [f"解析失败: {type(exc).__name__}"]


def dns_note(ips: list[str]) -> str:
    for ip in ips:
        if ip.startswith("198.18.") or ip.startswith("198.19."):
            return "看起来是 fake-ip（Clash 常见的做法，本身正常）"
    if any("失败" in ip for ip in ips):
        return "解析失败 —— 请求根本发不出去"
    return ""


# ------------------------------------------------------------------ #
# 4. 请求探测
# ------------------------------------------------------------------ #
SAMPLE_HEADERS = ("server", "date", "content-type", "content-length",
                  "cf-ray", "cf-mitigated", "via", "x-cache", "location")


def fetch(url: str, proxy: str | None, headers: dict[str, str], timeout: float = 25.0) -> dict[str, Any]:
    kw: dict[str, Any] = {"timeout": timeout, "trust_env": False, "follow_redirects": False}
    if proxy:
        kw["proxy"] = proxy
    t0 = time.time()
    try:
        with httpx.Client(**kw) as c:
            r = c.get(url, headers=headers)
        body = r.content
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:110]}", "ms": int((time.time() - t0) * 1000)}
    return {
        "status": r.status_code,
        "reason": r.reason_phrase,
        "ms": int((time.time() - t0) * 1000),
        "headers": dict(r.headers),
        "nbytes": len(body),
        "body": body[:400].decode("utf-8", "replace"),
        "url": str(r.url),
    }


def show_fetch(label: str, res: dict[str, Any]) -> None:
    print(f"  ── {label}")
    if res.get("error"):
        say("结果", f"请求失败 —— {res['error']}")
        say("耗时", f"{res.get('ms')} ms")
        return
    say("状态", f"HTTP {res['status']} {res.get('reason', '')}")
    say("耗时", f"{res['ms']} ms")
    say("大小", f"{res['nbytes']} 字节")
    if res.get("url") != res.get("_req_url"):
        say("最终 URL", res["url"][:100])

    hdrs = res.get("headers") or {}
    interesting = [(k, v) for k, v in hdrs.items() if k.lower() in SAMPLE_HEADERS]
    say(f"响应头（{len(hdrs)} 个，挑要紧的）")
    if interesting:
        for k, v in interesting:
            print(f"      {k}: {str(v)[:90]}")
    else:
        print("      （连 server / date / content-type 这类常规头都没有）")
    print("      全部头：" + ", ".join(sorted(hdrs))[:400])

    if res["nbytes"] == 0:
        say("body", "0 字节（空）")
    else:
        body = re.sub(r"\s+", " ", res["body"]).strip()
        say("body 开头", body[:240])

    # 这一步是整份诊断里最重要的判断
    has_serverish = any(k.lower() in ("server", "date", "content-type") for k in hdrs)
    if res["nbytes"] == 0 and not has_serverish:
        verdict("这个响应既没有 body，也没有 server/date 之类的常规头 —— "
                "更像是本机某一层（代理 / 杀毒 / 本地服务）伪造的，不是站点回的", bad=True)
    elif res["status"] in (403, 410, 451, 429):
        verdict(f"上游明确拒绝了（{res['status']}）—— 原因看上面的 body 开头", bad=True)


# ------------------------------------------------------------------ #
# 5. Clash 控制器（尽力而为，只读）
# ------------------------------------------------------------------ #
VERGE_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "io.github.clash-verge-rev.clash-verge-rev",
    Path(os.environ.get("APPDATA", "")) / "clash-verge",
    Path(os.environ.get("APPDATA", "")) / "clash-verge-rev",
    Path.home() / ".config" / "clash",
    Path.home() / ".config" / "clash-verge",
    Path.home() / "Library" / "Application Support" / "io.github.clash-verge-rev.clash-verge-rev",
    Path.home() / ".local" / "share" / "io.github.clash-verge-rev.clash-verge-rev",
]
DEFAULT_CONTROLLERS = ["127.0.0.1:9097", "127.0.0.1:9090", "127.0.0.1:7459", "127.0.0.1:9091"]


def clash_info() -> dict[str, Any] | None:
    ctrl, secret = None, None
    for d in VERGE_DIRS:
        if not d.is_dir():
            continue
        for f in list(d.glob("*.yaml")) + list(d.glob("*.yml")):
            try:
                t = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if ctrl is None:
                if m := re.search(r"^external-controller:\s*['\"]?([^'\"\s#]+)", t, re.M):
                    ctrl = m.group(1)
            if secret is None:
                if m := re.search(r"^secret:\s*['\"]?([^'\"\s#]+)", t, re.M):
                    secret = m.group(1)
        if ctrl:
            break

    for cand in ([ctrl] if ctrl else []) + DEFAULT_CONTROLLERS:
        if not cand:
            continue
        base = cand if cand.startswith("http") else "http://" + cand
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        try:
            with httpx.Client(timeout=6, trust_env=False, headers=headers) as c:
                cfg = c.get(base + "/configs").json()
                px = c.get(base + "/proxies").json().get("proxies", {})
        except Exception:  # noqa: BLE001
            continue
        groups = {g: v.get("now") for g, v in px.items()
                  if v.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance")}
        return {
            "controller": base,
            "mode": cfg.get("mode"),
            "mixed_port": cfg.get("mixed-port"),
            "tun": (cfg.get("tun") or {}).get("enable"),
            "groups": groups,
        }
    return None


# ------------------------------------------------------------------ #
# 目标站点
# ------------------------------------------------------------------ #
def target_for(name: str) -> tuple[str, dict[str, str]]:
    """返回 (要请求的 URL, 额外请求头)。头和主程序保持一致。"""
    h: dict[str, str] = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    key = name.lower()
    if key.startswith("http://") or key.startswith("https://"):
        return name, h

    alias = {"pornhub": "PornHub", "redtube": "RedTube", "hanime": "hanime"}
    site = alias.get(key)
    if site and site in ADAPTERS:
        ad = ADAPTERS[site]
        if getattr(ad, "cookie", ""):
            h["Cookie"] = ad.cookie
        if ad.key == "hanime":
            # hanime 的目录接口不需要签名，最能反映"网络通不通"
            return "https://guest.freeanimehentai.net/api/v11/search_hvs", h
        if ad.key == "PornHub":
            return "https://www.pornhub.com/video?o=mv&page=1", h
        if ad.key == "RedTube":
            return "https://www.redtube.com/?search=lesbian&page=1", h
    # 没匹配上就按 PornHub 默认来
    h["Cookie"] = "accessAgeDisclaimerPH=1; accessPH=1; platform=pc"
    return "https://www.pornhub.com/video?o=mv&page=1", h


def main() -> int:
    ap = argparse.ArgumentParser(description="站点连通性诊断")
    ap.add_argument("target", nargs="?", default="pornhub",
                    help="pornhub / redtube / hanime，或一个完整 URL（默认 pornhub）")
    args = ap.parse_args()

    T = 6
    print("=" * 70)
    print("  站点连通性诊断  ——  NoADPornSite")
    print("=" * 70)

    # ---------- 1 ----------
    step(1, T, "代理配置：软件用哪个，浏览器用哪个")
    if not _IMPORTED:
        say("提示", f"没能 import app.py，用内置兜底常量（{_IMPORT_ERR}）")
    a_proxy, a_src = app_proxy()
    e_proxies = env_proxies()
    s_proxy, s_src = system_proxy()

    say("平台", f"{platform.system()} {platform.release()}")
    say("软件用的代理", f"{a_proxy or '（直连）'}   ← {a_src}")
    say("环境变量代理", ", ".join(f"{k}={v}" for k, v in e_proxies.items()) or "无")
    say("系统代理", s_src)
    same_addr = (a_proxy or "") == (s_proxy or "")
    if same_addr:
        verdict(f"软件与浏览器的代理**地址相同**（{a_proxy or '都直连'}）"
                " —— 但地址相同不等于出口相同，看下一步")
    else:
        verdict(f"⚠ 软件与浏览器的代理**地址不同**：软件 {a_proxy or '直连'}，"
                f"浏览器 {s_proxy or '直连'}", bad=True)

    # ---------- 2 ----------
    step(2, T, "出口 IP 对比（关键：软件和浏览器走的是不是同一个出口）")
    a_who = whoami(a_proxy)
    s_who = whoami(s_proxy) if s_proxy != a_proxy else {"label": "同软件", "same": True}
    d_who = whoami(None)

    say("经软件的代理", fmt_whoami(a_who))
    if s_who.get("same"):
        say("经系统代理", "（与软件用的是同一个地址，结果同上）")
    else:
        say("经系统代理", fmt_whoami(s_who))
    say("直连", fmt_whoami(d_who))

    app_ip = a_who.get("ip")
    brw_ip = app_ip if s_who.get("same") else s_who.get("ip")
    if app_ip and brw_ip and app_ip != brw_ip:
        verdict(f"⚠⚠ 软件和浏览器的出口 IP **不一样**！（软件 {app_ip}，浏览器 {brw_ip}）"
                " —— 「浏览器能打开、程序打不开」多半就是这个原因", bad=True)
    elif app_ip and brw_ip:
        verdict(f"软件和浏览器出口一致（{app_ip}）—— 排除「两条路不同出口」")
    else:
        verdict("出口 IP 没测全，无法比较（上面有失败项）", bad=True)

    # ---------- 3 ----------
    step(3, T, "DNS 解析")
    url, headers = target_for(args.target)
    host = urlparse(url).hostname or ""
    ips = resolve_host(host)
    say("目标", url[:100])
    say(f"{host} 解析到", ", ".join(ips))
    if note := dns_note(ips):
        say("说明", note)

    # ---------- 4 ----------
    step(4, T, "经软件的代理请求目标站点")
    r_via = fetch(url, a_proxy, headers)
    r_via["_req_url"] = url
    show_fetch(f"经 {a_proxy or '直连'}", r_via)

    # ---------- 5 ----------
    step(5, T, "对照：同一个请求，绕过代理直连")
    r_dir = fetch(url, None, headers)
    r_dir["_req_url"] = url
    show_fetch("直连（不经任何代理）", r_dir)
    if r_via.get("status") and r_dir.get("status") and r_via["status"] != r_dir["status"]:
        verdict(f"两条路的**状态码不同**（代理 {r_via['status']} / 直连 {r_dir['status']}）"
                " —— 说明代理那一层对结果有影响")
    elif r_via.get("status") == r_dir.get("status") and r_via.get("status"):
        verdict("两条路状态码相同 —— 代理不是造成差异的原因")

    # ---------- 6 ----------
    step(6, T, "Clash 控制器（只读，尽力而为）")
    ci = clash_info()
    if not ci:
        say("结果", "没连上控制器（不影响其它结论）")
        say("提示", "可以手动看客户端的『代理』页：模式、当前节点、以及日志里该域名命中的规则")
    else:
        say("控制器", ci["controller"])
        say("模式", f"{ci['mode']}    mixed-port={ci['mixed_port']}    tun={ci['tun']}")
        for g, now in ci["groups"].items():
            say(f"  {g}", now)

    # ---------- 结论 ----------
    print("\n" + "=" * 70)
    print("  怎么读这份结果")
    print("=" * 70)
    print("""
  1. 第 2 步两个 IP 不同          -> 就是"两条路不同出口"，把 ADSKIPER_PROXY 指到
                                    和系统代理同一个地址即可
  2. 第 4 步 body 空、且没有 server/date 头
                                  -> 这个响应大概率是本机某层伪造的
                                     （代理在拦 / 杀毒 / 本地服务），不是站点回的
  3. 第 4 步有正常的 body         -> 是真·上游响应，原因写在 body 里
  4. 第 4 步失败、第 5 步成功     -> 代理本身有问题（端口错 / 没在跑）
  5. 两条都失败                   -> 网络出口问题
  6. 第 6 步能看模式/节点          -> 试试切 global 模式、换节点，再跑一次对比
""")
    bad = bool(a_who.get("error") or r_via.get("error"))
    if app_ip and brw_ip and app_ip != brw_ip:
        bad = True
    if r_via.get("status") in (403, 410, 451, 429):
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
