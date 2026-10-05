# VolatilitySurfaceActor

## 输入

`VolatilityRequest` 输入标的、到期日 / DTE 范围、到期数量、刷新频率、报价年龄和价差限制。历史模式须提供 `as_of`、纽约时间 `at`。GUI 可设置现价、利率、分红、Call / Put 和 IV 方法。周期按到期日划分。

```python
from qwork.runtime.actors import VolatilityRuntime
from qwork.core.config import Settings
from qwork.core.state import VolatilityRequest

runtime = VolatilityRuntime(Settings.load(), VolatilityRequest(symbol="SPY"))
runtime.start()
# runtime.state.snapshot()["surface"] 读取结果；退出调用 runtime.stop()
```

## 输出与实现

`actor.py` 使用原生 DataActor、Clock、消息总线与采集线程池。发布 `qwork.volatility.surface`，观察点包含到期日、剩余天数、K、方向、IV、远期价、BID / ASK / 数量、时间和方法。参数代数防止旧请求覆盖新结果。

供应商 Greek IV 不可用时用 QuantLib 反算中价。剔除失效报价，不向缺失行权价外推。现价未填写时用同步 Call / Put 平价估计远期价。优先查询持仓所需到期日和行权价。

## 运行与限制

项目目录执行 `.venv/Scripts/python.exe -m qwork dashboard`；Dash 通过 pywebview 显示，默认 15 秒刷新。快照可能延迟，休市可显式选择历史模式。Black IV 是欧式近似，使用纽约 16:00 PM 到期；合成样本只在明确选择 `fixture` 时生成。
