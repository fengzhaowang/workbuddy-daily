# Buddy 加油站 · 每日签到 + 派猫（GitHub Actions · 多账号版）

每天定时跑一次，把所有账号一次做完两件事：**加油站签到** + **猫猫旅行**；结果推送到飞书。

跑在 GitHub 的免费云端 runner 上，**不需要你有一台常开设备**，可以多人共用同一个仓库。

---

## 它到底做了什么

一次运行按顺序处理**每一个账号**：

1. **签到**：先查活动状态 → 没签就领，签过了就跳过（幂等，重复跑不会多领）。
2. **猫猫旅行**：
   - ① 先领掉**已经到家**那一趟的旅行积分；
   - ② 再判断今天还能不能派新的一趟——`daily_limit_reached` 为真（今天已经派过）就不派。
3. **推送飞书**：默认所有账号汇总成**一张卡片**，每个账号一个分区，区内「🏠 签到」「🐾 猫猫」分行写清；
   某个账号如果配了专属 webhook，则**单独发给他自己的群**。
4. **隔离**：
   - **账号之间互相隔离**——A 的 token 过期，B 照常签到；
   - **账号内部**，猫猫段被 `try` 兜住——它怎么炸都不改该账号的签到结论。

退出码：所有账号签到成功 → `0`；加 `--allow-partial`（或 `WB_ALLOW_PARTIAL=1`）则「至少一个成功」→ `0`。
**猫猫段一律不影响退出码。**

---

## 快速开始

### 1. 准备飞书机器人

飞书群 → 设置 → 群机器人 → 添加机器人 → **自定义机器人** → 复制 Webhook 地址。

**安全设置三选一，选错了必失败：**

| 设置 | 能不能用 | 说明 |
| --- | --- | --- |
| **签名校验** | ✅ 推荐 | 把密钥填进 secret `FEISHU_SECRET` |
| **自定义关键词** | ✅ 可以 | 关键词填 **`加油站`**（卡片标题里就有这三个字） |
| ~~IP 白名单~~ | ❌ **千万别选** | GitHub runner 的出口 IP 是动态的，必然被拒 |

拿到地址后**先自检一次**，别等 Actions 跑失败才发现配错了：

```bash
python3 scripts/test_feishu.py --webhook '<你的 Webhook 地址>'
python3 scripts/test_feishu.py --webhook '<你的 Webhook 地址>' --secret '<签名密钥>'   # 开了签名校验
python3 scripts/test_feishu.py --webhook '<你的 Webhook 地址>' --accounts 3            # 预览多账号卡片
```

它发的是**真实的日报卡片**（和 `daily.py` 用同一个构造函数，所以自检能通、线上一定能通），
失败时会直接告诉你原因（签名不对／关键词不匹配／IP 白名单）。

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
gh secret set FEISHU_WEBHOOK --repo <owner/name>                # 粘贴 Webhook
gh secret set FEISHU_SECRET  --repo <owner/name>                # 开了签名校验才需要
```

**方式 B：没有 gh（或不想登录）**——手动粘贴

```bash
python3 scripts/export_token.py --print-json
```

把输出的整段 JSON 复制到：仓库 → Settings → Secrets and variables → Actions → New repository secret，
名字填 **`WB_ACCOUNTS`**。飞书的两个 secret 同样在网页上手工加。

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

清单就是一个 JSON 数组，**一个 secret 装 N 个账号**：

```json
{
  "accounts": [
    {"name": "我",     "token": "eyJ...", "uid": "xxx", "domain": "www.workbuddy.cn"},
    {"name": "小号",   "token": "eyJ...", "uid": "yyy", "domain": "www.workbuddy.cn"},
    {"name": "同事A",  "token": "eyJ...", "uid": "zzz", "domain": "www.workbuddy.cn",
     "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"}
  ]
}
```

字段尽量宽容（`token`/`access_token`、`uid`/`user_id` 都认）；完整示例见 `accounts.example.json`。

**加账号的三种方式：**

```bash
# ① 在这台机器上登录了那个账号 → 直接抓本机登录态
python3 scripts/export_token.py --as "小号"

# ② 对方把自己的 token 给你（一个 JSON 文件），合并进来
python3 scripts/export_token.py --import 同事给的.json

# ③ 手工编辑 accounts.local.json
```

**移除账号：** `python3 scripts/export_token.py --remove "小号"`

### 每人收自己的通知

给某个账号填上他自己的 `webhook`，**他那一份就单独发到他自己的群**，不混进大卡片：

```
{"name":"同事A", "token":"...", "uid":"...", "webhook":"https://open.feishu.cn/..."}
```

没填 `webhook` 的账号，全部汇总到全局 `FEISHU_WEBHOOK` 那一张卡片里。
（这样「同事们各自建群、各自收自己的结果」和「一个大群看全量」可以同时成立。）

### 各账号怎么拿 token

每个账号的 token 来自**那个账号在客户端登录过的机器**：

- 同一个人的多个账号：在本机依次登录并刷新（或各跑一次 `--as`）；
- 别人的账号：让他按上面方式导出后把 JSON 给你，你用 `--import` 合并。

`--add-local` 按**名字**合并，同名视为同一个账号会被覆盖，所以换机器重跑不会产生重复条目。

---

## secret 清单

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `WB_ACCOUNTS` | ✅（推荐） | 账号清单 JSON 数组，一个 secret 装 N 个账号 |
| `WB_TOKEN` / `WB_UID` / `WB_DOMAIN` | ➖ | 旧版单账号写法，**仍然兼容**；同时配了清单则以清单为准 |
| `FEISHU_WEBHOOK` | ✅ | 飞书自定义机器人 Webhook（汇总卡片发这里） |
| `FEISHU_SECRET` | ➖ | 机器人开了「签名校验」才需要 |
| `WECOM_WEBHOOK` | ➖ | 顺带支持企业微信 |
| `SMTP_HOST` 等 | ➖ | 顺带支持邮件 |

可选环境变量：`WB_ENV`（卡片上显示的环境名）、`WB_BUDGET_SECONDS`（整轮时间预算）、
`WB_ALLOW_PARTIAL=1`（有账号失败也不算整轮失败）、`WB_ENDPOINT`（自建/私有化端点）。

---

## 定时

`.github/workflows/daily.yml` 里：

```yaml
on:
  schedule:
    - cron: "20 16 * * *"   # UTC 16:20 = 北京时间次日 00:20
```

**cron 是 UTC 时间**，换算规则：`北京时间 = UTC + 8`。
GitHub 的定时任务在高峰时段可能延迟几分钟到几十分钟，属正常现象。

### 两个必须知道的坑

**① 仓库连续 60 天没有新提交，GitHub 会自动停掉 schedule。**
这是一个「只为跑定时任务而生」的仓库最容易踩的坑——某天你会发现它几个月没跑过。
兜底办法任选一个：每两个月随手 commit 一次；或把 `workflow_dispatch` 当手动补跑。

**② 一天只跑一次，猫猫的礼物会压到第二天才到账。**
猫猫出去一趟 1~4 小时才回来，而「领礼物」这件事只有下一次运行才做，
延迟约 20 小时。想更快就把 cron 改成一天多跑几次：

```yaml
on:
  schedule:
    - cron: "20 */4 * * *"   # 每 4 小时一次
```

**多跑是安全的**：签到幂等（已签只会返回「已签」，不会重复发积分），
派猫由服务端的 `daily_limit_reached` 把关（今天派过就不再派），
所以一天跑七次和跑一次的效果一样，只是礼物到账更快、偶发漏跑也能被下一轮补上。

**账号数的两个限制：**时间预算按 `420 + 150 × (账号数 − 1)` 秒自动放大，
`daily.yml` 里 `timeout-minutes: 20` 要相应调大；账号特别多（十个以上）建议拆成多个 workflow，
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
python3 scripts/daily.py --list-accounts              # 只列出识别到的账号（脱敏）
python3 scripts/daily.py --only "我,同事A"             # 只跑指定账号（名字或序号）
python3 scripts/daily.py --local --raw                # 附上脱敏后的原始返回
python3 scripts/daily.py --local --dry-run            # 只查状态，不做写操作
python3 scripts/daily.py --local --no-notify          # 不推送，只看结论
```

`--raw` 会把每个账号的接口往返都附在 JSON 里，敏感字段被替换成 `<前6>***<后4>(len=N)`。

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
| 4xx 业务拒绝 ≠ 硬故障 | 「今日名额已用完」是每天的常态，算成故障会让飞书天天报警 |
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
│   ├── export_token.py           # 刷新脚本：本机 → 账号清单 → 仓库 secret
│   ├── daily.py                  # 云端每天跑这个（多账号）
│   └── test_feishu.py            # 飞书机器人自检（发一条真实卡片）
├── accounts.example.json         # 账号清单格式示例（可提交）
├── accounts.local.json           # 你的真实清单（自动生成，600 权限，已在 .gitignore）
├── .gitignore
└── README.md
```

脚本只用 Python 标准库，Actions 里不需要 `pip install`。
