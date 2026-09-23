"""LLM 调用层：兼容所有 OpenAI Chat Completions 格式的接口。

包括但不限于：DeepSeek、硅基流动 SiliconFlow、通义千问（兼容模式）、
Kimi、OpenAI、本地 Ollama（http://localhost:11434/v1）等。

同时提供：
- chat()：非流式调用，一次性返回完整文本（自动兜底切换）
- chat_stream()：流式调用，逐块 yield 文本 delta（自动兜底切换）

所有错误消息经过 security_utils.redact() 脱敏，确保 API Key / Token
不会通过异常文本泄露到 st.error 或日志。
"""
from __future__ import annotations

import json
from collections.abc import Generator

import requests

from security_utils import redact


def _build_url(api_base: str) -> str:
    base = api_base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _is_zhipu(api_base: str) -> bool:
    """是否为智谱 GLM 接口（需要为推理模型关闭 thinking，避免推理 token 吃光预算）。"""
    return "bigmodel.cn" in api_base


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }


def _payload(api_base: str, model: str, system: str, user: str,
             max_tokens: int, stream: bool = False) -> dict:
    payload = {
        "model": model.strip(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.8,
        # 给足输出空间，避免长提示词被截断产生残句/乱码
        "max_tokens": max_tokens,
        "stream": stream,
    }
    # 智谱 glm-4.5/4.6 系列默认开启深度推理（reasoning_content），简单一句"你好"
    # 都会烧掉上百推理 token，复杂任务更会吃光 max_tokens 导致正文为空。
    # 本场景 Prompt 已高度结构化，关闭推理更快更省、输出更可控；
    # flash 等非推理模型收到该参数会自动忽略，其他供应商则不发此参数。
    if _is_zhipu(api_base):
        payload["thinking"] = {"type": "disabled"}
    return payload


# ---------------- 非流式 ----------------

def chat(api_base: str, api_key: str, model: str,
         system: str, user: str, timeout: int = 90,
         max_tokens: int = 2000, fallback_model: str | None = None) -> str:
    """调用聊天补全接口，返回助手回复文本。

    主模型失败时，若提供 fallback_model（且与主模型不同）则自动用兜底模型
    重试一次；两者都失败时抛出包含两边错误详情的 RuntimeError。
    """
    try:
        return _chat_once(api_base, api_key, model, system, user, timeout, max_tokens)
    except RuntimeError as primary_exc:
        fb = (fallback_model or "").strip()
        if not fb or fb == model.strip():
            raise
        try:
            return _chat_once(api_base, api_key, fb, system, user, timeout, max_tokens)
        except RuntimeError as fallback_exc:
            raise RuntimeError(
                f"主模型 {model} 与兜底模型 {fb} 均调用失败。\n"
                f"主模型错误：{primary_exc}\n兜底模型错误：{fallback_exc}"
            ) from fallback_exc


def _chat_once(api_base: str, api_key: str, model: str,
               system: str, user: str, timeout: int, max_tokens: int) -> str:
    """单次非流式聊天补全请求，失败时抛出 RuntimeError。"""
    try:
        resp = requests.post(
            _build_url(api_base),
            headers=_headers(api_key),
            json=_payload(api_base, model, system, user, max_tokens, stream=False),
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"无法连接到接口 {api_base}，请检查网络或地址。") from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError("接口响应超时，请稍后重试。") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"接口返回错误（HTTP {resp.status_code}）：{redact(resp.text[:400])}"
        )

    try:
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(f"接口返回格式异常：{redact(resp.text[:400])}") from exc
    if not content.strip():
        # 推理模型在 max_tokens 不足时可能只产出 reasoning_content、正文为空
        raise RuntimeError(
            "接口返回了空正文（可能是输出 token 预算被推理过程耗尽，请重试）。"
        )
    return content.strip()


# ---------------- 流式 ----------------

def chat_stream(api_base: str, api_key: str, model: str,
                system: str, user: str, timeout: int = 90,
                max_tokens: int = 2000, fallback_model: str | None = None
                ) -> Generator[str, None, None]:
    """流式聊天补全：逐块 yield 文本 delta，供 st.write_stream 打字机展示。

    兜底策略：若主模型在产出任何内容之前就失败（连接/鉴权/限流），
    透明切换到 fallback_model 重试；若已经产出内容后中断则直接抛错
    （此时切换会拼出半段+整段的脏数据）。
    """
    yielded_any = False
    try:
        for piece in _chat_stream_once(
            api_base, api_key, model, system, user, timeout, max_tokens
        ):
            yielded_any = True
            yield piece
    except RuntimeError as primary_exc:
        fb = (fallback_model or "").strip()
        if yielded_any or not fb or fb == model.strip():
            raise
        try:
            for piece in _chat_stream_once(
                api_base, api_key, fb, system, user, timeout, max_tokens
            ):
                yield piece
        except RuntimeError as fallback_exc:
            raise RuntimeError(
                f"主模型 {model} 与兜底模型 {fb} 均流式调用失败。\n"
                f"主模型错误：{primary_exc}\n兜底模型错误：{fallback_exc}"
            ) from fallback_exc


def _chat_stream_once(api_base: str, api_key: str, model: str,
                      system: str, user: str, timeout: int,
                      max_tokens: int) -> Generator[str, None, None]:
    """单次 SSE 流式请求，逐块 yield delta.content；失败抛 RuntimeError。"""
    try:
        resp = requests.post(
            _build_url(api_base),
            headers=_headers(api_key),
            json=_payload(api_base, model, system, user, max_tokens, stream=True),
            timeout=timeout,
            stream=True,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"无法连接到接口 {api_base}，请检查网络或地址。") from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError("接口响应超时，请稍后重试。") from exc

    if resp.status_code != 200:
        # 流式接口出错时返回的是普通 JSON 错误体
        raise RuntimeError(
            f"接口返回错误（HTTP {resp.status_code}）：{redact(resp.text[:400])}"
        )

    got_content = False
    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except ValueError:
                continue
            # 个别供应商会在流里塞 error 对象
            if chunk.get("error"):
                raise RuntimeError(f"流式接口错误：{redact(str(chunk['error'])[:300])}")
            try:
                delta = chunk["choices"][0].get("delta", {}).get("content")
            except (KeyError, IndexError, AttributeError):
                continue
            # 注意：推理模型的 delta.reasoning_content 是思维链，必须忽略，
            # 只把正文 delta 推给打字机
            if delta:
                got_content = True
                yield delta
        if not got_content:
            raise RuntimeError(
                "流式返回了空正文（可能是输出 token 预算被推理过程耗尽，请重试）。"
            )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"流式传输中断：{exc}") from exc
    finally:
        resp.close()


def ping(api_base: str, api_key: str, model: str, timeout: int = 20) -> str:
    """连接测试：发一句最短请求，成功返回回复，失败抛出 RuntimeError。"""
    return chat(api_base, api_key, model,
                "你是连接测试助手。", "回复“连接成功”四个字即可。", timeout=timeout)
