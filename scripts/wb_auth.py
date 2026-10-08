#!/usr/bin/env python3
"""本机登录态读取与解密（只在「刷新脚本」里用，云端不依赖本文件）。

WorkBuddy 桌面端把登录态写在 CodeBuddyExtension 的 auth 目录下，accessToken 是
AES-256-GCM 信封（suite=1）加密的。解封密钥由客户端运行时持有，只能借
Electron 主进程的本地存储接口取出，所以这里把 WorkBuddy 可执行文件当 Node 跑一小段 JS。

安全约定：
  * 明文 token 只在本进程内存和子进程管道里流动，绝不落盘、绝不打印。
  * 所有对外输出都经过 redact()，日志里只会出现前后缀和长度。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from typing import Optional

ENDPOINT_DEFAULT = "https://copilot.tencent.com"

# 客户端凭据文件（macOS / Windows 桌面端，以及 Linux CodeBuddy CLI）
AUTH_REL = ("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")
CLI_AUTH_REL = ("CodeBuddyExtension", "Data", "Public", "auth",
                "Tencent-Cloud.coding-copilot.info")

TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]+=*")

# 这段 JS 在 WorkBuddy 客户端进程内执行：从原生存储取 atRestSecretKey，解开信封。
_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const fail = r => { throw { reason: r }; };
const obj = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function b64(v, len) {
  if (typeof v !== 'string') fail('INVALID_FORMAT');
  const b = Buffer.from(v, 'base64');
  if (b.toString('base64') !== v || (len !== undefined && b.length !== len)) fail('INVALID_FORMAT');
  return b;
}
function utf8(b) {
  const t = b.toString('utf8');
  if (!Buffer.from(t, 'utf8').equals(b)) fail('INVALID_FORMAT');
  return t;
}
function decode(v) {
  if (!obj(v) || v.$wbEncrypted !== 1 || Object.keys(v).sort().join(',') !== '$wbEncrypted,envelope')
    fail('UNSUPPORTED_ENVELOPE');
  let env;
  try { env = JSON.parse(utf8(b64(v.envelope))); } catch (e) { fail(e.reason || 'INVALID_FORMAT'); }
  if (!obj(env) || env.suite !== 1) fail('UNSUPPORTED_ENVELOPE');
  if (Object.keys(env).sort().join(',') !== 'authTag,ciphertext,keyId,nonce,suite') fail('INVALID_FORMAT');
  if (typeof env.keyId !== 'string' || !/^[0-9a-f]{16}$/.test(env.keyId)) fail('INVALID_FORMAT');
  return { keyId: env.keyId, nonce: b64(env.nonce, 12), tag: b64(env.authTag, 16),
           ciphertext: b64(env.ciphertext) };
}
function native() {
  try {
    const s = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof s.loggerGet !== 'function') fail('RUNTIME_UNAVAILABLE');
    return s;
  } catch (_) { fail('RUNTIME_UNAVAILABLE'); }
}
function decrypt(env) {
  let payload;
  try { payload = JSON.parse(native().loggerGet()); } catch (_) { fail('RUNTIME_UNAVAILABLE'); }
  let key, plain;
  try {
    if (!obj(payload) || payload.version !== 1) fail('RUNTIME_UNAVAILABLE');
    let secret;
    try { secret = b64(payload.atRestSecretKey, 32); } catch (_) { fail('RUNTIME_UNAVAILABLE'); }
    const empty = secret.every(b => b === 0);
    secret.fill(0);
    if (empty) fail('RUNTIME_UNAVAILABLE');
    key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    payload = null;
    if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== env.keyId)
      fail('KEY_MISMATCH');
    const lp = s => { const b = Buffer.from(s, 'utf8'); const l = Buffer.alloc(4);
                      l.writeUInt32BE(b.length); return Buffer.concat([l, b]); };
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]), lp(env.keyId), Buffer.from([2, 0, 0])]);
    try {
      const c = crypto.createDecipheriv('aes-256-gcm', key, env.nonce, { authTagLength: 16 });
      c.setAAD(aad); c.setAuthTag(env.tag);
      plain = Buffer.concat([c.update(env.ciphertext), c.final()]);
    } catch (_) { fail('DECRYPT_FAILED'); }
    const token = utf8(plain);
    if (!token.length || token.length > 32768 || !/^[A-Za-z0-9._~+\/-]+=*$/.test(token))
      fail('INVALID_FORMAT');
    return token;
  } finally { if (key) key.fill(0); if (plain) plain.fill(0); }
}
let chunks = [], size = 0;
function reply(v) { process.stdout.write(JSON.stringify({ version: 1, ...v }),
  () => process.exit(v.ok ? 0 : 1)); }
process.stdin.on('data', c => { size += c.length;
  if (size > 65536) reply({ ok: false, reason: 'INVALID_FORMAT' }); else chunks.push(c); });
process.stdin.on('error', () => reply({ ok: false, reason: 'HELPER_PROTOCOL' }));
process.stdin.on('end', () => {
  try {
    const req = JSON.parse(utf8(Buffer.concat(chunks))); chunks = [];
    if (!obj(req) || req.version !== 1) fail('HELPER_PROTOCOL');
    if (req.operation === 'probe') {
      native();
      if (!crypto.getCiphers().includes('aes-256-gcm')) fail('RUNTIME_UNAVAILABLE');
      reply({ ok: true, electron: process.versions.electron || 'unknown' });
    } else if (req.operation === 'decrypt') {
      reply({ ok: true, accessToken: decrypt(decode(req.value)) });
    } else fail('HELPER_PROTOCOL');
  } catch (e) {
    const list = ['INVALID_FORMAT', 'UNSUPPORTED_ENVELOPE', 'RUNTIME_UNAVAILABLE',
                  'KEY_MISMATCH', 'DECRYPT_FAILED', 'HELPER_PROTOCOL'];
    reply({ ok: false, reason: list.includes(e.reason) ? e.reason : 'HELPER_PROTOCOL' });
  }
});
"""

REASONS = {
    "INVALID_FORMAT": "登录凭据格式无效，请检查客户端版本",
    "UNSUPPORTED_ENVELOPE": "凭据加密格式不受支持，请更新本脚本",
    "RUNTIME_NOT_FOUND": "未找到 WorkBuddy 客户端，可用 WORKBUDDY_EXE 指定可执行文件",
    "RUNTIME_UNAVAILABLE": "客户端运行时缺少所需的原生存储接口",
    "KEY_MISMATCH": "凭据与所选客户端密钥不匹配，请确认是同一个账号的客户端",
    "DECRYPT_FAILED": "凭据解密失败（可能已过期或被改写），请在客户端重新登录",
    "HELPER_TIMEOUT": "凭据解密超时，请稍后重试",
    "HELPER_PROTOCOL": "凭据助手返回了无效结果",
    "NO_AUTH_FILE": "本机未找到登录凭据文件，请先登录 WorkBuddy 桌面端",
}


class AuthError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(REASONS.get(reason, reason))
        self.reason = reason


def redact(value: Optional[str]) -> str:
    """把敏感串压成 <前6>***<后4>(len=N)，用于日志。"""
    if not value:
        return "<empty>"
    if len(value) <= 12:
        return "*** (len=%d)" % len(value)
    return "%s***%s (len=%d)" % (value[:6], value[-4:], len(value))


def find_auth_file() -> str:
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        if os.path.exists(override):
            return override
        raise AuthError("NO_AUTH_FILE")
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    candidates = [
        os.path.join(home, "Library", "Application Support", *AUTH_REL),
        os.path.join(local, *AUTH_REL),
        os.path.join(xdg, *CLI_AUTH_REL),
        os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise AuthError("NO_AUTH_FILE")


def _mac_runtime(bundle: str) -> Optional[str]:
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            name = plistlib.load(f).get("CFBundleExecutable")
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name:
            return None
        return os.path.join(bundle, "Contents", "MacOS", name)
    except (OSError, ValueError, TypeError):
        return None


def find_runtime() -> str:
    """定位 WorkBuddy 桌面端可执行文件（macOS 上实际叫 Electron，从 Info.plist 取真名）。"""
    override = os.environ.get("WORKBUDDY_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path):
            raise AuthError("RUNTIME_NOT_FOUND")
        return path
    home = os.path.expanduser("~")
    candidates = []
    if sys.platform == "darwin":
        candidates = [_mac_runtime(os.path.join(root, "WorkBuddy.app"))
                      for root in ("/Applications", os.path.join(home, "Applications"))]
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates = [os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe")]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    raise AuthError("RUNTIME_NOT_FOUND")


def _run_helper(exe: str, request: dict, timeout: float = 15.0) -> dict:
    payload = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = subprocess.run([exe, "-e", _HELPER_JS], input=payload,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AuthError("HELPER_TIMEOUT") from None
    except OSError:
        raise AuthError("RUNTIME_UNAVAILABLE") from None
    try:
        reply = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise AuthError("HELPER_PROTOCOL") from None
    if reply.get("ok") is not True:
        raise AuthError(reply.get("reason") or "HELPER_PROTOCOL")
    return reply


def _b64_strict(value: str, length: Optional[int] = None) -> bytes:
    if not isinstance(value, str):
        raise AuthError("INVALID_FORMAT")
    raw = base64.b64decode(value, validate=True)
    if base64.b64encode(raw).decode("ascii") != value:
        raise AuthError("INVALID_FORMAT")
    if length is not None and len(raw) != length:
        raise AuthError("INVALID_FORMAT")
    return raw


def unwrap_envelope(value: dict) -> dict:
    """把 {$wbEncrypted:1, envelope:"..."} 拆成 keyId/nonce/tag/ciphertext。"""
    envelope = json.loads(_b64_strict(value["envelope"]).decode("utf-8"))
    if envelope.get("suite") != 1:
        raise AuthError("UNSUPPORTED_ENVELOPE")
    return envelope


def load_credentials(auth_file: Optional[str] = None) -> dict:
    """读本机登录态，返回解密后的 {token, uid, domain, endpoint, expires_at...}。

    明文 token 只存在于返回值中，调用方负责不外泄。
    """
    path = auth_file or find_auth_file()
    with open(path, "r", encoding="utf-8") as f:
        session = json.load(f)

    auth = session.get("auth") or {}
    account = session.get("account") or {}
    raw_token = auth.get("accessToken")
    if not raw_token:
        raise AuthError("NO_AUTH_FILE")

    if isinstance(raw_token, str):
        if not TOKEN_RE.fullmatch(raw_token):
            raise AuthError("INVALID_FORMAT")
        token = raw_token
    else:
        token = _run_helper(find_runtime(),
                            {"operation": "decrypt", "value": raw_token})["accessToken"]
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise AuthError("INVALID_FORMAT")

    return {
        "token": token,
        "uid": account.get("uid") or "",
        "enterprise_id": account.get("enterpriseId") or "",
        "domain": auth.get("domain") or "",
        "endpoint": auth.get("endpoint") or ENDPOINT_DEFAULT,
        "expires_at": auth.get("expiresAt") or 0,
        "refresh_expires_at": auth.get("refreshExpiresAt") or 0,
        "uid_hash": hashlib.sha256((account.get("uid") or "").encode()).hexdigest()[:12],
        "auth_file": path,
    }


if __name__ == "__main__":
    c = load_credentials()
    print(json.dumps({k: (redact(v) if k == "token" else v) for k, v in c.items()},
                     ensure_ascii=False, indent=2))
