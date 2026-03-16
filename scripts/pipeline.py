"""
Douyin Downloader Pipeline (v4)
================================
Download and transcribe Douyin videos to text.

Supports:
- Single video URL: download + transcribe one video
- User profile URL: scrape all videos, ask for confirmation, then batch process

Features:
- One markdown file per video
- Cookie persistence (login once, reuse session)
- Auto-resume via state.json (skip already-completed videos)
- Auto-retry failed downloads (up to 2 retries)
- Built-in rate limiting

Usage:
    python pipeline.py "https://www.douyin.com/video/xxxxx" --output-dir ~/transcripts/
    python pipeline.py "https://www.douyin.com/user/xxxxx" --output-dir ~/transcripts/
"""

from __future__ import annotations

import asyncio
import argparse
import json
import logging
import os
import random
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

CHALLENGE_MAX_WAIT = 45
SCROLL_PAUSE = 3.0
SCROLL_MAX_NO_NEW = 10
DEFAULT_WORK_DIR = "/tmp/douyin-transcriber"
DEFAULT_COOKIES_FILE = os.path.expanduser("~/.douyin-cookies.json")
CONCURRENT_DOWNLOADS = 2
BATCH_SIZE = 5
BATCH_PAUSE_MIN = 30
BATCH_PAUSE_MAX = 60
DOWNLOAD_DELAY_MIN = 8
DOWNLOAD_DELAY_MAX = 18
MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# 0) URL type detection
# ---------------------------------------------------------------------------

def detect_url_type(url: str) -> str:
    """Returns 'video', 'user', or 'unknown'."""
    if re.search(r"/video/\d+", url):
        return "video"
    if re.search(r"/user/", url):
        return "user"
    return "unknown"


def extract_video_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/video/(\d+)", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# 0.5) Cookie persistence
# ---------------------------------------------------------------------------

def save_cookies(cookies: list, cookies_file: str) -> None:
    """Save browser cookies to a JSON file."""
    path = Path(cookies_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Cookies saved to {cookies_file} ({len(cookies)} cookies)")


def load_cookies(cookies_file: str) -> list:
    """Load cookies from a JSON file. Returns empty list if file doesn't exist."""
    path = Path(cookies_file)
    if not path.exists():
        return []
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cookies, list) and cookies:
            logger.info(f"Loaded {len(cookies)} cookies from {cookies_file}")
            return cookies
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load cookies: {e}")
    return []


# ---------------------------------------------------------------------------
# 0.6) State tracking (auto-resume)
# ---------------------------------------------------------------------------

class StateTracker:
    """Track completed video IDs for auto-resume. Persists to state.json in output dir."""

    def __init__(self, output_dir: Path):
        self._path = output_dir / "state.json"
        self._state = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    logger.info(
                        f"Resumed state: {len(data.get('completed', []))} completed, "
                        f"{len(data.get('failed', []))} failed"
                    )
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {"completed": [], "failed": []}

    def _save(self) -> None:
        self._path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def is_completed(self, video_id: str) -> bool:
        return video_id in self._state["completed"]

    def mark_completed(self, video_id: str) -> None:
        if video_id not in self._state["completed"]:
            self._state["completed"].append(video_id)
        # Remove from failed if it was there
        self._state["failed"] = [
            v for v in self._state["failed"] if v != video_id
        ]
        self._save()

    def mark_failed(self, video_id: str) -> None:
        if video_id not in self._state["failed"]:
            self._state["failed"].append(video_id)
        self._save()

    @property
    def completed_count(self) -> int:
        return len(self._state["completed"])

    @property
    def failed_ids(self) -> list:
        return list(self._state["failed"])


# ---------------------------------------------------------------------------
# 1) Scrape single video metadata
# ---------------------------------------------------------------------------

async def scrape_single_video(video_url: str, cookies_file: str = DEFAULT_COOKIES_FILE) -> list:
    """Return a single-element list with {url, title, date, video_id} for one video."""
    from playwright.async_api import async_playwright

    video_id = extract_video_id_from_url(video_url)
    if not video_id:
        logger.error(f"Cannot extract video ID from: {video_url}")
        return []

    result = {
        "url": video_url,
        "title": f"video_{video_id}",
        "date": "unknown",
        "video_id": video_id,
    }

    cookies = load_cookies(cookies_file)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )

        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()
        detail_data = None

        async def capture_detail(response):
            nonlocal detail_data
            try:
                if response.status == 200 and "/aweme/v1/web/aweme/detail/" in response.url:
                    detail_data = await response.json()
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(capture_detail(r)))

        try:
            await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"Page load error: {e}")

        await page.wait_for_timeout(5000)

        if detail_data and isinstance(detail_data, dict):
            aweme = detail_data.get("aweme_detail", {})
            if isinstance(aweme, dict):
                desc = aweme.get("desc", "").strip()
                if desc:
                    result["title"] = desc
                ct = aweme.get("create_time", 0)
                if ct:
                    result["date"] = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")

        if result["title"].startswith("video_"):
            try:
                page_title = await page.title()
                if page_title and "抖音" not in page_title:
                    result["title"] = page_title.strip()
            except Exception:
                pass

        # Save cookies for future use
        new_cookies = await context.cookies()
        if new_cookies:
            save_cookies(new_cookies, cookies_file)

        await browser.close()

    logger.info(f"Video: {result['title'][:60]} ({result['date']})")
    return [result]


# ---------------------------------------------------------------------------
# 2) Scrape user profile for video list
# ---------------------------------------------------------------------------

async def scrape_user_videos(
    user_url: str, max_videos: int = 0, cookies_file: str = DEFAULT_COOKIES_FILE,
) -> list:
    """Return list of {url, title, date, video_id} for every video on a user's profile."""
    from playwright.async_api import async_playwright

    seen_ids = set()
    api_videos = []
    cookies = load_cookies(cookies_file)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            viewport={"width": 1280, "height": 800},
        )

        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()

        async def block_assets(route):
            if route.request.resource_type in ("image", "font", "stylesheet", "media"):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_assets)

        first_api_url = None
        first_api_data = None
        api_capture_event = asyncio.Event()

        async def capture_post_api(response):
            nonlocal first_api_url, first_api_data
            try:
                url = response.url
                if response.status == 200 and "/aweme/v1/web/aweme/post/" in url:
                    data = await response.json()
                    if first_api_url is None:
                        first_api_url = url
                        first_api_data = data
                        api_capture_event.set()
                    for item in data.get("aweme_list", []):
                        vid = item.get("aweme_id", "")
                        if vid and vid not in seen_ids:
                            seen_ids.add(vid)
                            desc = item.get("desc", "").strip() or f"video_{vid}"
                            ct = item.get("create_time", 0)
                            date_str = (
                                datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
                                if ct else "unknown"
                            )
                            api_videos.append({
                                "url": f"https://www.douyin.com/video/{vid}",
                                "title": desc,
                                "date": date_str,
                                "video_id": vid,
                            })
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(capture_post_api(r)))

        logger.info(f"Loading user profile: {user_url}")
        try:
            await page.goto(user_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.error(f"Failed to load profile: {e}")
            await browser.close()
            return []

        # Wait for WAF
        deadline = time.monotonic() + CHALLENGE_MAX_WAIT
        while time.monotonic() < deadline:
            html = await page.content()
            if not any(m in html.lower() for m in ["please wait", "waf-jschallenge", "_wafchallengeid"]):
                break
            await page.wait_for_timeout(2000)

        await page.wait_for_timeout(3000)

        try:
            await asyncio.wait_for(api_capture_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for first API response")

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3000)

        # Paginate via scroll
        no_new_count = 0
        prev_count = len(api_videos)
        for _ in range(100):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.evaluate("""() => {
                const c = document.querySelector('.route-scroll-container');
                if (c) c.scrollTop = c.scrollHeight;
            }""")
            await page.wait_for_timeout(int(SCROLL_PAUSE * 1000))

            current_count = len(api_videos)
            if current_count == prev_count:
                no_new_count += 1
            else:
                no_new_count = 0
            prev_count = current_count

            if no_new_count >= SCROLL_MAX_NO_NEW:
                break
            if max_videos > 0 and current_count >= max_videos:
                break

        # Fallback to DOM
        if not api_videos:
            logger.info("API interception found no videos, falling back to DOM parsing")
            links = await page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('a[href*="/video/"]').forEach(a => {
                    const match = a.href.match(/\\/video\\/(\\d+)/);
                    if (match) results.push({
                        url: a.href,
                        title: a.textContent?.trim() || a.getAttribute('title') || '',
                        video_id: match[1]
                    });
                });
                return results;
            }""")
            for link in links:
                vid = link.get("video_id", "")
                if vid not in seen_ids:
                    seen_ids.add(vid)
                    api_videos.append({
                        "url": link["url"],
                        "title": link.get("title", f"video_{vid}"),
                        "date": "unknown",
                        "video_id": vid,
                    })

        # Save cookies for future use
        new_cookies = await context.cookies()
        if new_cookies:
            save_cookies(new_cookies, cookies_file)

        await browser.close()

    if max_videos > 0:
        api_videos = api_videos[:max_videos]

    logger.info(f"Total videos found: {len(api_videos)}")
    return api_videos


# ---------------------------------------------------------------------------
# 3) Download video & extract audio
# ---------------------------------------------------------------------------

async def _get_video_src(page, video_url: str) -> Optional[str]:
    """Navigate to video page and extract the video source URL."""
    src = None
    aweme_detail = None
    media_candidates = []
    response_tasks = []

    async def handle_response(response):
        nonlocal aweme_detail
        try:
            url = response.url
            if response.status in (200, 206) and "douyinvod.com" in url and url.startswith("http"):
                media_candidates.append(url)
            if response.status == 200 and "/aweme/v1/web/aweme/detail/" in url and aweme_detail is None:
                aweme_detail = await response.json()
        except Exception:
            pass

    page.on("response", lambda r: response_tasks.append(asyncio.create_task(handle_response(r))))

    try:
        await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        logger.warning(f"Page load error for {video_url}: {e}")
        return None

    deadline = time.monotonic() + CHALLENGE_MAX_WAIT
    while time.monotonic() < deadline:
        try:
            html = await page.content()
            if not any(m in html.lower() for m in ["please wait", "waf-jschallenge", "_wafchallengeid"]):
                break
        except Exception:
            pass
        await page.wait_for_timeout(2000)

    await page.wait_for_timeout(6000)
    if response_tasks:
        await asyncio.gather(*response_tasks, return_exceptions=True)

    if aweme_detail:
        src = _extract_src_from_detail(aweme_detail)
    if not src and media_candidates:
        src = media_candidates[0]
    if not src:
        try:
            src = await page.evaluate("""() => {
                const v = document.querySelector('video');
                if (!v) return null;
                if (v.src && v.src.startsWith('http')) return v.src;
                const sources = Array.from(v.querySelectorAll('source'));
                const mp4 = sources.find(s => s.type === 'video/mp4');
                return mp4 ? mp4.src : (sources[0] ? sources[0].src : null);
            }""")
        except Exception:
            src = None

    return src if (src and src.startswith("http")) else None


async def download_single_audio(
    video_url: str, output_audio: Path, semaphore: asyncio.Semaphore, delay: float = 0,
) -> bool:
    """Download one video and extract audio. Uses semaphore to limit concurrency."""
    import aiohttp
    from playwright.async_api import async_playwright

    if delay > 0:
        await asyncio.sleep(delay)

    async with semaphore:
        video_file = output_audio.with_suffix(".mp4")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            page = await context.new_page()

            async def block_assets(route):
                if route.request.resource_type in ("image", "font", "stylesheet"):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_assets)

            src = await _get_video_src(page, video_url)
            if not src:
                logger.warning(f"No video src found for {video_url}")
                await browser.close()
                return False
            logger.info("Got video src, downloading...")

            headers = {
                "User-Agent": await page.evaluate("navigator.userAgent"),
                "Referer": "https://www.douyin.com/",
            }
            await browser.close()

        async with aiohttp.ClientSession() as session:
            async with session.get(src, headers=headers, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status not in (200, 206):
                    logger.warning(f"Download failed: status {resp.status}")
                    return False
                with open(video_file, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_file), "-vn", "-acodec", "copy",
             str(output_audio.with_suffix(".aac"))],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_file), "-vn", "-acodec", "libmp3lame",
                 "-q:a", "4", str(output_audio)],
                capture_output=True, text=True,
            )
        else:
            aac_file = output_audio.with_suffix(".aac")
            if aac_file.exists():
                aac_file.rename(output_audio)

        if video_file.exists():
            video_file.unlink()

        if result.returncode != 0 and not output_audio.exists():
            logger.warning(f"ffmpeg failed: {result.stderr[:200]}")
            return False

        return output_audio.exists()


async def download_with_retry(
    video_url: str, output_audio: Path, semaphore: asyncio.Semaphore,
    delay: float = 0, max_retries: int = MAX_RETRIES,
) -> bool:
    """Download with automatic retry on failure."""
    for attempt in range(1, max_retries + 1):
        ok = await download_single_audio(video_url, output_audio, semaphore, delay=(delay if attempt == 1 else 0))
        if ok:
            return True
        if attempt < max_retries:
            wait = random.uniform(5, 15)
            logger.info(f"Retry {attempt}/{max_retries} in {wait:.0f}s for {video_url[-20:]}")
            await asyncio.sleep(wait)
    return False


def _extract_src_from_detail(detail_payload: dict) -> Optional[str]:
    if not isinstance(detail_payload, dict):
        return None
    aweme = detail_payload.get("aweme_detail")
    if not isinstance(aweme, dict):
        return None
    video = aweme.get("video")
    if not isinstance(video, dict):
        return None

    bit_rates = video.get("bit_rate")
    if isinstance(bit_rates, list):
        sortable = []
        for item in bit_rates:
            if not isinstance(item, dict):
                continue
            score = item.get("bit_rate", 0)
            play_addr = item.get("play_addr")
            urls = play_addr.get("url_list") if isinstance(play_addr, dict) else []
            src = _first_http(urls)
            if src:
                sortable.append((score, src))
        if sortable:
            sortable.sort(key=lambda x: x[0], reverse=True)
            return sortable[0][1]

    for key in ("play_addr_h264", "play_addr", "download_addr", "play_addr_265"):
        addr = video.get(key)
        if isinstance(addr, dict):
            src = _first_http(addr.get("url_list"))
            if src:
                return src
    return None


def _first_http(urls) -> Optional[str]:
    if not isinstance(urls, list):
        return None
    for url in urls:
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


# ---------------------------------------------------------------------------
# 4) Transcribe
# ---------------------------------------------------------------------------

def transcribe_audio_mlx(audio_path: Path, model: str = "small", language: str = "zh") -> str:
    """Transcribe using mlx-whisper (Apple Silicon) or fallback to openai-whisper."""
    try:
        import mlx_whisper
        model_map = {
            "tiny": "mlx-community/whisper-tiny-mlx",
            "base": "mlx-community/whisper-base-mlx-q4",
            "small": "mlx-community/whisper-small-mlx",
            "medium": "mlx-community/whisper-medium-mlx",
            "large": "mlx-community/whisper-large-v3-mlx",
            "large-v3": "mlx-community/whisper-large-v3-mlx",
            "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
        }
        model_id = model_map.get(model, model_map["small"])
        result = mlx_whisper.transcribe(str(audio_path), path_or_hf_repo=model_id, language=language)
        return result.get("text", "").strip() or "[转录为空]"
    except ImportError:
        logger.info("mlx-whisper not available, falling back to openai-whisper CLI")
        return _transcribe_audio_cli(audio_path, model, language)
    except Exception as e:
        logger.warning(f"mlx-whisper error: {e}, falling back to CLI")
        return _transcribe_audio_cli(audio_path, model, language)


def _transcribe_audio_cli(audio_path: Path, model: str = "small", language: str = "zh") -> str:
    result = subprocess.run(
        ["whisper", str(audio_path), "--model", model, "--language", language,
         "--output_format", "txt", "--output_dir", str(audio_path.parent)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.warning(f"Whisper CLI failed: {result.stderr[:200]}")
        return "[转录失败]"
    txt_file = audio_path.with_suffix(".txt")
    if txt_file.exists():
        text = txt_file.read_text(encoding="utf-8").strip()
        txt_file.unlink()
        return text
    return "[转录失败 - 无输出文件]"


# ---------------------------------------------------------------------------
# 5) Format transcript text
# ---------------------------------------------------------------------------

def format_transcript(raw_text: str) -> str:
    """Turn raw Whisper output into human-readable paragraphs.

    Whisper often produces a single block of text with minimal punctuation.
    This function:
    1. Normalises whitespace
    2. Splits on Chinese/English sentence endings
    3. Groups sentences into paragraphs (~4-6 sentences each)
    4. Preserves existing paragraph breaks if present
    """
    if not raw_text or raw_text.startswith("["):
        # Error placeholders like [下载失败] — return as-is
        return raw_text

    text = raw_text.strip()

    # If the text already has paragraph breaks, just clean it up
    if "\n\n" in text:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        return "\n\n".join(paragraphs)

    # Normalise whitespace (collapse multiple spaces/newlines into single space)
    text = re.sub(r"\s+", " ", text)

    # Split on sentence-ending punctuation (Chinese and English)
    # Keep the punctuation attached to the preceding sentence
    sentences = re.split(r"(?<=[。！？!?\.…])\s*", text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) <= 1:
        # No sentence boundaries found — try splitting on commas for long text
        if len(text) > 200:
            segments = re.split(r"(?<=[，,；;])\s*", text)
            segments = [s.strip() for s in segments if s.strip()]
            return _group_into_paragraphs(segments, group_size=6)
        return text

    return _group_into_paragraphs(sentences, group_size=5)


def _group_into_paragraphs(segments: list, group_size: int = 5) -> str:
    """Group a list of text segments into paragraphs."""
    paragraphs = []
    for i in range(0, len(segments), group_size):
        chunk = segments[i : i + group_size]
        paragraphs.append("".join(chunk))
    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# 6) Write individual transcript file
# ---------------------------------------------------------------------------

def _safe_filename(title: str, max_len: int = 80) -> str:
    safe = re.sub(r'[\\/:*?"<>|\n\r#]', '', title)
    return safe[:max_len].strip()


def write_transcript_file(
    output_dir: Path, index: int, video: dict, text: str,
    raw: bool = False,
) -> Path:
    num = str(index).zfill(2)
    safe_title = _safe_filename(video["title"])
    filename = f"{num}-{safe_title}.md"
    filepath = output_dir / filename

    body = text if raw else format_transcript(text)

    content = (
        f"# {video['title']}\n\n"
        f"> 日期: {video.get('date', 'unknown')} | 来源: {video['url']}\n\n"
        f"{body}\n"
    )
    filepath.write_text(content, encoding="utf-8")
    return filepath


# ---------------------------------------------------------------------------
# 7) Pipeline orchestrator
# ---------------------------------------------------------------------------

async def run_pipeline(
    url: str,
    output_dir: str,
    whisper_model: str = "small",
    language: str = "zh",
    max_videos: int = 0,
    keep_audio: bool = False,
    work_dir: str = DEFAULT_WORK_DIR,
    concurrency: int = CONCURRENT_DOWNLOADS,
    skip: int = 0,
    auto_confirm: bool = False,
    cookies_file: str = DEFAULT_COOKIES_FILE,
    raw: bool = False,
):
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    url_type = detect_url_type(url)
    state = StateTracker(out_dir)

    # --- Single video ---
    if url_type == "video":
        logger.info("Detected: single video URL")
        video_id = extract_video_id_from_url(url)
        if video_id and state.is_completed(video_id):
            logger.info(f"Video {video_id} already completed (in state.json). Skipping.")
            return

        videos = await scrape_single_video(url, cookies_file=cookies_file)
        if not videos:
            logger.error("Failed to get video info.")
            return

        video = videos[0]
        safe_name = re.sub(r'[^\w\-]', '_', video["video_id"])
        audio_file = work / f"{safe_name}.mp3"
        sem = asyncio.Semaphore(1)
        ok = await download_with_retry(video["url"], audio_file, sem)
        if not ok:
            logger.error("Download failed after retries.")
            state.mark_failed(video["video_id"])
            return

        logger.info(f"Transcribing: {video['title'][:50]}...")
        text = transcribe_audio_mlx(audio_file, model=whisper_model, language=language)
        if not keep_audio and audio_file.exists():
            audio_file.unlink()
        filepath = write_transcript_file(out_dir, 1, video, text, raw=raw)
        state.mark_completed(video["video_id"])
        logger.info(f"Done! Output: {filepath}")
        return

    # --- User profile ---
    if url_type == "user":
        logger.info("Detected: user profile URL")
        logger.info("=" * 60)
        logger.info("STEP 1: Scraping user profile...")
        logger.info("=" * 60)
        videos = await scrape_user_videos(url, max_videos=max_videos, cookies_file=cookies_file)

        if not videos:
            logger.error("No videos found.")
            return

        # Filter out already-completed videos
        already_done = sum(1 for v in videos if state.is_completed(v["video_id"]))
        if already_done > 0:
            logger.info(f"Skipping {already_done} already-completed videos (from state.json)")

        # Ask for confirmation
        pending = [v for v in videos if not state.is_completed(v["video_id"])]
        if not pending:
            logger.info("All videos already completed!")
            return

        if not auto_confirm:
            print()
            print("=" * 60)
            if already_done > 0:
                print(f"找到 {len(videos)} 个视频，其中 {already_done} 个已完成。")
                print(f"剩余 {len(pending)} 个需要下载并转录。")
            else:
                print(f"找到 {len(videos)} 个视频，确定要全部下载并转录吗？")
            print(f"预计耗时：{len(pending) * 45 // 60} - {len(pending) * 75 // 60} 分钟")
            print("输入 y 继续，n 取消，或输入数字限制下载数量：")
            print("=" * 60)
            answer = input("> ").strip().lower()
            if answer == "n" or answer == "":
                logger.info("已取消。")
                return
            if answer.isdigit():
                limit = int(answer)
                pending = pending[:limit]
                logger.info(f"限制为 {limit} 个视频")
            elif answer != "y":
                logger.info("已取消。")
                return

        if skip > 0:
            logger.info(f"Skipping first {skip} pending videos")
            pending = pending[skip:]

        if not pending:
            logger.info("No remaining videos to process.")
            return

        total = len(pending)
        logger.info(f"Starting batched pipeline for {total} videos...")

        semaphore = asyncio.Semaphore(concurrency)
        start_time = time.monotonic()
        success_count = 0
        fail_count = 0

        # Find the global index for numbering output files
        all_ids = [v["video_id"] for v in videos]

        for batch_start in range(0, total, BATCH_SIZE):
            batch = pending[batch_start:batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1

            if batch_start > 0:
                pause = random.uniform(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
                logger.info(f"--- Rate limit: pausing {pause:.0f}s ---")
                await asyncio.sleep(pause)

            logger.info(f"--- BATCH {batch_num}: videos {batch_start+1}-{batch_start+len(batch)} / {total} ---")

            audio_files = []
            download_tasks = []
            for i, video in enumerate(batch):
                global_idx = all_ids.index(video["video_id"]) + 1
                safe_name = re.sub(r'[^\w\-]', '_', video.get("video_id", f"v{global_idx}"))
                audio_file = work / f"{safe_name}.mp3"
                audio_files.append((global_idx, video, audio_file))
                delay = i * random.uniform(DOWNLOAD_DELAY_MIN, DOWNLOAD_DELAY_MAX)
                download_tasks.append(
                    download_with_retry(video["url"], audio_file, semaphore, delay=delay)
                )

            logger.info(f"Downloading {len(batch)} videos (max {concurrency} parallel)...")
            results = await asyncio.gather(*download_tasks, return_exceptions=True)

            for (global_idx, video, audio_file), result in zip(audio_files, results):
                if isinstance(result, Exception) or result is False:
                    logger.warning(f"[{global_idx}] Download failed: {video['title'][:40]}")
                    write_transcript_file(out_dir, global_idx, video, "[下载失败，已跳过]", raw=raw)
                    state.mark_failed(video["video_id"])
                    fail_count += 1
                    continue

                logger.info(f"[{global_idx}] Transcribing: {video['title'][:40]}...")
                text = transcribe_audio_mlx(audio_file, model=whisper_model, language=language)
                write_transcript_file(out_dir, global_idx, video, text, raw=raw)
                state.mark_completed(video["video_id"])
                success_count += 1

                if not keep_audio and audio_file.exists():
                    audio_file.unlink()

                elapsed = time.monotonic() - start_time
                done = success_count + fail_count
                remaining = total - done
                eta = (elapsed / done) * remaining if done > 0 else 0
                logger.info(f"[{global_idx}] Done. ETA: {int(eta//60)}m{int(eta%60)}s")

        elapsed_total = time.monotonic() - start_time
        logger.info(f"ALL DONE! {success_count}/{total} videos in {int(elapsed_total//60)}m{int(elapsed_total%60)}s")
        logger.info(f"Output directory: {out_dir}")
        if fail_count > 0:
            logger.warning(f"{fail_count} videos failed after retries")
        return

    logger.error(f"Cannot determine URL type: {url}")
    logger.error("Expected a Douyin video URL (/video/xxx) or user profile URL (/user/xxx)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Douyin videos and transcribe audio to text. "
        "Supports single video URLs and user profile URLs.",
    )
    parser.add_argument("url", help="Douyin video or user profile URL")
    parser.add_argument("--output-dir", default="./transcripts", help="Output directory")
    parser.add_argument("--whisper-model", default="small", help="Whisper model: tiny/base/small/medium/large")
    parser.add_argument("--language", default="zh", help="Audio language for Whisper")
    parser.add_argument("--max-videos", type=int, default=0, help="Max videos (0=all)")
    parser.add_argument("--keep-audio", action="store_true", help="Keep audio files")
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR, help="Temp work directory")
    parser.add_argument("--concurrency", type=int, default=CONCURRENT_DOWNLOADS, help="Parallel downloads")
    parser.add_argument("--skip", type=int, default=0, help="Skip first N pending videos")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    parser.add_argument("--cookies-file", default=DEFAULT_COOKIES_FILE, help="Cookie persistence file")
    parser.add_argument("--raw", action="store_true", help="Skip formatting — output raw Whisper text")

    args = parser.parse_args()

    asyncio.run(run_pipeline(
        url=args.url,
        output_dir=args.output_dir,
        whisper_model=args.whisper_model,
        language=args.language,
        max_videos=args.max_videos,
        keep_audio=args.keep_audio,
        work_dir=args.work_dir,
        concurrency=args.concurrency,
        skip=args.skip,
        auto_confirm=args.yes,
        cookies_file=args.cookies_file,
        raw=args.raw,
    ))


if __name__ == "__main__":
    main()
