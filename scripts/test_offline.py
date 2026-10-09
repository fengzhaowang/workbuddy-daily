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
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daily  # noqa: E402

SEEN: list[dict] = []          # 假服务端收到的每一次请求
# 猫猫旅行状态可切换，用来演「还在路上」和「已到家」两种情形
TRAVEL: dict = {"state": "idle", "record_id": None}


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
            return self._reply({"code": 0, "data": dict(TRAVEL)})
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


def run_cli(argv: list) -> dict:
    """按命令行方式真跑一次 daily.main()，把 stdout 里的 JSON 收回来。

    走命令行而不是直接调函数，是为了连「参数有没有真的接上」一起验——
    「写了函数」≠「接上了」是这个地方最容易犯的错。
    """
    import contextlib
    import io

    buf = io.StringIO()
    saved = sys.argv
    sys.argv = ["daily.py"] + list(argv)
    try:
        with contextlib.redirect_stdout(buf):
            rc = daily.main()
    finally:
        sys.argv = saved
    out = buf.getvalue().strip()
    return {"rc": rc, "json": json.loads(out) if out.startswith("{") else {}, "stdout": out}


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

        print("⑦ --cat-only：只收猫（六小时一档），不碰签到接口")
        os.environ.pop("DINGTALK_WEBHOOK", None)
        os.environ.pop("SERVERCHAN_KEY", None)
        os.environ["WB_ACCOUNTS"] = json.dumps({"accounts": [
            {"name": "壬", "token": "t9", "uid": "u9", "endpoint": base,
             "notify": {"wecom": base + "/push/ren"}}]}, ensure_ascii=False)

        # ① 猫还在路上：没东西可领，不该推送（一天四遍「猫还在路上」是噪音）
        SEEN.clear()
        TRAVEL["state"], TRAVEL["record_id"] = "traveling", None
        body = run_cli(["--cat-only"])
        touched = {s["path"].split("?")[0] for s in SEEN}
        ok &= check("没调用签到接口（--cat-only 真的跳过了签到段）",
                    daily.P_CHECKIN_STATUS not in touched
                    and daily.P_CHECKIN_CLAIM not in touched, str(sorted(touched)))
        ok &= check("猫还在路上 -> 0 个推送请求", bodies("/push/ren") == [],
                    str(bodies("/push/ren")))
        ok &= check("无变化时把原因写进 notices",
                    any("无变化" in n for n in body["json"].get("notices", [])),
                    str(body["json"].get("notices")))
        ok &= check("退出码看猫猫段（路上 = 成功 = 0）",
                    body["rc"] == 0 and body["json"].get("ok") is True,
                    "rc=%s ok=%s" % (body["rc"], body["json"].get("ok")))
        ok &= check('结果里标明 segment="cat"',
                    body["json"].get("segment") == "cat", str(body["json"].get("segment")))

        # ② 猫已到家：领积分，并且因为 do_cat 是「先领后派」的闭环，
        #    领完会立刻再派一趟 —— 不然猫就闲着，积分转不起来
        SEEN.clear()
        TRAVEL["state"], TRAVEL["record_id"] = "arrived", "r1"
        body = run_cli(["--cat-only"])
        ok &= check("已到家 -> 调了领取接口", len(bodies(daily.P_TRAVEL_CLAIM)) == 1,
                    str(bodies(daily.P_TRAVEL_CLAIM)))
        ok &= check("领完立刻又派了一趟（猫不会闲着）",
                    len(bodies(daily.P_TRAVEL_DEPART)) == 1,
                    str(bodies(daily.P_TRAVEL_DEPART)))
        ok &= check("有收获 -> 推送 1 条", len(bodies("/push/ren")) == 1,
                    str(len(bodies("/push/ren"))))
        ok &= check("推送里写明领到多少积分",
                    "领到旅行积分 +" in (bodies("/push/ren") or [""])[0],
                    (bodies("/push/ren") or [""])[0][:160])

        TRAVEL["state"], TRAVEL["record_id"] = "idle", None
        os.environ.pop("WB_ACCOUNTS", None)

        print("⑧ 定时档位：cron 与档位表必须对得上（防「改了 cron 忘了改档位」）")
        root = os.path.dirname(os.path.dirname(os.path.abspath(daily.__file__)))
        with open(os.path.join(root, ".github/workflows/daily.yml"), encoding="utf-8") as fp:
            wf_text = fp.read()
        crons = re.findall(r'cron:\s*"([^"]+)"', wf_text)
        ok &= check("能从 workflow 里读出 cron 列表", len(crons) >= 2, str(crons))
        missing = [c for c in crons if c not in daily.SCHEDULE_MODES]
        ok &= check("每条 cron 都在 SCHEDULE_MODES 里（漏了就会把收猫档当整轮跑）",
                    not missing, str(missing))
        stale = [c for c in daily.SCHEDULE_MODES if c not in crons]
        ok &= check("SCHEDULE_MODES 里没有 workflow 已删掉的 cron",
                    not stale, str(stale))
        ok &= check("workflow 不再自带一份 cron->模式 映射（只留脚本里一处真相）",
                    'case "${TRIGGER_CRON' not in wf_text and 'MODE="cat"' not in wf_text
                    and "--mode" in wf_text and "TRIGGER_CRON:" in wf_text, "")

        print("⑨ 延迟自证：GitHub 的 cron 晚多久，要算得出来、说得明白")
        # 算例就是本次线上事故：计划北京 00:20，实际北京 05:21 才触发
        rep = daily.cron_report("20 16 * * *", datetime(2026, 10, 8, 21, 21, tzinfo=timezone.utc))
        ok &= check("算得出计划时刻（北京 00:20）",
                    rep["planned_utc"] == "10-08 16:20" and rep["planned_local"] == "10-09 00:20",
                    json.dumps(rep, ensure_ascii=False))
        ok &= check("算得出实际时刻（北京 05:21）", rep["actual_local"] == "10-09 05:21",
                    str(rep["actual_local"]))
        ok &= check("延迟 = 301 分钟", rep["delay_minutes"] == 301, str(rep["delay_minutes"]))
        note = daily.late_note(rep)
        ok &= check("延迟说明点明「GitHub 定时器」与两个时刻",
                    "GitHub" in note and "00:20" in note and "05:21" in note
                    and "5 小时 1 分" in note, note)
        ontime = daily.cron_report("20 16 * * *", datetime(2026, 10, 8, 16, 20, tzinfo=timezone.utc))
        ok &= check("准点时不产生说明（不打扰人）",
                    ontime["delay_minutes"] == 0 and daily.late_note(ontime) == "",
                    str(ontime["delay_minutes"]))
        ok &= check("认不出的 cron：报警，且按整轮跑（安全的一边）",
                    "不在已知列表" in (daily.cron_report("30 3 * * 1").get("warning") or "")
                    and daily.cron_report("30 3 * * 1").get("plan") is None, "")
        ok &= check("手动触发（无 cron）不报「延迟」",
                    daily.cron_report("")["delay_minutes"] is None
                    and "手动触发" in daily.cron_report("")["reason"], "")

        print("⑩ 延迟说明要进推送正文，且不许打乱账号编号")
        one = [{"name": "癸", "checkin_ok": True, "cat_ok": True,
                "checkin": {"result": "ALREADY", "lines": ["✅ 已签"]},
                "cat": {"result": "TRAVELING", "lines": ["🐾 在路上"]},
                "_notify": {}, "_declared_notify": False, "_notify_problems": []}]
        os.environ["SERVERCHAN_KEY"] = base + "/push/sct2"
        SEEN.clear()
        daily.notify(one, "2026-01-01 00:00", "test", note="⏱ 说明占位")
        pushed = bodies("/push/sct2")
        ok &= check("说明出现在推送正文里",
                    len(pushed) == 1 and "⏱ 说明占位" in pushed[0], str(pushed)[:200])
        ok &= check("单账号 + 一行说明，不该被编号成「1. 癸」",
                    "1. 癸" not in (pushed[0] if pushed else ""),
                    (pushed[0] if pushed else "")[:120])
        card = json.dumps(daily.build_feishu_card(
            "标题", daily._sections_of(one, "⏱ 说明占位"), True), ensure_ascii=False)
        ok &= check("飞书卡片同样带说明、同样不编号",
                    "⏱ 说明占位" in card and "1. 癸" not in card and "🏠 **签到**" in card, "")
        os.environ.pop("SERVERCHAN_KEY", None)
        SEEN.clear()

        print("⑪ 档位判定真的接上了命令行（TRIGGER_CRON -> 跑哪一段）")
        os.environ["WB_ACCOUNTS"] = json.dumps({"accounts": [
            {"name": "癸", "token": "t10", "uid": "u10", "endpoint": base,
             "notify": {"wecom": base + "/push/gui"}}]}, ensure_ascii=False)
        TRAVEL["state"], TRAVEL["record_id"] = "traveling", None

        SEEN.clear()
        os.environ["TRIGGER_CRON"] = "20 22,4,10 * * *"
        body = run_cli(["--mode", "auto"])
        touched = {s["path"].split("?")[0] for s in SEEN}
        ok &= check("收猫档的 cron -> segment=cat，且完全不碰签到接口",
                    body["json"].get("segment") == "cat"
                    and daily.P_CHECKIN_STATUS not in touched
                    and daily.P_CHECKIN_CLAIM not in touched, str(sorted(touched)))

        SEEN.clear()
        os.environ["TRIGGER_CRON"] = "20 16 * * *"
        body = run_cli(["--mode", "auto"])
        touched = {s["path"].split("?")[0] for s in SEEN}
        ok &= check("整轮档的 cron -> segment=all，会碰签到接口",
                    body["json"].get("segment") == "all"
                    and daily.P_CHECKIN_STATUS in touched, str(sorted(touched)))

        SEEN.clear()
        os.environ["TRIGGER_CRON"] = "7 7 * * *"
        body = run_cli(["--mode", "auto"])
        wj = body["json"]
        ok &= check("认不出的 cron：按整轮跑，并把原因写进 warnings + notices",
                    wj.get("segment") == "all"
                    and any("不在已知列表" in w for w in wj.get("warnings", []))
                    and any("不在已知列表" in n for n in wj.get("notices", [])),
                    json.dumps(wj.get("warnings"), ensure_ascii=False))

        os.environ.pop("TRIGGER_CRON", None)
        ok &= check("手动指定 --mode cat 生效",
                    run_cli(["--mode", "cat"])["json"].get("segment") == "cat")
        ok &= check("旧写法 --cat-only 仍等价于 --mode cat",
                    run_cli(["--cat-only"])["json"].get("segment") == "cat")
        body = run_cli([])
        ok &= check("无 cron、无参数 -> 整轮（默认走安全的一边）",
                    body["json"].get("segment") == "all", str(body["json"].get("segment")))
        ok &= check("结果 JSON 里有 cron 段（计划 / 实际 / 延迟）",
                    isinstance(body["json"].get("cron"), dict)
                    and "actual_utc" in body["json"]["cron"], "")

        print("⑫ 推送排版：换行要真的换行，内容要能一眼看懂")
        # 各家对 markdown 换行的支持不一样（企微/钉钉/Server酱会把单个 \n 折叠掉），
        # 而「会不会被折叠」可以精确判定：某一行后面紧跟另一个非空行、行尾又没补两个空格
        # —— 这个换行在渲染时就消失了。这是本项目最容易踩、又最难自查的一类问题，
        # 所以拿它当硬约束测出来（细节见 daily.py 里 NEWLINE_STYLES 的注释）。
        def folded(text: str) -> int:
            lines = text.split("\n")
            return sum(1 for i, l in enumerate(lines[:-1])
                       if l.strip() and lines[i + 1].strip() and not l.endswith("  "))

        multi = [
            {"name": "甲", "checkin_ok": True, "cat_ok": True,
             "checkin": {"result": "ALREADY", "today_credit": 100,
                         "lines": ["✅ 今天已经签过了（今日 100 · 连续 3 天 · 累计 300）"]},
             "cat": {"result": "CLAIMED", "reward": 6,
                     "lines": ["猫咪 龙焰喵（SSR）", "🎁 领到旅行积分 +6"]}},
            {"name": "乙", "checkin_ok": False, "cat_ok": True,
             "checkin": {"result": "AUTH", "lines": ["❌ 登录态失效（HTTP 401）"]},
             "cat": {"result": "TRAVELING", "location": "图书馆",
                     "lines": ["🐱 在路上 → 图书馆，约 6 分钟后回"]}},
        ]
        os.environ.pop("WB_PUSH_NEWLINE", None)
        secs = daily._sections_of(multi)
        body_md = daily._fmt_sections(secs)
        ok &= check("markdown 正文里没有会被折叠掉的换行（企微/钉钉/Server酱的坑）",
                    folded(body_md) == 0, "%d 处：%r" % (folded(body_md), body_md[:160]))
        ok &= check("纯文本正文不掺行尾空格（那边的 \\n 本来就是硬换行）",
                    "  \n" not in daily._fmt_sections(secs, md=False), "")
        ok &= check("总览行说清结论、并点名是谁有问题",
                    "✅ 签到 1/2" in body_md and "⚠️ 有问题：乙" in body_md, body_md[:120])
        ok &= check("总览行报出「收了几趟」「几只在路上」",
                    "🎁 收 1 趟积分" in body_md and "🐱 1 只在路上" in body_md, "")
        ok &= check("多账号有编号、单账号不编号",
                    "**1. 甲**" in body_md and "**2. 乙**" in body_md
                    and "1. 甲" not in daily._fmt_sections(daily._sections_of(multi[:1])), "")
        ok &= check("标题：多账号报人数、单账号报名字",
                    daily._push_title(multi, "2026-10-09 10:49", "prod")
                    == "Buddy 加油站日报 · 2 个账号 · 10-09 10:49"
                    and daily._push_title(multi[:1], "2026-10-09 10:49", "prod")
                    == "Buddy 加油站日报 · 甲 · 10-09 10:49",
                    daily._push_title(multi, "2026-10-09 10:49", "prod"))

        cat_only = [dict(multi[0], checkin={"result": "SKIPPED", "lines": ["（跳过）"]})]
        ok &= check("收猫轮次的标题标明「收猫」",
                    "收猫" in daily._push_title(cat_only, "2026-10-09 10:49", "prod"), "")
        ok &= check("收猫轮次不显示「🏠 签到」空壳段、总览里也不提签到",
                    "🏠" not in daily._fmt_sections(daily._sections_of(cat_only))
                    and "签到" not in daily._overview(cat_only), "")
        card = daily.build_feishu_card("t", secs, True)
        card_texts = [f["text"]["content"]
                      for el in card["card"]["elements"] if el.get("tag") == "div"
                      for f in el.get("fields") or []]
        ok &= check("飞书卡片走紧凑的 lark 风格（它认单换行），且不再塞开发者说明",
                    any("🏠 **签到**\n" in t for t in card_texts)
                    and any("✅ 签到 1/2" in t for t in card_texts)
                    and "各账号互相隔离" not in json.dumps(card, ensure_ascii=False),
                    str(card_texts)[:160])

        os.environ["WB_PUSH_NEWLINE"] = "lf"
        ok &= check("WB_PUSH_NEWLINE=lf 回到单换行（确认客户端认它时才用）",
                    folded(daily._fmt_sections(secs)) > 0, "")
        os.environ["WB_PUSH_NEWLINE"] = "blank"
        blank = daily._fmt_sections(secs)
        ok &= check("WB_PUSH_NEWLINE=blank 全部用空行（最保守，一定换行）",
                    folded(blank) == 0 and "\n\n" in blank and "  \n" not in blank, "")
        os.environ.pop("WB_PUSH_NEWLINE", None)

        os.environ.pop("WB_ACCOUNTS", None)
        TRAVEL["state"], TRAVEL["record_id"] = "idle", None
    finally:
        srv.shutdown()

    print()
    print("✅ 全部通过" if ok else "❌ 有用例失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
