#!/usr/bin/env python3
"""通知渠道自检（本机跑，不依赖 WorkBuddy 登录态）。

把「真实的日报内容」原样发一次，确认这个渠道配置对不对。
用的是 daily.py 里**同一条发送路径**（daily._dispatch），所以这里能通，
Actions 里就一定能通——避免「自检能过、线上不发」这种最难查的偏差。

用法（凭据优先从环境变量读，跟线上 secret 同名；也可用命令行覆盖）：

  # 飞书
  python3 scripts/test_notify.py --channel feishu --webhook '<Webhook>' [--secret '<签名密钥>']

  # 企业微信
  python3 scripts/test_notify.py --channel wecom --webhook '<Webhook>'

  # 钉钉（安全设置选了「加签」就把密钥带上）
  python3 scripts/test_notify.py --channel dingtalk --webhook '<Webhook>' --secret '<加签密钥>'

  # Server酱 / PushPlus（都推微信，零门槛）
  python3 scripts/test_notify.py --channel serverchan --key '<SendKey>'
  python3 scripts/test_notify.py --channel pushplus   --token '<Token>'

  # Bark（iOS）/ ntfy / Telegram
  python3 scripts/test_notify.py --channel bark     --key '<Key>' [--server 'https://api.day.app']
  python3 scripts/test_notify.py --channel ntfy     --topic '<topic>' [--server 'https://ntfy.sh']
  python3 scripts/test_notify.py --channel telegram --token '<BotToken>' --chat-id '<ChatID>'

  # 邮件
  python3 scripts/test_notify.py --channel email --smtp-host smtp.qq.com --smtp-user me@qq.com \
      --smtp-pass '<授权码>' --mail-to me@qq.com

  # 一次测所有「已经配好的」渠道
  python3 scripts/test_notify.py

  # 只想看会发出去什么内容，不发
  python3 scripts/test_notify.py --channel dingtalk --print

注意：会真的发出一条通知消息（这就是目的）。密钥在输出里做过打码。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daily  # noqa: E402

SAMPLE_CHECKIN = "✅ 今日已签过（今日 100，连续 3 天，累计 300）"
SAMPLE_CAT = ["🐾 猫咪：龙焰喵（SSR）", "🐱 猫猫旅行中：咖啡馆，约 6 分钟后回"]

# 每个渠道：需要哪些环境变量（用来判断「配好了没」）
CHANNEL_ENV = {
    "feishu": ["FEISHU_WEBHOOK"],
    "wecom": ["WECOM_WEBHOOK"],
    "dingtalk": ["DINGTALK_WEBHOOK"],
    "serverchan": ["SERVERCHAN_KEY"],
    "pushplus": ["PUSHPLUS_TOKEN"],
    "bark": ["BARK_KEY"],
    "ntfy": ["NTFY_TOPIC"],
    "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
    "email": ["SMTP_HOST", "SMTP_USER", "SMTP_PASS"],
}

# 该渠道怎么拿凭据、怎么配安全设置
HOWTO = {
    "feishu": "飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook；"
              "安全设置建议选「签名校验」（选「IP 白名单」在 GitHub Actions 上必挂）",
    "wecom": "企业微信群 → 右上角 … → 群机器人 → 添加 → 复制 Webhook。"
             "不需要企业认证，自己建个只有自己的群就行",
    "dingtalk": "钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义 → 复制 Webhook；"
                "安全设置**必须选「加签」**，把密钥填到 DINGTALK_SECRET",
    "serverchan": "微信扫码登录 https://sct.ftqq.com → 复制 SendKey（形如 SCTxxxxx）；"
                  "关注「方糖」服务号后即可在微信收到",
    "pushplus": "微信扫码登录 https://www.pushplus.plus → 发送消息 → 复制 token",
    "bark": "iPhone 装 Bark App → 首页复制那个 https://api.day.app/xxxx 里的 xxxx",
    "ntfy": "手机装 ntfy App → 订阅一个 topic → 把 topic 名字填这里。"
            "topic 就是密码，取随机一点，别用 buddy-daily 这种能被猜到的",
    "telegram": "找 @BotFather 建 bot 拿 token；给 bot 发条消息后访问 "
                "https://api.telegram.org/bot<token>/getUpdates 看 chat.id",
    "email": "任何邮箱都行，密码填「SMTP 授权码」而不是登录密码。"
             "QQ 邮箱：设置 → 账户 → 开启 SMTP → 生成授权码，端口 465",
}

# 常见错误码 → 人话
HINTS = {
    # 飞书
    "19001": "参数格式被拒（卡片结构是固定的，一般不会遇到）",
    "19021": "签名校验失败 → 机器人开了「签名校验」但密钥不对，或漏传了 --secret",
    "19022": "IP 不在白名单 → GitHub runner 出口 IP 是动态的，这个模式必然失败。"
             "请改成「签名校验」或「自定义关键词」",
    "19024": "关键词不匹配 → 你选的是「自定义关键词」，把关键词改成「加油站」即可",
    "9499": "请求被拒（多半仍是签名或安全设置的问题）",
    # 企业微信
    "93000": "机器人 Webhook 无效 → 地址贴错或机器人已被移除",
    "45009": "接口调用超过限制 → 等一会儿再试",
    # 钉钉
    "310000": "安全设置不匹配 → 最常见的两种：① 选了「自定义关键词」但消息里没有该关键词"
              "（关键词填「加油站」）；② 选了「加签」但没填 DINGTALK_SECRET",
    "300001": "access_token 无效 → Webhook 地址贴错或机器人已停用",
    "130101": "发送频率超限 → 钉钉限制每分钟 20 条，稍后再试",
    # 通用凭据类
    "40001": "凭据不对（Server酱的 SendKey 无效，或企业微信 Webhook 的 key 不完整）",
    "50000": "内容被拒（含敏感词）或当日额度用尽",
    # Bark
    "400": "Bark 参数错误 → 检查 key 是否完整（不要带 https://api.day.app/ 前缀）",
    # Telegram
    "401": "Bot Token 无效",
    "403": "bot 没被允许给你发消息 → 先在 Telegram 里主动给 bot 发一条消息",
}


def configured(ch: str) -> tuple[bool, str]:
    missing = [k for k in CHANNEL_ENV.get(ch, []) if not (os.environ.get(k) or "").strip()]
    if missing:
        return False, "缺 %s" % " / ".join(missing)
    return True, ""


def resolve_channels(spec: str) -> list[str]:
    spec = (spec or "all").strip().lower()
    if spec == "all":
        # 自检里的 all = 「所有配了凭据的渠道」，不受线上 NOTIFY_CHANNELS 限制
        saved = os.environ.pop("NOTIFY_CHANNELS", None)
        try:
            return daily.detect_channels()
        finally:
            if saved is not None:
                os.environ["NOTIFY_CHANNELS"] = saved
    return [c.strip() for c in spec.replace(";", ",").split(",") if c.strip()]


def apply_cli(args: argparse.Namespace) -> None:
    """命令行参数写进环境变量，让 daily 的渠道函数按线上同样的方式取值。"""
    wanted = None if args.channel in ("", "all") else set(
        c.strip().lower() for c in args.channel.replace(";", ",").split(",") if c.strip())

    pairs = [
        (args.webhook, {"feishu": "FEISHU_WEBHOOK", "wecom": "WECOM_WEBHOOK",
                        "dingtalk": "DINGTALK_WEBHOOK"}),
        (args.secret, {"feishu": "FEISHU_SECRET", "dingtalk": "DINGTALK_SECRET"}),
        (args.key, {"serverchan": "SERVERCHAN_KEY", "bark": "BARK_KEY"}),
        (args.token, {"pushplus": "PUSHPLUS_TOKEN", "telegram": "TELEGRAM_BOT_TOKEN"}),
        (args.chat_id, {"telegram": "TELEGRAM_CHAT_ID"}),
        (args.topic, {"ntfy": "NTFY_TOPIC"}),
        (args.server, {"bark": "BARK_URL", "ntfy": "NTFY_URL"}),
        (args.smtp_host, {"email": "SMTP_HOST"}),
        (args.smtp_port, {"email": "SMTP_PORT"}),
        (args.smtp_user, {"email": "SMTP_USER"}),
        (args.smtp_pass, {"email": "SMTP_PASS"}),
        (args.mail_to, {"email": "MAIL_TO"}),
    ]
    for value, mapping in pairs:
        if not value:
            continue
        for ch, key in mapping.items():
            # 一个参数对应多个渠道时（如 --webhook），必须靠 --channel 指明是哪个；
            # --channel all 下只有唯一对应关系的参数才会生效，避免串味。
            if (wanted is not None and ch in wanted) or (wanted is None and len(mapping) == 1):
                os.environ[key] = str(value)
                break


def sample_sections(count: int) -> list[dict]:
    names = ["我的账号", "小号", "同事的号", "账号4", "账号5"]
    out = []
    for i in range(max(1, count)):
        out.append({
            "name": names[i] if i < len(names) else "账号%d" % (i + 1),
            "checkin": [SAMPLE_CHECKIN if i % 2 == 0
                        else "✅ 签到成功，+100 积分（今日 100，连续 1 天）"],
            "cat": SAMPLE_CAT if i % 2 == 0
                   else ["🐾 猫咪：龙焰喵（SSR）", "🚀 派出猫猫去咖啡馆（1 小时后回）"],
        })
    return out


def mask(url: str) -> str:
    """给 URL 里的密钥段打码（Server酱 SendKey / Bark key / Telegram token 都在 path 上）。"""
    p = urllib.parse.urlsplit(url)
    segs = [seg[:4] + "***" + seg[-2:] if len(seg) > 8 else seg for seg in p.path.split("/")]
    pairs = []
    for k, vs in urllib.parse.parse_qs(p.query).items():
        v = vs[0]
        pairs.append("%s=%s" % (k, v[:4] + "***" + v[-2:] if len(v) > 8 else v))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "/".join(segs), "&".join(pairs), ""))


def diagnose(trace: dict | None, result: str) -> None:
    if trace:
        raw = str(trace.get("raw") or "")
        hit = None
        try:
            j = json.loads(raw)
            if isinstance(j, dict):
                for key in ("code", "errcode", "status", "error_code"):
                    if key in j:
                        hit = str(j[key])
                        break
        except ValueError:
            m = re.search(r'"?(?:code|errcode|error_code)":?\s*"?(\d+)', raw)
            if m:
                hit = m.group(1)
        if hit and HINTS.get(hit):
            print("   可能原因：%s" % HINTS[hit])
    if "HTTP 404" in result:
        print("   可能原因：Webhook 地址不完整（漏了尾部字符），或机器人已被删除")
    if "timed out" in result or "URLError" in result:
        print("   可能原因：这台机器访问不了该服务（网络或代理问题）")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="通知渠道自检（会真的发一条消息）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="all",
                    help="feishu/wecom/dingtalk/serverchan/pushplus/bark/ntfy/telegram/email，"
                         "逗号分隔；all = 所有已配置的（默认 all）")
    ap.add_argument("--webhook", help="feishu / wecom / dingtalk 的 Webhook")
    ap.add_argument("--secret", help="feishu 签名密钥 / dingtalk 加签密钥")
    ap.add_argument("--key", help="serverchan SendKey / bark key")
    ap.add_argument("--token", help="pushplus token / telegram bot token")
    ap.add_argument("--chat-id", dest="chat_id", help="telegram chat id")
    ap.add_argument("--topic", help="ntfy topic")
    ap.add_argument("--server", help="bark / ntfy 自建服务器地址")
    ap.add_argument("--smtp-host", dest="smtp_host")
    ap.add_argument("--smtp-port", dest="smtp_port")
    ap.add_argument("--smtp-user", dest="smtp_user")
    ap.add_argument("--smtp-pass", dest="smtp_pass")
    ap.add_argument("--mail-to", dest="mail_to")
    ap.add_argument("--accounts", type=int, default=2,
                    help="预览几个账号的内容（默认 2；填 1 可看单账号样式）")
    ap.add_argument("--print", dest="just_print", action="store_true",
                    help="只打印会发出去的内容，不真的发")
    args = ap.parse_args()

    # 先把命令行参数落到环境变量，再据此判断「哪些渠道配好了」
    apply_cli(args)
    channels = resolve_channels(args.channel)

    unknown = [c for c in channels if c not in daily.NOTIFY_ORDER]
    if unknown:
        print("❌ 不认识的渠道：%s\n   可选：%s"
              % (", ".join(unknown), ", ".join(daily.NOTIFY_ORDER)), file=sys.stderr)
        return 2

    if not channels:
        print("❌ 没有检测到任何已配置的渠道。", file=sys.stderr)
        print("   本次自动探测了：%s" % "、".join(daily.CHANNEL_LABEL.values()),
              file=sys.stderr)
        print("   挑一个配起来，比如钉钉：", file=sys.stderr)
        print("     DINGTALK_WEBHOOK='<Webhook>' DINGTALK_SECRET='<加签密钥>' \\",
              file=sys.stderr)
        print("       python3 scripts/test_notify.py --channel dingtalk", file=sys.stderr)
        return 2

    sections = sample_sections(args.accounts)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    title = "Buddy 加油站日报 · %s · test" % stamp

    print("=" * 62)
    print("本次测试：%s" % "、".join(daily.CHANNEL_LABEL.get(c, c) for c in channels))
    print("内容预览：%d 个账号" % len(sections))
    print("=" * 62)

    if args.just_print:
        print("\n--- markdown 版（飞书/企微/钉钉/Server酱/PushPlus 用这个）---")
        print(daily._fmt_sections(sections))
        print("\n--- 纯文本版（Bark / ntfy / 邮件 / Telegram 用这个）---")
        print(daily._fmt_sections(sections, md=False))
        return 0

    rc = 0
    for ch in channels:
        label = daily.CHANNEL_LABEL.get(ch, ch)
        good, why = configured(ch)
        print("\n### %s" % label)
        if not good:
            print("   ⏭  跳过（%s）。想测就照这样配：" % why)
            print("       %s" % HOWTO.get(ch, ""))
            rc = 1
            continue

        before = len(daily.LAST_TRACE)
        result = daily._dispatch(ch, stamp, "test", title, sections, True)
        trace = (daily.LAST_TRACE[before:] or [None])[-1]

        print("   发送：%s" % result)
        if trace:
            print("   URL ：%s" % mask(str(trace.get("url") or "")))
            print("   HTTP：%s" % trace.get("http"))
            print("   返回：%s" % str(trace.get("raw"))[:400])
        diagnose(trace, result)
        if "失败" in result:
            rc = 1
            print("   配置参考：%s" % HOWTO.get(ch, ""))

    print()
    if rc == 0:
        print("✅ 全部通过。发出去什么样，手机上收到的就是什么样。")
        print("   下一步：把这些凭据写进仓库 secret"
              "（Settings → Secrets and variables → Actions）。")
    else:
        print("❌ 有渠道没通过，按上面的「可能原因 / 配置参考」修。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
