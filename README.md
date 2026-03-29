# douyin-downloader

把抖音视频链接变成可读的 Markdown 文字稿。粘贴链接，自动完成下载、转录、分段格式化。

```
in  抖音视频链接 (/video/xxx) 或用户主页链接 (/user/xxx)
out 每个视频一个 .md 文件，含标题、日期、分段文字稿

fail URL 格式无法识别          → 报错退出，提示需要 /video/ 或 /user/ 链接
fail WAF/Cloudflare 拦截      → 自动等待 45s 重试，持续拦截则失败
fail 视频下载失败              → 自动重试 2 次，仍失败则写入 [下载失败] 占位并继续
fail Whisper 转录失败          → mlx-whisper 失败自动降级到 openai-whisper CLI
fail 未登录访问用户主页         → 只能抓到约 18 个视频，需先登录获取 cookie
fail 中途中断                  → state.json 记录进度，重跑自动跳过已完成视频
```

## 架构

```
URL
 │
 ▼
┌──────────────┐
│  类型检测     │  /video/xxx → 单视频模式
│              │  /user/xxx  → 批量模式（翻页抓取 + 用户确认）
└──────┬───────┘
       │
       ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Playwright  │────▶│   ffmpeg     │────▶│   Whisper    │
│  下载视频     │     │  提取音频     │     │  语音转文字   │
└──────────────┘     └──────────────┘     └──────┬───────┘
                                                  │
                                                  ▼
                                          ┌──────────────┐
                                          │  格式化 + 分段 │
                                          │  保存 .md     │
                                          └──────────────┘
```

批量模式额外流程：翻页抓取全部视频 → 用户确认数量 → 分批并发下载（默认 5 个/批，2 路并发，随机延迟 8-18s，批次暂停 30-60s）

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/zinan92/douyin-downloader.git
cd douyin-downloader

# 2. 安装依赖
pip install playwright aiohttp mlx-whisper
playwright install chromium
brew install ffmpeg  # macOS

# 3. 转录单个视频
python scripts/pipeline.py "https://www.douyin.com/video/7601234567890" \
  --output-dir ~/transcripts/

# 4. 批量转录某个博主
python scripts/pipeline.py "https://www.douyin.com/user/MS4wLjABAAAA..." \
  --output-dir ~/transcripts/blogger-name/
```

批量模式会显示视频总数并等待确认：

```
============================================================
找到 86 个视频，确定要全部下载并转录吗？
预计耗时：64 - 107 分钟
输入 y 继续，n 取消，或输入数字限制下载数量：
============================================================
>
```

## 输出格式

每个视频生成一个独立文件：`01-视频标题.md`、`02-视频标题.md`...

```markdown
# 视频标题

> 日期: 2026-03-10 | 来源: https://www.douyin.com/video/123456

今天想聊一个话题。就是AI时代的金融市场。如果我们把它具体细分的话。
分为一级市场和二级市场。二级市场比较简单。

我在这个行业做了五六年了。一级市场呢变化比较大。尤其是最近AI的发展。
让很多传统的模式都开始失效了。所以我觉得我们需要重新思考一下。
```

转录文本会自动按语句分段（约 5 句一段）。加 `--raw` 跳过格式化输出原始文本。

## 功能一览

| 功能 | 说明 |
|------|------|
| 自动类型检测 | 粘贴链接自动识别单个视频 vs 用户主页 |
| 批量翻页抓取 | 自动滚动翻页获取用户全部视频列表，API 拦截 + DOM 解析双保险 |
| 并发下载 | 默认 2 路并发，内置随机延迟和批次暂停 |
| Whisper 转录 | Apple Silicon 自动使用 mlx-whisper 加速，fallback 到 openai-whisper CLI |
| 自动分段格式化 | 转录结果按句号/感叹号/问号分段，输出可读文字稿 |
| Cookie 持久化 | 登录状态保存到 `~/.douyin-cookies.json`，后续自动复用 |
| 断点续传 | `state.json` 记录已完成视频 ID，中断后重跑自动跳过 |
| 自动重试 | 下载失败自动重试最多 2 次，随机等待 5-15s |
| 反爬内置 | 随机延迟 8-18s、批次暂停 30-60s、WAF 自动等待 45s |

## 技术栈

| 组件 | 技术 | 用途 |
|------|------|------|
| 网页抓取 | Playwright (Chromium headless) | 页面渲染 + API 拦截获取视频源 |
| 视频下载 | aiohttp | 异步 HTTP 分块下载 |
| 音频提取 | ffmpeg | 视频转音频（优先 aac copy，fallback mp3） |
| 语音转文字 | mlx-whisper / openai-whisper | Apple Silicon 优化转录 |
| 并发控制 | asyncio + Semaphore | 限速 + 批量处理 |
| 语言 | Python 3.9+ | 核心运行时 |

## 项目结构

```
douyin-downloader/
├── scripts/
│   └── pipeline.py      # 主程序：下载 + 转录 + 格式化 pipeline（单文件，~960 行）
├── SKILL.md             # Claude Code skill 描述文件
├── LICENSE              # MIT
└── README.md
```

## 配置

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `url` | (必填) | 抖音视频或用户主页链接 |
| `--output-dir` | `./transcripts` | 输出目录，每个视频一个 .md 文件 |
| `--whisper-model` | `small` | Whisper 模型：`tiny` / `base` / `small` / `medium` / `large` |
| `--language` | `zh` | 转录语言 |
| `--max-videos` | `0`（全部） | 限制下载视频数量（仅批量模式） |
| `--skip` | `0` | 跳过前 N 个待处理视频 |
| `--concurrency` | `2` | 并发下载数 |
| `--keep-audio` | `false` | 转录后保留音频文件 |
| `--raw` | `false` | 跳过格式化，输出原始 Whisper 文本 |
| `-y` / `--yes` | `false` | 跳过确认提示（用于自动化） |
| `--work-dir` | `/tmp/douyin-transcriber` | 中间文件临时目录 |
| `--cookies-file` | `~/.douyin-cookies.json` | Cookie 持久化文件路径 |

### Whisper 模型选择

| 模型 | 速度 | 质量 | 显存 |
|------|------|------|------|
| `tiny` | 最快 | 基础 | ~1 GB |
| `base` | 快 | 较好 | ~1 GB |
| `small` | 中等 | 很好 | ~2 GB |
| `medium` | 慢 | 优秀 | ~5 GB |
| `large` | 最慢 | 最佳 | ~10 GB |

### 常见问题

| 问题 | 解决方案 |
|------|----------|
| 只抓到 18 个视频 | 需要登录 — 先在浏览器登录抖音，cookie 会自动保存 |
| WAF/Cloudflare 拦截 | 脚本会自动等待 45s，持续失败请稍后重试 |
| Whisper 内存不足 | 换用更小的模型：`--whisper-model tiny` |
| 音频提取失败 | 确认已安装 ffmpeg：`brew install ffmpeg` |
| 部分视频下载失败 | 自动重试 2 次，也可重跑利用 state.json 自动跳过已完成的 |

## For AI Agents

### Capability Contract

```yaml
name: douyin-downloader
version: "4.0"
capability: 把抖音视频链接转为格式化 Markdown 文字稿
interface: CLI

input:
  type: string
  format: "Douyin URL — /video/{id} (单视频) 或 /user/{uid} (批量)"

output:
  type: file[]
  format: "Markdown files — {NN}-{title}.md, 含标题/日期/分段文字稿"
  location: "--output-dir 指定的目录"

dependencies:
  runtime: python 3.9+
  packages: [playwright, aiohttp, mlx-whisper]
  system: [ffmpeg, chromium]

failure_modes:
  - condition: "URL 不含 /video/ 或 /user/"
    behavior: "exit 1, stderr 提示格式"
  - condition: "视频下载失败（重试 2 次后）"
    behavior: "写入 [下载失败] 占位，继续处理下一个"
  - condition: "WAF 拦截超时"
    behavior: "等待 45s 后放弃当前页面"
  - condition: "进程中断"
    behavior: "state.json 保留进度，重跑自动续传"

flags:
  required_for_automation: "-y"
  note: "批量模式默认需要交互确认，agent 调用必须传 -y"
```

### Agent 调用示例

```python
import subprocess

# 单视频转录
result = subprocess.run(
    ["python", "scripts/pipeline.py",
     "https://www.douyin.com/video/7601234567890",
     "--output-dir", "./out", "-y"],
    capture_output=True, text=True,
)

# 批量转录（限制数量）
result = subprocess.run(
    ["python", "scripts/pipeline.py",
     "https://www.douyin.com/user/MS4wLjABAAAA...",
     "--output-dir", "./out",
     "--max-videos", "10", "-y"],
    capture_output=True, text=True,
)
```

### Agent 工作流

```yaml
steps:
  - name: detect_url_type
    logic: "/video/\\d+ → 单视频（直接处理）; /user/ → 批量（需确认）"

  - name: run_pipeline
    command: python scripts/pipeline.py "{url}" --output-dir {output_dir} -y
    note: "-y 是 agent 调用的硬性要求，否则会阻塞等待 stdin"

  - name: resume_after_interrupt
    command: python scripts/pipeline.py "{url}" --output-dir {output_dir} -y
    note: "state.json 自动跟踪进度，无需额外参数"
```

### Claude Code Skill 安装

```bash
git clone https://github.com/zinan92/douyin-downloader.git \
  ~/.claude/skills/douyin-downloader
```

安装后 Claude 会自动识别抖音链接并提供转录服务。

## License

MIT
