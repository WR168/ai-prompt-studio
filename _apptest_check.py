"""Streamlit AppTest：在服务端语义层完整模拟用户操作（不依赖浏览器事件）。"""
import shutil
from pathlib import Path

from streamlit.testing.v1 import AppTest

import storage

# 干净数据库开始
if Path("data").exists():
    shutil.rmtree("data")
storage.init_db()
storage.create_character(
    "小橘",
    "一只橘白相间的短毛小猫，翠绿色大眼睛，背蓝色小书包，系红色蝴蝶结",
    "3D皮克斯风", "", "1024", "ref.png",
)

at = AppTest.from_file("app.py", default_timeout=15)
# 测试环境隔离：屏蔽本地 secrets.toml 里的真实 Supabase/密码/AI 配置，
# 强制走 SQLite + 免密 + 无 Key，保证测试不碰云端、不受本地配置影响
at.secrets["APP_PASSWORD"] = ""
at.secrets["supabase"] = {"SUPABASE_URL": "", "SUPABASE_KEY": ""}
at.secrets["llm"] = {"API_BASE": "", "API_KEY": "", "MODEL": ""}
# fail-closed 校验要求 COOKIE_SIGN_KEY ≥32 位；测试用假密钥（非真实凭据）
at.secrets["COOKIE_SIGN_KEY"] = "0123456789abcdef0123456789abcdef0123456789abcdef"
at.run()
assert not at.exception, at.exception

# ---------- 1. 生成页：输入一句话 -> 生成模式默认本地模板 -> 点开始生成 ----------
at.text_area[0].input("小猫走路去上课")
next(b for b in at.button if "开始生成" in b.label).click().run()
assert not at.exception, at.exception
assert not at.error, [e.value for e in at.error]
result = at.session_state["last_result"]
for token in ("小猫走路去上课", "主角锚定", "镜头 1", "镜头 3", "负面提示词"):
    assert token in result, f"缺少 {token}"
print("[1] 生成页 PASS，提示词长度 =", len(result))

# 历史已落库（含设备昵称 creator_name 默认值）
hist = storage.list_history()
assert len(hist) == 1 and hist[0]["character_name"] == "小橘"
assert hist[0].get("creator_name") == "我的设备", hist[0].get("creator_name")
print("[2] 历史落库 PASS")

# ---------- 2.5 新功能：语言选项 / 详细程度滑块 / 一键复制 / 重新生成 ----------
assert len(at.slider) == 2, "生成页应有 镜头数量 + 提示词详细程度 两个滑块"
assert at.slider[0].value == 4, f"镜头数量默认应为 4，实际 {at.slider[0].value}"
assert at.slider[1].value == 200, f"单角色字数默认应为 200，实际 {at.slider[1].value}"
assert at.multiselect, "生成页应有本片主角 multiselect（支持多角色同框）"
radio_vals = [str(r.value) for r in at.radio]
assert any(v in ("中文", "英文", "中英对照") for v in radio_vals), radio_vals
try:
    codes = at.code
except Exception:  # noqa: BLE001
    codes = at.get("code")
assert codes, "结果应以 st.code 代码块展示（自带一键复制图标）"
# 重新生成：沿用本地模板路径，用当前参数再生成一次
next(b for b in at.button if "重新生成" in b.label).click().run()
assert not at.exception, at.exception
assert at.session_state["last_result"], "重新生成后结果应仍在"
assert at.session_state["last_mode"] == "本地模板"
assert len(storage.list_history()) == 2, "重新生成应新增一条历史记录"
print("[2.5] 语言选项/详细程度滑块/一键复制/重新生成 PASS")

# ---------- 2.7 历史页：搜索筛选表单 / 创作者徽章 / 删除确认 / JSON 导出 ----------
nav = next(r for r in at.radio if r.label == "导航")
nav.set_value("🗂 生成历史").run()
assert not at.exception, at.exception
assert any("creator-pill" in str(m.value) for m in at.markdown), "历史记录应显示创作者徽章"
assert len(at.download_button) >= 2, "历史页应有 JSON 备份 + 每条 TXT 导出"
# 删除保护：存在"确认删除"勾选框，未勾选时不应有可点即删的路径
assert any("确认删除" in str(c.label) for c in at.checkbox), "删除前必须有确认删除勾选框"
# 侧边栏昵称输入框存在
assert any("设备昵称" in str(t.label) for t in at.text_input), "侧边栏应有设备昵称输入框"
print("[2.7] 历史页筛选/徽章/导出/删除确认 PASS")

# ---------- 2. 主角档案页：表单新建第二个主角（含 on_click 回调） ----------
# 生成页新增了"输出语言"radio，导航 radio 需按 label 查找
nav = next(r for r in at.radio if r.label == "导航")
nav.set_value("🐱 主角档案").run()
assert not at.exception, at.exception
# 字段顺序（AppTest 元素聚合：主内容区在前、侧边栏在后）：
# ai_char_desc(text_input[0]), name(text_input[1]), anchor(text_area[0]),
# style(text_input[2]), seed(text_input[3]), negative(text_input[4]),
# reference_note(text_input[5]), 设备昵称(text_input[6], 侧边栏最后)
at.text_input[1].input("小奶糖")
at.text_area[0].input("一只奶白色垂耳兔，粉色内耳，穿黄色小围裙")
at.text_input[2].input("治愈系水彩风")
next(b for b in at.button if "创建档案" in b.label).click().run()
assert not at.exception, at.exception
assert at.success, "未出现成功提示"
names = [c["name"] for c in storage.list_characters()]
assert "小奶糖" in names and len(names) == 2, names
assert storage.list_characters()[0].get("creator_name") == "我的设备"
print("[3] 新建主角 PASS ->", names)

# ---------- 3. 必填校验：空名字提交应报错而非崩溃 ----------
at.text_input[1].input("")
at.text_area[0].input("")
next(b for b in at.button if "创建档案" in b.label).click().run()
assert not at.exception, at.exception
assert at.error and "必填" in at.error[0].value
print("[4] 必填校验 PASS")

# ---------- 3.5 多角色同框：multiselect 选 2 角色 -> 动态滑块 -> 本地模板生成 ----------
nav = next(r for r in at.radio if r.label == "导航")
nav.set_value("🎬 生成提示词").run()
assert not at.exception, at.exception
ms = at.multiselect[0]
ms.set_value(["小橘", "小奶糖"]).run()
assert not at.exception, at.exception
# 动态字数分配：2 角色 -> 范围 200-500，默认 350
assert at.slider[1].value == 350, f"2 角色字数默认应为 350，实际 {at.slider[1].value}"
try:
    caps = [c.value for c in at.caption]
except Exception:  # noqa: BLE001
    caps = [c.value for c in at.get("caption")]
assert any("检测到 2 个角色" in str(c) for c in caps), caps
# 本地模板多角色生成：结果须包含两个角色 + 防融合负面词
at.text_area[0].input("小橘和小奶糖一起野餐")
next(b for b in at.button if "开始生成" in b.label).click().run()
assert not at.exception, at.exception
result2 = at.session_state["last_result"]
for token in ("小橘", "小奶糖", "character fusion", "color bleeding"):
    assert token in result2, f"多角色结果缺少 {token}"
print("[4.5] 多角色同框/动态滑块/防融合负面词 PASS")

# ---------- 4. 设置页可正常渲染 ----------
nav = next(r for r in at.radio if r.label == "导航")
nav.set_value("⚙️ 设置").run()
assert not at.exception, at.exception
assert at.text_input, "设置页应有输入框"
# AppTest 元素聚合主区在前、侧边栏在后：text_input[0] 仍是接口地址
v0 = at.text_input[0].value
assert v0 == "" or isinstance(v0, str), f"设置页接口地址异常: {v0}"
print("[5] 设置页 PASS")

# ---------- 6. prompts 纯函数单测：多角色校验 / 动态消息 / 正负面拆分 ----------
import prompts

_chars = [
    {"name": "小橘", "anchor": "橘白相间的短毛小猫，翠绿色大眼睛", "style": "3D风",
     "negative": "blurry", "seed": "1", "reference_note": ""},
    {"name": "小奶糖", "anchor": "奶白色垂耳兔，粉色内耳", "style": "",
     "negative": "", "seed": "", "reference_note": ""},
]
# 合格样本：两个角色都有 ≥80 词特征段
_long_a = "橘白相间的短毛小猫，翠绿色大眼睛，粉色鼻头，左耳尖有一撮白毛，" * 4
_long_b = "奶白色垂耳兔，粉色内耳，穿黄色小围裙，圆滚滚的身子，" * 4
_good = (
    "【镜头1】\n"
    f"【小橘详细特征】{_long_a}\n"
    f"【小奶糖详细特征】{_long_b}\n"
    "【位置与互动】一左一右\n"
    "【镜头参数】时间：0-4秒，Wide Shot\n"
    "【合并的负面提示词】\nNegative Prompt：character fusion, color bleeding\n"
    "\n-----\n\n"
    "📌 全片设定：保持一致"
)
assert prompts.detect_multi_char_issues(_good, _chars) == [], "合格样本不应报问题"
# 偷懒样本：镜头1 缺小奶糖特征段，镜头2 小橘描述过短
_bad = (
    "【镜头1】\n"
    f"【小橘详细特征】{_long_a}\n"
    "【镜头参数】时间：0-4秒\n"
    "\n-----\n\n"
    "【镜头2】\n"
    "【小橘详细特征】橘白小猫\n"
    "【小奶糖详细特征】小白兔\n"
    "【镜头参数】时间：4-8秒\n"
)
_bad_issues = prompts.detect_multi_char_issues(_bad, _chars)
assert any("镜头 1" in i and "小奶糖" in i for i in _bad_issues), _bad_issues
assert any("镜头 2" in i and "小橘" in i and "词" in i for i in _bad_issues), _bad_issues
# 单角色不触发多角色校验
assert prompts.detect_multi_char_issues(_bad, _chars[:1]) == []
# build_llm_messages 多角色注入防融合规则
_sys, _usr = prompts.build_llm_messages(_chars, "一起野餐", 4, "16:9")
assert "多角色同框特别规则" in _sys and "character fusion" in _sys
assert "【角色A】小橘" in _usr and "【角色B】小奶糖" in _usr
# 正负面拆分
_parts = prompts.parse_prompt_parts(_good)
assert "小橘" in _parts["zh_pos"] and "character fusion" in _parts["zh_neg"]
assert _parts["en_pos"] == ""
assert hasattr(prompts, "MULTI_CHAR_MIN_WORDS")
print("[6] prompts 纯函数单测 PASS（多角色校验/动态消息/正负面拆分）")

print("\n=== ALL APPTEST CHECKS PASSED ===")
