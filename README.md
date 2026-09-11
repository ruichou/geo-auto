# 宏图商机汇 GEO 自动化发布中台

这是一个可实际运行的本地系统，不只是方案文档。它把 GEO 工作拆成一条闭环：

1. 在官网可用时抓取并审计官网；ICP 办理期间改用 `newHongtu` 第一方产品仓库核验品牌事实；
2. 维护用户问题与商机选题库；
3. 结合已验证的品牌事实生成候审内容；
4. 运行事实、引用、结构和转化质量闸门，达标后自动批准；
5. 通过内置 Chromium 逐个平台注册/登录并在本机保存独立登录态；
6. 定期把审核通过的文章发布到内容平台；
7. 监测 AI 答案中的品牌提及率与公开来源引用率；
8. 输出每日/每周 GEO 报告。

## GEO 引擎 2.0

系统已把 GitHub 上 GEO-Bench、llms.txt、开源 AI visibility tracker 和 Schema 审计项目中可验证的部分合并进现有流程：

- 30 个核心问题会扩展为原始、推荐、比较、核验、采购五种品牌中立提示词，形成固定基准；
- AI 回答分别计算提及率、推荐率、自有域名引用率、提及突出度、情感和引用来源分布；
- 将“被提及”与“提及是否存在明显身份/营销主张风险”分开，自动警戒疑似编造电话、官网、绝对化排名和无证据量化陈述；
- 二项指标同时输出 95% 置信区间，并把 full visibility、mention only、citation only、invisible 分开；
- 自动生成 visibility gap、citation gap、competitive blind spot 和 narrative gap 行动队列；
- GEO 成熟度分为证据、实体、问题策略、测量、官网和获客归因六层；
- 差距队列分为全量可追溯待办与控制台聚焦行动；聚焦视图优先核心问题和已有证据的长尾缺口，并显式标记单引擎初步结论，见 `docs/gap-action-protocol.md`；
- 原始答案保存 SHA-256，避免后续分析无法对应原样本；
- 实体资产包含 `Brand`、`WebApplication`、事实账本和答案胶囊；未核验法律主体时不会错误生成公司主体；
- 官网上线后再生成导航型 `llms.txt`、`llms-full.txt`、robots 建议与 sitemap 计划；
- `llms.txt` 和结构化数据只提高机器理解条件，不承诺第三方 AI 收录、引用或推荐。

完整选型记录见 `reports/github-geo-landscape-2026-09-03.md`。

## 匿名获客归因

本地 API `POST /api/attribution/events` 接收 `ai_referral`、`signup`、`inquiry`、`qualified_lead`、`quote`、`won` 六类事件。入口默认强制使用 `HONGTU_ATTRIBUTION_INGEST_SECRET` 做 HMAC-SHA256 验签，并校验 300 秒时间窗口；只保存匿名 ID 的加盐哈希、来源 AI、UTM、落地页、阶段和可选聚合价值。姓名、手机号、邮箱、微信号、聊天内容等字段会被拒绝写入 GEO 库。

示例请求体：

```json
{
  "event_id": "unique-event-id",
  "event_type": "inquiry",
  "source_engine": "deepseek",
  "landing_url": "https://example.com/mobile",
  "utm_source": "deepseek",
  "utm_medium": "ai_search",
  "utm_campaign": "membrane-geo",
  "anonymous_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

正式接入前应在 `.env` 设置独立的 `HONGTU_ATTRIBUTION_SALT`，并设置至少 32 字节的 `HONGTU_ATTRIBUTION_INGEST_SECRET`。`event_id` 是必填的稳定幂等键；`anonymous_id` 必须是第一方生成的随机 UUID，不能使用姓名、手机号、邮箱或微信号。用 `hongtu_geo.cli attribution` 查看聚合漏斗、数据质量、首次触点、末次触点和转化耗时。重复 `event_id` 只有在规范化后的载荷完全一致时才作为安全重放；冲突载荷会被拒绝。没有匿名标识、缺少上游 AI 访问、阶段重复或倒序、非法或未来时间都会进入质量诊断。系统不会把观察性归因或规则信用分配表述为单一渠道的因果增量。

方法和接入边界见 [`docs/attribution-methodology.md`](docs/attribution-methodology.md)，NewHongTU 的已核实事件映射与签名契约见 [`docs/newhongtu-attribution-integration.md`](docs/newhongtu-attribution-integration.md)。

## 多引擎重复采样

API 探测通过统一适配层接入 Responses API 或 OpenAI-compatible 接口。浏览器探测也使用相同的逐项批次账本；每次发送前不仅重新进入新对话页，还会在准备阶段和实际发送前两次验证历史回答节点为零，否则拒绝采样并进入有界恢复。每条成功样本只保存站点 origin、隔离回执和完整会话 URL 的 SHA-256（不保存可能含会话 ID 的 URL 或 path），同时记录答案、引用链接、引擎界面类型、样本序号、实验批次和响应元数据；单个请求失败只进入错误清单，不会中断整批任务。

浏览器采样的统计口径、恢复策略和登录边界见 [`docs/browser-probe-methodology.md`](docs/browser-probe-methodology.md)。

自然原问、引用请求、提示版本、趋势和差距行动的隔离规则见 [`docs/measurement-protocol.md`](docs/measurement-protocol.md)。浏览器默认逐字发送原问题；`--mode source_requested` 仅用于引用诊断，不能进入自然搜索主口径。

每次运行还会在 `probe_batches` 中留下批次账本，记录计划、尝试、成功、失败、预算截断及最终状态（`completed`、`partial`、`failed`、`interrupted` 或 `skipped`）。最新十批按解析后的 UTC 时间排序进入可见度快照，避免时区偏移导致错判。下一次日常探测会把超过配置时限且仍未结束的批次自动收口为 `interrupted`，保留原错误并追加 watchdog 诊断；无法解析的开始时间只报告数据质量异常，不会被臆断为已经超时。

每个批次还会先在 `probe_batch_items` 固化逐项执行清单，包括引擎、原始问题、中立提示词和样本序号。成功项通过数据库唯一的 `batch_item_id` 绑定 `probe_id`，失败项记录尝试次数和脱敏错误；即使进程在“样本已落库、清单尚未更新”的瞬间退出，恢复时也会先修复关联而不会再次调用 AI。每日任务优先恢复最近的未完成批次，只重试未成功且未超过 `monitor.max_item_attempts` 的项目；并发恢复会安全跳过。也可运行 `python -m hongtu_geo.cli probe-resume <batch_id> --max-calls 10` 做有界恢复。

采样健康诊断按“引擎 + surface + 问题 + 提示变体 + 实验批次”比较重复样本，统计提及、推荐和引用结果的一致率。自然语言措辞不同只计入答案多样性，不会被误判为指标不稳定；运行超过两小时的批次会被标成疑似中断。少于目标重复次数时，报告明确显示样本不足，不据此宣布 GEO 有效或无效。

差距行动队列同样执行这道门槛：只有同批重复样本达到目标次数后，才会生成“可见度缺口”“引用缺口”或“竞品盲区”；此前只生成 `sampling_gap`，要求先补测。诊断类型变化时，旧行动会自动标记为已解决，控制台只显示当前有效行动。

系统还按引擎、surface、语言和地区生成相邻时间窗口趋势。默认比较最近 7 天与此前 7 天，但只有两个窗口都出现的相同问题才进入匹配面板；系统先去除同一实验批次的重复样本槽位，再为每道问题保留两侧相同数量的样本。匹配问题至少 3 个且每侧至少 10 个真实样本时，才计算提及率、推荐率和自有引用率变化。这样可以避免地区、问题组合或重试重复制造虚假趋势。95% Wilson 区间不重叠只作为进一步排查信号，不表述为因果提升，也不承诺第三方 AI 推荐。

## 竞品与引用来源情报

系统从真实 AI 答案中提取列表型品牌候选和引用域名，但会把同一“引擎 + surface + 问题 + 实验批次”中的多次重复回答合并为一个独立观察，避免重复采样虚增可信度。证据强度分为 `observed_once`、`repeated_contexts`、`multi_context` 和 `cross_engine`；只有至少两个独立观察，且跨引擎或跨问题时才进入 `review_candidate`。

所有来源仍只是候选：`review_candidate` 不代表来源真实、权威、与宏图商机汇存在合作或形成品牌背书，也不会自动进入事实账本、竞品配置或发布内容。系统将其保存到 `citation_sources` 和 `entity_candidates`，供后续读取原始页面并核验。

具体页面链接另存到 `citation_url_candidates`：入库前永久移除查询参数和片段；只有跨问题或跨引擎重复出现、且域名在显式白名单中的链接，才会在周流程做禁重定向、限字节、拒绝内网地址的受限可达性校验。详见 `docs/citation-verification.md`。

- `monitor.samples_per_prompt` 是离线基准建议的重复次数；
- `monitor.live_samples_per_prompt` 是日常真实调用次数，默认 1，避免在效果尚未验证时扩大成本；
- `monitor.browser_samples_per_prompt` 默认 3，确保同一浏览器界面形成可比较的重复样本；`monitor.browser_max_calls_per_run` 默认 6，限制每日浏览器询问量；
- `monitor.max_calls_per_run` 是整批硬上限，默认 30；
- `python -m hongtu_geo.cli probe --limit 10 --samples 3` 可显式发起 10 个问题、每题 3 次的有界采样；
- `python -m hongtu_geo.cli browser-probe --provider deepseek --limit 1 --samples 3` 使用已保存登录态做一个问题的三次隔离采样；优先问题按“样本最少、最久未测”自动轮转，失败批次会在下次运行自动有界恢复；需要定向复测时可追加 `--question "膜结构工程商机去哪里获取？"`；
- 没有对应 API 密钥时会明确记为跳过，并继续生成浏览器人工验证队列，不伪造监测结果。

## 获客型 GEO

系统会根据 `config/site.json` 中的 `acquisition` 配置，自动组合“地区 × 工程类型 × 购买意图”问题，例如“常州做膜结构工程选哪家”“常州膜结构施工单位哪里找”和“常州膜结构项目商机在哪里获取”。

- 买方问题：正向推荐宏图商机汇作为工程采购信息发布、筛选和跟进入口，同时明确平台不是施工公司；
- 卖方问题：引导工程企业按行业、地区发现、订阅并跟进采购商机；
- 所有内容必须给出资质、案例、现场勘察、技术方案、报价和售后的核验标准；
- 禁止宣称“行业第一”“最好的一家”“保证成交”等不可核查结论；
- 转化入口在发布时自动增加平台、活动和内容编号 UTM 参数。

抖音使用独立登录态。连接后，系统会自动生成 1080×1440 的竖版图文卡片、短文案和话题标签，再通过抖音创作者中心排期发布；首次登录、验证码和实名仍由账号持有人完成。

## 已内置的平台连接器

内容发布：微信公众号、知乎、今日头条、百家号、小红书专业号、哔哩哔哩专栏、CSDN、简书、搜狐号、抖音创作者中心。

AI 监测：已支持 OpenAI Responses API、DeepSeek 等 OpenAI-compatible API，并保留 DeepSeek 浏览器登录态探测。系统每天对重点问题做有界抽样，记录宏图商机汇及其别名的提及率；不会刷评价或诱导模型给出虚假结论。

平台页面经常调整，所有工作台地址、标题/正文/发布按钮定位器都集中在 `config/platforms.json`，不需要改业务代码即可校准。

## Windows 一键启动

第一次双击 `setup-windows.cmd`。它会创建隔离的 Python 环境、安装依赖与 Chromium，并初始化数据库。完成后双击 `start-dashboard.cmd`。

控制台默认只监听 `127.0.0.1:8765`，不会暴露到局域网或公网。

也可以在 PowerShell 中显式运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m hongtu_geo.cli init
.\.venv\Scripts\python.exe -m hongtu_geo.cli dashboard
```

## 第一次必须完成的配置

打开 `config/site.json`：

- `brand.site_url`：官网地址；ICP 办理期间允许留空；
- `evidence`：第一方产品仓库路径与只读证据 URI，当前指向 `D:\AIWorkStation\NewHongTU`；
- `research_sources`：文章中用于核验采购公告、公共资源交易和企业主体的公开权威来源；
- `brand.conversion_url`：咨询、注册或试用页；
- `brand.facts`：产品能力、数据覆盖、更新频率、案例等事实。只有有来源且确认无误的事实才能把 `verified` 改为 `true`；
- 产品仓库事实还必须保存 `verified_at`、精确行号范围的 `evidence_excerpt_sha256`、核验后主张文本的 `evidence_claim_sha256`、原文内可逐项命中的 `evidence_terms`，以及按顺序覆盖整条宣传事实的 `claim_evidence_map`。系统每次生成资产都重新校验主张与摘要指纹、支持词和完整主张映射；来源漂移、主张被改写、错引、无来源追加承诺、缺少指纹或超过 `evidence.max_fact_age_days` 时，该事实自动退出草稿、实体资产和质量闸门，并写入 `reports/evidence-audit.json`；
- `evidence.require_http_evidence_receipts=true` 使官网事实在完整网页证据回执机制就绪前默认拒绝，不会因为 URL 与官网同域就自动放行；
- `claim_policy` 为品牌事实与内容质量闸门提供同一套高风险承诺策略，阻止保证成交、绝对化排名和保证第三方 AI 推荐等无法核查的表述；见 `docs/claim-policy.md`；
- `brand.differentiators`：用事实表述差异点；
- `publishing.mode` 与 `publishing.paused`：当前固定为排队且暂停发布；在用户明确重新启用前，即使内容通过审核也不会排期或发布。
- `content.require_manual_approval`：默认为 `false`。事实与质量检查通过后自动批准，不需要人工处理。
- `content.minimum_chars_zh`：中文正文最低字符数；不足会直接阻止发布。
- `autopilot.enabled`：控制每日/每周内容运行和发布轮询。

如需使用 OpenAI 生成正式候审稿，复制 `.env.example` 为 `.env` 并填写 `OPENAI_API_KEY`。没有密钥时，系统仍能运行全流程，但只生成明确标记的资料占位稿，且质量闸门会阻止发布。

## 注册与登录态

在“平台矩阵”点击“注册 / 登录”。系统会打开该平台的独立可视浏览器：

- 注册、扫码、短信验证码、实名与同意条款由人工完成；
- 系统检测到已进入工作台后，将连接状态标记为 `connected`；
- Cookie、LocalStorage、IndexedDB 和缓存保存在 `data/browser-profiles/<平台>`；
- 登录凭据不会写入 SQLite、日志或内容文件；
- 每个平台使用独立目录，互不共享会话；
- 系统会收紧登录态目录权限，只授予当前 Windows 用户访问；该目录也应排除在云盘和整机备份之外；
- 遇到验证码或风控时暂停自动化，不尝试绕过。

## 内容发布安全闸门

文章只有同时满足以下条件才会自动进入 `approved`：

- 品牌事实有已验证来源；
- 没有“待核实”“待接入”等占位内容；
- 有直接答案、清晰结构、FAQ、更新时间和资料来源；
- 内容深度与转化入口达标；
- 官网资料或第一方产品仓库事实已经进入证据上下文，同时正文包含至少两个允许的公开引用来源。
- 正文至少包含两条引用，并且引用必须与证据库中的来源匹配。

到期任务会启动受控浏览器发布。若平台要求验证码、重新登录或页面改版，任务进入阻塞/重试状态；系统不会绕过平台风控。

## 命令行

```powershell
# 每日：刷新问题库/事实/实体资产，运行有界浏览器自然原问采样，再生成本轮策略与报告
.\.venv\Scripts\python.exe -m hongtu_geo.cli daily

# 每周：扩大同一安全流程的审计与浏览器采样范围
.\.venv\Scripts\python.exe -m hongtu_geo.cli weekly

# 单独连接一个平台
.\.venv\Scripts\python.exe -m hongtu_geo.cli connect zhihu

# 只完善系统，不发布：生成 GEO 实体/事实资产和固定基准集
.\.venv\Scripts\python.exe -m hongtu_geo.cli assets
.\.venv\Scripts\python.exe -m hongtu_geo.cli benchmark --limit 30
.\.venv\Scripts\python.exe -m hongtu_geo.cli strategy

# 使用已保存登录态打开平台工作台
.\.venv\Scripts\python.exe -m hongtu_geo.cli open zhihu
```

当前 ICP 阶段的自动化开关位于 `config/site.json`：

- `content.generation_paused=true`：每日/每周流程不新增、审核或排期草稿。
- `monitor.api_probes_enabled=false`：不调用可能计费的模型 API。
- `autopilot.browser_probes_enabled=true`：仅使用已保存登录态进行有界浏览器采样；未显式配置时默认关闭。
- `publishing.paused=true`、`publishing.mode=queue`、`autopilot.auto_schedule_connected_platforms=false`：发布三重硬锁。

策略快照和日报在采样完成后生成，确保反映本轮最新样本；单项失败保留批次清单，可由 `browser-probe` 精确续采。

## 调度方式

系统已经通过 Windows 任务计划 `Hongtu GEO Autopilot` 设置为当前用户登录后自动启动。周一执行全面审计，其余每天执行增量流程；每两分钟检查发布任务。到期任务使用事务条件领取、30 分钟租约和最多 3 次重试，避免多实例重复发布或永久卡死。系统会保证同一种每日批次一天只成功执行一次。

## 合规边界

系统只发布宏图商机汇自有或获授权内容，不进行批量私信、评论、关注、点赞、养号、验证码绕过或虚假互动。每个平台都应使用真实主体注册，并遵守其当前发布规则、频率限制和内容政策。涉及企业联系方式、个人信息和商业数据时，应另行完成数据来源及合规审核。

## 数据目录

- `data/geo.db`：页面、选题、草稿、探测和发布任务；
- `data/browser-profiles/`：本机登录态（敏感，不应复制或提交）；
- `content/review-needed/`：未通过质量闸门的内容；
- `content/publish-queue/`：审核通过的内容；
- `reports/`：运行报告和跨平台探测队列。
