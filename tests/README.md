# 测试说明

## 环境与默认回归

使用项目已有的 uv 环境。与发布流程一致，自动检查目前使用 Windows / Python 3.12。

```bash
uv sync --frozen --extra desktop
uv run --no-sync pytest -q
```

默认回归使用临时知识库及确定性嵌入替身，包括真实 SQLite、HTTP/MCP、Worker 与 CLI 集成。浏览器和联网安装模块默认不收集，23 项规模测试默认跳过；不能把这些未执行范围计为通过。标记与发现范围统一配置在 `pyproject.toml`，公共入口和资源隔离在 `tests/conftest.py`。

## 其他验收范围

```bash
# 真实浏览器：安装 Python 绑定，复用已安装的 Windows Edge。
uv sync --frozen --extra desktop --group browser
uv run --no-sync pytest --run-browser -m browser -q

# 大数据量分页、图谱和 Wiki 性能回归。
uv run --no-sync pytest --run-slow -m slow -q

# 联网构建 wheel，并在仓库外创建精简与完整安装环境。
uv run --no-sync pytest --run-external -m external -q
```

显式浏览器验收缺少 Playwright 或 Edge 时应报错，不能静默跳过。当前浏览器 fixture 使用 Windows Edge；其他平台尚无浏览器验收承诺。安装测试明确使用当前测试解释器创建临时 venv，并按 `uv.lock` 导出的约束安装 wheel，验证已支持依赖组合的分发合同，不使用 uv 的隐式默认 Python，也不评估最新传递依赖组合。安装输出保存在临时目录，失败或超时会附带日志。

单文件调试也保留对应开关，例如：

```bash
uv run --no-sync pytest --run-browser tests/automated/test_upload_browser.py -q
uv run --no-sync pytest tests/test_office_convert.py -q
```

`test_vlm_client.py` 与 `test_mineru_client.py` 是需要输入文档和真实服务配置的手工验证脚本，没有 pytest 用例；不要按文件名把它们计入自动覆盖。

## 分支覆盖率

```bash
uv run --no-sync coverage erase
uv run --no-sync coverage run -m pytest -q
uv run --no-sync coverage report --skip-covered
uv run --no-sync coverage xml
uv run --no-sync coverage json
uv run --no-sync coverage html
```

基线测量 pytest 主进程中的 `app`、`indexing`、`retrieval` 分支覆盖。真实 CLI、解析器和服务子进程仍参与功能验收，但不自动插桩，以保持其导入、超时和退出边界；报告不代表这些子进程的覆盖率。HTML、XML、JSON 写入 `.test-results/`，原始数据单独写入 `.test-results/coverage-data/`，避免新版 coverage 自动合并其他采集历史；这些文件均不入库。每次新采集先 erase，避免混入旧运行。覆盖率只说明执行情况，不能替代行为断言或真实模型的检索质量评估。

## 新增或维护测试

- 解析器调用显式使用 `parser_helpers` fixture；需要数据库的离线测试复用 `knowledge_base`。不要依赖其他测试留下的解析器状态、限流额度或异步信号量。
- 只替换模型、远端服务等边界；业务写入、事务、任务状态和读回尽量走真实实现。
- 并发与退出测试先用事件确认目标状态已经发生，再触发取消或停机；时间戳边界使用固定时钟，避免用短 sleep 猜测时序。
- 相同层级的同一行为保留一个完整合同；跨 CLI、HTTP、MCP 或浏览器的入口差异另测。
- 浏览器成功路径应检查新数据实际写入并能读回，不能只断言按钮存在或通知出现。

## 覆盖率基线

2026-11-18，当前工作区，Windows / Python 3.12.9 / coverage 7.17.3。以下为隔离数据目录中的测试主进程覆盖率，不混入历史采集或真实子进程的数据。综合列按语句和分支的执行计数加权。

| 范围 | 语句 | 分支 | 综合 |
| --- | ---: | ---: | ---: |
| app、indexing、retrieval 全部代码 | 79.57% | 68.32% | 76.77% |
| 下列八个核心文件合并 | 90.40% | 83.40% | 88.59% |
| app/runtime.py | 87.50% | 63.64% | 84.74% |
| indexing/services/task_service.py | 95.15% | 75.00% | 91.87% |
| indexing/services/chunk_service.py | 86.84% | 70.31% | 83.22% |
| indexing/services/processor.py | 86.24% | 72.37% | 82.82% |
| indexing/services/config_service.py | 80.00% | 76.14% | 78.90% |
| indexing/mcp/auth.py | 92.98% | 100.00% | 94.03% |
| indexing/services/knowledge_service.py | 95.83% | 91.54% | 94.66% |
| indexing/services/wiki_service.py | 91.59% | 87.36% | 90.34% |

CI 要求这八个核心文件合并后的综合覆盖率至少 85%，保留少量环境差异余量；不是逐文件门槛。新增行为仍须有对应结果断言，不以覆盖率达标替代正确性验证。

本次分组验收：离线回归 1,542 项通过；默认跳过的 23 项规模测试单独全部通过；浏览器 30 项通过；仓库外安装 6 项通过。四份 JUnit 报告中没有重叠的通过用例，共 1,601 项。

## CI

`.github/workflows/tests.yml` 在 main 推送和 PR 时分别执行离线覆盖率回归与全部浏览器验收。每周一及手动选择扩展验收时，再执行规模与联网安装测试。结果、覆盖率报告、浏览器日志和截图由 Actions 保存 7 天。Release 工作流复用默认离线回归入口。
