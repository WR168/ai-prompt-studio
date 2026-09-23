"""提示词引擎。

两种生成模式：
1. build_prompt_local：纯本地模板引擎，免费、离线可用，保证每条提示词都
   原样注入主角锚定描述（anchor），实现跨视频主角一致。
2. build_llm_messages：构造发给 LLM 的 system/user 消息，由大模型智能扩写。

输出格式面向可灵 Kling / 即梦 Dreamina / Runway / Sora 等文生视频工具。
"""
from __future__ import annotations

import re
from typing import Any

DEFAULT_NEGATIVE = (
    "角色外观变化, 换服装, 毛色/颜色改变, 多余的角色, 多主角, 多余的肢体, "
    "畸形, 五官错位, 面部崩坏, 肢体粘连, 比例失调, 模糊, 闪烁, 抖动, "
    "穿模, 水印, 文字, logo, 低质量, 过曝, 噪点"
)

# 多角色同框：防融合负面词（必须出现在合并负面提示词里）
ANTI_FUSION_NEGATIVE = (
    "character fusion, mixed features, merged bodies, color bleeding"
)

# 多角色单镜最低词数（后台校验与重试共用此阈值）
MULTI_CHAR_MIN_WORDS = 80

# 分镜模板池：用户要几镜就按顺序取前 N 个
_SHOT_TEMPLATES: list[dict[str, str]] = [
    {
        "title": "远景 · 建立镜头",
        "duration": "3-4秒",
        "visual": (
            "开阔的环境全景，交代故事发生的时间、地点与天气；"
            "主角从画面一侧自然出现，环境细节清晰"
        ),
        "camera": "固定机位开场，随后缓慢向前推进（slow push-in）",
        "lighting": "柔和环境光，奠定全片色调",
    },
    {
        "title": "中景 · 核心动作",
        "duration": "4-5秒",
        "visual": "围绕“{action}”展开，动作完整、自然、有明确的起止姿态",
        "camera": "平稳跟拍（tracking shot），镜头与主角保持同速侧向移动",
        "lighting": "轮廓光勾勒主角身形，背景适度虚化突出主体",
    },
    {
        "title": "近景 · 表情特写",
        "duration": "2-3秒",
        "visual": "主角面部/神态特写，情绪生动，眼神有光，呈现动作中的心理反应",
        "camera": "浅景深特写，带极轻微的手持呼吸感",
        "lighting": "柔和顺光，眼球保留明显高光点（catchlight）",
    },
    {
        "title": "中景 · 互动推进",
        "duration": "3-4秒",
        "visual": "主角继续“{action}”，与环境中的关键道具/景物发生互动，推动情节",
        "camera": "侧后方跟随或平稳横移（pan），保持与上一镜相同的运动方向",
        "lighting": "光源方向、色温与前后镜头严格一致",
    },
    {
        "title": "远景 · 收尾镜头",
        "duration": "3-4秒",
        "visual": "主角完成动作、抵达目的地或向画面深处自然远去，画面留出呼吸空间",
        "camera": "缓慢拉升（crane up）或向后拉远（pull back）收尾",
        "lighting": "温暖收束的光线，整体氛围与开场呼应",
    },
]

_CONSISTENCY_RULES = (
    "1. 每个镜头中的主角必须与【主角锚定】逐字一致：物种/品种、毛色花纹、"
    "眼睛颜色、服装配饰、体型比例、标志性物品一律不得改变\n"
    "2. 全片同一光源逻辑、同一色温色调；相邻镜头动作连贯、运动方向连续"
    "（遵守 180 度轴线规则）\n"
    "3. 画面中始终只有这一个主角，不新增拟人化角色或抢戏的动物\n"
    "4. 首镜生成满意后锁定同一 Seed，后续镜头与后续视频均复用该 Seed"
)


def build_prompt_local(characters: list[dict[str, Any]], user_input: str,
                       shot_count: int = 3, ratio: str = "16:9") -> str:
    """本地模板生成：把一句话创意扩写为结构化分镜提示词。

    characters 支持传入多个角色（多角色同框时会合并锚定与负面词，
    并追加防融合负面项）。
    """
    names = [c["name"] for c in characters]
    anchors = [c["anchor"].strip() for c in characters]
    name = " + ".join(names)
    anchor = "；".join(anchors)

    # 多个角色都设了画风/负面词时合并去重；否则用第一个/默认值
    styles = list(dict.fromkeys(
        s for c in characters if (s := c.get("style", "").strip())
    ))
    style = "；".join(styles) or "3D 卡通动画风格，质感细腻"
    negatives = list(dict.fromkeys(
        n for c in characters if (n := c.get("negative", "").strip())
    ))
    negative = "，".join(negatives) or DEFAULT_NEGATIVE
    if len(characters) >= 2:
        negative += "，" + ANTI_FUSION_NEGATIVE
    seeds = [s for c in characters if (s := c.get("seed", "").strip())]
    seed = seeds[0] if seeds else ""

    action = user_input.strip()
    shots_text: list[str] = []

    # 多角色时每个分镜前先逐角色重申外观锚定（与 AI 模式的结构对齐）
    char_block = ""
    if len(characters) >= 2:
        char_block = "".join(
            f"【{n}详细特征】{a}\n" for n, a in zip(names, anchors)
        ) + "【位置与互动】各角色保持独立外观，互不融合\n"

    for i, tpl in enumerate(_SHOT_TEMPLATES[:shot_count], start=1):
        visual = tpl["visual"].format(action=action)
        shots_text.append(
            f"■ 镜头 {i}｜{tpl['title']}｜{i * 4 - 4}-{i * 4}秒\n"
            f"{char_block}"
            f"画面：{anchor}。{visual}。\n"
            f"　　　核心情节：{name}，{action}\n"
            f"运镜：{tpl['camera']}\n"
            f"光线：{tpl['lighting']}"
        )

    seed_line = (
        f"固定 Seed：{seed}（所有镜头与后续视频复用，确保是同一只主角）"
        if seed
        else "固定 Seed：首镜试出满意画面后，记录其 Seed 并长期复用"
    )

    return (
        f"# AI 视频分镜提示词\n"
        f"主角：{name}　|　创意：{action}　|　共 {shot_count} 个镜头　|　画幅 {ratio}\n"
        f"\n"
        f"【主角锚定 · 每个镜头必须原样包含，禁止改动】\n"
        f"{anchor}\n"
        f"\n"
        f"【统一画风】\n{style}\n"
        f"\n"
        f"【分镜脚本】\n" + "\n\n".join(shots_text) + "\n"
        f"\n"
        f"【跨镜头 / 跨视频一致性约束】\n{_CONSISTENCY_RULES}\n"
        f"\n"
        f"【建议生成参数】\n"
        f"画幅 {ratio}；逐镜生成后按顺序剪辑；{seed_line}\n"
        f"\n"
        f"【负面提示词 Negative Prompt】\n{negative}\n"
    )


SYSTEM_PROMPT = (
    "你是一位好莱坞顶级摄影指导（Director of Photography），同时是 Midjourney、"
    "ComfyUI、可灵 Kling、即梦 Dreamina、Runway、Sora 等 AI 绘图/视频工具的资深"
    "提示词专家。\n"
    "任务：把用户的一句话创意，扩写成可直接投喂给视频 AI 的电影级分镜提示词。"
    "用户会把每个分镜【单独复制】到 AI 绘图软件生成图片，因此每个分镜都必须"
    "自包含、不依赖上下文。\n\n"
    "铁律——每个分镜必须严格按以下四段结构组织，缺一不可：\n"
    "1.【角色详细特征】对于生成的每一个分镜（镜头1、镜头2、镜头3……），"
    "都必须原封不动地把角色的详细特征（如：橘白相间的短毛猫、琥珀色大眼睛、"
    "粉鼻子、背着白色小背包）完整地重新写一遍！即使显得很啰嗦也必须写！"
    "必须写全物种/品种、毛色与花纹分布、眼睛颜色与形状、"
    "鼻子颜色、耳型、体型比例、服装/配饰材质与颜色、随身标志性物品。\n"
    "绝对不允许使用「它」「小猫」「这只猫」等代词来偷懒；"
    "严禁只在第一个镜头描述角色、后续镜头省略——任何省略都会导致 AI 画错、"
    "角色形象在后续镜头中崩坏。\n"
    "★头号禁忌★：绝对禁止使用「如上」「同上」「同镜头1」「同前」「详见上文」"
    "「参见上文」「参考前文」「前述」「前文」「上述」"
    "「Subject as above」「As in Shot 1」「Same as previous」「See above」"
    "「aforementioned」「previously described」「as described」「as stated」"
    "等任何代词或引用写法来代替角色特征。每个分镜的【角色详细特征】段必须"
    "从头到尾完整写出全部特征（物种、毛色花纹、眼色、鼻色、耳型、服装配饰等），"
    "必须确保每一个分镜单独复制出去都是一段完整、可直接用于绘图的提示词！"
    "这是本系统的核心要求，违反即为失败。\n"
    "2.【动作与环境】场景地点与天气氛围、关键道具、动作三段式分解"
    "（起势 → 过程 → 收势）。\n"
    "3.【镜头参数】景别（Extreme Wide Shot / Wide Shot / Medium Shot / Close-up / "
    "Extreme Close-up）、运镜（Pan / Tilt / Dolly in / Follow / Tracking shot / "
    "Crane up 等）、光线与画质（Cinematic lighting / Golden hour / Soft rim light / "
    "DSLR, 85mm, shallow depth of field / 8k, highly detailed 等）、建议时长。\n"
    "4.【负面提示词】每个分镜末尾单独给一行 Negative Prompt：基础项 human face, "
    "extra animals, multiple characters, 3D render, deformed limbs, blurry, "
    "low quality, watermark, text，并按本镜头画面追加针对性风险项。\n\n"
    "格式要求：\n"
    "5. 每个分镜标题必须用实际序号：【镜头1】【镜头2】【镜头3】……"
    "（把 N 替换为实际数字）；每输出完一个分镜，必须紧跟单独一行 ----- "
    "（五个连字符）再输出下一个分镜；分镜数量必须与用户要求的完全一致，"
    "严禁输出空模板镜头、【镜头N】这样的占位符或任何多余分镜；\n"
    "6. 相邻镜头动作连贯、运动方向连续，全片光源逻辑与色调统一；\n"
    "7. 全部分镜之后，另起一行 -----，附上全片总述：跨镜头一致性约束"
    "（含 Seed 复用建议）与统一画风说明；\n"
    "8. 只输出提示词正文，不要寒暄、不要解释思路。\n"
    "9. 高密度原则（极其重要）：严禁生成任何无意义的修饰词、形容词堆砌和铺垫"
    '（如"在一个美丽的日子里，小橘开心地……"这类记叙文写法）。必须输出高密度的'
    "专业提示词——用逗号分隔的关键词短语组织内容，只保留画面信息：角色特征、"
    "动作、镜头语言、光影画质。每个词都要能影响出图，可写可不写的一律不写。\n"
    "10. ★强制动作递进★：分镜必须构成一个完整故事，严格按"
    "'起势（建立场景、主角出场）→ 发展（动作展开、推进情节）→ "
    "高潮（表情特写、关键细节、情绪顶点）→ 收尾（结果呈现、主角远去）'"
    "推进，每个镜头在故事线上必须有明确的推进作用，严禁两个镜头画面雷同。\n"
    "11. ★强制景别切换★：相邻镜头的景别必须不同！严格遵循"
    "'全景（交代环境）→ 中景（动作过程）→ 特写（表情/细节）→ 全景（结果/背影）'"
    "的递进节奏，禁止连续两个镜头使用同一景别。\n"
    "12. ★强制时间轴★：每个镜头必须在【镜头参数】段开头标注时间戳，"
    "格式为'时间：0-4秒'或'Time: 0-4s'，各镜头时间连续不重叠，"
    "总时长构成完整的 10-20 秒短视频。\n\n"
    "输出前先在内心自检，并特别执行「逐镜角色补全检查」：逐个镜头核对——"
    "该镜头是否完整重述了角色全部特征？是否残留「它/小猫」等代词？"
    "是否出现「如上」「As in Shot 1」等引用偷懒写法（一旦出现即为失败）？"
    "四段结构（角色特征/动作环境/镜头参数/负面提示词）是否齐全？"
    "是否存在不影响画面的废话修饰（有则删掉，用省下的篇幅补足核心要素）？"
    "发现任何镜头遗漏或偷懒，必须自动补全为完整版本后再输出，"
    "绝不允许抱侥幸心理直接交付。"
)

# 多角色同框特别规则：拼接在 SYSTEM_PROMPT 之后（{角色X名} 由实际名字替换）
MULTI_CHAR_RULES = (
    "\n\n===== 多角色同框特别规则（最高优先级，违反即为失败）=====\n"
    "本片有多名角色同框。角色融合是头号灾难：绝对禁止把两个角色的特征混在一起"
    "（如毛色混合、五官拼凑、体型合并、颜色互染），每个角色必须保持自己档案里的"
    "独立特征。\n"
    "★角色字数死命令★：无论总字数多少，每个分镜中每个角色都必须强制分配 "
    "80-100 词的极度详细外观特征描述（颜色、毛发、五官、穿戴），严禁压缩、"
    "严禁省略任何一个角色。剩下的字数用来描写环境、互动和镜头。\n"
    "★多角色结构化输出★：每个分镜必须严格按以下顺序组织段落，缺一不可：\n"
    "【{角色A名}详细特征】→【{角色B名}详细特征】→（如有更多角色以此类推）"
    "→【位置与互动】→【镜头参数】→【合并的正面提示词】→【合并的负面提示词】\n"
    "  · 【{角色X名}详细特征】：该角色的完整外观锚定，必须与档案逐字一致、"
    "80-100 词；段落标题必须使用角色的原始中文名字，任何输出语言下都不得翻译"
    "或改名；\n"
    "  · 【位置与互动】：明确每个角色在画面中的空间位置（左/右/前景/背景）、"
    "相对距离、朝向和互动方式，确保角色之间不重叠、不融合；\n"
    "  · 【镜头参数】：景别、运镜、光线，并在开头标注时间戳（时间：X-Y秒）；\n"
    "  · 【合并的正面提示词】：把角色特征、位置互动、镜头参数、风格提示词合并成"
    "一段可直接投喂 AI 绘图/视频工具的完整提示词；\n"
    "  · 【合并的负面提示词】：单独一行 Negative Prompt：，必须包含全部角色档案"
    "的负面条目，并且必须包含：" + ANTI_FUSION_NEGATIVE + "，再按本镜画面追加"
    "针对性风险项。\n"
    "禁止让任何一个角色在任何一个分镜中消失；禁止用「两个角色」「它们」等模糊"
    "表述代替逐一描述。输出前逐镜自检：每个角色是否都有独立、足量（≥80词）的"
    "特征段？发现遗漏或过短，必须补全后再输出。"
)


def _render_multi_char_rules(characters: list[dict[str, Any]]) -> str:
    """把 MULTI_CHAR_RULES 里的 {角色X名} 占位符替换为实际角色名。"""
    rules = MULTI_CHAR_RULES
    for i, c in enumerate(characters):
        rules = rules.replace("{角色" + chr(ord("A") + i) + "名}", c["name"])
    rules = rules.replace("{角色X名}", "{角色名}")
    return rules


# 输出语言指令（按用户选择注入 system prompt）
_LANGUAGE_RULES = {
    "中文": (
        "全程使用中文输出；专业摄影术语可在中文后用括号附英文原文，"
        "如：缓慢推进（Dolly in）、电影级布光（Cinematic lighting）。"
    ),
    "英文": (
        "全程以一位英语母语资深摄影师的手笔，输出纯正、专业的英文视频提示词："
        "必须使用地道英文影视术语（Wide shot、Close-up、Pan、Follow、Tracking shot、"
        "Dolly in、Cinematic lighting、DSLR、8k、highly detailed 等），句式紧凑专业，"
        "严禁中式直译和翻译腔；主角外观锚定同样写成英文，但毛色花纹、眼睛颜色、"
        "鼻色、耳型、服装配饰等每个要素都必须保留，且每个分镜（Shot 1、Shot 2…）"
        "都要完整重述，不得遗漏或改动。"
    ),
    "中英对照": (
        "先用中文输出全部分镜（每个分镜以【镜头N】开头、用单独一行 ----- 分隔）；"
        "然后另起一行，原样输出分隔标记 =====ENGLISH=====；再输出英文版——每个分镜"
        "以 Shot N: 开头、用 ----- 分隔，与中文版逐镜一一对应，每个英文分镜同样包含"
        " Subject（角色详细特征）/ Action & Environment（动作与环境）/ Camera（镜头"
        "参数）/ Negative Prompt 四段。英文必须是英语母语摄影师的手笔，使用 Wide "
        "shot、Close-up、Pan、Follow、Cinematic lighting、8k、highly detailed 等"
        "地道术语，严禁翻译腔。中文版与英文版的长度必须基本对等、内容精准对应，"
        "英文版不得只写缩写版或漏译镜头。"
    ),
}


def build_llm_messages(characters: list[dict[str, Any]], user_input: str,
                       shot_count: int = 3, ratio: str = "16:9",
                       recent_history: list[dict[str, Any]] | None = None,
                       word_count: int = 200, language: str = "中文"
                       ) -> tuple[str, str]:
    """返回 (system, user) 消息文本。

    characters：角色列表（支持 1-3 个，多角色同框时自动注入防融合规则）；
    word_count：提示词目标字数，强制注入 system 指令；
    language：输出语言（中文 / 英文 / 中英对照），决定输出指令。
    如果传入 recent_history（最近 N 条 AI 扩写记录），会作为上下文追加到
    user 消息末尾，让模型参考之前的风格和创意保持连贯。
    """
    lang_rule = _LANGUAGE_RULES.get(language, _LANGUAGE_RULES["中文"])
    # 滑块字数 → 高密度模式：宁短勿水，要素必须齐
    if word_count >= 250:
        depth_rule = (
            f"详写模式：请在约 {word_count} 词的范围内完成，每个分镜用逗号分隔的"
            "关键词短语，确保包含核心角色特征、动作分解、镜头语言（如 Medium Shot, "
            "Dolly in）与光影描写；可写可不写的词一律不写，不要记叙文、不要凑字数。"
        )
    elif word_count >= 150:
        depth_rule = (
            f"标准模式：请在约 {word_count} 词的范围内完成，每个分镜都必须包含核心的"
            "角色特征（如：橘白相间的短毛猫，琥珀色眼睛）、动作、镜头语言"
            "（如 Medium Shot, Dolly in）和光影描写；用逗号分隔关键词，不要写成记叙文。"
        )
    else:
        depth_rule = (
            f"精炼模式：请在约 {word_count} 词的范围内完成，每个分镜只保留"
            "最核心的角色特征、动作、镜头语言（Close-up / Pan 等）与光线关键词，"
            "逗号分隔、一词一画面；四段结构和逐镜角色重述一个都不能省。"
        )
    system = (
        SYSTEM_PROMPT
        + "\n10. 输出语言：" + lang_rule + "\n"
        + "11. 篇幅深度：" + depth_rule + "\n"
        + "12. 请确保输出格式完整，不要出现无意义的重复字词和胡言乱语；"
        "结尾必须完整收束（包含负面提示词与参数建议），不得中途截断。"
    )
    multi = len(characters) >= 2
    if multi:
        # 多角色同框：注入防融合 + 逐角色 80-100 词死命令 + 结构化输出规则
        system += _render_multi_char_rules(characters)

    if multi:
        char_lines: list[str] = []
        for i, c in enumerate(characters):
            letter = chr(ord("A") + i)
            char_lines.append(
                f"【角色{letter}】{c['name']}\n"
                f"  · 外观锚定（必须逐字一致）：{c['anchor'].strip()}\n"
                f"  · 风格提示词：{c.get('style', '').strip() or '由你根据情节设定，必须输出高密度视觉关键词'}\n"
                f"  · 参考图备注：{c.get('reference_note', '').strip() or '无'}\n"
                f"  · 负面提示词：{c.get('negative', '').strip() or '请你补充针对性负面提示词'}\n"
                f"  · 固定 Seed：{c.get('seed', '').strip() or '暂无，请提示用户锁定'}"
            )
        user_msg = (
            "\n".join(char_lines)
            + f"\n\n【一句话创意】{user_input.strip()}\n"
            f"【要求】生成 {shot_count} 个连贯镜头，画幅 {ratio}，"
            f"共 {len(characters)} 个角色同框互动。\n"
            f"★强制要求★：\n"
            f"  · 每个分镜必须按【多角色结构化输出】组织，每个角色都有独立的 "
            f"80-100 词详细特征段（标题用角色原始名字）；\n"
            f"  · 合并的正面提示词必须原样注入各角色的风格提示词，不得遗漏；\n"
            f"  · 合并的负面提示词必须包含全部角色档案负面条目，"
            f"且必须包含：{ANTI_FUSION_NEGATIVE}。"
        )
    else:
        character = characters[0]
        style_val = character.get('style', '').strip()
        negative_val = character.get('negative', '').strip()
        user_msg = (
            f"【主角名字】{character['name']}\n"
            f"【主角锚定】{character['anchor'].strip()}\n"
            f"【风格提示词】{style_val or '由你根据情节设定，必须输出高密度视觉关键词'}\n"
            f"【参考图备注】{character.get('reference_note', '').strip() or '无'}\n"
            f"【负面提示词】{negative_val or '请你补充针对性负面提示词'}\n"
            f"【固定 Seed】{character.get('seed', '').strip() or '暂无，请提示用户锁定'}\n"
            f"\n"
            f"【一句话创意】{user_input.strip()}\n"
            f"【要求】生成 {shot_count} 个连贯镜头，画幅 {ratio}。\n"
            f"★强制要求★：每个分镜都必须同时输出以下两部分，缺一不可：\n"
            f"  · 正面提示词：包含【角色详细特征】+【动作与环境】+【镜头参数】"
            f"+【风格提示词】（必须原样注入上面的风格关键词，不得遗漏）；\n"
            f"  · 负面提示词：在每镜末尾单独一行 Negative Prompt：，"
            f"必须包含上面的【负面提示词】全部条目，并按本镜画面追加针对性风险项。"
        )
    if recent_history:
        context_lines = ["\n【历史上下文（保持风格与主角一致，勿重复相同创意）】"]
        for i, h in enumerate(reversed(recent_history), 1):
            context_lines.append(
                f"--- 第{i}次创作 ---\n"
                f"创意：{h['user_input']}\n"
                f"结果摘要：{h['result'][:300]}…\n"
            )
        user_msg += "\n".join(context_lines)
    return system, user_msg


# ---------------- 结果解析：分镜切分 / 正负面拆分 / 多角色强校验 ----------------
# （纯函数，不依赖 streamlit，便于独立单元测试）

# 兼容【镜头N】/【Shot N】两种标题（此前【Shot N】带括号会漏切）
_SHOT_HEAD_SPLIT_RE = re.compile(r"\n(?=【?镜头\s*\d|【?Shot\s*\d)", re.IGNORECASE)
# 空模板镜头：结构标签齐全但内容为空（AI 纪律失误的产物），直接丢弃
_EMPTY_TEMPLATE_RE = re.compile(
    r"【动作与环境】\s*\n\s*【镜头参数】\s*\n\s*【负面提示词】"
)


def _split_shots_block(block: str) -> list[str]:
    """把一个语言块拆成多个分镜：按 ----- 分隔，块内若含多个镜头标题再拍扁，
    最后过滤掉空模板镜头。"""
    parts: list[str] = []
    for chunk in (c.strip() for c in block.split("\n-----\n")):
        if not chunk:
            continue
        subs = [s.strip() for s in _SHOT_HEAD_SPLIT_RE.split(chunk) if s.strip()]
        parts.extend(subs if len(subs) > 1 else [chunk])
    return [p for p in parts if not _EMPTY_TEMPLATE_RE.search(p)]


def shot_label(part: str) -> str:
    """分镜标签名：【镜头N】/ Shot N → 镜头 N；其余视为全片总述。"""
    m = re.match(r"【?镜头\s*(\d+)", part)
    if m:
        return f"镜头 {m.group(1)}"
    m = re.match(r"Shot\s*(\d+)", part, re.IGNORECASE)
    if m:
        return f"镜头 {m.group(1)}"
    return "📌 全片设定"


def parse_result(result: str) -> tuple[list[str], list[str] | None]:
    """解析 AI 结果为 (中文分镜列表, 英文分镜列表或 None)。"""
    if "=====ENGLISH=====" in result:
        zh_raw, en_raw = result.split("=====ENGLISH=====", 1)
        return _split_shots_block(zh_raw), _split_shots_block(en_raw)
    return _split_shots_block(result), None


# 用于切出每个分镜里的 Negative Prompt 行
_NEG_LINE_RE = re.compile(
    r"^\s*(?:Negative Prompt|负面提示词)\s*[:：]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def _split_positive_negative(shot: str) -> tuple[str, str]:
    """把单个分镜文本切为 (正面提示词, 负面提示词)。
    没有负面提示词时返回 (shot, '')。"""
    m = _NEG_LINE_RE.search(shot)
    if not m:
        return shot.strip(), ""
    neg = m.group(1).strip()
    positive = _NEG_LINE_RE.sub("", shot).strip().rstrip("-").strip()
    return positive, neg


def parse_prompt_parts(result: str) -> dict[str, str]:
    """把整段结果拆成 4 个区：zh_pos / en_pos / zh_neg / en_neg。
    每个区为字符串（多镜合并成一段，用 ----- 分隔），便于 ComfyUI 直接整段复制。
    没有英文版时 en_pos/en_neg 为空字符串。"""
    zh_shots, en_shots = parse_result(result)
    if not zh_shots:
        zh_shots = [result]
    zh_pos_parts, zh_neg_parts = [], []
    for s in zh_shots:
        p, n = _split_positive_negative(s)
        zh_pos_parts.append(p)
        if n:
            zh_neg_parts.append(n)
    en_pos_parts, en_neg_parts = [], []
    if en_shots:
        for s in en_shots:
            p, n = _split_positive_negative(s)
            en_pos_parts.append(p)
            if n:
                en_neg_parts.append(n)
    return {
        "zh_pos": "\n\n-----\n\n".join(zh_pos_parts).strip(),
        "zh_neg": "\n".join(zh_neg_parts).strip(),
        "en_pos": "\n\n-----\n\n".join(en_pos_parts).strip(),
        "en_neg": "\n".join(en_neg_parts).strip(),
    }


def count_words(text: str) -> int:
    """词数统计：中文字符逐字计 1，英文单词计 1。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[A-Za-z]+", text))
    return cjk + en


def extract_char_section(shot: str, name: str) -> str | None:
    """提取【{name}...】段落到下一个【标记之间的文本；无该角色标记返回 None。"""
    m = re.search(rf"【[^】]*{re.escape(name)}[^】]*】", shot)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"【", shot[start:])
    end = start + nxt.start() if nxt else len(shot)
    return shot[start:end]


def detect_multi_char_issues(result: str, characters: list[dict[str, Any]],
                             min_words: int = MULTI_CHAR_MIN_WORDS) -> list[str]:
    """多角色强校验：每个分镜中每个角色都必须有 ≥min_words 词的独立特征段。

    只校验真正的分镜块（跳过全片总述），返回问题描述列表；
    单角色（characters < 2）时直接返回空。
    """
    if len(characters) < 2:
        return []
    zh_shots, _ = parse_result(result)
    issues: list[str] = []
    for shot in zh_shots:
        label = shot_label(shot)
        if not label.startswith("镜头"):
            continue  # 跳过"全片设定"总述块
        for c in characters:
            name = c["name"]
            section = extract_char_section(shot, name)
            if section is None:
                if name in shot:
                    issues.append(
                        f"{label}缺少「{name}」的独立特征段"
                        f"（必须有【{name}详细特征】结构）"
                    )
                else:
                    issues.append(f"{label}完全遗漏角色「{name}」")
            else:
                wc = count_words(section)
                if wc < min_words:
                    issues.append(
                        f"{label}中「{name}」的描述只有{wc}词（要求≥{min_words}词）"
                    )
    return issues


# ---------------- AI 智能生成角色设定 ----------------

AI_CHARACTER_SYSTEM = (
    "你是一位资深角色设计师，同时精通 Midjourney、ComfyUI 等 AI 绘图工具的"
    "提示词编写。\n"
    "任务：根据用户的一句话描述，生成一份极度详细的角色外观锚定描述。\n"
    "要求：\n"
    "1. 必须包含以下所有要素：物种/品种、毛色/肤色与花纹分布、"
    "眼睛颜色与形状、鼻子/嘴巴颜色、耳型、体型比例、"
    "服装/配饰材质与颜色、随身标志性物品。\n"
    "2. 每个细节都要具体到颜色和材质（如'橘白相间的短毛'而非'橘色毛'）。\n"
    "3. 输出一段连贯的中文描述（150-250字），可直接作为角色锚定使用。\n"
    "4. 不要输出任何解释、标题、分点列表或多余格式，直接输出描述文本本身。\n"
    "5. 描述要适合 AI 绘图软件理解，用具体的视觉关键词而非抽象形容词。"
)


def build_ai_character_messages(desc: str) -> tuple[str, str]:
    """构造 AI 生成角色设定的 (system, user) 消息。

    用户输入一句话描述（如"一只穿和服的柴犬"），AI 返回一段极度详细的
    角色外观锚定描述，可直接填入角色档案的 anchor 字段。
    """
    user_msg = f"角色一句话描述：{desc.strip()}\n\n请生成详细的角色外观锚定描述。"
    return AI_CHARACTER_SYSTEM, user_msg
