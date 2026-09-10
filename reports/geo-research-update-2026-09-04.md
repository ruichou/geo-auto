# GEO 公开研究与开源实现增量审计（2026-09-04）

## 本轮结论

公开研究和近期自托管项目共同支持四个方向：真实产品界面与 API 分层、固定并版本化提示词、同条件重复采样、把提及/引用/经济结果分开。最需要避免的是把“请列来源”的测试回答当成普通用户搜索结果，或在提示词改变后继续绘制同一条趋势线。

## 已吸收进系统

1. 浏览器默认改为逐字发送自然用户原问；引用请求成为独立 `source_requested` 模式。
2. `probe_batch_items` 和 `probes` 同时保存 `prompt_variant` 与 `prompt_version`。
3. 重复采样、趋势、引擎/surface 汇总均按提示模式和版本分组。
4. GEO 差距行动只接受完整主口径 `browser + naturalistic + naturalistic-v1`；API、其他 surface 或其他版本不能代替自然基线。
5. 控制台头部提及率、推荐率与引用率使用同一完整主口径作为分母，避免实验样本污染结果。
6. 历史可识别的浏览器/API 批次按实际提示内容迁移为 `source_requested-v1`，无法确定的样本保留为 legacy。

## 未盲目照搬

- 不采纳任何“固定百分比提升”“保证被推荐”或只依据站内技术打分推断外部效果的说法。
- 不把 API 模型输出当作 ChatGPT、DeepSeek 等消费端产品界面结果。
- 不使用另一个 LLM 对回答做不可审计的二次打分来替代确定性指标；当前品牌提及、链接、位置和基础语境保留可回放规则。
- 官网 ICP 阶段不运行站点审计或部署建议；现有实体资产继续处于待官网可用状态。

## 来源

- https://arxiv.org/abs/2607.14035
- https://github.com/AntonioBlago/llm-visibility-framework
- https://github.com/princy2310/ai-visibility-audit
- https://github.com/aryamantodkar/oneglanse
- https://arxiv.org/abs/2311.09735
