# Excel 上传与表头结构

```powershell
cd backend
uv sync
uv run uvicorn main:app --host 127.0.0.1 --port 7546
```

`POST /api/upload` 使用 `multipart/form-data`，文件字段为 `file`，支持 `.xlsx`、`.xls`，单文件最大 20 MB。使用 `openpyxl` 读取 `.xlsx`，使用 `xlrd` 读取 `.xls`。A、B 两张表分别调用同一接口，每次上传独立保存。

原文件保留现有上传存储方式，同时生成一个只包含表头的 UTF-8 JSON 文件。结构文件保留工作表名、表头行范围、展开后的表头矩阵、每列的表头层级及原合并范围，不保存明细行、明细内容或明细公式，不执行字段识别和对账。

## 表头范围与合并处理

- 默认读取每张工作表的首个非空行。如果表头存在跨行合并，自动扩展到覆盖整个合并区域。
- 使用可选表单字段 `header_start_row` 指定表头起始行，`header_row_count` 指定表头行数，均从 1 开始。设置同时应用于该文件的所有非空工作表，空工作表返回空结构。
- 标题、说明行和仅横向合并的多行表头需要显式指定范围。例如表头在第 3～4 行，传入 `header_start_row=3`、`header_row_count=2`。默认不根据明细内容猜测更多表头行，避免保存文字型明细。
- 合并表头按其左上角内容填满每个位置，支持横向、纵向及矩形合并。普通空单元格仍为 `null`；空合并区域拆开后也为 `null`。原合并范围单独保留，原文件不被修改。
- `columns[].header_path` 按表头从上到下保留层级，忽略空值及连续重复值。`header_rows` 保留展开后的完整矩阵。列索引及列字母对应原 Excel，不以表头文字作为键，因此重复表头不会覆盖列。
- 列数依据选定表头中的实际内容和合并范围确定，忽略末尾仅有格式的空白列及明细区额外使用的列。表头中间的空列保留原位置，合并表头覆盖的空位置也会保留。

成功返回 HTTP 201：

```json
{
  "file_id": "上传文件的唯一编号",
  "filename": "账单.xlsx",
  "size": 12345,
  "structure_filename": "上传文件的唯一编号.headers.json",
  "structure_url": "/api/uploads/上传文件的唯一编号/headers.json"
}
```

文件位置：

- 原始上传文件：`backend/.data/uploads/<file_id>.xlsx` 或 `.xls`。
- 表头结构：`backend/.data/uploads/<file_id>.headers.json`。

使用 `GET /api/uploads/<file_id>/headers.json` 下载已生成的表头 JSON，响应中的 `structure_url` 为对应下载地址。文件不存在时返回 404。

## 在页面上测试

打开前端页面，在 A/B 上传框选择一张或两张 Excel，点击“生成表头 JSON”。页面会分别展示每个文件的 JSON 预览，并提供“下载 JSON”按钮。需要调整表头范围时，展开“指定表头范围（可选）”，分别填写每张表的起始行和行数。单张表解析失败时，另一张成功生成的 JSON 仍可预览和下载。

前端启动命令（另一个终端）：

```powershell
cd frontend
npm install
npm run dev
```

本地页面：[表头 JSON 测试](http://localhost:4567)。

例如原表头 `A1:A2=序号`、`B1:C1=商品`、`B2=名称`、`C2=规格`，保存的表头矩阵为：

```json
[
  ["序号", "商品", "商品"],
  ["序号", "名称", "规格"]
]
```

B 列的 `header_path` 为 `["商品", "名称"]`，C 列为 `["商品", "规格"]`。第 3 行开始的明细不会写入结构文件。

缺少文件或请求参数不合法返回 422，不支持的扩展名返回 415，空文件返回 400，超限返回 413。损坏、加密、格式与扩展名不符的文件、超出工作表的表头范围，以及展开后超过 100,000 个单元格的表头返回 422。失败上传会清除本次新建的原文件和结构文件，同名上传分别保存。

本地调试页面：[Swagger UI](http://127.0.0.1:7546/docs)。

```powershell
curl.exe -F "file=@账单.xlsx" http://127.0.0.1:7546/api/upload
curl.exe -F "file=@账单.xlsx" -F "header_start_row=3" -F "header_row_count=2" http://127.0.0.1:7546/api/upload
```

验证：

```powershell
uv run mypy
uv run pytest -q
```
