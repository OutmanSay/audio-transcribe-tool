# audio-transcribe-tool

播客和录音转文字工具 | Transcribe podcasts and audio recordings to text

支持多种播客平台和音频格式，三引擎自动 fallback，可选 AI 生成结构化笔记 / 会议纪要。

## 功能

- **播客转录** (`podcast_transcribe.py`)：从播客链接提取音频并转录
  - 支持：小宇宙、Apple Podcasts、YouTube、Bilibili、RSS feed、直接音频链接
  - Apple Podcasts 提取失败时，自动查找页面内小宇宙链接作为 fallback
  - 可选生成 AI 学习简报（`--brief`）
- **本地录音转录** (`audio_to_inbox.py`)：本地音频文件转录 + AI 整理
  - 支持 .m4a / .mp3 / .wav 等常见格式
  - 自动判断类型（会议/聊天/语音笔记）并生成结构化 Markdown
  - **🆕 会议模式**：自动检测会议 / 多人对话，输出末尾追加完整原文供审计回看
  - **🆕 Path B 回填模式**：`--from-text` 或 `--scan` 直接处理手动导出的 `_原文.md`
  - `--transcribe-only` 模式：只输出原始文本
- **三引擎自动 fallback**：通义听悟 > Groq Whisper > Cohere Transcribe
- **长音频自动分段**：>10 分钟自动切分，支持断点续传

👉 **会议模式 / 手动回填模式的完整说明：[MEETING_MODE.md](./MEETING_MODE.md)**

## 快速开始

### 1. 安装依赖

```bash
# 系统工具（必须）
brew install ffmpeg    # macOS
# apt install ffmpeg   # Linux

# 可选：YouTube/Bilibili 支持
brew install yt-dlp

# Python 依赖（通义听悟需要，其他引擎无额外依赖）
pip install aliyun-python-sdk-core oss2
```

### 2. 配置 API Key

```bash
cp .env.example .env
# 编辑 .env，至少配置一个转录引擎的 API Key
```

### 3. 使用

```bash
# 播客转录
python3 podcast_transcribe.py "https://www.xiaoyuzhoufm.com/episode/xxx"

# 播客转录 + AI 学习简报
python3 podcast_transcribe.py "https://www.xiaoyuzhoufm.com/episode/xxx" --brief

# 本地录音转录（自动 ASR + AI 整理）
python3 audio_to_inbox.py /path/to/recording.m4a

# 已有手动转录文件（如通义听悟网页版导出的 _原文.md），跳过 ASR 直接整理
python3 audio_to_inbox.py --from-text /path/to/transcript_原文.md

# 自动扫描 ~/Downloads 和 $TRANSCRIPT_INBOX_DIR 里最新的 _原文.md
python3 audio_to_inbox.py --scan

# 强制会议模式（输出末尾追加完整原文）
python3 audio_to_inbox.py recording.m4a --meeting

# 只转录不整理
python3 audio_to_inbox.py /path/to/recording.m4a --transcribe-only

# 指定输出目录
python3 podcast_transcribe.py "https://..." --output-dir ./my-output
```

## 转录引擎

三个引擎按优先级自动 fallback，至少配置一个即可使用：

| 引擎 | 中文质量 | 速度 | 免费额度 | 有效期 | 是否需要代理 |
|------|---------|------|---------|--------|------------|
| **通义听悟**（推荐） | ⭐⭐⭐⭐⭐ | 中等 | 每天 2 小时（文件转录） | 90 天试用 | 不需要 |
| **Groq Whisper** | ⭐⭐⭐⭐ | 快 | 每天 8 小时 | 永久免费 | 需要 |
| **Cohere Transcribe** | ⭐⭐⭐ | 中等 | 每月 1000 次请求 | 永久免费 | 不需要 |

### 申请方式

**通义听悟**（阿里云） — 中文最佳，90 天试用每天 2 小时
1. 访问 [通义听悟控制台](https://tingwu.aliyun.com)，开通服务
2. 获取 `TINGWU_APP_KEY`
3. 在 [RAM 控制台](https://ram.console.aliyun.com/manage/ak) 获取 AccessKey
4. （audio_to_inbox.py 需要）在 [OSS 控制台](https://oss.console.aliyun.com) 创建 Bucket
5. 需要阿里云账号（中国手机号注册）

**Groq Whisper** — 永久免费，每天 8 小时
1. 访问 [Groq Console](https://console.groq.com)，用邮箱或 GitHub 注册
2. 在 API Keys 页面创建 Key
3. 无需信用卡

**Cohere Transcribe** — 永久免费，每月 1000 次
1. 访问 [Cohere Dashboard](https://dashboard.cohere.com)，注册账号
2. 注册后自动获得 Trial API Key
3. 无需信用卡

## 支持的播客平台

| 平台 | 链接格式 | 说明 |
|------|---------|------|
| 小宇宙 | `xiaoyuzhoufm.com/episode/...` | 直接提取 |
| Apple Podcasts | `podcasts.apple.com/...` | 自动 fallback 到小宇宙 |
| YouTube | `youtube.com/watch?v=...` | 需要 yt-dlp |
| Bilibili | `bilibili.com/video/...` | 需要 yt-dlp |
| RSS Feed | 任意 RSS/XML URL | 提取最新一期 |
| 直接链接 | `.mp3` / `.m4a` 等 | 直接下载 |

## AI 简报

使用 `--brief` 参数，转录完成后自动用 AI 生成结构化学习简报，包含：

- 执行摘要
- 核心论点表格
- 详细笔记（按章节整理）
- 关键数据速查
- 金句摘录
- 行动建议

支持任何 OpenAI 兼容 API（OpenAI、MiniMax、Deepseek 等）。

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TINGWU_APP_KEY` | 通义听悟 AppKey | - |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | 阿里云 AK | - |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 阿里云 SK | - |
| `TINGWU_OSS_BUCKET` | OSS Bucket 名称 | - |
| `TINGWU_OSS_ENDPOINT` | OSS Endpoint | `https://oss-cn-beijing.aliyuncs.com` |
| `GROQ_API_KEY` | Groq API Key | - |
| `COHERE_API_KEY` | Cohere API Key | - |
| `AI_API_KEY` | AI API Key（简报用） | - |
| `AI_API_BASE_URL` | AI API Base URL | `https://api.openai.com/v1` |
| `AI_MODEL` | AI 模型名称 | `gpt-4o-mini` |
| `PODCAST_PROXY` | HTTP 代理 | - |
| `TRANSCRIPT_OUTPUT_DIR` | 输出目录 | `./output` |
| `TRANSCRIPT_NOTES_DIR` | 笔记输出目录（可选） | - |
| `TRANSCRIPT_INBOX_DIR` | `--scan` 模式额外扫描的目录（通常指向你的 Obsidian inbox） | - |

## License

MIT
