<div align="center">

# Douyin Downloader

**一条命令，把抖音视频变成可读的文字稿**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

---

## 痛点

- 抖音没有导出文字稿的功能，想回顾内容只能重新看一遍
- 批量下载工具多，但下完还得手动转录，流程断裂
- 博主几十上百个视频，一个个处理太慢，中途失败还得从头来
- Whisper 转录出来是一整块 raw text，没有分段，根本没法读

## 解决方案

粘贴一个链接，自动完成 **下载 → 转录 → 格式化 → 保存**，每个视频生成一个干净的 Markdown 文件。

- **单个视频**：粘贴视频链接，直接出文字稿
- **整个博主**：粘贴主页链接，自动翻页抓取全部视频，确认后批量处理
- **自动分段**：转录结果自动按语句分段，输出 human-readable 的文字稿
- **断点续传**：通过 `state.json` 自动跳过已完成的视频，中断后重跑即可
- **失败重试**：下载失败自动重试 2 次，无需人工干预

## 架构

```
URL
 │
 ▼
┌──────────────┐
│  类型检测     │  /video/xxx → 单视频模式
│              │  /user/xxx  → 批量模式
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

批量模式额外流程：翻页抓取全部视频 → 用户确认数量 → 分批并发下载（默认 5 个/批，2 路并发）

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

转录文本会自动按语句分段。如果需要原始输出，加 `--raw` 跳过格式化。

## 功能一览

| 功能 | 说明 | 状态 |
|------|------|------|
| 自动类型检测 | 粘贴链接自动识别单个视频 vs 用户主页 | 已完成 |
| 批量翻页抓取 | 自动滚动翻页，获取用户全部视频列表 | 已完成 |
| 并发下载 | 默认 2 路并发，内置随机延迟和批次暂停 | 已完成 |
| Whisper 转录 | Apple Silicon 自动使用 mlx-whisper（5-10x 加速） | 已完成 |
| 自动分段格式化 | 转录结果自动按语句分段，输出可读文字稿 | 已完成 |
| Cookie 持久化 | 登录状态保存到 `~/.douyin-cookies.json`，后续自动复用 | 已完成 |
| 断点续传 | `state.json` 记录进度，中断后重跑自动跳过已完成视频 | 已完成 |
| 自动重试 | 下载失败自动重试最多 2 次 | 已完成 |
| 反爬内置 | 随机延迟 8-18s、批次暂停 30-60s、WAF 自动等待 | 已完成 |

## 技术栈

| 组件 | 技术 | 用途 |
|------|------|------|
| 网页抓取 | Playwright (Chromium headless) | 页面渲染 + API 拦截 |
| 视频下载 | aiohttp | 异步 HTTP 下载 |
| 音频提取 | ffmpeg | 视频转音频 |
| 语音转文字 | mlx-whisper / openai-whisper | Apple Silicon 优化转录 |
| 并发控制 | asyncio + Semaphore | 限速 + 批量处理 |
| 语言 | Python 3.9+ | 核心运行时 |

## 项目结构

```
douyin-downloader/
├── scripts/
│   └── pipeline.py      # 主程序：下载 + 转录 + 格式化 pipeline
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
| `--max-videos` | `0`（全部） | 限制下载视频数量 |
| `--skip` | `0` | 跳过前 N 个视频 |
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
| 只抓到 18 个视频 | 需要登录 — 先在浏览器登录抖音 |
| WAF/Cloudflare 拦截 | 脚本会自动等待 45s，持续失败请稍后重试 |
| Whisper 内存不足 | 换用更小的模型：`--whisper-model tiny` |
| 音频提取失败 | 确认已安装 ffmpeg：`brew install ffmpeg` |
| 部分视频下载失败 | 脚本会自动重试，也可重跑自动跳过已完成的 |

## For AI Agents

本节面向需要将此项目作为工具集成的 AI Agent。

### 结构化元数据

```yaml
name: douyin-downloader
description: Download and transcribe Douyin videos to formatted text
version: 4.0
cli_command: python scripts/pipeline.py
cli_args:
  - name: url
    type: string
    required: true
    description: Douyin video URL (/video/xxx) or user profile URL (/user/xxx)
cli_flags:
  - name: --output-dir
    type: string
    default: ./transcripts
    description: Output directory for .md files
  - name: --whisper-model
    type: string
    default: small
    description: "Whisper model size: tiny/base/small/medium/large"
  - name: --language
    type: string
    default: zh
    description: Audio language code
  - name: --max-videos
    type: integer
    default: 0
    description: Max videos to process (0 = all, profile mode only)
  - name: --raw
    type: boolean
    default: false
    description: Skip formatting, output raw Whisper text
  - name: -y
    type: boolean
    default: false
    description: Skip confirmation prompt (required for non-interactive agent use)
input_format: Douyin URL (video or user profile)
output_format: Markdown files (one per video, auto-formatted paragraphs)
prerequisites:
  - playwright (pip install playwright && playwright install chromium)
  - aiohttp (pip install aiohttp)
  - mlx-whisper (pip install mlx-whisper)
  - ffmpeg (brew install ffmpeg)
capabilities:
  - "download single Douyin video and transcribe to text"
  - "batch download all videos from a Douyin user profile"
  - "auto-format transcripts into readable paragraphs"
  - "resume interrupted batch jobs via state.json"
  - "persist login cookies for authenticated scraping"
```

### Agent 调用示例

```python
import subprocess

# 单视频转录
def transcribe_video(url: str, output_dir: str = "./transcripts") -> str:
    result = subprocess.run(
        ["python", "scripts/pipeline.py", url,
         "--output-dir", output_dir, "-y"],
        capture_output=True, text=True,
    )
    return result.stdout

# 批量转录（限制数量）
def transcribe_profile(user_url: str, output_dir: str, max_videos: int = 10) -> str:
    result = subprocess.run(
        ["python", "scripts/pipeline.py", user_url,
         "--output-dir", output_dir,
         "--max-videos", str(max_videos), "-y"],
        capture_output=True, text=True,
    )
    return result.stdout
```

### Agent 工作流

```yaml
steps:
  - name: detect_url_type
    description: 判断输入是单个视频还是用户主页
    logic: |
      /video/\d+ → "video" (直接处理，无需确认)
      /user/    → "user"  (需要确认后批量处理)

  - name: run_single_video
    condition: url_type == "video"
    command: python scripts/pipeline.py "{url}" --output-dir {output_dir} -y

  - name: run_user_profile
    condition: url_type == "user"
    command: python scripts/pipeline.py "{url}" --output-dir {output_dir} -y --max-videos {limit}
    note: 务必先告知用户视频总数，获得确认后再执行

  - name: resume_after_interrupt
    description: 中断后重跑，自动跳过已完成视频
    command: python scripts/pipeline.py "{url}" --output-dir {output_dir} -y
    note: state.json 会自动跟踪进度，无需手动 --skip
```

### Claude Code Skill 安装

```bash
git clone https://github.com/zinan92/douyin-downloader.git \
  ~/.claude/skills/douyin-downloader
```

安装后 Claude 会自动识别抖音链接并提供转录服务。

## License

MIT
