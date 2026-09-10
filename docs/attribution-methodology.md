# 宏图商机汇 GEO 获客归因方法

## 目标

这套归因用于回答“可观测的 AI 访问是否继续形成咨询、有效线索、报价或成交”，不用于声称某个 AI 渠道造成了因果增量。官网尚处于 ICP 阶段时，先用测试事件验证接入协议；正式数据必须等第一方落地页和 CRM 接入后产生。

## 事件与身份边界

- 事件阶段固定为 `ai_referral → signup → inquiry → qualified_lead → quote → won`。
- 同一第一方随机 UUID 经过本地独立盐值哈希后串联路径；手机号、邮箱、微信号或姓名不能充当匿名标识。GEO 库不设计用于保存姓名、电话、邮箱、微信号或聊天内容，扩展 metadata 只接受配置白名单中的扁平 ASCII 分类标识。
- 每个事件必须使用稳定 `event_id`。相同 ID、相同规范化载荷是幂等重放；相同 ID、不同载荷会被拒绝，避免网络重试静默污染漏斗。
- 显式时间必须是带时区的 ISO-8601，并统一存为 UTC。
- UTM 和 metadata 都只接受短小的 ASCII 分类标识，不接受自由文本；中文展示名称应在报表层映射，不能把姓名或聊天内容塞入归因字段。

## 报告口径

- 原始漏斗显示所有可观测主体；可归因漏斗只显示与 `ai_referral` 使用同一匿名标识的主体。
- 同时输出首次触点与末次触点两个规则模型，默认回溯 90 天；不会用低样本规则模型冒充数据驱动归因。
- 输出从首次 AI 访问到咨询、成交的中位天数。
- 缺匿名标识、孤立下游事件、阶段重复、阶段倒序、非法事件类型、非法时间和未来时间单独报告；非法事件类型、非法时间与未来事件不进入有效漏斗和规则归因，不悄悄混入“健康”结论。

## 公开方法依据

- [Google Analytics：归因概览](https://support.google.com/analytics/answer/10596866)说明归因是向转化路径触点分配信用，并区分规则模型与数据驱动模型。
- [Google Analytics Measurement Protocol](https://developers.google.com/analytics/devguides/collection/protocol/ga4/reference)使用事件、用户/客户端标识和会话标识关联线上与线下行为。
- [Measurement Protocol 使用场景](https://developers.google.com/analytics/devguides/collection/protocol/ga4/use-cases)明确会话归因依赖会话标识与时间窗口。
- 开源项目 [ra_attribution](https://github.com/rittmananalytics/ra_attribution)展示了首次、末次及多触点规则模型，并强调回溯窗口和缺失营销触点的处理。

这些资料只用于方法设计，不代表 Google、开源作者或任何 AI 平台对宏图商机汇的认可或背书。
