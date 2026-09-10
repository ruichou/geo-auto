# 品牌事实证据完整性

`verified: true` 只表示有人声明核验过，不足以让事实进入 GEO 内容。产品仓库事实还必须同时满足：

- 来源使用配置的只读 `repo://newhongtu/` 边界，解析后的文件仍位于仓库内；
- 来源带合法、未越界的精确行号范围；
- 当前行号范围内容的 SHA-256 与事实中保存的 `evidence_excerpt_sha256` 完全一致；
- 开启 `evidence.require_claim_fingerprint` 时，当前宣传主张的 SHA-256 必须与显式核验后保存的 `evidence_claim_sha256` 一致；任何追加、删除或改写都会返回 `claim_changed`；
- 开启 `evidence.require_support_terms` 时，每条事实至少声明 `evidence.minimum_support_terms` 个关键支持词，且必须全部出现在指定原文片段内；
- 开启 `evidence.require_claim_evidence_map` 时，`claim_evidence_map` 必须按原顺序覆盖完整宣传主张，且每个原子主张段都必须有至少一个同时出现在该主张段和源片段中的 `evidence_terms`；仅存在于源码的路由名、组件名等可单独写入 `source_markers`，但不能代替语义共同词；
- `verified_at` 是有效且不在未来的日期，并且未超过 `evidence.max_fact_age_days`。

支持词会忽略空白和大小写差异，但不做模糊匹配。缺少足够支持词时状态为 `missing_support_terms`；任一支持词未命中时为 `claim_not_supported_by_excerpt`。回执还会尽力记录 Git HEAD 为 `repository_revision_context`，但仓库可能有未提交修改，所以真正的完整性边界仍是源文件和摘要 SHA-256。

支持词按忽略空白和大小写后的值去重，单字符或纯标点不计入数量，避免用空格变体凑够最低数量。

`evidence.require_http_evidence_receipts=true` 时，即使 URL 属于官网域名，当前也不会仅凭“同域名”标记为已核验，而是返回 `missing_http_evidence_receipt`。在官网证据采集、摘录指纹和重现机制完成前，网页事实保持关闭；这也防止 ICP 完成后刚填入官网地址就误放行任意声明。

主张映射闸门会产生三类明确失败状态：

- `missing_or_invalid_claim_evidence_map`：未配置映射、映射不是列表、原子主张为空或没有有效证据词；
- `claim_map_does_not_cover_claim`：映射片段去除标点和空白后，不能按顺序完整还原宣传主张；
- `claim_map_evidence_missing`：映射完整，但某个原子主张声明的证据词并未出现在当前源片段。

这使“在已核验事实后追加保证成交、行业第一、覆盖全国等无来源扩展”不能沿用旧回执通过。`claim-ledger.json` schema v3 保留每个原子映射、命中结果、源文件指纹和 Git HEAD 上下文，便于独立复核。

主张指纹和主张映射是两道独立闸门：指纹防止改写后沿用旧核验回执，映射使重新核验时能逐段查看证据。如果主张确有业务变更，必须重新阅读原文、重建映射，最后才能更新 `evidence_claim_sha256`；不得只对新文字机械重签。

系统每次构建实体资产时生成 `reports/evidence-audit.json`。如果源文件内容或行号发生变化，状态变为 `source_changed` 或 `line_range_out_of_bounds`，事实立即退出：

- LLM 与离线草稿的可用品牌事实；
- 实体图、答案胶囊和 AI 知识文件；
- 内容质量闸门中的 `brand_facts_available` 与 `verified_sources_complete`。

如果所有事实都失效，活动 `content/site-assets` 中的可部署实体与知识资产会移动到带时间戳的 `content/quarantined-site-assets` 可恢复隔离区，活动目录只保留 `blocked_invalid_evidence` 部署清单，避免旧文件被误用。

已批准草稿重新核验失败时，旧发布文件会移入 `content/quarantined-drafts`，关联的未发布任务改为 `blocked`。`content/review-needed` 只写停用原因和质量诊断，不复制可能包含失效事实的旧正文。

重新核验时应先阅读新的原始行号范围，确认每个原子主张仍被源内容支持，再更新摘要指纹、支持词、主张映射和 `verified_at`。映射仍不是自动的语义真值证明；不能为了让闸门通过而机械更新哈希、填入无关词或将多个独立承诺塞入一个模糊映射。
