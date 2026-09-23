"""🎬 主角一致 · AI 视频提示词工坊

启动方式：streamlit run app.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st

import llm
import prompts
import security_utils
import storage
from security_utils import redact, safe_error

# 桌面快捷方式是 Windows 专属功能（winreg / cscript），云端 Linux 不显示
IS_WINDOWS = sys.platform == "win32"


# ---------------- 密钥与配置（云端 st.secrets，本地数据库兜底） ----------------

def _read_secrets() -> dict:
    """安全读取 .streamlit/secrets.toml；文件不存在或无配置时返回空值/默认。"""
    cfg: dict = {
        "app_password": "",
        "api_base": "", "api_key": "", "model": "",
        "supabase_url": "", "supabase_key": "",
        "daily_limit": 20,
        "cookie_sign_key": "",
        "session_max_hours": 8,
    }
    try:
        cfg["app_password"] = (st.secrets.get("APP_PASSWORD", "") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        llm_cfg = st.secrets.get("llm", {}) or {}
        cfg["api_base"] = (llm_cfg.get("API_BASE", "") or "").strip()
        cfg["api_key"] = (llm_cfg.get("API_KEY", "") or "").strip()
        cfg["model"] = (llm_cfg.get("MODEL", "") or "").strip()
        # 兜底模型：主模型调用失败时自动切换重试（如 flash 失败 → air/plus）
        cfg["model_fallback"] = (llm_cfg.get("MODEL_FALLBACK", "") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        sb = st.secrets.get("supabase", {}) or {}
        cfg["supabase_url"] = (sb.get("SUPABASE_URL", "") or "").strip()
        cfg["supabase_key"] = (sb.get("SUPABASE_KEY", "") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg["daily_limit"] = max(1, int(st.secrets.get("DAILY_LIMIT", 20)))
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg["cookie_sign_key"] = (st.secrets.get("COOKIE_SIGN_KEY", "") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg["session_max_hours"] = max(1, int(st.secrets.get("SESSION_MAX_HOURS", 8)))
    except Exception:  # noqa: BLE001
        pass
    return cfg


SECRETS = _read_secrets()
DAILY_LIMIT = SECRETS["daily_limit"]

# Supabase 密钥权限校验：发现 service_role 等高权限 key 立即拒绝启动，
# 避免在 Streamlit Cloud 前端暴露高权限凭据
try:
    security_utils.assert_anon_key(SECRETS["supabase_key"])
except ValueError as _e:
    st.set_page_config(page_title="配置错误", layout="centered")
    st.error(str(_e))
    st.stop()

# Cookie 签名密钥强制校验（fail-closed）：
# 未配置或长度不足立即拒绝启动，不允许静默降级为"不签名"，
# 否则任何用户都能手改 Cookie 伪造他人昵称
try:
    security_utils.validate_cookie_sign_key(SECRETS["cookie_sign_key"])
except ValueError as _e:
    st.set_page_config(page_title="配置错误", layout="centered")
    st.error(str(_e))
    st.stop()

# Supabase 配置注入：配置了 [supabase] 即启用云端数据库，否则自动回退本地 SQLite
storage.configure_supabase(SECRETS["supabase_url"], SECRETS["supabase_key"])

# ---------------- 浏览器 Cookie（设备身份持久化，打开即自动登录） ----------------
# 用 streamlit-cookies-controller 在 Python 侧直接读写，不注入 JS、不依赖 URL 参数。
# Cookie 只存昵称（浏览器单 Cookie 上限约 4KB，放不下 2MB 头像的 Base64）；
# 头像按昵称从 Supabase user_profiles 拉取（avatar_map_cache 会缓存），体验无感。
# Cookie 值使用 HMAC 签名（nickname.signature），防止用户手改 Cookie 伪造他人身份。
COOKIE_KEY = "aps_nickname"
COOKIE_MAX_AGE = 365 * 24 * 3600  # 365 天
SESSION_MAX_SECONDS = SECRETS["session_max_hours"] * 3600

try:
    from streamlit_cookies_controller import CookieController
    _cookies = CookieController()
except Exception:  # noqa: BLE001  # 库缺失/组件环境不可用时降级为"无 Cookie 模式"
    _cookies = None


def _cookie_sign(nickname: str) -> str:
    """对昵称做 HMAC-SHA256 签名，返回 `nickname.hexsig`。

    签名密钥在启动阶段已强制校验（fail-closed），此处必然存在。
    """
    key = SECRETS["cookie_sign_key"]
    sig = hmac.new(key.encode("utf-8"), nickname.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{nickname}.{sig}"


def _cookie_verify(signed_value: str) -> str | None:
    """校验 Cookie 值的 HMAC 签名；通过返回昵称，失败返回 None。"""
    if not signed_value or "." not in signed_value:
        return None
    nickname, _, sig = signed_value.rpartition(".")
    if not nickname or not sig:
        return None
    expected = hmac.new(
        SECRETS["cookie_sign_key"].encode("utf-8"),
        nickname.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(sig, expected):
        return nickname
    return None


def get_effective_settings() -> dict[str, str]:
    """接口配置优先级：云端 Secrets > 设置页存入数据库的值 > 内置默认值。

    数据库读取结果缓存进 session_state，避免每次 rerun 都请求 Supabase；
    设置页保存时调用 _invalidate_settings_cache() 失效。"""
    cached = st.session_state.get("settings_cache")
    if cached is None:
        cached = dict(storage.get_settings())
        st.session_state["settings_cache"] = cached
    merged = dict(cached)
    for key in ("api_base", "api_key", "model"):
        if SECRETS[key]:
            merged[key] = SECRETS[key]
    merged["key_from_secrets"] = "yes" if SECRETS["api_key"] else "no"
    merged["model_fallback"] = SECRETS["model_fallback"]
    return merged


def _invalidate_settings_cache() -> None:
    st.session_state.pop("settings_cache", None)


# ---------------- 数据缓存（避免页面切换时重复请求 Supabase） ----------------

def _get_chars_cached() -> list[dict]:
    """主角列表缓存：在 session_state 里保留一份，避免每次 rerun 都打云端。
    任何写入/删除/更新主角的操作必须调用 _invalidate_chars_cache()。
    请求失败时展示友好提示且不缓存失败结果（下次 rerun 自动重试）。"""
    cache = st.session_state.get("chars_cache")
    if cache is None:
        try:
            cache = storage.list_characters()
            st.session_state["chars_cache"] = cache
        except Exception:  # noqa: BLE001
            st.error("数据加载失败，请检查网络连接（云端数据库暂不可用）。")
            return []
    return cache


def _invalidate_chars_cache() -> None:
    st.session_state.pop("chars_cache", None)


def _get_history_cached() -> list[dict]:
    """历史记录缓存：同上。每次新增历史后失效；失败不缓存，自动重试。"""
    cache = st.session_state.get("history_cache")
    if cache is None:
        try:
            cache = storage.list_history()
            st.session_state["history_cache"] = cache
        except Exception:  # noqa: BLE001
            st.error("历史记录加载失败，请检查网络连接。")
            return []
    return cache


def _invalidate_history_cache() -> None:
    st.session_state.pop("history_cache", None)


# ---------------- 密码门禁（防白嫖） ----------------

def _session_expired() -> bool:
    """检查会话是否过期（SESSION_MAX_HOURS 小时后自动登出）。"""
    start = st.session_state.get("session_start_ts")
    if not start:
        return False
    return (datetime.now().timestamp() - float(start)) > SESSION_MAX_SECONDS


def _do_logout() -> None:
    """登出：清除会话密码/时间/Cookie，回到登录页。"""
    st.session_state.pop("app_password", None)
    st.session_state.pop("session_start_ts", None)
    st.session_state.pop("login_pw", None)
    if _cookies is not None:
        try:
            _cookies.remove(COOKIE_KEY)
        except Exception:  # noqa: BLE001
            pass


def check_password() -> bool:
    """未配置 APP_PASSWORD 时门禁自动关闭（本地开发无感知）；
    配置后必须输入正确密码才能使用。
    会话超过 SESSION_MAX_HOURS 后自动登出，需重新输入密码。"""
    password = SECRETS["app_password"]
    if not password:
        return True
    # 已登录：检查会话是否过期
    if hmac.compare_digest(str(st.session_state.get("app_password", "")), password):
        if _session_expired():
            _do_logout()
            st.rerun()
            return False
        return True

    st.title("🔒 访问验证")
    st.caption("这是一个私人小工具，请输入访问密码。")
    with st.form("login_form"):
        st.text_input("密码", type="password", key="login_pw")
        submitted = st.form_submit_button("进入", type="primary")
    if submitted:
        if hmac.compare_digest(st.session_state.get("login_pw", ""), password):
            st.session_state["app_password"] = st.session_state["login_pw"]
            st.session_state["session_start_ts"] = datetime.now().timestamp()
            st.rerun()
        else:
            st.error("密码错误，请重试。")
    return False


# ---------------- 防刷额度（每人每日 AI 扩写次数限制） ----------------
# 限流计数以服务端（SQLite/Supabase）为准，按"昵称 + 日期"存取；
# session_state 仅作缓存加速。清 Cookie 换昵称无法绕过——服务端计数仍跟着旧昵称。
# 未配置昵称（默认"我的设备"）时退回 session_state 限流（同设备同会话内有效）。

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _quota_state() -> dict:
    """session_state 侧的额度缓存（兜底用）；跨天自动清零。"""
    q = st.session_state.get("api_quota")
    if not isinstance(q, dict) or q.get("date") != _today():
        q = {"date": _today(), "count": 0}
        st.session_state["api_quota"] = q
    return q


def quota_left() -> int:
    """查询剩余额度：优先读服务端，失败降级到 session_state。"""
    nick = _creator_name()
    if nick and nick != "我的设备":
        try:
            used = storage.get_daily_quota(nick, _today())
            st.session_state["api_quota"] = {"date": _today(), "count": used}
            return max(0, DAILY_LIMIT - used)
        except Exception:  # noqa: BLE001
            pass  # 服务端不可用 → 降级
    return max(0, DAILY_LIMIT - int(_quota_state()["count"]))


def consume_quota() -> None:
    """消耗 1 次额度：服务端 +1，同步更新 session_state 缓存。"""
    nick = _creator_name()
    if nick and nick != "我的设备":
        try:
            new_count = storage.incr_daily_quota(nick, _today())
            st.session_state["api_quota"] = {"date": _today(), "count": new_count}
            return
        except Exception:  # noqa: BLE001
            pass  # 服务端不可用 → 降级到 session_state
    q = _quota_state()
    q["count"] = int(q["count"]) + 1


def create_desktop_shortcut() -> str:
    """在桌面创建快捷方式，返回快捷方式路径。使用 VBScript 不受 PowerShell 策略限制。"""
    import winreg

    project_dir = Path(__file__).parent
    bat_path = project_dir / "启动.bat"
    # 用注册表查找真实桌面路径（可能被 OneDrive 重定向）
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        )
        desktop_dir = Path(winreg.QueryValueEx(key, "Desktop")[0])
    except OSError:
        desktop_dir = Path(os.path.expanduser("~")) / "Desktop"
    lnk_path = desktop_dir / "AI视频提示词工坊.lnk"

    vbs_content = (
        'Set ws = WScript.CreateObject("WScript.Shell")\n'
        f'Set sc = ws.CreateShortcut("{lnk_path}")\n'
        f'sc.TargetPath = "{bat_path}"\n'
        f'sc.WorkingDirectory = "{project_dir}"\n'
        'sc.IconLocation = "shell32.dll, 132"\n'
        'sc.Description = "AI 视频提示词工坊"\n'
        'sc.Save\n'
    )
    vbs_file = Path(tempfile.gettempdir()) / "create_shortcut.vbs"
    vbs_file.write_text(vbs_content, encoding="gbk")
    subprocess.run(
        ["cscript", "//nologo", str(vbs_file)],
        capture_output=True, check=True,
    )
    vbs_file.unlink(missing_ok=True)
    return str(lnk_path)

st.set_page_config(
    page_title="主角一致 · AI视频提示词工坊",
    page_icon="🎬",
    layout="wide",
)
storage.init_db()
storage.init_output_dirs()

# 密码门禁：未通过则只渲染登录页，阻止后续所有内容
if not check_password():
    st.stop()


def inject_css() -> None:
    """注入全局 CSS，实现简约卡片式 UI。"""
    st.markdown(
        """
        <style>
        /* ---- 隐藏默认噪音 ---- */
        #MainMenu, header, footer { visibility: hidden !important; }
        .stDeployButton { display: none !important; }

        /* ---- 全局字体与背景 ---- */
        .stApp {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                         "PingFang SC", "Microsoft YaHei", sans-serif;
            font-size: 15px;
            background: #FAFAFA;
        }
        .stApp > header { background: transparent; }

        /* ---- 主标题 ---- */
        h1 {
            font-weight: 700 !important;
            background: linear-gradient(135deg, #FF6B35, #FF9F1C);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        h2, h3 {
            font-weight: 600 !important;
            color: #333 !important;
        }

        /* ---- 卡片容器 ---- */
        .stVerticalBlock > div[data-testid="stVerticalBlock"] > div {
            border-radius: 12px;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 12px !important;
            box-shadow: 0 2px 12px rgba(0,0,0,0.06) !important;
            border: 1px solid #eee !important;
            background: #fff;
        }

        /* ---- 侧边栏 ---- */
        section[data-testid="stSidebar"] {
            background: linear-gradient(180deg, #FFF5F0 0%, #FFFAF5 100%);
        }
        section[data-testid="stSidebar"] .stRadio > div {
            gap: 4px;
        }
        section[data-testid="stSidebar"] label {
            padding: 8px 12px;
            border-radius: 8px;
            transition: background 0.2s;
        }
        section[data-testid="stSidebar"] label:hover {
            background: rgba(255,107,53,0.08);
        }

        /* ---- 按钮 ---- */
        .stButton > button {
            border-radius: 8px !important;
            font-weight: 500 !important;
            transition: all 0.2s !important;
        }
        .stButton > button:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }
        .stButton > button[kind="primary"] {
            background: linear-gradient(135deg, #FF6B35, #FF9F1C) !important;
            border: none !important;
            color: #fff !important;
        }

        /* ---- 输入框与文本域 ---- */
        .stTextInput > div > div > input,
        .stTextArea > div > textarea {
            border-radius: 8px !important;
            border: 1.5px solid #e0e0e0 !important;
            transition: border-color 0.2s !important;
        }
        .stTextInput > div > div > input:focus,
        .stTextArea > div > textarea:focus {
            border-color: #FF6B35 !important;
            box-shadow: 0 0 0 3px rgba(255,107,53,0.12) !important;
        }

        /* ---- 代码块 ---- */
        .stCodeBlock {
            border-radius: 10px !important;
            overflow: hidden;
        }
        .stCodeBlock > div {
            border-radius: 10px !important;
        }

        /* ---- Expander ---- */
        .stExpander > details {
            border-radius: 10px !important;
            border: 1px solid #eee !important;
            box-shadow: 0 1px 6px rgba(0,0,0,0.04) !important;
        }

        /* ---- Toast ---- */
        .stToast {
            border-radius: 10px !important;
        }

        /* ---- 减少间距 ---- */
        .stVerticalBlock {
            gap: 0.6rem !important;
        }

        /* ---- Notion/Linear 风排版：主区留白 + 标题节奏 ---- */
        .block-container { padding-top: 2.2rem; max-width: 1200px; }
        h1 { letter-spacing: -0.5px; }
        h2 { margin-top: 1.2rem !important; }
        h3 { margin-top: 0.9rem !important; }
        p, li { line-height: 1.65; }

        /* ---- 侧边栏仪表盘卡片 ---- */
        section[data-testid="stSidebar"] .side-card {
            background: #fff;
            border: 1px solid #f3e8df;
            border-radius: 14px;
            padding: 12px 14px;
            box-shadow: 0 2px 10px rgba(255, 107, 53, 0.07);
        }
        section[data-testid="stSidebar"] hr {
            margin: 0.8rem 0 !important;
        }

        /* ---- 创作者徽章（头像 + 昵称标签） ---- */
        .creator-row {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            vertical-align: middle;
        }
        .avatar-badge {
            border-radius: 50%;
            object-fit: cover;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            flex: none;
            box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
        }
        .creator-pill {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            border-radius: 999px;
            padding: 2px 10px;
            font-size: 12px;
            font-weight: 600;
            line-height: 18px;
            white-space: nowrap;
        }
        .creator-pill.me {
            background: #E8F7EE !important;
            color: #1B7F4D !important;
            border: 1px solid #BFE9D0;
        }
        .creator-pill.other {
            background: #F1F2F4 !important;
            color: #5A6169 !important;
            border: 1px solid #E2E4E8;
        }

        /* ---- 额度进度条：同款橘色渐变 ---- */
        .stProgress > div > div > div {
            background: linear-gradient(90deg, #FF6B35, #FF9F1C) !important;
        }

        /* ---- 手机端适配（窄屏自动优化） ---- */
        @media (max-width: 640px) {
            .stApp { font-size: 14px; }
            h1 { font-size: 1.45rem !important; }
            h2 { font-size: 1.2rem !important; }
            /* 按钮、输入框占满宽度，方便手指点击 */
            .stButton > button {
                width: 100% !important;
                min-height: 44px;
                font-size: 15px;
            }
            /* 代码块横向可滚动，不挤压页面 */
            .stCodeBlock { overflow-x: auto; }
            /* 侧边栏与主区间距收紧 */
            .css-1adrfps, section[data-testid="stSidebar"] { width: 100% !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_css()


# ---------------- 设备昵称与头像（只存 session_state，不进数据库） ----------------

def _creator_name() -> str:
    """当前设备昵称；未填写时用默认值。"""
    return (st.session_state.get("creator_name") or "我的设备").strip() or "我的设备"


def _get_avatar_map_cached() -> dict[str, str]:
    """『昵称 -> 头像 data URI』映射：从 user_profiles 全量加载，每会话缓存一次。
    历史记录/角色列表里任意创建者的头像都靠它渲染。
    加载失败静默降级（显示字母色块），保存配置后失效重取。"""
    cache = st.session_state.get("avatar_map_cache")
    if cache is None:
        try:
            cache = {
                (p.get("nickname") or ""): (p.get("avatar_base64") or "")
                for p in storage.list_user_profiles()
            }
            st.session_state["avatar_map_cache"] = cache
        except Exception:  # noqa: BLE001
            return {}
    return cache


def _invalidate_avatar_map_cache() -> None:
    st.session_state.pop("avatar_map_cache", None)


def _avatar_html(creator: str, size: int = 22) -> str:
    """头像 HTML：自己的设备优先用会话内最新头像（可能刚上传还没保存）；
    其他创建者从 user_profiles 档案表加载头像；
    都没有时用昵称首字母色块，颜色由昵称哈希决定。"""
    if creator == _creator_name() and st.session_state.get("avatar_uri"):
        uri = st.session_state["avatar_uri"]
    else:
        uri = _get_avatar_map_cached().get(creator, "")
    if uri:
        return (
            f'<img src="{uri}" class="avatar-badge" alt="{html.escape(creator)}" '
            f'style="width:{size}px;height:{size}px;" />'
        )
    initial = html.escape((creator or "?")[:1].upper())
    hue = int(hashlib.md5((creator or "?").encode("utf-8")).hexdigest()[:4], 16) % 360
    return (
        f'<span class="avatar-badge" '
        f'style="width:{size}px;height:{size}px;background:hsl({hue},65%,82%);'
        f'color:hsl({hue},55%,28%);font-size:{max(10, size // 2)}px;'
        f'line-height:{size}px;">{initial}</span>'
    )


def _creator_badge_html(creator: str, size: int = 20) -> str:
    """『头像 + 👤 由 xxx 生成』徽章：自己的设备绿色标签，他人灰色。"""
    own = creator == _creator_name()
    pill_cls = "me" if own else "other"
    label = "我的设备" if own else "由 TA 生成"
    return (
        f'<span class="creator-row">{_avatar_html(creator, size)}'
        f'<span class="creator-pill {pill_cls}">👤 {html.escape(creator)}'
        f'（{label}）</span></span>'
    )


def _user_card_html() -> str:
    """侧边栏顶部的用户预览卡：大头像 + 昵称 + 绿色『我的设备』标签。"""
    return (
        f'<div style="display:flex;align-items:center;gap:10px;margin-top:8px;">'
        f'{_avatar_html(_creator_name(), 40)}'
        f'<div><div style="font-weight:700;font-size:14px;">'
        f'{html.escape(_creator_name())}</div>'
        f'<span class="creator-pill me">🟢 我的设备</span></div></div>'
    )


# ---------------- 身份持久化（数据库 user_profiles + URL 书签） ----------------

def _apply_profile(profile: dict | None, fallback_name: str) -> None:
    """把数据库里的用户档案填充到 session_state（昵称 + 头像）。
    avatar_from_profile 标记：头像来自身份档案而非本次上传，
    防止 file_uploader 在后续 rerun 中返回 None 时误把档案头像清掉。"""
    st.session_state["creator_name"] = (profile or {}).get("nickname") or fallback_name
    uri = (profile or {}).get("avatar_base64") or ""
    if uri:
        st.session_state["avatar_uri"] = uri
        st.session_state["avatar_from_profile"] = True
    else:
        st.session_state.pop("avatar_uri", None)


def _cookie_get_all() -> dict | None:
    """读取浏览器 Cookie 字典。
    None  = Cookie 组件尚未就绪（首帧 default 未回传 / AppTest 无真实前端）→ 调用方等待；
    dict  = 已就绪（空 {} = 浏览器里没有本应用的 Cookie）；
    库不可用时返回空 dict，按"全新设备"处理。"""
    if _cookies is None:
        return {}
    try:
        data = _cookies.getAll()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _init_identity() -> None:
    """每帧检查浏览器 Cookie：有昵称就从数据库自动恢复身份（头像+昵称）。

    Cookie 值使用 HMAC 签名，签名校验失败视为未登录（防伪造他人身份）。
    状态机（不做一次性锁定——组件首帧给 default 空 dict，第二帧才回传真实
    Cookie，锁定会导致老用户永远恢复不了）：
    · identity_active=True（已恢复/已保存）：本帧起不再自动覆盖，用户手改昵称不被冲掉；
    · 未激活 + 组件未就绪(None)：等待下一帧；
    · 未激活 + Cookie 中有昵称：查库恢复头像与昵称，置 active；
    · 未激活 + Cookie 为空：保持初始填写界面（且不覆盖用户正在输入的内容）。"""
    if st.session_state.get("identity_active"):
        return
    all_cookies = _cookie_get_all()
    if all_cookies is None:
        return  # 组件尚未就绪，等组件回传后的下一帧
    raw_cookie = (all_cookies.get(COOKIE_KEY) or "").strip() if all_cookies else ""
    if not raw_cookie:
        return  # 真实的全新设备：保持初始界面
    # 校验 HMAC 签名：失败则丢弃，视为未登录
    nick = _cookie_verify(raw_cookie)
    if not nick:
        return  # 签名无效或被篡改：忽略，保持初始界面
    try:
        profile = storage.get_user_profile(nick)
    except Exception:  # noqa: BLE001
        profile = None
    if profile:
        _apply_profile(profile, nick)
    else:
        # Cookie 里有昵称但数据库无记录（如表被清）：恢复昵称，头像留空
        st.session_state["creator_name"] = nick
    st.session_state["identity_active"] = True


# ---------------- 侧边栏（仪表盘样式） ----------------
with st.sidebar:
    st.title("🎬 提示词工坊")
    st.caption("一个主角 · 无限视频 · 形象始终一致")

    # ---- 用户配置卡：昵称 + 头像 + Cookie 自动登录（独立于所有 st.form） ----
    _init_identity()  # 有 Cookie 就自动恢复身份（头像按昵称从数据库加载）
    _is_new_device = not st.session_state.get("identity_active")
    st.markdown('<div class="side-card">', unsafe_allow_html=True)
    st.text_input(
        "设备昵称", key="creator_name",
        placeholder="输入您的设备昵称",
        help="用于在历史记录和角色档案里标记是谁生成的；"
        "点『💾 保存我的配置』后存入数据库并写入浏览器 Cookie（365 天），"
        "以后打开本页自动恢复身份，无需任何操作。",
    )
    if _is_new_device:
        st.caption("👋 首次使用：填昵称、传头像后点『💾 保存我的配置』，"
                   "本浏览器会自动记住你的身份。")
    _avatar_file = st.file_uploader(
        "上传头像（jpg / png，≤2MB）", type=["jpg", "jpeg", "png"],
        help="头像仅在点击『保存我的配置』时随昵称写入一次数据库；"
        "平时展示用的是会话内存里的 Base64，不会频繁写云端。",
    )
    if _avatar_file is not None:
        # 用内容签名区分"rerun 带回的旧文件"和"新上传"：相同签名不覆盖
        # 当前头像（可能刚从身份档案/URL 恢复），避免恢复身份后被顶掉
        _sig = hashlib.md5(_avatar_file.getvalue()).hexdigest()
        if _sig != st.session_state.get("avatar_file_sig"):
            st.session_state["avatar_file_sig"] = _sig
            st.session_state.pop("avatar_from_profile", None)
            if _avatar_file.size > 2 * 1024 * 1024:
                st.error("头像超过 2MB，请压缩后再上传。")
                st.session_state.pop("avatar_uri", None)
            else:
                _mime = "image/png" if _avatar_file.type == "image/png" else "image/jpeg"
                st.session_state["avatar_uri"] = (
                    f"data:{_mime};base64,"
                    + base64.b64encode(_avatar_file.getvalue()).decode()
                )
    else:
        # 用户点掉了上传框的 ×：清签名；若头像不是来自身份档案则一并清掉
        st.session_state.pop("avatar_file_sig", None)
        if not st.session_state.get("avatar_from_profile"):
            st.session_state.pop("avatar_uri", None)
    st.markdown(_user_card_html(), unsafe_allow_html=True)

    # ---- 持久化：保存配置（数据库 + 浏览器 Cookie，后台自动记住） ----
    if st.button("💾 保存我的配置", use_container_width=True):
        _save_nick = _creator_name()
        _save_ok = True
        try:
            storage.upsert_user_profile(
                _save_nick, st.session_state.get("avatar_uri") or ""
            )
            _invalidate_avatar_map_cache()  # 让历史/角色列表立刻显示新头像
        except Exception as e:  # noqa: BLE001
            # 错误信息经 safe_error() 脱敏后再展示，避免泄露连接串/Key
            st.error(safe_error(e))
            _save_ok = False
        if _save_ok and _cookies is not None:
            try:
                # Cookie 只存昵称（头像 ~2.7MB 远超浏览器 4KB 单 Cookie 上限），
                # 头像按昵称从数据库加载；SameSite=Lax 防 CSRF，path=/ 全站生效。
                # Cookie 值使用 HMAC 签名，防止用户手改 Cookie 伪造他人身份。
                # Secure=True：仅 HTTPS 传输（Streamlit Cloud 全站 HTTPS；
                #   现代 Chrome/Edge/Firefox 对 http://localhost 也允许 Secure Cookie）。
                # 注意：streamlit-cookies-controller 0.0.4 不支持 HttpOnly 参数——
                #   该库通过前端 JS 组件读写 Cookie，HttpOnly 会使其无法读取。
                #   Cookie 内容仅为 HMAC 签名后的昵称，不含密码/密钥，风险可接受。
                # 注意：set 后绝不能立即 st.rerun()，否则组件来不及把 Cookie 落地。
                _cookies.set(
                    COOKIE_KEY, _cookie_sign(_save_nick),
                    max_age=COOKIE_MAX_AGE, path="/",
                    secure=True, same_site="lax",
                )
            except Exception as e:  # noqa: BLE001
                st.error(safe_error(e))
                _save_ok = False
        if _save_ok:
            st.session_state["identity_active"] = True
            st.toast("配置已保存", icon="💾")
    # ---- 登出按钮（仅配置了 APP_PASSWORD 时显示） ----
    if SECRETS["app_password"]:
        if st.button("🚪 登出", use_container_width=True,
                     help="清除本浏览器会话与 Cookie，返回登录页"):
            _do_logout()
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    page = st.radio(
        "导航",
        ["🎬 生成提示词", "🐱 主角档案", "🗂 生成历史", "⚙️ 设置"],
        key="nav_page",
        label_visibility="collapsed",
    )
    st.divider()

    # ---- 额度仪表盘 ----
    _left = quota_left()
    st.caption(f"🪙 今日 AI 额度：**{_left} / {DAILY_LIMIT}** 次剩余")
    st.progress(max(0.0, min(1.0, _left / DAILY_LIMIT)) if DAILY_LIMIT else 0.0)
    st.caption(
        "主角档案即你的“角色资产库”，\n重要数据请定期在【生成历史】里导出 JSON 备份。"
    )
    _sidebar_settings = get_effective_settings()
    _current_model = _sidebar_settings.get("model") or "未配置"
    _fallback_model = _sidebar_settings.get("model_fallback") or "无"
    st.caption(
        f"🤖 当前模型：**{_current_model}**"
        f"\n🔄 兜底模型：{_fallback_model}"
        f"\n🗄️ 数据后端：{'☁️ Supabase 云端' if storage.use_supabase() else '💾 本地 SQLite'}"
    )

EXAMPLE_CHARACTER = {
    "name": "小橘",
    "anchor": (
        "一只橘白相间的短毛小猫，约8个月大，圆滚滚的体型，"
        "圆圆的翠绿色大眼睛，粉色小鼻头，左耳尖有一小撮标志性白毛，"
        "背着天蓝色帆布小书包，脖子上系红色蝴蝶结"
    ),
    "style": "3D 皮克斯风格卡通动画，毛发细腻有光泽，暖色调，电影级质感",
    "negative": prompts.DEFAULT_NEGATIVE,
    "seed": "1024",
    "reference_note": "images/xiaoju_ref.png（定妆照：图生视频时始终上传同一张）",
}


# 分镜解析与多角色校验函数已移至 prompts.py（纯函数，可独立测试）：
# prompts.parse_result / prompts.parse_prompt_parts / prompts.shot_label /
# prompts.detect_multi_char_issues / prompts.count_words


# ---------------- AI 偷懒检测：杜绝"如上/As in Shot 1" ----------------
_LAZY_PATTERNS = re.compile(
    r"如上|同上|同镜头\s*1|同前|详见上文|参见上文|参考前文|前述|前文|上述|"
    r"角色详细特征如上|"
    r"Subject\s+as\s+(?:above|in\s+Shot)|Same\s+as\s+previous|"
    r"As\s+in\s+Shot|See\s+above|aforementioned|"
    r"previously\s+described|as\s+described|as\s+stated|refer\s+to",
    re.IGNORECASE,
)


def _detect_lazy_references(text: str) -> list[str]:
    """检测 AI 是否使用了'如上/As in Shot 1'等偷懒引用。返回匹配列表。"""
    return _LAZY_PATTERNS.findall(text)


def _calc_max_tokens(language: str, n_chars: int) -> int:
    """动态 max_tokens：多角色同框时自动提升，防止长文被截断。
    1 角色：中英对照 4000 / 其余 2000；2 角色 4000；3 角色 6000。"""
    if n_chars >= 3:
        return 6000
    if n_chars == 2:
        return 4000
    return 4000 if language == "中英对照" else 2000


def _run_ai_once(settings: dict, system: str, user_msg: str,
                 language: str, streaming: bool, placeholder,
                 n_chars: int = 1) -> str:
    """发起一次 AI 请求。流式时在 placeholder 里用 code 块逐字渲染
    （避免 Markdown 标题撑爆页面），返回拼接好的完整文本。"""
    mt = _calc_max_tokens(language, n_chars)
    fb = settings.get("model_fallback") or None
    if streaming:
        # 首个 chunk 到达前显示"构思中"提示，首个 chunk 到达后自动替换为 code 块
        placeholder.info("⚡ AI 正在构思，请稍候...")
        full_text = ""
        for chunk in llm.chat_stream(
            settings["api_base"], settings["api_key"],
            settings["model"], system, user_msg,
            max_tokens=mt, fallback_model=fb,
        ):
            full_text += chunk
            # 用 code 块渲染：等宽字体、不走 Markdown、标题不会变大；
            # height=400 限制高度，超出部分滚动，避免页面无限拉长
            placeholder.code(full_text, language="text", height=400)
        if not full_text.strip():
            raise RuntimeError("流式返回了空正文，请重试。")
        return full_text.strip()
    return llm.chat(
        settings["api_base"], settings["api_key"],
        settings["model"], system, user_msg,
        max_tokens=mt, fallback_model=fb,
    )


def _generate_with_retry(
    settings: dict, system: str, user_msg: str,
    language: str, word_count: int = 200, max_retries: int = 2,
    streaming: bool = False, placeholder=None,
    characters: list[dict] | None = None, n_chars: int = 1,
) -> tuple[str, list[str]]:
    """调用 AI 并检测偷懒引用 + 字数不足 + 多角色完整性；不达标则重试。

    streaming=True 时通过 placeholder.code() 实时打字机展示（code 块渲染，
    避免 Markdown 标题撑爆页面）；每次重试前用 placeholder.empty() 擦掉
    上一次的流式内容，避免页面堆积/重复。
    characters：多角色同框时传入角色列表，启用每镜每角色 ≥80 词强校验。
    返回 (结果, 质量问题描述列表)。
    """
    characters = characters or []
    attempts = 0
    result = ""
    issues: list[str] = []
    lazy_hits: list[str] = []
    actual_len = 0
    char_issues: list[str] = []
    current_user_msg = user_msg
    while attempts <= max_retries:
        if streaming and placeholder is not None:
            placeholder.empty()
            if attempts > 0:
                placeholder.warning(
                    f"🔁 第 {attempts} 次自动重试：" + "；".join(issues)
                )
        result = _run_ai_once(
            settings, system, current_user_msg, language, streaming, placeholder,
            n_chars=n_chars,
        )
        issues = []
        # 检查1：偷懒引用
        lazy_hits = _detect_lazy_references(result)
        if lazy_hits:
            issues.append(
                f"检测到偷懒引用：「{'、'.join(set(lazy_hits))}」"
            )
        # 检查2：字数不足（中英对照只统计中文部分，因为英文会翻倍）
        check_text = result.split("=====ENGLISH=====")[0] if "=====ENGLISH=====" in result else result
        actual_len = len(check_text.strip())
        min_len = int(word_count * 0.8)
        if actual_len < min_len:
            issues.append(
                f"长度不足：实际约 {actual_len} 词，要求 ≥{min_len} 词（设定的 80%）"
            )
        # 检查3：多角色同框完整性（每镜每角色 ≥80 词独立特征段）
        char_issues = prompts.detect_multi_char_issues(result, characters)
        issues.extend(char_issues)
        if not issues:
            # 校验通过：擦掉流式原文，交由下方结果卡片统一渲染 tabs，避免重复
            if streaming and placeholder is not None:
                placeholder.empty()
            return result, []
        attempts += 1
        if attempts <= max_retries:
            # 构建补救指令
            remedy_parts = []
            if lazy_hits:
                remedy_parts.append(
                    f"你上一次生成偷懒了！检测到「{'、'.join(set(lazy_hits))}」"
                    "等偷懒引用。请务必将角色特征在每一个镜头里完整重复一遍！"
                    "即使显得很啰嗦也必须写！绝对禁止使用'如上/同上/As in Shot 1'"
                    "等任何代词或省略句式！"
                )
            if actual_len < min_len:
                remedy_parts.append(
                    f"你上一次生成太短了，只有 {actual_len} 词。请在 {word_count} 词的"
                    f"范围内，扩充角色细节、光影和动作分解，达到 {min_len} 词以上。"
                    "不要用无意义的形容词堆砌！"
                )
            if char_issues:
                # 点名批评：具体到哪个镜头哪个角色只有多少词
                detail = "；".join(char_issues[:4])
                remedy_parts.append(
                    f"你上次生成严重偷懒了！{detail}。"
                    "请必须把每个角色的描述扩充到80词以上，并把所有角色特征"
                    "完整重写一遍，严禁特征融合、严禁遗漏任何角色！"
                )
            current_user_msg = user_msg + (
                "\n\n【系统严厉警告】" + " ".join(remedy_parts)
            )
    # 重试用尽：擦掉流式原文，结果卡片会带警告统一展示
    if streaming and placeholder is not None:
        placeholder.empty()
    return result, issues


# ---------------- 页面：生成提示词 ----------------
def page_generate() -> None:
    st.title("🎬 一句话 → 视频提示词")
    st.caption("输入“小猫走路去上课”，自动扩写成带分镜、运镜、光线、负面词的完整提示词")

    chars = _get_chars_cached()
    if not chars:
        st.warning(
            "还没有主角档案。请点击左侧导航【🐱 主角档案】创建主角——"
            "主角的固定外观是所有视频保持同一个形象的关键。"
        )
        return

    # ---- 卡片：主角选择（支持多角色同框） + 锚定预览 ----
    with st.container(border=True):
        all_names = [c["name"] for c in chars]
        saved_sel = st.session_state.get("selected_char_names") or []
        valid_default = [n for n in saved_sel if n in all_names] or [all_names[0]]
        selected_names = st.multiselect(
            "本片主角（可多选 2-3 个角色同框互动）",
            all_names,
            default=valid_default,
            max_selections=3,
            key="selected_char_names",
            help="选择 2-3 个角色可生成同框互动分镜；系统会自动扩大字数上限、"
            "提升 max_tokens，并对每个角色强制 80 词以上外观描述，防止特征融合。",
        )
        selected_chars = [c for c in chars if c["name"] in selected_names]

        if selected_chars:
            exp_title = "📌 查看固定锚定（每条提示词都会自动注入）" if len(selected_chars) == 1 \
                else f"📌 查看 {len(selected_chars)} 个角色的固定锚定"
            with st.expander(exp_title):
                for c in selected_chars:
                    st.markdown(f"**🎨 {c['name']} · 外观锚定：** {c['anchor']}")
                    st.markdown(f"　- 风格提示词：{c['style'] or '（未设置）'}")
                    st.markdown(f"　- 固定 Seed：{c['seed'] or '（建议生成首镜后锁定）'}")

    # ---- 卡片：创意输入 + 参数 + 按钮（用 st.form 隔离，调参数不触发重绘） ----
    n_sel = len(selected_chars)
    # 动态字数分配：角色越多，上限越高，确保每个角色的细节不被压缩
    if n_sel >= 3:
        wc_min, wc_max, wc_def = 300, 700, 500
    elif n_sel == 2:
        wc_min, wc_max, wc_def = 200, 500, 350
    else:
        wc_min, wc_max, wc_def = 100, 300, 200

    with st.container(border=True):
        with st.form("generate_form"):
            user_input = st.text_area(
                "一句话创意",
                key="user_input",
                height=90,
                placeholder="例如：小猫走路去上课 / 小橘第一次看到下雪，兴奋地追着雪花跑",
            )

            col1, col2, col3 = st.columns(3)
            shot_count = col1.slider("镜头数量", 1, 6, 4, help="💡 建议 3-5 个镜头以制作 10-20 秒连贯视频")
            ratio = col2.selectbox("画幅", ["16:9", "9:16", "1:1"], index=0)
            word_count = col3.slider(
                "提示词详细程度", wc_min, wc_max, wc_def, step=50,
                key=f"word_count_{n_sel}",
            )
            st.caption("💡 建议 3-5 个镜头以制作 10-20 秒连贯视频；景别须按 全景→中景→特写→全景 递进")
            if n_sel >= 2:
                st.caption(
                    f"🔍 检测到 {n_sel} 个角色，已自动扩大字数上限"
                    f"（{wc_min}-{wc_max} 词），确保角色细节不丢失。"
                )
            st.caption(
                "💡 提示：100-300 词为 AI 绘图/视频模型的最佳理解区间，"
                "过长的内容反而会被模型忽略。"
            )

            language = st.radio(
                "提示词输出语言",
                ["中文", "英文", "中英对照"],
                index=0,
                horizontal=True,
                help=(
                    "对 AI 智能扩写生效（本地模板为中文）；英文模式使用 "
                    "Close-up / Pan / Cinematic lighting 等专业术语；"
                    "中英对照模式结果区分中/英两个标签页，各自可一键复制"
                ),
            )

            stream_enabled = st.checkbox(
                "⚡ 开启打字机效果（流式输出）", value=False,
                help="默认关闭：等待生成完一步到位显示标签页；"
                "开启后 AI 内容逐字实时显示在代码块中"
                "（生成完毕自动整理为分镜标签页）。",
            )

            settings = get_effective_settings()
            ai_ready = bool(settings["api_key"].strip())

            mode_choice = st.radio(
                "生成模式",
                ["🎲 本地模板（免费/离线）", "✨ AI 智能扩写"],
                index=0,
                horizontal=True,
                help="本地模板完全免费离线；AI 智能扩写每次消耗 1 次今日额度",
            )
            do_generate = st.form_submit_button(
                "🚀 开始生成",
                type="primary",
                use_container_width=True,
                disabled=(mode_choice.startswith("✨") and not ai_ready),
                help="唯一触发点：拖动滑块、输入文字都不会重绘页面，"
                "点击此按钮才提交全部参数",
            )

    # 流式输出占位符：用 st.empty() 确保生成完成后能彻底清空，
    # 最终内容统一由下方结果卡片渲染，避免同一段内容显示两次
    stream_placeholder = st.empty()

    trigger_regen = st.session_state.pop("regen_requested", False)
    if do_generate or trigger_regen:
        if not user_input.strip():
            st.error("请先输入一句话创意。")
            return
        if not selected_chars:
            st.error("请先选择至少一个主角。")
            return
        primary = selected_chars[0]
        record_name = " + ".join(c["name"] for c in selected_chars)

        # 重新生成沿用上次模式；按钮提交则按生成模式单选决定
        if trigger_regen and not do_generate:
            use_ai = st.session_state.get("last_mode") == "AI扩写"
        else:
            use_ai = mode_choice.startswith("✨")

        if not use_ai:
            result = prompts.build_prompt_local(selected_chars, user_input, shot_count, ratio)
            mode, model = "本地模板", ""
        else:
            if quota_left() <= 0:
                st.error(
                    f"今日额度已用完（每人每天 {DAILY_LIMIT} 次）。"
                    "请明天再来，或先用本地模板生成（免费）。"
                )
                return
            try:
                recent = storage.get_recent_history(primary["id"], limit=3)
            except Exception:  # noqa: BLE001
                recent = []
            system, user_msg = prompts.build_llm_messages(
                selected_chars, user_input, shot_count, ratio, recent_history=recent,
                word_count=word_count, language=language,
            )
            if stream_enabled:
                # 打字机模式：code 块逐字渲染，生成完即清空，下方统一渲染结果
                try:
                    result, issues = _generate_with_retry(
                        settings, system, user_msg, language,
                        word_count=word_count, max_retries=2,
                        streaming=True, placeholder=stream_placeholder,
                        characters=selected_chars, n_chars=len(selected_chars),
                    )
                except Exception:  # noqa: BLE001
                    stream_placeholder.empty()
                    st.error("AI 正在休息，请稍后重试。")
                    st.caption("可能是网络波动或接口限流，稍等片刻再点一次「重新生成」即可。")
                    return
            else:
                with st.spinner("AI 正在构思，请稍候..."):
                    try:
                        result, issues = _generate_with_retry(
                            settings, system, user_msg, language,
                            word_count=word_count, max_retries=2,
                            streaming=False, placeholder=stream_placeholder,
                            characters=selected_chars, n_chars=len(selected_chars),
                        )
                    except Exception:  # noqa: BLE001
                        st.error("AI 正在休息，请稍后重试。")
                        st.caption("可能是网络波动或接口限流，稍等片刻再试一次即可。")
                        return
            if issues:
                st.session_state["lazy_warning"] = (
                    "AI 本次生成未达到要求：" + "；".join(issues)
                    + "。请尝试缩短字数或点击「重新生成」。"
                )
            else:
                st.session_state.pop("lazy_warning", None)
            consume_quota()
            mode, model = "AI扩写", settings["model"]

        try:
            storage.add_history(
                primary["id"], record_name, user_input, mode, result, model,
                creator_name=_creator_name(),
            )
            _invalidate_history_cache()
            # 自动保存到 output/prompts/（云端为容器内临时存储）
            saved_path = storage.save_prompt_to_file(
                result, record_name, mode, user_input
            )
            st.session_state["last_saved_file"] = str(saved_path)
        except Exception:  # noqa: BLE001
            st.session_state["last_saved_file"] = ""
            st.toast("⚠️ 生成成功，但保存失败，请检查网络连接", icon="⚠️")
        st.session_state["last_result"] = result
        st.session_state["last_mode"] = mode
        st.session_state["last_language"] = language
        if mode == "AI扩写":
            try:
                storage.auto_backup_to_json()
            except Exception:  # noqa: BLE001
                pass  # 备份失败不影响主流程，结果已在页面上
        st.toast("✅ 已生成并保存到历史记录", icon="✅")

    result = st.session_state.get("last_result")
    if result:
        saved_file = st.session_state.get("last_saved_file")
        # ---- 卡片：生成结果（针对 ComfyUI 拆为正/负面 4 块） ----
        with st.container(border=True):
            if saved_file:
                st.caption(f"📁 已同时存档（文件：`{Path(saved_file).name}`），历史记录里可随时查看。")
            lazy_warn = st.session_state.pop("lazy_warning", None)
            if lazy_warn:
                st.warning("⚠️ " + lazy_warn)
            st.subheader("生成结果（每个区块右上角图标独立复制，直接粘贴到 ComfyUI）")
            parts = prompts.parse_prompt_parts(result)
            zh_shots, _ = prompts.parse_result(result)
            multi_shot = len(zh_shots) > 1
            # 若无分镜结构（如本地模板），退回整块展示
            if len(zh_shots) <= 1 and not parts["en_pos"]:
                st.code(result, language="markdown", height=520)
            else:
                c1, c2 = st.columns(2)
                with c1:
                    st.caption("🇨🇳 中文 · 正面提示词（Positive）")
                    st.code(parts["zh_pos"] or "（无）", language="markdown", height=360)
                with c2:
                    st.caption("🇬🇧 英文 · 正面提示词（Positive）")
                    st.code(parts["en_pos"] or "（无英文版）", language="markdown", height=360)
                c3, c4 = st.columns(2)
                with c3:
                    st.caption("🇨🇳 中文 · 负面提示词（Negative）")
                    st.code(parts["zh_neg"] or "（无）", language="markdown", height=180)
                with c4:
                    st.caption("🇬🇧 英文 · 负面提示词（Negative）")
                    st.code(parts["en_neg"] or "（无英文版）", language="markdown", height=180)
                if multi_shot:
                    st.caption("💡 多镜头之间用 ----- 分隔，逐镜复制即可；如需逐镜标签页视图，可在历史记录里查看完整原文。")

            btn_dl, btn_re = st.columns(2)
            btn_dl.download_button(
                "⬇️ 下载为 txt",
                data=result,
                file_name="prompt.txt",
                mime="text/plain",
                use_container_width=True,
            )
            if btn_re.button(
                "🔄 重新生成",
                use_container_width=True,
                help="用当前页面参数再生成一次（AI 模式将消耗 1 次额度）",
            ):
                st.session_state["regen_requested"] = True
                st.rerun()


# ---------------- 页面：主角档案 ----------------
FORM_FIELDS = ["name", "anchor", "style", "negative", "seed", "reference_note"]


def page_characters() -> None:
    st.title("🐱 主角档案（角色卡）")
    st.caption(
        "把主角的外观固定成一段“锚定描述”，之后无论做多少条视频，"
        "提示词里都会原样带上它——这就是跨视频保持同一主角的核心。"
    )

    chars = _get_chars_cached()
    options = ["➕ 新建档案"] + [c["name"] for c in chars]

    # 切换下拉框时（回调早于控件重建），把所选档案灌入表单
    def on_picker_change() -> None:
        picked = st.session_state.get("char_picker", options[0])
        row = next((c for c in chars if c["name"] == picked), None)
        for key in FORM_FIELDS:
            st.session_state[f"f_{key}"] = row[key] if row else ""

    choice = st.selectbox(
        "选择档案", options, key="char_picker", on_change=on_picker_change
    )
    current = next((c for c in chars if c["name"] == choice), None)

    def fill_example() -> None:
        for key in FORM_FIELDS:
            st.session_state[f"f_{key}"] = EXAMPLE_CHARACTER[key]

    st.button(
        '🐱 用"小猫"示例填充（新建时可参考格式）',
        disabled=current is not None, on_click=fill_example,
    )

    # ---- 卡片：AI 智能生成角色设定（一句话 → 详细外观锚定；form 隔离） ----
    with st.container(border=True):
        st.subheader("🤖 AI 智能生成角色设定")
        st.caption("一句话描述你想要的角色，AI 自动生成详细外观锚定，填入下方表单供微调后保存。")
        _char_settings = get_effective_settings()
        _char_ai_ready = bool(_char_settings["api_key"].strip())
        with st.form("ai_char_form"):
            ai_char_desc = st.text_input(
                "一句话描述你想要的角色",
                key="ai_char_desc",
                placeholder="例如：一只穿和服的柴犬 / 赛博朋克风格的机器人少女",
            )
            gen_char = st.form_submit_button(
                "✨ AI 生成角色设定",
                type="primary",
                use_container_width=True,
                disabled=not _char_ai_ready,
                help="消耗 1 次今日额度" if _char_ai_ready else "请先在设置页配置 API Key",
            )
        if gen_char:
            if not ai_char_desc.strip():
                st.error("请先输入一句话描述。")
            elif quota_left() <= 0:
                st.error(f"今日额度已用完（每人每天 {DAILY_LIMIT} 次）。")
            else:
                with st.spinner("AI 正在构思角色设定…"):
                    try:
                        sys_msg, usr_msg = prompts.build_ai_character_messages(ai_char_desc)
                        generated = llm.chat(
                            _char_settings["api_base"], _char_settings["api_key"],
                            _char_settings["model"], sys_msg, usr_msg,
                            max_tokens=800,
                            fallback_model=_char_settings.get("model_fallback"),
                        )
                        consume_quota()
                        st.session_state["f_anchor"] = generated.strip()
                        # 如果名字为空，用描述前 8 个字作为默认名字
                        if not st.session_state.get("f_name", "").strip():
                            st.session_state["f_name"] = ai_char_desc.strip()[:8]
                        st.toast("✅ 角色设定已生成，请检查并微调后保存。")
                        st.rerun()
                    except Exception:  # noqa: BLE001
                        st.error("AI 正在休息，请稍后重试。")
                        st.caption("可能是网络波动或接口限流，请稍后再试。")

    # 表单提交同样放在回调里处理，保证在控件重建前读写 session_state
    def on_submit() -> None:
        vals = {
            key: st.session_state.get(f"f_{key}", "").strip()
            for key in FORM_FIELDS
        }
        if not vals["name"] or not vals["anchor"]:
            st.session_state["form_msg"] = (
                "error", "名字和外观锚定描述为必填项。"
            )
            return
        try:
            if current is not None:
                storage.update_character(
                    current["id"], vals["name"], vals["anchor"], vals["style"],
                    vals["negative"], vals["seed"], vals["reference_note"],
                )
                label = f"档案「{vals['name']}」已更新。"
            else:
                storage.create_character(
                    vals["name"], vals["anchor"], vals["style"],
                    vals["negative"], vals["seed"], vals["reference_note"],
                    creator_name=_creator_name(),
                )
                label = f"主角「{vals['name']}」创建成功，现在可以去生成提示词了。"
            st.session_state["form_msg"] = ("success", label)
            st.session_state["char_picker"] = options[0]
            for key in FORM_FIELDS:
                st.session_state[f"f_{key}"] = ""
            _invalidate_chars_cache()
        except Exception:  # noqa: BLE001
            st.session_state["form_msg"] = (
                "error", "保存失败，请检查网络连接（云端数据库暂不可用）。"
            )

    with st.container(border=True):
        with st.form("character_form"):
            st.text_input(
                "主角名字 *", key="f_name", value="", placeholder="例如：小橘",
            )
            st.text_area(
                "外观锚定描述 *（越具体越一致，每个镜头原样注入）",
                key="f_anchor", value="", height=150,
                placeholder=(
                    "例如：一只橘白相间的短毛小猫，圆滚滚的体型，"
                    "翠绿色大眼睛，粉色鼻头，背蓝色小书包，系红色蝴蝶结……"
                ),
            )
            st.text_input(
                "风格提示词 Style Prompt（注入每镜正面提示词）",
                key="f_style", value="",
                placeholder=(
                    "例如：3D 皮克斯风格, 毛发细腻有光泽, 暖色调, 电影级质感, "
                    "8k, highly detailed"
                ),
                help="将作为【风格提示词】注入 AI 生成的每个镜头正面提示词，"
                "切勿遗漏；建议使用逗号分隔的高密度视觉关键词。",
            )
            c1, c2 = st.columns(2)
            c1.text_input(
                "固定 Seed（可留空）", key="f_seed", value="",
                placeholder="例如 1024，锁定后主角更稳定",
            )
            c2.text_input(
                "负面提示词 Negative Prompt（留空则用默认）",
                key="f_negative", value="",
                placeholder="例如：human face, extra animals, deformed, blurry",
                help="将作为【负面提示词】注入每个镜头，AI 必须原样包含；"
                "留空则使用系统默认负面项。",
            )
            st.text_input(
                "参考图/定妆照备注（路径或链接，图生视频时复用同一张）",
                key="f_reference_note", value="",
            )
            st.form_submit_button(
                "💾 保存修改" if current else "➕ 创建档案",
                type="primary", on_click=on_submit,
            )

    msg = st.session_state.pop("form_msg", None)
    if msg:
        kind, text = msg
        (st.success if kind == "success" else st.error)(text)

    # ---- 卡片：角色资产一览（每条带创建者头像与颜色标签） ----
    if chars:
        with st.container(border=True):
            st.subheader("📋 角色资产一览")
            st.caption("绿色标签表示由当前设备创建，灰色为其他设备创建。")
            for c in chars:
                cc1, cc2 = st.columns([0.06, 0.94])
                with cc1:
                    st.markdown(
                        _creator_badge_html(c.get("creator_name") or "未知设备", 24),
                        unsafe_allow_html=True,
                    )
                with cc2:
                    st.markdown(
                        f"**{c['name']}** — {c['anchor'][:70]}"
                        + ("…" if len(c["anchor"]) > 70 else "")
                    )

    if current is not None:
        def ask_delete() -> None:
            st.session_state["confirm_delete"] = True

        def do_delete() -> None:
            try:
                storage.delete_character(current["id"])
                st.session_state["form_msg"] = (
                    "success", f"档案「{current['name']}」已删除。"
                )
            except Exception:  # noqa: BLE001
                st.session_state["form_msg"] = (
                    "error", "删除失败，请检查网络连接。"
                )
                return
            st.session_state["char_picker"] = options[0]
            for key in FORM_FIELDS:
                st.session_state[f"f_{key}"] = ""
            st.session_state["confirm_delete"] = False
            _invalidate_chars_cache()

        def cancel_delete() -> None:
            st.session_state["confirm_delete"] = False

        with st.container(border=True):
            st.button("🗑️ 删除此档案", on_click=ask_delete, type="secondary")
            if st.session_state.get("confirm_delete"):
                st.warning(
                    f"确认删除「{current['name']}」？历史提示词会保留，但不再关联该档案。"
                )
                col1, col2 = st.columns(2)
                col1.button("✅ 确认删除", on_click=do_delete)
                col2.button("取消", on_click=cancel_delete)


# ---------------- 页面：生成历史 ----------------
def page_history() -> None:
    st.title("🗂 生成历史")
    st.caption("支持关键词模糊搜索与按设备昵称筛选；每条记录可单独导出或删除（删除需先勾选确认）。")
    all_items = _get_history_cached()

    # 全局操作反馈 toast
    toast_err = st.session_state.pop("toast_err", None)
    if toast_err:
        st.error(toast_err)
    toast_msg = st.session_state.pop("toast_msg", None)
    if toast_msg:
        st.toast(toast_msg, icon="🗑️")

    if not all_items:
        st.info("还没有历史记录，去生成第一条提示词吧。")
        return

    # ---- 卡片：搜索与筛选（st.form 隔离，输入过程零重绘） ----
    creators = sorted({(it.get("creator_name") or "未知设备") for it in all_items})
    with st.container(border=True):
        with st.form("history_filter_form"):
            fc1, fc2 = st.columns([2, 1.2])
            fc1.text_input(
                "关键词搜索（创意 / 角色名 / 提示词内容）",
                key="hist_kw", placeholder="例如：小猫 / 下雪 / 野餐",
            )
            fc2.selectbox(
                "按设备昵称筛选", ["全部设备"] + creators, key="hist_creator",
            )
            st.form_submit_button("🔍 筛选", type="primary", use_container_width=True)
        # 表单提交后的值会保存在 widget state 里，这里直接读取应用过滤
        kw = (st.session_state.get("hist_kw") or "").strip().lower()
        sel_creator = st.session_state.get("hist_creator") or "全部设备"
        items = all_items
        if kw:
            items = [
                it for it in items
                if kw in (
                    (it.get("user_input") or "") + (it.get("character_name") or "")
                    + (it.get("result") or "")
                ).lower()
            ]
        if sel_creator != "全部设备":
            items = [
                it for it in items
                if (it.get("creator_name") or "未知设备") == sel_creator
            ]
        st.caption(f"共 {len(all_items)} 条记录，当前筛选出 {len(items)} 条。")

    # ---- 卡片：批量操作 ----
    with st.container(border=True):
        col1, col2 = st.columns(2)
        col1.download_button(
            "⬇️ 导出筛选结果（JSON 备份）",
            data=json.dumps(items, ensure_ascii=False, indent=2),
            file_name=f"prompt_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json",
            use_container_width=True,
        )
        if col2.button("🧹 清空全部历史", use_container_width=True):
            st.session_state["confirm_clear"] = True
        if st.session_state.get("confirm_clear"):
            st.warning("确认清空全部历史？此操作不可恢复。")
            c1, c2 = st.columns(2)
            if c1.button("✅ 确认清空"):
                try:
                    storage.clear_history()
                    _invalidate_history_cache()
                    st.session_state.pop("confirm_clear", None)
                    st.rerun()
                except Exception:  # noqa: BLE001
                    st.error("清空失败，请检查网络连接。")
            if c2.button("取消"):
                st.session_state.pop("confirm_clear", None)
                st.rerun()

    if not items:
        st.info("没有符合筛选条件的记录，试试换个关键词或设备。")
        return

    # ---- 记录列表 ----
    for item in items:
        creator = item.get("creator_name") or "未知设备"
        label = (
            f"[{item['created_at']}] {item['character_name']}："
            f"{item['user_input'][:40]}"
        )
        with st.expander(label):
            st.markdown(_creator_badge_html(creator), unsafe_allow_html=True)
            st.caption(
                f"模式：{item['mode']}　|　模型：{item['model'] or '—'}"
            )
            st.code(item["result"], language="text")

            def _delete_one_history(item_id: int) -> None:
                try:
                    storage.delete_history(item_id)
                    _invalidate_history_cache()
                    st.session_state["toast_msg"] = "已删除该条记录"
                except Exception:  # noqa: BLE001
                    st.session_state["toast_err"] = "删除失败，请检查网络连接。"

            bc, bd, be = st.columns(3)
            bc.download_button(
                "⬇️ 导出 TXT", data=item["result"],
                file_name=f"prompt_{item['id']}.txt", mime="text/plain",
                key=f"txt_{item['id']}", use_container_width=True,
            )
            bd.checkbox(
                "确认删除", key=f"cfm_{item['id']}",
                help="先勾选才能点删除，防止手滑误删自己或他人的记录",
            )
            be.button(
                "🗑️ 删除此条", key=f"del_{item['id']}",
                disabled=not st.session_state.get(f"cfm_{item['id']}", False),
                on_click=_delete_one_history, args=(item["id"],),
                use_container_width=True,
            )


# ---------------- 页面：设置 ----------------
def page_settings() -> None:
    st.title("⚙️ 设置：AI 扩写接口（可选）")
    st.caption(
        "不填也能用：本地模板模式完全免费。填入任意 OpenAI 兼容接口的 "
        "Key 后，可解锁大模型智能扩写。"
    )
    settings = storage.get_settings()
    key_via_secrets = bool(SECRETS["api_key"])

    if key_via_secrets:
        st.info(
            "🔐 当前 API Key 由部署者通过 **云端 Secrets** 统一配置，"
            "所有访客共用，此页面无需填写。"
        )

    # ---- 卡片：接口配置表单 ----
    with st.container(border=True):
        with st.form("settings_form"):
            api_base = st.text_input(
                "接口地址（Base URL）",
                value="" if SECRETS["api_base"] else settings["api_base"],
                placeholder="已由云端 Secrets 提供" if SECRETS["api_base"] else "",
                disabled=bool(SECRETS["api_base"]),
                help="结尾带不带 /chat/completions 都可以",
            )
            st.text_input(
                "API Key",
                value="" if key_via_secrets else settings["api_key"],
                type="password",
                placeholder="已由云端 Secrets 提供" if key_via_secrets else "",
                disabled=key_via_secrets,
                key="settings_api_key_input",
            )
            model = st.text_input(
                "模型名称",
                value="" if SECRETS["model"] else settings["model"],
                placeholder="已由云端 Secrets 提供" if SECRETS["model"] else "",
                disabled=bool(SECRETS["model"]),
            )
            saved = st.form_submit_button(
                "💾 保存", type="primary", disabled=key_via_secrets
            )

        if saved:
            try:
                storage.save_settings(
                    api_base,
                    st.session_state.get("settings_api_key_input", ""),
                    model,
                )
                _invalidate_settings_cache()
                st.success("设置已保存。")
            except Exception:  # noqa: BLE001
                st.error("设置保存失败，请检查网络连接。")

        # 测试连接始终使用“生效中”的配置（Secrets 优先）
        if st.button("🔌 测试当前连接"):
            effective = get_effective_settings()
            if not effective["api_key"].strip():
                st.error("请先填写并保存 API Key，或让管理员配置云端 Secrets。")
            else:
                with st.spinner("正在连接…"):
                    try:
                        reply = llm.ping(
                            effective["api_base"], effective["api_key"],
                            effective["model"],
                        )
                        st.success(f"连接成功，模型回复：{reply}")
                    except RuntimeError as exc:
                        st.error(safe_error(exc))

    # ---- 卡片：接口参考 ----
    with st.container(border=True):
        st.subheader("常用兼容接口参考")
        st.markdown(
            "| 服务商 | Base URL | 模型名示例 |\n"
            "|---|---|---|\n"
            "| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |\n"
            "| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |\n"
            "| 通义千问（兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |\n"
            "| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` |\n"
        )

    # ---- 卡片：数据备份 ----
    with st.container(border=True):
        st.subheader("💾 数据备份")
        st.caption(
            "把全部主角档案和历史记录导出为 JSON 文件，建议定期备份"
            "（云端运行环境会定期重置，未导出的数据可能丢失）。"
        )
        if st.button("💾 立即导出 JSON 备份", use_container_width=True):
            with st.spinner("正在导出…"):
                try:
                    backup_path = storage.auto_backup_to_json()
                    st.success(f"备份成功：`{backup_path}`")
                except Exception:  # noqa: BLE001
                    st.error("备份失败，请检查网络连接。")

    # ---- 桌面快捷方式：仅本机 Windows 显示，云端自动隐藏 ----
    if IS_WINDOWS:
        with st.container(border=True):
            st.subheader("🖥️ 桌面快捷方式")
            st.caption("一键在桌面创建快捷方式，双击即可启动软件。")
            if st.button("🖥️ 创建桌面快捷方式", use_container_width=True):
                try:
                    lnk_path = create_desktop_shortcut()
                    st.success(f"快捷方式已创建：`{lnk_path}`")
                except Exception as exc:  # noqa: BLE001
                    st.error(safe_error(exc))


# ---------------- 路由（全局异常兜底） ----------------
# Streamlit 默认会把未处理异常的完整 traceback 渲染到前端，
# 而异常消息/栈帧变量中可能夹带 st.secrets 内容（Key、URL、密码）。
# 这里统一兜底：前端只显示脱敏后的一句话，完整 traceback 仅留在服务端日志。
# 注意：StopException（st.stop）/ RerunException（st.rerun）是 Streamlit
# 的流程控制信号，必须原样放行，不能当作错误吞掉。
from streamlit.runtime.scriptrunner import RerunException as _Rerun
from streamlit.runtime.scriptrunner import StopException as _Stop

try:
    if page.startswith("🎬"):
        page_generate()
    elif page.startswith("🐱"):
        page_characters()
    elif page.startswith("🗂"):
        page_history()
    else:
        page_settings()
except (_Stop, _Rerun):
    raise
except Exception as _e:  # noqa: BLE001
    st.error(safe_error(_e))
    # 服务端日志保留完整异常（仅运维可见，不展示给前端用户）
    import traceback
    print("[ERROR] 未处理异常（前端已脱敏）：", safe_error(_e), flush=True)
    traceback.print_exc()
