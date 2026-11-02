# 清账后端：数据准备、匹配、制表与交付

当前已接通三块：A/B 接入生成 `PreparedData`，逐行匹配及人工复核生成 `MatchPlan`，再按 [第三块方案](../docs/实现方案/03-制表反向核查与交付.md) 生成 C、实际反向核查 B 并登记 `DeliveryResult`。正式交付仅在所有实际文件强制验收通过后开放；当前公式和显示能力有明确支持范围，超出范围保留诊断。

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
- 持久化真实执行时间线，记录解析、字段规则/模型调用、暂停、确认、标准化、底稿、索引及产物发布。

`ready` 仅表示第一块数据准备完成，不代表对账或最终核查完成。

## 在页面查看执行过程

前端已接入真实 `/api/preparation/tasks` 接口。启动后端（7546）及前端（`cd frontend; npm run dev`，4567）后，上传 A/B `.xlsx` 并开始数据准备。

- 页面约每 800ms 查询真实状态，按时间展示每个动作、数据版本、结果和暂停原因；断线保留已有记录并自动重连。
- 任务编号保存在网址和浏览器中。刷新页面会恢复最近任务，也可输入任务编号查看已有记录。
- 缺少月份、字段或公式结果时显示补充入口；确认人、说明及前后版本留存。执行失败可重试，中断可恢复。
- 完成后可下载 C 初始底稿、标准化数据、索引及审计；执行记录也可下载为 JSON。
- 当前入口不再回退为本地演示比对，不再用定时器模拟进度。

`GET /api/preparation/tasks/{task_id}` 的 `events` 包含 `sequence`、`occurred_at`（UTC）、`data_version`、`stage`、`kind`、`title`、`detail`，与状态一起原子保存到 `task.json`。记录在重试和确认后保留，ready 幂等重跑不追加记录。旧任务的历史为空，不追补未记录的动作。页面按北京时间显示。

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

工作簿和模型能力通过 `Protocol` 注入。第二块独立位于 `app/matching/`，直接消费 `PreparedData`，每条 A 经过 LangGraph 行工作流；匹配逻辑与上传接口、工作簿解析器分离。

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
uv run ruff check app/preparation app/matching app/delivery app/core app/factory.py app/api/v1/preparation.py app/api/v1/matching.py app/api/v1/delivery.py app/schema tests main.py
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
| `RECONCILIATION_LLM_PROVIDER` | `openai`（默认）或 `ollama`（本地原生接口，无需密钥） |
| `RECONCILIATION_LLM_API_KEY` | 对应模型接口密钥，不写入任务数据 |
| `RECONCILIATION_LLM_BASE_URL` | 兼容服务地址；Ollama 使用服务根地址，如 `http://127.0.0.1:11434` |
| `RECONCILIATION_LLM_TIMEOUT_SECONDS` | 单次模型调用超时，默认 180 秒 |
| `RECONCILIATION_LLM_CONTEXT_TOKENS` | 本地 Ollama 上下文长度，默认 16384 |

不配置模型时，已知模板可以直接准备，陌生表头进入人工字段确认；第二块严格匹配照常执行，语义疑难项保留人工入口。模型参与结构识别及疑难匹配的语义建议，数值计算、正式关系提交和文件操作仍由程序执行。离线回归不调用真实模型服务。

### 本地 Ollama

确保 Ollama 服务运行，使用 `ollama list` 查看已安装模型。在 `backend/.env` 中配置：

```dotenv
RECONCILIATION_LLM_PROVIDER=ollama
RECONCILIATION_LLM_MODEL=qwen3.5:9b
RECONCILIATION_LLM_BASE_URL=http://127.0.0.1:11434
RECONCILIATION_LLM_TIMEOUT_SECONDS=180
RECONCILIATION_LLM_CONTEXT_TOKENS=16384
```

`.env` 按后端目录定位，启动目录变化不影响加载。修改后重启后端；本机模型无需设置 API Key，也无需新增 Ollama Python 依赖。

规则无法明确的角色会交给本地模型，输入仅含该角色的表头、有限明细、尾行及公式证据，避免 A/B 中同名工作表混淆。通过 [Ollama 原生结构化输出](https://docs.ollama.com/capabilities/structured-outputs) 约束 JSON 结果，程序继续校验；格式或范围结构不合法时，模型有一次自动修正机会。失败或仍有业务歧义时不会伪造有效映射。

上传只需提供 A/B 两个文件。未指定月份时，程序从 A 表业务日期推断唯一年月；证据不足或 A 跨月时仍明确报告问题，不从当前日期猜测年份。只有汇总公式、没有日期及业务身份的末行自动作为结构行处理，支持 Excel 共享公式，避免合计再次计入业务明细。

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
- 准备阶段承接已有公式缓存或人工核实结果。第三块提供受限精确重算，不包含完整 Excel/WPS 计算引擎；未知公式、外部及结构化引用等记为未完成验证，不发布正式交付。
- 自动规则适配每张工作表一个业务区域。多候选表头或业务工作表需要明确范围；分段业务可在同一范围中排除结构行。
- 底稿处理清除静态单元格底色；条件格式和表格样式作为原始工作簿结构保留，最终状态颜色及显示验收在第三块统一处理。
- 初次执行使用 FastAPI 本进程后台任务，配合持久化检查点和显式 `/run` 恢复；独立任务队列和自动重启扫描可在部署层扩展。
- 前端已接入准备、匹配及交付三阶段；正式文件使用版本化 `/api/delivery/tasks` 接口，不回退本地演示报表。

## 第二块：匹配与人工复核

第一块完成后，在页面下方点击“开始逐行匹配”。页面提供候选及真实差额、A 行状态、B 使用情况、确认及释放、业务排除及恢复、日期和规格别名调整、预算续搜以及匹配方案下载。

核心代码为 `app/matching/models.py`、`search.py`、`workflow.py`、`validation.py`、`engine.py` 和 `service.py`。程序支持严格单行、非连续一对多、多对一以及独立部分状态。B 共享容量只能出现在统一组的显式分配中，候选本身不占用 B。来源以 Decimal 解析，计算使用精确有理数，不受 Decimal 默认 28 位上下文截断影响；循环小数分配保留问题并拒绝自动舍入。

接口前缀：`/api/matching/tasks/{task_id}`，沿用准备任务编号。

| 方法与后缀 | 功能 |
| --- | --- |
| `POST /run` | 启动或恢复，首次可提交 `MatchingRules` JSON；已发布状态幂等返回 |
| `GET`（无后缀） | 实际行进度、候选及模型输入输出追踪 |
| `GET /plan?expected_version=N` | 校验摘要、准备版本及匹配版本，返回第三块交接契约 |
| `GET /results` | 同一版本的行结果、匹配组、B 使用关系及复核事项 |
| `POST /confirm` | 人工选择、释放、排除、恢复或规则调整 |
| `POST /search?expected_version=N` | 从未完成游标继续组合搜索，产生新版本 |
| `GET /artifacts/{name}?expected_version=N` | 下载当前版本白名单产物，旧版本返回 409 |

首次省略规则时继承第一块的日期容差和已确认别名；未配置的数量、单价及金额容差均为十进制文本 `"0"`。即使配置数量容差，数量不等仍按 `partial` 保存剩余量。默认每行每轮预算 2,000 组合、100 候选、最大组合 6 行、模型 2 轮、追加查询 2 次；受限范围记录 `search_incomplete`。增大上限或调整数值口径需通过带审计的新规则版本。

人工确认示例：

```json
{
  "actor": "财务复核人",
  "reason": "已核实原始业务单据",
  "expected_match_plan_version": 1,
  "decision": {"action": "accept", "candidate_id": "从当前候选中取得的真实标识"}
}
```

`decision.action` 可用 `accept`（`candidate_id`）、`release`（`a_record_ids`）、`exclude_b / restore_b`（`b_record_ids`）或 `rules`（完整 `MatchingRules`）。排除原因使用请求顶层 `reason`，禁止空白原因。规则内容变化必须使用新的 `rules.version`。模型和人工建议都必须重新通过源值、方向和容量校验，不能修改或编造源值。

匹配状态保存到 `matching/state.json`，版本文件保存到 `matching/versions/{N}/`，包含 `match_plan.json`、`a_row_results.json`、`match_groups.json`、`b_usage.json`、`review_items.json`、`exclusions.json`、`execution_issues.json`、`confirmations.json` 和 `progress.json`。待执行行、原规则和行级别名查询各自保存游标；文件全部成功写入后才原子公开新方案。任务与第一块共用排他锁。

第三块通过 `MatchingService.plan(...)` / `artifacts(...)` 独立加载。`MatchPlan` 的 `ready` 表示可按真实状态制表，允许保留未匹配、部分和待复核项；必要数值缺失为 `needs_input`，工具失败为 `failed`。最终文件及强制验收由下述交付模块实现。

详细实现和验证范围见 [第二块方案](../docs/实现方案/02-逐行匹配与复核.md#7-已落地实现2026-11-02)。

## 第三块：制表、反向核查与交付

匹配完成后在页面第三阶段点击“生成文件并验收”。核心实现为 `app/delivery/`：`generate.py` 从完整底稿展开正式 B 来源，`mark_b.py` 根据真实 C 核查事实标注 B 副本，`verify.py` 独立重新读取实际文件，`service.py` 负责版本、受限修复、复验和正式/诊断下载门槛。

六项输出包括 C 对账工作簿、B 核查副本、JSON 与 Markdown 核查报告、待复核业务明细、生成版本说明。C 内另有独立来源、分配及候选复核表。多对一 B 原值只展示一次；部分余额归“已标注缺失”，统计原始 B 行数与方向记录数分别列出。

接口前缀：`/api/delivery/tasks/{task_id}`。

| 方法及后缀 | 功能 |
| --- | --- |
| `POST /run` | JSON `{"expected_match_plan_version":2}` 启动/恢复；`force:true` 创建新生成版本 |
| `GET`（无后缀） | 实际阶段、时间线、问题和版本 |
| `GET /result?expected_generation_version=N` | 同一版本的结果、统计和文件摘要 |
| `GET /report?expected_generation_version=N` | 实际文件的逐项强制验收报告 |
| `POST /verify?expected_generation_version=N` | 重新独立验收；损坏、丢失或修改会撤销正式交付资格 |
| `GET /artifacts/{name}?expected_generation_version=N` | 仅限验收通过的当前正式文件 |
| `GET /diagnostics/{name}?expected_generation_version=N` | 明确标记的诊断文件与待修复报告 |

可选 `options` 包含 `max_auto_repairs`（默认 1，0—3）、`formula_engine`（默认 `builtin`）和 `display_engine`（默认 `bounds`），后两项可显式设为 `unavailable` 验证门槛。代码不会用模型意见代替未执行的验收。

公式能力 `exact-formulas-v1` 支持有限十进制加减乘除、跨表普通引用、`SUM`、`VALUE`、`SUBTOTAL(9/109)`，展开共享公式并重新保存缓存。独立范围验收会发现旧范围漏业务及嵌套小计重复累计。未知函数、数组/数据表、外部和结构化引用等不能仅凭旧缓存通过。

显示能力 `ooxml-display-bounds-v1` 验证字体、行高/列宽、换行、General/整数/固定日期格式、顶部/活动页及冻结/筛选。它不是 Excel/WPS 实机渲染；超过一般 15 位数值显示精度、原生表格/绘图/扩展显示、未知字体或格式等保留源值并记 `not_run`，需扩展受支持的渲染/重算适配器后验收。不能通过舍入或删掉来源内容换取完成。

状态保存在 `delivery/state.json`，版本及尝试文件位于 `delivery/versions/{generation}/attempts/{attempt}/`，最终结果引用为该版本的 `result.json`。同一输入已完成重跑幂等返回；新匹配、明确强制重生成或已失败的重新执行会形成新生成版本。复验失败另存诊断报告，保留原版本记录。正式下载同时验证当前匹配/生成版本及实际 SHA-256。

`completed` / `completed_with_review` 均要求全部强制项通过；后者允许真实业务差异或待复核。任何 `fail/not_run` 只开放诊断，不登记正式完成。最近完整验证为 94 项测试、生产/测试严格类型、Ruff、前端构建及隔离页面回归通过。详细边界和测试见 [第三块实现说明](../docs/实现方案/03-制表反向核查与交付.md#8-已落地实现2026-11-02)。

LangGraph 用法依据：[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
