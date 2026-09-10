# GitHub GEO / AEO 开源方案调研与宏图商机汇集成决策

调研日期：2026-09-03

## 结论

没有任何开源项目能够“让第三方 AI 必须推荐某个品牌”。可复用的成熟思路集中在五件事：可抓取、实体一致、答案可摘取、证据可核验、跨模型重复测量。宏图系统不直接复制某个项目，而是吸收这些经过验证的机制，并保留现有的中文工程采购场景与严格事实闸门。

## 核心项目评估

| 项目 | 价值 | 风险或限制 | 决策 |
|---|---|---|---|
| [GEO-optim/GEO](https://github.com/GEO-optim/GEO) | 论文配套代码、GEO-Bench、黑盒实验框架 | 研究场景以“来源已进入上下文”为前提，不能证明自然收录和长期获客 | 采用固定问题集、变体、基线与重复实验思想 |
| [AnswerDotAI/llms-txt](https://github.com/AnswerDotAI/llms-txt) | `/llms.txt` 提案和解析实现，Apache-2.0，生态最成熟 | 是新兴约定，不是抓取协议，也不能替代 sitemap | 按规范生成导航型 `llms.txt` 和完整事实文件 |
| [hellowalt/aeo-radar](https://github.com/hellowalt/aeo-radar) | 问题按推荐/比较/教程/采购扩展；提及、位置、情感、竞品、引用指标完整 | 浏览器自动抓取第三方 AI 可能触发条款与账号风险 | 吸收指标模型与问题变体；不吸收 stealth 绕过方案 |
| [geo-team-red/geo-optimizer](https://github.com/geo-team-red/geo-optimizer) | AnswerFirst、Authority、FAQ、Schema 等策略可组合 | Go 技术栈与本项目不同；部分评分属于启发式 | 在 Python 质量闸门中加入答案胶囊、分块和引用多样性 |
| [osvaldoabel/geo-skill](https://github.com/osvaldoabel/geo-skill) | 站点审计分类完整：crawler、sitemap、llms、schema、freshness、authority | 新项目、样本少；“Elite”阈值没有跨平台因果证明 | 扩展站点资源审计，但不照搬营销化等级 |
| [staksoft/geo-seo-aeo-skill](https://github.com/staksoft/geo-seo-aeo-skill) | 无依赖审计、内容与 JSON-LD 输出分离 | 新项目、规则型评分为主 | 吸收可重复的静态审计，不引入其模板内容 |
| [paulacavero/aeo-tracker](https://github.com/paulacavero/aeo-tracker) | API 与真实浏览器双通道、原始结果分开保存 | 无明确许可证且仍是 WIP | 只采用“双通道结果不能混为一谈”的原则，不复制代码 |
| [anyin-ai/aperture](https://github.com/anyin-ai/aperture) | BYOK、自托管、趋势和竞品监测 | 项目仍处早期；其安全说明提示密钥明文风险 | 保持本机数据、环境变量密钥和供应商适配器架构 |
| [ansvisor/ansvisor](https://github.com/ansvisor/ansvisor) | 多引擎、引用来源分类、主题聚类、AI 流量分析 | 体量较大，直接引入会重复现有系统 | 吸收 citation domain、topic、referral 的数据模型 |
| [optifeed/optifeed-radar](https://github.com/optifeed/optifeed-radar) | 零密钥技术审计覆盖 robots、llms、schema、sitemap | 实际 AI 检测仍依赖供应商密钥 | 将技术就绪度与真实 AI 可见度拆成两个指标 |
| [princy2310/ai-visibility-audit](https://github.com/princy2310/ai-visibility-audit) | 提及、突出度、情感、来源集中度与固定 buyer-intent prompt | 仍需要真实多轮样本才能稳定 | 加入 answer hash、来源域名分布与重复采样基础 |
| [generative-engine-optimization-handbook](https://github.com/ferinazumaDEV/generative-engine-optimization-handbook) | 研究索引、技术清单、测量与伦理边界 | 文档型项目且非常新 | 用作交叉检查，不作为代码依赖 |

## 本次已落地

1. 建立 30 个核心问题 × 5 种意图的固定 GEO 基准集；提示词保持品牌中立。
2. 每条 AI 回答记录品牌提及、推荐、位置、情感、竞品、引用 URL/域名、自有域名引用、答案哈希和透明可见度分。
3. 建立来源域名统计与按供应商聚合的趋势快照，区分“被提到”“被推荐”“被官网引用”。
4. 生成 Brand + WebApplication 实体图、事实账本、答案胶囊和部署清单；未核验公司主体前不冒充 Organization。
5. 官网恢复后生成符合提案结构的 `llms.txt`、`llms-full.txt`、robots 建议和 sitemap 计划。
6. 站点审计加入 indexability、语言、Open Graph、answer-first、问句标题、段落分块，以及 robots/sitemap/llms 资源检查。
7. 内容闸门加入可摘取直接答案和分块检查，仍坚持事实、来源、联系方式和夸大宣传阻断。

## 明确不采用

- stealth、验证码绕过、批量账号或高频抓取；
- 用随机数、模拟数据或单次回答冒充真实 GEO 成效；
- 把 `llms.txt`、Schema 或 IndexNow 描述成“保证被 AI 推荐”；
- 未经核验的公司、电话、客户、案例、交易数据、排名与好评；
- 只重写关键词、不保留来源和原始回答的黑盒优化。

## 上线后的实验方法

以当前 0% 提及和 0% 自有域名引用为真实基线。官网上线后按周固定抽样，并保留同一问题的五种表达。分别观察抓取与索引、来源被检索、品牌被提及、品牌被推荐、官网被引用以及最终带 UTM 的访问/注册，不能把其中任一步骤当成最终获客。
