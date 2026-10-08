#!/usr/bin/env python3
"""WorkBuddy「Buddy 加油站」每日一体化任务（多账号版）。

一次跑完所有账号，每个账号做两件事：
  1. 签到：先查活动状态，未签才领（幂等，重复跑不会多领）。
  2. 猫猫旅行：先领掉「已到家」那一趟的旅行积分，再判断今天还能不能派新的一趟；
     今天已经派过（daily_limit_reached）就不派。

结果推送：默认把没有专属 webhook 的账号汇总成**一份**，每个账号一个分区、
区内「🏠 签到」与「🐾 猫猫」分行写清；某个账号如果自带 webhook，
则单独发给他自己的群。

通知渠道可插拔，支持 9 种，**配了哪个就发哪个**（配几个发几个）：
  飞书 / 企业微信 / 钉钉 / Server酱 / PushPlus / Bark / ntfy / Telegram / 邮件
用 NOTIFY_CHANNELS 点名可以只发其中几个（例：NOTIFY_CHANNELS=dingtalk,email）。
一个渠道都没配不会报错，只是在结果里提示一句。某个渠道失败只写进 notices，
既不影响退出码，也不影响别的渠道。

隔离层级（重要）：
  * 账号之间互相隔离——A 的 token 过期不影响 B 照常签到；
  * 账号内部，猫猫段被 try 兜住——它怎么炸都不改该账号的签到结论。

退出码：所有账号签到成功 => 0；加 --allow-partial 则「至少一个成功」=> 0。
猫猫段一律不影响退出码（需求：签到成功就算成功）。

凭证来源（优先级从高到低）：
  1. --local                  本机登录态（调试用，单账号）
  2. --accounts <文件>         本机账号清单 JSON（调试多账号）
  3. WB_ACCOUNTS              环境变量，JSON 数组（云端推荐）
  4. WB_TOKEN / WB_UID        旧版单账号环境变量（向后兼容）

明文 token 只从上述来源读入，不打印、不落盘；所有输出经脱敏。

用法：
  python3 scripts/daily.py                          # 云端：读 WB_ACCOUNTS
  python3 scripts/daily.py --local                  # 本机单账号
  python3 scripts/daily.py --accounts accounts.local.json   # 本机多账号
  python3 scripts/daily.py --list-accounts          # 只列出识别到的账号（脱敏）
  python3 scripts/daily.py --only "我的账号,小号"    # 只跑指定账号（名字或序号）
  python3 scripts/daily.py --local --raw            # 附上脱敏后的原始返回
  python3 scripts/daily.py --dry-run                # 只查状态，不做写操作
  python3 scripts/daily.py --no-notify              # 不推送，只看结论
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
_FIELD_ALIASES = {
    "name": ("name", "alias", "label", "账号", "备注"),
    "token": ("token", "access_token", "accessToken", "wb_token"),
    "uid": ("uid", "user_id", "userId", "wb_uid"),
    "domain": ("domain", "wb_domain"),
    "endpoint": ("endpoint", "base_url", "baseUrl"),
    "webhook": ("webhook", "feishu_webhook", "feishuWebhook"),
    "secret": ("secret", "feishu_secret", "feishuSecret"),
}


@dataclass
class Account:
    name: str
    token: str
    uid: str = ""
    domain: str = ""
    endpoint: str = ENDPOINT_DEFAULT
    webhook: str = ""      # 该账号专属飞书机器人（留空则并入汇总卡片）
    secret: str = ""       # 专属机器人开了签名校验时才需要


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
    return Account(
        name=_pick(raw, "name") or "账号%d" % index,
        token=token,
        uid=_pick(raw, "uid"),
        domain=_pick(raw, "domain"),
        endpoint=_pick(raw, "endpoint") or endpoint_env,
        webhook=_pick(raw, "webhook"),
        secret=_pick(raw, "secret"),
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
    """给日志/JSON 用的脱敏视图。"""
    out = {"name": a.name, "uid": redact(a.uid), "token": redact(a.token),
           "domain": a.domain or "-", "endpoint": a.endpoint}
    if a.webhook:
        out["webhook"] = "专属（已配置）"
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
    code, body = api.call(P_CHECKIN_STATUS, "POST", {}, retry=True)
    if code == -1:
        return {"ok": False, "segment": "签到", "result": "NETWORK",
                "lines": ["❌ 网络不可达，未拿到签到状态（%s）" % body.get("error", "")]}
    if code in (401, 403):
        return {"ok": False, "segment": "签到", "result": "AUTH",
                "lines": ["❌ 鉴权失败（HTTP %s）：该账号的 token 可能已过期，"
                          "请在本机重跑刷新脚本导出新 token" % code]}
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
                "lines": ["✅ 今日已签过" + _status_tail(st)]}

    if dry_run:
        return {"ok": True, "segment": "签到", "result": "DRY_RUN",
                "lines": ["🔎 dry-run：今日未签，跳过领取"]}

    # 领取接口虽然写数据，但本身幂等（当天重复领取只会返回「已签」，不会再发一次积分），
    # 所以允许重试；其余写操作一律不重试，避免超时发生在服务端已处理完之后造成重复提交。
    code2, body2 = api.call(P_CHECKIN_CLAIM, "POST", {}, retry=True)
    if code2 == -1:
        return {"ok": False, "segment": "签到", "result": "NETWORK",
                "lines": ["❌ 领取请求未能送达，请下次重试"]}
    if code2 in (401, 403):
        return {"ok": False, "segment": "签到", "result": "AUTH",
                "lines": ["❌ 鉴权失败（HTTP %s）" % code2]}

    if is_ok(code2, body2):
        got = data_of(body2)
        c3, body3 = api.call(P_CHECKIN_STATUS, "POST", {}, retry=True)
        fresh = data_of(body3) if is_ok(c3, body3) else st
        credit = got.get("credit", fresh.get("today_credit"))
        return {"ok": True, "segment": "签到", "result": "CLAIMED",
                "lines": ["✅ 签到成功，+%s 积分%s" % (_num(credit), _status_tail(fresh))]}

    if isinstance(body2, dict) and body2.get("code") == CODE_ALREADY_CHECKED_IN:
        return {"ok": True, "segment": "签到", "result": "ALREADY",
                "lines": ["✅ 今日已签过（服务端判定已领取）" + _status_tail(st)]}

    return {"ok": False, "segment": "签到", "result": "ERROR",
            "lines": ["❌ 签到失败（HTTP %s%s）"
                      % (code2, "：" + msg_of(body2) if msg_of(body2) else "")]}


def _status_tail(st: Any) -> str:
    """把状态里的积分/连签信息拼成一句尾巴。"""
    if not isinstance(st, dict):
        return ""
    bits = []
    for label, key, unit in (("今日", "today_credit", ""), ("连续", "streak_days", " 天"),
                             ("累计", "total_credits", "")):
        v = st.get(key)
        if v is not None:
            bits.append("%s %s%s" % (label, _num(v), unit))
    return "（%s）" % "，".join(bits) if bits else ""


# ================= 第 2 段：猫猫旅行 =================
def do_cat(api: Api, dry_run: bool = False) -> dict:
    """先领已到家的旅行积分，再看今日名额决定派不派新的一趟。

    整段被 try 兜住：任何异常都只记进本段结论，不影响签到。
    """
    seg: dict = {"ok": True, "segment": "猫猫旅行", "result": "IDLE", "lines": []}
    try:
        code, body = api.call(P_TRAVEL_STATUS, "GET", retry=True)
        if code == -1:
            return {"ok": False, "segment": "猫猫旅行", "result": "NETWORK",
                    "lines": ["⚠️ 网络不可达，本段跳过（%s）" % body.get("error", "")]}
        if code in (401, 403):
            return {"ok": False, "segment": "猫猫旅行", "result": "AUTH",
                    "lines": ["⚠️ 鉴权失败（HTTP %s），本段跳过" % code]}
        if not is_ok(code, body):
            return {"ok": False, "segment": "猫猫旅行", "result": "ERROR",
                    "lines": ["⚠️ 旅行状态接口异常（HTTP %s%s），本段跳过"
                              % (code, "：" + msg_of(body) if msg_of(body) else "")]}

        seg["lines"].append("🐾 猫咪：%s" % _buddy_name(api))
        st = data_of(body)
        claimed = False

        # ---- ① 先领掉「已到家」那一趟的旅行积分 ----
        if _arrived(st):
            if dry_run:
                seg["lines"].append("🎁 有一趟已到家的积分待领（dry-run 不领）")
            else:
                # 带上 record_id：服务端目前对空 body 也接受（实测同样返回 not arrived yet），
                # 但明确的契约是认 record_id，带上更稳妥。
                rid = st.get("record_id")
                payload = {"record_id": rid} if rid is not None else {}
                c, cb = api.call(P_TRAVEL_CLAIM, "POST", payload)
                if is_ok(c, cb):
                    reward = data_of(cb).get("reward_credit", st.get("reward_credit"))
                    seg["lines"].append("🎁 领到已到家的旅行积分 +%s" % _num(reward))
                    seg["result"] = "CLAIMED"
                    claimed = True
                    c2, body = api.call(P_TRAVEL_STATUS, "GET", retry=True)
                    st = data_of(body) if is_ok(c2, body) else {}
                else:
                    seg["ok"] = False
                    seg["result"] = "CLAIM_FAILED"
                    seg["lines"].append(
                        "⚠️ 领取旅行积分失败（HTTP %s%s），本次不派新的一趟，"
                        "避免覆盖尚未领取的奖励" % (c, "：" + msg_of(cb) if msg_of(cb) else ""))
                    return seg
        elif _traveling(st):
            pass  # 还在路上，没什么可领
        else:
            seg["lines"].append("ℹ️ 没有待领取的旅行积分")

        # ---- ② 再判断能不能派新的一趟 ----
        if _traveling(st):
            seg["lines"].append("🐱 猫猫旅行中：%s%s"
                                % (_loc_name(st), _eta(st.get("arrive_at"), st.get("server_now"))))
            seg["result"] = "TRAVELING"
        elif st.get("daily_limit_reached"):
            seg["lines"].append("🛑 今日派发名额已用完（今天已经派过了），不再派新的一趟")
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
                seg["lines"].append("⚠️ 没取到可选目的地（HTTP %s），本次不派" % c)
                return seg
            loc = locs[0]
            d, db = api.call(P_TRAVEL_DEPART, "POST", {"location_id": loc.get("id")})
            if is_ok(d, db):
                new = data_of(db)
                name = (new.get("location") or {}).get("name") or loc.get("name") or "?"
                dur = new.get("duration_hours") or loc.get("duration_hours_min") or "?"
                seg["lines"].append("🚀 派出猫猫去%s（%s 小时后回）" % (name, dur))
                seg["result"] = "DEPARTED"
            else:
                # 4xx 多为业务规则（活动结束、名额变化），不该跟 5xx/网络故障一样当成"要人管"。
                hard = _is_hard_failure(d)
                seg["ok"] = not hard
                seg["result"] = "DEPART_FAILED" if hard else "DEPART_REJECTED"
                seg["lines"].append("%s 派猫未成功（HTTP %s%s）"
                                    % ("⚠️" if hard else "ℹ️", d,
                                       "：" + msg_of(db) if msg_of(db) else ""))

        if claimed and seg["result"] == "CLAIMED":
            seg["lines"].append("ℹ️ 只领了积分，本次没有派新的一趟")
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
    c, body = api.call(P_BUDDY_INFO, "GET", retry=True)
    if is_ok(c, body):
        b = data_of(body).get("buddy") or {}
        if b.get("name"):
            return "%s（%s）" % (b["name"], b.get("rarity") or "?")
    return "（资料未取到）"


# ================= 推送 =================
NOTE_TEXT = "各账号互相隔离 · 猫猫段失败不影响该账号签到结论 · 由 GitHub Actions 定时执行"

# 支持的推送渠道。没配 NOTIFY_CHANNELS 时按这个顺序自动探测：
# 哪个渠道的 secret 配了，就发哪个；配了几个就发几个。
# 不想全发就用 NOTIFY_CHANNELS 点名，例：NOTIFY_CHANNELS=dingtalk,email
NOTIFY_ORDER = ("feishu", "wecom", "dingtalk", "serverchan", "pushplus",
                "bark", "ntfy", "telegram", "email")

CHANNEL_LABEL = {
    "feishu": "飞书", "wecom": "企业微信", "dingtalk": "钉钉",
    "serverchan": "Server酱", "pushplus": "PushPlus", "bark": "Bark",
    "ntfy": "ntfy", "telegram": "Telegram", "email": "邮件",
}


# 每次 POST 的原始返回都记在这里，供自检脚本（scripts/test_notify.py）
# 打印出来做诊断。正常运行时没人读它，纯观测用途。里面可能含渠道密钥，
# 所以只在自检脚本本地打印，不进任何输出。
LAST_TRACE: list[dict] = []


def _lines_to_md(lines: list[str]) -> str:
    return "\n".join(l for l in lines if l).strip() or "（无输出）"


def _lines_to_plain(lines: list[str]) -> str:
    """去掉 markdown 记号，给只认纯文本的渠道（Bark / ntfy / 邮件）。"""
    return "\n".join(re.sub(r"\*\*|<[^>]+>", "", l)
                     for l in lines if l).strip() or "（无输出）"


def _fmt_sections(sections: list[dict], md: bool = True) -> str:
    """把各账号拼成一段文本：每个账号一块，块内「签到」「猫猫」分行。"""
    multi = len(sections) > 1
    blocks = []
    for i, s in enumerate(sections):
        if multi:
            head = "**%d. %s**" % (i + 1, s["name"]) if md else "%d. %s" % (i + 1, s["name"])
        else:
            head = "**%s**" % s["name"] if md else s["name"]
        ck = _lines_to_md(s.get("checkin") or []) if md else _lines_to_plain(s.get("checkin") or [])
        ct = _lines_to_md(s.get("cat") or []) if md else _lines_to_plain(s.get("cat") or [])
        if md:
            blocks.append("%s\n🏠 **签到**\n%s\n🐾 **猫猫**\n%s" % (head, ck, ct))
        else:
            blocks.append("%s\n🏠 签到\n%s\n🐾 猫猫\n%s" % (head, ck, ct))
    return "\n\n".join(blocks)


def build_feishu_card(stamp: str, env: str, sections: list[dict],
                      ok: bool, secret: str = "") -> dict:
    """构造飞书自定义机器人的交互卡片。

    多账号：每个账号一个分区，分区内「🏠 签到」「🐾 猫猫」分行写清；
    单账号时 structure 一样，只是只有一个分区。

    单独抽出来是为了让自检脚本（scripts/test_notify.py）走**同一条**发送路径，
    避免「自检能通、线上不通」这种最难查的偏差。

    sections: [{"name": str, "checkin": [lines], "cat": [lines]}, ...]
    """
    elements: list[dict] = []
    multi = len(sections) > 1
    for i, s in enumerate(sections):
        if i:
            elements.append({"tag": "hr"})
        head = "**%d. %s**" % (i + 1, s["name"]) if multi else "**%s**" % s["name"]
        content = "%s\n🏠 **签到**\n%s\n🐾 **猫猫**\n%s" % (
            head, _lines_to_md(s.get("checkin") or []), _lines_to_md(s.get("cat") or []))
        elements.append({"tag": "div", "fields": [
            {"is_short": False, "text": {"tag": "lark_md", "content": content}}]})
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": NOTE_TEXT}]})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green" if ok else "orange",
                   "title": {"tag": "plain_text",
                             "content": "Buddy 加油站日报 · %s · %s" % (stamp, env)}},
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


def push_feishu(webhook: str, stamp: str, env: str, sections: list[dict],
                ok: bool, secret: str = "") -> str:
    """飞书自定义机器人：一张卡片，账号分区、签到与猫猫分行写清。"""
    return _post_json(webhook, build_feishu_card(stamp, env, sections, ok, secret), "飞书")


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
    """决定这轮要发哪些渠道。

    * 配了 NOTIFY_CHANNELS：按点名发（all = 所有已配置的渠道）；
    * 没配：哪个渠道的 secret 配了就发哪个（配了几个发几个）。
    """
    def env(k: str) -> str:
        return (os.environ.get(k) or "").strip()

    available = set()
    if env("FEISHU_WEBHOOK"):
        available.add("feishu")
    if env("WECOM_WEBHOOK"):
        available.add("wecom")
    if env("DINGTALK_WEBHOOK"):
        available.add("dingtalk")
    if env("SERVERCHAN_KEY") or env("SERVERCHAN_SENDKEY"):
        available.add("serverchan")
    if env("PUSHPLUS_TOKEN"):
        available.add("pushplus")
    if env("BARK_KEY") or env("BARK_URL"):
        available.add("bark")
    if env("NTFY_TOPIC"):
        available.add("ntfy")
    if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
        available.add("telegram")
    if env("SMTP_HOST"):
        available.add("email")

    spec = env("NOTIFY_CHANNELS").lower().replace(";", ",")
    if spec:
        want = {c.strip() for c in spec.split(",") if c.strip()}
        if "all" in want:
            return [c for c in NOTIFY_ORDER if c in available]
        return [c for c in NOTIFY_ORDER if c in want and c in available]
    return [c for c in NOTIFY_ORDER if c in available]


def _dispatch(ch: str, stamp: str, env: str, title: str,
              sections: list[dict], ok: bool) -> str:
    """把一份内容投给某个渠道。单个渠道炸了不影响其他渠道。"""
    def e(k: str) -> str:
        return (os.environ.get(k) or "").strip()

    try:
        if ch == "feishu":
            return push_feishu(e("FEISHU_WEBHOOK"), stamp, env, sections, ok,
                               os.environ.get("FEISHU_SECRET", ""))
        if ch == "wecom":
            return push_wecom(e("WECOM_WEBHOOK"), sections, title)
        if ch == "dingtalk":
            return push_dingtalk(e("DINGTALK_WEBHOOK"), sections, title, e("DINGTALK_SECRET"))
        if ch == "serverchan":
            return push_serverchan(e("SERVERCHAN_KEY") or e("SERVERCHAN_SENDKEY"),
                                   title, sections)
        if ch == "pushplus":
            return push_pushplus(e("PUSHPLUS_TOKEN"), title, sections)
        if ch == "bark":
            return push_bark(e("BARK_KEY"), title, sections, e("BARK_URL"))
        if ch == "ntfy":
            return push_ntfy(e("NTFY_TOPIC"), title, sections, e("NTFY_URL"))
        if ch == "telegram":
            return push_telegram(e("TELEGRAM_BOT_TOKEN"), e("TELEGRAM_CHAT_ID"),
                                 title, sections)
        if ch == "email":
            return push_email(e("SMTP_HOST"), int(e("SMTP_PORT") or "465"),
                              e("SMTP_USER"), os.environ.get("SMTP_PASS") or "",
                              e("MAIL_TO") or e("SMTP_USER"),
                              "[Buddy加油站] %s · %s" % (stamp, env),
                              "%s\n\n%s" % (title, _fmt_sections(sections, md=False)))
    except Exception as ex:  # noqa: BLE001
        return "%s推送失败：%s" % (CHANNEL_LABEL.get(ch, ch), ex)
    return "%s：未知渠道（可选：%s）" % (ch, ", ".join(NOTIFY_ORDER))


# ================= 单账号执行 =================
def run_one(acc: Account, dry_run: bool = False) -> dict:
    """跑一个账号。返回的 dict 里下划线开头的键是内部字段，不对外输出。"""
    api = Api(acc.endpoint, acc.token, acc.uid, acc.domain)
    checkin = do_checkin(api, dry_run)
    cat = do_cat(api, dry_run)          # 独立 try，炸了也只影响本段
    return {
        "name": acc.name,
        "uid": redact(acc.uid),
        "checkin_ok": checkin["ok"],
        "cat_ok": cat["ok"],
        "checkin": checkin,
        "cat": cat,
        "_trace": api.trace,
        "_webhook": acc.webhook,
        "_secret": acc.secret,
    }


def _public(obj: Any) -> Any:
    """去掉内部字段（含专属 webhook / 签名密钥）。"""
    if isinstance(obj, dict):
        return {k: _public(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_public(v) for v in obj]
    return obj


def _sections_of(results: list[dict]) -> list[dict]:
    return [{"name": r["name"], "checkin": r["checkin"]["lines"],
             "cat": r["cat"]["lines"]} for r in results]


def notify(results: list[dict], stamp: str, env: str) -> list[str]:
    """推送。

    * 账号自带 webhook 的：单独发给他自己的群（飞书），不混进大卡片；
    * 其余的：按 detect_channels() 得出的渠道，每个渠道各发一份。

    单个渠道失败只写进 notices，不影响退出码，也不影响别的渠道。
    """
    notices: list[str] = []
    shared = [r for r in results if not r["_webhook"]]
    own = [r for r in results if r["_webhook"]]
    title = "Buddy 加油站日报 · %s · %s" % (stamp, env)

    # ① 账号专属群
    for r in own:
        notices.append("%s：%s" % (r["name"], push_feishu(
            r["_webhook"], stamp, env, _sections_of([r]), r["checkin_ok"], r["_secret"])))

    # ② 全局渠道
    if shared:
        channels = detect_channels()
        if not channels:
            notices.append("未配置任何推送渠道，已跳过（见 README「通知渠道」一节）")
        else:
            secs = _sections_of(shared)
            ok = all(r["checkin_ok"] for r in shared)
            for ch in channels:
                notices.append(_dispatch(ch, stamp, env, title, secs, ok))

    return notices


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
    ap.add_argument("--allow-partial", action="store_true",
                    help="只要有任意一个账号签到成功就返回 0（默认要求全部成功）")
    args = ap.parse_args()

    # 也认环境变量，方便在 workflow 里开关，不用改命令行
    allow_partial = args.allow_partial or (
        (os.environ.get("WB_ALLOW_PARTIAL") or "").strip().lower() in ("1", "true", "yes", "on"))

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

    print("[cred] 共 %d 个账号，预算 %.0fs" % (len(accounts), _BUDGET), file=sys.stderr)
    for a in accounts:
        print("  - %s  uid=%s  token=%s  %s"
              % (a.name, redact(a.uid), redact(a.token), a.endpoint), file=sys.stderr)

    results: list[dict] = []
    for a in accounts:
        if _budget_left() <= 1:
            results.append({
                "name": a.name, "uid": redact(a.uid), "checkin_ok": False, "cat_ok": False,
                "checkin": {"ok": False, "segment": "签到", "result": "SKIPPED",
                            "lines": ["⏱ 时间预算已耗尽，本账号本轮跳过（下轮会自动补上）"]},
                "cat": {"ok": False, "segment": "猫猫旅行", "result": "SKIPPED",
                        "lines": ["⏱ 时间预算已耗尽，本段跳过"]},
                "_trace": [], "_webhook": a.webhook, "_secret": a.secret})
            continue
        results.append(run_one(a, args.dry_run))

    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    env_name = os.environ.get("WB_ENV", "prod")

    checkin_oks = [r["checkin_ok"] for r in results]
    ok = any(checkin_oks) if allow_partial else all(checkin_oks)
    n_ok = sum(1 for x in checkin_oks if x)

    body = {
        "ok": ok,
        "timestamp": stamp,
        "env": env_name,
        "mode": "partial" if allow_partial else "all",
        "summary": {
            "total": len(results),
            "checkin_ok": n_ok,
            "cat_ok": sum(1 for r in results if r["cat_ok"]),
            "failed": [r["name"] for r in results if not r["checkin_ok"]],
        },
        "accounts": _public(results),
    }
    if args.raw:
        for pub, raw in zip(body["accounts"], results):
            pub["raw"] = raw["_trace"]

    if args.no_notify:
        body["notices"] = ["(--no-notify：已跳过推送)"]
    else:
        body["notices"] = notify(results, stamp, env_name)

    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 需求 4：签到成功就算成功，猫猫失败不改退出码
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
