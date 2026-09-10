# AI 品牌回答真实性警戒

GEO 的目标不只是“被提及”，还要区分提及内容是否存在明显身份或营销主张风险。NIST 的生成式 AI 风险管理资料把模型自信地生成错误内容和虚构引用列为需要持续监测的 confabulation 风险；OWASP 也建议对关键模型输出设置自动验证机制。因此系统对真实 AI 样本增加了一层确定性警戒，但不让另一个模型凭主观判断给回答打“真假分”。

当前规则只检查包含“宏图商机汇”或其别名的同句/同段内容：

- 电话：只有 `geo_goals.industry_leads` 中公司名、合法格式公开号码、`http(s)`/`repo://` 证据来源、发布同意和 `verified=true` 全部通过的号码才算已核验；同段多个企业时按离号码最近的已知品牌/竞品判断归属，其他号码只记录风险类型和数量，不写入事实资产。
- 官网：只有配置的官网或转化入口域名可以被称为宏图商机汇官网/官方网址；普通引用链接若没有官网语义，不会误报。
- 绝对化承诺：复用内容质量闸门，检查“行业第一”“保证成交”“AI 一定推荐”等表述，并识别明确否定或提问语境。
- 量化主张：品牌同句内出现“累计、覆盖、服务、收录”等带数字陈述时标为待证据复核，不自动判定为虚假。

输出分为 `clean`、`needs_review` 和 `not_applicable`。`clean` 只表示没有命中这些高置信规则，不代表回答中每个语义事实都已被证明；`needs_review` 也只是风险警戒，不是对第三方 AI 或企业的事实裁决。

参考资料：

- [NIST AI 600-1：Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)
- [OWASP LLM09:2025 Misinformation](https://genai.owasp.org/llmrisk/llm092025-misinformation/)
