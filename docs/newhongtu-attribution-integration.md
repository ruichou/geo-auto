# NewHongTU → GEO 匿名获客归因接入契约

状态：接口与安全契约已就绪，NewHongTU 尚未接入。本文不授权修改 NewHongTU，也不代表已经收到真实事件。

## 安全边界

- GEO 只接收第一方生成的随机 UUID；不接收用户 ID、openid、手机号、姓名、邮箱、微信号、公司名或聊天内容。
- NewHongTU 的 `remark` 是自由文本，禁止复制、摘要或哈希后发送。
- 每个业务事件使用稳定且不含用户标识的幂等键，例如 `signup:<outbox_uuid>`。
- 请求头必须包含 Unix 秒 `X-Hongtu-Timestamp` 与 `X-Hongtu-Signature`。
- 签名原文是 `timestamp + "." + raw_request_body`，算法 HMAC-SHA256，格式 `sha256=<64位十六进制>`。
- 双方通过环境变量 `HONGTU_ATTRIBUTION_INGEST_SECRET` 保存同一条至少 32 字节密钥；密钥不得进入代码、数据库事件载荷或日志。
- GEO 拒绝超过 300 秒的请求，防止旧请求被重放；业务幂等键负责阻止窗口内重复入库。

## 已核实的业务事件映射

| NewHongTU 事实来源 | GEO 事件 | 触发条件 | 明确排除 |
|---|---|---|---|
| `server/src/services/customer-identity-service.js` | `signup` | `loginWithIdentity` 的新用户事务实际返回 `created: true` | 普通登录、资料刷新不能算注册 |
| AI 落地页/入口 | `ai_referral` | 有真实 AI UTM、referrer 或用户自报来源，并已建立匿名 UUID | 不得从注册或购买反推 AI 来源 |
| `server/src/routes/mobile-owned-opportunities.js` | `qualified_lead` | 反馈状态明确变为 `interested`，且产品负责人确认它代表合格线索 | 不发送 `remark`；`contacted`/`following_up` 不自动拔高 |
| 同上 | `won` | 反馈状态明确变为 `closed_won`，且业务定义确认这就是成交 | 不把购买商机自动等同于工程成交 |

`server/src/routes/mobile.js` 的接单/买断会生成 `purchase_records`，这是可靠的产品付费动作，但当前 GEO 漏斗没有“购买商机”独立阶段，暂不错误映射为 `inquiry` 或 `won`。后续应在业务口径确认后新增独立 `purchase` 指标。当前也没有可靠的报价事件，因此不发送 `quote`。

## 推荐投递架构

在产生业务事实的同一数据库事务中写入 NewHongTU 自己的 attribution outbox；事务提交后由异步 worker 投递到 GEO。投递失败不能回滚注册、购买或反馈。worker 应使用租约/抢占、指数退避、最大重试、死信和幂等键，沿用项目已有队列 worker 的可靠性模式。

事件体示例（仅演示结构）：

```json
{
  "event_id": "signup:018f-example-outbox-id",
  "occurred_at": "2026-09-05T12:00:00+08:00",
  "event_type": "signup",
  "source_engine": "unknown",
  "anonymous_id": "00000000-0000-4000-8000-000000000001",
  "utm_source": "",
  "utm_medium": "",
  "utm_campaign": "",
  "landing_url": "",
  "metadata": {"page": "mobile-login"}
}
```

Node.js 签名核心：

```js
const timestamp = String(Math.floor(Date.now() / 1000));
const rawBody = JSON.stringify(event);
const digest = crypto.createHmac('sha256', process.env.HONGTU_ATTRIBUTION_INGEST_SECRET)
  .update(`${timestamp}.${rawBody}`, 'utf8').digest('hex');
// X-Hongtu-Timestamp: timestamp
// X-Hongtu-Signature: `sha256=${digest}`
```

## 验收条件

1. 同一 outbox 事件重复投递只入库一次；同一幂等键换载荷必须报警。
2. 篡改正文、错误密钥、缺失签名、过期时间戳全部拒绝。
3. 业务接口在 GEO 停机时仍正常成功，outbox 稍后重试。
4. 抽查 GEO 数据库与日志，不能出现上述个人信息或反馈备注。
5. 只有真实 `ai_referral` 与后续同一匿名 UUID 的事件才能进入可归因漏斗；规则归因不宣称因果增量。
