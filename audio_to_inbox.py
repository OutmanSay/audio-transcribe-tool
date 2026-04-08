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


def load_contacts_reference(transcript: str, db_path: Path | None = None,
                             max_items: int = 40) -> tuple[str, dict[str, dict]]:
    """从 contacts.db 读取与 transcript 相关的人员，生成给 AI 的参考 context。

    两层策略：
      1. 取所有 aliases 对应的 real_name（常见 ASR 纠错字典，通常很小）
      2. 全量扫 contacts，找 transcript 里直接出现的人名
      3. 合并去重，按 max_items 截断

    Args:
        transcript: ASR 转写文本
        db_path: contacts.db 路径。None 时读环境变量 CONTACTS_DB，没设就返回空
        max_items: 最多塞到 prompt 的条目数（控制 token 消耗）

    Returns:
        (reference_text, contacts_dict)
          - reference_text: 给 AI prompt 用的 markdown 字符串（空字符串 = 未启用）
          - contacts_dict: {name: {position, team, phone, email, ...}} 用于后处理 enrich

    该函数是 **只读** 的，不会修改 contacts.db。兼容 contacts-tool 项目的 schema
    （参考: https://github.com/OutmanSay/contacts-tool）
    """
    # 决定 db path
    if db_path is None:
        env_path = os.getenv("CONTACTS_DB", "")
        if not env_path:
            return "", {}
        db_path = Path(env_path).expanduser()

    if not db_path.exists():
        return "", {}

    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # 只读打开
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return "", {}

    try:
        # Step 1: 所有 aliases
        try:
            alias_rows = conn.execute("SELECT real_name, alias FROM aliases").fetchall()
        except sqlite3.OperationalError:
            alias_rows = []
        aliases_map: dict[str, list[str]] = {}
        for row in alias_rows:
            aliases_map.setdefault(row["real_name"], []).append(row["alias"])

        # Step 2: 全量 contacts
        try:
            contact_rows = conn.execute(
                "SELECT name, team, position, phone, email FROM contacts"
            ).fetchall()
        except sqlite3.OperationalError:
            contact_rows = []

        # Step 3: 筛选 transcript 里出现过的人（name 或其任一 alias）
        matched: list[dict] = []
        matched_names: set[str] = set()
        for row in contact_rows:
            name = row["name"]
            if not name or name in matched_names:
                continue
            hit = name in transcript
            if not hit:
                for a in aliases_map.get(name, []):
                    if a and a in transcript:
                        hit = True
                        break
            if hit:
                matched.append({
                    "name": name,
                    "team": row["team"] or "",
                    "position": row["position"] or "",
                    "phone": row["phone"] or "",
                    "email": row["email"] or "",
                    "aliases": aliases_map.get(name, []),
                })
                matched_names.add(name)
    finally:
        conn.close()

    if not matched:
        return "", {}

    # Step 4: 截断
    if len(matched) > max_items:
        matched = matched[:max_items]

    # Step 5: 生成 reference text
    lines = ["【已知人员参考（从你的通讯录自动筛选的相关人员）】"]
    for c in matched:
        parts = [c["name"]]
        meta_bits = []
        if c["position"]:
            meta_bits.append(c["position"])
        if c["team"]:
            meta_bits.append(c["team"])
        if c["aliases"]:
            meta_bits.append(f"别名：{'、'.join(c['aliases'])}")
        if meta_bits:
            parts.append(f"（{'；'.join(meta_bits)}）")
        lines.append(f"- {''.join(parts)}")

    lines.append("")
    lines.append("请在下面的转写中识别这些人，即使 ASR 把名字听成了别的同音字"
                 "（比如人名容易被听成同音字，或者只出现了别名/昵称）。")
    lines.append("- people 字段里请使用上面的**标准名字**（列表第一列）")
    lines.append("- 如果转写出现的名字不在列表里，记到\"疑义 / 待核实\"")
    lines.append("")

    contacts_dict = {c["name"]: c for c in matched}
    return "\n".join(lines), contacts_dict


def enrich_people_field(md_content: str, contacts_dict: dict[str, dict]) -> str:
    """后处理：从 md 里 frontmatter 的 people: [...] 字段抽出名字，用 contacts_dict
    补充一段"# 人员信息"小板块，插在"# 归档建议"后面（或正文末尾，原文 footer 前）。

    不修改原 people: 字段，只追加补充信息。
    """
    if not contacts_dict:
        return md_content

    # 抓 people 列表
    m = re.search(r"^people:\s*\[(.*?)\]", md_content, flags=re.M)
    if not m:
        return md_content
    raw = m.group(1).strip()
    if not raw:
        return md_content

    # 解析 "name1, name2, name3" 或 "'name1', 'name2'"
    names = [n.strip().strip("'\"") for n in raw.split(",") if n.strip()]
    enriched: list[tuple[str, dict]] = []
    for n in names:
        if n in contacts_dict:
            enriched.append((n, contacts_dict[n]))

    if not enriched:
        return md_content

    # 生成补充板块
    block_lines = ["# 人员信息补充", ""]
    for name, info in enriched:
        bits = []
        if info.get("position"):
            bits.append(info["position"])
        if info.get("team"):
            bits.append(info["team"])
        if info.get("phone"):
            bits.append(f"📱 {info['phone']}")
        if info.get("email"):
            bits.append(f"✉️ {info['email']}")
        block_lines.append(f"- **{name}**：{' · '.join(bits) if bits else '（联系方式待补充）'}")
    block_lines.append("")

    block = "\n".join(block_lines)

    # 插入位置：找"# 原文"头部，插在它前面；没有就追加末尾
    raw_header_match = re.search(r"\n---\n\n> 以下为原始转写", md_content)
    if raw_header_match:
        pos = raw_header_match.start()
        return md_content[:pos].rstrip() + "\n\n" + block + md_content[pos:]
    else:
        return md_content.rstrip() + "\n\n" + block


def detect_is_meeting(transcript: str, filename_hint: str = "") -> tuple[bool, str]:
    """启发式判断转写是否为会议 / 多人对话场景。

    返回 (is_meeting, reason)。命中则建议在输出末尾附完整原文，方便审计回看。
    """
    reasons = []

    # 文件名信号
    name_lower = (filename_hint or "").lower()
    meeting_kws = [
        "会议", "讨论", "对齐", "汇报", "月会", "周会", "复盘", "评审", "宣讲", "电话",
        "meeting", "align", "discuss", "review", "standup", "retro", "sync",
    ]
    for kw in meeting_kws:
        if kw in name_lower:
            reasons.append(f'文件名含"{kw}"')
            break

    # 内容信号 1：多发言人时间戳（通义听悟 / 飞书妙记 / Otter 导出常见格式）
    if re.search(r"(?:发言人|Speaker|speaker)\s*\d+\s+\d+:\d+", transcript):
        reasons.append("检测到多发言人时间戳格式")

    # 内容信号 2：转写够长（长对话大概率是会议）
    if len(transcript) > 2000:
        reasons.append(f"长度 {len(transcript)} 字（>2000）")

    is_meeting = len(reasons) >= 2 or any("发言人时间戳" in r for r in reasons)
    return is_meeting, " / ".join(reasons) if reasons else "无会议信号"


def scan_for_raw_transcript(extra_dirs: list[Path] | None = None) -> Path | None:
    """扫描几个默认目录，找最近的手动导出的原文 md 文件。

    用于 Path B 的 `--scan` 模式：用户把 Tingwu / Otter / 飞书妙记的 `_原文.md`
    下载到 Downloads 或 inbox 后，脚本自动捡起来整理。

    扫描规则：
      1. 默认扫 `~/Downloads` 和 `TRANSCRIPT_INBOX_DIR` 环境变量指定的目录
      2. 文件名含 "_原文" 的 .md 优先匹配
      3. 或者内容前 500 字含 "发言人 N NN:NN" 格式
      4. 按 mtime 倒序，返回最新一份
    """
    search_dirs = [Path.home() / "Downloads"]
    inbox_env = os.getenv("TRANSCRIPT_INBOX_DIR", "")
    if inbox_env:
        search_dirs.append(Path(inbox_env))
    if extra_dirs:
        search_dirs.extend(extra_dirs)

    candidates = []
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            candidates.append(f)

    matches = []
    for f in candidates:
        name = f.name
        if "_原文" in name or "_transcript" in name.lower():
            matches.append(f)
            continue
        try:
            head = f.read_text(encoding="utf-8", errors="ignore")[:500]
            if re.search(r"(?:发言人|Speaker|speaker)\s*\d+\s+\d+:\d+", head):
                matches.append(f)
        except Exception:
            continue

    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def ai_process(transcript: str, date_str: str, append_raw: bool = False,
               contacts_reference: str = "",
               contacts_dict: dict[str, dict] | None = None) -> tuple[str, str]:
    """用 AI 整理转录文本，返回 (filename, markdown_content)。

    Args:
        transcript: ASR 转写原文
        date_str: 日期字符串（AI 生成 frontmatter.created 和文件名前缀用）
        append_raw: 是否在整理后的 md 底部追加完整原文。
                    会议 / 长对话建议 True，短语音笔记建议 False。
                    默认 False。调用方可传入 detect_is_meeting 的结果。
        contacts_reference: 从 load_contacts_reference 返回的第一个元素，
                           作为"已知人员参考"注入 prompt，帮 AI 纠正 ASR 听错的名字。
        contacts_dict: 从 load_contacts_reference 返回的第二个元素，用于 AI 输出后
                      的 enrich_people_field 后处理（补充职位/team/联系方式）。

    Returns:
        (filename, markdown_content)
    """
    import urllib.request

    # contacts reference 放在 prompt 最前面，让 AI 在处理前就有"人物表"
    reference_section = contacts_reference + "\n" if contacts_reference else ""

    prompt = f"""{reference_section}你现在的任务，是把下面的 ASR 转写文本，整理成一份可直接进入知识管理系统 inbox 的标准 Markdown 文件。

目标：
- 原始转写后续可以删除
- 保留足够有用的信息，供后续进入你的工作流
- 输出必须稳定、精炼、可分流
- 最终结果要适合"下载为 .md 文件后手动放进 inbox"

请严格按下面规则执行：

【一、先判断类型】
把内容判断为以下三类之一：
- meeting：会议 / 对齐 / 工作沟通 / 讨论
- chat：和朋友 / 同事 / 他人的聊天
- voice_note：个人语音记录 / 随想 / 复盘

【二、信息处理原则】
1. 删除冗余口语、重复句、语气词、无意义寒暄
2. 不要脑补；不确定内容放进"疑义 / 待核实"
3. 人名、数字、时间、地点、任务归属单独检查
4. 不保留长原文，但保留少量关键原话作为证据
5. 输出尽量精炼，但不能丢掉真正有价值的信息
6. 目标是：即使删掉原始转写，这份 md 仍然够用

【三、命名规则】
请先生成一个文件名，格式必须是：

YYYY-MM-DD 类型-标题.md

其中：
- meeting → 会议-标题.md
- chat → 聊天-标题.md
- voice_note → 语音-标题.md

要求：
- 标题不超过 12 个字
- 只保留核心主题
- 不要用空泛标题，比如"记录""内容整理""聊天摘要"
- 文件名只输出一个最终版本

示例：
- 2026-03-13 会议-产品需求对齐.md
- 2026-03-13 聊天-和朋友聊创业.md
- 2026-03-13 语音-关于工作边界.md

【四、输出格式】
请严格按以下顺序输出：

第一部分：
文件名：xxx.md

第二部分：
一个完整 markdown 文件内容，格式如下：

---
type:
title:
created:
source: audio_asr
people: []
tags: [inbox, transcript]
status: raw-processed
confidence:
---

# 一句话总结

# 核心信息

# 结论 / 决策

# 待办

# 关键观点 / 有价值表达

# 疑义 / 待核实

# 少量原话摘录

# 归档建议

【五、字段要求】
- type：只能是 meeting / chat / voice_note
- title：和文件名标题一致
- created：如果原文没有明确时间，就用今天日期（{date_str}）
- people：提取明确出现的人
- confidence：high / medium / low 三选一
- 归档建议：给出后续更可能进入哪个知识库主档

【六、输出要求】
- 不要解释
- 不要额外说"以下是整理结果"
- 不要重复原文
- 直接给出可下载的文件

下面是 ASR 转写内容：

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
            "User-Agent": "audio-transcribe-tool/1.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"].strip()
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.S)

    # 解析文件名。四种匹配方式按优先级尝试：
    #   1. 带标签的 "文件名：xxx.md" / "FILENAME: xxx.md"
    #   2. 首行裸文件名（AI 偶尔省略标签）
    #   3. 从 frontmatter 的 type + title 推断（AI 经常直接跳过文件名声明）
    #   4. 兜底：date 语音-未命名
    filename = None
    md_content = content

    m = re.search(r"(?:文件名[:：]|FILENAME\s*:)\s*(.+?\.md)", content)
    if m:
        filename = m.group(1).strip()
        md_content = re.sub(r"(?:文件名[:：]|FILENAME\s*:)\s*.+?\.md\s*\n?", "", content, count=1).strip()
    else:
        first_line = content.lstrip().split("\n", 1)[0].strip()
        m2 = re.match(r"^(\d{4}-\d{2}-\d{2}\s+(?:会议|聊天|语音)-[^\n]+?\.md)$", first_line)
        if m2:
            filename = m2.group(1).strip()
            md_content = content.lstrip().split("\n", 1)[1].strip() if "\n" in content.lstrip() else ""

    # 方式 3：从 frontmatter 的 type + title 推断
    if not filename:
        type_match = re.search(r"^type:\s*(meeting|chat|voice_note)", content, flags=re.M)
        title_match = re.search(r"^title:\s*(.+?)$", content, flags=re.M)
        created_match = re.search(r"^created:\s*(\d{4}-\d{2}-\d{2})", content, flags=re.M)
        if type_match and title_match:
            kind_map = {"meeting": "会议", "chat": "聊天", "voice_note": "语音"}
            kind = kind_map[type_match.group(1)]
            title = title_match.group(1).strip().strip("\"'")
            title = re.sub(r'[/\\:*?"<>|]', '', title).strip()[:30]
            fn_date = created_match.group(1) if created_match else date_str
            filename = f"{fn_date} {kind}-{title}.md"

    if not filename:
        filename = f"{date_str} 语音-未命名.md"

    # 后处理：从 people 字段 enrich 补充人员信息（职位/团队/联系方式）
    if contacts_dict:
        md_content = enrich_people_field(md_content, contacts_dict)

    # 会议 / 长对话：追加完整原文（code 硬拼接，不依赖 AI 输出）
    if append_raw:
        footer = (
            "\n\n---\n\n"
            "> 以下为原始转写，保留审计 / 回看用\n\n"
            "# 原文\n\n"
            f"{transcript.strip()}\n"
        )
        md_content = md_content.rstrip() + footer

    return filename, md_content


def main() -> int:
    parser = argparse.ArgumentParser(
        description="本地录音转录工具：音频 → ASR 转录 → AI 整理（支持会议自动检测 + 手动转录回填）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
两条使用路径:
  Path A: 本地音频 → ASR → AI 整理
    python3 audio_to_inbox.py recording.m4a

  Path B: 手动导出的转录文本 → AI 整理（当 ASR 质量差时)
    python3 audio_to_inbox.py --from-text transcript_原文.md
    python3 audio_to_inbox.py --scan                     # 自动找最近的 _原文.md

转录引擎优先级: 通义听悟 > Groq Whisper > Cohere Transcribe
长音频(>10min)自动分段，支持断点续传。

会议自动检测:
  脚本会基于文件名("会议/讨论/align/review"等) + 内容("发言人 N NN:NN" 格式) + 长度
  自动判断是否为会议。命中会议模式时，**输出末尾会追加完整原文**，方便审计回看。
  用 --meeting / --no-meeting 手动覆盖自动判断。
        """,
    )
    parser.add_argument("audio", type=Path, nargs="?", default=None, help="音频文件路径（Path A 自动转录模式）")
    parser.add_argument("--from-text", type=Path, default=None, help="已有的转录文本路径（Path B 回填模式，跳过 ASR）")
    parser.add_argument("--scan", "--scan-inbox", dest="scan", action="store_true",
                        help="扫 ~/Downloads + $TRANSCRIPT_INBOX_DIR 自动找最近的 _原文.md（等同 --from-text）")
    parser.add_argument("--title", type=str, default="", help="标题（Path B 可选，会影响输出文件名）")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="日期（默认今天）")
    parser.add_argument("--transcribe-only", action="store_true", help="只转录不整理")
    parser.add_argument("--output-dir", type=Path, help="输出目录（默认 ./output）")
    parser.add_argument("--meeting", action="store_true", help="强制会议模式（在输出末尾追加完整原文）")
    parser.add_argument("--no-meeting", action="store_true", help="强制关闭会议模式（即使自动检测命中）")
    args = parser.parse_args()

    output_dir = args.output_dir or OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # --scan 等价于自动找 --from-text 源文件
    if args.scan and not args.from_text:
        found = scan_for_raw_transcript()
        if not found:
            print("[scan] 没在 ~/Downloads 或 $TRANSCRIPT_INBOX_DIR 里找到 _原文.md / 发言人格式的转录文件",
                  file=sys.stderr)
            return 1
        print(f"[scan] 发现候选: {found}", flush=True)
        args.from_text = found

    # 参数校验
    if not args.audio and not args.from_text:
        print("错误：必须提供音频文件、--from-text 文本路径，或 --scan", file=sys.stderr)
        parser.print_help()
        return 1
    if args.audio and args.from_text:
        print("错误：音频和 --from-text 只能二选一", file=sys.stderr)
        return 1

    # ════════════════════════════════════════════════════════════
    # Path B: 回填模式（--from-text / --scan）
    # ════════════════════════════════════════════════════════════
    if args.from_text:
        if not args.from_text.exists():
            print(f"文件不存在: {args.from_text}", file=sys.stderr)
            return 1

        print(f"[回填模式] 读取转录: {args.from_text}", flush=True)
        transcript = args.from_text.read_text(encoding="utf-8").strip()
        # 如果是 md 文件，尝试去掉 frontmatter 和标题头
        if args.from_text.suffix.lower() == ".md":
            if transcript.startswith("---\n"):
                end = transcript.find("\n---\n", 4)
                if end > 0:
                    transcript = transcript[end+5:].strip()
            transcript = re.sub(r"^#\s+.*?\n", "", transcript, count=1)
            transcript = re.sub(r"(?m)^>\s*.*?\n", "", transcript)
            transcript = re.sub(r"^---+\n", "", transcript, count=1, flags=re.M).strip()

        if not transcript:
            print("转录文本为空", file=sys.stderr)
            return 1

        print(f"  读取完成: {len(transcript)} 字", flush=True)

        # 保存转录副本
        transcript_file = output_dir / "transcript-latest.md"
        transcript_file.write_text(
            f"# 录音转录：{args.title or args.from_text.stem}（手动回填）\n\n"
            f"> 日期：{args.date}\n"
            f"> 源文件：{args.from_text}\n\n"
            f"---\n\n{transcript}\n",
            encoding="utf-8",
        )

        if args.transcribe_only:
            print(f"\n✅ 回填完成！文本在: {transcript_file}", flush=True)
            return 0

        if not AI_API_KEY:
            print("缺少 AI_API_KEY，跳过整理", file=sys.stderr)
            return 1

        # 会议判断 → 决定是否附原文
        is_meeting, reason = detect_is_meeting(transcript, args.from_text.name)
        use_meeting = args.meeting or (is_meeting and not args.no_meeting)
        print(f"[模式判断] is_meeting={is_meeting} reason=\"{reason}\" → {'会议模式（附原文）' if use_meeting else '普通模式'}",
              flush=True)

        # contacts 集成：从 CONTACTS_DB 环境变量指向的 db 智能筛选人员 → 注入 AI context
        contacts_ref, contacts_dict = load_contacts_reference(transcript)
        if contacts_ref:
            print(f"[contacts] 从通讯录匹配到 {len(contacts_dict)} 个相关人员，已注入 AI context", flush=True)

        print("[AI 整理]", flush=True)
        filename, md_content = ai_process(transcript, args.date, append_raw=use_meeting,
                                          contacts_reference=contacts_ref,
                                          contacts_dict=contacts_dict)

        # 用户显式 --title 时覆盖 AI 生成的文件名
        if args.title:
            safe_title = re.sub(r'[/\\:*?"<>|]', '', args.title).strip()
            kind = "会议" if use_meeting else "语音"
            filename = f"{args.date} {kind}-{safe_title}.md"
        print(f"  文件名: {filename}", flush=True)

        output_path = output_dir / filename
        output_path.write_text(md_content, encoding="utf-8")
        print(f"  写入: {output_path}", flush=True)
        print(f"\n✅ 回填完成！", flush=True)
        return 0

    # ════════════════════════════════════════════════════════════
    # Path A: 自动转录模式
    # ════════════════════════════════════════════════════════════
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
        print("缺少 AI_API_KEY，跳过整理", file=sys.stderr)
        return 1

    # 会议判断 → 决定是否附原文
    is_meeting, reason = detect_is_meeting(transcript, args.audio.name)
    use_meeting = args.meeting or (is_meeting and not args.no_meeting)
    print(f"[模式判断] is_meeting={is_meeting} reason=\"{reason}\" → {'会议模式（附原文）' if use_meeting else '普通模式'}",
          flush=True)

    # contacts 集成：从 CONTACTS_DB 环境变量指向的 db 智能筛选人员 → 注入 AI context
    contacts_ref, contacts_dict = load_contacts_reference(transcript)
    if contacts_ref:
        print(f"[contacts] 从通讯录匹配到 {len(contacts_dict)} 个相关人员，已注入 AI context", flush=True)

    print("[bonus] AI 整理中...", flush=True)
    filename, md_content = ai_process(transcript, args.date, append_raw=use_meeting,
                                      contacts_reference=contacts_ref,
                                      contacts_dict=contacts_dict)
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
