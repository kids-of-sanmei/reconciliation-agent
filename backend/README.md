# Excel 上传接口

```powershell
cd backend
uv sync
uv run uvicorn main:app --host 127.0.0.1 --port 7546
```

唯一业务接口：`POST /api/upload`，使用 `multipart/form-data`，文件字段为 `file`。

支持 `.xlsx`、`.xls` 扩展名，单个文件最大 20 MB。接口只检查扩展名、大小及非空，原样保存文件，不校验或解析工作簿内容。

成功返回 HTTP 201：

```json
{
  "file_id": "上传文件的唯一编号",
  "filename": "账单.xlsx",
  "size": 12345
}
```

文件保存到 `backend/.data/uploads/<file_id>.<扩展名>`；每次上传使用独立编号，同名文件分别保存。失败上传不会保留不完整文件。缺少文件返回 422，不支持的扩展名返回 415，空文件返回 400，超限返回 413。

本地调试页面：[Swagger UI](http://127.0.0.1:7546/docs)。

```powershell
curl.exe -F "file=@账单.xlsx" http://127.0.0.1:7546/api/upload
```
