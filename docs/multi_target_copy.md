# 多地址跟单、实盘 Maker 入场与止盈止损

2026-11-26 起，系统支持多个跟单地址，每个地址独立参数。纸盘仍是盘口模拟，
实盘新增 maker GTC 入场、FAK 补单和止盈止损。本文是配置与运维说明；具体实现
见 `polycopy/engine.py`、`execution.py`、`live_client.py`、`store.py`。

## 1. 每个地址的参数

配置页「策略配置」可以新增/切换/删除地址。每个 `[targets.<name>]` 独立：

| 参数 | 默认/示例 | 说明 |
|---|---:|---|
| `buy_price_min` / `buy_price_max` | 0.30 / 0.70 | 只跟目标买入价在这个区间的成交 |
| `min_target_usd` | 1000 | 目标单笔买入金额低于此值不跟 |
| `sizing_mode` `fixed_notional_usd` | fixed / 10 | 我方每笔固定金额（也可用 scale） |
| `min_hours_to_end` | 2.0 | 距市场结束至少 N 小时；0=不检查 |
| `maker_entry_enabled` | true | 实盘 maker 挂单入场；纸盘忽略 |
| `maker_timeout_seconds` | 300 | maker 未成交超时，撤单后 FAK 补剩余 |
| `fak_max_premium_pct` | 0.02 | FAK 补单最高价 = 目标价 × 1.02 |
| `take_profit_pct` | 0.10 | 相对我方入场均价的止盈；0=关闭 |
| `stop_loss_pct` | 0.20 | 相对入场均价的止损；0=关闭 |
| `max_market_usd` | 500 | 该地址在同一 condition 的成本上限 |
| `max_open_positions` | 300 | 该地址的最大同时持仓数 |
| `copy_sells` | true | 目标卖出时按比例跟卖 |

`min_hours_to_end > 0` 时，如果无法从 CLOB/Gamma 解析市场结束时间，信号会以
`expiry_unknown` 跳过（fail closed）。可用 `paper.risk_check_seconds` 调整止盈
止损检查间隔，`paper.orphan_grace_seconds` 给刚买入的仓位一个保护期，避免
data-api 持仓对账延迟触发误强平。

## 2. 实盘入场状态机

纸盘：维持原有立即 FAK/盘口模拟，`maker_entry_enabled` 在纸盘不生效。

实盘非 dry-run：

```
目标 BUY 信号
  ├─ gate：价格带 / 最小目标金额 / 距结算时间 / 现有仓位与敞口
  ├─ 固定金额 → 股数
  ├─ 写 pending_buys(SUBMITTING) + signals(maker_pending)
  ├─ 以目标成交价挂 GTC BUY（按 tick 向下取整）
  │    └─ 若价格已跨卖一：立即成交，按 taker 费率估算
  ├─ 有成交 → 先 apply 已成交部分
  └─ 剩余挂单 → PENDING（deadline = 现在 + maker_timeout_seconds）

主循环 trading_tick（约 1 秒）：
  PENDING
    ├─ 轮询订单，成交增量按 fills 表幂等入账
    ├─ 目标开始卖出/REDEEM/MERGE → CANCEL_REQUESTED，撤单，不补单
    ├─ 目标停用 / 市场关闭 / kill switch / 日亏上限 → 撤单，不补单
    └─ 到期（5 分钟）
         ├─ 撤单失败 → 保留重试，绝不并发 FAK
         ├─ 撤单确认后重新检查：距结算、目标是否仍持仓、风控
         └─ 在 +2% 价格上限内 FAK 剩余 → TAIL_SUBMITTED → DONE
```

关键安全规则：

- 每笔挂单/补单都是 `orders` 表的一行；崩溃后按 client order id 查询，
  **不会自动重发**。查不到就标 UNKNOWN 并提示人工检查。
- 撤单请求不等于撤单成功：必须通过 cancel 响应或 `get_order` 确认后才能 FAK。
- 成交增量按 `fills` 表按 `order_id` 汇总计算，重复轮询/崩溃恢复不会重复记账。
- 启动时先等链上 feed 首次 catch-up 与目标持仓对账成功，才允许超时补单；
  避免重启后拿旧信号补单。
- 如果目标在挂单期间卖出，未成交剩余立即撤销；已成交部分按跟卖比例处理。

## 3. 目标卖出与自己退出

- 目标卖出：沿用原有按目标卖出占其仓位比例跟卖；手动卖出形成的
  `manual_credit` 会先抵扣。目标清仓时强制卖出我方剩余。
- 目标未卖出：`take_profit_pct` / `stop_loss_pct` 用**中间价**判断，
  触发后立即 strict FAK 卖出（接受任意买一价），成交记为 `fills.kind='RISK'`，
  note 为 `take_profit` / `stop_loss`；流动性不足时进入 `pending_exits` 重试。
- 风控卖出前会撤销同 token 的未完成 maker 挂单，避免“先卖后挂单又买回来”。

## 4. 数据库与迁移

启动时如果检测到旧库的 `target_fills` / `orders` 仍是全局 dedupe 唯一键，
会自动迁移为按 target 作用域的唯一键，并在同目录生成一次备份：

```
data/paper_f705.db.pre-multitarget.bak
```

迁移是幂等的；完成后 `kv.schema:multi_target_keys=1`。**升级前建议手动再备份
一次 VPS 上的实盘库**。

新增表 `pending_buys` 只记录实盘 maker 挂单；纸盘为空。`pending_exits` 新增
`kind` 列（COPY/RISK）。

## 5. 上线顺序

1. 纸盘验证：更新配置（`sizing_mode=fixed`、`min_target_usd=1000`、
   `min_hours_to_end=2`、TP/SL），跑 `python -m polycopy onchain run`。
   纸盘会直接 FAK，但到期过滤、固定金额、止盈止损都生效。
2. 实盘 dry-run：`run.mode=live`、`live.dry_run=true`，重启，确认
   `signals` 出现 `dry_run`，不会创建 pending 挂单。
3. `python -m polycopy live check --sdk`，再关掉 dry-run，用
   `max_order_usd` 很小（例如 $2）做 canary。
4. 观察 `orders` / `pending_buys` / `onchain_trace.jsonl`：
   GTC 挂单状态、5 分钟撤单、FAK 补单价格、目标卖出撤单、TP/SL 成交。
5. 确认后再放大金额、增加地址。

## 6. 运维要点

```bash
# 配置页新增/删除地址后需要重启；页面会自动提交重启请求
# 手工检查链上/实盘订单
sqlite3 data/live_f705.db \
  "select id,target,side,status,limit_price,size,filled_size,client_order_id
   from orders order by id desc limit 20;"
sqlite3 data/live_f705.db \
  "select id,target,status,limit_price,shares,filled_shares,deadline_ts,reason
   from pending_buys order by id desc limit 20;"
```

- `kill switch` 文件存在时会撤销未完成的 maker 挂单，并阻止新买入；卖出不受影响。
- 有未完成实盘挂单时，配置页禁止切换到 paper/dry-run；请先在实盘处理挂单。
- 删除地址前要求该地址没有未平仓持仓、挂单、待执行卖出和实盘订单。

## 7. 已知限制

- 纸盘不模拟 maker 成交，纸盘结果不反映“挂单未成交 / +2% 补单”的成本。
- 目标账本每 60 秒从 data-api `/positions` 全量对账；快照可能短暂落后于链上，
  `orphan_grace_seconds` 用于保护刚买入的仓位。
- `target_fills` 的 dedupe key 仍不含 target；数据库已按 (target, dedupe_key)
  唯一，两个地址在同一 tx 出现完全相同的成交不会互相覆盖。
- 重启时若仍有 maker 挂单但进程没有 gateway（例如误切到 dry-run），会打
  critical 日志并拒绝自动处理，需要人工到 Polymarket 撤单。
