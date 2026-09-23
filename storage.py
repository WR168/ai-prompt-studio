"""存储层：主角档案（角色卡）、生成历史、LLM 设置。

双后端设计：
- Supabase（PostgREST REST API）：配置了 [supabase] secrets 时启用，云端数据持久
- 本地 SQLite（data/app.db）：未配置 Supabase 时自动回退，零配置离线可用

三张表（两个后端字段完全一致）：
- characters：主角的固定外观锚定描述，是"多视频同一主角"的核心
- history：每次生成的提示词记录
- settings：LLM API 的 base_url / key / model
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from security_utils import redact

# 均为相对项目根目录的跨平台路径（Windows / macOS / Linux / Streamlit Cloud 通用）
DB_PATH = Path(__file__).parent / "data" / "app.db"

# 输出目录：自动保存的提示词文件和 JSON 备份
OUTPUT_DIR = Path(__file__).parent / "output"
PROMPTS_DIR = OUTPUT_DIR / "prompts"
BACKUPS_DIR = OUTPUT_DIR / "backups"


# ================= Supabase（云端 Postgres / PostgREST） =================

_SB_URL = ""
_SB_KEY = ""


def configure_supabase(url: str, key: str) -> None:
    """注入 Supabase 配置（由 app.py 启动时调用）。

    url 填 Project URL 即可，带不带 /rest/v1 后缀都兼容。
    """
    global _SB_URL, _SB_KEY
    u = (url or "").strip().rstrip("/")
    if u.endswith("/rest/v1"):
        u = u[: -len("/rest/v1")]
    _SB_URL = u
    _SB_KEY = (key or "").strip()


def use_supabase() -> bool:
    """是否已启用云端数据库。"""
    return bool(_SB_URL and _SB_KEY)


def _sb_request(method: str, table: str, *, params: dict | None = None,
                json_body: Any = None, prefer: str | None = None) -> list[dict[str, Any]]:
    """PostgREST 通用请求；非 2xx 抛 RuntimeError（含 RLS 权限错误原文）。"""
    url = f"{_SB_URL}/rest/v1/{table}"
    headers: dict[str, str] = {
        "apikey": _SB_KEY,
        "Authorization": f"Bearer {_SB_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    try:
        resp = requests.request(
            method, url, headers=headers, params=params,
            json=json_body, timeout=30,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"无法连接 Supabase（{_SB_URL}），请检查网络或 SUPABASE_URL 是否正确。"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError("Supabase 响应超时，请稍后重试。") from exc

    if resp.status_code >= 300:
        # RLS 拦截时 PostgREST 会返回 401/403 + 错误详情，原样抛给上层展示
        # 响应体经 redact() 脱敏，避免 API Key / Token 泄露到 st.error
        raise RuntimeError(
            f"Supabase {method} {table} 失败（HTTP {resp.status_code}）："
            f"{redact(resp.text[:300])}"
        )
    if not resp.content:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


# ================= 本地 SQLite =================

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_output_dirs() -> None:
    """创建输出目录（首次运行自动执行，已存在则跳过）。"""
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    """建表（仅 SQLite 后端需要；首次运行自动执行，已存在则跳过）。

    同时做轻量迁移：老库缺 creator_name 列时自动补上。
    """
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS characters (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT    NOT NULL UNIQUE,
                anchor         TEXT    NOT NULL,
                style          TEXT    NOT NULL DEFAULT '',
                negative       TEXT    NOT NULL DEFAULT '',
                seed           TEXT    NOT NULL DEFAULT '',
                reference_note TEXT    NOT NULL DEFAULT '',
                created_at     TEXT    NOT NULL,
                updated_at     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id   INTEGER REFERENCES characters(id) ON DELETE SET NULL,
                character_name TEXT    NOT NULL,
                user_input     TEXT    NOT NULL,
                mode           TEXT    NOT NULL,
                model          TEXT    NOT NULL DEFAULT '',
                result         TEXT    NOT NULL,
                created_at     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname      TEXT    NOT NULL UNIQUE,
                avatar_base64 TEXT    NOT NULL DEFAULT '',
                created_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_quota (
                nickname      TEXT    NOT NULL,
                quota_date    TEXT    NOT NULL,
                count         INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (nickname, quota_date)
            );
            """
        )
        _ensure_column(conn, "characters", "creator_name",
                       "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "history", "creator_name",
                       "TEXT NOT NULL DEFAULT ''")


def _ensure_column(conn: sqlite3.Connection, table: str,
                   column: str, decl: str) -> None:
    """SQLite 轻量迁移：表缺少指定列时自动 ALTER TABLE 补上。"""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ================= Supabase 后端实现 =================

def _sb_list_characters() -> list[dict[str, Any]]:
    return _sb_request(
        "GET", "characters", params={"select": "*", "order": "updated_at.desc"}
    )


def _sb_get_character(char_id: int) -> dict[str, Any] | None:
    rows = _sb_request(
        "GET", "characters", params={"select": "*", "id": f"eq.{char_id}"}
    )
    return rows[0] if rows else None


def _sb_post_row(table: str, payload: dict[str, Any],
                 prefer: str) -> list[dict[str, Any]]:
    """POST 单行数据；远端尚未添加 creator_name 列（PGRST204 schema cache 报错）
    时自动去掉该列重试一次，保证老表结构不被破坏、新字段渐进生效。"""
    try:
        return _sb_request("POST", table, json_body=[payload], prefer=prefer)
    except RuntimeError as exc:
        if "creator_name" in payload and "creator_name" in str(exc):
            fallback = {k: v for k, v in payload.items() if k != "creator_name"}
            return _sb_request("POST", table, json_body=[fallback], prefer=prefer)
        raise


def _sb_create_character(name: str, anchor: str, style: str, negative: str,
                         seed: str, reference_note: str,
                         creator_name: str = "") -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = _sb_post_row("characters", {
        "name": name.strip(), "anchor": anchor.strip(),
        "style": style.strip(), "negative": negative.strip(),
        "seed": seed.strip(), "reference_note": reference_note.strip(),
        "creator_name": creator_name.strip(),
        "created_at": now, "updated_at": now,
    }, prefer="return=representation")
    return int(rows[0]["id"])


def _sb_update_character(char_id: int, name: str, anchor: str, style: str,
                         negative: str, seed: str, reference_note: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _sb_request(
        "PATCH", "characters",
        params={"id": f"eq.{char_id}"},
        json_body={
            "name": name.strip(), "anchor": anchor.strip(),
            "style": style.strip(), "negative": negative.strip(),
            "seed": seed.strip(), "reference_note": reference_note.strip(),
            "updated_at": now,
        },
    )


def _sb_delete_character(char_id: int) -> None:
    _sb_request("DELETE", "characters", params={"id": f"eq.{char_id}"})


def _sb_add_history(character_id: int | None, character_name: str,
                    user_input: str, mode: str, result: str, model: str,
                    creator_name: str = "") -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = _sb_post_row("history", {
        "character_id": character_id, "character_name": character_name,
        "user_input": user_input.strip(), "mode": mode,
        "model": model, "result": result,
        "creator_name": creator_name.strip(),
        "created_at": now,
    }, prefer="return=representation")
    return int(rows[0]["id"])


def _sb_list_history(limit: int) -> list[dict[str, Any]]:
    return _sb_request(
        "GET", "history",
        params={"select": "*", "order": "id.desc", "limit": limit},
    )


def _sb_delete_history(history_id: int) -> None:
    _sb_request("DELETE", "history", params={"id": f"eq.{history_id}"})


def _sb_clear_history() -> None:
    # id 为自增正整数，用 gt.0 匹配全部行；不要用 "neq.null"——
    # PostgREST 会把 "null" 当字符串转 bigint 失败（HTTP 400 invalid input syntax）
    _sb_request("DELETE", "history", params={"id": "gt.0"})


def _sb_get_settings() -> dict[str, str]:
    rows = _sb_request("GET", "settings", params={"select": "key,value"})
    return {r["key"]: r["value"] for r in rows}


def _sb_save_settings(api_base: str, api_key: str, model: str) -> None:
    rows = [
        {"key": k, "value": v.strip()}
        for k, v in (("api_base", api_base), ("api_key", api_key), ("model", model))
    ]
    _sb_request(
        "POST", "settings", json_body=rows,
        prefer="resolution=merge-duplicates",  # 主键冲突时更新（等价于 upsert）
    )


def _sb_get_recent_history(character_id: int, limit: int) -> list[dict[str, Any]]:
    return _sb_request(
        "GET", "history",
        params={
            "select": "user_input,result,mode",
            "character_id": f"eq.{character_id}",
            "mode": "eq.AI扩写",
            "order": "id.desc",
            "limit": limit,
        },
    )


# ================= 用户档案（设备昵称 + Base64 头像持久化） =================

def _sb_upsert_user_profile(nickname: str, avatar_base64: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _sb_request(
        "POST", "user_profiles",
        params={"on_conflict": "nickname"},  # 按 nickname 唯一约束 upsert
        json_body=[{
            "nickname": nickname.strip(),
            "avatar_base64": avatar_base64,
            "created_at": now,
        }],
        prefer="resolution=merge-duplicates",
    )


def _sb_list_user_nicknames() -> list[str]:
    rows = _sb_request(
        "GET", "user_profiles",
        params={"select": "nickname", "order": "nickname.asc"},
    )
    return [r["nickname"] for r in rows]


def _sb_list_user_profiles() -> list[dict[str, Any]]:
    """全部用户档案（昵称 + 头像），供展示历史/角色记录时渲染他人头像。"""
    return _sb_request(
        "GET", "user_profiles",
        params={"select": "nickname,avatar_base64", "order": "nickname.asc"},
    )


def _sb_get_user_profile(nickname: str) -> dict[str, Any] | None:
    rows = _sb_request(
        "GET", "user_profiles",
        params={"select": "*", "nickname": f"eq.{nickname.strip()}"},
    )
    return rows[0] if rows else None


# ================= 对外统一接口（自动选择后端） =================

# ---------------- 主角档案 ----------------

def list_characters() -> list[dict[str, Any]]:
    if use_supabase():
        return _sb_list_characters()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM characters ORDER BY datetime(updated_at) DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_character(char_id: int) -> dict[str, Any] | None:
    if use_supabase():
        return _sb_get_character(char_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE id = ?", (char_id,)
        ).fetchone()
        return dict(row) if row else None


def create_character(name: str, anchor: str, style: str,
                     negative: str, seed: str, reference_note: str,
                     creator_name: str = "") -> int:
    if use_supabase():
        return _sb_create_character(name, anchor, style, negative, seed,
                                    reference_note, creator_name)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO characters
               (name, anchor, style, negative, seed, reference_note,
                creator_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name.strip(), anchor.strip(), style.strip(), negative.strip(),
             seed.strip(), reference_note.strip(), creator_name.strip(),
             now, now),
        )
        return int(cur.lastrowid)


def update_character(char_id: int, name: str, anchor: str, style: str,
                     negative: str, seed: str, reference_note: str) -> None:
    if use_supabase():
        _sb_update_character(char_id, name, anchor, style, negative, seed, reference_note)
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        conn.execute(
            """UPDATE characters SET
                 name=?, anchor=?, style=?, negative=?, seed=?,
                 reference_note=?, updated_at=?
               WHERE id=?""",
            (name.strip(), anchor.strip(), style.strip(), negative.strip(),
             seed.strip(), reference_note.strip(), now, char_id),
        )


def delete_character(char_id: int) -> None:
    if use_supabase():
        _sb_delete_character(char_id)
        return
    with _connect() as conn:
        conn.execute("DELETE FROM characters WHERE id=?", (char_id,))


# ---------------- 生成历史 ----------------

def add_history(character_id: int | None, character_name: str, user_input: str,
                mode: str, result: str, model: str = "",
                creator_name: str = "") -> int:
    if use_supabase():
        return _sb_add_history(character_id, character_name, user_input, mode,
                               result, model, creator_name)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO history
               (character_id, character_name, user_input, mode, model, result,
                creator_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (character_id, character_name, user_input.strip(), mode,
             model, result, creator_name.strip(), now),
        )
        return int(cur.lastrowid)


def list_history(limit: int = 100) -> list[dict[str, Any]]:
    if use_supabase():
        return _sb_list_history(limit)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_history(history_id: int) -> None:
    if use_supabase():
        _sb_delete_history(history_id)
        return
    with _connect() as conn:
        conn.execute("DELETE FROM history WHERE id=?", (history_id,))


def clear_history() -> None:
    if use_supabase():
        _sb_clear_history()
        return
    with _connect() as conn:
        conn.execute("DELETE FROM history")


# ---------------- 设置 ----------------

DEFAULT_SETTINGS = {
    "api_base": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "",
    "model": "glm-4.5-air",
}


def get_settings() -> dict[str, str]:
    if use_supabase():
        return {**DEFAULT_SETTINGS, **_sb_get_settings()}
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    saved = {r["key"]: r["value"] for r in rows}
    return {**DEFAULT_SETTINGS, **saved}


def save_settings(api_base: str, api_key: str, model: str) -> None:
    if use_supabase():
        _sb_save_settings(api_base, api_key, model)
        return
    with _connect() as conn:
        for key, value in (("api_base", api_base), ("api_key", api_key),
                           ("model", model)):
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value.strip()),
            )


# ---------------- 用户档案（设备昵称 + 头像持久化） ----------------

def upsert_user_profile(nickname: str, avatar_base64: str) -> None:
    """按昵称存入或更新用户档案；avatar_base64 传完整 data URI 或空串。"""
    if not (nickname or "").strip():
        return
    if use_supabase():
        _sb_upsert_user_profile(nickname, avatar_base64 or "")
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        conn.execute(
            """INSERT INTO user_profiles(nickname, avatar_base64, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(nickname)
               DO UPDATE SET avatar_base64 = excluded.avatar_base64""",
            (nickname.strip(), avatar_base64 or "", now),
        )


def list_user_nicknames() -> list[str]:
    """所有已保存的昵称（不拉头像大字段）。"""
    if use_supabase():
        return _sb_list_user_nicknames()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT nickname FROM user_profiles ORDER BY nickname"
        ).fetchall()
    return [r["nickname"] for r in rows]


def list_user_profiles() -> list[dict[str, Any]]:
    """全部用户档案（nickname + avatar_base64）；
    用于在历史/角色列表里把每位创建者的头像渲染出来。"""
    if use_supabase():
        return _sb_list_user_profiles()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT nickname, avatar_base64 FROM user_profiles ORDER BY nickname"
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_profile(nickname: str) -> dict[str, Any] | None:
    """按昵称查单条档案（含头像 Base64）；不存在返回 None。"""
    if not (nickname or "").strip():
        return None
    if use_supabase():
        return _sb_get_user_profile(nickname)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_profiles WHERE nickname = ?",
            (nickname.strip(),),
        ).fetchone()
    return dict(row) if row else None


# ---------------- 服务端限流（每日 AI 调用次数，按昵称 + 日期） ----------------
# 防 Cookie 绕过：清 Cookie 换昵称即可重置额度的漏洞被封堵——
# 限流计数存数据库，按"昵称 + 当日日期"唯一约束，跨设备/跨会话生效。

def _sb_get_quota(nickname: str, quota_date: str) -> int:
    rows = _sb_request(
        "GET", "daily_quota",
        params={"select": "count",
                "nickname": f"eq.{nickname.strip()}",
                "quota_date": f"eq.{quota_date}"},
    )
    return int(rows[0]["count"]) if rows else 0


def _sb_rpc_incr_quota(nickname: str, quota_date: str) -> int | None:
    """调用 Supabase RPC 函数 incr_daily_quota 做服务端原子自增。

    函数为 SECURITY DEFINER（见 README 的 RLS SQL），即使 RLS 禁止 anon
    UPDATE/DELETE 也能自增计数。RPC 未部署（404）或任何异常时返回 None，
    由调用方降级到旧的"先查后写"路径。
    """
    url = f"{_SB_URL}/rest/v1/rpc/incr_daily_quota"
    headers = {
        "apikey": _SB_KEY,
        "Authorization": f"Bearer {_SB_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            url, headers=headers,
            json={"p_nickname": nickname.strip(), "p_quota_date": quota_date},
            timeout=30,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code >= 300:
        return None  # 404=函数未部署 / 401·403=权限问题：降级
    try:
        return int(resp.json())
    except (ValueError, TypeError):
        return None


def _sb_incr_quota(nickname: str, quota_date: str) -> int:
    """原子自增：存在则 +1，不存在则插入 1。返回自增后的值。

    优先走 SECURITY DEFINER RPC（RLS 收紧后唯一可靠路径）；
    RPC 不可用时降级为"先查后写"upsert（要求表有 anon UPDATE 权限）。
    """
    rpc_result = _sb_rpc_incr_quota(nickname, quota_date)
    if rpc_result is not None:
        return rpc_result
    # 降级路径：未部署 RPC / 未收紧 RLS 的旧项目
    current = _sb_get_quota(nickname, quota_date)
    new_count = current + 1
    payload = {
        "nickname": nickname.strip(), "quota_date": quota_date,
        "count": new_count,
    }
    _sb_request(
        "POST", "daily_quota", json_body=[payload],
        prefer="resolution=merge-duplicates",
        params={"on_conflict": "nickname,quota_date"},
    )
    return new_count


def get_daily_quota(nickname: str, quota_date: str) -> int:
    """查询指定昵称当日已用额度。"""
    if not nickname.strip():
        return 0
    if use_supabase():
        try:
            return _sb_get_quota(nickname, quota_date)
        except Exception:  # noqa: BLE001
            return 0  # 查询失败降级为 0（不阻断用户）
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM daily_quota WHERE nickname=? AND quota_date=?",
            (nickname.strip(), quota_date),
        ).fetchone()
    return int(row["count"]) if row else 0


def incr_daily_quota(nickname: str, quota_date: str) -> int:
    """自增并返回当日新额度值。"""
    if not nickname.strip():
        return 0
    if use_supabase():
        try:
            return _sb_incr_quota(nickname, quota_date)
        except Exception:  # noqa: BLE001
            return 0  # 写入失败降级（不阻断用户主流程）
    with _connect() as conn:
        conn.execute(
            "INSERT INTO daily_quota(nickname, quota_date, count) VALUES(?, ?, 1) "
            "ON CONFLICT(nickname, quota_date) DO UPDATE SET count = count + 1",
            (nickname.strip(), quota_date),
        )
        row = conn.execute(
            "SELECT count FROM daily_quota WHERE nickname=? AND quota_date=?",
            (nickname.strip(), quota_date),
        ).fetchone()
    return int(row["count"]) if row else 1


# ---------------- 自动保存到本地文件 ----------------

def save_prompt_to_file(result: str, character_name: str, mode: str,
                        user_input: str) -> Path:
    """把生成结果保存为 .txt 文件到 output/prompts/，返回文件路径。"""
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = character_name.replace(" ", "_")
    filename = f"{now}_{safe_name}_{mode}.txt"
    filepath = PROMPTS_DIR / filename
    filepath.write_text(result, encoding="utf-8")
    return filepath


def get_recent_history(character_id: int, limit: int = 3) -> list[dict[str, Any]]:
    """获取指定主角最近 N 条 AI 扩写记录，用于上下文记忆。"""
    if use_supabase():
        return _sb_get_recent_history(character_id, limit)
    with _connect() as conn:
        rows = conn.execute(
            """SELECT user_input, result, mode FROM history
               WHERE character_id = ? AND mode = 'AI扩写'
               ORDER BY id DESC LIMIT ?""",
            (character_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def auto_backup_to_json() -> Path:
    """把所有角色档案 + 历史记录导出为 JSON 备份到 output/backups/。"""
    chars = list_characters()
    history = list_history(limit=500)
    data = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "characters": chars,
        "history": history,
    }
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = BACKUPS_DIR / f"backup_{now}.json"
    filepath.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return filepath
