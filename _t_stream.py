"""SSE 流式解析 + 兜底切换的离线单元测试（mock requests，不消耗 API）。"""
import requests

import llm


SSE_LINES = [
    'data: {"choices":[{"delta":{"content":"你好"}}]}',
    "",
    ': 注释行应被忽略',
    'data: {"choices":[{"delta":{"content":"，世界"}}]}',
    'data: {"choices":[{"delta":{"role":"assistant"}}]}',
    'data: [DONE]',
    'data: {"choices":[{"delta":{"content":"不应出现"}}]}',
]


class FakeResp:
    status_code = 200

    def __init__(self, lines=None, fail_after=None):
        self._lines = lines or []
        self._fail_after = fail_after
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        n = 0
        for line in self._lines:
            if self._fail_after is not None and n >= self._fail_after:
                raise requests.exceptions.ConnectionError("simulated drop")
            n += 1
            yield line

    def close(self):
        self.closed = True


def fake_post_factory(side_effects):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append({"url": url, "model": json["model"], "stream": json["stream"]})
        effect = side_effects[len(calls) - 1]
        if isinstance(effect, Exception):
            raise effect
        return effect
    return fake_post, calls


# 1) 正常流式：delta 拼接、忽略注释/role-only/[DONE] 后内容、payload 带 stream=True
fake_post, calls = fake_post_factory([FakeResp(SSE_LINES)])
llm.requests.post = fake_post
out = "".join(llm.chat_stream("https://x/v1", "k", "glm-4.5-air", "s", "u"))
assert out == "你好，世界", repr(out)
assert calls[0]["stream"] is True and calls[0]["model"] == "glm-4.5-air"
assert len(calls) == 1
print("[1] SSE 正常解析 PASS")

# 2) 主模型产出前失败 → 自动切换兜底模型
fake_post, calls = fake_post_factory([
    requests.exceptions.ConnectionError("boom"),
    FakeResp(SSE_LINES),
])
llm.requests.post = fake_post
out = "".join(llm.chat_stream("https://x/v1", "k", "glm-4.5-air", "s", "u",
                              fallback_model="glm-4.7-flash"))
assert out == "你好，世界"
assert [c["model"] for c in calls] == ["glm-4.5-air", "glm-4.7-flash"], calls
print("[2] 产出前失败自动兜底 PASS")

# 3) 已产出内容后中断 → 不做兜底（避免脏拼接），直接抛 RuntimeError
fake_post, calls = fake_post_factory([FakeResp(SSE_LINES, fail_after=1)])
llm.requests.post = fake_post
try:
    list(llm.chat_stream("https://x/v1", "k", "glm-4.5-air", "s", "u",
                         fallback_model="glm-4.7-flash"))
    raise AssertionError("应当抛出 RuntimeError")
except RuntimeError:
    pass
assert len(calls) == 1, "产出后中断不应切换兜底模型"
print("[3] 产出后中断不兜底 PASS")

# 4) 非流式接口回归：chat() 仍走 stream=False
fake_post, calls = fake_post_factory([type("R", (), {
    "status_code": 200,
    "text": "",
    "json": lambda self: {"choices": [{"message": {"content": "ok"}}]},
})()])
llm.requests.post = fake_post
assert llm.chat("https://x/v1", "k", "m", "s", "u") == "ok"
assert calls[0]["stream"] is False
print("[4] 非流式 chat() 回归 PASS")

print("=== ALL STREAM TESTS PASSED ===")
