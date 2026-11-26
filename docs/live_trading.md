# 实盘接入说明

实盘代码已经接入，但默认仍是纸盘。模式在配置文件 `[run] mode` 中切换，
也可以在 Web「策略配置 → 运行模式」中修改；**模式或 `[live]` 设置变更后
必须重启 bot**，引擎不会在运行中热切换执行器。

## 1. 安装与账户准备

```bash
# 只装在运行 bot 的 VPS；纸盘和 Web 不需要
pip install -r requirements-live.txt
```

Polymarket 2026 年的 V2 协议使用 pUSD 抵押，订单/赎回由 SDK 处理。不要照抄
旧版 USDC.e 的授权教程；先用当前官方文档和 `polycopy live check` 验证。

需要准备：

- 一个独立的实盘热钱包（不要用主钱包），只放准备承担风险的资金。
- `funder`：实际持有资金/仓位的代理钱包地址。用 Polymarket 网页钱包时，
  签名钱包和 funder 通常不是同一个地址。
- 私钥只放在 VPS 的环境变量里，例如：

```bash
# /root/polycopy/.env.live  (chmod 600, 不要提交到 git)
export POLY_PRIVATE_KEY=0x...
```

- 网页登录的 Polymarket 账户：如果显示的是 **deposit address**（2026 年
  OKX/MetaMask 等插件登录常见），通常是 `signature_type=3`（POLY_1271
  deposit wallet），`funder` 填该 deposit 地址；旧版 Safe 代理用 2；
  Email/Magic 用 1；自己新建的 EOA 热钱包用 0。
- 首次使用先跑一笔 $1–2 的手工单，确认授权、tick size、neg-risk、费用和
  赎回都正常。

## 2. 配置项

```toml
[run]
mode = "paper"          # paper | live

[live]
dry_run = true          # true = 只记录订单意图，不提交
private_key_env = "POLY_PRIVATE_KEY"
funder = "0x..."
signature_type = 3        # deposit wallet；旧版 Safe 代理用 2
max_order_usd = 500.0
max_total_exposure_usd = 5000.0
max_daily_loss_usd = 500.0
min_order_shares = 5.0
order_timeout_seconds = 30
kill_switch_path = "data/LIVE_HALT"
redeem = true
max_book_depth_pct = 0.25
db_path = "data/live_f705.db"
log_path = "data/live_f705.log"
```

实盘买入额外受这些约束：目标配置的单笔/单市场上限、现金保留比例、盘口
深度占比、当日已实现亏损上限、总持仓成本上限。卖出（降低风险）不受
kill switch 和日亏损上限阻挡。

实盘的可用现金来自 CLOB 的 collateral balance（`LiveExecutor.available_cash`），
不再用内部纸面 cash 决定下单量；余额读不到时会直接停止买入。内部
`account.cash` 仍用于面板 PnL，首次接实盘时把 `[account] starting_cash`
设成当时的真实余额，或接受面板 PnL 与钱包余额存在偏差。

## 3. 上线顺序

1. 保持 `run.mode = "paper"`，继续跑纸盘。
2. 设置 `run.mode = "live"`、`live.dry_run = true`，重启。此时只记录
   `orders` 意图和风控结果，不提交任何订单。
3. 跑 `polycopy live check --sdk` 检查配置、私钥环境变量、kill switch，
   并实际导入 SDK、派生 API 凭证（不会下单）。
4. 关掉 `dry_run`，用最小金额（例如 `max_order_usd = 5`）重启；观察
   `orders` 表和日志里的 `COPY BUY ... mode=live order=...`。
5. 确认成交、费用、持仓和赎回与交易所一致后，再逐步放大。

## 4. 订单安全设计

- 每笔实盘订单先写 `orders` 表（`INTENT`）并 commit，再发网络请求。
- 签名后、提交前记录 `client_order_id`（订单 hash）；重启对账时按该 id
  查询交易所，**不会自动重发**。查不到就标 `UNKNOWN` 并要求人工检查。
- 入场默认先挂 GTC maker（价格 = 目标成交价），超时未成交再撤单并用 FAK
  补剩余（价格上限 = 目标价 × 1.02）；`maker_entry_enabled=false` 或纸盘时
  仍直接 FAK。目标卖出、REDEEM/MERGE、kill switch、市场关闭会撤销未完成挂单。
- 卖单：strict exit 使用 FAK 限价 0.001（接受任意买一价），成交不了的剩余
  由 `pending_exits` 重试。止盈止损同样走 strict FAK，但标记 `kind=RISK`。
- 多地址、maker 状态机、止盈止损的完整说明见
  [multi_target_copy.md](multi_target_copy.md)。
- `fills.order_id` 关联到订单，便于逐笔核对。
- `live check` 不提交订单；`kill switch` 触发后只停止买入。

## 5. 持仓页手动卖出

「持仓 → 未平仓」每行有「卖出」按钮，弹框可选 25% / 50% / 100%：

- 请求写入 `manual_sells` 表，状态 `PENDING`；Web 容器不需要私钥。
- bot 在下一次维护循环通过当前执行器（纸盘走盘口模拟，实盘走 FAK）按当前
  盘口市价卖出；部分成交会保留 `PARTIAL` 并在冷却后继续卖。
- 手动卖出的份额会记入 `positions.manual_credit`，同时保留
  `basis_shares`。后续目标卖出时先消耗这笔 credit，再按比例卖我们的
  仓位；目标完全清仓时强制卖出剩余全部份额，不会留下手动卖出造成的残仓。
- 实盘低于最小下单份额（默认 5 股）的仓位按钮会禁用，避免提交必被拒绝的单。

## 6. 运维

```bash
# 纸盘
scripts/onchain_ctl.sh restart

# 实盘：显式标记，避免误用纸盘 DB；日志/DB 默认取 [live] 配置
POLY_LIVE=1 scripts/onchain_ctl.sh restart

# 紧急停止新买入（卖出仍允许）
# kill_switch_path 相对 bot 的工作目录；onchain_ctl 下是 /root/polycopy-onchain
ssh root@<vps> 'touch /root/polycopy-onchain/data/LIVE_HALT'

# 解除
ssh root@<vps> 'rm /root/polycopy-onchain/data/LIVE_HALT'

# 查看订单状态
sqlite3 data/paper_f705.db \
  "select id,side,status,size,filled_size,avg_price,token_id,created_at
   from orders order by id desc limit 20;"
```

实盘和纸盘务必使用不同的数据库与配置（例如 `data/live_f705.db` 和
`configs/live_f705.toml`），不要共用纸盘的账户账本。Web 面板通过
`POLY_DB_PATH` / `POLY_CONFIG_PATH` 决定看哪一个；切到实盘后把这两个
环境变量指向实盘库和实盘配置（或单独起一个面板实例）。

## 7. 已知边界

- 多地址配置、maker 挂单、止盈止损见
  [multi_target_copy.md](multi_target_copy.md)。新增/删除地址后必须重启，
  因为链上订阅的 wallet 集合在进程启动时固定。

- SDK 的具体版本会随 Protocol V2 演进；升级后必须重新跑 `live check` 和
  一笔最小实盘单。
- `redeem_positions` 依赖 SDK 支持；如果不可用，结算会保留为可领取，
  不会伪造现金。
- 回测/纸盘的滑点模型不能替代真实盘口；先用 `max_book_depth_pct` 和
  小额 canary 验证成交质量。
