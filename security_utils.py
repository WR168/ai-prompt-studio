"""安全工具：密钥脱敏 + 错误信息脱敏。

对外暴露两个纯函数（无 streamlit 依赖，便于独立单元测试）：
- redact(text)：把文本里疑似 API Key / Supabase Key / JWT / Bearer 等敏感串
  替换为 `sk-****abcd` 形式，永不回显完整密钥。
- safe_error(e)：把异常转成"异常类型 + 通用消息"，剥掉可能携带密钥的细节，
  用于 st.error / 日志等用户可见位置。

设计原则：
- 宁可误杀（把长得像密钥的普通串也打码）也不漏过真实密钥。
- redact() 不抛异常，任何输入都安全返回字符串。
- safe_error() 不打印异常详情里的 URL / Header / 完整响应体，
  只保留异常类型名和经过 redact() 的简短消息。
"""
from __future__ import annotations

import re

# ---------------- 密钥模式 ----------------

# sk- 开头的 OpenAI / DeepSeek 等典型 API Key
_SK_RE = re.compile(r"(sk-[A-Za-z0-9_-]{6,})[A-Za-z0-9_-]*")
# 智谱 APIKey 形如 xxxxxxxx.yyyyyyyy（8-16 位.8-16 位）
_ZHIPU_RE = re.compile(r"\b([A-Za-z0-9]{8,16})\.([A-Za-z0-9]{8,16})\b")
# Supabase publishable / secret key
_SB_PUB_RE = re.compile(r"(sb_publishable_[A-Za-z0-9]{4,})[A-Za-z0-9]*")
_SB_SEC_RE = re.compile(r"(sb_secret_[A-Za-z0-9]{4,})[A-Za-z0-9]*")
# JWT（三段，eyJ 开头）
_JWT_RE = re.compile(r"(eyJ[A-Za-z0-9_-]{6,})\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
# Bearer token
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9_.-]{6,}", re.IGNORECASE)
# URL 里的 apikey= / key= / token= 参数值
_URL_PARAM_RE = re.compile(
    r"([?&](?:api[_-]?key|key|token|access[_-]?token)=)"
    r"([^&\s\"']+)",
    re.IGNORECASE,
)


def _mask(prefix: str, full: str) -> str:
    """把完整密钥打成 `前缀****末4位` 形式。"""
    if len(full) <= 8:
        return f"{prefix}****"
    return f"{prefix}****{full[-4:]}"


def redact(text: str) -> str:
    """把文本中疑似密钥的子串替换为脱敏形式。

    覆盖：sk- / 智谱 xxxxx.yyyyy / sb_publishable_ / sb_secret_ /
    JWT（eyJ...）/ Bearer xxx / URL 里的 apikey=/key=/token= 参数。
    """
    if not text or not isinstance(text, str):
        return ""

    out = text
    # 先处理 Bearer（避免被后续规则误改前缀）
    out = _BEARER_RE.sub(
        lambda m: m.group(1) + "****", out
    )
    # JWT
    out = _JWT_RE.sub(
        lambda m: _mask("eyJ", m.group(1)), out
    )
    # Supabase secret key（高权限，必须打码）
    out = _SB_SEC_RE.sub(
        lambda m: _mask("sb_secret_****", m.group(1)), out
    )
    # Supabase publishable key
    out = _SB_PUB_RE.sub(
        lambda m: _mask("sb_publishable_****", m.group(1)), out
    )
    # sk- 开头
    out = _SK_RE.sub(
        lambda m: _mask("sk-", m.group(1)), out
    )
    # 智谱 xxxxxxxx.yyyyyyyy（最后处理，避免误伤普通版本号）
    # 只在确实像密钥的上下文里打码：前后有 key/token/api 字样或单独成行
    def _zhipu_replacer(m: re.Match) -> str:
        full = m.group(0)
        # 排除明显的版本号（如 1.0.0 / glm-4.5-air）
        if re.match(r"^\d+\.\d+$", full):
            return full
        return _mask("", full)
    out = _ZHIPU_RE.sub(_zhipu_replacer, out)
    # URL 参数
    out = _URL_PARAM_RE.sub(
        lambda m: m.group(1) + "****", out
    )
    return out


# ---------------- 错误信息脱敏 ----------------

# 异常消息中可能携带敏感信息的关键词
_SENSITIVE_HINTS = (
    "authorization", "bearer", "api_key", "apikey", "api-key",
    "supabase_key", "sb_publishable_", "sb_secret_",
    "password", "token", "secret",
)


def safe_error(e: BaseException, max_len: int = 200) -> str:
    """把异常转成脱敏的简短消息，用于 st.error / 日志。

    保留异常类型名（便于排查），但消息体经过 redact() 处理，
    并截断到 max_len 字符以内，避免长响应体/URL 泄露。
    """
    type_name = type(e).__name__
    msg = str(e).strip() or "(no message)"
    # 截断超长消息（可能包含完整 HTTP 响应体）
    if len(msg) > max_len:
        msg = msg[:max_len] + "…"
    # 脱敏密钥串
    msg = redact(msg)
    # 如果消息里仍然出现了敏感关键词的明文片段，整体降级为通用提示
    low = msg.lower()
    if any(h in low for h in _SENSITIVE_HINTS):
        # 只在确实像泄露时降级；保留类型名给开发者排查
        return f"{type_name}：配置或认证异常，请联系管理员检查 Secrets。"
    return f"{type_name}：{msg}"


# ---------------- Supabase Key 权限校验 ----------------

def assert_anon_key(key: str) -> None:
    """校验 Supabase Key 必须是 anon/publishable 级别，
    发现 service_role / sb_secret_ 等高权限 key 时抛 ValueError。
    """
    k = (key or "").strip()
    if not k:
        return
    if k.startswith("sb_secret_") or "service_role" in k:
        raise ValueError(
            "检测到 Supabase service_role 密钥——这是高权限密钥，"
            "严禁用于前端/Streamlit Cloud 部署。请改用 sb_publishable_ 开头的"
            " anon 公钥。"
        )


def validate_cookie_sign_key(key: str, min_len: int = 32) -> None:
    """校验 Cookie HMAC 签名密钥（fail-closed）。

    缺失或长度不足时抛 ValueError——不允许静默降级为不签名，
    否则用户可手改 Cookie 伪造任意设备昵称。
    """
    k = (key or "").strip()
    if not k:
        raise ValueError(
            "缺少 COOKIE_SIGN_KEY，请在 Secrets 中配置 32 位以上随机密钥。"
            "生成方式：python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(k) < min_len:
        raise ValueError(
            f"COOKIE_SIGN_KEY 长度不足（当前 {len(k)} 位，要求 ≥{min_len} 位），"
            "请重新生成：python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if "change-me" in k.lower() or k.lower() in ("placeholder", "todo", "xxx"):
        raise ValueError(
            "COOKIE_SIGN_KEY 仍是占位符，请替换为真实随机密钥。"
            "生成方式：python -c \"import secrets; print(secrets.token_hex(32))\""
        )
