#!/usr/bin/env python3
"""WorkBuddy「Buddy 加油站」每日一体化任务（多账号版）。

一次跑完所有账号，每个账号做两件事：
  1. 签到：先查活动状态，未签才领（幂等，重复跑不会多领）。
  2. 猫猫旅行：先领掉「已到家」那一趟的旅行积分，再判断今天还能不能派新的一趟；
     今天已经派过（daily_limit_reached）就不派。

结果推送分两层，**可以逐人定制**：

  * **每个人自己的渠道**：账号清单里给某个账号写 `notify`，他的那一份就只发到
    他自己指定的渠道（可以多个），不混进汇总卡片。
    凭据可以自带（他自己的机器人/邮箱/微信推送），也可以只写渠道名去借全局 secret。
  * **没人认领的账号**：汇总成**一份**，每个账号一个分区、区内「🏠 签到」与
    「🐾 猫猫」分行写清，再发到全局配置的渠道。

通知渠道可插拔，支持 9 种，**配了哪个就发哪个**（配几个发几个）：
  飞书 / 企业微信 / 钉钉 / Server酱 / PushPlus / Bark / ntfy / Telegram / 邮件
用 NOTIFY_CHANNELS 点名可以只发其中几个（例：NOTIFY_CHANNELS=dingtalk,email），
它只管「全局那一层」。一个渠道都没配不会报错，只是在结果里提示一句。
某个渠道失败只写进 notices，既不影响退出码，也不影响别的渠道。

隔离层级（重要）：
  * 账号之间互相隔离——A 的 token 过期不影响 B 照常签到；
  * 账号内部，猫猫段被 try 兜住——它怎么炸都不改该账号的签到结论。

退出码：所有账号签到成功 => 0；加 --allow-partial 则「至少一个成功」=> 0。
猫猫段一律不影响退出码（需求：签到成功就算成功）。
例外：--mode cat（旧写法 --cat-only）时签到段跳过，此时退出码看猫猫段。

两类定时（workflow 里用不同 cron 触发），跑哪一段由**触发的 cron** 决定：
  * 每天一次「整轮」：签到 + 猫猫；
  * 其余每 6 小时「只收猫」：只跑猫猫段。
    因为 do_cat() 本身是「先领已到家的积分、再该派就派新的一趟」的闭环，
    收猫轮次领完会立刻再派一趟，积分才转得起来；签到一天一次就够，不必重复。
  cron -> 档位 的映射表只有一处（下面 SCHEDULE_MODES），workflow 里不再复制一份，
  test_offline.py 会读 workflow 文件逐条核对，改了 cron 忘了改档位会当场失败。

推送的另一条规则：**只推「有变化」的**（默认 onchange，逐账号判定）。
  只报三件真事：签到领到积分、派出了新的一趟、领到旅行积分；以及任何出错。
  以下情形**不推**——它们都是「今天已经有人做过了」，重复报只会让真正的异常被淹掉：
    * 今天已经签过（签到段 ALREADY）；
    * 今天已经派过猫猫、猫还在路上、或今天名额已用完（LIMIT / TRAVELING）；
    * 已到家的积分已经领过、眼下没有待领的（IDLE，以及领完后回落的 LIMIT）；
    * 本轮有意跳过的段（收猫轮不跑签到 = SKIPPED）、dry-run。
  逐人判、逐人裁：有自己渠道的人只在自己有变化时才收到自己那一份；
  汇总卡片里也只列有变化的人。一轮下来谁都没变化，就整轮静默（日志与结果 JSON 里
  会写清「为什么没推」，Actions 页面还有本轮摘要，不怕看不出它跑没跑）。
  想每轮都推（比如自己手动触发时想立刻看到结果）就加 --notify-mode always。

关于「几点触发的」：GitHub 的 schedule 是**尽力而为**——官方文档写明高峰期会延迟
（整点最挤），实测有晚几分钟到好几小时的。所以脚本会把「计划 / 实际 / 延迟」算出来
写进 stderr、body.cron 与 notices；晚超过 LATE_MINUTES 还会在推送正文末尾附一句说明。

凭证来源（优先级从高到低）：
  1. --local                  本机登录态（调试用，单账号）
  2. --accounts <文件>         本机账号清单 JSON（调试多账号）
  3. WB_ACCOUNTS              环境变量，JSON 数组（云端推荐）
  4. WB_TOKEN / WB_UID        旧版单账号环境变量（向后兼容）

明文 token 只从上述来源读入，不打印、不落盘；所有输出经脱敏。
注意：账号自带的渠道凭据写在清单里（= WB_ACCOUNTS 这个 secret 内），
所以它跟 token 一样不能进仓库、不进日志；报错信息里只报「缺哪个字段」。

用法：
  python3 scripts/daily.py                          # 云端：按触发的 cron 自动定档位
  python3 scripts/daily.py --mode cat               # 只收猫（领积分 + 该派就派）
  python3 scripts/daily.py --cat-only               # 同上，旧写法
  python3 scripts/daily.py --notify-mode always     # 每轮都推，不看有没有变化
  python3 scripts/daily.py --notify-mode onchange   # 默认：只在有变化时推（逐人裁）
  python3 scripts/daily.py --local                  # 本机单账号
  python3 scripts/daily.py --accounts accounts.local.json   # 本机多账号
  python3 scripts/daily.py --list-accounts          # 只列出识别到的账号（脱敏 + 渠道）
  python3 scripts/daily.py --only "我的账号,小号"    # 只跑指定账号（名字或序号）
  python3 scripts/daily.py --local --raw            # 附上脱敏后的原始返回
  python3 scripts/daily.py --dry-run                # 只查状态，不做写操作
  python3 scripts/daily.py --no-notify              # 不推送，只看结论

自检脚本：
  python3 scripts/test_offline.py    # 本机假服务端跑通全链路（含 cron 档位与延迟算例）
  python3 scripts/test_notify.py     # 真发一条通知，验证渠道配置
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ENDPOINT_DEFAULT = "https://copilot.tencent.com"
TIMEOUT = 30

# 退避节奏按失败类型分开（参考 88lin/workbuddy-auto-signin 的做法）：
#   * 网络不可达（-1）：刚开机/唤醒时网络要几十秒才就绪，5 秒一次的重试几乎无效；
#   * 服务端 5xx：抖动，短退避多试几次就够。
# 4xx 一律不重试——业务规则和参数问题，重试一百次是同一个答案。
NETWORK_RETRY_DELAYS = (5, 20, 45)
SERVER_RETRY_DELAYS = (3, 10)

# 整轮时间预算：留足余量，保证「推送飞书」这一步一定能执行到。
# 否则 Actions 的 timeout-minutes 把进程强杀，连通知都发不出去。
# 账号数越多预算越大（每个账号最坏要走 6 个请求）。
BUDGET_BASE_SECONDS = 420.0
BUDGET_PER_EXTRA_ACCOUNT = 150.0
_started_at = time.monotonic()
_BUDGET: float = float(os.environ.get("WB_BUDGET_SECONDS") or BUDGET_BASE_SECONDS)

# ================= 定时档位（唯一真相）=================
# 「触发的 cron」-> 本轮跑哪一段。**必须与 .github/workflows/daily.yml 里的 cron
# 逐字一致**，scripts/test_offline.py 会直接读那个文件逐条核对：
# 改了一处忘了另一处，以前是「看起来正常、就是没积分」的静默错，现在当场失败。
SCHEDULE_MODES = {
    "20 0 * * *": "all",             # 北京 00:20        整轮：签到 + 派猫猫
    "20 6,12,18 * * *": "cat",       # 北京 06/12/18:20  只收猫：领已到家的积分 + 该派就派
}
# cron 的时区。**必须与 workflow 里 schedule 的 timezone 一致**：
# 2026-03-19 起 GitHub 支持在 cron 旁边写 IANA 时区，workflow 用的是 Asia/Shanghai，
# 所以上面那几条 cron 是**北京时间**，不是 UTC。算「计划几点、晚了多久」也得按这个时区来，
# 否则会算出一个 8 小时的假延迟（或者负数）。
CRON_TIMEZONE = "Asia/Shanghai"
CRON_TZ_OFFSET_HOURS = 8
CRON_TZ = timezone(timedelta(hours=CRON_TZ_OFFSET_HOURS))
LATE_MINUTES = 30                    # 比计划晚这么多分钟，就在结果与推送里说明原因

UNKNOWN_CRON_WARNING = (
    "触发的 cron「%s」不在已知列表里：本轮按整轮（签到+猫猫）跑——安全的那一边。"
    "若你刚在 workflow 里改过 cron，请同步 daily.py 的 SCHEDULE_MODES，"
    "否则收猫那一档会悄悄变成整轮（不会少领积分，但会多跑一段签到、推送也更吵）。")

# 「晚了整整 8 小时」= 时区没对齐的特征。
# 这类错只会污染「延迟几分钟」这一个数字，功能全对，所以它能长期藏在那里；
# 而真·GitHub 延迟也要好几个小时，两者只能靠「正好卡一整个 8 小时」来区分。
TZ_MISMATCH_WARNING = (
    "本轮延迟 %s 分钟，正好是一个 ±%d 小时的整偏移：这更像是**时区没对齐**"
    "（daily.yml 里 schedule 的 timezone 与 daily.py 的 CRON_TIMEZONE=%s 不一致），"
    "而不是真晚了这么久——GitHub 的高峰延迟是几分钟到几小时不等，"
    "正好卡着一整个 8 小时的概率极低。请核对这两处是不是同一个时区。")

# GitHub 的 schedule 是**尽力而为**：官方文档写明高峰期会延迟（整点最挤），
# 实测有晚几分钟到好几小时的，甚至整轮被丢弃。所以「几点触发的」不该靠猜，
# 下面几个函数把它算清楚写进日志、notices 与结果 JSON。


def _cron_hm(cron: str) -> Optional[tuple[list[int], list[int]]]:
    """极简 cron 解析：只认我们在用的 "M H[,H…] * * *" 形态，其余一律返回 None。

    刻意不引 cron 库：这里只需要「最近一次计划时间」，而
    「认不出来就明说认不出来」比「假装支持全套语法」安全得多。
    """
    parts = (cron or "").split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        return None
    try:
        mins = sorted({int(x) for x in parts[0].split(",")})
        hrs = sorted({int(x) for x in parts[1].split(",")})
    except ValueError:
        return None
    if not mins or not hrs:
        return None
    if any(not 0 <= m <= 59 for m in mins) or any(not 0 <= h <= 23 for h in hrs):
        return None
    return mins, hrs


def cron_slot(cron: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """这条 cron 最近一次「本该触发」的时刻（带 CRON_TZ 时区）；认不出来返回 None。

    注意时区：cron 里的 `20 0 * * *` 是**北京时间** 00:20（workflow 里写了
    `timezone: Asia/Shanghai`），不是 UTC 00:20。这里必须先换算到 CRON_TZ 再比，
    否则「最近一次计划时刻」会差 8 小时，延迟算出来是假的（甚至负数）。
    """
    hm = _cron_hm(cron)
    if hm is None:
        return None
    mins, hrs = hm
    now_local = (now or datetime.now(timezone.utc)).astimezone(CRON_TZ)
    for back in (0, 1):                       # 今天找不到就看昨天（跨零点的档）
        day = (now_local - timedelta(days=back)).date()
        due = [datetime(day.year, day.month, day.day, h, m, tzinfo=CRON_TZ)
               for h in hrs for m in mins
               if datetime(day.year, day.month, day.day, h, m, tzinfo=CRON_TZ) <= now_local]
        if due:
            return max(due)
    return None


def _local_hhmm(dt: datetime) -> str:
    """按 cron 的时区显示时刻——就是收到通知的人手机上那个时间。"""
    return dt.astimezone(CRON_TZ).strftime("%m-%d %H:%M")


def cron_report(trigger: str, now: Optional[datetime] = None) -> dict:
    """算清「计划几点 vs 实际几点、晚了多久」，供日志 / notices / 结果 JSON 用。

    为什么要写成一件正经事：不把这件事写进结果，人只会看到
    「我明明设的 00:20，怎么凌晨 5 点才发通知」，然后去怀疑自己的配置
    —— 而根因其实在 GitHub 的定时器。
    """
    now = now or datetime.now(timezone.utc)
    rep: dict = {
        "timezone": CRON_TIMEZONE,
        "triggered_cron": trigger or "",
        "plan": SCHEDULE_MODES.get(trigger or ""),
        "actual_utc": now.astimezone(timezone.utc).strftime("%m-%d %H:%M"),
        "actual_local": _local_hhmm(now),
        "delay_minutes": None,
    }
    if not trigger:
        rep["reason"] = "手动触发（workflow_dispatch）：没有计划时间，不存在延迟"
        return rep

    slot = cron_slot(trigger, now)
    if slot is None:
        rep["reason"] = "这条 cron 认不出来，算不出计划时间"
        rep["warning"] = UNKNOWN_CRON_WARNING % trigger
        return rep

    delay = int((now - slot).total_seconds() // 60)
    rep.update({"planned_utc": slot.astimezone(timezone.utc).strftime("%m-%d %H:%M"),
                "planned_local": _local_hhmm(slot),
                "delay_minutes": delay})
    offset = CRON_TZ_OFFSET_HOURS * 60
    if abs(abs(delay) - offset) <= 2:
        rep["warning"] = TZ_MISMATCH_WARNING % (delay, CRON_TZ_OFFSET_HOURS, CRON_TIMEZONE)
    elif rep["plan"] is None:
        rep["warning"] = UNKNOWN_CRON_WARNING % trigger
    return rep


def cron_line(rep: dict) -> str:
    """给 stderr 一行话。Actions 日志里应当一眼看到「计划 -> 实际，晚了多少」。"""
    if not rep.get("triggered_cron"):
        return "本轮为手动触发，无计划时间"
    if rep.get("planned_utc") is None:
        return "触发 cron='%s'，认不出它的计划时间（详见 notices）" % rep["triggered_cron"]
    return ("触发 cron='%s' -> 计划 %sZ（北京 %s），实际 %sZ（北京 %s），延迟 %s 分钟"
            % (rep["triggered_cron"], rep["planned_utc"], rep["planned_local"],
               rep["actual_utc"], rep["actual_local"], rep["delay_minutes"]))


def late_note(rep: dict) -> str:
    """晚得太久时给人一句能看懂的话（进 notices，也附在推送正文末尾）。"""
    d = rep.get("delay_minutes")
    if d is None or d < LATE_MINUTES:
        return ""
    took = ("%d 小时 %d 分" % (d // 60, d % 60)) if d >= 60 else ("%d 分钟" % d)
    return ("⏱ 本轮是 GitHub 定时器延迟触发的：计划 UTC %s（北京 %s），"
            "实际 UTC %s（北京 %s），晚了 %s。"
            "GitHub 的 cron 只是「尽力而为」（官方文档：高峰期会延迟，整点最挤），"
            "晚几分钟到几小时都属正常，不是配置写错了。"
            % (rep.get("planned_utc"), rep.get("planned_local"),
               rep.get("actual_utc"), rep.get("actual_local"), took))

# ---------- 接口路径：全部与客户端实际请求核对通过 ----------
# 每个路径都用真实请求验证过「存在且语义正确」，不是网上抄的旧名字。
#   实测：伪造路径 -> 404 route not found；下列路径 -> 200 / 业务 4xx。
P_CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"   # POST {} -> 活动状态
P_CHECKIN_CLAIM = "/v2/billing/meter/daily-checkin"              # POST {} -> 领签到积分
P_TRAVEL_STATUS = "/v2/activity/growth/buddy/travel/status"      # GET      -> 旅行状态
P_TRAVEL_CONFIG = "/v2/activity/growth/buddy/travel/config"      # GET      -> 可选目的地
P_TRAVEL_CLAIM = "/v2/activity/growth/buddy/travel/claim"        # POST {record_id}
P_TRAVEL_DEPART = "/v2/activity/growth/buddy/travel/depart"      # POST {location_id}
P_BUDDY_INFO = "/v2/activity/growth/buddy/info"                  # GET      -> 猫咪资料

# 服务端约定：HTTP 200 且 body.code == 0 才算业务成功；其余走 code/msg。
CODE_ALREADY_CHECKED_IN = 10001   # 实测「今天已签到，请明天再来」

# ---------- 脱敏 ----------
# 只盖真敏感字段。刻意不含 `name`：目的地/猫咪名字不算敏感，盖掉反而看不清结论。
SENSITIVE_KEYS = re.compile(
    r"token|secret|passwd|password|phone|mobile|openid|unionid|uin|"
    r"email|mail|session|avatar|nickname",
    re.I,
)


def redact(value: Optional[str]) -> str:
    """压成 <前6>***<后4>(len=N)，用于日志。"""
    if not value:
        return "<empty>"
    if len(value) <= 12:
        return "*** (len=%d)" % len(value)
    return "%s***%s (len=%d)" % (value[:6], value[-4:], len(value))


def scrub(obj: Any, _depth: int = 0) -> Any:
    """递归脱敏，把原始返回安全地贴出来。"""
    if _depth > 12:
        return "<too-deep>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and SENSITIVE_KEYS.search(str(k)):
                out[k] = redact(v)
            else:
                out[k] = scrub(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [scrub(v, _depth + 1) for v in obj]
    if isinstance(obj, str) and len(obj) > 400:
        return obj[:400] + "...(truncated,len=%d)" % len(obj)
    return obj


def data_of(body: Any) -> dict:
    """取 {code,msg,data} 信封里的 data。"""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return {}


def msg_of(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("msg") or body.get("message") or "").strip()
    return ""


def is_ok(code: int, body: Any) -> bool:
    """HTTP 2xx 且信封 code==0 才算成功。"""
    return 200 <= code < 300 and isinstance(body, dict) and body.get("code") == 0


# ================= 账号清单 =================
# 兼容多种字段写法，尽量让人少踩坑：token/access_token、uid/user_id……
# notify 的细节（渠道名、字段别名）在下面「推送」一节，那里与渠道规格表放在一起。
_FIELD_ALIASES = {
    "name": ("name", "alias", "label", "账号", "备注"),
    "token": ("token", "access_token", "accessToken", "wb_token"),
    "uid": ("uid", "user_id", "userId", "wb_uid"),
    "domain": ("domain", "wb_domain"),
    "endpoint": ("endpoint", "base_url", "baseUrl"),
    "notify": ("notify", "channels", "push", "通知", "通知渠道"),
}

# 本机清单里的元数据字段：只在本地存在，不会进 secret，本地跑也不该报「不认识」
_LOCAL_META_KEYS = {"added_at", "expires_at", "expires_in", "note", "remark", "说明"}

# 本版代码认识的字段全集。用来做「静默忽略」检查（见 _to_account 的 unknown）。
_KNOWN_KEYS = frozenset(
    {alias for aliases in _FIELD_ALIASES.values() for alias in aliases}
    | {"webhook", "feishu_webhook", "secret", "feishu_secret"}   # 旧写法，等价 notify.feishu
    | _LOCAL_META_KEYS
)


@dataclass
class Account:
    name: str
    token: str
    uid: str = ""
    domain: str = ""
    endpoint: str = ENDPOINT_DEFAULT
    # {渠道: {字段: 值}}；空 dict 表示该渠道「借全局 secret」。
    # 整个为空 = 这个人的结果并入汇总卡片。
    notify: dict = field(default_factory=dict)
    # 清单里写了、但本版代码不认识的字段。
    # 非致命，但**必须**报出来：否则「清单升了级、云端跑的还是旧代码」会让配置
    # 被静默忽略，人只看到「我明明配了怎么没生效」。这个字段就是为那次事故加的。
    unknown: list = field(default_factory=list)


def _pick(raw: dict, key: str) -> str:
    for alias in _FIELD_ALIASES[key]:
        v = raw.get(alias)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _to_account(raw: Any, index: int, endpoint_env: str) -> Account:
    if not isinstance(raw, dict):
        raise ValueError("账号清单第 %d 项不是对象" % index)
    token = _pick(raw, "token")
    if not token:
        raise ValueError("账号清单第 %d 项缺少 token" % index)
    # 认不出来的字段要报出来，不能静默吞掉：可能是拼错，也可能是「清单比代码新」
    # （云端跑的 commit 太旧，新字段被这版代码忽略——正是这一条能提前暴露它）。
    unknown = sorted(str(k) for k in raw
                     if not str(k).startswith("_") and k not in _KNOWN_KEYS)
    return Account(
        name=_pick(raw, "name") or "账号%d" % index,
        token=token,
        uid=_pick(raw, "uid"),
        domain=_pick(raw, "domain"),
        endpoint=_pick(raw, "endpoint") or endpoint_env,
        notify=_parse_notify(raw),
        unknown=unknown,
    )


def _parse_accounts(raw: Any, endpoint_env: str) -> list[Account]:
    items = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("账号清单为空，或格式不对（应为 JSON 数组，或 {\"accounts\": [...]}）")
    return [_to_account(it, i, endpoint_env) for i, it in enumerate(items, 1)]


def _load_accounts(args: argparse.Namespace) -> list[Account]:
    endpoint_env = os.environ.get("WB_ENDPOINT", ENDPOINT_DEFAULT)

    if args.local:
        import wb_auth
        c = wb_auth.load_credentials()
        return [Account(name=args.as_name or "本机账号", token=c["token"], uid=c["uid"],
                        domain=c.get("domain", ""),
                        endpoint=c.get("endpoint") or endpoint_env)]

    if args.accounts:
        with open(args.accounts, encoding="utf-8") as fp:
            return _parse_accounts(json.load(fp), endpoint_env)

    env_json = (os.environ.get("WB_ACCOUNTS") or "").strip()
    if env_json:
        return _parse_accounts(json.loads(env_json), endpoint_env)

    # ---- 旧版单账号环境变量：继续支持，别让已有的 secret 失效 ----
    token = (os.environ.get("WB_TOKEN") or "").strip()
    if token:
        return [Account(name=os.environ.get("WB_ACCOUNT_NAME") or "默认账号",
                        token=token, uid=(os.environ.get("WB_UID") or "").strip(),
                        domain=os.environ.get("WB_DOMAIN", ""),
                        endpoint=endpoint_env)]

    raise RuntimeError(
        "未识别到任何账号。推荐设置 WB_ACCOUNTS（JSON 数组）；"
        "也兼容旧的 WB_TOKEN / WB_UID 单账号写法")


def _select(accounts: list[Account], spec: str) -> list[Account]:
    """按名字或序号筛选，支持逗号分隔。"""
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    picked, missed = [], []
    for w in wanted:
        hit = None
        for i, a in enumerate(accounts, 1):
            if w == str(i) or w == a.name:
                hit = a
                break
        if hit is None:
            missed.append(w)
        elif hit not in picked:
            picked.append(hit)
    if missed:
        raise ValueError("--only 里这些账号找不到：%s（可选：%s）"
                         % ("、".join(missed), "、".join(a.name for a in accounts)))
    return picked


def public_account(a: Account) -> dict:
    """给日志/JSON 用的脱敏视图（只报渠道名，绝不报凭据）。"""
    chans, problems = account_channels(a)
    out = {"name": a.name, "uid": redact(a.uid), "token": redact(a.token),
           "domain": a.domain or "-", "endpoint": a.endpoint,
           "channels": "、".join(CHANNEL_LABEL.get(c, c) for c in chans) or "汇总卡片"}
    if problems:
        out["channel_problems"] = problems
    return out


# ================= HTTP =================
class Api:
    """极薄 HTTP 客户端，记录每次调用以便脱敏回放。"""

    def __init__(self, endpoint: str, token: str, uid: str, domain: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.uid = uid
        self.trace: list[dict] = []
        self.headers = {
            "Accept": "application/json",
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
            "User-Agent": "WorkBuddy",
        }
        if uid:
            self.headers["X-User-Id"] = uid
        if domain:
            self.headers["X-Domain"] = domain

    def call(self, path: str, method: str = "GET", payload: Any = None,
             retry: bool = False) -> tuple[int, Any]:
        url = self.endpoint + path
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        code, data = self._once(url, method, body)
        if retry:
            # 按失败类型各自走自己的退避表：失败类型会在重试途中变化
            # （典型：冷启动时先是网络不可达，之后转成 5xx），全局计数会让后一种
            # 撞上已经用光的计数、一次都重试不到。
            buckets: dict[tuple, int] = {}
            while True:
                delays = _retry_delays(code)
                used = buckets.get(delays, 0)
                if not delays or used >= len(delays):
                    break
                if _budget_left() <= delays[used] + TIMEOUT:
                    break                      # 预算不够就别开始，宁可如实返回失败
                buckets[delays] = used + 1
                time.sleep(delays[used])
                code, data = self._once(url, method, body)
        self.trace.append({
            "method": method, "path": path,
            "request": scrub(payload) if payload is not None else None,
            "http": code, "response": scrub(data),
        })
        return code, data

    def _once(self, url: str, method: str, body: Optional[bytes]) -> tuple[int, Any]:
        req = urllib.request.Request(url, data=body, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_req_timeout(),
                                        context=ssl.create_default_context()) as resp:
                return resp.status, _json_or_raw(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return e.code, _json_or_raw(e.read().decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            return -1, {"error": str(e.reason)}
        except Exception as e:  # noqa: BLE001 - 网络层任何异常都收敛成 -1
            return -1, {"error": "%s: %s" % (type(e).__name__, e)}


def _retry_delays(code: int) -> tuple:
    """该失败码对应的退避表；空元组 = 重试也没用。"""
    if code == -1:                 # 网络不可达
        return NETWORK_RETRY_DELAYS
    if code >= 500:                # 服务端抖动
        return SERVER_RETRY_DELAYS
    return ()                      # 2xx / 4xx / 401 / 403


def set_budget(seconds: float) -> None:
    global _BUDGET
    _BUDGET = float(seconds)


def default_budget(account_count: int) -> float:
    return BUDGET_BASE_SECONDS + BUDGET_PER_EXTRA_ACCOUNT * max(0, account_count - 1)


def _budget_left() -> float:
    return _BUDGET - (time.monotonic() - _started_at)


def _req_timeout() -> float:
    """单次请求超时随剩余预算收缩，别把预算一次性用穿。"""
    return max(1.0, min(float(TIMEOUT), _budget_left()))


def _is_hard_failure(code: int) -> bool:
    """是否属于「需要人关注」的失败。

    5xx 与网络不可达算硬失败；4xx 绝大多数是业务规则（今日名额已用完、活动已结束），
    属于每天的正常状态，算成失败会让通知天天报警。
    """
    return code >= 500 or code == -1


def _json_or_raw(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw[:500]}


def _num(v: Any) -> Any:
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return v


# ================= 第 1 段：加油站签到 =================
def do_checkin(api: Api, dry_run: bool = False) -> dict:
    """签到段。

    每行文本都是**发给手机通知**看的，所以刻意写得短：
    「怎么做」这类步骤留在 README/日志里，通知里只说「发生了什么」。
    """
    code, body = api.call(P_CHECKIN_STATUS, "POST", {}, retry=True)
    if code == -1:
        return {"ok": False, "segment": "签到", "result": "NETWORK",
                "lines": ["❌ 网络不可达，没拿到签到状态（%s）" % body.get("error", "")]}
    if code in (401, 403):
        return {"ok": False, "segment": "签到", "result": "AUTH",
                "lines": ["❌ 登录态失效（HTTP %s）：该账号的 token 已过期，"
                          "需在本机重新导出" % code]}
    if not is_ok(code, body):
        return {"ok": False, "segment": "签到", "result": "ERROR",
                "lines": ["❌ 签到状态接口异常（HTTP %s%s）"
                          % (code, "：" + msg_of(body) if msg_of(body) else "")]}

    st = data_of(body)
    if st.get("active") is False:
        return {"ok": True, "segment": "签到", "result": "INACTIVE",
                "lines": ["⚪️ 签到活动未开启（%s）" % (st.get("theme_name") or "")]}

    if st.get("today_checked_in") in (True, 1):
        return {"ok": True, "segment": "签到", "result": "ALREADY",
                "credit": None, "today_credit": _num(st.get("today_credit")),
                "lines": ["✅ 今天已经签过了" + _status_tail(st)]}

    if dry_run:
        return {"ok": True, "segment": "签到", "result": "DRY_RUN",
                "credit": None, "today_credit": _num(st.get("today_credit")),
                "lines": ["🔎 dry-run：今天还没签，本次不领"]}

    # 领取接口虽然写数据，但本身幂等（当天重复领取只会返回「已签」，不会再发一次积分），
    # 所以允许重试；其余写操作一律不重试，避免超时发生在服务端已处理完之后造成重复提交。
    code2, body2 = api.call(P_CHECKIN_CLAIM, "POST", {}, retry=True)
    if code2 == -1:
        return {"ok": False, "segment": "签到", "result": "NETWORK",
                "lines": ["❌ 领取请求没送达，下次会重试"]}
    if code2 in (401, 403):
        return {"ok": False, "segment": "签到", "result": "AUTH",
                "lines": ["❌ 登录态失效（HTTP %s），签到未完成" % code2]}

    if is_ok(code2, body2):
        got = data_of(body2)
        c3, body3 = api.call(P_CHECKIN_STATUS, "POST", {}, retry=True)
        fresh = data_of(body3) if is_ok(c3, body3) else st
        credit = got.get("credit", fresh.get("today_credit"))
        return {"ok": True, "segment": "签到", "result": "CLAIMED",
                "credit": _num(credit), "today_credit": _num(fresh.get("today_credit")),
                "lines": ["✅ 签到成功 +%s 积分%s" % (_num(credit), _status_tail(fresh))]}

    if isinstance(body2, dict) and body2.get("code") == CODE_ALREADY_CHECKED_IN:
        return {"ok": True, "segment": "签到", "result": "ALREADY",
                "credit": None, "today_credit": _num(st.get("today_credit")),
                "lines": ["✅ 今天已经签过了（服务端判定已领取）" + _status_tail(st)]}

    return {"ok": False, "segment": "签到", "result": "ERROR",
            "lines": ["❌ 签到失败（HTTP %s%s）"
                      % (code2, "：" + msg_of(body2) if msg_of(body2) else "")]}


def _status_tail(st: Any) -> str:
    """把状态里的积分/连签信息拼成一句尾巴。

    用「·」而不是逗号：一行里塞三个数字时，逗号会跟中文句子糊在一起，
    分隔符能让人一眼看出这是并列的三个数。
    """
    if not isinstance(st, dict):
        return ""
    bits = []
    for label, key, unit in (("今日", "today_credit", ""), ("连续", "streak_days", " 天"),
                             ("累计", "total_credits", "")):
        v = st.get(key)
        if v is not None:
            bits.append("%s %s%s" % (label, _num(v), unit))
    return "（%s）" % " · ".join(bits) if bits else ""


# ================= 第 2 段：猫猫旅行 =================
def do_cat(api: Api, dry_run: bool = False) -> dict:
    """先领已到家的旅行积分，再看今日名额决定派不派新的一趟。

    整段被 try 兜住：任何异常都只记进本段结论，不影响签到。

    `lines` 是给人看的（会进通知正文），另外几个字段是给程序看的——
    buddy / reward / departed / location，总览行直接读它们，
    不去解析人看的文本（文案随时会改，解析文本的方案一改就坏）。
    """
    seg: dict = {"ok": True, "segment": "猫猫旅行", "result": "IDLE", "lines": []}
    try:
        code, body = api.call(P_TRAVEL_STATUS, "GET", retry=True)
        if code == -1:
            return {"ok": False, "segment": "猫猫旅行", "result": "NETWORK",
                    "lines": ["⚠️ 网络不可达，本段跳过（%s）" % body.get("error", "")]}
        if code in (401, 403):
            return {"ok": False, "segment": "猫猫旅行", "result": "AUTH",
                    "lines": ["⚠️ 登录态失效（HTTP %s），本段跳过" % code]}
        if not is_ok(code, body):
            return {"ok": False, "segment": "猫猫旅行", "result": "ERROR",
                    "lines": ["⚠️ 旅行状态接口异常（HTTP %s%s），本段跳过"
                              % (code, "：" + msg_of(body) if msg_of(body) else "")]}

        buddy = _buddy_name(api)
        if buddy:
            seg["buddy"] = buddy
            seg["lines"].append("猫咪 %s" % buddy)
        st = data_of(body)
        claimed = False

        # ---- ① 先领掉「已到家」那一趟的旅行积分 ----
        if _arrived(st):
            if dry_run:
                seg["lines"].append("🎁 有一趟已到家的积分待领（dry-run 没领）")
            else:
                # 带上 record_id：服务端目前对空 body 也接受（实测同样返回 not arrived yet），
                # 但明确的契约是认 record_id，带上更稳妥。
                rid = st.get("record_id")
                payload = {"record_id": rid} if rid is not None else {}
                c, cb = api.call(P_TRAVEL_CLAIM, "POST", payload)
                if is_ok(c, cb):
                    reward = data_of(cb).get("reward_credit", st.get("reward_credit"))
                    seg["reward"] = _num(reward)
                    seg["lines"].append("🎁 领到旅行积分 +%s" % _num(reward))
                    seg["result"] = "CLAIMED"
                    claimed = True
                    c2, body = api.call(P_TRAVEL_STATUS, "GET", retry=True)
                    st = data_of(body) if is_ok(c2, body) else {}
                else:
                    seg["ok"] = False
                    seg["result"] = "CLAIM_FAILED"
                    seg["lines"].append(
                        "⚠️ 领取旅行积分失败（HTTP %s%s），本次没派新的"
                        % (c, "：" + msg_of(cb) if msg_of(cb) else ""))
                    return seg
        # 「既没到家、也没在路上」= 今天还没派过，这是常态，不占一行。
        # 通知里每多一句废话，真正的异常就多一分被忽略的概率。

        # ---- ② 再判断能不能派新的一趟 ----
        if _traveling(st):
            seg["location"] = _loc_name(st)
            seg["lines"].append("🐱 在路上 → %s%s"
                                % (seg["location"],
                                   _eta(st.get("arrive_at"), st.get("server_now"))))
            seg["result"] = "TRAVELING"
        elif st.get("daily_limit_reached"):
            seg["lines"].append("🛑 今天已经派过了（每天一趟）")
            seg["result"] = "LIMIT"
        elif dry_run:
            seg["lines"].append("🔎 dry-run：可以派新的一趟，本次不派")
            seg["result"] = "DRY_RUN"
        else:
            c, cb = api.call(P_TRAVEL_CONFIG, "GET", retry=True)
            locs = data_of(cb).get("locations") if is_ok(c, cb) else None
            if not locs or not isinstance(locs[0], dict) or locs[0].get("id") is None:
                seg["ok"] = False
                seg["result"] = "NO_LOCATION"
                seg["lines"].append("⚠️ 没取到可选目的地（HTTP %s），本次没派" % c)
                return seg
            loc = locs[0]
            d, db = api.call(P_TRAVEL_DEPART, "POST", {"location_id": loc.get("id")})
            if is_ok(d, db):
                new = data_of(db)
                name = (new.get("location") or {}).get("name") or loc.get("name") or "?"
                dur = new.get("duration_hours") or loc.get("duration_hours_min") or "?"
                seg["departed"] = name
                seg["lines"].append("🚀 派出猫猫 → %s（%s 小时后回）" % (name, dur))
                seg["result"] = "DEPARTED"
            else:
                # 4xx 多为业务规则（活动结束、名额变化），不该跟 5xx/网络故障一样当成"要人管"。
                hard = _is_hard_failure(d)
                seg["ok"] = not hard
                seg["result"] = "DEPART_FAILED" if hard else "DEPART_REJECTED"
                seg["lines"].append("%s 派猫没成功（HTTP %s%s）"
                                    % ("⚠️" if hard else "ℹ️", d,
                                       "：" + msg_of(db) if msg_of(db) else ""))

        if claimed and seg["result"] == "CLAIMED":
            seg["lines"].append("ℹ️ 本次只领到积分，没派新的一趟")
        return seg
    except Exception as e:  # noqa: BLE001 - 需求：猫猫挂了不能影响签到
        return {"ok": False, "segment": "猫猫旅行", "result": "EXCEPTION",
                "lines": ["⚠️ 本段异常（%s: %s），不影响签到结论" % (type(e).__name__, e)]}


def _traveling(st: Any) -> bool:
    """是否有一趟在路上。

    先用服务端真实状态字面量（arrived / idle / traveling，参考实现已核对）；
    字面量缺失或改版时才回落到 arrive_at / server_now 的数值判断。
    """
    if not isinstance(st, dict) or not st:
        return False
    if st.get("state"):
        return _norm_state(st.get("state")) == "traveling"
    eta = _left_seconds(st.get("arrive_at"), st.get("server_now"))
    return eta is not None and eta > 0


def _arrived(st: Any) -> bool:
    """是否有已到家、待领取的那一趟。"""
    if not isinstance(st, dict) or not st:
        return False
    if st.get("state"):
        return _norm_state(st.get("state")) == "arrived"
    if _traveling(st):
        return False
    eta = _left_seconds(st.get("arrive_at"), st.get("server_now"))
    return eta is not None and eta <= 0


def _norm_state(state: Any) -> str:
    """把可能的状态别名归一化。"""
    s = str(state or "").strip().lower()
    if s in ("traveling", "travelling", "on_the_way", "in_transit"):
        return "traveling"
    if s in ("arrived", "done", "finished", "settled"):
        return "arrived"
    return s


def _left_seconds(arrive_at: Any, server_now: Any) -> Optional[float]:
    try:
        left = float(arrive_at) - float(server_now)
    except (TypeError, ValueError, OverflowError):
        return None
    return left if math.isfinite(left) else None


def _loc_name(st: Any) -> str:
    if isinstance(st, dict) and isinstance(st.get("location"), dict):
        return st["location"].get("name") or "?"
    return "?"


def _eta(arrive_at: Any, server_now: Any) -> str:
    left = _left_seconds(arrive_at, server_now)
    if left is None:
        return ""
    if left <= 0:
        return "，已到达待领取"
    minutes = int(round(left / 60.0))
    if minutes < 60:
        return "，约 %d 分钟后回" % max(1, minutes)
    return "，约 %.1f 小时后回" % (left / 3600.0)


def _buddy_name(api: Api) -> str:
    """猫猫的名字 + 稀有度，例如「龙焰喵（SSR）」。

    取不到就返回空串，由调用方整行不显示——写「（资料未取到）」
    对读通知的人没有任何价值，只是多占一行。
    """
    c, body = api.call(P_BUDDY_INFO, "GET", retry=True)
    if is_ok(c, body):
        b = data_of(body).get("buddy") or {}
        if b.get("name"):
            return "%s（%s）" % (b["name"], b.get("rarity") or "?")
    return ""


# ================= 推送 =================

# 支持的推送渠道。没配 NOTIFY_CHANNELS 时按这个顺序自动探测：
# 哪个渠道的 secret 配了，就发哪个；配了几个就发几个。
# 不想全发就用 NOTIFY_CHANNELS 点名，例：NOTIFY_CHANNELS=dingtalk,email
#
# 渠道规格表是「一份真相」：全局 secret 与每个账号自带的 notify 共用它，
# 所以加渠道 / 改字段名只动这张表，两条路径不会走偏。
#   fields  : 该渠道认的字段
#   require : 少一个就算没配好（缺了会明确报出来，而不是静默不发）
#   primary : 配置写成一个字符串时，这个值落到哪个字段
#   env     : 字段对应的全局 secret 名（可以是名字元组，按序取第一个非空的）
CHANNEL_SPEC: dict[str, dict] = {
    "feishu": {"label": "飞书", "fields": ("webhook", "secret"), "require": ("webhook",),
               "primary": "webhook",
               "env": {"webhook": "FEISHU_WEBHOOK", "secret": "FEISHU_SECRET"}},
    "wecom": {"label": "企业微信", "fields": ("webhook",), "require": ("webhook",),
              "primary": "webhook", "env": {"webhook": "WECOM_WEBHOOK"}},
    "dingtalk": {"label": "钉钉", "fields": ("webhook", "secret"), "require": ("webhook",),
                 "primary": "webhook",
                 "env": {"webhook": "DINGTALK_WEBHOOK", "secret": "DINGTALK_SECRET"}},
    "serverchan": {"label": "Server酱", "fields": ("key",), "require": ("key",),
                   "primary": "key",
                   "env": {"key": ("SERVERCHAN_KEY", "SERVERCHAN_SENDKEY")}},
    "pushplus": {"label": "PushPlus", "fields": ("token",), "require": ("token",),
                 "primary": "token", "env": {"token": "PUSHPLUS_TOKEN"}},
    "bark": {"label": "Bark", "fields": ("key", "server"), "require": ("key",),
             "primary": "key", "env": {"key": "BARK_KEY", "server": "BARK_URL"}},
    "ntfy": {"label": "ntfy", "fields": ("topic", "server"), "require": ("topic",),
             "primary": "topic", "env": {"topic": "NTFY_TOPIC", "server": "NTFY_URL"}},
    "telegram": {"label": "Telegram", "fields": ("bot_token", "chat_id"),
                 "require": ("bot_token", "chat_id"), "primary": "bot_token",
                 "env": {"bot_token": "TELEGRAM_BOT_TOKEN", "chat_id": "TELEGRAM_CHAT_ID"}},
    "email": {"label": "邮件", "fields": ("host", "port", "user", "pass", "to"),
              "require": ("host", "user", "pass"), "primary": "to",
              "env": {"host": "SMTP_HOST", "port": "SMTP_PORT", "user": "SMTP_USER",
                      "pass": "SMTP_PASS", "to": "MAIL_TO"}},
}

NOTIFY_ORDER = tuple(CHANNEL_SPEC.keys())

CHANNEL_LABEL = {ch: s["label"] for ch, s in CHANNEL_SPEC.items()}

# 字段别名：手写配置最容易在字段名上踩坑（webhook / url / hook…），尽量都兜住
_NOTIFY_FIELD_ALIASES = {
    "webhook": ("url", "hook", "web_hook", "feishu_webhook", "wecom_webhook"),
    "secret": ("sign", "sign_key", "feishu_secret", "dingtalk_secret"),
    "key": ("sendkey", "send_key", "bark_key", "serverchan_key"),
    "token": ("pushplus_token",),
    "bot_token": ("bottoken", "telegram_token"),
    "chat_id": ("chatid", "chat", "telegram_chat_id"),
    "topic": ("ntfy_topic",),
    "server": ("base_url", "baseurl", "server_url"),
    "host": ("smtp_host",),
    "port": ("smtp_port",),
    "user": ("username", "smtp_user", "sender"),
    "pass": ("password", "smtp_pass", "auth_code"),
    "to": ("mail_to", "mailto", "recipient"),
}

# 渠道名别名：中文、常见简写都认。写错的名字不会被静默丢掉，
# 而是原样保留到解析结果里，由 account_channels() 明确报「不认识的渠道」。
_CHANNEL_ALIASES = {
    "feishu": "feishu", "lark": "feishu", "飞书": "feishu",
    "wecom": "wecom", "weixin": "wecom", "qywx": "wecom", "企业微信": "wecom", "企微": "wecom",
    "dingtalk": "dingtalk", "dingding": "dingtalk", "钉钉": "dingtalk",
    "serverchan": "serverchan", "sct": "serverchan", "方糖": "serverchan",
    "pushplus": "pushplus", "pp": "pushplus",
    "bark": "bark",
    "ntfy": "ntfy",
    "telegram": "telegram", "tg": "telegram",
    "email": "email", "mail": "email", "smtp": "email", "邮件": "email", "邮箱": "email",
}

# notify 里用来点名渠道名单的键（其余键都当成渠道名）
_NOTIFY_WHITELIST_KEYS = ("channels", "channel", "use", "only", "list", "渠道", "启用")


def _chan_key(name: Any) -> str:
    """归一化渠道名。不认识的返回小写原名，让上层能报错而不是静默丢弃。"""
    s = str(name or "").strip().lower()
    return _CHANNEL_ALIASES.get(s, s)


def normalize_field(ch: str, name: Any) -> str:
    """把用户写的字段名归一化成该渠道的标准字段名；不认识返回空串。"""
    spec = CHANNEL_SPEC.get(ch)
    if not spec:
        return ""
    key = str(name or "").strip().lower()
    for f in spec["fields"]:
        if key == f.lower() or key in _NOTIFY_FIELD_ALIASES.get(f, ()):
            return f
    return ""


def _pick_notify_fields(ch: str, raw: dict) -> dict:
    """从一段配置里按该渠道的字段表取值（容忍别名）。空值不写入。"""
    cfg: dict = {}
    for k, v in raw.items():
        field = normalize_field(ch, k)
        if field and v not in (None, ""):
            cfg[field] = str(v).strip()
    return cfg


def _env_cfg(ch: str) -> dict:
    """全局 secret 里该渠道的配置（没配的字段不会出现）。"""
    cfg: dict = {}
    for field, names in CHANNEL_SPEC[ch]["env"].items():
        for name in ((names,) if isinstance(names, str) else names):
            v = (os.environ.get(name) or "").strip()
            if v:
                cfg[field] = v
                break
    return cfg


def _missing_fields(ch: str, cfg: dict) -> list[str]:
    return [f for f in CHANNEL_SPEC[ch]["require"] if not str(cfg.get(f) or "").strip()]


def _parse_notify(raw: dict) -> dict:
    """把账号里的通知配置归一化成 {渠道: {字段: 值}}。

    空 dict 表示「这个渠道借全局 secret」。支持三种写法，可混用：

      "notify": ["dingtalk"]                                  # 只选渠道，凭据用全局的
      "notify": {"wecom": "https://...webhook..."}             # 字符串 = 该渠道的 primary 字段
      "notify": {"channels": ["wecom","email"], "wecom": {"webhook": "...", "secret": "..."}}
      "notify": "serverchan,email"                            # 逗号分隔的名单也行

    兼容旧写法：账号上直接写 "webhook" / "secret" = 飞书专属机器人。
    """
    out: dict[str, dict] = {}
    order: list[str] = []

    def touch(ch: str) -> None:
        if ch not in out:
            out[ch] = {}
            order.append(ch)

    def is_whitelist_key(key: Any) -> bool:
        """这个键是「渠道名单」而不是渠道名吗（channels / use / only / 渠道…）。"""
        return (str(key).strip().lower() in _NOTIFY_WHITELIST_KEYS
                and _chan_key(key) not in CHANNEL_SPEC)

    # ① 旧写法：账号级的 webhook / secret 等价于 notify.feishu
    legacy: dict = {}
    for field in ("webhook", "secret"):
        for alias in (field, "feishu_" + field):
            v = raw.get(alias)
            if v not in (None, ""):
                legacy[field] = str(v).strip()
                break
    if legacy:
        touch("feishu")
        out["feishu"].update(legacy)

    # ② notify 字段本身
    val = None
    for alias in _FIELD_ALIASES["notify"]:
        v = raw.get(alias)
        if v not in (None, "", [], {}):
            val = v
            break

    if isinstance(val, str):
        val = [x for x in re.split(r"[,;\s]+", val) if x]

    whitelist: Optional[list[str]] = None
    if isinstance(val, (list, tuple)):
        # 名单式写法：点名的渠道全部「借全局凭据」
        for one in val:
            touch(_chan_key(one))
        whitelist = list(order)
    elif isinstance(val, dict):
        # 先读名单（它决定渠道顺序），再读各渠道自己的配置
        for key, v in val.items():
            if not is_whitelist_key(key):
                continue
            names = v if isinstance(v, (list, tuple)) else re.split(r"[,;\s]+", str(v or ""))
            for one in names:
                if str(one).strip():
                    touch(_chan_key(one))
            whitelist = list(order)
        for key, v in val.items():
            if is_whitelist_key(key):
                continue
            ch = _chan_key(key)
            touch(ch)
            if ch not in CHANNEL_SPEC:
                continue        # 写错的渠道名：留个空壳，交给 account_channels 明确报出来
            if not v or v is True or isinstance(v, (list, tuple)):
                continue        # 只点名 / 写空 -> 借全局 secret
            if isinstance(v, str):
                out[ch][CHANNEL_SPEC[ch]["primary"]] = v.strip()
            elif isinstance(v, dict):
                out[ch].update(_pick_notify_fields(ch, v))

    if whitelist is not None:
        # 名单就是白名单：没点到的渠道即使写了配置也不发（写空 = 回到汇总卡片）
        out = {ch: out.get(ch, {}) for ch in whitelist}
    return out


def account_channels(acc: Account) -> tuple[dict, list[str]]:
    """算出这个账号本轮发到哪几个渠道。

    规则（关键差异在凭据来源）：
      * 账号里**写了字段**的渠道 -> 只用账号自带的字段，**绝不拿全局 secret 兜底**
        （否则会把 A 的机器人密钥发到 B 的群/邮箱，串号比不发更糟）；
      * 只写了渠道名（配置为空）-> 借全局同名 secret。

    返回 (渠道->配置, 问题列表)。渠道为空时：没声明过 -> 并入汇总卡片；
    声明了但没配全 -> 只报问题，不重复发一份汇总（尊重「我只收自己渠道」的意图）。
    """
    resolved: dict[str, dict] = {}
    problems: list[str] = []
    for ch, own in acc.notify.items():
        spec = CHANNEL_SPEC.get(ch)
        if not spec:
            problems.append("不认识的渠道「%s」，已跳过（可选：%s）"
                            % (ch, "、".join(CHANNEL_LABEL[c] for c in NOTIFY_ORDER)))
            continue
        if own:
            cfg, src = dict(own), "账号自带"
        else:
            cfg, src = _env_cfg(ch), "全局 secret"
        miss = _missing_fields(ch, cfg)
        if miss:
            problems.append("%s：凭据来自%s，但缺 %s，本轮这条没发出去"
                            % (spec["label"], src, "、".join(miss)))
            continue
        resolved[ch] = cfg
    return resolved, problems



# 每次 POST 的原始返回都记在这里，供自检脚本（scripts/test_notify.py）
# 打印出来做诊断。正常运行时没人读它，纯观测用途。里面可能含渠道密钥，
# 所以只在自检脚本本地打印，不进任何输出。
LAST_TRACE: list[dict] = []


def _lines_to_md(lines: list[str], nl: str = "\n") -> str:
    return nl.join(l for l in lines if l).strip() or "（无输出）"


def _lines_to_plain(lines: list[str], nl: str = "\n") -> str:
    """去掉 markdown 记号，给只认纯文本的渠道（Bark / ntfy / 邮件）。"""
    return nl.join(re.sub(r"\*\*|<[^>]+>", "", l) for l in lines if l).strip() or "（无输出）"


# 段内换行 / 段间换行。**别把这里改回单个 "\n"**：
#   * 企业微信 markdown：实测单个 \n 不换行，必须 \n\n（且 \n\n 不会多出空行）；
#   * 钉钉 markdown：官方 FAQ「换行格式：\n，重要：\n 前后各两个空格」，
#     社区实测 \n\n 兼容性最好；
#   * Server酱 / PushPlus：走 markdown 渲染，单个 \n 属软换行，同样会被折叠；
#   * 飞书卡片（lark_md）：单个 \n 就是换行，最紧凑，不必空行；
#   * Bark / ntfy / Telegram / 邮件：纯文本，\n 本身就是硬换行。
# 所以「一份内容发给 9 个渠道」不能只有一个 "\n"。手机上看着还是挤成一行时，
# 不用改代码：设 WB_PUSH_NEWLINE=lf（回单换行）或 space（行尾两空格）即可。
NEWLINE_STYLES: dict[str, tuple[str, str]] = {
    "space": ("  \n", "\n\n"),      # 默认：段内行尾两空格（markdown 硬换行，钉钉官方写法），段间空行
    "blank": ("\n\n", "\n\n"),      # 最保守：全用空行。渲染器不认行尾空格时用它
    "lf": ("\n", "\n\n"),           # 单换行：确认客户端认它时才用（最紧凑）
    "lark": ("\n", "\n\n"),         # 飞书卡片 lark_md
}
DEFAULT_NEWLINE = "space"


def _newlines(md: bool, style: str = "") -> tuple[str, str]:
    """返回 (段内换行符, 段间换行符)。纯文本渠道固定 \\n，不参与上面的取舍。"""
    if not md:
        return "\n", "\n\n"
    mode = (style or os.environ.get("WB_PUSH_NEWLINE") or DEFAULT_NEWLINE).strip().lower()
    return NEWLINE_STYLES.get(mode, NEWLINE_STYLES[DEFAULT_NEWLINE])


# 两个段落的显示名。冒号后面就是正文，段标题单独一行、加了粗，
# 扫一眼就知道哪几行说的是签到、哪几行说的是猫猫。
_SEGMENTS = (("🏠", "checkin", "签到"), ("🐾", "cat", "猫猫"))


def _account_block(s: dict, multi: bool, idx: int, md: bool,
                   inner: str, para: str) -> str:
    """一个账号的正文块：标题 + 「🏠 签到」段 +「🐾 猫猫」段。

    某一段没有内容（例如收猫轮次跳过了签到）就整段不渲染——
    留个空标题比不写更难读。
    """
    name = s.get("name") or "?"
    if multi:
        head = "**%d. %s**" % (idx, name) if md else "%d. %s" % (idx, name)
    else:
        head = "**%s**" % name if md else name

    parts = [head]
    for label, key, seg_name in _SEGMENTS:
        lines = s.get(key) or []
        if not lines:
            continue
        body = _lines_to_md(lines, inner) if md else _lines_to_plain(lines, inner)
        # 段标题与它自己的内容之间用 inner（贴着），段与段之间才用 para（空行）：
        # 「哪几行属于猫猫」靠贴在一起就够了，不必每段前后都空一行——那样正文会翻倍长。
        parts.append("%s %s%s%s" % (label, "**%s**" % seg_name if md else seg_name, inner, body))
    return para.join(parts)


def _fmt_sections(sections: list[dict], md: bool = True, style: str = "") -> str:
    """把各账号拼成一段正文。

    结构：总览（可选）→ 各账号块 → 末尾说明（可选）。
    `{"head": "…"}` 不占账号编号、排在开头；`{"note": "…"}` 同样不占编号、排在末尾。
    编号只看账号条目——否则「只有 1 个账号 + 1 行说明」会被编成「1. 张三」，
    看着像多账号。
    """
    inner, para = _newlines(md, style)
    accounts = [s for s in sections if not (s.get("head") or s.get("note"))]
    multi = len(accounts) > 1

    blocks: list[str] = []
    for s in sections:
        if s.get("head"):
            blocks.append(s["head"])
    idx = 0
    for s in sections:
        if s.get("head") or s.get("note"):
            continue
        idx += 1
        blocks.append(_account_block(s, multi, idx, md, inner, para))
    for s in sections:
        if s.get("note"):
            blocks.append(s["note"])
    return para.join(b for b in blocks if b)


def build_feishu_card(title: str, sections: list[dict], ok: bool, secret: str = "") -> dict:
    """构造飞书自定义机器人的交互卡片。

    多账号：每个账号一个分区（hr 隔开），分区内「🏠 签到」「🐾 猫猫」分行写清。
    飞书卡片的 lark_md 单个 \\n 就换行，所以这里用最紧凑的 lark 风格——
    不必像企微/钉钉那样拿空行换行。

    单独抽出来是为了让自检脚本（scripts/test_notify.py）走**同一条**发送路径，
    避免「自检能通、线上不通」这种最难查的偏差。

    sections: [{"name": str, "checkin": [lines], "cat": [lines]}, ...]
              可含 {"head": str}（排最前）与 {"note": str}（排最后），都不占编号。
    """
    inner, para = _newlines(True, "lark")
    elements: list[dict] = []
    accounts = [s for s in sections if not (s.get("head") or s.get("note"))]
    multi = len(accounts) > 1

    for s in sections:
        if s.get("head"):
            elements.append({"tag": "div", "fields": [
                {"is_short": False, "text": {"tag": "lark_md", "content": s["head"]}}]})

    idx = 0
    for s in sections:
        if s.get("head") or s.get("note"):
            continue
        idx += 1
        if elements:
            elements.append({"tag": "hr"})
        head = "**%d. %s**" % (idx, s["name"]) if multi else "**%s**" % s["name"]
        parts = [head]
        for label, key, seg_name in _SEGMENTS:
            lines = s.get(key) or []
            if not lines:
                continue
            parts.append("%s **%s**%s%s" % (label, seg_name, inner,
                                            _lines_to_md(lines, inner)))
        elements.append({"tag": "div", "fields": [
            {"is_short": False, "text": {"tag": "lark_md", "content": para.join(parts)}}]})

    # note 是卡片底部的小字，里面不能带 markdown 记号（那边不解析）
    for s in sections:
        if s.get("note"):
            elements.append({"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": re.sub(r"\*\*|<[^>]+>", "", s["note"]).strip()}]})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green" if ok else "orange",
                   "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }
    payload: dict = {"msg_type": "interactive", "card": card}
    if secret:
        import base64
        import hashlib
        import hmac
        ts = str(int(time.time()))
        key = ("%s\n%s" % (ts, secret)).encode("utf-8")
        payload["timestamp"] = ts
        payload["sign"] = base64.b64encode(
            hmac.new(key, b"", digestmod=hashlib.sha256).digest()).decode()
    return payload


def push_feishu(webhook: str, title: str, sections: list[dict],
                ok: bool, secret: str = "") -> str:
    """飞书自定义机器人：一张卡片，账号分区、签到与猫猫分行写清。"""
    return _post_json(webhook, build_feishu_card(title, sections, ok, secret), "飞书")


def push_wecom(webhook: str, sections: list[dict], title: str) -> str:
    """企业微信群机器人：markdown 消息。

    不需要企业认证，手机上装个企业微信、自己建个群就能用，
    形态跟飞书最接近（都是「建群 → 加机器人 → 复制 Webhook」）。
    """
    content = "**%s**\n\n%s" % (title, _fmt_sections(sections))
    return _post_json(webhook, {"msgtype": "markdown", "markdown": {"content": content}},
                      "企业微信")


def push_dingtalk(webhook: str, sections: list[dict], title: str, secret: str = "") -> str:
    """钉钉群机器人：markdown 消息，支持「加签」安全模式。

    钉钉的加签跟飞书同源：sign = base64(HMAC-SHA256(key = timestamp + "\\n" + secret, msg = ""))，
    但 timestamp 是**毫秒**，且 timestamp/sign 要拼在 URL 查询参数上（不是放 body）。
    安全模式三选一里务必用「加签」——GitHub runner 的出口 IP 是动态的，「IP 白名单」必挂。
    """
    url = webhook
    if secret:
        import base64
        import hashlib
        import hmac
        import urllib.parse
        ts = str(int(round(time.time() * 1000)))
        key = ("%s\n%s" % (ts, secret)).encode("utf-8")
        sign = urllib.parse.quote_plus(base64.b64encode(
            hmac.new(key, b"", digestmod=hashlib.sha256).digest()).decode())
        url += ("&" if "?" in url else "?") + "timestamp=%s&sign=%s" % (ts, sign)
    return _post_json(url, {"msgtype": "markdown",
                            "markdown": {"title": title,
                                         "text": "### %s\n\n%s" % (title, _fmt_sections(sections))}},
                      "钉钉")


def push_serverchan(sendkey: str, title: str, sections: list[dict]) -> str:
    """Server酱：推到你的微信（微信扫码关注「方糖」服务号即可拿 SendKey）。

    对没有飞书/企业微信的人，这是最省事的一条——不用建群、不用装 App。
    免费版每天有额度限制，日常只跑一两次完全够。
    """
    url = sendkey if sendkey.startswith("http") else "https://sctapi.ftqq.com/%s.send" % sendkey
    return _post_json(url, {"title": title, "desp": _fmt_sections(sections)}, "Server酱")


def push_pushplus(token: str, title: str, sections: list[dict]) -> str:
    """PushPlus：也是推微信，扫码登录拿 token 即可，同样零门槛。"""
    return _post_json("https://www.pushplus.plus/send",
                      {"token": token, "title": title,
                       "content": _fmt_sections(sections), "template": "markdown"},
                      "PushPlus")


def push_bark(key: str, title: str, sections: list[dict], server: str = "") -> str:
    """Bark：iOS 推送（装个免费 App，拿到 key 就能收）。可自建服务器，用 BARK_URL 覆盖。"""
    base = (server or "https://api.day.app").rstrip("/")
    url = key if key.startswith("http") else "%s/%s" % (base, key.strip("/"))
    return _post_json(url, {"title": title, "body": _fmt_sections(sections, md=False),
                            "group": "Buddy加油站"}, "Bark")


def push_ntfy(topic: str, title: str, sections: list[dict], server: str = "") -> str:
    """ntfy：开源推送，手机装 App 订阅一个 topic 即可，不用注册。

    topic 相当于密码——名字取得随机一点，别用 buddy-daily 这种能被猜到的。
    """
    base = (server or "https://ntfy.sh").rstrip("/")
    url = topic if topic.startswith("http") else "%s/%s" % (base, topic.strip("/"))
    body = "%s\n\n%s" % (title, _fmt_sections(sections, md=False))
    # HTTP 头只能是 ASCII，所以 Title 用固定英文，中文标题放正文首行
    return _post_raw(url, body.encode("utf-8"),
                     {"Content-Type": "text/plain; charset=utf-8",
                      "Title": "Buddy Gas Station Daily", "Tags": "cat"}, "ntfy")


def push_telegram(bot_token: str, chat_id: str, title: str, sections: list[dict]) -> str:
    """Telegram Bot。用纯文本（不带 parse_mode），避免内容里的特殊字符把消息卡住。"""
    return _post_json("https://api.telegram.org/bot%s/sendMessage" % bot_token,
                      {"chat_id": chat_id, "disable_web_page_preview": True,
                       "text": "%s\n\n%s" % (title, _fmt_sections(sections, md=False))},
                      "Telegram")


def push_email(host: str, port: int, user: str, password: str, to: str,
               subject: str, text: str) -> str:
    """SMTP 邮件。465 走 SSL，其他端口（587/25）走 STARTTLS。

    最通用的一条：任何邮箱都行（QQ/163/Outlook/Gmail 都发「授权码」，不是登录密码）。
    缺点是不即时——当兜底渠道用比较合适。
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    if int(port) == 465:
        with smtplib.SMTP_SSL(host, int(port), timeout=TIMEOUT) as s:
            s.login(user, password)
            refused = s.send_message(msg)
    else:
        with smtplib.SMTP(host, int(port), timeout=TIMEOUT) as s:
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            s.login(user, password)
            refused = s.send_message(msg)
    # send_message 不抛异常也可能"部分被拒"，这里必须显式检查，否则就是静默丢信
    if refused:
        return "邮件部分被拒：%s" % refused
    return "邮件已发送"


def _resp_verdict(raw: str) -> tuple[bool, str]:
    """各家推送服务的成功判定收敛成一处。

    飞书 `code=0`、企微/钉钉 `errcode=0`、Server酱 `code=0`、
    PushPlus/Bark `code=200`、Telegram `ok=true`；ntfy 返回纯文本，HTTP 通了就算成。
    """
    try:
        j = json.loads(raw)
    except ValueError:
        return True, ""
    if not isinstance(j, dict):
        return True, ""
    for key, good in (("code", (0, 200)), ("errcode", (0,)),
                      ("StatusCode", (0,)), ("status", (0,))):
        if key in j:
            try:
                v = int(j[key])
            except (TypeError, ValueError):
                continue
            if v not in good:
                reason = (j.get("msg") or j.get("errmsg")
                          or j.get("message") or j.get("error") or "")
                return False, "code=%s%s" % (v, " " + str(reason) if reason else "")
    if j.get("ok") is False:
        return False, "ok=false %s" % (j.get("description") or "")
    return True, ""


def _post_json(url: str, payload: dict, label: str) -> str:
    # ensure_ascii=False：中文直出（RFC 8259 规定 JSON 就是 UTF-8），
    # 报文体积小、抓包和日志里也直接看得懂。
    return _post_raw(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                     {"Content-Type": "application/json; charset=utf-8"}, label)


def _post_raw(url: str, data: bytes, headers: dict, label: str) -> str:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    code, out = -1, ""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            code = r.status
            out = r.read().decode("utf-8", "replace")
        LAST_TRACE.append({"label": label, "url": url, "http": code, "raw": out})
    except urllib.error.HTTPError as e:
        code = e.code
        out = e.read().decode("utf-8", "replace")
        LAST_TRACE.append({"label": label, "url": url, "http": code, "raw": out})
        return "%s推送失败：HTTP %s %s" % (label, code, out[:200].strip())
    except Exception as e:  # noqa: BLE001
        LAST_TRACE.append({"label": label, "url": url, "http": -1,
                           "raw": "%s: %s" % (type(e).__name__, e)})
        return "%s推送失败：%s" % (label, e)
    ok, why = _resp_verdict(out)
    return ("%s推送成功" % label) if ok else ("%s推送失败：%s" % (label, why or out[:200]))


def detect_channels() -> list[str]:
    """全局那一层这轮要发哪些渠道。

    * 配了 NOTIFY_CHANNELS：按点名发（all = 所有已配置的渠道）；
    * 没配：哪个渠道的 secret 配全了就发哪个（配了几个发几个）。

    只关心**全局 secret**；账号自带的渠道由 account_channels() 单独算，互不影响。
    """
    available = [ch for ch in NOTIFY_ORDER if not _missing_fields(ch, _env_cfg(ch))]

    spec = (os.environ.get("NOTIFY_CHANNELS") or "").strip().lower().replace(";", ",")
    if not spec:
        return available
    want = {_chan_key(c) for c in spec.split(",") if c.strip()}
    if "all" in want:
        return available
    return [ch for ch in available if ch in want]


def _dispatch(ch: str, cfg: dict, stamp: str, env: str, title: str,
              sections: list[dict], ok: bool) -> str:
    """把一份内容投给某个渠道；配置由调用方给（全局的，或某个账号自带的）。

    单个渠道炸了只返回一句失败原因，不影响其他渠道、也不影响退出码。
    """
    try:
        if ch == "feishu":
            return push_feishu(cfg["webhook"], title, sections, ok,
                               cfg.get("secret") or "")
        if ch == "wecom":
            return push_wecom(cfg["webhook"], sections, title)
        if ch == "dingtalk":
            return push_dingtalk(cfg["webhook"], sections, title, cfg.get("secret") or "")
        if ch == "serverchan":
            return push_serverchan(cfg["key"], title, sections)
        if ch == "pushplus":
            return push_pushplus(cfg["token"], title, sections)
        if ch == "bark":
            return push_bark(cfg["key"], title, sections, cfg.get("server") or "")
        if ch == "ntfy":
            return push_ntfy(cfg["topic"], title, sections, cfg.get("server") or "")
        if ch == "telegram":
            return push_telegram(cfg["bot_token"], cfg["chat_id"], title, sections)
        if ch == "email":
            return push_email(cfg["host"], int(cfg.get("port") or 465), cfg["user"],
                              cfg.get("pass") or "", cfg.get("to") or cfg["user"],
                              title,
                              "%s\n\n%s" % (title, _fmt_sections(sections, md=False)))
    except Exception as ex:  # noqa: BLE001
        return "%s推送失败：%s" % (CHANNEL_LABEL.get(ch, ch), ex)
    return "%s：未知渠道（可选：%s）" % (ch, ", ".join(NOTIFY_ORDER))


# ================= 单账号执行 =================
def run_one(acc: Account, dry_run: bool = False, cat_only: bool = False) -> dict:
    """跑一个账号。返回的 dict 里下划线开头的键是内部字段，不对外输出。

    cat_only=True：只跑猫猫段（领已到家的积分 + 该派就派新的一趟），跳过签到段。
    用于「六小时收一次猫」那几轮——签到一天一次就够，重复调只是白跑接口。
    do_cat() 本身自带「先领后派」的闭环，所以收猫轮次不会让猫闲着，
    领完积分会立刻再派一趟，积分才转得起来。
    """
    api = Api(acc.endpoint, acc.token, acc.uid, acc.domain)
    if cat_only:
        checkin = {"ok": False, "segment": "签到", "result": "SKIPPED",
                   "lines": ["（本轮只收猫，签到段跳过——签到每天一次即可）"]}
    else:
        checkin = do_checkin(api, dry_run)
    cat = do_cat(api, dry_run)          # 独立 try，炸了也只影响本段
    resolved, problems = account_channels(acc)
    return {
        "name": acc.name,
        "uid": redact(acc.uid),
        "checkin_ok": checkin["ok"],
        "cat_ok": cat["ok"],
        "checkin": checkin,
        "cat": cat,
        "_trace": api.trace,
        "_notify": resolved,               # {渠道: 配置}，可能为空
        "_notify_problems": problems,
        "_declared_notify": bool(acc.notify),
    }


def _public(obj: Any) -> Any:
    """去掉内部字段（含账号自带的渠道凭据）。"""
    if isinstance(obj, dict):
        return {k: _public(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_public(v) for v in obj]
    return obj


def _seg_result(r: dict, seg: str) -> str:
    """读某一段的结论字面量。缺键一律当空串——
    这几个函数只用来排版，不该因为少一个键就把整轮推送炸掉。"""
    return str(((r.get(seg) or {}).get("result")) or "")


def _account_ok(r: dict) -> bool:
    """该账号本轮算不算成功。

    只收猫的轮次签到段恒为 SKIPPED，拿它当门槛等于「必然失败」，
    所以此时看猫猫段；整轮则看签到（猫猫段炸了不影响签到结论，这是需求）。
    """
    if _seg_result(r, "checkin") in _SKIPPED_RESULTS:
        return bool(r.get("cat_ok"))
    return bool(r.get("checkin_ok"))


def _is_cat_only(results: list[dict]) -> bool:
    """这一轮是不是「只收猫」（签到段整段没跑）。预算耗尽被动跳过的也算——
    它同样没跑签到，总览里不该冒出「签到 0/N」这种吓人的数。"""
    return bool(results) and all(_seg_result(r, "checkin") in _SKIPPED_RESULTS
                                 for r in results)


def _overview(results: list[dict]) -> str:
    """一行总览。

    收到通知的人第一件事是「有没有事要我管」，而不是逐行读六个账号。
    所以这行只报结论：签到几个成了、领到几趟积分、派出几趟、谁有问题。
    常态的事只给个数，不铺开。
    """
    n = len(results)
    cat_only = _is_cat_only(results)
    bad_ck = [r["name"] for r in results
              if _seg_result(r, "checkin") != "SKIPPED" and not r.get("checkin_ok")]
    bad_ct = [r["name"] for r in results if not r.get("cat_ok")]
    got = sum(1 for r in results if (r.get("cat") or {}).get("reward") is not None)
    dep = sum(1 for r in results if (r.get("cat") or {}).get("departed"))
    trav = sum(1 for r in results if _seg_result(r, "cat") == "TRAVELING")

    bits: list[str] = []
    if not cat_only:
        bits.append("✅ 签到 %d/%d" % (n - len(bad_ck), n))
    if got:
        bits.append("🎁 收 %d 趟积分" % got)
    if dep:
        bits.append("🚀 派出 %d 趟" % dep)
    if trav:
        bits.append("🐱 %d 只在路上" % trav)

    problems = bad_ck + [x for x in bad_ct if x not in bad_ck]
    if problems:
        bits.insert(0, "⚠️ 有问题：%s" % "、".join(problems))
    return " · ".join(bits) or "没什么变化"


def _push_title(results: list[dict], stamp: str, env: str, clipped: bool = False) -> str:
    """通知标题。手机通知栏只给一行，所以按「这份是发给谁看的」来定制：

    * 单账号（各人收自己那份）→ 把名字写上去，一眼知道是谁的结果；
    * 多账号（汇总卡片）→ 说清一共几个账号；
    * 只收猫那几轮 → 标明「收猫」，否则半夜收到一个「日报」会莫名其妙；
    * clipped（把没变化的账号裁掉后）→ 标明「仅报变化」，
      否则名单里少了人，收到的人会以为那几个账号跑挂了。
    """
    who = results[0]["name"] if len(results) == 1 else "%d 个账号" % len(results)
    if clipped:
        # 裁掉了没变化的人就必须标出来：否则「本来 6 个账号，卡片里只剩 1 个」
        # 看着像另外 5 个跑挂了。裁完只剩一个人时尤其要标。
        who += "（仅报变化）"
    kind = "收猫" if _is_cat_only(results) else "日报"
    # stamp 是「2026-10-09 10:49」，标题里只留「10-09 10:49」（完整日期占地方、信息重复）
    short = stamp[5:] if len(stamp) >= 16 else stamp
    title = "Buddy 加油站%s · %s · %s" % (kind, who, short)
    if env and env != "prod":
        title += " [%s]" % env
    return title


def _sections_of(results: list[dict], note: str = "") -> list[dict]:
    """推送内容：一条总览 + 各账号块 + 末尾说明（都不占账号编号）。

    * 只收猫的轮次里签到段是跳过的，那一段整段不渲染——
      留一个「🏠 签到 ⏱ 本轮跳过」的空壳只会让正文更长；
    * note 用来放「本轮因为 GitHub 定时器延迟才 5 点发出来」这类话：
      收到通知的人第一反应就是「怎么这个点发」，正文里答掉它，省一次困惑。
    """
    secs: list[dict] = [{"head": _overview(results)}]
    for r in results:
        checkin = (r.get("checkin") or {}).get("lines") or []
        if _seg_result(r, "checkin") == "SKIPPED":
            checkin = []                    # 只收猫的轮次：不显示「🏠 签到」段
        secs.append({"name": r["name"], "checkin": checkin,
                     "cat": (r.get("cat") or {}).get("lines") or []})
    if note:
        secs.append({"note": note})
    return secs


# ================= 「这轮有没有值得打扰人的东西」=================
# 判定的唯一真相：每一段里属于「一切照旧」的结论。**不在表里的一律算有变化**——
# 认不出来的结论宁可多推一次，也不能悄悄吞掉一个错误（服务端加个新状态字面量时，
# 我们只会多收到一条消息，不会漏掉一条）。
_QUIET_RESULTS = {
    # 今天已经签过 / 活动没开 / dry-run
    "checkin": frozenset({"ALREADY", "INACTIVE", "DRY_RUN"}),
    # 今天已派过（猫在路上 / 名额已用完）/ 积分已经领过、眼下没有待领的 / dry-run
    "cat": frozenset({"TRAVELING", "LIMIT", "IDLE", "DRY_RUN"}),
}

# 「这一段本轮压根没跑」的结论。它们既不算变化、也不算失败，所以要单独拎出来：
#   * SKIPPED   —— 有意跳过（收猫轮不跑签到），安静是对的；
#   * NO_BUDGET —— 时间预算耗尽被动跳过，**必须报**（这是「没跑到」，不是「没事」）。
# 用同一个 SKIPPED 表示这两种情况曾把这个区别抹掉：预算耗尽会被当成「安静」而静默。
_SKIPPED_RESULTS = frozenset({"SKIPPED", "NO_BUDGET"})

# 安静结论的人话解释：静默时要说清「为什么没推」，
# 否则用户只能自己猜「是不是跑挂了」——静默必须能自证。
_QUIET_WHY = {
    "checkin": {"ALREADY": "今天已经签过", "INACTIVE": "签到活动没开",
                "SKIPPED": "本轮不跑签到（只收猫）", "DRY_RUN": "dry-run 没写数据"},
    "cat": {"TRAVELING": "今天已经派过，猫还在路上",
            "LIMIT": "今天已经派过（每天一趟），积分也已领过",
            "IDLE": "眼下没有待领的积分", "SKIPPED": "本轮不跑猫猫",
            "DRY_RUN": "dry-run 没写数据"},
}
_SEG_LABEL = {"checkin": "签到", "cat": "猫猫"}


def _seg_news(r: dict, seg: str) -> bool:
    """这一段有没有值得打扰人的东西。"""
    res = _seg_result(r, seg)
    if res == "SKIPPED":
        return False     # 有意跳过（收猫轮不跑签到）：不算变化，也不算失败
    if res in _QUIET_RESULTS[seg]:
        # 结论看着安静，判定却是失败 -> 照样得说（例如「已签过」但接口其实报了错）
        return not bool(r.get("%s_ok" % seg))
    return True          # 领到积分 / 派了新一趟 / 出错 / 没见过的结论（含 NO_BUDGET）


def _account_news(r: dict) -> bool:
    """这个账号本轮有没有变化（= 该不该推给他）。"""
    return any(_seg_news(r, s) for s in _SEG_LABEL)


def _account_quiet_why(r: dict) -> list[str]:
    """没变化时，逐段给出人话原因（日志 / 结果 JSON / Actions 摘要用）。"""
    out = []
    for seg, label in _SEG_LABEL.items():
        res = _seg_result(r, seg)
        why = _QUIET_WHY.get(seg, {}).get(res) or "没变化（%s）" % (res or "无结论")
        out.append("%s：%s" % (label, why))
    return out


def _has_news(results: list[dict]) -> bool:
    """这一轮里有没有任何一个账号值得打扰。谁都没有 -> 整轮不推。

    一天要跑四轮，而「今天已经签过、已经派过猫、积分也领过了」在后面的轮次里
    完全是常态：把这种重复的「一切照旧」推出去，只会让真正的异常被淹掉。
    """
    return any(_account_news(r) for r in results)


def _clipped_note(names: list[str]) -> str:
    """汇总卡片被裁掉的人，末尾补一句说明。

    单独抽出来是因为 preview_push.py 也要显示同一句——
    预览和真发必须是同一套文案，否则「预览看着好好的、发出去不一样」。
    """
    return ("另有 %d 个账号本轮无变化（已签到 / 今日已派过猫 / 积分已领过），未列出：%s"
            % (len(names), "、".join(names)))


def notify(results: list[dict], stamp: str, env: str, note: str = "",
           news_only: bool = False) -> list[str]:
    """推送。逐人分层，互不牵连：

    * 声明了自己的渠道（`notify`）的账号：**只**发给他自己那几个渠道，
      不混进汇总卡片，也不受全局 NOTIFY_CHANNELS 影响；
    * 其余账号：汇总成一份，发到全局渠道（detect_channels）。

    news_only=True（默认的 onchange 语义）时**逐人裁掉没变化的**：
    今天已经签过、已经派过猫、积分也领过的人，这一轮不该再被打扰一次。
    有变化的人照发；汇总卡片里也只列有变化的人，被裁掉的在末尾说明一行。

    note 非空时附在正文末尾（例如「本轮是 GitHub 定时器延迟触发的」）。
    单个渠道失败只写进 notices，不影响退出码，也不影响别的渠道。
    """
    notices: list[str] = []
    shared: list[dict] = []
    skipped: list[str] = []            # 因为「没变化」而没推的账号名

    # ① 各人发自己的（标题带上他自己的名字，通知栏一眼能认出是谁的结果）
    for r in results:
        for p in (r.get("_notify_problems") or []):
            notices.append("%s（通知）：%s" % (r["name"], p))
        if news_only and not _account_news(r):
            # 没变化：不打扰他，但要在 notices 里留下痕迹——静默必须能自证
            skipped.append(r["name"])
            notices.append("%s：本轮无变化（%s），不推送"
                           % (r["name"], "；".join(_account_quiet_why(r))))
            continue
        resolved = r.get("_notify") or {}
        if not resolved:
            # 没声明过渠道 -> 并入汇总；
            # 声明了但没配全 -> 只报上面那条问题，不再补发汇总（尊重「我只收自己的渠道」）
            if not r.get("_declared_notify"):
                shared.append(r)
            continue
        secs = _sections_of([r], note)
        for ch, cfg in resolved.items():
            notices.append("%s → %s" % (r["name"], _dispatch(
                ch, cfg, stamp, env, _push_title([r], stamp, env), secs,
                _account_ok(r))))

    # ② 没人认领的账号汇总成一份，发到全局渠道
    if shared:
        channels = detect_channels()
        if not channels:
            who = "、".join(r["name"] for r in shared)
            notices.append(
                "未配置任何推送渠道，已跳过：%s 这 %d 个账号没在自己的条目里配 notify，"
                "全局也没配任何渠道 secret。"
                "（若他们其实配过自己的渠道 → 多半是云端清单/代码是旧的："
                "重新 export_token.py --push，并确认 Actions 跑的是最新提交。"
                "详见 README「通知渠道」一节）" % (who, len(shared)))
        else:
            secs = _sections_of(shared, note)
            if skipped:
                # 裁了人就得说：否则名单里少了谁，收到的人会以为那几个账号跑挂了
                secs.append({"note": _clipped_note(skipped)})
            ok = all(_account_ok(r) for r in shared)
            title = _push_title(shared, stamp, env, clipped=bool(skipped))
            for ch in channels:
                notices.append(_dispatch(ch, _env_cfg(ch), stamp, env, title, secs, ok))

    return notices


def write_step_summary(body: dict, results: list[dict]) -> None:
    """把本轮结论写进 Actions 页面的 Job Summary（不在 Actions 里跑就跳过）。

    为什么需要它：默认「没变化就不推送」。真一整天没收到消息时，得有个地方
    能立刻确认「它跑了，而且确实没变化」——否则「安静」和「跑挂了」在人眼里长得一样。
    Actions 页面上的这一页摘要就是那个地方，它不打扰任何人。
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rep = body.get("cron") or {}
    summ = body.get("summary") or {}
    seg = "只收猫（领积分 + 该派就派）" if body.get("segment") == "cat" else "整轮（签到 + 猫猫）"

    out = ["## Buddy 加油站 · %s" % seg, ""]
    out.append("- 跑的时间：%s（北京）" % (rep.get("actual_local") or "-"))
    if rep.get("triggered_cron"):
        out.append("- 触发：cron `%s` · 计划 %s（北京） · 实际晚了 %s 分钟"
                   % (rep["triggered_cron"], rep.get("planned_local") or "-",
                      rep.get("delay_minutes")))
    else:
        out.append("- 触发：手动（workflow_dispatch）")
    out.append("- 结果：%d 个账号，%s" % (
        summ.get("total", len(results)),
        "全部成功" if body.get("ok") else "有失败：%s" % "、".join(summ.get("failed") or [])))
    if body.get("silent"):
        out.append("- 推送：**跳过**（本轮没有变化，理由见下表「变化」列）")
    else:
        out.append("- 推送：%s" % "；".join(body.get("notices") or [])[:500])

    out += ["", "| 账号 | 签到 | 猫猫 | 变化 |", "| --- | --- | --- | --- |"]
    for r in results:
        why = "、".join(_account_quiet_why(r)) if not _account_news(r) else "有变化 → 已推送"
        out.append("| %s | %s | %s | %s |" % (
            r["name"], _seg_result(r, "checkin") or "-", _seg_result(r, "cat") or "-", why))
    for w in body.get("warnings") or []:
        out.append("\n> ⚠️ %s" % w)
    out.append("")

    try:
        with open(path, "a", encoding="utf-8") as fp:
            fp.write("\n".join(out) + "\n")
    except OSError as e:      # 摘要只是给人看的，写不进去不该影响任务本身
        print("[summary] 写 Actions 摘要失败：%s" % e, file=sys.stderr)


# ================= 主流程 =================
def main() -> int:
    ap = argparse.ArgumentParser(description="Buddy 加油站每日签到 + 派猫（多账号）")
    ap.add_argument("--local", action="store_true", help="从本机登录态取凭证（单账号，调试用）")
    ap.add_argument("--as", dest="as_name", help="配合 --local：给本机账号起个名字")
    ap.add_argument("--accounts", help="本机账号清单 JSON 文件（多账号调试）")
    ap.add_argument("--only", help="只跑指定账号（名字或序号，逗号分隔）")
    ap.add_argument("--list-accounts", action="store_true", help="只列出识别到的账号（脱敏）后退出")
    ap.add_argument("--dry-run", action="store_true", help="只查状态，不做写操作")
    ap.add_argument("--raw", action="store_true", help="附上脱敏后的原始返回")
    ap.add_argument("--no-notify", action="store_true", help="不推送，只看结论")
    ap.add_argument("--mode", choices=("auto", "all", "cat"), default="auto",
                    help="跑哪一段。auto（默认）：按「触发的 cron」自动判定"
                         "（workflow 注入 TRIGGER_CRON；认不出来按整轮跑并报警）；"
                         "all 签到 + 猫猫；cat 只收猫")
    ap.add_argument("--cat-only", action="store_true",
                    help="等价于 --mode cat：只收猫（领已到家的积分 + 该派就派新的一趟），跳过签到")
    ap.add_argument("--notify-mode", choices=("auto", "always", "onchange"), default="auto",
                    help="auto / onchange（都是默认）：只在有变化时推送，并逐人裁掉"
                         "「今天已签到 / 已派过猫 / 积分已领过」的人；"
                         "always：每轮都推，不看有没有变化")
    ap.add_argument("--allow-partial", action="store_true",
                    help="只要有任意一个账号签到成功就返回 0（默认要求全部成功）")
    args = ap.parse_args()

    # 也认环境变量，方便在 workflow 里开关，不用改命令行
    allow_partial = args.allow_partial or (
        (os.environ.get("WB_ALLOW_PARTIAL") or "").strip().lower() in ("1", "true", "yes", "on"))

    # 跑哪一段：手动指定优先；auto 则由「触发的 cron」决定。
    # 映射表 SCHEDULE_MODES 只有这一处，workflow 里不再复制一份 case——
    # 两处各写一份、改了一处忘了另一处，就是这类脚本最容易出的静默事故。
    trigger = (os.environ.get("TRIGGER_CRON") or "").strip()
    cron_rep = cron_report(trigger)
    if args.mode == "auto":
        cat_only = args.cat_only or (cron_rep.get("plan") == "cat")
    else:
        cat_only = args.cat_only or args.mode == "cat"
    print("[cron] %s；本轮模式=%s" % (cron_line(cron_rep), "只收猫" if cat_only else "整轮"),
          file=sys.stderr)

    # 推送频率：默认**只在有变化时推**，而且逐人判、逐人裁。
    # 一天跑四轮，「今天已经签过、已经派过猫、积分也领过了」在后面几轮里是常态，
    # 把这句重复的「一切照旧」推四遍，只会让真正的异常被淹掉。
    # 要每轮都推（比如手动触发时想立刻看到结果）就用 --notify-mode always。
    notify_mode = (args.notify_mode if args.notify_mode != "auto"
                   else (os.environ.get("WB_NOTIFY_MODE") or "").strip().lower())
    if notify_mode not in ("always", "onchange"):
        notify_mode = "onchange"

    try:
        accounts = _load_accounts(args)
        if args.only:
            accounts = _select(accounts, args.only)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "result": "NO_CREDENTIAL",
                          "report": "账号清单不可用：%s" % e}, ensure_ascii=False))
        return 3

    if not accounts:
        print(json.dumps({"ok": False, "result": "NO_ACCOUNT",
                          "report": "筛选后没有可执行的账号"}, ensure_ascii=False))
        return 3

    if not os.environ.get("WB_BUDGET_SECONDS"):
        set_budget(default_budget(len(accounts)))

    if args.list_accounts:
        print(json.dumps({"ok": True, "count": len(accounts),
                          "budget_seconds": int(_BUDGET),
                          "accounts": [public_account(a) for a in accounts]},
                         ensure_ascii=False, indent=2))
        return 0

    print("[cred] 共 %d 个账号，预算 %.0fs，本轮=%s，推送=%s"
          % (len(accounts), _BUDGET,
             "只收猫（跳过签到）" if cat_only else "签到 + 猫猫", notify_mode),
          file=sys.stderr)
    for a in accounts:
        chans, problems = account_channels(a)
        where = "、".join(CHANNEL_LABEL.get(c, c) for c in chans) or (
            "汇总卡片" if not a.notify else "（渠道没配全，见下方 notices）")
        print("  - %s  uid=%s  token=%s  推送=%s  %s"
              % (a.name, redact(a.uid), redact(a.token), where, a.endpoint), file=sys.stderr)
        for p in problems:
            print("    ⚠️ %s" % p, file=sys.stderr)

    # 清单里有本版代码不认识的字段 -> 大声报出来（否则就是「配了却不生效」）
    unknown_warnings = [
        "%s：清单条目里有本版代码不认识的字段 %s，它们会被静默忽略。"
        "最常见的原因是「清单已升级、跑的代码还是旧的」——"
        "push 最新代码后重跑；若是拼错字段名，改掉即可。"
        % (a.name, "、".join(a.unknown))
        for a in accounts if a.unknown
    ]
    for w in unknown_warnings:
        print("  ⚠️ %s" % w, file=sys.stderr)

    results: list[dict] = []
    for a in accounts:
        if _budget_left() <= 1:
            resolved, problems = account_channels(a)
            results.append({
                "name": a.name, "uid": redact(a.uid), "checkin_ok": False, "cat_ok": False,
                # 用 NO_BUDGET 而不是 SKIPPED：这不是「有意跳过」，是「没跑到」，
                # 必须推出去让人看见（SKIPPED 会被当成安静而静默掉）。
                "checkin": {"ok": False, "segment": "签到", "result": "NO_BUDGET",
                            "lines": ["⏱ 时间预算已耗尽，本账号本轮跳过（下轮会自动补上）"]},
                "cat": {"ok": False, "segment": "猫猫旅行", "result": "NO_BUDGET",
                        "lines": ["⏱ 时间预算已耗尽，本段跳过"]},
                "_trace": [], "_notify": resolved, "_notify_problems": problems,
                "_declared_notify": bool(a.notify)})
            continue
        results.append(run_one(a, args.dry_run, cat_only=cat_only))

    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    env_name = os.environ.get("WB_ENV", "prod")

    checkin_oks = [r["checkin_ok"] for r in results]
    cat_oks = [r["cat_ok"] for r in results]
    # 判定用哪一段：只收猫的轮次里签到段恒为 SKIPPED，拿它当门槛等于必失败
    gates = cat_oks if cat_only else checkin_oks
    ok = any(gates) if allow_partial else all(gates)
    n_ok = sum(1 for x in checkin_oks if x)

    body = {
        "ok": ok,
        "timestamp": stamp,
        "env": env_name,
        # 本轮实际跑了哪几段，方便在 Actions 日志里一眼区分两类定时
        "segment": "cat" if cat_only else "all",
        "mode": "partial" if allow_partial else "all",
        # 「计划几点、实际几点、晚了多久」：GitHub 的 cron 是尽力而为，
        # 不写进结果就只能靠猜，人只会怀疑自己的配置。
        "cron": cron_rep,
        "summary": {
            "total": len(results),
            "checkin_ok": n_ok,
            "cat_ok": sum(1 for r in results if r["cat_ok"]),
            "failed": [r["name"] for r, g in zip(results, gates) if not g],
        },
        "accounts": _public(results),
    }
    if cat_only:
        body["summary"]["note"] = (
            "本轮只收猫：签到段跳过，checkin_ok 恒为 0 属正常，成败看 cat_ok")
    if args.raw:
        for pub, raw in zip(body["accounts"], results):
            pub["raw"] = raw["_trace"]

    # 延迟太久 / cron 认不出来：给人一句能看懂的话，别让人对着「我明明设的 00:20」发懵
    late = late_note(cron_rep)
    cron_warnings = [w for w in (cron_rep.get("warning"), late) if w]
    for w in cron_warnings:
        print("  ⚠️ %s" % w, file=sys.stderr)

    news = _has_news(results)
    body["has_news"] = news
    body["notify_mode"] = notify_mode

    if args.no_notify:
        body["notices"] = ["(--no-notify：已跳过推送)"]
    elif notify_mode == "onchange" and not news:
        # 谁都没变化：整轮静默。但必须留下自证的痕迹——
        # 「安静」和「跑挂了」在人眼里长得一样，不写清楚就只能靠人猜。
        body["silent"] = {
            "reason": "本轮所有账号都没有变化（今天已签到 / 已派过猫猫 / 积分已领过）",
            "details": {r["name"]: _account_quiet_why(r) for r in results},
        }
        detail = "；".join("%s（%s）" % (r["name"], "、".join(_account_quiet_why(r)))
                           for r in results)
        body["notices"] = ["⏸ 本轮没有变化，未推送以免打扰 —— %s" % detail]
        print("[notify] 本轮无变化，跳过推送：%s" % detail, file=sys.stderr)
    else:
        # 延迟说明附进推送正文：收到 5 点的通知时，正文里就能看到为什么。
        # news_only：逐人裁掉没变化的（他今天已经做过的事，不必再报一遍）。
        body["notices"] = notify(results, stamp, env_name, note=late,
                                 news_only=(notify_mode == "onchange"))

    # 「配置被静默忽略」是最难自查的一类问题：放到最显眼的两处，别让人去翻日志
    if unknown_warnings:
        body["warnings"] = unknown_warnings
        body["notices"] = unknown_warnings + body["notices"]
    if cron_warnings:
        body["warnings"] = cron_warnings + body.get("warnings", [])
        body["notices"] = cron_warnings + body["notices"]

    write_step_summary(body, results)      # Actions 页面上留一页摘要（不在 Actions 里跑就跳过）
    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 需求 4：签到成功就算成功，猫猫失败不改退出码
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
