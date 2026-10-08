#!/usr/bin/env python3
"""WorkBuddy「Buddy 加油站」每日一体化任务（多账号版）。

一次跑完所有账号，每个账号做两件事：
  1. 签到：先查活动状态，未签才领（幂等，重复跑不会多领）。
  2. 猫猫旅行：先领掉「已到家」那一趟的旅行积分，再判断今天还能不能派新的一趟；
     今天已经派过（daily_limit_reached）就不派。

结果推送飞书：默认所有账号汇总成**一张卡片**，每个账号一个分区、区内
「🏠 签到」与「🐾 猫猫」分行写清；某个账号如果自带 webhook，则单独发给他自己的群。

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


def _lines_to_md(lines: list[str]) -> str:
    return "\n".join(l for l in lines if l).strip() or "（无输出）"


def build_feishu_card(stamp: str, env: str, sections: list[dict],
                      ok: bool, secret: str = "") -> dict:
    """构造飞书自定义机器人的交互卡片。

    多账号：每个账号一个分区，分区内「🏠 签到」「🐾 猫猫」分行写清；
    单账号时 structure 一样，只是只有一个分区。

    单独抽出来是为了让自检脚本（scripts/test_feishu.py）用**同一个**构造函数，
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
    blocks = "\n\n".join(
        "**%s**\n\n**🏠 加油站签到**\n%s\n\n**🐾 猫猫旅行**\n%s"
        % (s["name"], _lines_to_md(s.get("checkin") or []), _lines_to_md(s.get("cat") or []))
        for s in sections)
    return _post_json(webhook, {"msgtype": "markdown",
                                "markdown": {"content": "**%s**\n\n%s" % (title, blocks)}},
                      "企业微信")


def push_email(host: str, port: int, user: str, password: str, to: str,
               subject: str, text: str) -> str:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as s:
        s.login(user, password)
        s.send_message(msg)
    return "邮件已发送"


def _post_json(url: str, payload: dict, label: str) -> str:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            out = r.read().decode("utf-8", "replace")
        if label == "飞书":
            try:
                j = json.loads(out)
                if j.get("code") not in (0, None) or j.get("StatusCode") not in (0, None):
                    return "%s推送失败：%s" % (label, out[:200])
            except ValueError:
                pass
        return "%s推送成功" % label
    except Exception as e:  # noqa: BLE001
        return "%s推送失败：%s" % (label, e)


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
    """推送。自带 webhook 的账号单独发，其余的汇总成一张卡片。"""
    notices: list[str] = []
    shared = [r for r in results if not r["_webhook"]]
    own = [r for r in results if r["_webhook"]]

    global_wh = (os.environ.get("FEISHU_WEBHOOK") or "").strip()
    global_secret = os.environ.get("FEISHU_SECRET", "")
    if shared:
        if global_wh:
            ok = all(r["checkin_ok"] for r in shared)
            notices.append(push_feishu(global_wh, stamp, env, _sections_of(shared),
                                       ok, global_secret))
        else:
            notices.append("飞书：%d 个账号未配置 Webhook，已跳过" % len(shared))

    for r in own:
        notice = push_feishu(r["_webhook"], stamp, env, _sections_of([r]),
                             r["checkin_ok"], r["_secret"])
        notices.append("%s：%s" % (r["name"], notice))

    ww = (os.environ.get("WECOM_WEBHOOK") or "").strip()
    if ww:
        notices.append(push_wecom(ww, _sections_of(results), "%s · %s" % (stamp, env)))

    if os.environ.get("SMTP_HOST"):
        try:
            body = "\n\n".join(
                "%s\n签到：%s\n猫猫：%s" % (r["name"], "\n".join(r["checkin"]["lines"]),
                                          "\n".join(r["cat"]["lines"]))
                for r in results)
            notices.append(push_email(
                os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "465")),
                os.environ["SMTP_USER"], os.environ["SMTP_PASS"],
                os.environ.get("MAIL_TO", os.environ["SMTP_USER"]),
                "[Buddy加油站] %s · %s" % (stamp, env), body))
        except Exception as e:  # noqa: BLE001
            notices.append("邮件推送失败：%s" % e)

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
