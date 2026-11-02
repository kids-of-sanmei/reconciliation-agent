# 提示词模块维护说明

本模块管理第一块当前使用的提示词。文案、变量装配、版本和模型调用分别维护。

| 文件 | 职责 |
| --- | --- |
| `templates/field_mapping_system.md` | 角色、识别规则、业务边界及输出要求 |
| `templates/field_mapping_user.md` | 输入说明及 `${profiles_json}` 占位符 |
| `field_mapping.py` | 提示词标识、版本、资源绑定及类型化输入构建器 |
| `base.py` | 包资源加载、变量校验、单次替换及内容摘要 |
| `../infrastructure/llm.py` | 模型连接、消息转换、调用及结果类型检查 |

## 修改现有文案

1. 编辑对应 Markdown，不在模型适配器或业务节点中重新写提示词。
2. 更新 `field_mapping.py` 中的 `FIELD_MAPPING_PROMPT.version`，使用 `major.minor.patch` 格式。
3. 新增或删除变量时，同步修改构建器提供的变量；漏传或多传都会产生明确配置错误。
4. 执行后端的类型检查、Ruff 和测试。当前测试验证变量契约、真实消息装配及结果协议，不把整段文案锁死成快照。

`${name}` 表示变量；需要字面量 `$` 时使用 `$$`。工作表 JSON 内的美元符号、花括号和类似占位符的内容只作为输入插入，不会进行第二次模板替换。

正文按调用读取，路径相对 Python 包定位，与启动目录无关。版本定义属于 Python 代码，修改后需要让服务加载新的代码版本。部署时需随应用一起带上 `templates` 目录；以后制作安装包时也需要将 Markdown 作为包资源包含。

## 版本与调用追踪

每次模型调用的运行名称为 `preparation.field_mapping`，调用 metadata 包含：

- `prompt_id`：稳定的业务提示词标识。
- `prompt_version`：本次使用的文案版本。
- `prompt_template_sha256`：标识、版本及实际模板文本的内容摘要。

摘要不包含业务输入，不同账单使用同一版文案时摘要相同；文案或版本变化后摘要变化。metadata 可供模型调用回调或链路追踪使用，目前不会额外写入任务的 `mapping_advice.json`。

历史文案通过 Git 版本管理；版本号本身不是运行时历史版本选择器。提示词和文件内容保持一致后，才能通过版本及摘要定位实际调用。

## 输出协议与新增提示词

结构化输出仍以 `domain/models.py` 的 `MappingAdvice` 和 `SheetMapping` 为准，由模型适配器提供 JSON Schema，不在 Markdown 中再维护一份手工 Schema。程序继续校验字段、范围及来源。

增加后续阶段提示词时，为该任务增加独立的系统/用户 Markdown 和类型化构建器；复用 `render_prompt`，使用新的 `prompt_id`。是否调用模型仍由业务流程决定，拆分文案不会改变默认启用条件。
