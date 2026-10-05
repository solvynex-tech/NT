# PortfolioPnLActor

## 输入与使用

单窗口支持同标的最多 20 组合、每组 32 腿，可混合到期日。数量为合约张数，`entry_premium` 为每股实际建仓权利金；支持标准乘数 100。GUI 可逐腿输入、导入候选、编辑单组合 JSON 或组合数组。

```json
{"name":"SPY Put Spread","legs":[
  {"symbol":"SPY","expiration":"2026-10-09","strike":750,"right":"put","side":"buy","quantity":1,"entry_premium":1.2},
  {"symbol":"SPY","expiration":"2026-10-09","strike":755,"right":"put","side":"sell","quantity":1,"entry_premium":2.8}
]}
```

示例只说明格式，请替换实际合约与成交价。

```python
from qwork.actors.portfolio_pnl import PortfolioRequest

state.configure_portfolio(PortfolioRequest(combinations=[combination_dict]))
```

## 输出

原生 DataActor 订阅曲面，异步估值后发布 `qwork.volatility.portfolio_pnl`。结果也在 `state.snapshot()['portfolio']`。

- 当前曲线：QuantLib Black 固定行权价 IV 情景，扣实际成本和预计双边佣金。
- 可平仓盈亏：多头 BID、空头 ASK，扣成本与佣金。
- 到期曲线：内在价值减成本和建仓佣金，横轴保留行权价拐点。
- 多组合分别绘图与汇总；缺报价保留到期图，缺失当前合计不显示、不记为零。

持仓到期与行权价自动加入查询，参数变化或行情更新自动重算。

## 假设

界面实时指快照轮询。欧式模型不处理提前行权与离散分红；到期图不含指派、行权与融资费用。多到期日采用各结算日相同标的价格的静态假设，不是价格路径。暂停或行情失败显示状态及报价时间。
