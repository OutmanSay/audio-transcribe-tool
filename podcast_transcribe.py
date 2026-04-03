#!/usr/bin/env python3
"""播客转录脚本：播客链接 → ASR 转文稿 → AI 生成学习简报。

支持：小宇宙、Apple Podcasts、YouTube、Bilibili、RSS feed、直接音频链接。
转录引擎：通义听悟 > Groq Whisper > Cohere Transcribe（自动 fallback）。

Usage:
  python3 podcast_transcribe.py <url> [--brief] [--no-summary]
  python3 podcast_transcribe.py <url> --brief --output-dir ./output
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────────────
OUTPUT_DIR = Path(os.getenv("TRANSCRIPT_OUTPUT_DIR", "./output/podcast"))
NOTES_DIR = Path(os.getenv("TRANSCRIPT_NOTES_DIR", ""))  # 可选：简报输出目录

# ── 加载 .env 文件 ──────────────────────────────────────────
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:]
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

# 尝试加载 .env 文件
for env_path in [Path(".env"), Path(".env.local"), Path.home() / ".env.local"]:
    _load_env(env_path)

# ── 通义听悟配置（主力）──────────────────────────────────────
TINGWU_APP_KEY = os.getenv("TINGWU_APP_KEY", "")
AK_ID = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
AK_SECRET = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")

# ── Groq Whisper 配置（fallback）──────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# ── Cohere Transcribe 配置（垫底 fallback）──────────────────────
COHERE_API_KEY = os.getenv("COHERE_API_KEY", "")
COHERE_API_URL = "https://api.cohere.com/v2/audio/transcriptions"
COHERE_MODEL = "cohere-transcribe-03-2026"

# ── AI 简报配置 ────────────────────────────────────────────────
AI_API_KEY = os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
AI_API_BASE = os.getenv("AI_API_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")

# ── 代理配置 ────────────────────────────────────────────────
PROXY = os.getenv("PODCAST_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy") or ""


def _build_opener():
    """构建 urllib opener，自动检测代理。"""
    if not PROXY:
        return urllib.request.build_opener()
    import socket
    try:
        host = PROXY.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(PROXY.rstrip("/").split(":")[-1])
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((host, port))
        s.close()
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
        )
    except Exception:
        return urllib.request.build_opener()


# ── 音频 URL 提取 ─────────────────────────────────────────────

def extract_audio_url(page_url: str) -> tuple[str, str, str]:
    """从各种来源提取音频 URL、标题、节目名。

    支持：
    - 小宇宙 (xiaoyuzhoufm.com)
    - Apple Podcasts (podcasts.apple.com) — 失败时自动查找页面内小宇宙链接
    - YouTube / Bilibili (via yt-dlp)
    - RSS feed URL (直接解析 XML)
    - 直接音频链接 (.mp3/.m4a/.wav/.ogg/.flac)
    - 通用网页（尝试从页面中找音频链接，找不到则 yt-dlp 兜底）
    """
    import urllib.parse
    url_lower = page_url.lower().strip()

    # 1. 直接音频链接
    if re.search(r'\.(mp3|m4a|wav|ogg|flac|opus)(\?|$)', url_lower):
        title = Path(urllib.parse.urlparse(page_url).path).stem or "音频"
        return page_url, title, ""

    # 2. 小宇宙
    if "xiaoyuzhou" in url_lower or "xyzcdn" in url_lower:
        return _extract_xiaoyuzhou(page_url)

    # 3. YouTube / Bilibili（用 yt-dlp）
    if any(d in url_lower for d in ["youtube.com", "youtu.be", "bilibili.com", "b23.tv"]):
        return _extract_via_ytdlp(page_url)

    # 4. Apple Podcasts
    if "podcasts.apple.com" in url_lower:
        return _extract_apple_podcasts(page_url)

    # 5. RSS feed
    if url_lower.endswith(".xml") or url_lower.endswith("/feed") or "rss" in url_lower:
        return _extract_from_rss(page_url)

    # 6. 通用网页：先尝试从页面找音频链接，找不到再 yt-dlp 兜底
    try:
        return _extract_from_webpage(page_url)
    except RuntimeError:
        pass

    # 7. yt-dlp 万能兜底
    try:
        return _extract_via_ytdlp(page_url)
    except Exception:
        pass

    raise RuntimeError(
        f"无法从 {page_url} 提取音频。\n"
        f"请提供原始播客链接，比如：\n"
        f"  - 小宇宙: xiaoyuzhoufm.com/episode/...\n"
        f"  - Apple Podcasts: podcasts.apple.com/...\n"
        f"  - YouTube: youtube.com/watch?v=...\n"
        f"  - 或直接的 .mp3 链接"
    )


def _extract_xiaoyuzhou(page_url: str) -> tuple[str, str, str]:
    """小宇宙页面提取。"""
    opener = _build_opener()
    req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    audio_match = re.search(r'(https://media\.xyzcdn\.net/[^"\'<>\s]+\.m(?:p3|4a))', html)
    if not audio_match:
        raise RuntimeError("小宇宙页面未找到音频 URL")
    audio_url = audio_match.group(1)

    title_match = re.search(r'<title[^>]*>([^<]+)</title>', html)
    title = title_match.group(1).strip() if title_match else "未知标题"
    title = re.sub(r'\s*[-—]\s*小宇宙.*$', '', title)

    show_match = re.search(r'"podcastTitle"\s*:\s*"([^"]+)"', html)
    show = show_match.group(1) if show_match else ""

    return audio_url, title, show


def _extract_via_ytdlp(page_url: str) -> tuple[str, str, str]:
    """用 yt-dlp 提取音频 URL（YouTube/Bilibili/通用）。"""
    import subprocess as sp

    r = sp.run(
        ["yt-dlp", "--dump-json", "--no-download", page_url],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp 提取失败: {r.stderr[:200]}")

    info = json.loads(r.stdout)
    title = info.get("title", "未知标题")
    show = info.get("series", info.get("uploader", info.get("channel", "")))

    r2 = sp.run(
        ["yt-dlp", "-f", "bestaudio", "--get-url", page_url],
        capture_output=True, text=True, timeout=60,
    )
    if r2.returncode == 0 and r2.stdout.strip():
        audio_url = r2.stdout.strip().split("\n")[0]
        return audio_url, title, show

    import tempfile
    tmp = tempfile.mktemp(suffix=".mp3", dir="/tmp")
    r3 = sp.run(
        ["yt-dlp", "-f", "bestaudio", "-x", "--audio-format", "mp3",
         "-o", tmp, page_url],
        capture_output=True, timeout=600,
    )
    if r3.returncode == 0 and os.path.exists(tmp):
        return f"file://{tmp}", title, show

    raise RuntimeError(f"yt-dlp 无法提取音频: {r2.stderr[:200]}")


def _extract_apple_podcasts(page_url: str) -> tuple[str, str, str]:
    """Apple Podcasts 页面提取。
    如果直接提取音频失败，会尝试从页面中查找小宇宙等第三方播客平台链接作为 fallback。
    """
    opener = _build_opener()
    req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  [warn] Apple Podcasts 页面访问失败: {e}", flush=True)
        html = ""

    if html:
        # 尝试直接提取音频
        audio_match = re.search(r'"assetUrl"\s*:\s*"(https://[^"]+\.mp3[^"]*)"', html)
        if not audio_match:
            audio_match = re.search(r'(https://[^"\'<>\s]+\.mp3(?:\?[^"\'<>\s]*)?)', html)

        if audio_match:
            audio_url = audio_match.group(1)
            title_match = re.search(r'<title[^>]*>([^<]+)</title>', html)
            title = title_match.group(1).strip() if title_match else "未知标题"
            title = re.sub(r'\s*[-–—|]\s*Apple Podcasts.*$', '', title)
            show_match = re.search(r'"podcastName"\s*:\s*"([^"]+)"', html)
            if not show_match:
                show_match = re.search(r'"showName"\s*:\s*"([^"]+)"', html)
            show = show_match.group(1) if show_match else ""
            return audio_url, title, show

        # 音频提取失败 → 查找页面中的小宇宙链接作为 fallback
        xiaoyuzhou_match = re.search(r'(https?://(?:www\.)?xiaoyuzhoufm\.com/episode/[^"\'<>\s]+)', html)
        if xiaoyuzhou_match:
            fallback_url = xiaoyuzhou_match.group(1)
            print(f"  [fallback] Apple Podcasts 无直接音频，发现小宇宙链接: {fallback_url}", flush=True)
            return _extract_xiaoyuzhou(fallback_url)

        # 查找其他播客平台链接
        for pattern, name, extractor in [
            (r'(https?://(?:www\.)?xiaoyuzhoufm\.com/episode/[^"\'<>\s]+)', "小宇宙", _extract_xiaoyuzhou),
        ]:
            match = re.search(pattern, html)
            if match:
                fallback_url = match.group(1)
                print(f"  [fallback] 发现{name}链接: {fallback_url}", flush=True)
                return extractor(fallback_url)

    raise RuntimeError(
        "Apple Podcasts 页面未找到音频 URL。\n"
        "Apple Podcasts 可能需要登录或地区限制。\n"
        "建议：\n"
        "  1. 搜索该播客在小宇宙上的链接\n"
        "  2. 使用 RSS feed URL\n"
        "  3. 使用直接音频链接"
    )


def _extract_from_rss(feed_url: str) -> tuple[str, str, str]:
    """从 RSS feed 提取最新一期的音频。"""
    opener = _build_opener()
    req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=30) as resp:
        xml = resp.read().decode("utf-8", errors="ignore")

    enc_match = re.search(r'<enclosure[^>]+url="([^"]+)"', xml)
    if not enc_match:
        raise RuntimeError("RSS feed 中未找到 enclosure")
    audio_url = enc_match.group(1)

    item_match = re.search(r'<item[^>]*>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>.*?<enclosure', xml, re.DOTALL)
    title = item_match.group(1).strip() if item_match else "未知标题"

    channel_title = re.search(r'<channel[^>]*>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', xml, re.DOTALL)
    show = channel_title.group(1).strip() if channel_title else ""

    return audio_url, title, show


def _extract_from_webpage(page_url: str) -> tuple[str, str, str]:
    """通用网页：尝试从 HTML 中找音频链接。"""
    opener = _build_opener()
    req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    patterns = [
        r'(https?://[^"\'<>\s]+\.mp3(?:\?[^"\'<>\s]*)?)',
        r'(https?://[^"\'<>\s]+\.m4a(?:\?[^"\'<>\s]*)?)',
        r'(https?://[^"\'<>\s]+\.ogg(?:\?[^"\'<>\s]*)?)',
        r'(https?://[^"\'<>\s]+\.wav(?:\?[^"\'<>\s]*)?)',
        r'"audioUrl"\s*:\s*"(https?://[^"]+)"',
        r'"audio_url"\s*:\s*"(https?://[^"]+)"',
        r'"enclosure_url"\s*:\s*"(https?://[^"]+)"',
        r'<audio[^>]+src="(https?://[^"]+)"',
        r'"url"\s*:\s*"(https?://[^"]+\.mp3[^"]*)"',
    ]

    audio_url = None
    for p in patterns:
        match = re.search(p, html)
        if match:
            audio_url = match.group(1)
            break

    if not audio_url:
        raise RuntimeError("网页中未找到音频链接")

    title_match = re.search(r'<title[^>]*>([^<]+)</title>', html)
    title = title_match.group(1).strip() if title_match else "未知标题"

    return audio_url, title, ""


# ── 通义听悟 API ──────────────────────────────────────────────

def _tingwu_request(method: str, uri: str, body: dict | None = None, query: dict | None = None) -> dict:
    """调用通义听悟 OpenAPI（aliyunsdkcore 签名）。"""
    try:
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkcore.request import CommonRequest
        from aliyunsdkcore.auth.credentials import AccessKeyCredential
    except ImportError:
        import subprocess
        print("[install] aliyun-python-sdk-core...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "--user",
                        "--break-system-packages", "aliyun-python-sdk-core"], check=True)
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkcore.request import CommonRequest
        from aliyunsdkcore.auth.credentials import AccessKeyCredential

    credentials = AccessKeyCredential(AK_ID, AK_SECRET)
    client = AcsClient(region_id="cn-beijing", credential=credentials)

    request = CommonRequest()
    request.set_accept_format("json")
    request.set_domain("tingwu.cn-beijing.aliyuncs.com")
    request.set_version("2023-09-30")
    request.set_protocol_type("https")
    request.set_method(method)
    request.set_uri_pattern(uri)
    request.add_header("Content-Type", "application/json")

    if query:
        for k, v in query.items():
            request.add_query_param(k, v)
    if body:
        request.set_content(json.dumps(body).encode("utf-8"))

    response = client.do_action_with_exception(request)
    return json.loads(response)


def create_transcription_task(audio_url: str, enable_summary: bool = True) -> str:
    """提交通义听悟离线转录任务，返回 TaskId。"""
    import datetime
    body = {
        "AppKey": TINGWU_APP_KEY,
        "Input": {
            "SourceLanguage": "cn",
            "TaskKey": "podcast" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
            "FileUrl": audio_url,
        },
        "Parameters": {
            "Transcription": {
                "DiarizationEnabled": True,
                "Diarization": {"SpeakerCount": 0},
            },
        },
    }

    if enable_summary:
        body["Parameters"]["SummarizationEnabled"] = True
        body["Parameters"]["Summarization"] = {
            "Types": ["Paragraph", "Conversational"],
        }
        body["Parameters"]["AutoChaptersEnabled"] = True

    result = _tingwu_request("PUT", "/openapi/tingwu/v2/tasks", body=body, query={"type": "offline"})
    if result.get("Code") != "0":
        raise RuntimeError(f"CreateTask failed: {result}")

    task_id = result["Data"]["TaskId"]
    print(f"  TaskId: {task_id}", flush=True)
    return task_id


def poll_task(task_id: str, max_wait: int = 600, interval: int = 10) -> dict:
    """轮询任务状态直到完成。"""
    uri = f"/openapi/tingwu/v2/tasks/{task_id}"
    for i in range(max_wait // interval):
        result = _tingwu_request("GET", uri)
        status = result.get("Data", {}).get("TaskStatus", "UNKNOWN")
        elapsed = (i + 1) * interval
        print(f"  [{elapsed}s] status={status}", flush=True)

        if status == "COMPLETED":
            return result["Data"].get("Result", {})
        elif status == "FAILED":
            error = result.get("Data", {}).get("ErrorMessage", "unknown")
            raise RuntimeError(f"Task failed: {error}")

        time.sleep(interval)

    raise RuntimeError(f"Task timeout after {max_wait}s")


def fetch_transcript_from_url(url: str) -> str:
    """从通义听悟返回的结果 URL 下载转录内容并格式化。"""
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    paragraphs = data.get("Transcription", {}).get("Paragraphs", [])
    if not paragraphs:
        return json.dumps(data, ensure_ascii=False, indent=2)

    lines = []
    current_speaker = None
    for p in paragraphs:
        speaker = p.get("SpeakerId", "")
        words = p.get("Words", [])
        text = "".join(w.get("Text", "") for w in words)

        if not text.strip():
            continue

        start_ms = p.get("StartTime", 0)
        start_str = f"{start_ms // 3600000:02d}:{(start_ms % 3600000) // 60000:02d}:{(start_ms % 60000) // 1000:02d}"

        if speaker != current_speaker:
            current_speaker = speaker
            lines.append(f"\n**说话人{speaker}** [{start_str}]")

        lines.append(text)

    return "\n".join(lines)


# ── Groq Whisper fallback ──────────────────────────────────

def groq_transcribe(audio_url: str) -> str:
    """用 Groq Whisper 转录（通义听悟不可用时的 fallback）。"""
    import math
    import subprocess
    import tempfile

    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY 未配置")

    with tempfile.TemporaryDirectory(prefix="groq-") as tmp:
        tmp_dir = Path(tmp)

        audio_path = tmp_dir / "audio.mp3"
        if audio_url.startswith("file://"):
            local_path = audio_url[7:]
            print(f"  [groq] 使用本地文件: {local_path}", flush=True)
            subprocess.run(["cp", local_path, str(audio_path)], check=True, timeout=30)
        else:
            print("  [groq] 下载音频...", flush=True)
            subprocess.run(["curl", "-sL", "-o", str(audio_path), audio_url], check=True, timeout=300)

        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip())
        chunk_seconds = 600
        n_chunks = math.ceil(duration / chunk_seconds)

        chunks = []
        if n_chunks <= 1:
            chunks = [audio_path]
        else:
            print(f"  [groq] 分段: {duration:.0f}s → {n_chunks} chunks", flush=True)
            for i in range(n_chunks):
                start = i * chunk_seconds
                chunk_path = tmp_dir / f"chunk_{i:03d}.mp3"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(audio_path),
                     "-ss", str(start), "-t", str(chunk_seconds),
                     "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
                     str(chunk_path)],
                    capture_output=True, timeout=120,
                )
                if chunk_path.exists() and chunk_path.stat().st_size > 0:
                    chunks.append(chunk_path)

        texts = []
        proxy_args = ["--proxy", PROXY] if PROXY else []
        for i, chunk in enumerate(chunks):
            print(f"  [groq] chunk {i+1}/{len(chunks)}", flush=True)
            if i > 0:
                time.sleep(3)
            try:
                cmd = [
                    "curl", "-sS", "--max-time", "120",
                    *proxy_args,
                    "-X", "POST", GROQ_API_URL,
                    "-H", f"Authorization: Bearer {GROQ_API_KEY}",
                    "-F", f"file=@{chunk}",
                    "-F", "model=whisper-large-v3-turbo",
                    "-F", "language=zh",
                    "-F", "response_format=verbose_json",
                ]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    texts.append(f"[转录失败: {r.stderr[:200]}]")
                    continue
                data = json.loads(r.stdout)
                if "error" in data:
                    texts.append(f"[转录失败: {data['error']}]")
                    continue
                texts.append(data.get("text", ""))
            except Exception as e:
                texts.append(f"[转录失败: {e}]")

    return "\n\n".join(texts)


# ── Cohere Transcribe fallback（垫底）──────────────────────

def cohere_transcribe(audio_url: str) -> str:
    """用 Cohere Transcribe 转录（最后 fallback）。"""
    import math
    import subprocess
    import tempfile

    if not COHERE_API_KEY:
        raise RuntimeError("COHERE_API_KEY 未配置")

    with tempfile.TemporaryDirectory(prefix="cohere-") as tmp:
        tmp_dir = Path(tmp)

        audio_path = tmp_dir / "audio.mp3"
        if audio_url.startswith("file://"):
            local_path = audio_url[7:]
            print(f"  [cohere] 转换本地文件: {local_path}", flush=True)
            subprocess.run(
                ["ffmpeg", "-y", "-i", local_path,
                 "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
                 str(audio_path)],
                capture_output=True, timeout=300,
            )
        else:
            print("  [cohere] 下载并转换音频...", flush=True)
            subprocess.run(
                ["ffmpeg", "-y", "-i", audio_url,
                 "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
                 str(audio_path)],
                capture_output=True, timeout=300,
            )
            if not audio_path.exists():
                raw_path = tmp_dir / "raw_audio"
                subprocess.run(["curl", "-sL", "-o", str(raw_path), audio_url], check=True, timeout=300)
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(raw_path),
                     "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
                     str(audio_path)],
                    capture_output=True, timeout=120,
                )

        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip())
        chunk_seconds = 600
        n_chunks = math.ceil(duration / chunk_seconds)

        chunks = []
        if n_chunks <= 1:
            chunks = [audio_path]
        else:
            print(f"  [cohere] 分段: {duration:.0f}s → {n_chunks} chunks", flush=True)
            for i in range(n_chunks):
                start = i * chunk_seconds
                chunk_path = tmp_dir / f"chunk_{i:03d}.mp3"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(audio_path),
                     "-ss", str(start), "-t", str(chunk_seconds),
                     "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
                     str(chunk_path)],
                    capture_output=True, timeout=120,
                )
                if chunk_path.exists() and chunk_path.stat().st_size > 0:
                    chunks.append(chunk_path)

        texts = []
        for i, chunk in enumerate(chunks):
            print(f"  [cohere] chunk {i+1}/{len(chunks)}", flush=True)
            if i > 0:
                time.sleep(2)
            try:
                cmd = [
                    "curl", "-sS", "--max-time", "120",
                    "-X", "POST", COHERE_API_URL,
                    "-H", f"Authorization: Bearer {COHERE_API_KEY}",
                    "-H", "accept: application/json",
                    "-F", f"model={COHERE_MODEL}",
                    "-F", "language=zh",
                    "-F", f"file=@{chunk}",
                ]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    texts.append(f"[转录失败: {r.stderr[:200]}]")
                    continue
                data = json.loads(r.stdout)
                if "message" in data and "error" in str(data.get("message", "")).lower():
                    texts.append(f"[转录失败: {data['message']}]")
                    continue
                texts.append(data.get("text", ""))
            except Exception as e:
                texts.append(f"[转录失败: {e}]")

    return "\n\n".join(texts)


# ── AI 简报 ──────────────────────────────────────────────

def generate_brief(title: str, show: str, transcript: str) -> str:
    """用 AI 从文稿生成学习简报。"""
    prompt = f"""请基于以下播客文稿，生成一份专业级的学习简报。

## 播客信息
- 标题：{title}
- 节目：{show}

## 输出格式（严格按此结构）

# {title} — 学习简报

## 执行摘要
（3-5 句话概括本期核心结论，让没听过的人 30 秒抓到重点）

## 核心论点

| # | 论点 | 支撑依据 | 重要程度 |
|---|------|----------|---------|
| 1 | （一句话论点） | （关键数据或案例） | 高/中 |
| 2 | ... | ... | ... |

（列 3-5 条，每条必须有数据或案例支撑）

## 详细笔记

按话题/章节整理，每个章节包含：
- **二级标题**（章节主题）
- 关键论点和推理链条
- **数据和案例用表格或加粗呈现**
- 如果有不同嘉宾的观点分歧，用「嘉宾A认为... vs 嘉宾B认为...」格式标注
- 涉及对比的内容，**必须用表格**

## 关键数据速查

| 数据 | 数值 | 语境 |
|------|------|------|
| （指标名） | （具体数字） | （一句话解释为什么重要） |

（从全文提取 5-10 个最值得记住的数据点）

## 金句摘录
- "原话引用"——说话人名字
- （选 3-5 句最有冲击力的原话）

## 行动建议

（基于内容，给出 2-3 条具体的、可执行的行动建议）

1. **（行动名）**：具体怎么做
2. ...

---

## 格式原则
1. 提炼观点和推理链条，不要复述原文
2. **数据必须保留**：百分比、金额、时间、排名等具体数字不能丢
3. **对比必须用表格**
4. **案例要具体**：公司名、人名、事件要保留
5. 金句必须是原话引用
6. 只输出最终正文

## 播客文稿

{transcript[:30000]}
"""

    url = AI_API_BASE.rstrip("/") + "/chat/completions"
    payload = {
        "model": AI_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "你是一个播客学习笔记整理专家，擅长从对话文稿中提炼结构化的学习简报。"},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"].strip()
    # 去除部分模型的思考标签
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S)
    return text


def slugify(text: str) -> str:
    text = re.sub(r'[^\w\u4e00-\u9fff\s-]', '', text)
    return text.strip()[:60]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="播客转录工具：播客链接 → ASR 转文稿 → AI 学习简报",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
支持的链接类型：
  - 小宇宙 (xiaoyuzhoufm.com)
  - Apple Podcasts (podcasts.apple.com)
  - YouTube / Bilibili (需要 yt-dlp)
  - RSS feed URL
  - 直接音频链接 (.mp3/.m4a 等)

转录引擎优先级：通义听悟 > Groq Whisper > Cohere Transcribe
        """,
    )
    parser.add_argument("url", help="播客链接或音频 URL")
    parser.add_argument("--brief", action="store_true", help="转录后自动生成 AI 学习简报")
    parser.add_argument("--no-summary", action="store_true", help="不启用通义听悟的摘要功能（省费用）")
    parser.add_argument("--output-dir", type=Path, help="输出目录（默认 ./output/podcast）")
    args = parser.parse_args()

    # 检查是否有可用的转录引擎
    has_tingwu = bool(TINGWU_APP_KEY and AK_ID and AK_SECRET)
    has_groq = bool(GROQ_API_KEY)
    has_cohere = bool(COHERE_API_KEY)

    if not has_tingwu and not has_groq and not has_cohere:
        print("错误：未配置任何转录引擎。请在 .env 中配置至少一个：", file=sys.stderr)
        print("  - 通义听悟: TINGWU_APP_KEY + ALIBABA_CLOUD_ACCESS_KEY_ID + ALIBABA_CLOUD_ACCESS_KEY_SECRET", file=sys.stderr)
        print("  - Groq Whisper: GROQ_API_KEY", file=sys.stderr)
        print("  - Cohere: COHERE_API_KEY", file=sys.stderr)
        return 1

    output_dir = args.output_dir or OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: 提取音频 URL
    print("[1/4] 提取音频 URL...", flush=True)
    audio_url, title, show = extract_audio_url(args.url)
    print(f"  标题: {title}", flush=True)
    print(f"  节目: {show}", flush=True)
    print(f"  音频: {audio_url}", flush=True)

    slug = slugify(title) or "podcast"

    # Step 2-3: 转录（通义听悟优先 → Groq fallback → Cohere 垫底）
    transcript = ""
    engine = "unknown"

    if has_tingwu:
        try:
            print("[2/4] 提交通义听悟转录任务...", flush=True)
            task_id = create_transcription_task(audio_url, enable_summary=not args.no_summary)

            print("[3/4] 等待转录完成...", flush=True)
            result_urls = poll_task(task_id)
            print(f"  结果: {list(result_urls.keys())}", flush=True)

            transcript_url = result_urls.get("Transcription", "")
            if transcript_url:
                print("  下载转录文稿...", flush=True)
                transcript = fetch_transcript_from_url(transcript_url)
                engine = "通义听悟"

                for key in ["AutoChapters", "Summarization"]:
                    url = result_urls.get(key, "")
                    if url:
                        try:
                            with urllib.request.urlopen(url, timeout=30) as resp:
                                summary_data = json.loads(resp.read().decode("utf-8"))
                            summary_file = output_dir / f"{slug}-{key}.json"
                            summary_file.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
                            print(f"  {key}: {summary_file}", flush=True)
                        except Exception as e:
                            print(f"  [warn] {key} download failed: {e}", flush=True)
        except Exception as e:
            print(f"  [warn] 通义听悟失败: {e}", flush=True)
            print("  → 切换到 Groq Whisper fallback", flush=True)

    if not transcript and has_groq:
        print("[2/4] Groq Whisper 转录（fallback）...", flush=True)
        try:
            transcript = groq_transcribe(audio_url)
            engine = "Groq Whisper"
            print(f"[3/4] Groq 转录完成: {len(transcript)} chars", flush=True)
        except Exception as e:
            print(f"  [warn] Groq 也失败了: {e}", flush=True)
            print("  → 切换到 Cohere Transcribe fallback", flush=True)

    if not transcript and has_cohere:
        print("[2/4] Cohere Transcribe 转录（最后 fallback）...", flush=True)
        try:
            transcript = cohere_transcribe(audio_url)
            engine = "Cohere Transcribe"
            print(f"[3/4] Cohere 转录完成: {len(transcript)} chars", flush=True)
        except Exception as e:
            print(f"  [error] 所有引擎都失败了: {e}", file=sys.stderr)
            return 1

    if not transcript:
        print("[error] 所有转录引擎都失败了", file=sys.stderr)
        return 1

    # 保存文稿
    transcript_file = output_dir / f"{slug}-transcript.md"
    transcript_content = (
        f"# {title}\n\n"
        f"> 节目：{show}\n"
        f"> 来源：{args.url}\n"
        f"> 转录时间：{time.strftime('%Y-%m-%d %H:%M')}\n"
        f"> 转录引擎：{engine}\n\n"
        f"---\n\n{transcript}\n"
    )
    transcript_file.write_text(transcript_content, encoding="utf-8")
    print(f"  文稿: {transcript_file} ({engine})", flush=True)

    # Step 4: 生成学习简报（可选）
    if args.brief:
        if not AI_API_KEY:
            print("[warn] 缺少 AI API Key，跳过简报生成", flush=True)
        else:
            print("[4/4] 生成学习简报...", flush=True)
            brief = generate_brief(title, show, transcript)

            brief_file = output_dir / f"{slug}-brief.md"
            brief_file.write_text(brief, encoding="utf-8")
            print(f"  简报: {brief_file}", flush=True)

            # 如果配置了 notes 目录，也写一份
            notes_dir = NOTES_DIR
            if notes_dir and str(notes_dir):
                notes_dir.mkdir(parents=True, exist_ok=True)
                today_str = time.strftime("%Y-%m-%d")
                note_file = notes_dir / f"{today_str} 播客-{slug}.md"
                note_content = (
                    f"---\n"
                    f"source: podcast\n"
                    f"source_url: \"{args.url}\"\n"
                    f"created: {today_str}\n"
                    f"tags: [播客]\n"
                    f"---\n\n"
                    f"# 播客：{title}\n\n"
                    f"> 节目：{show}\n"
                    f"> 来源：{args.url}\n"
                    f"> 转录引擎：{engine}\n\n"
                    f"---\n\n"
                    f"{brief}\n"
                )
                note_file.write_text(note_content, encoding="utf-8")
                print(f"  笔记: {note_file}", flush=True)
    else:
        print("[4/4] 跳过简报生成（加 --brief 启用）", flush=True)

    print(f"\n✅ 播客转录完成：{title}", flush=True)
    print(f"转录引擎：{engine}", flush=True)
    print(f"输出目录：{output_dir}/", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
