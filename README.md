# NoADPornSite

本地视频直链播放器。**重在线无广播放、轻下载管理、无媒体库管理。**

本项目的核心是**根本不进入广告播放链路**：站点把正片的 CDN 直链明文放在页面里，
把这个直链交给自己的播放器，广告就从来不曾被请求过。

```
浏览器 (127.0.0.1:8000)          只用连本机 —— 不需要代理、不需要翻墙
      │
      ├── /api/random   关键词为空时的随机推荐（不知道看什么就刷一批）
      ├── /api/search   站点搜索（自己抓，yt-dlp 不支持搜索）
      ├── /api/resolve  yt-dlp 解析出 1080p/720p/480p/240p 直链
      └── /api/media    流式中转（透传 Range，可拖进度条）
      │
      ▼
本地 FastAPI ──[走 PROXY]──▶ 站点 / 广告无关的 CDN
```

---

## 前置环境

| 需要 | 版本 | 干什么用的 | 没有会怎样 |
|---|---|---|---|
| **Python** | **3.10 或更新** | 跑后端 | 完全起不来 |
| **Node.js** | **18 或更新** | hanime.tv 的 WASM 签名助手 | **另外两个站照常可用**，只有 hanime 取流失败 |
| **ffmpeg** | 任意较新版本 | 下载 hanime 视频时解密并封装 | **播放和另两个站的下载照常可用**，只有 hanime 点「下载」会报错 |

**Python 3.10 是硬下限**：`fastapi` / `uvicorn` / `starlette` / `anyio` / `click` / `yt-dlp`
声明的 `Requires-Python` 全是 `>=3.10`，低于这个版本 pip 根本装不上。

**Node 和 ffmpeg 都是可选的**，各自只影响一个功能：

| 缺什么 | 影响 | 不影响 |
|---|---|---|
| 没有 Node | hanime 播不了（取流要签名） | RedTube / PornHub 的播放和下载 |
| 没有 ffmpeg | hanime 下载不了（HLS 要解密封装） | 全部播放功能；RedTube / PornHub 的下载 |

自己确认一下（macOS / Linux 上大多只有 `python3`，没有 `python`）：

```bash
python3 --version    # 需要 >= 3.10
node --version       # 需要 >= 18（可选）
ffmpeg -version      # 可选，装了才能下载 hanime 的视频
```

### 怎么装

Windows 的安装包直接下；**macOS / Linux 那两行是按开源圈最常见的做法给的，
我没在对应系统上实测过**：

| 系统 | Python | Node | ffmpeg |
|---|---|---|---|
| **Windows** | [python.org](https://www.python.org/downloads/) 安装包，**务必勾选 `Add python.exe to PATH`** | [nodejs.org](https://nodejs.org/) 安装包 | [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下载后把 `bin` 加进 PATH，或 `winget install ffmpeg` |
| **macOS**<br>（未实测） | `brew install python@3.12`<br>或 [python.org](https://www.python.org/downloads/) 安装包 | `brew install node` | `brew install ffmpeg` |
| **Debian / Ubuntu**<br>（未实测） | `sudo apt install python3 python3-venv python3-pip` | 见下面的 ⚠️ | `sudo apt install ffmpeg` |

> ⚠️ **Linux 上不要用 `apt install nodejs`** —— Ubuntu 22.04 源里是 Node **12**，
> 达不到 18，装了也白装。用 [NodeSource](https://github.com/nodesource/distributions)
> 或 [nvm](https://github.com/nvm-sh/nvm)：
> ```bash
> curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs
> ```
> 另外 **`python3-venv` 别漏**，少了它下一步 `python3 -m venv` 会直接报错。

> **ffmpeg 不在 PATH 里怎么办**：和 Node 一样留了环境变量，
> 用 `ADSKIPER_FFMPEG=/path/to/ffmpeg` 指绝对路径即可。
> 启动后也可以打开 <http://127.0.0.1:8000/api/sites> 看 `ffmpeg` 字段是不是 `null`。

> **上面这张表是安装命令，没逐条验证过**：实测机上没装 Homebrew（Python 来自 uv、
> Node 来自 nodejs.org 安装包、ffmpeg 没装），所以这三行的标注保持原样。
> 实测过的是**方式一的 `./start.sh`** —— 见下面那一节。

### 还需要一个能连出去的代理

站点和它们的 CDN 都在墙外，后端默认出口是 `http://127.0.0.1:7890`
（Clash 混合端口；ClashX / mihomo 的默认值也是这个）。
**先把代理跑起来再启动**，否则两个 tube 站的搜索会直接超时。

浏览器本身**不需要任何代理** —— 所有上游媒体都由后端中转，浏览器只连 `127.0.0.1`。

用别的方式改出口，见下面的「配置」。

---

## 安装 / 启动

两条路，选一条就行 —— 跑起来的是同一个东西。

### 方式一：一键启动脚本（最快）

Windows 双击 `start.bat`，macOS / Linux 跑 `./start.sh` —— 两者做的事一样，
都自动建好环境、装好依赖，然后启动。

#### Windows：双击 `start.bat`

1. 装好 Python（3.10+，且进了 PATH）
2. **双击 `start.bat`**
3. 浏览器打开 <http://127.0.0.1:8000>

#### macOS / Linux：`./start.sh`

已在 **macOS (Apple Silicon)** 上实测通过（建 venv → 装依赖 → 检查 Node / ffmpeg →
判定出口代理 → 首页 HTTP 200）；**Linux 未实测**。

1. 装好 Python（3.10+）和 node（18+，可选）
2. 在项目目录里跑 `./start.sh`（首次没有执行权限就先 `chmod +x start.sh`）
3. 浏览器打开 <http://127.0.0.1:8000>

**依赖是自动装的，不用手动 `pip install`，而且两个脚本都装进项目自己的 `.venv`，
不污染全局环境。** 它们依次做这几件事：

```
先看有没有可用的 .venv            有     → 直接复用，跳过下面三步（约 2 秒）
没有，才检查 python 在不在 PATH    不在   → 报错退出，并给出下载地址
检查 Python 版本 >= 3.10          不够   → 报错退出（否则 pip 只会丢一堆看不懂的解析错误）
建 .venv                        失败   → 报错退出（.sh 优先 uv venv --seed，没有 uv 就用 python -m venv）
用 import 试探依赖装没装           缺了   → 装进 .venv（不动全局 site-packages）
找不到 node 时给一句提示           不拦   → RedTube / PornHub 照常可用，只是 hanime 用不了
找不到 ffmpeg 时给一句提示         不拦   → 播放不受影响，只是 hanime 的「下载」用不了
探一下出口代理通不通（仅 .sh）     不通   → 只警告不拦，并提示怎么换出口
最后启动 app.py
```

所以**第一次跑要建环境 + 装依赖（联网，几十 MB，实测约 70 秒），之后就秒开**。

> **为什么要建 `.venv` 而不是装到全局**：全局只有一份 site-packages，两个项目要同一个库
> 的不同版本时只能有一个赢；而且以后卸载时分不清哪些包是自己要的、哪些是某个项目带进来的。
> 代价是磁盘上多占约 **65 MB**，但**不会额外下载** —— pip 的 wheel 缓存会命中。
> 想彻底清掉就删掉 `.venv` 目录（它已在 `.gitignore` 里）。
>
> **已有的 `.venv` 优先复用**：即使系统 Python 后来变旧了，只要 `.venv` 里的解释器
> 还够用，脚本就不会因为系统版本而拒绝启动；只有真的要新建时才检查系统 Python。
>
> **两个脚本都只自动装 Python 依赖，不会装 Node 或 ffmpeg** —— 那两个不是 pip 包，
> 脚本只能检测到缺失后提示你。想用 hanime.tv 就去 [nodejs.org](https://nodejs.org/)
> 装 Node；想下载 hanime 的视频再去装 [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)。
> **两个都不装也不影响 RedTube / PornHub。**
>
> 装依赖走的是系统网络 / pip 自己的代理设置，**不走上面那个出口代理**。
> 国内网络嫌慢可以换镜像：
> ```bash
> .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

> **`start.sh` 的代理判定**：启动前它先探一下默认端口 `7890` 通不通 —— 通就自动
> 用它，不通就按**直连**跑。所以用 Shadowrocket / Surge 这类**系统级 VPN**
> （在 IP 层接管全部流量、没有本地 HTTP 代理端口）时什么都不用配；
> 反过来，这类方案下**不要**设 `ADSKIPER_PROXY`，设了反而连不上。
> 想强制指定或强制直连见「配置」。
>
> **`start.sh` 认 `ADSKIPER_NODE_BIN` / `ADSKIPER_FFMPEG`**：node 或 ffmpeg 不在
> PATH 里时，用这两个变量指绝对路径，脚本的检查会跟着认（`start.bat` 只查 PATH）。

### 方式二：自己装依赖再启动（跨平台，mac、linux）

和方式一做的事一样，只是手动来 —— **同样务必用虚拟环境**。
macOS（Homebrew）和 Ubuntu 23.04+ / Debian 12+ 的系统 Python 受
[PEP 668](https://peps.python.org/pep-0668/) 保护，直接 `pip install` 会被拦下来报
`externally-managed-environment`。Windows 没这个限制，但隔离本身就有价值。

```bash
python3 -m venv .venv            # Linux 上报错就先 sudo apt install python3-venv

source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows (cmd / PowerShell)

python -m pip install -r requirements.txt
python app.py
```

> **为什么用 `python3`**：macOS 和多数 Linux 上**没有** `python` 这个命令。
> 激活 venv 之后里面两个名字都有，后面几条命令照样能用 `python`。
>
> **Intel Mac 注意**：`cryptography` 从 49.0.0 起不再发布 Intel macOS 的预编译包。
> `requirements.txt` 里已经用环境标记（marker）帮你把上界卡在 49 以下，正常情况
> 不用管；万一还是报编译错误，单独跑 `python -m pip install "cryptography<49"`。

然后浏览器打开 <http://127.0.0.1:8000>。嫌这一串步骤麻烦，直接用方式一的 `./start.sh` 就行。

装完可以自检一下：

```bash
python -c "import fastapi, uvicorn, httpx, yt_dlp, cryptography; print('依赖 OK')"
node --version        # 可选；没装也不影响另两个站
```

---

## 装不上怎么办

**macOS / Linux 那两行命令我没实测过**（开发机只有 Windows），只按最通用的做法给。
跟着做卡住了，按下面顺序排查，绝大多数问题在前两步就解决。

### 1. 按报错关键词直接对号入座

| 报错里出现 | 意思 | 怎么办 |
|---|---|---|
| `externally-managed-environment` | 没在虚拟环境里装 | 回到方式二，先 `source .venv/bin/activate` |
| `No module named pip` | 复用了 uv 建的 venv —— uv 默认**不往 venv 里装 pip** | `./start.sh` 会自己用 `ensurepip` 补上；手动跑就 `python -m ensurepip --upgrade`，或删掉 `.venv` 重来 |
| `No module named venv` / `ensurepip is not available` | 缺 `python3-venv` | `sudo apt install python3-venv` |
| `command not found: python` | 只有 `python3` | 命令里的 `python` 换成 `python3` |
| `command not found: node` | 没装 Node | 只影响 hanime，见「前置环境」 |
| `Requires-Python` / `Unsupported` 之类版本报错 | Python 太老 | `python3 --version` 确认 ≥ 3.10 |
| `Microsoft Visual C++ ... required` / `error: can't find Rust compiler` | 在源码编译而不是装现成包 | 一般是平台/Python 版本没有预编译包，见上面 Intel Mac 那条 |
| pip 卡住 / `SSLError` / `ConnectionError` | 网络 | 换镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple` |

> **装好了但用起来报 4xx（403 / 410 / 451 / 429…）怎么读？**
>
> 这类错是**上游站点拒绝了这次请求**，而**原因不在状态码里，在响应体里**。
> 所以本项目的错误信息会把上游原文一并带出来，形如：
>
> ```
> 错误：HTTP 410 Gone ｜ server=cloudflare ｜ 上游说：Sorry, you have been blocked …
> ```
>
> 对照着读：
>
> | 上游原文里出现 | 说明 | 怎么办 |
> |---|---|---|
> | `cloudflare` / `Attention Required` / `blocked` | 是 **CDN 层的风控**拦的，不是站点本身 | 换一个**未被拉黑**的代理出口节点；机房 IP、公共 VPN 段最容易被拦 |
> | 地区 / 年龄验证 / `not available in your` 之类字样 | 该站点在该地区**已停止服务** | 换节点到别的地区。注意 PornHub 因年龄验证法规已在 25 个美国州 + 3 个国家停止服务，而**同属 Aylo 的 RedTube 没有跟进** —— 所以"RedTube 能用、PornHub 410"是正常现象 |
> | 一片空白（`上游 body 为空`） | 多半是你**本地代理**直接拒绝的，不是站点回的 | 检查代理的分流规则，看这个域名是否被某条规则 REJECT 或指向了不可用节点 |
> | `Too Many Requests` / `429` | 请求太频繁 | 等一会儿；或降低"随机推荐"的刷新频率 |
>
> **同一个程序在别人机器上正常、在你这 410，几乎总是出口 IP 的问题，不是代码。**

### 2. 把这段模板丢给 AI（ChatGPT / Claude / DeepSeek 都行）

装环境的问题几乎都是"平台 + 版本"的特定组合，说清楚这几样 AI 基本一次就能给准：

```
我在 <macOS 14 / Ubuntu 22.04 / Windows 11> 上装这个 Python 项目：
<仓库链接>

执行的命令：<原样贴你敲的那条>
完整报错：<原样贴，别截断，别只说"报错了一大堆">

python3 --version 输出：<...>
node --version 输出：<...>
已经试过：<...>
```

> 关键在**原样贴完整报错**。AI 猜不准通常就是因为只给了"报错了一堆"这种描述。

### 3. 装好了但跑起来才出问题

| 现象 | 原因 / 方向 |
|---|---|
| 两个 tube 站搜不出东西或超时 | 代理没跑起来、或端口不对。见「配置」 |
| hanime 报 **「找不到 node 可执行文件」** | 装 Node，或它不在当前 PATH 里（nvm / Homebrew 装的常见）。可以直接指定绝对路径：`ADSKIPER_NODE_BIN=/opt/homebrew/bin/node python app.py` |
| hanime 报「签名助手启动失败」 | 先跑 `python tools/hanime_probe.py`，它会一步步打印链路，能直接看出是"我们坏了"还是"站点改版了" |
| 播放报「域名不在白名单内」 | 该站 CDN 不在白名单，用 `ADSKIPER_MEDIA_HOSTS` 加上（见「支持的站点」） |
| 播放报「直链可能已过期」 | 签名 URL 约 2 小时过期，点「重载直链」即可 |

---

## 配置

改出口代理（默认 `http://127.0.0.1:7890`）：

```powershell
$env:ADSKIPER_PROXY = "socks5://127.0.0.1:7891"   # 或留空表示直连
python app.py
```

用 `start.sh` 时可以直接写在命令行上，不用先 export：

```bash
ADSKIPER_PROXY=socks5://127.0.0.1:7891 ./start.sh   # 指定出口
ADSKIPER_PROXY= ./start.sh                          # 强制直连（系统级 VPN 方案）
./start.sh                                          # 不设 = 自动探测 7890，不通就直连
```

全部环境变量：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ADSKIPER_PROXY` | `http://127.0.0.1:7890` | 出口代理。留空 = 直连 |
| `ADSKIPER_HOST` | `127.0.0.1` | 监听地址。**别改成 0.0.0.0**，本服务没有鉴权 |
| `ADSKIPER_PORT` | `8000` | 监听端口 |
| `ADSKIPER_MEDIA_HOSTS` | 空 | 追加媒体 CDN 域名后缀，逗号分隔 |
| `ADSKIPER_RESOLVE_TTL` | `3000` | 直链解析缓存秒数（签名 URL 约 2 小时过期） |
| `ADSKIPER_NODE_BIN` | `node` | 签名助手用的 Node 可执行文件。Node 是 nvm / Homebrew 装的、又不在当前 PATH 里时，用绝对路径指过去（如 `/opt/homebrew/bin/node`） |
| `ADSKIPER_FFMPEG` | 从 PATH 找 | 下载 hanime 视频用的 ffmpeg 可执行文件。不在 PATH 里时用绝对路径指过去 |

---

## 支持的站点

三个站点，分属**两种截然不同的形态**：

| 站点 | 形态 | 搜索 | 取流 |
|---|---|---|---|
| **RedTube** | 服务端渲染的列表页 | 抓 HTML，36 条/页 | yt-dlp |
| **PornHub** | 服务端渲染的列表页 | 抓 HTML，38 条/页 | yt-dlp |
| **hanime.tv** | Astro 前端 + JSON API | **全量索引 + 本地过滤** | **WASM 签名握手 → HLS** |

前两个同属 Aylo（原 MindGeek），页面结构、播放器、CDN 都很相近。
hanime 完全不同，逻辑单独放在 `hanime.py` 里，**它挂了不影响另外两个**。

站点切换是搜索框右边的**并排按钮**，一次点击切换，选择记在 localStorage。

### ⚠️ 站点清单只管"按钮上能选什么"

`SUPPORTED_SITES` 只决定界面上的站点按钮。前两个站走 yt-dlp，所以理论上**粘任何 yt-dlp
支持的链接都能播** —— 哪怕不在清单里。

### 为什么只有这三个站能"搜关键词"

yt-dlp 的 `RedTubeIE._VALID_URL` 只匹配视频 ID 形式的 URL，**不提供搜索**；
`_SEARCH_KEY` 机制也只有 YouTube / Bilibili / SoundCloud 等少数站点实现了。
hanime 更是**完全没有 yt-dlp extractor**。
所以"能搜关键词"是本项目自己写的（`RedTubeSite` / `PornHubSite` / `hanime.py`），
不是 yt-dlp 给的能力。其余站点只能粘链接。

### 想扩展时的两个入口

1. **只加播放**：不用改代码，粘链接即可。若该站 CDN 不在
   `MEDIA_SUFFIXES`（会报 403「域名不在白名单内」），加进 `ADSKIPER_MEDIA_HOSTS`
   环境变量；若 CDN 要 Referer（403/404），加进 `DEFAULT_REFERER_BY_SUFFIX`。
2. **加搜索**：写一个 `SiteAdapter` 子类（见下节），并往 `SUPPORTED_SITES` 加一行。

---

## hanime.tv（形态不同，逻辑单独隔离）

hanime 是**番剧站**，和 tube 站完全不是一回事：yt-dlp 不支持、取流要过一道
WASM 签名的握手、搜索接口一次返回全量索引。相关代码在 `hanime.py` +
`hanime_signer.js`，**整体可降级**：目录接口挂了只会让这个站点不可用。

### 三条链路

```
目录（不需要认证、不需要签名）
    GET guest.freeanimehentai.net/api/v11/search_hvs
    → 一次性返回全量索引（约 3400 条 / 4MB），带结构化 tags
    → 搜索、标签过滤、排序、分页全部在本地做（缓存 30 分钟）

取流（需要签名）
    POST auth.hanime.tv/api/v11/handshake
    ← 头：x-signature-version: web2 / x-signature / x-time
    → 响应头 x-token（AES-256-GCM 封装的 JSON）→ sources[] → HLS 地址

播放
    m3u8 经 /api/media 中转时**改写每条 URI**，让分片和 AES 密钥也走代理
    → 前端用 hls.js 播放
```

### 签名怎么来的（关键设计）

hanime 把签名算法打在一个 Emscripten 模块里（`hanime-cdn.com/js/vendor.<hash>.min.js`）。
本项目的做法**移植自 [DonMecca/hanime-stremio](https://github.com/DonMecca/hanime-stremio)**
（MIT, Copyright (c) 2025 Anime Source）：不逆向算法、不启动无头浏览器，而是

1. 在 Node 里伪造最小的浏览器全局对象（`window` / `document` / `navigator` / `CustomEvent`）
2. **`process.type = 'renderer'`** —— 把 Emscripten 胶水层逼到「内嵌 base64 WASM」
   分支，否则它会去 fs 找一个并不存在的 `.wasm` 文件
3. `require` 那份 vendor，等它把 `window.stime` 填上
4. 之后每 `dispatchEvent(new CustomEvent('e'))` 一次，它就重算一次签名

**但那个 200KB 的 vendor 文件是 hanime.tv 的专有代码**（不属于本项目，也不属于任何
MIT 参考项目），所以本项目**不打包、不分发**它 —— 改由 Python 通过代理从官方 CDN
运行时抓取并缓存到 `.cache/hanime/`。顺带的好处是站点更新签名后会自动跟上。

> 想要"完全照搬参考项目布局"（把 vendor 文件提交进仓库）也完全可行，技术上等价。
> 这里选择运行时抓取，是为了让本项目分发的只有自己写的代码。

### 账号：不做

游客（不登录）能拿到 **≤720p**（实测 720p/480p/360p）。1080p 那一档返回的是
`kind: "promotion"` 且 `src` 为空的**推广占位**，需要登录才能解锁。
本项目**不做任何账号功能**，所以1080p 不可用。

### 广告：天然免疫

握手响应里带 `is_preroll_enabled` / `preroll_urls`（实测指向 TrafficJunky 等），
页面里还有 `adv1.clickadu.net` 的 iframe 广告。本项目**只取 `sources`，完全忽略这些
字段**，也不加载站点页面 —— 所以三类广告一个都不会出现。

### 前端适配

hanime 的形态不同，所以界面上做了四处自适应：

1. **标签筛选条** —— 只在选中有结构化标签的站点时出现。61 个标签渲染成可点的
   chips（默认显示前 24 个 + 「全部」展开），点击即筛选、**多选＝AND**、再点取消。
   顶部提示条也换成该站点的说明（因为它的搜索语义和 tube 站不同）。
2. **竖版封面** —— 番剧封面是 268×394，网格换成 `.grid.portrait` 的竖版比例，
   并在卡片上露出前 3 个标签。
3. **hls.js 播放** —— `formats[].is_hls=true` 时走 hls.js（浏览器原生只有 Safari
   支持 m3u8）。hls.js 从 CDN 引入（Apache-2.0），取不到会给出明确提示，不影响其它站。
4. **播放页的标签行** —— 标题下方把该视频的所有标签横向铺开（`.ptags`）。
   一条番剧实测 **2~27 个**（中位 10、平均 10.5），所以是 `flex-wrap: wrap`
   的多行横排，而不是单行横滚 —— 单行会把一半标签挤出可视区。

   **只有 hanime 显示标签**，这是刻意的：它的 tag 是站方给定的结构化元数据；
   tube 站则不适合 —— 实测 RedTube 的 `tags`/`categories` 两个字段都是空的，
   PornHub 会同时给 11 个 `categories` + 16 个自由 `tags`（还混着意大利语关键词），
   铺在标题下只会变成噪音。所以 `_normalize_info()`（yt-dlp 那条路）**故意不返回
   `tags` 字段**，前端 `renderPlayerTags()` 拿到空值就整行隐藏，
   UI 里不需要任何按站点特判的逻辑。想让 tube 站也显示，在后端加上字段即可。

### 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ADSKIPER_HANIME_CATALOG_TTL` | `1800` | 目录缓存秒数 |
| `ADSKIPER_HANIME_STREAM_TTL` | `5400` | 取流结果缓存秒数 |
| `ADSKIPER_HANIME_CACHE` | `.cache/hanime` | vendor 文件缓存目录 |
| `HANIME_BOOT_TIMEOUT_MS` | `15000` | 签名助手启动超时（传给 Node） |

### 自检脚本

`tools/hanime_probe.py` 是**独立验证脚本**，可以在不启动主程序的情况下确认整条链路：

```bash
python tools/hanime_probe.py            # 用内置 slug
python tools/hanime_probe.py <slug>     # 指定 slug
```

它会依次打印：vendor 地址 → 签名 → 握手 HTTP 状态 → 解密后的 `sources`。
站点改版时先跑这个，能快速判断是"我们坏了"还是"站点变了"。

它认主程序那两个环境变量（`ADSKIPER_PROXY` / `ADSKIPER_NODE_BIN`），
所以「主程序怎么连、用哪个 node」可以直接照搬过来。不设 `ADSKIPER_PROXY`
时同样默认 `127.0.0.1:7890`；用系统级 VPN（Shadowrocket / Surge）的话要显式直连：

```bash
ADSKIPER_PROXY= python tools/hanime_probe.py
```

---

## 搜索格式：空格分隔多个词 = AND

**这是本站唯一的搜索语法**，界面上有常驻提示。

```
lesbian                      → 全部含这个词
lesbian threesome            → 同时含这两个词
lesbian threesome outdoor    → 三个词都要命中
```

实测收窄效果（RedTube，用站点自己返回的 `searchCount`）：

| 搜索词 | 结果总数 |
|---|---|
| `lesbian` | 82,103 |
| `lesbian threesome` | **9,533** |
| `lesbian threesome outdoor` | **312** |

三条实测性质：

- **顺序无关**：`lesbian threesome` 与 `threesome lesbian` 返回同一个结果集。
- **草稿词会被忽略**：`lesbian zzzqqqww` ≈ `lesbian`，不会因为一个词不认得就返回 0。全是生造词才返回 0。
- **`+` 也当分隔符**：站点 URL 本身就是 `a+b+c` 形式，所以从别处复制过来的 `lesbian+threesome`
  能直接用。这也意味着**不能搜字面加号**（本站场景下无影响）。

实现要点在 `app.py` 的 `split_terms()` / `encode_query()`：

```python
def encode_query(q: str) -> str:
    # 不要对整串调用 quote() —— '+' 会被转义成 %2B（字面加号），
    # 站点就不再按多词 AND 处理了。必须逐个词编码再用 '+' 连接。
    return "+".join(quote(t, safe="") for t in split_terms(q))
```

最多取 8 个词（`MAX_TERMS`），防止拼出离谱的长查询。

---

## 随机推荐（关键词为空时的默认内容）

关键词框为空、且没选任何标签时，列表区显示 **10 个随机视频**，格式和搜索结果完全一样
（点开就能播）。搜索格式提示行的正下方有个 **「🎲 刷新」** 按钮换一批；
**一旦搜索框有内容或选中了标签，按钮就置灰**——有搜索条件时"随机"没有意义。

三个站的实现方式不同，因为它们的"随机"能力差很远：

| 站点 | 做法 | 候选池 |
|---|---|---|
| RedTube | 站点没有随机接口，`?page=N` 还一律 404 → 在 6 个固定列表页里随机挑 | 六个入口合计约 **225** 条 |
| PornHub | `?o=<排序档>&page=<随机页>`，排序档 × 页码都能用 | 每页 35~47 条，页码空间很大 |
| hanime | 全量目录本来就在内存里（约 3400 条）→ **真随机** | **3404** 条 |

### 关键设计：记住最近给过的，新一轮避开

"随机"最容易做错的地方是**只看池子大小**。实测过一件事：把单页候选池
（PornHub 47 条）换成每站累积的大池子（涨到 500+ 条）之后，用
「任意两次抽样的期望重叠」去衡量是变好的，但**用户的体感反而变差** ——
因为用户对比的是**上一次和这一次**。池子大了，任意两两组合的重叠总量会上升，
而"连续两次"的重叠才是眼睛能看到的东西。

所以真正的解法不是把池子做大，而是**直接排除最近展示过的条目**：

```python
_recent_shown: dict[str, deque[str]] = {}       # 每站记最近 6 轮
RECENT_WINDOW = 60                              # 10 个/轮 × 6 轮

def _pick_random(pool, count, site_key):
    recent = _recent_shown.setdefault(site_key, deque(maxlen=RECENT_WINDOW))
    fresh = [x for x in pool if x["id"] not in recent]
    if len(fresh) < count:
        fresh = pool        # 池子快被"避"空了就放宽，宁可重复也不能越刷越少
    picks = random.sample(fresh, min(count, len(fresh)))
    recent.extend(x["id"] for x in picks)
    return picks
```

实测结果（三站各连续刷新 12 次，每次 10 个）：

| 站点 | 相邻两次的重叠 | 12 轮共 120 个位置 / 不同 id |
|---|---|---|
| RedTube | **0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0** | 120 / 113 |
| PornHub | **0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0** | 120 / 118 |
| hanime | **0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0** | 120 / 120 |

（RedTube 只有 225 条候选，12 轮之后自然开始饱和；hanime 池子最大所以一个不重。）

---

### 前端接线

- 首次载入、以及**关键词为空时切换站点** → 自动加载随机推荐
- 搜索框空着点「搜索」→ 也是加载随机推荐（而不是什么都不做）
- 搜索框的 `input` 事件、`doSearch`、`runSearch`、`selectSite` 都会调 `updateShuffleBtn()`
- 随机推荐的返回里 `total_pages` 为 `null`，`renderGrid()` 据此**隐藏分页条**
- 「← 返回列表」的判据从 `!state.q` 改成 `!state.q && !state.random`

**每次打开都是随机推荐**：`init()` 里**不重放**上次的搜索词 —— 早先的实现会在刷新页面时
自动重跑 `localStorage.lastQuery`，那样搜索框非空、刷新按钮是灰的，就看不到随机推荐了。
既然改成了"打开即随机"，`lastQuery` 就没有任何地方会读它，所以连写入也一并删掉了。
**搜索词现在不跨会话保留**；想接着上次搜就重新输一遍。

---

## 播放历史记录

右上角最右边是 **「历史记录」** 按钮（再点一次退出，退出后回到随机推荐）。点进去后
**复用搜索结果的卡片网格**，所以缩略图、时长角标、点击播放的行为都和搜索页一致。

历史存在 `localStorage.history`，规则只有三条：

| 规则 | 说明 |
|---|---|
| **重看只覆盖，不新增** | 同一个 URL 第二次播放时，用最新的标题/封面/时长覆盖旧记录并提到最前。实测「看 3 条 → 重看第 1 条」之后仍然是 3 条 |
| **不自动删除** | 只有用户手动删才会消失。`HISTORY_MAX = 2000` 纯粹是 localStorage 配额的安全阀（单条约 250B，2000 条约 0.5MB），正常个人使用到不了 |
| **删除要两步** | 见下 |

### 删除：两步确认 + 选择模式

```
[删除]  ──点一下──▶  进入选择模式：卡片可勾选
                     按钮变 [确定删除]，旁边出现 [取消]
                     没勾任何项时 [确定删除] 是灰的
         ──再点一下──▶  彻底删除勾选的记录（同时写回 localStorage）
```

- 一条记录都没有时 **[删除] 本身就是灰的**
- 选择模式下点卡片 = 勾选/取消（直接改 class，**不整页重绘**，几千条也不卡）
- 勾选状态用 `.card.pick.sel` 表达：绿色描边 + 右上角打勾
- **[取消]** 用于反悔 —— 破坏性操作没有退路是设计缺陷，所以多加了这一个按钮

### 两个实现细节

**逐卡片的竖版封面**：历史里会混着番剧（竖版 268×394 封面）和 tube 站（横版 16:9），
所以竖版比例**按每一条记录判断**（`card.classList.toggle("portrait", ...)`），
不能只看当前选中的站点 —— 搜索页因为只有一个站点，仍然用网格级的 `.grid.portrait`。

**记录自己的站点**：`playItem()` 用 `it.site || state.site`，卡片封面兜底请求
（`/api/thumb?id=&site=`）也带各自的站点。否则从历史里点开一个 hanime 的番剧、
而当前站点选的是 RedTube 时，封面会去 RedTube 找，直接 404。

### 旧数据迁移

早期版本用 `localStorage.recents`（只留 12 条、没有封面）。`migrateRecents()` 在
`init()` 里跑一次把它们并进 `history`，然后删掉旧 key —— 不会丢已攒下的记录。
旧记录没有封面字段，前端的封面三级回退会自动去 `/api/thumb` 补。

### 自检脚本

`tools/history_check.js` 用一个极简 DOM 桩把 `index.html` 里的 `<script>` 真跑起来，
按真实操作顺序验证上面这些规则（写入/覆盖/排序/迁移/选择/删除/取消/空状态）：

```bash
node tools/history_check.js      # 56 项断言，无需启动服务、无第三方依赖
```

历史逻辑全在前端且直接动 localStorage，"重看变成两条记录"或"删除没写回本地"
这类错误光看代码很容易漏，所以值得有个可重复跑的检查。

---

## 下载（另存为）

播放页左上角有 **「下载」** 按钮，存当前选中的清晰度。三个站都支持，但底下的机制
完全不同 —— 这也是为什么 hanime 的下载要额外依赖 ffmpeg。

### 为什么 tube 站"右键就能存"，hanime 不行

| | 形态 | 能不能直接存 |
|---|---|---|
| RedTube / PornHub | **单个渐进式 MP4**，一个 URL 就是一个完整文件 | ✅ 浏览器自带的下载按钮就能存 |
| hanime | **HLS**：m3u8 只是一张目录，真正的视频是 **142 个 AES-128 加密的分片**（实测一部 44 分钟的番剧 ≈ 294 MB） | ❌ 没有"一个文件"可存 |

在 hanime 上直接"下载"，存下来的是**播放列表文本文件**（十几到几十 KB，记事本打开是
`#EXTM3U` 开头的一堆 URL），不是视频。而且那些分片还是加密的，光拖下来也播不了。

### 两条路

```
tube 站  →  /api/download 返回 307，跳到 /api/media 并带上附件头
            复用那边已经写好的 Range / Referer / 域名白名单逻辑，一行都不重复

hanime   →  /api/download 起一个 ffmpeg 子进程：
            ffmpeg 读「我们自己代理改写过的 m3u8」→ 解密 → 封装成 MP4 → 管道流出
            不落盘：294 MB 先写磁盘要么占空间，要么让用户对着没有反馈的按钮干等
```

让 ffmpeg 读**经本机 `/api/media` 中转**的播放列表是个关键设计：分片和 AES 密钥都会
走我们的出口，于是 **ffmpeg 自己完全不用配代理**，也不受域名白名单影响。

### 三个必须记住的 ffmpeg 参数（都是实测踩出来的）

```bash
ffmpeg -i <经 /api/media 的 m3u8> -c copy \
       -bsf:a aac_adtstoasc \
       -movflags frag_keyframe+empty_moov+default_base_moof \
       -f mp4 pipe:1
```

| 参数 | 不加会怎样 |
|---|---|
| `-bsf:a aac_adtstoasc` | HLS 分片里的 AAC 带 ADTS 头，塞进 MP4 必须剥掉。**写普通文件时 ffmpeg 会自动补这个滤镜，输出到管道时不会** —— 不加直接报 `Malformed AAC bitstream detected`，只产出 2 KB 垃圾 |
| `frag_keyframe+empty_moov` | 管道不可 seek，`+faststart` 会直接报 `muxer does not support non seekable output`。改用 fragmented MP4，moov 写在最前面（实测偏移 32），才能边生成边发 |
| `-nostdin` | 防止 ffmpeg 去抢主进程的 stdin |

### 实测数据

一部 44 分 27 秒的番剧（720p）：

| 项 | 结果 |
|---|---|
| 体积 | 293.4 MB |
| 首字节 | **0.6 秒**（浏览器立刻开始有进度，不用等整部） |
| 完整下载 | **19.7 秒**（14.91 MB/s） |
| ffprobe | h264 1280×720 + aac，时长 2666.66 秒 |
| 全程解码校验 | `ffmpeg -f null -` **零错误** |

> 没有 ffmpeg 时会返回一句明确的中文错误，并且**只影响 hanime 的下载** ——
> 播放、以及 RedTube / PornHub 的下载都不受影响。前端也会提前拦下来，不会让浏览器
> 跳到一个 JSON 报错页（下载用隐藏 iframe 触发，错误就渲染在看不见的地方）。

---

## 加一个新站点的搜索适配器

继承 `SiteAdapter`，注册进 `SITES`，再往 `SUPPORTED_SITES` 加一行（站点按钮由它驱动）。
**`key` 必须和 yt-dlp 的 `IE_NAME` 完全一致**（注意 RedTube / PornHub 是大写驼峰）。

```python
class SomeSite(SiteAdapter):
    key, name, base = "SomeSite", "SomeSite", "https://www.somesite.com"
    cookie = ""

    @classmethod
    def search_url(cls, q, page):
        return f"{cls.base}/video/search?search={quote(q)}&page={page}"

    @classmethod
    def video_page_url(cls, vid):
        # /api/thumb 取 og:image 兜底封面时要用
        return f"{cls.base}/view/{vid}"

    @classmethod
    def parse_search(cls, body, page=1):
        ...
        return items, total_pages, total_count   # 必须是三元组

SITES[SomeSite.key] = SomeSite
```

两个实测得来的注意点：

1. **最后一块必须按 `</li>` 截断**。卡片是按 `<li … data-video-id>` 切分的，最后一块会
   一直延伸到页面末尾，不截断就会把整页尾部的 `<img>` 都当成该视频的封面候选
   （实测能凑出 30 个垃圾候选）。
2. **结果里的 `id` 要是"能拼出视频页 URL"的那个值**。RedTube 用数字 id，PornHub 用
   `viewkey`（十六进制串，`view_video.php?viewkey=…`），所以两边的 `video_page_url()`
   实现不同。

顺便把该站 CDN 域名加进 `MEDIA_SUFFIXES`（或环境变量 `ADSKIPER_MEDIA_HOSTS`），
需要校验 Referer 的再加进 `DEFAULT_REFERER_BY_SUFFIX`。

### PornHub 的实测要点

| 项 | 结论 |
|---|---|
| 搜索页 | `https://www.pornhub.com/video/search?search=<q>&page=<n>`，38 条/页，**不需要 cookie**（但仍带上年龄 cookie 更稳） |
| 总页数 | **页面上不提供**，只能在"下一页按钮 disabled"时推断出当前页就是末页，所以 `total_pages` 通常是 `None` |
| 视频页 | `view_video.php?viewkey=<vkey>` |
| CDN | `*.phncdn.com`，**必须带 Referer**（不带直接 404，不是 403），已由 `DEFAULT_REFERER_BY_SUFFIX` 覆盖 |
| URL 签名 | 带 **`ip=` 参数**，按请求 IP 签名 —— 所以解析和取流**必须走同一个出口代理**，否则 404 |
| 限速 | 形如 `rate=500k`，数值对应码率（720P_4000K → 500KB/s ≈ 4Mbps），正常播放够用；**测试时务必带 `Range`**，否则会拉整个几百 MB 文件 |

---

## API

| 端点 | 说明 |
|---|---|
| `GET /api/sites` | 支持的站点（含 `supports_search`）。顶层还有 `ffmpeg` 字段：`null` = 没找到 |
| `GET /api/random?site=&count=` | 随机推荐。`count` 默认 10、上限 60。返回结构和 `/api/search` 一致，多一个 `random: true` |
| `GET /api/search?q=&page=&site=` | 搜索。`q` 用空格分隔多个词（AND）。返回里带 `terms` / `term_count` |
| `GET /api/thumb?id=&site=` | 兜底封面：取视频页 `og:image` |
| `GET /api/resolve?url=` 或 `?id=&site=` | 解析直链，`&refresh=1` 强制刷新 |
| `GET /api/download?url=` 或 `?id=&site=` | **另存为**。`&quality=720p` 选清晰度，留空 = 最高。tube 站返回 307 跳去 `/api/media`；hanime 由 ffmpeg 流式吐 MP4 |
| `GET /api/media?u=&r=` | Range-aware 流式中转（m3u8 会自动改写 URI）。`&dl=<文件名>` 则作为附件下载（带 RFC 5987 文件名，中文标题不乱码） |
| `GET /api/docs` | 自动生成的接口文档 |

`?id=` 的形状**各站不同**（RedTube 纯数字、PornHub 是 viewkey、hanime 是 slug），
所以后端只校验 URL 安全字符，页面 URL 交给各站适配器的 `video_page_url()` 拼。
想按 `id` 调这个接口，直接用 `/api/search` 或 `/api/random` 返回里的 `id` 字段即可。

---

## 许可证

### 本项目本身：MIT

见仓库根目录的 [`LICENSE`](LICENSE)。可以自由使用、修改、分发、商用、闭源二次开发，
只要保留版权声明和许可声明。

选 MIT 而不是别的，是因为它和下面这张依赖表的方向一致 —— 全部宽松，没有任何 copyleft
需要你去兼容，所以你拿这个项目再做什么都不受牵连。

### 本项目的依赖 —— 全部宽松许可

| 组件 | 许可证 | 商用 |
|---|---|---|
| **yt-dlp** | **Unlicense**（公有领域） | ✅ 最宽松，无任何限制 |
| fastapi / pydantic / anyio / h11 | MIT | ✅ |
| uvicorn / httpx / starlette / httpcore / click / idna | BSD-3-Clause | ✅ |
| certifi | MPL-2.0 | ✅ 文件级 copyleft，作为依赖使用无影响 |
| **cryptography** | Apache-2.0 OR BSD-3-Clause | ✅ |
| **hls.js**（前端 CDN 引入） | Apache-2.0 | ✅ |
| **Node.js**（签名助手） | MIT | ✅ |

**本项目不包含任何 copyleft（GPL/AGPL）依赖**，所以你自己项目的许可证可以自由选择
（MIT / Apache / 闭源商业都行）—— 本项目自己选了 MIT。

### 借鉴过的项目

| 项目 | 许可证 | 用法 |
|---|---|---|
| [DonMecca/hanime-stremio](https://github.com/DonMecca/hanime-stremio) | **MIT** | `hanime_signer.js` 的浏览器环境伪造思路**移植自此**，已在文件头注明出处与版权 |
| [anime-src/hanime-stremio](https://github.com/anime-src/hanime-stremio) | **MIT** | 交叉参考（Node `vm` 沙箱方案、`refresh-htv-signature` 思路） |
| [MatrixRobots/Hanime-Api](https://github.com/MatrixRobots/Hanime-Api) | **MIT** | 交叉参考（Python 侧结构、运行时抓 vendor 的做法） |

MIT 的复用条件是**保留版权声明**，本项目的相关文件里都写了。

⚠️ **刻意避开**的项目（因为许可证不合适）：

| 项目 | 许可证 | 为什么不用 |
|---|---|---|
| Lysagxra/HanimeDownloader | **GPL-3.0** | copyleft 传染，抄进来会污染整个项目 |
| Kraptor123/Cs-GizliKeyif | **无许可证** | 默认保留所有权利，只能看不能抄 |
| rxqv/htv | **无许可证** | 同上 |

### ⚠️ 一个重要例外：那个 200KB 的 vendor 文件

hanime 的 `vendor.<hash>.min.js` 是**hanime.tv 自己的专有代码**，
**MIT 覆盖不到它**（MIT 只保护参考项目作者自己写的那部分）。

所以本项目**不分发、不打包**它，改为运行时从官方 CDN 抓取到 `.cache/hanime/`
（该目录已在 `.gitignore` 里）。
这样本项目仓库里只有自己写的代码。

### 许可证 ≠ 用途合法（这条比上面所有都重要）

上面的"可商用"指的是**这些代码**可以商用。但代码许可证管不到"你拿它访问什么"，而后者是**独立的法律问题**：

- 绕过 hanime.tv 的签名访问控制，可能触及其服务条款里的**反规避条款**
- hanime.tv 分发的**内容本身授权状况不明** —— 见下

#### 关于 hanime.tv 的内容授权：这是推断，不是已证实的事实

我**没有**查到任何直接证据（判决、授权清单、侵权认定）。下面是我能查到的客观事实和我据此做的推断，请分开看：

**查得到的事实**

| 项 | 结果 |
|---|---|
| 首页 HTML 里 `dmca` / `copyright` / `all rights reserved` / `legal` / `abuse` | **各出现 0 次**（没有版权声明，也没有侵权投诉入口） |
| 有没有服务条款 | 有（`/terms-of-use`、`/privacy`） |
| 条款怎么描述内容 | 主张"**所有内容受版权保护**"，署名为 `HTV Network`；对侵权只给一个笼统的 **Contact Us Form**；全篇 `dmca` / `rights holder` / `takedown` 均 0 次 |
| 条款有没有说内容从版权方获得授权 | **没有**。正规平台通常会写明授权来源 |
| 运营方 | `HTV Network` —— 与实测抓到的 CDN 域名 `htv-services.com` / `htv-elidibus.com` 对得上 |

**我们自己逆向出来的旁证**（这一档最硬，因为是实测，不是猜测）

取流链路带有明显的**规避封锁**设计：分片 CDN 是轮换域名池（实测 41 个播放列表里出现 48 个不同主机）、取流要过一道混淆的 WASM 签名握手、视频分片用 `.html` 扩展名配 `text/html` 伪装成网页、站点本身被 GFW 屏蔽。**正规流媒体不需要这么做。**

**推断**

上面这些旁证指向"内容很可能未获授权"：播放的是有商业发行的日本成人动画（在 FANZA / DLsite 上正常售卖），却能免费/低价访问，同时查不到任何授权披露，反而有规避封锁的工程特征。

**但我无法排除**它和某些片方存在私下安排、或片方默许的可能。所以结论只能是：**授权状况不明，多项旁证指向未授权。** 本项目也不加载任何站点页面、不下发它的广告。

#### 正规渠道

这类内容的**正规渠道主要在**日本国内，且**大多锁区**（需要日本支付方式，部分内容海外买不到；还有"修正版 / 無修正版"的区别）：

| 渠道 | 性质 |
|---|---|
| [AnimeFesta](https://animefesta.iowl.jp/) | 成人动画的官方流媒体平台（WWWave 运营），不少新番独占配信 |
| FANZA（DMM 旗下） | 最大的成人向平台，卖/租动画本编与 DL 版 |
| DLsite | 同人 + 商业成人作品 |
| 各制作公司官网 | Mary Jane / Queen Bee / Pink Pineapple / Lune Soft 等，通常也走 FANZA |

#### 为什么这类站还能存在

- **运营主体在执法薄弱地区**：条款从不披露注册地，跨境维权成本极高
- **权利人分散且小**：成人动画多是小工作室，没有跨国诉讼的预算
- **域名 / CDN 轮换让封锁失效**：这正是上面那套规避设计的用途
- **题材降低了维权意愿**：片方公开主张"我的色情动画被盗版"的声誉成本不低，压力往往改从**支付通道**下手
- **但不是永远安全**：2026-07，大型动漫盗版站 HiAnime 被关停、运营者被捕（MPA 牵头的 ACE 联盟证实）。方向是明确的，只是成人动画离这条线还比较远

> 以上是对**观察到的事实**与**推断**的区分，**不构成法律意见**。若要商业使用，请自行评估用途侧的风险。

---

## 注意

仅用于个人学习与自用。绕过广告与访问控制属于违反站点服务条款的行为。
上一节说明了各依赖的许可证，但**不构成法律意见**；若要商业使用，请自行评估用途侧的风险。
