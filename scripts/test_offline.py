#!/usr/bin/env python3
"""离线端到端自测：不开外网、不发真通知，把整条链路跑一遍。

为什么要它：改完通知分发逻辑，「看起来对」不等于「真的对」。
这里起一个本机假服务端同时扮演**两个角色**：

  1. WorkBuddy 的接口（签到 / 猫猫旅行）—— 让 run_one() 走完正常路径；
  2. 各通知渠道的接收端 —— 把真实发出去的报文体记下来。

然后用断言检查「谁的内容发到了哪个地址」，尤其是：
  * 自带渠道的人**只**收到自己那一份，且不出现在汇总卡片里；
  * 借全局渠道的人也是单独一份；
  * 没配的人汇总成一份发到全局渠道；
  * 配置不完整的人**不**被静默塞进汇总（会明确报一句问题）。

跑法：python3 scripts/test_offline.py      （退出码 0 = 全部通过）
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daily  # noqa: E402

SEEN: list[dict] = []          # 假服务端收到的每一次请求


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # 静音，别把测试输出淹掉
        pass

    def _reply(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._handle("GET", None)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        self._handle("POST", raw)

    def _handle(self, method: str, raw: str) -> None:
        SEEN.append({"method": method, "path": self.path, "body": raw})
        p = self.path.split("?")[0]

        # ---- 真接口的假实现 ----
        if p == daily.P_CHECKIN_STATUS:
            return self._reply({"code": 0, "data": {
                "active": True, "theme_name": "加油站", "today_checked_in": False,
                "today_credit": 0, "streak_days": 3, "total_credits": 300}})
        if p == daily.P_CHECKIN_CLAIM:
            return self._reply({"code": 0, "data": {"credit": 100}})
        if p == daily.P_BUDDY_INFO:
            return self._reply({"code": 0, "data": {"buddy": {"name": "龙焰喵", "rarity": "SSR"}}})
        if p == daily.P_TRAVEL_STATUS:
            return self._reply({"code": 0, "data": {"state": "idle", "record_id": None}})
        if p == daily.P_TRAVEL_CONFIG:
            return self._reply({"code": 0, "data": {"locations": [
                {"id": 7, "name": "咖啡馆", "duration_hours_min": 1}]}})
        if p == daily.P_TRAVEL_CLAIM:
            return self._reply({"code": 0, "data": {"reward_credit": 6}})
        if p == daily.P_TRAVEL_DEPART:
            return self._reply({"code": 0, "data": {
                "location": {"name": "咖啡馆"}, "duration_hours": 4}})

        # ---- 通知渠道的假接收端：一律回 code=0（各家的成功码） ----
        if p.startswith("/push/"):
            return self._reply({"code": 0, "errcode": 0, "ok": True, "message": "ok"})

        return self._reply({"code": 404, "msg": "route not found"}, 404)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def bodies(path: str) -> list[str]:
    return [s["body"] for s in SEEN if s["path"].split("?")[0] == path]


def check(label: str, cond: bool, detail: str = "") -> bool:
    print("  %s %s%s" % ("✅" if cond else "❌", label, "" if cond else "  <- %s" % detail))
    return cond


def main() -> int:
    srv, base = start_server()
    ok = True
    try:
        # ---------- 场景：4 个人，3 种去向 ----------
        #  甲：自带企业微信机器人        -> /push/jia
        #  乙：只写渠道名 dingtalk       -> 借全局 -> /push/ding
        #  丙：没配                      -> 汇总卡片 -> 全局（/push/ding、/push/sct）
        #  丁：邮箱配了一半              -> 只报问题，不补发、不混进汇总
        os.environ.pop("NOTIFY_CHANNELS", None)
        os.environ["DINGTALK_WEBHOOK"] = base + "/push/ding"
        os.environ["SERVERCHAN_KEY"] = base + "/push/sct"
        os.environ.pop("SMTP_HOST", None)

        raw = {"accounts": [
            {"name": "甲", "token": "t1", "uid": "u1", "endpoint": base,
             "notify": {"wecom": base + "/push/jia"}},
            {"name": "乙", "token": "t2", "uid": "u2", "endpoint": base,
             "notify": ["dingtalk"]},
            {"name": "丙", "token": "t3", "uid": "u3", "endpoint": base},
            {"name": "丁", "token": "t4", "uid": "u4", "endpoint": base,
             "notify": {"email": {"host": "smtp.example.com"}}},
        ]}
        accounts = daily._parse_accounts(raw, base)
        daily.set_budget(60)

        print("① 每个账号跑一遍（签到 + 猫猫）")
        results = [daily.run_one(a) for a in accounts]
        for r in results:
            ok &= check("%s 签到成功（%s）" % (r["name"], r["checkin"]["result"]),
                        r["checkin_ok"], json.dumps(r["checkin"], ensure_ascii=False))
        ok &= check("猫猫派发成功",
                    all(r["cat"]["result"] == "DEPARTED" for r in results),
                    json.dumps([r["cat"]["result"] for r in results], ensure_ascii=False))

        print("② 推送分发")
        notices = daily.notify(results, "2026-01-01 00:00", "test")
        for n in notices:
            print("     · %s" % n)

        jia = bodies("/push/jia")
        ding = bodies("/push/ding")
        sct = bodies("/push/sct")
        ok &= check("甲：自己的企业微信只收到 1 条", len(jia) == 1, str(len(jia)))
        ok &= check("甲：内容里有甲、没有别人", "甲" in jia[0] and "丙" not in jia[0], jia[0] if jia else "无请求")
        ok &= check("乙：借全局钉钉，单独一条（含乙不含丙）",
                    any("乙" in b and "丙" not in b for b in ding), str(ding))
        ok &= check("丙：汇总卡片发到了全局两个渠道",
                    any("丙" in b for b in ding) and len(sct) == 1, str([len(ding), len(sct)]))
        # 汇总卡片 = 含「丙」的那些报文（钉钉那条同时还有乙的单发，要分开看）
        aggregate = [b for b in ding + sct if "丙" in b]
        ok &= check("汇总卡片正好 2 份（钉钉 + Server酱），且里面只有丙",
                    len(aggregate) == 2
                    and all("丙" in b and not any(x in b for x in ("甲", "乙", "丁"))
                            for b in aggregate),
                    str(aggregate))
        ok &= check("丁：邮件没配全 -> 不回退进汇总，改为明确报问题",
                    any("丁" in n and "邮件" in n for n in notices))
        ok &= check("没有出现「未配置任何推送渠道」的误报",
                    not any("未配置任何推送渠道" in n for n in notices))

        print("③ 脱敏")
        dumped = json.dumps(daily._public(results), ensure_ascii=False)
        ok &= check("输出里不含账号自带的渠道凭据", "/push/jia" not in dumped and "t1" not in dumped)

        print("④ 通知渠道确实被调用（假服务端收到 %d 个推送请求）" % (len(jia + ding + sct)))
        ok &= check("推送请求数 = 4（甲 1 / 乙 1 / 汇总 2）",
                    len(jia) + len(ding) + len(sct) == 4, str(len(jia) + len(ding) + len(sct)))

        print("⑤ NOTIFY_CHANNELS 只该管全局那一层")
        SEEN.clear()
        os.environ["NOTIFY_CHANNELS"] = "serverchan"      # 全局只留 Server酱
        daily.notify(results, "2026-01-01 00:00", "test")
        jia2, ding2, sct2 = bodies("/push/jia"), bodies("/push/ding"), bodies("/push/sct")
        ok &= check("全局被点名为 serverchan -> 汇总只走 Server酱，钉钉那条不再发",
                    len(sct2) == 1 and not any("丙" in b for b in ding2),
                    str([len(sct2), len(ding2)]))
        ok &= check("各人自己的渠道不受 NOTIFY_CHANNELS 影响（甲企微 / 乙钉钉照发）",
                    len(jia2) == 1 and any("乙" in b for b in ding2),
                    str([len(jia2), [b[:40] for b in ding2]]))
        os.environ.pop("NOTIFY_CHANNELS", None)

        print("⑥ 静默忽略检查 + 「没配渠道」要说清是谁")
        # 6.1 清单里有本版代码不认识的字段，必须被点名。
        #     这次的线上事故就是「清单比代码新」-> notify 被旧代码静默吞掉，
        #     人只看到「我明明配了怎么没生效」。这条断言守住它。
        future = daily._to_account(
            {"name": "戊", "token": "t5", "uid": "u5", "notifyFromFuture": {"x": 1}}, 1, base)
        ok &= check("清单里的未知字段被识别出来（不再静默忽略）",
                    future.unknown == ["notifyFromFuture"], json.dumps(future.unknown))
        known = daily._to_account(
            {"name": "己", "token": "t6", "uid": "u6", "notify": ["dingtalk"],
             "added_at": 1, "expires_at": 2, "domain": "d", "feishu_webhook": "https://x"}, 1, base)
        ok &= check("已知字段 / 本机元数据不误报", known.unknown == [], json.dumps(known.unknown))

        # 6.2 谁都没配 notify、全局也没渠道 -> 提示里必须点名是哪几个人
        os.environ.pop("DINGTALK_WEBHOOK", None)
        os.environ.pop("SERVERCHAN_KEY", None)
        plain = daily._parse_accounts({"accounts": [
            {"name": "庚", "token": "t7", "uid": "u7", "endpoint": base},
            {"name": "辛", "token": "t8", "uid": "u8", "endpoint": base}]}, base)
        n2 = daily.notify([daily.run_one(a) for a in plain], "2026-01-01 00:00", "test")
        msg = next((n for n in n2 if "未配置任何推送渠道" in n), "")
        ok &= check("提示点名了受影响的账号（庚、辛）且给了下一步",
                    "庚" in msg and "辛" in msg and "--push" in msg and "提交" in msg, msg)
    finally:
        srv.shutdown()

    print()
    print("✅ 全部通过" if ok else "❌ 有用例失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
