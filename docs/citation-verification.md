# AI 引用链接核验

系统把 AI 回答中的“域名统计”和“具体页面链接”分开保存。写入探测样本前，回答正文和捕获链接里的 URL 都会先脱敏；具体链接再做离线规范化：只接受 HTTP/HTTPS，拒绝用户名密码、非标准端口、localhost、内网/保留地址，并永久移除查询参数和片段，避免把令牌或跟踪参数写入数据库。

只有同时满足以下条件的链接，才会在每周流程中发起受限公网请求：

- 至少来自两个独立观察，且跨引擎或跨问题；
- 域名属于 `research_sources`、已核验竞品域名或 `citation_verification.allowed_domains`；
- DNS 解析结果全部为公网地址，实际连接固定到本次已验证的 IP，同时保留正确的 HTTP Host 与 HTTPS SNI；
- 请求不跟随重定向，只读取有限字节并设置短超时。

不在白名单的链接标记为 `needs_domain_review`，不会自动访问。自有域名只记录为 `owned_observed`，ICP 阶段不会因该台账触发官网访问。HTTP 可访问只说明当时能读取，不代表内容真实、权威、与宏图商机汇合作或形成背书。

安全边界参考 OWASP 的 [SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html) 与 [OWASP Top 10 SSRF](https://owasp.org/Top10/2021/A10_2021-Server-Side_Request_Forgery_%28SSRF%29/)：限制协议和目标、校验 DNS/IP、拒绝私网与本地地址、禁用自动重定向。为避免 DNS 校验后再次解析产生时序差异，连接固定到已校验公网 IP；核验结果仍不会自动进入品牌事实账本。
