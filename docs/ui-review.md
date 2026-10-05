# 期权交易终端交互评估

评估日期：2026-10-05。对象为当前运行的 Dash / pywebview 终端。

## 方法与结论

使用 Win32 `PrintWindow` 获取真实桌面窗口截图，按 Windows 192 DPI（200%）正确采集；使用 Edge / Playwright 实际操作期权链、组合篮子、账户、策略和回放工作区，检查 1680×1050、1366×800、1120×800 三种尺寸。另外建立一个使用应用默认设置的独立 WebView2 窗口，观察真实下载事件。没有连接券商、启动策略或提交交易委托。

目前更接近可用的期权研究终端。距离专业交易软件的主要差距是数据时效与结果对应关系、组合编辑状态、期权风险表达及关键流程的空间分配。继续调整配色的收益小于修复这些问题。

本轮只生成评估文档、脚本及证据，没有修改应用行为。

## 后续修复记录

2026-10-05 后续实施：已修复行情来源版本与时间标注、实时分析过期校验、桌面下载、草稿持久化及未应用提示、就地选腿反馈和期权链面板溢出。默认增加链 / 风险并排预设，Paper / LIVE 打开更高的 EA 区；MT5 和自定义停靠布局保留。风险区增加同日到期精确盈亏边界、盈亏平衡点与无限风险提示，混合到期日限制置于图表上方。

完整 unittest 为 119 项；浏览器验证三个尺寸下链面板溢出为 0，草稿编辑 / 刷新恢复 / 清空、未应用状态及连续导出通过。桌面真实 WebView2 下载写入 CSV 和取消反馈通过，保存对话框选择由测试自动化代选。

新证据：`data/verification/options-desk-browser.json`、`desktop-options-export.json`、`desktop-options-cancel.json`、`tests-ui-improvements.log`；新截图 `options-desk-1366.png`、`options-desk-paper.png`。

新版桌面已实际启动并截图：[桌面界面](../data/verification/options-desktop-final.png)。原有 PUT 合约 / K 线联动、候选原生 Actor 分析、回测参数独立及 LIVE 历史数据隔离流程也复跑通过，JavaScript / HTTP 错误均为零。当前券商未连接、策略未启动。

以下正文保留为修复前的审查记录。组合 Greeks、IV / 时间压力情景、多窗口后端任务隔离、真实券商故障恢复仍需继续完善，当前修复不等同实盘验收。

## 优先处理的问题

### P1：行情时间与分析结果没有严格对应

**隔离复现，真实代码路径。** `DashboardState.publish_portfolio()` 按配置 generation 接受结果，没有校验产生结果的曲面版本；更新曲面后仍能接受旧结果。`analysis_render()` 用当前 `frame.observed_at` 标注组合盈亏，而非组合结果自己的时间。

复现：旧估值来自 09:45，新曲面为 10:00，界面摘要仍显示旧估值 42 USD，却标注 10:00。该数值来自明确标记的测试输入，不是市场测量。

建议：为每个行情批次设置不可混用的版本，组合盈亏、候选与期限结构携带来源版本；展示结果自己的报价时间，并明确显示“等待重算”。

代码：[state.py](../src/qwork/core/state.py)、[app.py](../src/qwork/ui/terminal/app.py)。证据：[review-data.json](../data/verification/review-data.json)。

### P1：实时分析入口缺少独立的新鲜度检查

**隔离复现，真实代码路径。** `chain_frame()` 和 `analyze_combinations()` 主要依据 `mode == "live"` 放行，没有核验快照年龄、采集错误或暂停状态。带旧时间的 live 快照仍可展示、送入分析。底栏“服务在线”只说明本地服务响应。

当前实界面正确标注历史行情；此问题针对实时行情中断后的状态处理，不代表已经发现过期行情实盘下单。

建议：分开展示服务、数据源、报价年龄和券商状态；过期时保留只读历史结果，阻止其作为有效实时分析输入。

代码：[workflow.py](../src/qwork/ui/terminal/workflow.py)、[backend.py](../src/qwork/ui/terminal/backend.py)。证据同上。

### P1：桌面导出被取消

**原生 WebView2 实际复现。** CSV 按钮启用，点击后产生 `DownloadStarting`，但 `Cancel == True`。应用没有开启 pywebview 的 `ALLOW_DOWNLOADS`，安装版本默认值为 False。此前浏览器下载测试不能覆盖桌面宿主行为。

建议：启用桌面下载，提供保存成功、取消和失败反馈；分别验证期权链与成交导出。

代码：[desktop.py](../src/qwork/ui/terminal/desktop.py)。证据：[review-desktop-export.json](../data/verification/review-desktop-export.json)。

### P2：组合草稿丢失，修改与已分析状态不清

**浏览器实际复现。** 加入一条有效期权腿、修改数量为 2 后刷新，草稿由 1 条变为 0 条。编辑数量后状态提示仍是原来的加入提示；没有未应用标记。代码中盈亏分析读取已提交请求，编辑草稿不会自动重算。

建议：持久化草稿；区分“草稿已修改”“正在分析”“已应用版本”；显示已应用数量与报价时间。避免让用户把编辑后的草稿与上一版图表对应起来。

证据：[组合编辑截图](../data/verification/review-builder-1366.png)、[review-browser.json](../data/verification/review-browser.json)。

### P2：选腿反馈藏在另一个页签

**浏览器实际复现。** 在期权链加入有效腿后，成功提示位于不可见的组合风险页签。点击空白 Call 报价再加入，失败提示同样不可见，而且只显示一段通用校验说明。链工具栏没有篮子数量或所选合约确认。

建议：链上直接展示所选合约、买卖方向、预填报价与篮子数量；缺少有效报价时禁用加入操作，并就地说明原因。

### P2：期权链横向滚动条落在面板首屏之外

**测量与截图复现。** 16 列最小总宽 1520 px；1366×800 时实际表格宽约 1086 px。面板底部在 y=609，横向滚动条在 y=659，需要先向下滚动面板再横向滚动。工具栏换行后，固定 `calc(100% - 96px)` 没有适应其实际高度。1680 宽也存在 74 px 面板溢出。

建议：以 flex 布局让表格占剩余空间，避免双层纵向滚动；固定行权价，默认只展示核心报价，将时间、报价量和价差放入可选列或明细。

代码：[app.py](../src/qwork/ui/terminal/app.py)、[terminal.css](../src/qwork/ui/terminal/assets/terminal.css)。证据：[1366 期权链](../data/verification/review-chain-1366.png)、[review-workspaces.json](../data/verification/review-workspaces.json)。

### P2：默认布局没有围绕期权交易流程分配空间

**截图确认。** 链与盈亏图处于同一页签组，选腿和看风险需要反复切换。1366×800 时展开组合编辑后，盈亏图在首屏之外。Paper 主区仍显示不可用的历史 K 线空图，账户、预算和 EA 操作挤在底部；回放报告的英文参数串也占据有限空间。

建议：保留 MT5 的停靠能力，增加期权专用预设：链在左侧，组合篮子与风险图在右侧，账户及执行状态在底部；交易工作区以有效行情、组合风险和 EA 控制为主。运行参数改成易读摘要。

证据：[Paper 截图](../data/verification/review-paper-1366.png)、[回放截图](../data/verification/review-replay-1366.png)、[实际桌面截图](../data/verification/review-desktop.png)。

## 期权功能与架构缺口

- 任意多腿分析没有集中显示组合 Greeks、盈亏平衡点、无限风险识别及 IV / 时间压力情景。有限横轴上的盈亏图不足以表达裸卖尾部风险。
- 混合到期日的“到期盈亏”是假设各到期日标的价格相同的静态情景，代码已有诊断说明，但说明位于图表下方。应提升为显眼的模型标识，并提供估值日期选择。
- 自定义组合模型只支持乘数 100；不能泛化到所有调整合约。原生账户持仓读取实际乘数与该分析限制应明确区分。
- 多窗口共用同一个后端图表、分析运行时和回放状态。布局与草稿分开保存，并不代表后端任务独立；需明确全局任务归属，或隔离视图选择和分析请求。

依据：[portfolio.py](../src/qwork/analytics/portfolio.py)、[组合模型](../src/qwork/actors/portfolio_pnl/models.py)、[backend.py](../src/qwork/ui/terminal/backend.py)。这些是代码确认的限制，不是本轮实盘测试结论。

## 验证边界与执行顺序

交互过程未捕获 JavaScript 异常，刷新后旧草稿不再存在。真实桌面已截图、独立桌面下载路径已验证；没有覆盖所有 GPU 的残影、多日连续运行、券商成交与断线恢复。部分数据问题用隔离输入复现，不能替代实盘压力测试。

建议先修行情版本与时效、桌面导出、草稿保存及未应用状态，再修面板尺寸和期权布局，随后补组合风险表达与多窗口任务归属。

持续维护的复现脚本为 `tests/terminal_review_smoke.py`、`tests/terminal_workflow_smoke.py` 和 `tests/terminal_desktop_export_smoke.py`。关键结论与截图保留在 `data/verification/`；一次性排查脚本已清理。
