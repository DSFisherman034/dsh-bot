# DSH Bot

一个会自己看群、挑时机说话的 QQ 群友。基于 [`botsns`](../qqbot/botsns)。

- **webhook**：`https://dshbot.rantindom.com` → nginx :8443 → `127.0.0.1:3704`
- **服务**：`dsh-bot.service`（systemd，用户 `dsfisherman`）
- **解释器**：`/opt/qqbot/venv/bin/python`
- **模型**：阿里云兼容端点 `deepseek-v4.1-flash-1017`（key 在 `config.ini`）

## 结构

| 文件 | 职责 | 改了要重启吗 |
|---|---|---|
| `main.py` | 编排：收消息、判断要不要理、发回复、心跳 | 要 |
| `brain.py` | 提示词组装 + LLM 调用 + 输出清洗 | 要 |
| `memory.py` | SQLite 群聊流水（7 天）、群事件记忆（60 天）与每群状态 | 要 |
| `agent/AGENTS.md` | **主路径人设与输出纪律**（@/插话走 DSH，心跳暂时关闭，下一轮自动生效） | 不要 |
| `persona.md` | 兜底人设（DSH 桥挂掉时的本地模型、私信；改了立即生效） | 不要 |
| `config.ini` | 凭据、模型、行为参数 | 要 |
| `data.db` | 运行时数据库（自动创建，别手动改） | — |

## 行为逻辑

**被 @ / 私信** → 一定回复。
**没被 @ 的群消息** → 进 `ambient_debounce` 秒合并窗口，模型决定说还是 SKIP；
明显是在接你上一句、或者问你的，必须回。
**群里安静** → 心跳/暖场暂时关闭（`heartbeat_enabled = false`），等重新设计话题机制；
打开后按 `heartbeat_interval / idle / cooldown` 跑。
**群事件记忆** → 每 30 分钟把 ≥12 条新消息压成 1–3 条事件，存 `memories` 表，
注入后续 prompt（最近 8 条，保留 60 天）；启动 5 秒后先补一次。
**同群发送** → 串行；发送中途来了真人追问/纠错，会停掉剩余气泡，交给新消息处理。

两种平台模式都支持：
- 「接收全部消息」：事件是 `GROUP_MESSAGE_CREATE`，用 `mentions[].is_you` / 内容含名字判断；
- 「仅 @ 时说话」：事件全是 `GROUP_AT_MESSAGE_CREATE`，直接当被 @。

其他护栏：被 QQ 拒绝（无权限/频控）会给该群关掉主动性；等 1–4 秒再发，模拟打字。

## 调参（config.ini）

```ini
[behavior]
reply_policy = full         ; full：允许插话；mention_only：只回 @
ambient_debounce = 10       ; 非 @ 消息的合并窗口（秒）
heartbeat_enabled = false   ; 暖场/心跳总开关（先关，等重新设计话题机制）
heartbeat_interval = 120    ; 心跳检查间隔（仅 enabled=true）
heartbeat_idle = 300        ; 群静多久才考虑自己开口
heartbeat_cooldown = 600    ; 距上次机器人发言多久才允许开口
max_reply_chars = 300
```

**想让它更活跃**：调小 `ambient_debounce`（5）；心跳先关着，等新的话题机制。
**想让它更安静**：调大 `ambient_debounce`，或把 `reply_policy` 改成 `mention_only`（只回 @）。

## 调人设

主路径（@、插话都走 DSH；心跳暂时关闭）的人设在 `agent/AGENTS.md`，改完**下一轮自动生效**，不用重启——
DSH 每次请求前会重新读工作区的 AGENTS.md，把变更注入会话。

`persona.md` 只剩兜底作用：DSH 桥挂掉时的本地模型、以及私信回复。它每次调用都查 mtime，改完立即生效。
两份最好一起改，语气才一致，不然桥上桥下像两个人。

当前人设：**小鲸鱼**（外号大肥鱼，2026-10-28 v0.36，见 `CHANGELOG.md`）。想快速试兜底那份：

```bash
cd /opt/dsh-bot && /opt/qqbot/venv/bin/python - <<'PY'
import asyncio, sys, time; sys.path.insert(0, ".")
from brain import Brain
cfg = dict(base_url="...", api_key="...", model="deepseek-v4.1-flash-1017",
           temperature=0.95, max_tokens=90, name="DSH")
b = Brain(cfg, "persona.md")
rows = [("小明", "这破服务器风扇吵死了", time.time()-60, 0)]
print(asyncio.run(b.respond(rows, "addressed", "小明")))
PY
```

## 运维

```bash
systemctl status dsh-bot
journalctl -u dsh-bot -f                 # 看它每次说话/沉默的决策
curl -s localhost:3704/health
curl -s localhost:3704/stats             # 库里消息数/群数
```

改代码后的部署（**工作区里的 `~/dsh/dsh-bot` 是源码副本**）：

```bash
cd ~/dsh/dsh-bot
sudo install -o dsfisherman -g dsfisherman -m 644 main.py brain.py memory.py persona.md README.md CHANGELOG.md /opt/dsh-bot/
sudo install -o dsfisherman -g dsfisherman -m 600 config.ini /opt/dsh-bot/config.ini
sudo install -o dsfisherman -g dsfisherman -m 600 agent/AGENTS.md /opt/dsh-bot/agent/AGENTS.md
sudo systemctl restart dsh-bot
```

只改人设不用重启：`persona.md` 热读，`agent/AGENTS.md` 下一轮自动重载。


## 出站口（给 DSH 用）

```bash
qq-send --groups                       # 看有哪些群
qq-send <group_openid> "在吗"           # 发一句话
qq-send --dc <openid> "在吗"            # 发私信
echo "多行也行" | qq-send <group_openid>
```

底层是 `POST http://127.0.0.1:3704/send`（群）和 `/send_dc`（私信），body 分别是
`{"group": "...", "text": "..."}` / `{"openid": "...", "text": "..."}`，必须带 `x-token`
（值在 `config.ini [send] token`）。token 是防止公网乱发的唯一屏障——
这台机器的 nginx 把整个域名反代到 3704，`/send`、`/send_dc` 公网可达，所以**别把 token 贴到聊天记录里**。

## 私聊与群上下文（弱隔离）

- 私聊 prompt 会带**群里最近 12 条 + 群记忆 6 条**，标注为「群里公开」；
- 群聊 prompt 不读 `dc:`，私聊内容不会回流群聊；
- 私聊里可以评论群里发生的事，但不会把私聊内容当成群里已知。

## 图片适用范围（scope）

- `images.scope` 和图描述分开：描述是「图里有什么」，scope 是「什么时候能用」。
- 模型 prompt 会带图库「描述｜范围」；嘲讽/互损图不用于严肃、安慰、自己被调侃。
- 模型可另起一行写 `[[img-scope:图描述|适用范围]]` 更新，`send_group` 会解析并剥离，不发群。
- 手动：`GET/POST /image_scope`（token），body `{"image":"hash 或描述","scope":"..."}`。
- 发错图直接认，不解释后台。

## 图库来源与搜索（v0.36）

- 已导入 **nekowhale 322 张**（大肥鱼/GPT娘，CC BY-NC 4.0，作者：可以叫我云大人 / kiriyuki.com）
  和 **dsh-meme 内置大肥鱼 24 张**（代码 MIT；图包许可见包内）。
- 模型没有工具调用；要搜图就输出 `[[img-search:描述或情绪]]`，`send_group` 会搜索图库并发最像的一张，不需要审批。
- 手动搜索：`GET /image_search?q=摸鱼&limit=5`（token），返回 hash/描述/scope/score。
- 后续图源登记在 `image_sources.json`；`GET /image_sources` 查看，先存入口不急着接。
- 图片文件统一 `644`：`/opt/dsh-bot/images` 是 nginx root，权限 600 会 403，QQ 就报 `40093007 富媒体文件下载失败`。

## 本地测试（不往群里发）

```bash
TOKEN=$(awk -F' = ' '/^token =/{print $2}' /opt/dsh-bot/config.ini)
curl -s localhost:3704/debug_reply -H "x-token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"大肥鱼","task":"ambient","reset":true}'
```

- `task`：`ambient`（非 @）或 `addressed`（被 @）
- `reset: true` 重开一个测试会话；不传就沿用上下文
- 只返回 `reply`，不发送到 QQ；用来做人设/接话回归

## 排障

| 现象 | 看什么 |
|---|---|
| 群里完全没反应 | 平台回调地址；`journalctl` 有没有 POST；两种模式是否设置对了 |
| 有 POST 但没回复 | 日志里 `决定插话但模型选择沉默` / `模型选择沉默` 是正常行为 |
| 回复失败 | 日志 err_code；`40034100` 主动消息频控、`40034105` 无主动消息权限 |
| LLM 报错 | `LLM 调用失败`，检查 key 与 base_url 是否还有额度 |
| 502 | 进程挂了，`systemctl status dsh-bot` |

## 已知问题 / 待办

- 群名片/群名拿不到，只能用 openid 前 8 位做标识。
- 长期记忆只有 7 天流水，没有摘要；聊久了它会忘掉早期内容。

## 开发约定

- **每次改动（代码/人设/文档）后必须推 git**：`git add -A && git commit -m "..." && git push`。
- `config.ini`、`data.db` 等本地文件永不入库（见 `.gitignore`）。
