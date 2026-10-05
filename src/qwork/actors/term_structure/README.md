# TermStructureActor

## 输入与注册

原生 DataActor 接收同标的曲面，订阅 `qwork.volatility.surface`。与曲面共用参数与时间基准，不单独取第二份行情。

```python
from qwork.actors.term_structure.actor import TermStructureActor

node.add_actor(TermStructureActor(state))  # 在曲面生产者之前注册
```

## 输出与计算

发布 `qwork.volatility.term_structure`，状态结果在 `state.snapshot()['term']`。每个到期日分 Call / Put 输出 ATM 远期 IV、总方差、远期价、剩余天数、插值行权价、报价时间和方法。

仅在远期价被观察行权价包围时计算；采用 log(K/F) 上的线性总方差插值，不向无报价区外推。后续期限总方差下降时标记警告，保持观察结果。

## 使用与验证

运行 `.venv/Scripts/python.exe -m qwork dashboard --symbol SPY`。图表可选择到期日 / 剩余天数横轴，黄色点提示总方差下降。金融测试覆盖不外推和期限异常，原生测试覆盖消息总线。期限结构不是未来 IV 预测。
