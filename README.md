# A 股量化分析看板

本地 Tornado 看板入口：<http://127.0.0.1:9989/>。在首页搜索股票代码或名称，或进入 [买点分析](http://127.0.0.1:9989/analyze)。分析页展示技术面、板块与资金、媒体新闻、九项检查结果，以及候选入场区、止损和止盈参考价；不自动下单。

## 运行环境

- Python 3.13 虚拟环境：`instock/.venv/bin/python`；依赖 Tornado、MySQL、pandas、TA-Lib、akshare。
- 看板由 macOS launchd 的 `com.myflicker.stock-web` 管理。更新代码后可运行：`launchctl kickstart -k gui/$(id -u)/com.myflicker.stock-web`。
- `scripts/scheduler.py` 的 `spot` 时段负责盘中喂表；Web 服务只做分析与展示，不重复写表。
- `config/config.yaml` 保存常规配置，`config/secrets.yaml` 是敏感文件：严禁提交到 git、严禁写入代码、严禁外传。

## 分析数据与限制

- 技术指标来自 `quant_hist_daily` 与核对后的当日 THS 单股报价，包含 MA10/20、MACD、RSI、量比及近 20 日高点。
- 报价需结合本地昨收和东财单股分钟 K 校验；板块与 THS 全单资金仅读现有缓存或当日喂表，东财主力资金与媒体新闻按单股拉取。来源缺失、失效或相互矛盾时不会推断为买入信号。
- 仅连续竞价时段且全部条件满足才可能显示「规则条件具备」。休市后所示价格只是下一交易时段的研究情景，不能当作当日挂单价。媒体关键词不等于公司正式公告，买入前须自行核对公告原文、账户权限、持仓上限和实际成交价格。
- `/analyze/status` 供冷缓存轮询，`/analyze/refresh` 供手动重新采集。服务会保留过期样本并显式标注刷新状态。

## 验证

```bash
PYTHONPATH=. ./instock/.venv/bin/python -m unittest discover -s tests -v
sh tests/browser/analyze.test.sh
```

浏览器操作与响应式检查记录见 `tests/browser/INDEX.md`。
