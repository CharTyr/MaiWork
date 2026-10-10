"""jev_presets.py：内置的「判断服务」预设（地址 / 模型名 / 文档链接）。

这些事实**全部抄自各家官方文档（2026-10）**，没有用真密钥实测过：
- TypeSafe 官方：https://docs.typesafe.ai/api
- OpenRouter：https://openrouter.ai/docs/guides/community/jev
- OpenCode Zen：https://opencode.ai/docs/zen
- Command Code：https://commandcode.ai/models/jev
- Vercel AI Gateway：https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe
- Upstage：https://console.upstage.ai/api/systemone
- Inception Labs：https://docs.inceptionlabs.ai/capabilities/decisions
- Liquid AI：https://docs.liquid.ai/lfm/models/d1
- Cloudflare Workers AI：https://developers.cloudflare.com/workers-ai/models/clef/
- OpenAI Decisions：https://developers.openai.com/api/docs/guides/decisions

协议只有两种（`PROTOCOLS`）：
- ``systemone``：TypeSafe 原生形状——请求 ``{model, state, questions}``；应答 ``{answers: {名字: …}}``。
  上面除了 OpenAI 之外的家都用这一种，Cloudflare Workers AI 的 REST 会再多包一层 ``result``。
- ``openai_decisions``：OpenAI 的 Decisions API（公测）形状——请求 ``{model, input, questions: […]}}``；
  应答 ``answers`` 是数组。

预设只是「照着文档填好的默认值」：地址/模型名各家可能会变，网页上都能改。
Cloudflare 的地址里有 ``{account_id}`` 占位，用之前必须换成自己的账号 ID。
"""

from __future__ import annotations

from dataclasses import dataclass

PROTOCOLS: tuple[str, ...] = ("systemone", "openai_decisions")


@dataclass(frozen=True)
class JevPreset:
    """一家判断服务的预设：填配置时的默认值 + 给用户看的说明。"""

    id: str
    name: str          # 显示名
    protocol: str      # PROTOCOLS 里的一个
    url: str           # 请求地址（Cloudflare 带 {account_id} 占位）
    model: str         # 默认模型名
    models: tuple[str, ...]   # 这家还认的别的模型名（含默认那个）
    docs_url: str      # 官方文档
    key_url: str       # 去哪拿密钥（没有官方页就留空）
    note: str          # 一句中文提醒


PRESETS: dict[str, JevPreset] = {
    "typesafe": JevPreset(
        id="typesafe",
        name="TypeSafe 官方",
        protocol="systemone",
        url="https://api.typesafe.ai/v1/systemone",
        model="jev-1.13.0",
        models=("jev-1.13.0", "jev-latest"),
        docs_url="https://docs.typesafe.ai/api",
        key_url="",
        note="Jev 的原厂；内置那个就是它（也能用密钥文件）",
    ),
    "openrouter": JevPreset(
        id="openrouter",
        name="OpenRouter",
        protocol="systemone",
        url="https://openrouter.ai/api/v1/systemone",
        model="typesafe/jev-1.13",
        models=("typesafe/jev-1.13", "~typesafe/jev-latest"),
        docs_url="https://openrouter.ai/docs/guides/community/jev",
        key_url="https://openrouter.ai/keys",
        note="一把密钥能用很多家模型",
    ),
    "opencode": JevPreset(
        id="opencode",
        name="OpenCode Zen",
        protocol="systemone",
        url="https://opencode.ai/zen/v1/systemone",
        model="jev-1.13",
        models=("jev-1.13", "jev-1.13-free"),
        docs_url="https://opencode.ai/docs/zen",
        key_url="",
        note="jev-1.13-free 限时免费",
    ),
    "commandcode": JevPreset(
        id="commandcode",
        name="Command Code",
        protocol="systemone",
        url="https://api.commandcode.ai/provider/v1/systemone",
        model="typesafe/jev",
        models=("typesafe/jev",),
        docs_url="https://commandcode.ai/models/jev",
        key_url="",
        note="要有 Provider API 的套餐",
    ),
    "vercel": JevPreset(
        id="vercel",
        name="Vercel AI Gateway",
        protocol="systemone",
        url="https://ai-gateway.vercel.sh/typesafe/v1/systemone",
        model="typesafe-ai/jev",
        models=("typesafe-ai/jev",),
        docs_url="https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe",
        key_url="",
        note="走 Vercel 的网关，计费算在 Vercel 账上",
    ),
    "upstage": JevPreset(
        id="upstage",
        name="Upstage Solar Decide",
        protocol="systemone",
        url="https://api.upstage.ai/v1/systemone",
        model="solar-decide",
        models=("solar-decide",),
        docs_url="https://console.upstage.ai/api/systemone",
        key_url="",
        note="公测；choice 最多 26 个选项",
    ),
    "inception": JevPreset(
        id="inception",
        name="Inception Mercury Decide",
        protocol="systemone",
        url="https://api.inceptionlabs.ai/v1/decisions",
        model="mercury-decide",
        models=("mercury-decide",),
        docs_url="https://docs.inceptionlabs.ai/capabilities/decisions",
        key_url="",
        note="",
    ),
    "liquid": JevPreset(
        id="liquid",
        name="Liquid AI d1",
        protocol="systemone",
        url="https://api.liquid.ai/decisions/v1/systemone",
        model="d1",
        models=("d1", "d1:free"),
        docs_url="https://docs.liquid.ai/lfm/models/d1",
        key_url="",
        note="d1:free 免费档",
    ),
    "cloudflare": JevPreset(
        id="cloudflare",
        name="Cloudflare Clef",
        protocol="systemone",
        url=(
            "https://api.cloudflare.com/client/v4/accounts/{account_id}"
            "/ai/run/@cf/cloudflare/clef-flash"
        ),
        model="clef-flash",
        models=("clef-flash", "clef"),
        docs_url="https://developers.cloudflare.com/workers-ai/models/clef/",
        key_url="",
        note="地址里的 {account_id} 换成你的 Cloudflare 账号 ID；换成 clef 时地址末尾也改成 clef",
    ),
    "openai": JevPreset(
        id="openai",
        name="OpenAI Decisions",
        protocol="openai_decisions",
        url="https://api.openai.com/v1/decisions",
        model="gpt-6-luna",
        models=("gpt-6-luna",),
        docs_url="https://developers.openai.com/api/docs/guides/decisions",
        key_url="",
        note="OpenAI 自家的判断模型（公测），不是 Jev",
    ),
}
