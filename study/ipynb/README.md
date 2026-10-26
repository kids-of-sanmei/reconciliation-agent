# LangGraph 人工介入示例

打开 `langgraph_human_in_the_loop.ipynb`，从上到下运行。示例使用模拟对账数据，不调用大模型或外部业务接口，无需 API Key。

## 运行方式

在本目录创建独立的学习环境（PowerShell）：

```powershell
uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.\.venv\Scripts\python.exe -m ipykernel install --user --name reconciliation-hitl --display-name "Python (reconciliation HITL)"
```

在 VS Code / Jupyter 中选择 `Python (reconciliation HITL)` 内核。若使用其他已有内核，可执行 Notebook 内提供的 `%pip` 安装命令，然后重启内核。

## 观察重点

1. 有差异时，`interrupt()` 返回待审批信息，执行节点尚未运行。
2. 修改“人工回复”单元格中的字典，再运行恢复单元格：使用相同 `thread_id` 和 `Command(resume=...)`。
3. 对比直接批准、修改后批准、拒绝、无差异自动结束四条路径。
4. 非法审批回复会再次暂停；不同任务使用不同 `thread_id`。
5. SQLite 示例先关闭连接，再重建图并恢复，演示检查点不依赖原图对象。

“全部运行”使用预设人工回复，便于复现；手动体验时请在回复单元格停下并修改内容。所有执行结果均为模拟数据。SQLite 检查点保存在系统临时目录，运行时会打印路径。

依赖版本与项目当前 LangGraph / SQLite 检查点版本一致。Notebook 的核心代码使用同步 `invoke()`，便于逐步观察。

官方资料：[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
