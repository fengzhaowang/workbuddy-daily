#!/usr/bin/env python3
"""刷新脚本（只在本机跑）：把本机登录态导出的凭证写进 GitHub 仓库 secret。

为什么需要它：GitHub Actions 跑在云端 runner 上，读不到本机的登录态。
所以由本机把登录态解密出来，直接写进仓库 secret，Actions 再从 secret 读。
明文 token 全程不落盘、不进仓库、不进日志——只走 gh 的 stdin。

用法：
  python3 scripts/export_token.py --repo <owner/name>            # 写入 secret
  python3 scripts/export_token.py --repo <owner/name> --check    # 先验活再写
  python3 scripts/export_token.py --show                         # 只看脱敏信息，不写

会写入的 secret：
  WB_TOKEN   解密后的 accessToken（必填）
  WB_UID     账号 uid（接口需要 X-User-Id，必填）
  WB_DOMAIN  登录域（多租户时需要，如 www.workbuddy.cn）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wb_auth import AuthError, load_credentials, redact  # noqa: E402

ENDPOINT_DEFAULT = "https://copilot.tencent.com"
# 用只读接口验活：POST 空 body 只查状态，不改任何数据。
PROBE_PATH = "/v2/billing/meter/checkin-activity-status"


def probe(token: str, uid: str, domain: str, endpoint: str) -> tuple[bool, str]:
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy",
    }
    if uid:
        headers["X-User-Id"] = uid
    if domain:
        headers["X-Domain"] = domain
    req = urllib.request.Request(endpoint.rstrip("/") + PROBE_PATH, data=b"{}",
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20,
                                    context=ssl.create_default_context()) as r:
            body = json.loads(r.read().decode("utf-8"))
        if body.get("code") == 0:
            d = body.get("data") or {}
            return True, "token 有效（活动=%s，今日已签=%s）" % (
                d.get("theme_name") or "?", d.get("today_checked_in"))
        return False, "token 校验失败：code=%s msg=%s" % (body.get("code"), body.get("msg"))
    except urllib.error.HTTPError as e:
        return False, "token 校验失败：HTTP %s（请先在客户端重新登录）" % e.code
    except Exception as e:  # noqa: BLE001
        return False, "token 校验请求异常：%s" % e


def gh_set_secret(repo: str, name: str, value: str) -> None:
    """把值通过 stdin 交给 gh，避免出现在命令行参数或进程列表里。"""
    gh = shutil.which("gh")          # 不写死路径
    if not gh:
        raise RuntimeError("未找到 gh CLI，请先安装 GitHub CLI 并执行 gh auth login")
    proc = subprocess.run([gh, "secret", "set", name, "--repo", repo],
                          input=value.encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("写入 secret %s 失败：%s"
                           % (name, proc.stderr.decode("utf-8", "replace").strip()))


def main() -> int:
    ap = argparse.ArgumentParser(description="导出本机凭证到仓库 secret")
    ap.add_argument("--repo", help="目标仓库 owner/name")
    ap.add_argument("--check", action="store_true", help="写之前先验活")
    ap.add_argument("--show", action="store_true", help="只打印脱敏信息，不写 secret")
    ap.add_argument("--expiry-warn-days", type=int, default=7)
    args = ap.parse_args()

    try:
        c = load_credentials()
    except AuthError as e:
        print("❌ 读取本机登录态失败：%s" % e, file=sys.stderr)
        return 2

    now_ms = int(time.time() * 1000)
    exp = int(c.get("expires_at") or 0)
    left_days = (exp - now_ms) / 86400000.0 if exp else None
    print("来源文件：%s" % c["auth_file"], file=sys.stderr)
    print("账号 uid：%s" % redact(c["uid"]), file=sys.stderr)
    print("token   ：%s" % redact(c["token"]), file=sys.stderr)
    print("域名    ：%s" % (c.get("domain") or "-"), file=sys.stderr)
    if left_days is not None:
        print("有效期  ：剩 %.1f 天（%s）"
              % (left_days, time.strftime("%Y-%m-%d %H:%M", time.localtime(exp / 1000))),
              file=sys.stderr)
        if left_days < args.expiry_warn_days:
            print("⚠️  token 快过期了，建议尽快重跑本脚本刷新 secret", file=sys.stderr)

    if args.check:
        ok, msg = probe(c["token"], c["uid"], c.get("domain", ""),
                        c.get("endpoint") or ENDPOINT_DEFAULT)
        print(("✅ " if ok else "❌ ") + msg, file=sys.stderr)
        if not ok:
            return 1

    if args.show:
        return 0

    if not args.repo:
        print("未指定 --repo，已跳过写入（用 --show 可只看信息）", file=sys.stderr)
        return 0

    try:
        gh_set_secret(args.repo, "WB_TOKEN", c["token"])
        gh_set_secret(args.repo, "WB_UID", c["uid"])
        if c.get("domain"):
            gh_set_secret(args.repo, "WB_DOMAIN", c["domain"])
    except RuntimeError as e:
        print("❌ %s" % e, file=sys.stderr)
        return 1
    print("✅ 已写入 secret：WB_TOKEN / WB_UID%s"
          % (" / WB_DOMAIN" if c.get("domain") else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
