# BrokenWingButterflyActor

## 输入

`actor.py` 订阅曲面，`models.py` 的 `ButterflyScanRequest` 输入标的、到期日、最低模型概率、盈亏比、组数、费用和滑点。

```python
from qwork.actors.broken_wing import ButterflyScanRequest

state.configure_scan(
    ButterflyScanRequest(
        symbol="SPY", min_probability=0.70, target_ratio=1, ratio_mode="at_least", expirations=["2026-10-09"]
    )
)
```

## 筛选与输出

比较远期价两侧等距离 OTM Put / Call IV。Put 偏斜更大时加宽下方翼，Call 偏斜更大时加宽上方翼。同到期组合为买 1、卖 2、买 1，要求报价数量足够，并限制中心与翼宽。

买 ASK、卖 BID，计滑点和预计开平双边佣金。分段线性内在价值精确计算最大盈亏、平衡点与获利区间；QuantLib 风险中性对数正态分布用 ATM IV 估计获利概率。

发布 `qwork.volatility.broken_wing`；`state.snapshot()['scan']` 包含候选腿、方向、风险指标、报价时间与诊断。GUI 可选候选加入盈亏草稿，须复核建仓成本。

## 条件解释

Actor 默认概率严格大于 70%、盈亏比至少为 1；GUI 支持至少、接近、至多。交易 Strategy 另强制盈亏比严格大于 1。概率不是历史胜率；不外推两侧 IV、不自动放宽阈值，无符合组合返回空列表。此 Actor 不发送委托。
