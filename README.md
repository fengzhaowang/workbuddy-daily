# Buddy 加油站 · 每日签到 + 派猫（GitHub Actions · 多账号版）

**每 6 小时跑一次**，把「加油站签到」和「猫猫旅行」都做掉：整点那轮做全套，
中间三轮只收猫（领走已到家的旅行积分，并立刻再派一趟）。结果推送到你选的通知渠道。

跑在 GitHub 的免费云端 runner 上，**不需要你有一台常开设备**，可以多人共用同一个仓库。

---

## 它到底做了什么

整轮（每天 00:20）按顺序处理**每一个账号**：

1. **签到**：先查活动状态 → 没签就领，签过了就跳过（幂等，重复跑不会多领）。
2. **猫猫旅行**：
   - ① 先领掉**已经到家**那一趟的旅行积分；
   - ② 再判断今天还能不能派新的一趟——`daily_limit_reached` 为真（今天已经派过）就不派。
   - > 这两步是同一个闭环。所以**只收猫**的那几轮（`--mode cat`）领完积分会立刻再派一趟，
   > 猫不会闲着，积分才转得起来；见「[定时](#定时)」。
3. **推送通知**：分两层，**可以逐人定制**——
   - **每个人自己的渠道**：账号清单里给他写 `notify`，他那一份就**只发到他自己指定的渠道**（可多个），不进汇总卡片；
   - **没配的人**：汇总成**一份**，前面一行总览、每个账号一个分区、区内「🏠 签到」「🐾 猫猫」分行写清，发到全局渠道。
   共支持 9 种通知渠道，配哪个发哪个（见下）。只收猫那几轮默认**有变化才推**，不拿「猫还在路上」刷你屏。
   正文长什么样、换行怎么处理，见「[推送长什么样](#推送长什么样)」。
4. **隔离**：
   - **账号之间互相隔离**——A 的 token 过期，B 照常签到；
   - **账号内部**，猫猫段被 `try` 兜住——它怎么炸都不改该账号的签到结论。

退出码：所有账号签到成功 → `0`；加 `--allow-partial`（或 `WB_ALLOW_PARTIAL=1`）则「至少一个成功」→ `0`。
**猫猫段一律不影响退出码**（`--mode cat` 那几轮签到段跳过，此时退出码看猫猫段）。

---

## 快速开始

### 1. 准备通知渠道（9 选 1，也可以都配）

**不用飞书完全没问题。** 支持下面 9 种，**配了哪个就发哪个，配几个就发几个**：

| 渠道 | 需要的 secret | 拿到它的成本 | 适合谁 |
| --- | --- | --- | --- |
| **Server酱** | `SERVERCHAN_KEY` | 微信扫码，约 1 分钟 | ⭐ 没有飞书/企微的首选 |
| **PushPlus** | `PUSHPLUS_TOKEN` | 微信扫码，约 1 分钟 | ⭐ 同上，另一个选择 |
| **钉钉机器人** | `DINGTALK_WEBHOOK` + `DINGTALK_SECRET` | 建个只有自己的群 | 已经在用钉钉 |
| **企业微信机器人** | `WECOM_WEBHOOK` | 建个只有自己的群（**不需要企业认证**） | 想要最像飞书的形态 |
| **飞书机器人** | `FEISHU_WEBHOOK`（+ `FEISHU_SECRET`） | 建群加机器人 | 已经在用飞书 |
| **邮件** | `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` / `MAIL_TO` | 邮箱开 SMTP、生成授权码 | 最通用；不即时，适合当兜底 |
| **Bark** | `BARK_KEY` | iPhone 装个免费 App | iPhone 用户 |
| **ntfy** | `NTFY_TOPIC` | 手机装个 App，订阅一个 topic | 开源、免注册 |
| **Telegram** | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | 找 @BotFather 建 bot | 用 Telegram 的人 |

配了多个会**各发一份**。只想发其中几个就点名（写在 secret `NOTIFY_CHANNELS` 里）：

```yaml
NOTIFY_CHANNELS: dingtalk,email   # 只发这两个
NOTIFY_CHANNELS: all              # 所有已配置的渠道（默认就是这个行为）
```

> 这一节说的是**全局那一层**（收「汇总卡片」，以及那些在 `notify` 里只写渠道名、想借全局凭据的人）。
> 想让每个人各收各的、各用各的渠道，见下面的「[每人收自己的通知](#每人收自己的通知可以逐人配各用各的渠道)」。

**一个都没配也不会报错**，只在运行结果里明确写一句「未配置任何推送渠道，已跳过」——
不会静默失败。某个渠道发失败也只影响它自己，既不改退出码，也不影响别的渠道。

#### 各渠道怎么拿凭据

| 渠道 | 步骤 |
| --- | --- |
| Server酱 | 微信扫码登录 <https://sct.ftqq.com> → 复制 SendKey（形如 `SCTxxxxx`）。关注「方糖」服务号后即可在微信收到 |
| PushPlus | 微信扫码登录 <https://www.pushplus.plus> → 发送消息 → 复制 token |
| 钉钉 | 钉钉群 → 群设置 → 智能群助手 → 添加机器人 → **自定义** → 复制 Webhook；安全设置见下方 ⚠️ |
| 企业微信 | 企业微信群 → 右上角 `…` → 群机器人 → 添加 → 复制 Webhook。自己建个只有自己的群即可 |
| 飞书 | 飞书群 → 设置 → 群机器人 → 添加机器人 → **自定义机器人** → 复制 Webhook；安全设置见下方 ⚠️ |
| 邮件 | 任何邮箱都行，密码填 **SMTP 授权码**而不是登录密码。QQ 邮箱：设置 → 账户 → 开启 SMTP → 生成授权码，端口 `465` |
| Bark | iPhone 装 Bark App → 首页复制 `https://api.day.app/xxxx` 里的 `xxxx`（自建服务器填 `BARK_URL`） |
| ntfy | 手机装 ntfy App → 订阅一个 topic → 把 topic 名填进 `NTFY_TOPIC`。**topic 就是密码，取随机一点** |
| Telegram | 找 @BotFather 建 bot 拿 token；给 bot 发条消息后访问 `https://api.telegram.org/bot<token>/getUpdates` 看 `chat.id` |

> ⚠️ **钉钉和飞书的安全设置，选错了必然失败：**
>
> | 设置 | 钉钉 | 飞书 | 说明 |
> | --- | --- | --- | --- |
> | **加签 / 签名校验** | ✅ 推荐 | ✅ 推荐 | 密钥填进 `DINGTALK_SECRET` / `FEISHU_SECRET` |
> | **自定义关键词** | ✅ 可以 | ✅ 可以 | 关键词都填 **`加油站`**（消息正文里本来就有） |
> | ~~IP 白名单~~ | ❌ **千万别选** | ❌ **千万别选** | GitHub runner 出口 IP 是动态的，必然被拒 |

#### 配完先自检，别等 Actions 跑失败才发现

```bash
python3 scripts/test_notify.py                        # 自动测「所有已配好的」渠道
python3 scripts/test_notify.py --channel serverchan --key '<SendKey>'
python3 scripts/test_notify.py --channel pushplus   --token '<Token>'
python3 scripts/test_notify.py --channel dingtalk --webhook '<Webhook>' --secret '<加签密钥>'
python3 scripts/test_notify.py --channel email --smtp-host smtp.qq.com \
    --smtp-user me@qq.com --smtp-pass '<授权码>' --mail-to me@qq.com
python3 scripts/test_notify.py --channel bark --key '<Key>'
python3 scripts/test_notify.py --channel ntfy --topic '<topic>'
python3 scripts/test_notify.py --channel feishu --webhook '<Webhook>' --secret '<签名密钥>'
python3 scripts/test_notify.py --channel bark --print   # 只看会发什么内容，不真发
```

它发的是**真实内容**，走的也是 `daily.py` **同一条发送路径**（`_dispatch`），
所以自检能通、线上就一定能通。失败时直接告诉你原因
（签名/加签不对、关键词不匹配、IP 白名单、凭据无效、chat_id 不对……），
**输出里的密钥都做过打码**。

### 2. 本机导出凭证

云端 runner 读不到你本机的登录态，所以由本机把凭证集中成一份清单，再写进仓库 secret。

```bash
python3 scripts/export_token.py --as "我的名字"     # 把本机账号加入清单
python3 scripts/export_token.py --list              # 看一眼（脱敏 + 剩余有效期）
python3 scripts/export_token.py --check             # 逐个验活
```

清单落在 `accounts.local.json`（权限 `600`，已在 `.gitignore` 里，**不会进仓库**）。

### 3. 注入 secret

**方式 A：装了 gh 并且已登录**

```bash
gh auth login                                                   # 只需一次
python3 scripts/export_token.py --repo <owner/name> --push      # 写入 WB_ACCOUNTS
# 再把你选的通知渠道凭据写进去（下面以 Server酱 + 邮件为例）
gh secret set SERVERCHAN_KEY --repo <owner/name>                # 粘贴 SendKey
gh secret set SMTP_HOST      --repo <owner/name>                # smtp.qq.com
gh secret set SMTP_USER      --repo <owner/name>                # 你的邮箱
gh secret set SMTP_PASS      --repo <owner/name>                # 邮箱授权码
```

**方式 B：没有 gh（或不想登录）**——手动粘贴

```bash
python3 scripts/export_token.py --print-json
```

把输出的整段 JSON 复制到：仓库 → Settings → Secrets and variables → Actions → New repository secret，
名字填 **`WB_ACCOUNTS`**。通知渠道的那些 secret（`SERVERCHAN_KEY` / `DINGTALK_WEBHOOK` / `SMTP_*` …）
同样在网页上手工加，加几个都行。

> `--print-json` 会输出**明文凭证**，只粘贴到 GitHub 的 Secret 输入框，别贴进聊天、别提交进仓库。

### 4. 建仓库并跑一次

```bash
git init && git add -A && git commit -m "Buddy 加油站每日任务"
git remote add origin git@github.com:<你的账号>/<仓库名>.git
git push -u origin main
```

然后 仓库 → Actions → 选「Buddy 加油站每日任务」→ Run workflow。

---

## 多人/多账号怎么用

清单就是一个 JSON 数组，**一个 secret 装 N 个账号**（`notify` 决定这一份发给谁，不写就进汇总卡片）：

```json
{
  "accounts": [
    {"name": "我",     "token": "eyJ...", "uid": "xxx", "domain": "www.workbuddy.cn"},
    {"name": "小号",   "token": "eyJ...", "uid": "yyy", "domain": "www.workbuddy.cn",
     "notify": ["dingtalk"]},
    {"name": "同事A",  "token": "eyJ...", "uid": "zzz", "domain": "www.workbuddy.cn",
     "notify": {"wecom": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"}}
  ]
}
```

字段尽量宽容（`token`/`access_token`、`uid`/`user_id` 都认，渠道名认中文和简写）；
完整示例见 `accounts.example.json`。

**加账号的三种方式：**

```bash
# ① 在这台机器上登录了那个账号 → 直接抓本机登录态
python3 scripts/export_token.py --as "小号"

# ② 对方把自己的 token 给你（一个 JSON 文件），合并进来
python3 scripts/export_token.py --import 同事给的.json

# ③ 手工编辑 accounts.local.json
```

**移除账号：** `python3 scripts/export_token.py --remove "小号"`

**清理重复：** `python3 scripts/export_token.py --dedupe`

判重按 **uid**，不是按名字——所以同一个人用两个名字各跑一次 `--as` 也只会留一条
（保留带自己通知配置的那条）。没有 uid 的旧凭证退回按 token 判。
`--list` 会把「其实是同一个人」的条目直接点名出来。

> `--add-local`（以及不带任何参数的默认动作）合并时**先按名字、再按 uid**。
> 只按名字判重会出这种坑：本机登录态只有一个人，`--as 甲` 跑一次、`--as 乙` 再跑一次
> 就得到两条同一个人——云端「共 2 个账号」其实只覆盖 1 个人，白占一份时间预算，
> 卡片上还重复一遍。

### 每人收自己的通知（可以逐人配、各用各的渠道）

默认是把没配 `notify` 的人汇总成一份发给全局渠道。想让人各收各的，就在他名下写 `notify`：

**写法一：只选渠道，凭据用全局的**（最省事——全局只配一份，各人自己挑）

```json
{"name":"小号", "token":"eyJ...", "uid":"yyy", "domain":"www.workbuddy.cn",
 "notify": ["dingtalk"]}
```

小号的结果会**单独一条**发到钉钉（用全局 `DINGTALK_WEBHOOK`），不再进汇总卡片。
全局凭据没配全的话，运行结果里会明确写「钉钉：凭据来自全局 secret，但缺 webhook」，
不会静默不发。

**写法二：带自己的凭据**（他自己建的群 / 自己的微信推送 / 自己的邮箱）

```json
{"name":"同事A", "token":"eyJ...", "uid":"zzz", "domain":"www.workbuddy.cn",
 "notify": {
   "channels": ["wecom", "serverchan"],
   "wecom":      {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=他的"},
   "serverchan": {"key": "SCT他的"}
 }}
```

一个渠道也能写成一整串（值直接落到该渠道的主字段）：

```json
"notify": {"wecom": "https://qyapi.weixin.qq.com/...", "email": "他@qq.com"}
```

**写法三：白名单**——`channels` 是白名单，没点到的渠道即使写了配置也不发；
写成 `"channels": []` 就等于回到汇总卡片。

#### 三条关键规则

| 规则 | 为什么 |
| --- | --- |
| **自带凭据 = 只用自带的，绝不拿全局的兜底** | 否则会把 A 的机器人密钥发进 B 的群、B 的邮箱——串号比不发更糟 |
| **配了 `notify` 就不进汇总卡片** | 尊重「我只收自己渠道」的意图；不想收到大卡片的人不会收到 |
| **配了但没配全 → 只报问题，不静默补发汇总** | 少字段会在运行结果的 `notices` 里点名，一眼能看出是谁没收到 |

#### 不用手抄 JSON，用命令配

```bash
# 先看这个人在发给谁
python3 scripts/export_token.py --notify "张三"

# 只选渠道（借全局凭据）
python3 scripts/export_token.py --notify "张三" --use dingtalk

# 带上他自己的凭据（一个渠道一条 --use，可写多次）
python3 scripts/export_token.py --notify "张三" --use 'serverchan:key=SCTxxxxx'
python3 scripts/export_token.py --notify "张三" \
    --use 'wecom:webhook=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx' \
    --use 'email:host=smtp.qq.com,user=me@qq.com,pass=授权码,to=me@qq.com'

# 改回「并进汇总卡片」
python3 scripts/export_token.py --notify "张三" --clear-notify

# 忘了某个渠道要哪些字段？先看这张表
python3 scripts/export_token.py --channels
```

字段名写错、必填项漏了会**当场报错**（不用等云端跑完才发现某人一条通知都没收到），
渠道名也认中文（`企微` / `钉钉` / `邮件` / `飞书`）。

> ⚠️ **改完要重新推一次 secret**，因为「谁发给谁」就存在 `WB_ACCOUNTS` 里：
> `python3 scripts/export_token.py --repo <owner/name> --push`
> （用网页粘贴的，重新跑 `--print-json` 覆盖一下那个 secret。）

#### 配完先体检一遍，别等 Actions 跑失败

```bash
python3 scripts/test_notify.py --plan --from accounts.local.json   # 只看路由，不发消息、不打印凭证
python3 scripts/test_notify.py --plan                              # 或直接读环境变量 WB_ACCOUNTS
```

输出形如：

```
  1. 甲        → 企业微信（自带）
  2. 乙        → 钉钉（借全局）
  3. 丙        → 汇总卡片
全局渠道（NOTIFY_CHANNELS 未点名）：钉钉、Server酱
```

它会顺带把「张三声明了渠道但没配全」「有汇总卡片要发却一个全局渠道都没配」这类问题指出来。
确认路由无误后，再按渠道跑一次真实自检（会真的发一条）：
`python3 scripts/test_notify.py --channel dingtalk`。

#### 配了却没生效？按这三处对

这个坑真的发生过一次（清单配好了逐人通知，线上跑的却是旧提交，字段被静默丢掉，
最后只剩一句「未配置任何推送渠道」）。现在这三处会自动把话说清楚：

1. **每个人到底发去哪** —— 运行日志里每个账号后面都写着，
   例如 `推送=Server酱`、`推送=汇总卡片`。这是最直接的一处。
2. **本次跑的是哪个提交** —— 日志开头会打印 `本次运行提交：<sha>`，
   和 `git log -1` 对一下就知道代码是不是最新的。
3. **清单里有没有代码不认识的字段** —— 出现了就会点名：
   `张三：清单条目里有本版代码不认识的字段 notify，它们会被静默忽略……`
   看到这句先 push 最新代码；如果是字段名拼错，改掉即可。

另外，如果结果是
`未配置任何推送渠道，已跳过：张三、李四 这 2 个账号……`，
那它的意思很明确：这几个人没配 `notify`，全局也没配任何渠道 secret。
**若他们其实配过** → 多半是 `WB_ACCOUNTS` 这个 secret 还是旧的，重新 `--push` 一次。

#### 旧写法仍然兼容

账号上直接写 `webhook` / `secret`（只支持飞书）等价于 `notify.feishu`。
清单文件里的旧写法会在下次写入时自动迁移成 `notify`；
刷新 token（`--as` / `--import`）时**不会**冲掉某人的 `notify` 配置。

### 各账号怎么拿 token

每个账号的 token 来自**那个账号在客户端登录过的机器**：

- 同一个人的多个账号：在本机依次登录并刷新（或各跑一次 `--as`）；
- 别人的账号：让他按上面方式导出后把 JSON 给你，你用 `--import` 合并。

`--add-local` 合并时**先按名字、再按 uid 兜底**，同名视为同一个账号会被覆盖，
所以换机器重跑、或者同一个人换个名字再跑，都不会产生重复条目。

---

## 推送长什么样

```
⚠️ 有问题：XXX · ✅ 签到 3/4 · 🎁 收 1 趟积分 · 🚀 派出 1 趟 · 🐱 1 只在路上

**1. XXX**

🏠 **签到**
✅ 今天已经签过了（今日 100 · 连续 3 天 · 累计 300）

🐾 **猫猫**
猫咪 龙焰喵（SSR）
🎁 领到旅行积分 +6
🛑 今天已经派过了（每天一趟）

**2. XXX**

🏠 **签到**
✅ 签到成功 +6 积分（今日 6 · 连续 1 天 · 累计 6）

🐾 **猫猫**
猫咪 奶油喵（SR）
🚀 派出猫猫 → 图书馆（3 小时后回）
```

固定的三段结构：

1. **一行总览**（`⚠️ 有问题：… · ✅ 签到 3/4 · 🎁 收 1 趟积分 …`）——
   拿到通知第一眼就能回答「有没有事要我管」，不用逐行读六个账号，出错时直接点名是谁；
2. **每个账号一块**，块内「🏠 签到」「🐾 猫猫」两段分行写清；多账号才有编号，单账号不编号；
3. **需要时末尾一行说明**，例如「本轮比计划晚 301 分钟触发（GitHub 定时器延迟）」——
   凌晨收到通知时正文自己会解释原因（见「[cron 是 UTC](#cron-是-utc而且它是尽力而为不准点是正常的)」）。

标题也分场景：单账号是 `Buddy 加油站日报 · 冯召旺 · 10-09 10:49`（各人收自己那份时一眼知道是谁的），
多账号汇总报人数，只收猫那几轮标明「收猫」。

### 换行：为什么不能全文只用一个 `\n`

各家对 markdown 换行的规矩不一样，写死一个 `\n` 的结果就是「手机上全挤成一行」：

| 渠道 | 单个 `\n` 会换行吗 | 本项目怎么发 |
| --- | --- | --- |
| 企业微信 | ❌ 实测不换行，得用 `\n\n` | 段内行尾补两个空格 |
| 钉钉 | ⚠️ 官方 FAQ：换行格式是 `\n`，且「`\n` 前后各两个空格」 | 段内行尾补两个空格 |
| Server酱 / PushPlus | ❌ 走 markdown 渲染，`\n` 属软换行，会被折叠 | 段内行尾补两个空格 |
| 飞书卡片 | ✅ 卡片里的 lark_md 单换行就有效 | 直接用 `\n`，最紧凑 |
| Bark / ntfy / Telegram / 邮件 | ✅ 纯文本，`\n` 本来就是硬换行 | 直接用 `\n` |

所以默认用「**段内行尾两个空格 + 段间空行**」——这是 markdown 的硬换行写法，
也是钉钉官方推荐的。**手机上要是还看着挤成一行**，加一个 secret `WB_PUSH_NEWLINE` 换个风格即可，
不用改代码：

| 取值 | 效果 | 什么时候用 |
| --- | --- | --- |
| `space` | 段内行尾两空格换行、段间空行（**默认**） | 一般情况 |
| `blank` | 所有行之间都空一行 | 渲染器不认行尾空格时的兜底：最保守，一定能换行，就是正文长一些 |
| `lf` | 单个换行符 | 确认自己的客户端认单换行、想要最紧凑时 |

想先看效果再决定（不联网、不发消息）：

```bash
python3 scripts/preview_push.py                 # 默认（四个典型账号的完整日报）
python3 scripts/preview_push.py --single        # 单账号（各人收自己那份的样子）
python3 scripts/preview_push.py --cat-only      # 只收猫那一档
python3 scripts/preview_push.py --newline blank # 换一种换行风格
```

预览里会把行尾那两个空格显示成 `··`——真实内容里就是「两个空格 + 换行」，
渲染出来就是换行。`--raw` 可以看到原样。

### 为什么每行都写得这么短

正文是给**手机通知**看的，不是给日志看的：「该怎么办」写在本文档里，通知里只说「发生了什么」。
所以像「没有待领取的旅行积分」这种**常态**是直接不出现的——
每多一句废话，真正的异常就多一分被忽略的概率。

---

## secret 清单

**账号凭证（必须）**

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `WB_ACCOUNTS` | ✅（推荐） | 账号清单 JSON 数组，一个 secret 装 N 个账号 |
| `WB_TOKEN` / `WB_UID` / `WB_DOMAIN` | ➖ | 旧版单账号写法，**仍然兼容**；同时配了清单则以清单为准 |

**通知渠道（一个都不配也能跑，只是不发通知；配几个发几个）**

> 下面这些是**全局** secret，供「没配 `notify` 的人」的汇总卡片、以及「`notify` 里只写渠道名想借全局」的人使用。
> 某个人想用自己的机器人 / 自己的微信推送 / 自己的邮箱，那些凭据写在 `WB_ACCOUNTS` 的 `notify` 里，不用另外建 secret。

| 名称 | 渠道 | 说明 |
| --- | --- | --- |
| `NOTIFY_CHANNELS` | — | 可选。点名全局要发哪几个，如 `dingtalk,email` 或 `all`；**不影响**各人自己的 `notify` |
| `SERVERCHAN_KEY` | Server酱 | 推微信，扫码即可 |
| `PUSHPLUS_TOKEN` | PushPlus | 推微信，扫码即可 |
| `WECOM_WEBHOOK` | 企业微信 | 群机器人 Webhook |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` | 钉钉 | 安全设置**必须选「加签」** |
| `FEISHU_WEBHOOK` / `FEISHU_SECRET` | 飞书 | 签名校验才需要 `_SECRET` |
| `BARK_KEY` / `BARK_URL` | Bark | iOS 推送；`BARK_URL` 用于自建 |
| `NTFY_TOPIC` / `NTFY_URL` | ntfy | topic 相当于密码；`NTFY_URL` 用于自建 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram | bot token 与你的 chat id |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `MAIL_TO` | 邮件 | 密码填**授权码**；465 走 SSL，其他端口走 STARTTLS |

可选环境变量：`WB_ENV`（结果里显示的环境名）、`WB_BUDGET_SECONDS`（整轮时间预算）、
`WB_ALLOW_PARTIAL=1`（有账号失败也不算整轮失败）、`WB_NOTIFY_MODE=always`（只收猫那几轮也每轮都推）、
`WB_PUSH_NEWLINE=space|blank|lf`（推送正文的换行风格，见「[推送长什么样](#推送长什么样)」）、
`WB_ENDPOINT`（自建/私有化端点）。

---

## 定时

`.github/workflows/daily.yml` 里有**两条 cron，合起来每 6 小时一次**：

```yaml
on:
  schedule:
    - cron: "20 16 * * *"        # ① 整轮：签到 + 派猫猫    = 北京 00:20
    - cron: "20 22,4,10 * * *"   # ② 只收猫 ×3            = 北京 06:20 / 12:20 / 18:20
```

| 档 | 北京 | 做什么 |
| --- | --- | --- |
| ① | 00:20 | 签到 + 派猫猫（**整轮**） |
| ② | 06:20 / 12:20 / 18:20 | 只收猫：领已到家的旅行积分，并该派就派新的一趟 |

**为什么收猫要单独排？** 猫猫旅行是「派出去 → 几小时后回来 → 领积分」，一天只跑一次的话，
猫回来后就一直闲着，积分自然攒得慢。每 6 小时收一次，领完立刻又派一趟，一天能转好几轮。

**为什么收猫那几轮不重复签到？** 签到一天一次就够，重复调只是白跑接口。
所以那几轮用 `--mode cat`，只跑猫猫段（`do_cat()` 本身就是「先领后派」的闭环）。

**跑哪一段由「触发的 cron」决定**，映射表只有一处：`scripts/daily.py` 里的 `SCHEDULE_MODES`。
workflow 里刻意**不再写一份** `case`——两处各写一份、改了一处忘了另一处，就会静默跑错，
而且是那种「看起来一切正常、就是没积分」的错。
改了 cron 却忘了改那张表，脚本会**大声报警**并按整轮跑（安全的那一边），不会悄悄降级。
`scripts/test_offline.py` 还会直接读 `daily.yml` 逐条核对 cron 与那张表是否一致，对不上就测试失败。

**收猫轮默认只在有变化时推送**，免得「猫还在路上」一天推你四遍。
真领到积分、派了新的一趟、或者出错，都会照常推。想每轮都推就加 `WB_NOTIFY_MODE: always`。

也可以到 Actions 页面手动触发，并顺手选 `all`（整轮）或 `cat`（只收猫）。

### cron 是 UTC；而且它是「尽力而为」，不准点是正常的

**换算规则：`北京时间 = UTC + 8`。** 上面两条 cron 对应北京 00:20 与 06/12/18:20。

但更要紧的是另一件事：**GitHub 的 `schedule` 不保证准点。** 官方文档原话：

> The `schedule` event can be delayed during periods of high loads of GitHub Actions workflow runs.
> High load times include the start of every hour. If the load is sufficiently high enough,
> some queued jobs may be dropped. To decrease the chance of delay, schedule your workflow to run
> at a different time of the hour.

也就是：高峰期（整点最挤）会延迟，挤到一定程度**整轮会被丢掉**。这不是个例——社区里
2026 年 8 月、10 月都有大批人反馈「晚 3~10 小时」，有博主实测 4 个公开仓库的 `*/15`
工作流，7 天里只有 64% 的时间点到点跑上了，中位延迟 7.4 分钟，最长空档 137 分钟。
所以**看到通知在凌晨 5 点才来，第一反应不该是怀疑配置**。

本项目为此做了三件事，让它自己说清楚：

1. **日志第一行**就打出计划与实际：

   ```
   [cron] 触发 cron='20 16 * * *' -> 计划 10-08 16:20Z（北京 10-09 00:20），
          实际 10-08 21:21Z（北京 10-09 05:21），延迟 301 分钟；本轮模式=整轮
   ```

2. **结果 JSON 里的 `cron` 段**（机器可读）：`triggered_cron` / `planned_utc` /
   `planned_local` / `actual_utc` / `actual_local` / `delay_minutes`。

3. **延迟超过 30 分钟**（`LATE_MINUTES`）时，说明会同时写进 `notices`，
   并**附在推送正文末尾**——收到凌晨 5 点的通知时，正文里就解释了为什么。

**这件事对结果有影响吗？基本没有。** 签到是幂等的、派猫由服务端按天限额把关，
晚几个小时跑照样达标；真要说影响，只有「整轮被丢掉」这一种——那种情况下的兜底是：
① 每 6 小时那三档会补派当天的猫；② 签到一天一次，晚点到也比不到好。

> **想再稳一点（可选）：** GitHub 自 2026-03 起支持给 cron 配 IANA 时区，
> 可以写成下面这样，省掉 UTC 换算：
>
> ```yaml
>     - cron: "20 0 * * *"
>       timezone: "Asia/Shanghai"
> ```
>
> 本文没默认改成它，是因为**它一旦不被支持，整个 workflow 文件会被判为非法、一条都不跑**
> ——那正是我们最想避免的「静默失败」。你要改用，请先改完到 Actions 页面手动
> dispatch 一次确认能跑，再删掉旧的两条 cron。
>
> 若要把「定时是否真的发生」也监控起来（`schedule` 被延迟或丢弃时 GitHub **不会**通知你），
> 可以在最后加一步 ping 一个心跳地址（UptimeRobot / Healthchecks 之类），
> **没收到心跳就报警**——这是唯一能发现「整轮没跑」的办法。

> ⚠️ 今日派发名额是**按天限额**的（服务端 `daily_limit_reached`），所以收得多勤都不会「刷爆」：
> 名额用完那几轮会明确回一句「今日派发名额已用完」，不会报错、也不会重复派。

> 小贴士：若想让猫几乎不闲着，把收猫那档改成每 4 小时（`"20 20,0,4,8,12 * * *"` 这类）
> 会更贴一趟的时间。但注意 GitHub 定时有个已知毛病：**仓库连续 60 天无提交会自动停掉
> schedule**（见下），而且跑得越密，Actions 额度用得越快。

### 运行环境：镜像钉死，不用 `ubuntu-latest`

```yaml
runs-on: ubuntu-24.04        # 不用 ubuntu-latest
steps:
  - uses: actions/checkout@v5
```

两个原因，都是「别让环境在你不知道的时候变」：

- **`ubuntu-latest` 会自己换底。** GitHub 已公告该标签自 2026-10-19 起分几周内切换为 Ubuntu 26.04
  （[runner-images#14748](https://github.com/actions/runner-images/issues/14748)），
  完成时间约 2026-11-19。`latest` 是自动生效的，你不用改也会跟着变——
  而 Ubuntu 26.04 的 python3 版本、系统库、SSL 策略都和 24.04 不同。
  钉死 `ubuntu-24.04` 就是让每天跑的是同一套东西；想升级时手动改这一行。
- **`actions/checkout@v4` 跑在 Node 20 上，已被弃用。** runner 现在会强制用 Node 24 执行它并打告警。
  `v5` 的 `action.yml` 声明的是 `using: node24`，换上去告警就没了。

> 校验方式（不是猜的）：`curl -sSL https://raw.githubusercontent.com/actions/checkout/v5.1.0/action.yml | grep -A2 '^runs:'`
> → `using: node24`；同样办法看 `v4.4.0` → `using: node20`。

这两条都是**告警，不影响结果**——但它们属于「今天不修，将来某天静默挂掉」那类。
本项目除了 checkout 没有别的 action，所以换完就没有 Node 相关的告警了。

### 两个必须知道的坑

**① 仓库连续 60 天没有新提交，GitHub 会自动停掉 schedule。**
这是一个「只为跑定时任务而生」的仓库最容易踩的坑——某天你会发现它几个月没跑过。
兜底办法任选一个：每两个月随手 commit 一次；或把 `workflow_dispatch` 当手动补跑。

**② 猫猫的礼物会压到下一次运行才到账（现在最多 6 小时）。**
猫猫出去一趟 1~4 小时才回来，而「领礼物」这件事只有运行到才会做。
现在 6 小时一档，礼物最坏也就是等 6 小时——**这已经是这套定时能做到的上限**，
再密不会更快：一天只能派一趟（服务端 `daily_limit_reached`），瓶颈是「当天那趟有没有派成」，
不是收得多勤。真要多拿分，靠的是 6 小时那三档的**补派**能力：
整轮那趟若因为延迟、异常或额度问题没派成，中间几轮会把当天名额补上。

**多跑是安全的**：签到幂等（已签只会返回「已签」，不会重复发积分），
派猫由服务端的 `daily_limit_reached` 把关（今天派过就不再派），
所以多跑几轮的效果只是礼物到账更快、偶发漏跑能被下一轮补上。

**账号数的两个限制：**时间预算按 `420 + 150 × (账号数 − 1)` 秒自动放大，
`daily.yml` 里 `timeout-minutes: 30` 必须**明显大于**脚本预算（6 个账号时预算是 1170s），
否则 GitHub 会先一步强杀进程，脚本那套「超预算就优雅跳过」根本轮不到生效。
账号特别多（十个以上）相应调大，或拆成多个 workflow，
否则一个账号卡住会挤掉后面的预算（被挤掉的账号会在卡片上标 `SKIPPED`，下一轮自动补上）。

---

## 接口：全部与客户端实际请求核对过

> 没有用网上传的旧接口名。下面每个路径都用真实请求验证过「存在且语义正确」：
> 伪造路径返回 `404 route not found`，下列路径返回 `200` / 业务 `4xx`。

| 用途 | 方法 | 路径 |
| --- | --- | --- |
| 签到活动状态 | POST | `/v2/billing/meter/checkin-activity-status` |
| 领取签到积分 | POST | `/v2/billing/meter/daily-checkin` |
| 旅行状态 | GET | `/v2/activity/growth/buddy/travel/status` |
| 可选目的地 | GET | `/v2/activity/growth/buddy/travel/config` |
| 领取到家积分 | POST | `/v2/activity/growth/buddy/travel/claim`（body `{record_id}`） |
| 派出猫猫 | POST | `/v2/activity/growth/buddy/travel/depart`（body `{location_id}`） |
| 猫咪资料 | GET | `/v2/activity/growth/buddy/info` |

- Base URL：`https://copilot.tencent.com`
- 请求头：`Authorization: Bearer <token>`、`X-User-Id: <uid>`、`X-Domain: <domain>`
- 约定：**HTTP 200 且 `body.code == 0`** 才算业务成功；`code=10001` 表示今天已签到。

另有几个**实测存在、但本次没用到**的成长中心接口（想扩展时可直接用）：

| 用途 | 方法 | 路径 | 实测 |
| --- | --- | --- | --- |
| 任务列表 | GET | `/v2/activity/growth/tasks` | 200，返回任务数组 |
| 能量余额 | GET | `/v2/activity/growth/buddy/quota` | 200，`balance=8` |
| 开 Buddy 盲盒 | POST | `/v2/activity/growth/buddy/open` | 400 `insufficient energy` |

---

## 本地调试

```bash
python3 scripts/daily.py --local --as "我的名字"      # 本机单账号
python3 scripts/daily.py --accounts accounts.local.json   # 本机多账号（读清单文件）
python3 scripts/daily.py --list-accounts              # 只列出识别到的账号（脱敏 + 推送去向）
python3 scripts/daily.py --only "我,同事A"             # 只跑指定账号（名字或序号）
python3 scripts/daily.py --local --raw                # 附上脱敏后的原始返回
python3 scripts/daily.py --local --dry-run            # 只查状态，不做写操作
python3 scripts/daily.py --local --no-notify          # 不推送，只看结论
python3 scripts/daily.py --accounts accounts.local.json --mode cat   # 只收猫（跳过签到）
python3 scripts/daily.py --local --mode cat --no-notify              # 只收猫，看结论不发
python3 scripts/daily.py --local --mode auto                         # 按 TRIGGER_CRON 自动定档（云端就是这个）
python3 scripts/test_offline.py                       # 离线全链路自测（假服务端，不开外网）
python3 scripts/preview_push.py                       # 只看推送长什么样（不联网、不发消息）
python3 scripts/test_notify.py                        # 试通知渠道（真的发一条，失败给原因）
python3 scripts/test_notify.py --plan --from accounts.local.json  # 只体检路由，不发消息
python3 scripts/test_notify.py --channel ntfy --print  # 只看会发出去什么内容，不真发
```

改推送排版 / 文案时先跑 `scripts/preview_push.py`——它用一份覆盖各种状态的假数据，
把 markdown 版、纯文本版、飞书卡片会收到的内容并排打出来，比真发一条再回手机上看快得多
（行尾那两个空格会显示成 `··`，那是 markdown 的换行标记，真实内容里就是换行）。

`--raw` 会把每个账号的接口往返都附在 JSON 里，敏感字段被替换成 `<前6>***<后4>(len=N)`。
账号自带的渠道凭据（`notify` 里的 webhook / key / 邮箱授权码）属于内部字段，
**任何输出里都不会出现**，只会以「自带 / 借全局」的形式出现。

改完代码先跑 `test_offline.py` 再提交：它用本机假服务端把「签到 → 派猫 → 按人分发」整条链路
真跑一遍，并断言「谁的内容发到了哪个地址」，能挡住「看起来对、其实把 A 的结果发给了 B」这类错。

---

## 与参考实现 `88lin/workbuddy-auto-signin` 的对比

同源参考：<https://github.com/88lin/workbuddy-auto-signin>（MIT，签到接口同样逆向自桌面端）。
已核实它用的签到路径与本项目**完全一致**，是独立的第二份证据。

### 采纳的改进

| 改进 | 原因 |
| --- | --- |
| `claim` 带 `record_id` | 实测空 body 与带 `record_id` 行为一致，但显式契约更稳 |
| 状态用真实字面量 `arrived/idle/traveling` | 只靠 `arrive_at` 数值推断不够，字面量优先更准，数值留作兜底 |
| 退避按失败类型分桶 | 网络不可达 `(5,20,45)`、5xx `(3,10)`；4xx 不重试 |
| 签到领取接口允许重试 | 它幂等：当天重复领取只返回「已签」，不会重复发积分 |
| 4xx 业务拒绝 ≠ 硬故障 | 「今日名额已用完」是每天的常态，算成故障会让通知天天报警 |
| 整轮时间预算 | 防止 Actions 的 `timeout-minutes` 把进程强杀，导致连通知都发不出去 |

### 没采纳的部分

- **成长中心的全套**（接任务、开盲盒、能量、断登补签）。接口实测存在，但本次范围是「签到 + 派猫猫」。
- **本机系统定时（任务计划 / launchd）**。本项目的前提是「没有常开设备」，本机方案不成立。

### 它没有、本项目补上的部分

参考仓库 README 明确写着 macOS 加密凭据解密「尚待客户端实机验证」。
本项目在 macOS 上**实测跑通**了 `$wbEncrypted` 信封解密（`sym-v1 / suite 1`）。

---

## token 会过期，怎么办

token 有效期约 28 天。过期后该账号签到段会返回 `result=AUTH`，卡片上会点名是哪个账号。

**在本机重新登录该账号后**，重跑一次刷新：

```bash
python3 scripts/export_token.py --as "那个账号的名字" --check --repo <owner/name> --push
```

多账号时**只需刷新过期的那个**（同名会覆盖，别的账号不受影响）。
建议每月顺手跑一次 `--list` 看还剩多少天。

---

## 目录

```
workbuddy-daily/
├── .github/workflows/daily.yml   # 定时任务：解释器路径用 command -v 动态取
├── scripts/
│   ├── wb_auth.py                # 本机登录态解密（只有刷新脚本用得到）
│   ├── export_token.py           # 刷新脚本：本机 → 账号清单 → 仓库 secret（含逐人通知配置）
│   ├── daily.py                  # 云端每天跑这个（多账号 + 逐人/全局两级通知 + cron 档位与延迟自证）
│   ├── preview_push.py           # 只打印推送长什么样（不联网、不发消息，改排版时用）
│   ├── test_notify.py            # 通知渠道自检（真的发一条，失败给出原因）+ 路由体检
│   └── test_offline.py           # 离线全链路自测（假服务端，验证「谁发给谁」）
├── accounts.example.json         # 账号清单格式示例（可提交）
├── accounts.local.json           # 你的真实清单（自动生成，600 权限，已在 .gitignore）
├── .gitignore
└── README.md
```

脚本只用 Python 标准库，Actions 里不需要 `pip install`（邮件走 `smtplib`，
其余渠道走 `urllib`，都不依赖第三方包）。
