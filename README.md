# Buddy 加油站 · 每日签到 + 派猫（GitHub Actions 版）

每天定时跑一次，一次做完两件事：**加油站签到** + **猫猫旅行**；结果推送到飞书。

跑在 GitHub 的免费云端 runner 上，**不需要你有一台常开设备**。

---

## 它到底做了什么

一次运行按顺序做：

1. **签到**：先查活动状态 → 没签就领，签过了就跳过（幂等，重复跑不会多领）。
2. **猫猫旅行**：
   - ① 先领掉**已经到家**那一趟的旅行积分；
   - ② 再判断今天还能不能派新的一趟——`daily_limit_reached` 为真（今天已经派过）就不派；有闲名额才派。
3. **推送飞书**：签到和猫猫分成两个分区写清楚，互不混淆。
4. **隔离**：猫猫整段用 `try` 兜住，它怎么炸都不改签到结论。**签到成功即退出码 0**。

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
| 领取到家积分 | POST | `/v2/activity/growth/buddy/travel/claim` |
| 派出猫猫 | POST | `/v2/activity/growth/buddy/travel/depart`（body 需要 `location_id`） |
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

## 一次性部署（5 步）

### 1. 建仓库并推代码

```bash
git init && git add -A && git commit -m "Buddy 加油站每日任务"
git remote add origin git@github.com:<你的账号>/<仓库名>.git
git push -u origin main
```

### 2. 准备飞书机器人

飞书群 → 设置 → 群机器人 → 添加「自定义机器人」→ 复制 Webhook 地址。
如果开了「签名校验」，把签名密钥也留着。

### 3. 注入凭证（在本机跑刷新脚本）

云端 runner 读不到你本机的登录态，所以由本机把 token 解密出来写进仓库 secret：

```bash
gh auth login                                    # 只需一次
python3 scripts/export_token.py --repo <owner/name> --check
```

它会写入 `WB_TOKEN` / `WB_UID` / `WB_DOMAIN` 三个 secret。
**明文 token 全程只走 `gh` 的 stdin，不落盘、不进仓库、不进日志。**

### 4. 注入飞书 Webhook

```bash
gh secret set FEISHU_WEBHOOK --repo <owner/name>     # 粘贴 Webhook 地址
gh secret set FEISHU_SECRET  --repo <owner/name>     # 只有开了签名校验才需要
```

### 5. 手动跑一次验收

仓库 → Actions → 选「Buddy 加油站每日任务」→ Run workflow。
本地想先看效果可以用 `python3 scripts/daily.py --local --dry-run`。

---

## secret 清单

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `WB_TOKEN` | ✅ | 解密后的 accessToken |
| `WB_UID` | ✅ | 账号 uid，接口要求 `X-User-Id` |
| `WB_DOMAIN` | ➖ | 登录域，多租户才需要 |
| `FEISHU_WEBHOOK` | ✅ | 飞书自定义机器人 Webhook |
| `FEISHU_SECRET` | ➖ | 开了签名校验才需要 |
| `WECOM_WEBHOOK` | ➖ | 顺带支持企业微信 |
| `SMTP_HOST` 等 | ➖ | 顺带支持邮件 |

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
兜底办法任选一个：每两个月随手 commit 一次；或把 `workflow_dispatch` 当手动补跑；
或改用本机系统定时（Windows 任务计划 / macOS launchd）。

**② 一天只跑一次，猫猫的礼物会压到第二天才到账。**
猫猫出去一趟 1~4 小时才回来，而「领礼物」这件事只有下一次运行才做。
00:20 跑 → 当天 01:20~04:20 猫回来 → 直到次日 00:20 才领，「延迟到账」约 20 小时。

这不影响「每天领到」，只是到账慢。如果你想要更快，把 cron 改成一天多跑几次：

```yaml
on:
  schedule:
    - cron: "20 */4 * * *"   # 每 4 小时一次，北京时间 0/4/8/12/16/20 点各一次
```

**多跑是安全的**：签到幂等（已签只会返回「已签」，不会重复发积分），
派猫由服务端的 `daily_limit_reached` 把关（今天派过就不再派），
所以一天跑七次和跑一次的效果一样，只是礼物到账更快、偶发漏跑也能被下一轮补上。

> 代价：免费额度是每月 2000 分钟（公开仓库不计），一天 6 次约 6~12 分钟/月，
> 完全够用。

---

## 与参考实现 `88lin/workbuddy-auto-signin` 的对比

同源参考：<https://github.com/88lin/workbuddy-auto-signin>（MIT，签到接口同样逆向自桌面端）。
已核实它用的签到路径与本项目**完全一致**，独立的第二份证据。

### 采纳的改进

| 改进 | 原因 |
| --- | --- |
| `claim` 带 `record_id` | 实测空 body 与带 `record_id` 今天行为一致，但显式契约更稳 |
| 状态用真实字面量 `arrived/idle/traveling` | 之前只靠 `arrive_at` 数值推断，字面量优先更准，数值留作兜底 |
| 退避按失败类型分桶 | 网络不可达用长退避 `(5,20,45)`，5xx 用短退避 `(3,10)`；4xx 不重试 |
| 签到领取接口允许重试 | 它幂等：当天重复领取只返回「已签」，不会重复发积分 |
| 4xx 业务拒绝 ≠ 硬故障 | 「今日名额已用完」是每天的常态，算成故障会让飞书天天报警 |
| 整轮时间预算（默认 420s） | 防止 Actions 的 `timeout-minutes` 把进程强杀，导致连通知都发不出去 |

### 没采纳的部分

- **成长中心的全套**（接任务 `/v2/activity/growth/tasks`、开盲盒 `/v2/activity/growth/buddy/open`、
  能量 `/v2/activity/growth/buddy/quota`、断登补签、连登兑换）。这几个接口我都实测存在
  （`quota` 返回 `balance=8`，`open` 返回 `insufficient energy`），但你这次只要「签到 + 派猫猫」，
  没往范围外扩。
- **本机系统定时（任务计划 / launchd）**。你的硬约束是「没有常开设备」，本机方案直接不成立。

### 它没有、本项目补上的部分

参考仓库 README 明确写着 macOS 加密凭据解密「尚待客户端实机验证」。
本项目在 macOS 上**实测跑通**了 `$wbEncrypted` 信封解密（`sym-v1 / suite 1`），
并验证了完整的签到 + 派猫链路。

---

## token 会过期，怎么办

截图里的 token 剩余约 28 天。过期后 Actions 会报鉴权失败（签到段 `result=AUTH`），
这时**在本机重跑一次**：

```bash
python3 scripts/export_token.py --repo <owner/name> --check
```

建议每月顺手跑一次。想验证当前 token 还有多久：

```bash
python3 scripts/export_token.py --show
```

---

## 本地调试

```bash
python3 scripts/daily.py --local --raw     # 读本机登录态 + 打印脱敏原始返回
python3 scripts/daily.py --local --dry-run # 只查状态，不做写操作
python3 scripts/daily.py --no-notify       # 不推送，只看结论
```

## 目录

```
workbuddy-daily/
├── .github/workflows/daily.yml   # 定时任务：解释器路径用 command -v 动态取
├── scripts/
│   ├── wb_auth.py                # 本机登录态解密（只有刷新脚本用得到）
│   ├── export_token.py           # 刷新脚本：本机导出 token → 仓库 secret
│   └── daily.py                  # 云端每天跑这个
├── .gitignore
└── README.md
```

脚本只用 Python 标准库，Actions 里不需要 `pip install`。
