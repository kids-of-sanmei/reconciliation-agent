# 清账后端：第一块数据接入与底稿准备

当前实现对应 [第一块方案](../docs/实现方案/01-数据接入与底稿准备.md)，从接收 A/B 表到生成 `PreparedData` 和 C 表初始底稿。后续匹配、最终 C 表、B 表反向核查及交付验收由第二、第三块扩展。

## 已实现能力

- 上传 A/B 原始 `.xlsx`，可附人工调整参考表，保存原文件、文件身份和 SHA-256。
- 读取工作表、原始单元格、公式缓存、合并范围、隐藏行列、数字格式及尺寸信息。
- 按已知表头识别字段；陌生结构可调用配置的模型，所有建议继续经过程序验证。
- 字段、月份或必要公式结果不明确时，通过 LangGraph `interrupt` 等待补充，并以 `Command` 续跑。
- A 明细按原顺序标准化；日期只在原合并区域内补全，完整保留原始值及来源位置。
- 使用 `Decimal` 计算数量和金额合计，结构化产物中的数值采用十进制字符串。
- B 入库/出库分方向保存，零数量、零金额、跨月及重复内容的原始行均保留。
- 完整复制 A 工作簿创建 C 底稿，保留未加工工作簿部件、附件表、字体及边框；解除选定工作表合并、清理单元格旧底色、调整数值显示。
- 持久化 B 查询索引、来源快照、清理审计、人员确认时间和数据版本。
- SQLite 检查点及任务级跨进程文件锁支持服务重启恢复、幂等重放和排他执行。

`ready` 仅表示第一块数据准备完成，不代表对账或最终核查完成。

## 项目结构

```text
backend/
  main.py                         启动入口
  app/
    factory.py                    应用依赖组装和错误响应
    core/config.py                类型化环境配置
    api/v1/preparation.py         上传、查询、确认、下载接口
    schema/preparation.py         HTTP JSON 与严格领域类型的边界适配
    preparation/
      domain/
        models.py                 不可变 Pydantic 数据契约及枚举
        errors.py                 稳定业务错误
        values.py                 来源值读取
      ports.py                    工作簿适配器、模型建议接口
      application/
        mapping.py                结构摘要、表头规则、字段映射校验
        normalization.py          精确数据标准化及 B 索引
        tasks.py                  显式类型化的状态更新
        steps.py                  各阶段业务用例
        service.py                任务接入、执行、续跑及产物访问
      workflow/graph.py           LangGraph 状态、节点与路由
      prompts/
        base.py                   文案加载、变量契约、版本摘要
        field_mapping.py          字段识别提示词定义和输入构建
        templates/                独立维护的系统/用户 Markdown 文案
      infrastructure/
        storage.py                原子文件持久化及排他锁
        xlsx.py                   OOXML 读取和底稿加工
        llm.py                    可选结构化模型适配器
  tests/                          业务、HTTP 及代码类型/注释契约回归
```

领域层不依赖框架。工作簿和模型能力通过 `Protocol` 注入。增加第二块时直接消费 `PreparedData`，新增流程节点或子图，不把匹配逻辑塞进上传接口或工作簿解析器。

## 启动与检查

在 `backend` 目录运行，Python 版本要求 3.13 或更高：

```powershell
uv sync --group dev
uv run uvicorn main:app --host 127.0.0.1 --port 7546
```

接口文档：[Swagger UI](http://localhost:7546/docs)。根接口和 `/demo/` 继续保留。

```powershell
uv run mypy
uv run mypy tests
uv run ruff check app/preparation app/core app/factory.py app/api/v1/preparation.py app/schema/preparation.py tests main.py
uv run pytest -q
```

已开启 `mypy strict` 和不可达代码检查。新生产函数的参数、返回值必须有类型注解；回归检查还会验证参数说明、返回说明和函数文档存在。工作簿测试使用独立的 openpyxl 库生成和重读输入输出，生产文件处理不经过二进制浮点重写。

## 配置

可复制 `.env.example` 为 `.env`。默认存储目录为 `backend/.data`，已加入 Git 忽略。

| 环境变量 | 作用 |
| --- | --- |
| `RECONCILIATION_STORAGE_ROOT` | 任务持久化根目录，建议使用绝对路径 |
| `RECONCILIATION_MAX_UPLOAD_BYTES` | 每个输入文件大小上限，默认 20 MB |
| `RECONCILIATION_MAX_ARCHIVE_BYTES` | 单工作簿解压后的部件总量上限，默认 200 MB |
| `RECONCILIATION_MAX_CELLS` | 单工作簿显式单元格上限，默认 500,000 |
| `RECONCILIATION_LLM_MODEL` | 可选的实际模型名称 |
| `RECONCILIATION_LLM_API_KEY` | 对应模型接口密钥，不写入任务数据 |
| `RECONCILIATION_LLM_BASE_URL` | 可选兼容接口地址 |

不配置模型时，已知模板可以直接准备，陌生表头进入人工字段确认。模型仅参与结构识别，不参与金额计算和文件改写。离线回归不调用真实模型服务。

## 提示词维护

提示词文案位于 [系统提示词](app/preparation/prompts/templates/field_mapping_system.md) 和 [用户提示词](app/preparation/prompts/templates/field_mapping_user.md)，版本及输入装配位于 [field_mapping.py](app/preparation/prompts/field_mapping.py)。模型适配器只负责消息转换与调用。

详细编辑规则、变量约定、版本追踪和扩展方式见 [提示词模块说明](app/preparation/prompts/README.md)。本次拆分保留“规则优先、必要时模型辅助”的调用条件。

## HTTP 接口

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/preparation/tasks` | multipart 接收文件，返回 202 和任务编号，后台开始准备 |
| `GET /api/preparation/tasks/{task_id}` | 查询真实状态、问题、配置和可下载产物 |
| `GET /api/preparation/tasks/{task_id}/profiles` | 获取辅助字段确认的表结构摘要 |
| `POST /api/preparation/tasks/{task_id}/confirm` | 补充配置/公式结果，从检查点续跑 |
| `POST /api/preparation/tasks/{task_id}/run` | 恢复进程中断或重试失败任务；ready 时幂等返回 |
| `GET /api/preparation/tasks/{task_id}/prepared` | 获取下一块使用的 `PreparedData` |
| `GET /api/preparation/tasks/{task_id}/artifacts/{name}` | 下载清单内的已就绪产物 |

上传字段为 `file_a`、`file_b`、可选 `reference_file`，以及 `TaskOptions` JSON 字符串 `options`。

PowerShell 7 上传示例，文件路径替换为实际输入：

```powershell
$form = @{
    file_a = Get-Item -LiteralPath '.\A.xlsx'
    file_b = Get-Item -LiteralPath '.\B.xlsx'
    options = '{"reconciliation_period":"2026-07"}'
}
$task = Invoke-RestMethod -Method Post -Uri 'http://localhost:7546/api/preparation/tasks' -Form $form
Invoke-RestMethod -Uri "http://localhost:7546/api/preparation/tasks/$($task.task_id)"
```

任务缺少月份时，可向 `confirm` 提交以下 JSON；`expected_data_version` 建议取最近查询到的版本，过期会返回 409：

```json
{
  "actor": "财务复核人",
  "reason": "已确认本次对账月份",
  "expected_data_version": 1,
  "options": {
    "reconciliation_period": "2026-07"
  }
}
```

`options` 是完整替换配置。人工提供字段映射时，应保留任务查询结果中已确认的其他映射和规则。每项映射包括 `role`、`sheet_name`、`header_row`、`data_start_row`、`data_end_row`、`fields` 和可选 `excluded_rows`；字段列使用 Excel 字母。`excluded_rows` 仅用于结构行，不能用来无说明丢弃业务记录。

公式缓存缺失时，使用问题中的 `source_ref` 作为 `formula_values` 键：

```json
{
  "actor": "财务复核人",
  "reason": "已在来源工作簿核实公式结果",
  "expected_data_version": 1,
  "formula_values": {
    "file_a:1:E3": "7285.659612"
  }
}
```

补充值仅允许指向真实的来源公式单元格，不接受 NaN、Infinity 或来源不存在的单元格。确认内容、时间及前后版本随数据包保存。

## 阶段产物与后续交接

```text
.data/tasks/{task_id}/
  task.json
  checkpoints.sqlite
  sources/
    input_a.xlsx
    input_b.xlsx
    input_reference.xlsx          存在人工参考输入时保存
    snapshot.json
    profiles.json
  versions/{data_version}/
    prepared.json
    a_records.json
    b_records.json
    b_index.json
    c_baseline.xlsx
    audit.json
    mapping_advice.json           调用模型时保存
```

`PreparedData` 中的引用携带相对路径和 SHA-256；所有引用绑定同一任务和数据版本。A 记录使用原始工作表和行号，B 记录额外按 `in/out/unknown` 区分方向。索引保留记录标识列表，不以集合去重业务行。

`a_baseline_totals` 由原始业务明细重新累计，不重复算入小计、合计和后续附加行。原始公式和缓存仍保存在快照，底稿仅对必要 A 数值明细承接已核实结果。

## 当前能力边界

- 原生支持 `.xlsx`。`.xls` 和 CSV 会明确返回 `UNSUPPORTED_FORMAT`；后续可通过工作簿适配器扩展，不静默转换并丢失底稿版式。
- 不内置 Excel 公式重算引擎；已有缓存按来源结果承接，缺缓存时等待重算后的新输入或人工核实结果。
- 自动规则适配每张工作表一个业务区域。多候选表头或业务工作表需要明确范围；分段业务可在同一范围中排除结构行。
- 底稿处理清除静态单元格底色；条件格式和表格样式作为原始工作簿结构保留，最终状态颜色及显示验收在第三块统一处理。
- 初次执行使用 FastAPI 本进程后台任务，配合持久化检查点和显式 `/run` 恢复；独立任务队列和自动重启扫描可在部署层扩展。
- 尚未接入前端现有演示对账入口，也不提供最终 `/api/reconcile` 报表；第一块产物供后续模块使用。

LangGraph 用法依据：[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
