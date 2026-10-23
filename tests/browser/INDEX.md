# Browser regression index

| 用例 | 覆盖页面 | 脚本 |
|---|---|---|
| 买点分析入口、代码搜索、数据加载、XSS 转义、状态接口（本地 HTTP 冒烟） | `/`、`/analyze` | `analyze.test.sh` |

交互/响应式补充验证：`myflicker-browser new_tab --url http://127.0.0.1:9989/analyze` → `snapshot` → 输入六位股票代码提交 → `wait_for --selector .analysis-verdict` → `console_messages` / `network_requests`；检查九项依据、消息链接、625px 宽度无溢出。
