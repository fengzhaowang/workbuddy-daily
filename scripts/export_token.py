#!/usr/bin/env python3
"""凭证刷新脚本（只在本机跑）：把各账号凭证汇总成一份清单，注入仓库 secret。

为什么需要它：GitHub Actions 跑在云端 runner 上，读不到本机的登录态。
所以由本机把登录态解密出来，集中成一份账号清单，Actions 再从 secret 读。

多人/多账号怎么用：
  * 每台机器（或每个账号）各自跑一次 --add-local，把本机登录的账号加进清单；
    换机器时把清单文件拷过去，或用 --import 合并别人给你的账号 JSON。
  * 清单累积在本机 accounts.local.json（权限 600，已在 .gitignore 里），
    最后整体作为 **一个** 仓库 secret：WB_ACCOUNTS。

写法上刻意不落敏感信息：明文 token 只经由 stdin 交给 gh，
不经过命令行参数、不进仓库、不进日志。

用法：
  python3 scripts/export_token.py                       # 把本机账号加入清单（默认动作）
  python3 scripts/export_token.py --as "我的账号"        # 顺便起个名字，便于多人区分
  python3 scripts/export_token.py --import team.json    # 合并别人给的账号 JSON
  python3 scripts/export_token.py --list                # 看清单（脱敏 + 剩余有效期）
  python3 scripts/export_token.py --remove "小号"        # 移除账号
  python3 scripts/export_token.py --dedupe              # 清理重复（同一个人只留一条）
  python3 scripts/export_token.py --check               # 逐个验活
  python3 scripts/export_token.py --repo <owner/name> --check   # 验活后写进仓库 secret
  python3 scripts/export_token.py --print-json          # 打印明文 JSON，手动粘进 GitHub 网页
  python3 scripts/export_token.py --show                # 只打印脱敏信息，不写任何东西
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daily  # noqa: E402  复用同一套 HTTP 实现与脱敏
from wb_auth import AuthError, load_credentials, redact  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(REPO_ROOT, "accounts.local.json")
WIRE_FIELDS = ("name", "token", "uid", "domain", "endpoint", "webhook", "secret")


# ---------------- 清单读写 ----------------
def load_store(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fp:
        data = json.load(fp)
    items = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("清单格式不对：%s（应为 {\"accounts\": [...]}）" % path)
    return [it for it in items if isinstance(it, dict) and it.get("token")]


def save_store(path: str, accounts: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump({"version": 1, "accounts": accounts}, fp,
                  ensure_ascii=False, indent=2)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)     # 600：只有本人可读
    os.replace(tmp, path)


def wire_payload(accounts: list[dict]) -> str:
    """只保留 daily.py 认识的字段，去掉本机元数据（added_at / expires_at 等）。"""
    clean = [{k: a[k] for k in WIRE_FIELDS if a.get(k) not in (None, "")} for a in accounts]
    return json.dumps({"accounts": clean}, ensure_ascii=False)


def upsert(accounts: list[dict], entry: dict) -> str:
    """合并进清单：先按名字找，找不到再按 uid 兜底。返回 added / updated / merged。

    按 uid 兜底很关键：本机登录态只有一个人，`--as 甲` 跑一次、`--as 乙` 再跑一次，
    如果只按名字判重就会得到两条同一个人——云端「共 2 个账号」其实只有 1 个人，
    白占一份时间预算，卡片上还重复一遍。命中时保留原有的名字和专属 webhook
    （名字是人有意的命名，webhook 是有意的配置，都不该被本机登录态冲掉）。
    """
    for i, a in enumerate(accounts):
        if a.get("name") == entry["name"]:
            accounts[i] = entry
            return "updated"
    uid = entry.get("uid")
    if uid:
        for i, a in enumerate(accounts):
            if a.get("uid") == uid:
                merged = dict(entry)
                merged["name"] = a.get("name") or entry["name"]
                for k in ("webhook", "secret"):
                    if a.get(k):
                        merged[k] = a[k]
                accounts[i] = merged
                return "merged"
    accounts.append(entry)
    return "added"


def dedupe(accounts: list[dict]) -> tuple[list[dict], list[str]]:
    """同一个人只留一条。

    判重按 uid（没有 uid 就退回 token 前缀）——**不能只按名字**，
    否则 `--as 甲` 和 `--as 乙` 分别跑一次就会把同一个人塞进去两条，
    结果云端「共 2 个账号」其实只有 1 个人，白占一份时间预算、卡片上还重复一遍。
    保留顺序：带专属 webhook 的 > 先出现的。
    """
    kept: list[dict] = []
    removed: list[str] = []
    index: dict[str, int] = {}
    for a in accounts:
        key = a.get("uid") or ("tok:" + (a.get("token") or "")[:64])
        if key not in index:
            index[key] = len(kept)
            kept.append(a)
            continue
        prev = kept[index[key]]
        if a.get("webhook") and not prev.get("webhook"):
            removed.append(prev.get("name") or "?")
            kept[index[key]] = a
        else:
            removed.append(a.get("name") or "?")
    return kept, removed


def find_duplicates(accounts: list[dict]) -> list[str]:
    groups: dict[str, list[str]] = {}
    for a in accounts:
        key = a.get("uid") or ("tok:" + (a.get("token") or "")[:64])
        groups.setdefault(key, []).append(a.get("name") or "?")
    return ["%s" % "、".join(v) for v in groups.values() if len(v) > 1]


# ---------------- 取本机账号 ----------------
def local_entry(name: Optional[str]) -> dict:
    c = load_credentials()
    return {
        "name": name or "本机账号",
        "token": c["token"],
        "uid": c["uid"],
        "domain": c.get("domain", ""),
        "expires_at": int(c.get("expires_at") or 0),
        "added_at": int(time.time()),
    }


def local_meta(c: dict) -> str:
    exp = int(c.get("expires_at") or 0)
    if not exp:
        return "（未取到有效期）"
    left = (exp - time.time() * 1000) / 86400000.0
    return "剩 %.1f 天（%s）" % (left, time.strftime("%Y-%m-%d %H:%M", time.localtime(exp / 1000)))


# ---------------- 验活 ----------------
def probe(acc: dict) -> tuple[bool, str]:
    """用只读接口验活：POST 空 body 只查状态，不改任何数据。"""
    api = daily.Api(acc.get("endpoint") or daily.ENDPOINT_DEFAULT,
                    acc["token"], acc.get("uid", ""), acc.get("domain", ""))
    code, body = api.call(daily.P_CHECKIN_STATUS, "POST", {}, retry=True)
    if code == -1:
        return False, "网络不可达（%s）" % (body.get("error", "") if isinstance(body, dict) else "")
    if code in (401, 403):
        return False, "鉴权失败（HTTP %s）：token 可能已过期，请先在客户端重新登录再跑本脚本" % code
    if not daily.is_ok(code, body):
        return False, "HTTP %s %s" % (code, daily.msg_of(body))
    d = daily.data_of(body)
    return True, "有效（活动=%s，今日已签=%s）" % (
        d.get("theme_name") or "?", d.get("today_checked_in"))


# ---------------- gh ----------------
def gh_set_secret(repo: str, name: str, value: str) -> None:
    """把值通过 stdin 交给 gh，避免出现在命令行参数或进程列表里。"""
    gh = shutil.which("gh")          # 不写死路径
    if not gh:
        raise RuntimeError("未找到 gh CLI。要么安装并 gh auth login，"
                           "要么用 --print-json 手动把 JSON 粘进 GitHub 网页")
    proc = subprocess.run([gh, "secret", "set", name, "--repo", repo],
                          input=value.encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("写入 secret %s 失败：%s"
                           % (name, proc.stderr.decode("utf-8", "replace").strip()))


# ---------------- 展示 ----------------
def show_list(accounts: list[dict]) -> None:
    if not accounts:
        print("清单是空的。先跑一次：python3 scripts/export_token.py --as \"你的名字\"")
        return
    print("清单 %d 个账号：" % len(accounts))
    for i, a in enumerate(accounts, 1):
        line = "  %d. %-12s uid=%s token=%s" % (
            i, a.get("name", "?"), redact(a.get("uid", "")), redact(a.get("token", "")))
        if a.get("webhook"):
            line += "  [专属飞书]"
        print(line)
        if a.get("expires_at"):
            print("     有效期：%s" % local_meta(a))
    dups = find_duplicates(accounts)
    if dups:
        print()
        for d in dups:
            print("  ⚠️ 这几条其实是同一个人：%s" % d)
        print("     -> 跑一次 --dedupe 清掉（只按 uid 判重，不会误删不同的人）")


# ---------------- 主流程 ----------------
def main() -> int:
    ap = argparse.ArgumentParser(description="把各账号凭证汇总注入仓库 secret（多账号）")
    ap.add_argument("--store", default=DEFAULT_STORE, help="本机清单文件（默认 %s）" % DEFAULT_STORE)
    ap.add_argument("--add-local", action="store_true", help="把本机登录态账号加入清单（默认动作）")
    ap.add_argument("--as", dest="as_name", help="本机账号的显示名，便于多人区分")
    ap.add_argument("--import", dest="import_file", metavar="FILE", help="合并一个账号 JSON 文件")
    ap.add_argument("--remove", metavar="NAME", help="按名字移除账号")
    ap.add_argument("--dedupe", action="store_true",
                    help="清理重复：同一个人（uid 相同）只留一条，带专属 webhook 的优先")
    ap.add_argument("--list", action="store_true", help="列出清单（脱敏）")
    ap.add_argument("--check", action="store_true", help="逐个验活")
    ap.add_argument("--print-json", action="store_true",
                    help="输出明文 JSON，供手动粘贴到 GitHub 仓库 Secret")
    ap.add_argument("--push", action="store_true", help="用 gh 写入仓库 secret WB_ACCOUNTS")
    ap.add_argument("--repo", help="目标仓库 owner/name（配合 --push）")
    ap.add_argument("--show", action="store_true", help="只打印脱敏信息，不写任何东西（兼容旧用法）")
    args = ap.parse_args()

    try:
        accounts = load_store(args.store)
    except Exception as e:  # noqa: BLE001
        print("❌ 读取清单失败：%s" % e, file=sys.stderr)
        return 2

    # ---- 移除 ----
    if args.remove:
        before = len(accounts)
        accounts = [a for a in accounts if a.get("name") != args.remove]
        if len(accounts) == before:
            print("❌ 清单里没有叫「%s」的账号" % args.remove, file=sys.stderr)
            return 1
        save_store(args.store, accounts)
        print("✅ 已移除「%s」，剩 %d 个账号" % (args.remove, len(accounts)), file=sys.stderr)

    # ---- 去重 ----
    if args.dedupe:
        before = len(accounts)
        accounts, removed = dedupe(accounts)
        if removed:
            save_store(args.store, accounts)
            print("✅ 已去重：删除 %s（%d -> %d 个账号）"
                  % ("、".join("「%s」" % n for n in removed), before, len(accounts)),
                  file=sys.stderr)
        else:
            print("✅ 没有重复条目（共 %d 个账号）" % len(accounts), file=sys.stderr)

    # ---- 导入 ----
    merged = False
    if args.import_file:
        try:
            with open(args.import_file, encoding="utf-8") as fp:
                incoming = json.load(fp)
            items = incoming.get("accounts") if isinstance(incoming, dict) else incoming
            if not isinstance(items, list):
                raise ValueError("应为 JSON 数组，或 {\"accounts\": [...]}")
            for it in items:
                if not isinstance(it, dict) or not it.get("token"):
                    continue
                it.setdefault("name", "账号%d" % (len(accounts) + 1))
                upsert(accounts, it)
                merged = True
            print("✅ 已合并 %s（现在共 %d 个账号）" % (args.import_file, len(accounts)),
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print("❌ 导入失败：%s" % e, file=sys.stderr)
            return 2

    # ---- 把本机账号加入清单 ----
    # 只有「明确 --add-local」或「什么动作都没给」时才读本机登录态并入清单。
    # 否则 --remove / --dedupe 刚做完的事会被这条默认动作又加回去，白折腾。
    explicit_action = (args.list or args.show or args.import_file or args.remove
                       or args.dedupe)
    added = None
    if args.add_local or not explicit_action:
        try:
            c = load_credentials()
        except AuthError as e:
            print("❌ 读取本机登录态失败：%s" % e, file=sys.stderr)
            return 2
        entry = local_entry(args.as_name)
        added = upsert(accounts, entry)
        save_store(args.store, accounts)
        what = {"added": "新增", "updated": "覆盖同名", "merged": "合并进同一账号的已有条目"}[added]
        print("来源文件：%s" % c["auth_file"], file=sys.stderr)
        print("账号 uid：%s（%s）" % (redact(c["uid"]), what), file=sys.stderr)
        print("token   ：%s" % redact(c["token"]), file=sys.stderr)
        print("域名    ：%s" % (c.get("domain") or "-"), file=sys.stderr)
        print("有效期  ：%s" % local_meta(entry), file=sys.stderr)
        print("✅ 清单已更新：%s（共 %d 个账号）" % (args.store, len(accounts)), file=sys.stderr)
        for d in find_duplicates(accounts):
            print("⚠️ 仍有同一个人出现多条：%s（跑 --dedupe 清掉）" % d, file=sys.stderr)

    # ---- 验活（走 stderr，把 stdout 留给 --print-json 的纯 JSON）----
    if args.check:
        print()
        print("验活：", file=sys.stderr)
        bad = 0
        for a in accounts:
            ok, msg = probe(a)
            if not ok:
                bad += 1
            print("  %s %s：%s" % ("✅" if ok else "❌", a.get("name", "?"), msg), file=sys.stderr)
        if bad:
            print("❌ 有 %d 个账号未通过验活，先修好再推 secret" % bad, file=sys.stderr)
            return 1
        print("✅ 全部 %d 个账号验活通过" % len(accounts), file=sys.stderr)

    # ---- 只读展示 ----
    if args.list or args.show:
        print()
        show_list(accounts)
        return 0

    # ---- 明文 JSON（给不想/不能用 gh 的人手动粘贴）----
    if args.print_json:
        if not accounts:
            print("❌ 清单是空的，没有可输出的内容", file=sys.stderr)
            return 1
        print("⚠️  下面是**明文凭证**。只粘贴到 GitHub 仓库的 Secret 输入框，"
              "别贴进聊天、别提交进仓库。", file=sys.stderr)
        print(wire_payload(accounts))
        return 0

    # ---- 推送 ----
    if args.push or args.repo:
        if not args.repo:
            print("❌ 要推送请给 --repo <owner/name>", file=sys.stderr)
            return 2
        if not accounts:
            print("❌ 清单是空的，没什么可推", file=sys.stderr)
            return 1
        try:
            gh_set_secret(args.repo, "WB_ACCOUNTS", wire_payload(accounts))
        except RuntimeError as e:
            print("❌ %s" % e, file=sys.stderr)
            return 1
        print("✅ 已写入 secret WB_ACCOUNTS（%d 个账号）" % len(accounts), file=sys.stderr)
        print("   旧的 WB_TOKEN / WB_UID 如果还在，可以到仓库设置里删掉了。", file=sys.stderr)
        return 0

    # 收尾提示统一走 stderr，保证 stdout 始终是机器可读的
    print()
    print("清单就绪，共 %d 个账号。" % len(accounts), file=sys.stderr)
    print("下一步二选一：", file=sys.stderr)
    print("  A. 已装并登录 gh：python3 scripts/export_token.py --repo <owner/name> --push",
          file=sys.stderr)
    print("  B. 手动粘贴    ：python3 scripts/export_token.py --print-json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
