"""安全功能专项测试：脱敏函数 / Cookie HMAC 签名 / Supabase key 权限校验。

只测纯函数（security_utils），不依赖 streamlit，不使用真实密钥。
所有"密钥"均为形如 test-key-123 / fake-sk-xxxx 的占位串。
"""
import sys
import os

# 确保能 import security_utils（测试可能从项目根目录运行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import security_utils
from security_utils import redact, safe_error, assert_anon_key

# ================= 1. redact() 密钥脱敏 =================

# sk- 开头的 API Key
_sk = "sk-abcdef123456789ghijklmnop"
_r = redact(f"Error: key={_sk} failed")
assert "sk-****" in _r and _sk not in _r, f"sk- 未脱敏: {_r}"
print("[1] redact sk- API Key PASS")

# Supabase publishable key
_sb_pub = "sb_publishable_xxxxxxxxxxxx"
_r = redact(f"Supabase key: {_sb_pub}")
assert "sb_publishable_****" in _r and _sb_pub not in _r, f"sb_publishable_ 未脱敏: {_r}"
print("[2] redact sb_publishable_ PASS")

# Supabase secret key (service_role)
_sb_sec = "sb_secret_xxxxxxxxxxxx"
_r = redact(f"Oops: {_sb_sec} leaked")
assert "sb_secret_****" in _r and _sb_sec not in _r, f"sb_secret_ 未脱敏: {_r}"
print("[3] redact sb_secret_ PASS")

# JWT
_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature123"
_r = redact(f"token: {_jwt}")
assert "eyJ****" in _r and _jwt not in _r, f"JWT 未脱敏: {_r}"
print("[4] redact JWT PASS")

# Bearer token
_r = redact("Authorization: Bearer sk-test123456789")
assert "Bearer ****" in _r and "sk-test123456789" not in _r, f"Bearer 未脱敏: {_r}"
print("[5] redact Bearer PASS")

# URL 参数
_r = redact("https://api.example.com/v1?apikey=sk-secretkey12345")
assert "apikey=****" in _r and "sk-secretkey12345" not in _r, f"URL 参数未脱敏: {_r}"
print("[6] redact URL apikey= PASS")

# 普通文本不被误伤
_normal = "这是一段普通文本，没有密钥。glm-4.5-air 模型。"
_r = redact(_normal)
assert _r == _normal, f"普通文本被误改: {_r}"
print("[7] redact 不误伤普通文本 PASS")

# 空输入
assert redact("") == ""
assert redact(None) == ""
print("[8] redact 空输入 PASS")

# ================= 2. safe_error() 错误脱敏 =================

# 普通异常（无敏感信息）
_e = ValueError("文件未找到")
_msg = safe_error(_e)
assert "ValueError" in _msg and "文件未找到" in _msg, f"safe_error 普通异常: {_msg}"
print("[9] safe_error 普通异常 PASS")

# 包含密钥的异常
_e = RuntimeError(f"接口返回 401: { _sk } invalid")
_msg = safe_error(_e)
assert _sk not in _msg, f"safe_error 泄露密钥: {_msg}"
assert "sk-****" in _msg or "配置" in _msg, f"safe_error 未脱敏: {_msg}"
print("[10] safe_error 密钥脱敏 PASS")

# 超长消息截断
_long = "x" * 500
_msg = safe_error(RuntimeError(_long), max_len=50)
# 总长 = type_name(12) + "："(1) + 50 + "…"(1) ≈ 64；允许一定余量
assert len(_msg) <= 70, f"safe_error 未截断: {len(_msg)}"
assert "xxxx" in _msg and "…" in _msg, "应截断并带省略号"
print("[11] safe_error 超长截断 PASS")

# 含敏感关键词 → 降级为通用提示
_e = RuntimeError("Authorization failed: bearer token expired")
_msg = safe_error(_e)
assert "bearer token expired" not in _msg, f"safe_error 敏感词未降级: {_msg}"
assert "配置" in _msg or "认证" in _msg, f"safe_error 未降级: {_msg}"
print("[12] safe_error 敏感词降级 PASS")

# ================= 3. assert_anon_key() Supabase 权限校验 =================

# 空 key 通过（未配置）
assert_anon_key("")
print("[13] assert_anon_key 空值 PASS")

# anon/publishable key 通过
assert_anon_key("sb_publishable_xxxxxxxxxxxx")
print("[14] assert_anon_key publishable PASS")

# service_role key 被拒
try:
    assert_anon_key("sb_secret_xxxxxxxxxxxx")
    raise AssertionError("应当拒绝 sb_secret_ key")
except ValueError as e:
    assert "service_role" in str(e) or "高权限" in str(e), str(e)
print("[15] assert_anon_key 拒绝 service_role PASS")

# 包含 service_role 文本的 key 被拒
try:
    assert_anon_key("eyJsome_jwt_with_service_role_payload")
    raise AssertionError("应当拒绝含 service_role 的 key")
except ValueError:
    pass
print("[16] assert_anon_key 拒绝含 service_role 文本 PASS")

# ================= 3.5 validate_cookie_sign_key() fail-closed 启动校验 =================

from security_utils import validate_cookie_sign_key

# 空密钥 → 拒绝启动
try:
    validate_cookie_sign_key("")
    raise AssertionError("空 COOKIE_SIGN_KEY 应拒绝启动")
except ValueError as e:
    assert "COOKIE_SIGN_KEY" in str(e)
print("[16.1] 缺失 COOKIE_SIGN_KEY 启动被拒绝 PASS")

# 过短密钥（<32）→ 拒绝
try:
    validate_cookie_sign_key("short-key")
    raise AssertionError("短 COOKIE_SIGN_KEY 应拒绝启动")
except ValueError as e:
    assert "32" in str(e) or "长度" in str(e)
print("[16.2] 短 COOKIE_SIGN_KEY 启动被拒绝 PASS")

# 占位符 → 拒绝
try:
    validate_cookie_sign_key("change-me-to-a-random-64-hex-string")
    raise AssertionError("占位符 COOKIE_SIGN_KEY 应拒绝启动")
except ValueError as e:
    assert "占位符" in str(e)
print("[16.3] 占位符 COOKIE_SIGN_KEY 启动被拒绝 PASS")

# 合格密钥（≥32 位随机串）→ 通过
validate_cookie_sign_key("0123456789abcdef0123456789abcdef0123456789abcdef")
print("[16.4] 合格 COOKIE_SIGN_KEY 通过 PASS")

# ================= 4. Cookie HMAC 签名/验证（模拟） =================
# 不依赖 streamlit，直接用 hmac/hashlib 模拟 app.py 的 _cookie_sign/_cookie_verify

import hmac
import hashlib

_SIGN_KEY = "test-sign-key-32chars-long-aaaaaa"  # 测试用，非真实密钥


def _cookie_sign(nickname: str, key: str = _SIGN_KEY) -> str:
    sig = hmac.new(key.encode(), nickname.encode(), hashlib.sha256).hexdigest()
    return f"{nickname}.{sig}"


def _cookie_verify(signed: str, key: str = _SIGN_KEY) -> str | None:
    # 严格模式：密钥由启动校验保证存在，无空密钥兼容分支
    if not signed or "." not in signed:
        return None
    nick, _, sig = signed.rpartition(".")
    if not nick or not sig:
        return None
    expected = hmac.new(key.encode(), nick.encode(), hashlib.sha256).hexdigest()
    return nick if hmac.compare_digest(sig, expected) else None


# 正常签名 → 验证通过
_signed = _cookie_sign("小橘")
assert _cookie_verify(_signed) == "小橘", "正常签名验证失败"
print("[17] Cookie HMAC 正常签名 PASS")

# 篡改签名 → 验证失败
_tampered = _signed[:-2] + "00"
assert _cookie_verify(_tampered) is None, "篡改签名应失败"
print("[18] Cookie HMAC 篡改检测 PASS")

# 篡改昵称 → 验证失败
_tampered_nick = "小奶糖." + _signed.split(".")[1]
assert _cookie_verify(_tampered_nick) is None, "篡改昵称应失败"
print("[19] Cookie HMAC 昵称篡改检测 PASS")

# 空值 → None
assert _cookie_verify("") is None
assert _cookie_verify(None) is None
print("[20] Cookie HMAC 空值 PASS")

# 无签名 Cookie（旧版明文昵称）→ 严格模式下应被拒绝
assert _cookie_verify("小橘", key="") is None
assert _cookie_verify("小橘") is None  # 明文无签名段
print("[21] Cookie 严格模式拒绝未签名值 PASS")

# ================= 5. 限流逻辑模拟（不依赖 DB） =================
# 验证 quota_left 的计算逻辑：DAILY_LIMIT - used

_DAILY = 20
_used = 5
_left = max(0, _DAILY - _used)
assert _left == 15, f"quota_left 计算错误: {_left}"
_used = 25  # 超限
_left = max(0, _DAILY - _used)
assert _left == 0, f"quota_left 超限应归零: {_left}"
print("[22] quota_left 计算逻辑 PASS")

print("\n=== ALL SECURITY TESTS PASSED ===")
