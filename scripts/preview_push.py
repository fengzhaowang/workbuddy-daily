#!/usr/bin/env python3
"""推送正文预览：直接看每条渠道最终收到的内容（不联网、不发消息）。

改排版 / 改文案时先跑这个，比真发一条再回手机上看快得多。
正文里**行尾那两个空格是 markdown 的硬换行标记**（渲染出来就是换行），
肉眼看不见，所以这里统一显示成 `··`——真实内容里就是「两个空格 + 换行」。

用法：
  python3 scripts/preview_push.py                    # 四个账号的整轮（默认）
  python3 scripts/preview_push.py --single            # 单账号（各人收自己那份的样子）
  python3 scripts/preview_push.py --cat-only          # 只收猫那一档（签到段不显示）
  python3 scripts/preview_push.py --newline blank     # 换行风格：space(默认)/blank/lf
  python3 scripts/preview_push.py --raw               # 原样输出（不把行尾空格显示成 ··）
  python3 scripts/preview_push.py --json              # 顺带打印结果 JSON 的结构
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daily  # noqa: E402


def _acc(name: str, ck: dict, cat: dict) -> dict:
    """按 run_one() 的返回结构造一份假结果（字段名与真实一致）。"""
    return {"name": name, "uid": "***", "checkin_ok": ck["ok"], "cat_ok": cat["ok"],
            "checkin": ck, "cat": cat, "_notify": {}, "_notify_problems": [],
            "_declared_notify": False}


def _ck(result: str, ok: bool, lines: list[str], **extra) -> dict:
    d = {"ok": ok, "segment": "签到", "result": result, "lines": lines}
    d.update(extra)
    return d


def _cat(result: str, ok: bool, lines: list[str], **extra) -> dict:
    d = {"ok": ok, "segment": "猫猫旅行", "result": result, "lines": lines}
    d.update(extra)
    return d


def samples() -> list[dict]:
    """四种典型形态各来一个：已签 / 新签 / token 失效 / 猫在路上。"""
    return [
        _acc("冯召旺",
             _ck("ALREADY", True, ["✅ 今天已经签过了（今日 100 · 连续 3 天 · 累计 300）"],
                 credit=None, today_credit=100),
             _cat("CLAIMED", True, ["猫咪 龙焰喵（SSR）", "🎁 领到旅行积分 +6",
                                    "🛑 今天已经派过了（每天一趟）"],
                  buddy="龙焰喵（SSR）", reward=6)),
        _acc("李灏然",
             _ck("CLAIMED", True, ["✅ 签到成功 +6 积分（今日 6 · 连续 1 天 · 累计 6）"],
                 credit=6, today_credit=6),
             _cat("DEPARTED", True, ["猫咪 奶油喵（SR）", "🚀 派出猫猫 → 图书馆（3 小时后回）"],
                  buddy="奶油喵（SR）", departed="图书馆")),
        _acc("栋哥",
             _ck("CLAIMED", True, ["✅ 签到成功 +5 积分（今日 5 · 连续 2 天 · 累计 11）"],
                 credit=5, today_credit=5),
             _cat("TRAVELING", True, ["猫咪 布丁喵（R）", "🐱 在路上 → 咖啡馆，约 42 分钟后回"],
                  buddy="布丁喵（R）", location="咖啡馆")),
        _acc("于得水",
             _ck("AUTH", False, ["❌ 登录态失效（HTTP 401）：该账号的 token 已过期，需在本机重新导出"]),
             _cat("AUTH", False, ["⚠️ 登录态失效（HTTP 401），本段跳过"])),
    ]


# 换行标记：把「行尾两个空格 + 换行」显示成「·· + 换行」，否则肉眼看不出来
def _visible(text: str, show: bool) -> str:
    return text.replace("  \n", "··\n") if show else text


def _card_lines(card_payload: dict) -> str:
    """把飞书卡片里真正会被渲染的文本抽出来看（不然只能对着 JSON 想象）。"""
    out: list[str] = []
    for el in card_payload["card"]["elements"]:
        if el.get("tag") == "div":
            for f in el.get("fields") or []:
                out.append(f["text"]["content"])
        elif el.get("tag") == "hr":
            out.append("———— （分隔线）————")
        elif el.get("tag") == "note":
            out.append("（底部小字）" + "".join(
                e.get("content", "") for e in el.get("elements") or []))
    return "\n\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="预览推送正文（不发消息）")
    ap.add_argument("--single", action="store_true", help="只预览单账号（各人收自己那份）")
    ap.add_argument("--cat-only", action="store_true", help="预览只收猫那一档")
    ap.add_argument("--newline", choices=sorted(daily.NEWLINE_STYLES),
                    default=os.environ.get("WB_PUSH_NEWLINE") or daily.DEFAULT_NEWLINE,
                    help="换行风格（默认 %s，也可用环境变量 WB_PUSH_NEWLINE）" % daily.DEFAULT_NEWLINE)
    ap.add_argument("--raw", action="store_true", help="原样输出，不把行尾空格显示成 ··")
    ap.add_argument("--json", action="store_true", help="顺带打印结果 JSON 的结构")
    args = ap.parse_args()

    os.environ["WB_PUSH_NEWLINE"] = args.newline
    show = not args.raw

    results = samples()
    if args.single:
        results = results[:1]
    if args.cat_only:
        for r in results:
            r["checkin"] = _ck("SKIPPED", False, ["（跳过）"])
            r["checkin_ok"] = False
            r["cat_ok"] = True

    note = "ℹ️ 本轮比计划晚 301 分钟触发（GitHub 定时器延迟，属正常）"
    secs = daily._sections_of(results, note)
    stamp = "2026-10-09 10:49"

    print("=" * 68)
    print("通知标题（手机通知栏那一行）")
    print("=" * 68)
    print("  汇总：" + daily._push_title(results, stamp, "prod"))
    print("  单人：" + daily._push_title(results[:1], stamp, "prod"))
    print()

    print("=" * 68)
    print("① markdown 渠道：企业微信 / 钉钉 / Server酱 / PushPlus")
    print("   （换行风格 = %s）" % args.newline)
    print("=" * 68)
    print(_visible(daily._fmt_sections(secs), show))
    print()

    print("=" * 68)
    print("② 纯文本渠道：Bark / ntfy / Telegram / 邮件")
    print("   （这几家 \\n 就是硬换行，不参与上面的换行取舍）")
    print("=" * 68)
    print(_visible(daily._fmt_sections(secs, md=False), show))
    print()

    print("=" * 68)
    print("③ 飞书卡片（lark_md，单换行即换行）")
    print("=" * 68)
    ok = all(daily._account_ok(r) for r in results)
    card = daily.build_feishu_card(daily._push_title(results, stamp, "prod"), secs, ok)
    print("  标题条：%s（%s）" % (card["card"]["header"]["title"]["content"],
                                 card["card"]["header"]["template"]))
    print()
    print(_visible(_card_lines(card), show))
    print()

    print("=" * 68)
    print("④ 换行风格对照（同一个猫猫段，三种风格分别长什么样）")
    print("=" * 68)
    one = daily._sections_of(results[:1], note)
    for mode in ("space", "blank", "lf"):
        os.environ["WB_PUSH_NEWLINE"] = mode
        body = daily._fmt_sections(one)
        mark = "  <- 当前默认" if mode == daily.DEFAULT_NEWLINE else ""
        print("--- WB_PUSH_NEWLINE=%s%s" % (mode, mark))
        print(_visible(body, True))
        print()

    if args.json:
        print("=" * 68)
        print("⑤ 结果 JSON 结构（`daily.py` 打到 Actions 日志里的形状）")
        print("=" * 68)
        body = {"ok": all(daily._account_ok(r) for r in results), "timestamp": stamp,
                "env": "prod", "segment": "cat" if args.cat_only else "all",
                "summary": {"total": len(results),
                            "checkin_ok": sum(1 for r in results if r["checkin_ok"]),
                            "cat_ok": sum(1 for r in results if r["cat_ok"]),
                            "failed": [r["name"] for r in results
                                       if not daily._account_ok(r)]},
                "accounts": daily._public(results)}
        print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
