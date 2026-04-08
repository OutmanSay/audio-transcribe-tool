# 会议模式 + 手动回填 + Contacts 集成

`audio_to_inbox.py` v1.1 新增三大特性：

1. **会议自动检测**：基于文件名 + 内容信号判断，命中后输出末尾追加完整原文
2. **Path B 手动回填**：`--from-text` / `--scan` 直接处理手动导出的 `_原文.md`
3. **Contacts 集成**：从你维护的通讯录 sqlite 智能筛选相关人员，注入 AI context 做 ASR 人名纠错 + 输出后 enrich

---

## 为什么需要这些

ASR（包括通义听悟 / Whisper）对**多人中文对话**的识别质量有天花板：
- 人名同音字会乱（例：同一个"X总"在不同段落被听成不同的字）
- 专有名词（项目/产品）听成生僻词
- 多说话人时归属经常错

所以推荐的处理策略：

```
音频类型            推荐路径                    AI 整理
─────────────────────────────────────────────────────────────
个人语音笔记、闲聊   Path A（自动 ASR）          默认 prompt
短会议（<10min）    Path A（自动 ASR）          默认 prompt
工作会议、对齐      Path B（通义听悟网页转）     会议 prompt（附原文）
多人对话、电话      Path B                     会议 prompt（附原文）
含专有名词/人名     Path B + CONTACTS_DB       会议 prompt + 人名纠错
```

---

## Path A：本地音频自动转录

```bash
# 基本用法：自动转录 + 自动会议检测 + AI 整理
python3 audio_to_inbox.py ~/Downloads/xxx.m4a

# 强制会议模式（输出附原文）
python3 audio_to_inbox.py xxx.m4a --meeting

# 强制关闭会议模式（自动检测命中时也不附原文）
python3 audio_to_inbox.py xxx.m4a --no-meeting

# 只转录不整理
python3 audio_to_inbox.py xxx.m4a --transcribe-only
```

流程：
1. ASR（通义听悟 → Groq → Cohere fallback）
2. 会议自动检测（见下）
3. AI 整理（分类 meeting/chat/voice_note + 结构化 markdown）
4. 命中会议模式 → 输出末尾追加完整原文
5. 写入 `$TRANSCRIPT_OUTPUT_DIR`

---

## Path B：手动转录回填

当你对 ASR 质量不满意时（常见于工作会议、多专有名词、多说话人），推荐流程：

```
1. 把音频上传到 通义听悟网页版（或 Otter / 飞书妙记 / 腾讯会议转写）
2. 下载导出的 _原文.md（含"发言人 1  00:00" 格式的时间戳）
3. 把文件留在 ~/Downloads 或拖到 $TRANSCRIPT_INBOX_DIR
4. 跑 audio_to_inbox.py --scan
```

### 精确指定文件

```bash
python3 audio_to_inbox.py --from-text /path/to/xxx_原文.md
python3 audio_to_inbox.py --from-text xxx_原文.md --title "Q2 复盘"
```

### 自动扫描

```bash
# 扫 ~/Downloads + $TRANSCRIPT_INBOX_DIR，取最近的 _原文.md
python3 audio_to_inbox.py --scan
```

**扫描规则**：
1. 搜索路径：`~/Downloads` + 环境变量 `TRANSCRIPT_INBOX_DIR` 指向的目录
2. 匹配条件（满足任一即可）：
   - 文件名含 `_原文` 或 `_transcript`
   - 内容前 500 字含 `发言人 N  NN:NN` 格式
3. 按 mtime 倒序，选最新一份

**支持的输入格式**：
- 纯文本 `.txt`
- Markdown `.md`（自动剥离 frontmatter 和一级标题）
- 通义听悟网页版导出的"发言人 1  00:03" 格式
- 飞书妙记、Otter、腾讯会议转写的同类格式

---

## 会议自动检测

`detect_is_meeting()` 根据以下信号判断，命中则在输出末尾追加完整原文供审计回看：

**文件名信号**（任一命中即得一票）：
- 中文：会议 / 讨论 / 对齐 / 汇报 / 月会 / 周会 / 复盘 / 评审 / 宣讲 / 电话
- 英文：meeting / align / discuss / review / standup / retro / sync

**内容信号**：
- **多发言人时间戳**（通义听悟 / 飞书妙记 / Otter 导出格式）→ **直接命中会议模式**
  - `发言人 1  00:03` / `Speaker 1  00:03` / `speaker 1  00:03`
- **长度 > 2000 字**（长对话大概率不是短语音笔记）

**判定**：≥2 个信号命中，或"发言人时间戳"单独命中。

**手动覆盖**：
- `--meeting`：强制走会议模式（即使没命中自动检测）
- `--no-meeting`：强制不走会议模式（即使命中自动检测）

---

## Contacts 集成（v1.1 新增）⭐

**问题**：ASR 对中文人名的识别质量非常差。即使手动转录，人名也常有别名变体（比如"张总"在转写里可能被听成"章总"，或者一个人在对话中既被叫全名又被叫昵称）需要消歧。

**方案**：维护一个 sqlite 通讯录，audio_to_inbox 跑的时候**只读查询**这个库，把相关人员作为"参考表"注入 AI prompt。

### 配置

设置环境变量 `CONTACTS_DB` 指向你的通讯录 sqlite 文件：

```bash
export CONTACTS_DB="~/.openclaw/runtime/contacts.db"
```

**完全可选**——没设 / 文件不存在时，audio_to_inbox 会正常工作，只是没有人名纠错这一步。

### Schema

兼容 [contacts-tool](https://github.com/OutmanSay/contacts-tool) 项目的 schema。如果你直接用 sqlite3 创建也行：

```sql
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,           -- 标准名字（列表第一列）
    team TEXT DEFAULT '',         -- 部门/团队
    position TEXT DEFAULT '',     -- 职位
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    note TEXT DEFAULT ''
);

CREATE TABLE aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    real_name TEXT NOT NULL,      -- 对应 contacts.name
    alias TEXT NOT NULL            -- ASR 听错的变体或昵称（例："张总"、"小张"、"Zhang" 等）
);
```

### 运行时行为

1. **读取** `$CONTACTS_DB`（只读打开，`file:db?mode=ro`）
2. **筛选**：扫 transcript 里直接出现的 contact.name，以及其 aliases 出现的情况
3. **生成参考表**：塞到 AI prompt 最前面：
   ```
   【已知人员参考（从你的通讯录自动筛选的相关人员）】
   - 张三（产品经理；产品团队；别名：小张、张PM）
   - 李四（设计主管；设计团队）
   - ...

   请在下面的转写中识别这些人，即使 ASR 把名字听成了别的同音字。
   people 字段里请使用上面的标准名字（列表第一列）。
   如果转写出现的名字不在列表里，记到"疑义 / 待核实"。
   ```
4. **AI 输出后 enrich**：在 md 正文末尾（原文 footer 前）插入 `# 人员信息补充` 板块，附上每个 `people:` 成员的职位 / 部门 / 电话 / 邮箱：
   ```markdown
   # 人员信息补充

   - **张三**：产品经理 · 产品团队 · 📱 138-xxxx-xxxx
   - **李四**：设计主管 · 设计团队
   ```

### 只读保证

- 只用 `SELECT` 查询，绝不 `INSERT/UPDATE/DELETE`
- 使用 `sqlite3.connect("file:...?mode=ro", uri=True)` 明确只读
- 可以安全地和 [contacts-tool](https://github.com/OutmanSay/contacts-tool) 共用同一份 db

---

## 输出格式示例

触发会议模式 + contacts 集成后，输出的 md 结构：

```markdown
---
type: meeting
title: Q2 产品路线对齐
created: 2026-04-08
source: audio_asr
people: [张三, 李四]
tags: [inbox, transcript]
status: raw-processed
confidence: medium
---

# 一句话总结

与张三对齐 Q2 产品路线和设计资源安排...

# 核心信息

1. ...
2. ...

# 结论 / 决策

- ...

# 待办

- [ ] **张三**：节后约时间与外部团队开会
- [ ] **李四**:：修改设计方案

# 关键观点 / 有价值表达

> "..."
> — 发言人

# 疑义 / 待核实

- 发言人 3 身份待确认
- ...

# 少量原话摘录

> "..."

# 归档建议

建议进入 xxx 主档

# 人员信息补充

- **张三**：产品经理 · 产品团队 · 📱 138-xxxx-xxxx
- **李四**：设计主管 · 设计团队 · 📱 139-xxxx-xxxx

---

> 以下为原始转写，保留审计 / 回看用

# 原文

发言人 1  00:00
...（完整原文 8000 字）
```

---

## 环境变量速查

| 变量 | 必需？ | 说明 |
|---|---|---|
| `AI_API_KEY` | 是 | AI 整理用（OpenAI 兼容，如 OpenAI / MiniMax / DeepSeek 等） |
| `AI_API_BASE` | 否 | 默认 `https://api.openai.com/v1` |
| `AI_MODEL` | 否 | 默认 `gpt-4o-mini` |
| `TINGWU_APP_KEY` + AK/SK + OSS_BUCKET | Path A 推荐 | 通义听悟 ASR |
| `GROQ_API_KEY` | Path A fallback | Groq Whisper ASR |
| `COHERE_API_KEY` | Path A fallback | Cohere Transcribe ASR |
| `CONTACTS_DB` | 否（推荐） | 通讯录 sqlite 路径，启用人名纠错 |
| `TRANSCRIPT_OUTPUT_DIR` | 否 | 输出目录，默认 `./output` |
| `TRANSCRIPT_INBOX_DIR` | 否 | `--scan` 额外扫描的目录 |

---

## 未来计划

- [ ] 质量检测（`detect_transcript_quality`）：识别 Whisper 训练污染、乱码、重复等，质量差时自动建议走 Path B
- [ ] Contacts 反向学习：AI 输出 `疑义` 里新发现的人名自动提示"要不要加到 contacts.db 并建 alias"
- [ ] 多 transcript 合并：把同一场会议的多段录音合并后一次性整理
