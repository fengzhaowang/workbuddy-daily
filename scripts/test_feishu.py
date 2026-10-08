#!/usr/bin/env python3
"""飞书自定义机器人自检（本机跑，不依赖 WorkBuddy 登录态）。

把「真实的日报卡片」原样发一次，确认机器人配置没问题。
用的是 daily.py 里**同一个**构造函数，所以这里能通，Actions 里就一定能通——
避免「自检能过、线上不发」这种最难查的偏差。

用法：
  python3 scripts/test_feishu.py --webhook '<Webhook 地址>'
  python3 scripts/test_feishu.py --webhook '<Webhook 地址>' --secret '<签名密钥>'
  python3 scripts/test_feishu.py --webhook '<Webhook 地址>' --accounts 3   # 预览多账号卡片
  FEISHU_WEBHOOK=... FEISHU_SECRET=... python3 scripts/test_feishu.py

注意：会在飞书群里真的发出一条消息（这就是目的）。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from daily import build_feishu_card  # noqa: E402

SAMPLE_CHECKIN = "✅ 今日已签过（今日 100，连续 3 天，累计 300）"
SAMPLE_CAT = ["🐾 猫咪：龙焰喵（SSR）", "🐱 猫猫旅行中：咖啡馆，约 6 分钟后回"]

# 飞书自定义机器人的常见错误码 → 人话
HINTS = {
    19001: "参数格式被拒（卡片结构是固定的，一般不会遇到）",
    19021: "签名校验失败 → 机器人开了「签名校验」，但密钥不对，或漏传了 --secret",
    19022: ("IP 不在白名单 → GitHub runner 的出口 IP 是动态的，这个模式必然失败。"
            "请把安全设置改为「签名校验」或「自定义关键词」"),
    19024: "关键词不匹配 → 你选的是「自定义关键词」，把关键词改成「加油站」即可",
    9499: "请求被拒（多半仍是签名或安全设置的问题）",
}


def sample_sections(count: int) -> list[dict]:
    names = ["我的账号", "小号", "同事的号", "账号4", "账号5"]
    out = []
    for i in range(max(1, count)):
        out.append({
            "name": names[i] if i < len(names) else "账号%d" % (i + 1),
            "checkin": [SAMPLE_CHECKIN if i % 2 == 0 else "✅ 签到成功，+100 积分（今日 100，连续 1 天）"],
            "cat": SAMPLE_CAT if i % 2 == 0 else ["🐾 猫咪：龙焰喵（SSR）", "🚀 派出猫猫去咖啡馆（1 小时后回）"],
        })
    return out


def post(webhook: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        webhook, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20,
                                    context=ssl.create_default_context()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, "%s: %s" % (type(e).__name__, e)


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书自定义机器人自检")
    ap.add_argument("--webhook", default=os.environ.get("FEISHU_WEBHOOK", ""))
    ap.add_argument("--secret", default=os.environ.get("FEISHU_SECRET", ""))
    ap.add_argument("--accounts", type=int, default=2,
                    help="预览几个账号的卡片（默认 2；填 1 可看单账号样式）")
    ap.add_argument("--no-secret", action="store_true",
                    help="即使有密钥也不签名（用来测没开签名校验的机器人）")
    args = ap.parse_args()

    wh = (args.webhook or "").strip()
    if not wh:
        print("❌ 没给 Webhook。加 --webhook '<地址>'，或设 FEISHU_WEBHOOK 环境变量。",
              file=sys.stderr)
        return 2
    if not wh.startswith("https://"):
        print("⚠️  Webhook 不是 https 开头，飞书一般只认 https，先确认地址没贴错。",
              file=sys.stderr)

    secret = "" if args.no_secret else (args.secret or "").strip()
    sections = sample_sections(args.accounts)
    payload = build_feishu_card("2026-10-08 11:30", "test", sections, True, secret)

    print("→ 即将向群里发送测试卡片：")
    for i, s in enumerate(sections, 1):
        print("   账号 %d「%s」" % (i, s["name"]))
        print("     签到：%s" % s["checkin"][0])
        print("     猫猫：%s" % s["cat"][-1])
    print("   签名  ：%s" % ("已附带（timestamp + sign）" if secret else "未附带"))
    print()

    code, raw = post(wh, payload)
    print("← HTTP %s" % code)
    print(raw[:600])
    print()

    ok = False
    try:
        j = json.loads(raw)
        ec = j.get("code", j.get("StatusCode"))
        ok = ec in (0, None) and j.get("StatusCode") in (0, None)
        if not ok:
            print("❌ 失败：飞书返回错误码 %s（%s）"
                  % (ec, j.get("msg") or j.get("StatusMessage") or "无描述"))
            hint = HINTS.get(ec)
            if hint:
                print("   可能原因：%s" % hint)
    except ValueError:
        print("❌ 返回的不是 JSON。检查 Webhook 地址是否完整（有没有漏掉尾部字符），"
              "或这台机器能不能正常访问外网。")

    if not ok:
        return 1

    print("✅ 机器人通了。这条卡片长什么样，群里收到的就是什么样。")
    print()
    print("下一步：把它写进仓库 secret")
    print("  gh secret set FEISHU_WEBHOOK --repo <owner/name>")
    if secret:
        print("  gh secret set FEISHU_SECRET  --repo <owner/name>")
    else:
        print("  （如果机器人开了「签名校验」，还得再加 FEISHU_SECRET）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
