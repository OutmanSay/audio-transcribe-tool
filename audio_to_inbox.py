#!/usr/bin/env python3
"""本地录音转录工具：音频文件 → ASR 转录 → AI 整理成结构化 Markdown。

支持三引擎自动 fallback：通义听悟 > Groq Whisper > Cohere Transcribe。
长音频自动分段（>10min），支持断点续传。

Usage:
  python3 audio_to_inbox.py /path/to/recording.m4a
  python3 audio_to_inbox.py /path/to/recording.mp3 --date 2026-03-20
  python3 audio_to_inbox.py /path/to/recording.m4a --transcribe-only
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────────────
OUTPUT_DIR = Path(os.getenv("TRANSCRIPT_OUTPUT_DIR", "./output"))
WORK_DIR = Path(os.getenv("TRANSCRIPT_WORK_DIR", "./output/.work"))

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

for env_path in [Path(".env"), Path(".env.local"), Path.home() / ".env.local"]:
    _load_env(env_path)

# ── 通义听悟配置（主力）──────────────────────────────────────
TINGWU_APP_KEY = os.getenv("TINGWU_APP_KEY", "")
AK_ID = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
AK_SECRET = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
OSS_BUCKET = os.getenv("TINGWU_OSS_BUCKET", "")
OSS_ENDPOINT = os.getenv("TINGWU_OSS_ENDPOINT", "https://oss-cn-beijing.aliyuncs.com")

# ── Groq Whisper 配置（fallback）──────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
PROXY = os.getenv("PODCAST_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy") or ""

# ── Cohere Transcribe 配置（垫底 fallback）──────────────────────
COHERE_API_KEY = os.getenv("COHERE_API_KEY", "")
COHERE_API_URL = "https://api.cohere.com/v2/audio/transcriptions"
COHERE_MODEL = "cohere-transcribe-03-2026"

# ── AI 整理配置 ────────────────────────────────────────────────
AI_API_KEY = os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
AI_API_BASE = os.getenv("AI_API_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")

MAX_CHUNK_SECONDS = 600
MAX_RETRIES = 3
RETRY_BACKOFF = [5, 15, 30]


def get_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(r.stdout.strip())


def split_audio(path: Path, work_dir: Path) -> list[Path]:
    duration = get_duration(path)
    n = math.ceil(duration / MAX_CHUNK_SECONDS)
    if n <= 1:
        compressed = work_dir / "chunk_000.mp3"
        if compressed.exists() and compressed.stat().st_size > 0:
            print(f"  chunk_000.mp3 已存在，跳过切分", flush=True)
            return [compressed]
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path),
             "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
             str(compressed)],
            capture_output=True, timeout=120,
        )
        return [compressed] if compressed.exists() else [path]

    print(f"  分段: {duration:.0f}s → {n} chunks", flush=True)
    chunks = []
    for i in range(n):
        chunk = work_dir / f"chunk_{i:03d}.mp3"
        if chunk.exists() and chunk.stat().st_size > 0:
            print(f"  chunk_{i:03d}.mp3 已存在，跳过", flush=True)
            chunks.append(chunk)
            continue
        start = i * MAX_CHUNK_SECONDS
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path),
             "-ss", str(start), "-t", str(MAX_CHUNK_SECONDS),
             "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
             str(chunk)],
            capture_output=True, timeout=120,
        )
        if chunk.exists() and chunk.stat().st_size > 0:
            chunks.append(chunk)
    return chunks


# ── 通义听悟（主力引擎）──────────────────────────────────────

def _tingwu_available() -> bool:
    return bool(TINGWU_APP_KEY and AK_ID and AK_SECRET and OSS_BUCKET)


def transcribe_tingwu(audio_path: Path) -> str:
    """通义听悟转录：上传 OSS → 提交任务 → 轮询 → 下载文稿 → 清理 OSS。"""
    import oss2
    import urllib.request
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest
    from aliyunsdkcore.auth.credentials import AccessKeyCredential

    oss_key = f"audio-tmp/{audio_path.name}"

    print("  [tingwu] 上传 OSS...", flush=True)
    auth = oss2.Auth(AK_ID, AK_SECRET)
    bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET)
    bucket.put_object_from_file(oss_key, str(audio_path))
    file_url = bucket.sign_url("GET", oss_key, 3600)
    print("  [tingwu] 上传完成", flush=True)

    try:
        credentials = AccessKeyCredential(AK_ID, AK_SECRET)
        client = AcsClient(region_id="cn-beijing", credential=credentials)

        body = {
            "AppKey": TINGWU_APP_KEY,
            "Input": {
                "SourceLanguage": "cn",
                "TaskKey": "audio" + datetime.now().strftime("%Y%m%d%H%M%S"),
                "FileUrl": file_url,
            },
            "Parameters": {
                "Transcription": {
                    "DiarizationEnabled": True,
                    "Diarization": {"SpeakerCount": 0},
                },
            },
        }

        req = CommonRequest()
        req.set_accept_format("json")
        req.set_domain("tingwu.cn-beijing.aliyuncs.com")
        req.set_version("2023-09-30")
        req.set_protocol_type("https")
        req.set_method("PUT")
        req.set_uri_pattern("/openapi/tingwu/v2/tasks")
        req.add_header("Content-Type", "application/json")
        req.add_query_param("type", "offline")
        req.set_content(json.dumps(body).encode("utf-8"))

        resp = client.do_action_with_exception(req)
        result = json.loads(resp)
        if result.get("Code") != "0":
            raise RuntimeError(f"CreateTask failed: {result}")

        task_id = result["Data"]["TaskId"]
        print(f"  [tingwu] TaskId: {task_id}", flush=True)

        uri = f"/openapi/tingwu/v2/tasks/{task_id}"
        for i in range(60):
            time.sleep(10)
            req2 = CommonRequest()
            req2.set_accept_format("json")
            req2.set_domain("tingwu.cn-beijing.aliyuncs.com")
            req2.set_version("2023-09-30")
            req2.set_protocol_type("https")
            req2.set_method("GET")
            req2.set_uri_pattern(uri)
            req2.add_header("Content-Type", "application/json")

            resp2 = client.do_action_with_exception(req2)
            r = json.loads(resp2)
            status = r.get("Data", {}).get("TaskStatus", "UNKNOWN")
            elapsed = (i + 1) * 10
            print(f"  [tingwu] [{elapsed}s] status={status}", flush=True)

            if status == "COMPLETED":
                result_data = r["Data"].get("Result", {})
                trans_url = result_data.get("Transcription", "")
                if not trans_url:
                    raise RuntimeError("No transcription URL in result")

                with urllib.request.urlopen(trans_url, timeout=30) as tresp:
                    trans_data = json.loads(tresp.read().decode("utf-8"))

                lines = []
                for para in trans_data.get("Transcription", {}).get("Paragraphs", []):
                    speaker = para.get("SpeakerId", "?")
                    words = "".join(w.get("Text", "") for w in para.get("Words", []))
                    lines.append(f"[说话人{speaker}] {words}")

                return "\n\n".join(lines)

            elif status == "FAILED":
                error = r.get("Data", {}).get("ErrorMessage", "unknown")
                raise RuntimeError(f"Task failed: {error}")

        raise RuntimeError("Task timeout after 600s")

    finally:
        try:
            bucket.delete_object(oss_key)
            print("  [tingwu] OSS 临时文件已删除", flush=True)
        except Exception:
            pass


# ── Groq Whisper（fallback）──────────────────────────────────

def _load_state(state_file: Path) -> dict:
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return {}


def _save_state(state_file: Path, state: dict):
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _transcribe_one_chunk(chunk: Path) -> tuple[bool, str]:
    proxy_args = ["--proxy", PROXY] if PROXY else []
    cmd = [
        "curl", "-sS", "--max-time", "180",
        *proxy_args,
        "-X", "POST", GROQ_API_URL,
        "-H", f"Authorization: Bearer {GROQ_API_KEY}",
        "-F", f"file=@{chunk}",
        "-F", "model=whisper-large-v3-turbo",
        "-F", "language=zh",
        "-F", "response_format=verbose_json",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if r.returncode != 0:
        return False, r.stderr[:300]
    try:
        data = json.loads(r.stdout)
        if "error" in data:
            return False, str(data["error"])
        return True, data.get("text", "")
    except Exception as e:
        return False, str(e)


def transcribe_groq(audio_path: Path) -> str:
    work_dir = WORK_DIR / audio_path.stem
    work_dir.mkdir(parents=True, exist_ok=True)
    state_file = work_dir / "state.json"
    state = _load_state(state_file)

    chunks = split_audio(audio_path, work_dir)
    total = len(chunks)
    texts: dict[str, str] = state.get("texts", {})

    for i, chunk in enumerate(chunks):
        chunk_key = chunk.name
        if chunk_key in texts and not texts[chunk_key].startswith("[转录失败"):
            print(f"  [groq] chunk {i+1}/{total} 已有结果，跳过", flush=True)
            continue

        print(f"  [groq] chunk {i+1}/{total} ({chunk.stat().st_size/1024:.0f}KB)", flush=True)
        if i > 0:
            time.sleep(3)

        success = False
        for attempt in range(MAX_RETRIES):
            ok, result = _transcribe_one_chunk(chunk)
            if ok:
                texts[chunk_key] = result
                success = True
                state["texts"] = texts
                _save_state(state_file, state)
                break
            else:
                wait = RETRY_BACKOFF[attempt] if attempt < len(RETRY_BACKOFF) else RETRY_BACKOFF[-1]
                print(f"  [groq] chunk {i+1}/{total} 失败 (attempt {attempt+1}/{MAX_RETRIES}): {result[:100]}", flush=True)
                if attempt < MAX_RETRIES - 1:
                    print(f"  [groq] {wait}s 后重试...", flush=True)
                    time.sleep(wait)

        if not success:
            texts[chunk_key] = f"[转录失败: 重试{MAX_RETRIES}次后仍失败: {result[:200]}]"
            state["texts"] = texts
            _save_state(state_file, state)

    state["texts"] = texts
    state["completed"] = True
    _save_state(state_file, state)

    ordered = [texts.get(c.name, "") for c in chunks]
    return "\n\n".join(ordered)


def cohere_transcribe_local(audio_path: Path) -> str:
    """Cohere Transcribe 转录本地文件（垫底 fallback）。"""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        mp3_path = tmp.name
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(audio_path),
         "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
         mp3_path],
        capture_output=True, timeout=120,
    )

    try:
        duration = get_duration(Path(mp3_path))
        chunk_seconds = 600
        n_chunks = math.ceil(duration / chunk_seconds)

        if n_chunks <= 1:
            chunks = [mp3_path]
        else:
            chunks = []
            for i in range(n_chunks):
                chunk_path = f"{mp3_path}.chunk{i}.mp3"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", mp3_path,
                     "-ss", str(i * chunk_seconds), "-t", str(chunk_seconds),
                     "-acodec", "libmp3lame", "-ab", "32k", "-ar", "16000", "-ac", "1",
                     chunk_path],
                    capture_output=True, timeout=120,
                )
                if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
                    chunks.append(chunk_path)

        texts = []
        for i, chunk in enumerate(chunks):
            print(f"  [cohere] chunk {i+1}/{len(chunks)}", flush=True)
            if i > 0:
                time.sleep(2)
            r = subprocess.run(
                ["curl", "-sS", "--max-time", "120",
                 "-X", "POST", COHERE_API_URL,
                 "-H", f"Authorization: Bearer {COHERE_API_KEY}",
                 "-H", "accept: application/json",
                 "-F", f"model={COHERE_MODEL}",
                 "-F", "language=zh",
                 "-F", f"file=@{chunk}"],
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode == 0:
                data = json.loads(r.stdout)
                texts.append(data.get("text", f"[转录失败: {data}]"))
            else:
                texts.append(f"[转录失败: {r.stderr[:200]}]")
        return "\n\n".join(texts)
    finally:
        os.unlink(mp3_path)
        for f in Path(mp3_path).parent.glob(f"{Path(mp3_path).stem}.chunk*"):
            f.unlink(missing_ok=True)


def ai_process(transcript: str, date_str: str) -> tuple[str, str]:
    """用 AI 整理转录文本，返回 (filename, markdown_content)。"""
    import urllib.request

    prompt = f"""你现在的任务，是把下面的 ASR 转写文本，整理成一份结构化的 Markdown 文件。

目标：
- 保留足够有用的信息
- 输出必须稳定、精炼
- 最终结果适合直接作为笔记保存

请严格按下面规则执行：

【一、先判断类型】
- meeting：会议 / 对齐 / 工作沟通 / 讨论
- chat：和朋友 / 同事 / 他人的聊天
- voice_note：我自己的语音记录 / 随想 / 复盘

【二、信息处理原则】
1. 删除冗余口语、重复句、语气词、无意义寒暄
2. 不要脑补；不确定内容放进"疑义 / 待核实"
3. 人名、数字、时间、地点、任务归属单独检查
4. 不保留长原文，但保留少量关键原话作为证据
5. 输出尽量精炼，但不能丢掉真正有价值的信息

【三、命名规则】
生成文件名，格式：{date_str} 类型-标题.md
- meeting → 会议-标题
- chat → 聊天-标题
- voice_note → 语音-标题
- 标题不超过 12 个字，只保留核心主题

【四、输出格式】
先输出一行：
FILENAME: xxx.md

然后输出完整 markdown：

---
type: (meeting/chat/voice_note)
title: (和文件名标题一致)
created: {date_str}
source: audio_asr
people: []
tags: [transcript]
status: raw-processed
confidence: (high/medium/low)
---

# 一句话总结
(一句话)

# 核心信息
(要点列表)

# 结论 / 决策
(如有)

# 待办
(如有)

# 关键观点 / 有价值表达
(如有)

# 疑义 / 待核实
(如有)

# 少量原话摘录
(2-3 句关键原话)

【五、输出要求】
- 不要解释，不要说"以下是整理结果"
- 直接给 FILENAME 和 markdown 内容
- 只输出最终正文

ASR 转写内容：

{transcript[:25000]}
"""

    url = AI_API_BASE.rstrip("/") + "/chat/completions"
    payload = {
        "model": AI_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "你是一个语音转录整理专家，擅长把 ASR 转写的粗糙文本整理成结构化、精炼的 Markdown 文件。"},
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
    content = data["choices"][0]["message"]["content"].strip()
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.S)

    filename_match = re.search(r"FILENAME:\s*(.+\.md)", content)
    if filename_match:
        filename = filename_match.group(1).strip()
        md_content = re.sub(r"FILENAME:\s*.+\.md\s*", "", content, count=1).strip()
    else:
        filename = f"{date_str} 语音-未命名.md"
        md_content = content

    return filename, md_content


def main() -> int:
    parser = argparse.ArgumentParser(
        description="本地录音转录工具：音频 → ASR 转录 → AI 整理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
转录引擎优先级：Groq Whisper > Cohere Transcribe
（通义听悟需要额外配置 OSS，适合播客等在线音频）

长音频（>10min）自动分段处理，支持断点续传。
        """,
    )
    parser.add_argument("audio", type=Path, help="音频文件路径")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="日期（默认今天）")
    parser.add_argument("--transcribe-only", action="store_true", help="只转录不整理")
    parser.add_argument("--output-dir", type=Path, help="输出目录（默认 ./output）")
    args = parser.parse_args()

    if not args.audio.exists():
        print(f"文件不存在: {args.audio}", file=sys.stderr)
        return 1

    has_groq = bool(GROQ_API_KEY)
    has_cohere = bool(COHERE_API_KEY)
    has_tingwu = _tingwu_available()

    if not has_groq and not has_cohere and not has_tingwu:
        print("错误：未配置任何转录引擎。请在 .env 中配置至少一个：", file=sys.stderr)
        print("  - Groq Whisper: GROQ_API_KEY", file=sys.stderr)
        print("  - Cohere: COHERE_API_KEY", file=sys.stderr)
        print("  - 通义听悟: TINGWU_APP_KEY + ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET + TINGWU_OSS_BUCKET", file=sys.stderr)
        return 1

    output_dir = args.output_dir or OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    transcript_file = output_dir / "transcript-latest.md"

    duration = get_duration(args.audio)
    work_dir = WORK_DIR / args.audio.stem
    print(f"[1/2] 音频: {args.audio.name} ({duration/60:.1f} 分钟)", flush=True)
    if work_dir.exists():
        state = _load_state(work_dir / "state.json")
        done_count = sum(1 for v in state.get("texts", {}).values() if not v.startswith("[转录失败"))
        if done_count > 0:
            print(f"  发现上次进度: {done_count} 段已完成，断点续传", flush=True)

    # Step 1: 转录
    transcript = ""
    engine = "unknown"

    if has_tingwu:
        print("[2/2] 通义听悟转录...", flush=True)
        try:
            transcript = transcribe_tingwu(args.audio)
            engine = "通义听悟"
        except Exception as e:
            print(f"  [warn] 通义听悟失败: {e}", flush=True)

    if not transcript and has_groq:
        print("[2/2] Groq Whisper 转录...", flush=True)
        try:
            transcript = transcribe_groq(args.audio)
            engine = "Groq Whisper"
        except Exception as e:
            print(f"  [warn] Groq 失败: {e}", flush=True)

    if not transcript and has_cohere:
        print("[2/2] Cohere Transcribe 转录（fallback）...", flush=True)
        try:
            transcript = cohere_transcribe_local(args.audio)
            engine = "Cohere Transcribe"
        except Exception as e:
            print(f"  [error] Cohere 也失败了: {e}", file=sys.stderr)
            return 1

    if not transcript:
        print("[error] 所有转录引擎都失败了", file=sys.stderr)
        return 1

    failed_count = transcript.count("[转录失败")
    print(f"  转录完成: {len(transcript)} 字 ({engine})" + (f" ({failed_count} 段失败)" if failed_count else ""), flush=True)

    # 保存转录文本
    transcript_file.write_text(
        f"# 录音转录：{args.audio.name}\n\n"
        f"> 日期：{args.date}\n"
        f"> 时长：{duration/60:.1f} 分钟\n"
        f"> 转录引擎：{engine}\n\n"
        f"---\n\n{transcript}\n",
        encoding="utf-8",
    )
    print(f"  转录文本: {transcript_file}", flush=True)

    if args.transcribe_only:
        print(f"\n✅ 转录完成！文本在: {transcript_file}", flush=True)
        return 0

    # Step 2: AI 整理
    if not AI_API_KEY:
        print("缺少 AI API Key，跳过整理", file=sys.stderr)
        return 1

    print("[bonus] AI 整理中...", flush=True)
    filename, md_content = ai_process(transcript, args.date)
    print(f"  文件名: {filename}", flush=True)

    output_path = output_dir / filename
    output_path.write_text(md_content, encoding="utf-8")
    print(f"  写入: {output_path}", flush=True)

    # 清理工作目录
    import shutil
    work_dir = WORK_DIR / args.audio.stem
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n✅ 完成！", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
