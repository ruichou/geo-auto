# 市场 GEO 获客公司的公开方法与宏图系统路线

调研日期：2026-09-03

## 商业 GEO 公司实际上在做什么

### 1. 建立真实问题库

商业平台不会只监测品牌名，而是维护品牌词、行业词、比较词、推荐词、采购词和地区词，并按漏斗阶段、用户角色、地区和语言分组。Profound 公开说明其问题来自自定义问题、自动生成问题和真实用户问题数据；Otterly 允许按 branded、non-branded、top-of-funnel、bottom-of-funnel 等标签筛选。

### 2. 跨 AI 采集原始回答

按固定频率在 ChatGPT、Perplexity、Gemini、Google AI、Copilot、Claude、DeepSeek 等平台重复运行同一批问题，并保存原始答案、引用链接、日期、地区和平台。成熟系统会区分消费者浏览器答案和 API 答案，避免把两个不同表面混成同一趋势。

### 3. 把回答拆成可测量信号

常用指标包括品牌提及率、推荐率、Share of Voice、出现位置、情感、官网引用率、引用页面、引用域名和竞品。更严谨的开源实现还区分 full visibility、mention only、citation only 和 invisible，并保留原始答案或哈希。

### 4. 做引用来源与竞品差距

成熟平台会把引用分为 Owned、Competitor、Earned Media、Social、Forum、Institution 等类型，找出：

- 哪些问题竞品出现而自己不出现；
- 哪些第三方网站经常被 AI 引用；
- 哪些自有页面被检索但没有转化成引用；
- 哪些品牌已经被提到但官网从未成为来源。

### 5. 反向指导内容和实体建设

系统根据差距生成具体动作：补哪一个问题的答案页、更新哪一个过期页面、强化哪个品牌事实、修正哪个错误描述、在哪类权威来源中建立可核验资料。内容本身通常采用答案优先、短段落、明确实体、可追溯事实、作者/更新时间、FAQ 和结构化数据。

### 6. 监控机器抓取与技术就绪度

检查 robots、sitemap、canonical、SSR/HTML 可读性、Schema/JSON-LD、站点名称、更新时间和 `llms.txt`。`llms.txt` 只是辅助导航约定，不是排名开关；结构化数据也必须与用户可见内容一致。

### 7. 把 GEO 连接到获客结果

真正的获客闭环需要将 AI referral、UTM、落地页、注册/咨询、自报来源、有效线索、报价和成交分层记录。AI 影响经常以品牌搜索或直接访问结束，因此不能只依赖最后点击；需要把直接 referral、辅助转化和用户自报来源结合分析。

## GitHub 可参照实现

- [GEO-optim/GEO](https://github.com/GEO-optim/GEO)：论文基准与黑盒优化实验。
- [AR-BABER/geocheck](https://github.com/AR-BABER/geocheck)：重复采样、五种 visibility state、盲区和一致性指标。
- [princy2310/ai-visibility-audit](https://github.com/princy2310/ai-visibility-audit)：原始 JSONL、跨供应商适配、引用媒体类型与来源集中度。
- [NomaDamas/geobench](https://github.com/NomaDamas/geobench)：多次运行、bootstrap 置信区间和 run-to-run diff。
- [hellowalt/aeo-radar](https://github.com/hellowalt/aeo-radar)：问题意图扩展、品牌/竞品/引用/情感看板。
- [anyin-ai/aperture](https://github.com/anyin-ai/aperture)：BYOK、自托管和多模型适配架构。
- [ansvisor/ansvisor](https://github.com/ansvisor/ansvisor)：主题、引用分类、内容机会与 AI referral 分析。
- [AnswerDotAI/llms-txt](https://github.com/AnswerDotAI/llms-txt)：`llms.txt` 提案及解析生态。

## 宏图系统当前对应能力

| 层 | 当前状态 | 下一里程碑 |
|---|---|---|
| 事实证据 | 5 条第一方产品事实、行级来源、事实哈希 | 官网上线后增加主体、备案、公开联系方式与页面级来源 |
| 实体 | Brand + WebApplication 实体图、答案胶囊 | 核验公司主体后再连接 Organization |
| 问题策略 | 210 个中文问题；30 × 5 固定基准 | 接入真实咨询记录后按匿名聚类补充问题 |
| AI 测量 | 提及、推荐、引用、情感、状态、置信区间 | 扩展多引擎、每问题至少 3 次重复样本 |
| 差距诊断 | 自动生成 visibility/citation/narrative/competitor gap | 用真实竞品和引用域名生成内容与公关动作 |
| 官网技术 | 资产已预生成，官网保持停用 | ICP 完成后部署并验证 robots/sitemap/JSON-LD/llms |
| 获客归因 | 已有 UTM 规范和事件方案 | 产品入口接入首访、注册、咨询、有效线索和成交聚合回传 |

## 当前最重要的事实

系统成熟度当前为 67/100，表示内部能力已经成型，不表示外部 GEO 达到 67 分。当前只有 4 个 DeepSeek 样本，全部属于 invisible；95% 区间仍然很宽，不能据此判断长期表现。现阶段最大的系统缺口是重复样本、多引擎真实观测、官网公开实体和最终获客归因。
