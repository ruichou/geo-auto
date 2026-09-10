# GEO 公开研究周观察｜2026-09-07

## 本周采用

- [open-geo](https://github.com/Pupok462/open-geo) 的可复核方向值得采用：浏览器渲染结果、版本化运行工件、本地历史、按引擎拆分、重复运行观察波动。本系统本周补齐统一采样溯源回执与策略快照 schema 版本。
- [State of GEO 2026](https://github.com/Broadcastwell/state-of-geo-2026) 公开数据明确暴露单引擎单次运行的局限，以及跨引擎差异和重复运行噪声。本系统继续坚持逐引擎报告、自然原问主口径和重复采样，不合成一个伪精确总分。

## 暂不直接接入

- open-geo 的“来源到引用”漏斗依赖平台能稳定暴露完整来源面板。当前 DeepSeek/Kimi 适配器只能保存回答中实际捕获的引用链接，因此不推断隐藏检索来源。
- [GEOBench](https://github.com/glad-lab/geobench) 与 [SafeGEO](https://github.com/QianfengWen/SafeGEO) 更适合作为离线对抗/可验证性评测参考；不把攻击样本或排名操纵方法接入生产获客流程。

## 方法边界

本周改动只增强测量可追溯性，不产生新采样、不改变历史结果、不生成内容、不排期、不发布，也不声称第三方 AI 会稳定推荐宏图商机汇。
