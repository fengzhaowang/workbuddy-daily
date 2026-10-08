#!/usr/bin/env python3
"""WorkBuddy「Buddy 加油站」每日一体化任务：先签到，再处理猫猫旅行。

一次跑完两件事：
  1. 签到：先查活动状态，未签才领（幂等，重复跑不会多领）。
  2. 猫猫旅行：先领掉「已到家」那一趟的旅行积分，再判断今天还能不能派新的一趟；
     今天已经派过（daily_limit_reached）就不派。
  3. 结果分「签到」「猫猫」两段推送飞书（可选企业微信 / 邮件）。
  4. 猫猫整段被 try 兜住：它怎么炸都不改签到结论；签到成功即退出码 0。

凭证只从环境变量读（GitHub Actions 由仓库 secret 注入）。
本机调试时加 --local 才读本机登录态；明文 token 不打印、不落盘，所有输出经脱敏。

用法：
  python3 scripts/daily.py                  # 云端：读 WB_TOKEN / WB_UID / WB_DOMAIN
  python3 scripts/daily.py --local          # 本机：读本机登录态
  python3 scripts/daily.py --local --raw    # 额外打印脱敏后的原始返回（验收用）
  python3 scripts/daily.py --dry-run        # 只查状态，不做任何写操作
  python3 scripts/daily.py --no-notify      # 不推送，只看结论
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
RUN_BUDGET_SECONDS = int(os.environ.get("WB_BUDGET_SECONDS", "420"))
_started_at = time.monotonic()

# ---------- 接口路径：全部与客户端实际请求核对通过 ----------
# 每个路径都用真实请求验证过「存在且语义正确」，不是网上抄的旧名字。
#   实测：伪造路径 -> 404 route not found；下列路径 -> 200 / 业务 4xx。
P_CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"   # POST {} -> 活动状态
P_CHECKIN_CLAIM = "/v2/billing/meter/daily-checkin"              # POST {} -> 领签到积分
P_TRAVEL_STATUS = "/v2/activity/growth/buddy/travel/status"      # GET      -> 旅行状态
P_TRAVEL_CONFIG = "/v2/activity/growth/buddy/travel/config"      # GET      -> 可选目的地
P_TRAVEL_CLAIM = "/v2/activity/growth/buddy/travel/claim"        # POST {} -> 领到家积分
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


def _budget_left() -> float:
    return RUN_BUDGET_SECONDS - (time.monotonic() - _started_at)


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
                "lines": ["❌ 鉴权失败（HTTP %s）：secret 里的 token 可能已过期，"
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
def push_feishu(webhook: str, title: str, checkin: str, cat: str, env: str,
                ok: bool, secret: str = "") -> str:
    """飞书自定义机器人：一张卡片，签到与猫猫分两个分区写清楚。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green" if ok else "orange",
                   "title": {"tag": "plain_text",
                             "content": "Buddy 加油站日报 · %s · %s" % (title, env)}},
        "elements": [
            {"tag": "div", "fields": [{"is_short": False, "text": {
                "tag": "lark_md", "content": "**🏠 加油站签到**\n%s" % checkin}}]},
            {"tag": "hr"},
            {"tag": "div", "fields": [{"is_short": False, "text": {
                "tag": "lark_md", "content": "**🐾 猫猫旅行**\n%s" % cat}}]},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "猫猫段失败不影响签到结论 · 由 GitHub Actions 定时执行"}]},
        ],
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
    return _post_json(webhook, payload, "飞书")


def push_wecom(webhook: str, checkin: str, cat: str, title: str) -> str:
    md = ("**%s**\n\n**🏠 加油站签到**\n%s\n\n**🐾 猫猫旅行**\n%s" % (title, checkin, cat))
    return _post_json(webhook, {"msgtype": "markdown", "markdown": {"content": md}}, "企业微信")


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


# ================= 主流程 =================
def _load_credentials(local: bool) -> tuple[str, str, str, str]:
    endpoint = os.environ.get("WB_ENDPOINT", ENDPOINT_DEFAULT)
    if local:
        import wb_auth
        c = wb_auth.load_credentials()
        return c["token"], c["uid"], c.get("domain", ""), c.get("endpoint") or endpoint
    token = (os.environ.get("WB_TOKEN") or "").strip()
    uid = (os.environ.get("WB_UID") or "").strip()
    if not token:
        raise RuntimeError("环境变量 WB_TOKEN 未设置（云端应由仓库 secret 注入）")
    return token, uid, os.environ.get("WB_DOMAIN", ""), endpoint


def main() -> int:
    ap = argparse.ArgumentParser(description="Buddy 加油站每日签到 + 派猫")
    ap.add_argument("--local", action="store_true", help="从本机登录态取凭证（调试用）")
    ap.add_argument("--dry-run", action="store_true", help="只查状态，不做写操作")
    ap.add_argument("--raw", action="store_true", help="打印脱敏后的原始返回")
    ap.add_argument("--no-notify", action="store_true", help="不推送，只看结论")
    args = ap.parse_args()

    try:
        token, uid, domain, endpoint = _load_credentials(args.local)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "result": "NO_CREDENTIAL",
                          "report": "未取得凭证：%s" % e}, ensure_ascii=False))
        return 3

    print("[cred] token=%s uid=%s domain=%s endpoint=%s"
          % (redact(token), redact(uid), domain or "-", endpoint), file=sys.stderr)

    api = Api(endpoint, token, uid, domain)
    checkin = do_checkin(api, args.dry_run)
    cat = do_cat(api, args.dry_run)          # 独立 try，炸了也只影响本段

    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    env_name = os.environ.get("WB_ENV", "prod")
    title = "签到成功" if checkin["ok"] else "签到需关注"

    body = {
        "ok": checkin["ok"],
        "checkin_ok": checkin["ok"],
        "cat_ok": cat["ok"],
        "checkin": checkin,
        "cat": cat,
        "timestamp": stamp,
    }
    if args.raw:
        body["raw"] = api.trace

    if not args.no_notify:
        notices = []
        fw = os.environ.get("FEISHU_WEBHOOK")
        if fw:
            notices.append(push_feishu(fw, title, "\n".join(checkin["lines"]),
                                       "\n".join(cat["lines"]), env_name,
                                       checkin["ok"], os.environ.get("FEISHU_SECRET", "")))
        ww = os.environ.get("WECOM_WEBHOOK")
        if ww:
            notices.append(push_wecom(ww, "\n".join(checkin["lines"]),
                                      "\n".join(cat["lines"]), "%s · %s" % (title, stamp)))
        if os.environ.get("SMTP_HOST"):
            try:
                notices.append(push_email(
                    os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "465")),
                    os.environ["SMTP_USER"], os.environ["SMTP_PASS"],
                    os.environ.get("MAIL_TO", os.environ["SMTP_USER"]),
                    "[Buddy加油站] %s · %s" % (title, stamp),
                    "签到：%s\n\n猫猫：%s" % ("\n".join(checkin["lines"]),
                                              "\n".join(cat["lines"]))))
            except Exception as e:  # noqa: BLE001
                notices.append("邮件推送失败：%s" % e)
        body["notices"] = notices
    else:
        body["notices"] = ["(--no-notify：已跳过推送)"]

    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 需求 4：签到成功就算成功，猫猫失败不改退出码
    return 0 if checkin["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
