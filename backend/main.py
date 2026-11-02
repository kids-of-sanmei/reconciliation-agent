"""仅提供 Excel 原文件上传。"""

from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

UPLOAD_DIRECTORY = Path(__file__).resolve().parent / ".data" / "uploads"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
ALLOWED_EXTENSIONS = {".xlsx", ".xls"}

app = FastAPI(title="Excel 文件上传", version="0.1.0")


class UploadResult(BaseModel):
    """已保存文件的编号、原文件名和实际字节数。"""

    file_id: str
    filename: str
    size: int


@app.post("/api/upload", status_code=201, response_model=UploadResult, summary="上传 Excel")
def upload_excel(
    file: Annotated[UploadFile, File(description="Excel 文件（.xlsx 或 .xls）")],
) -> UploadResult:
    """保存 Excel 原文件，不解析内容或触发业务流程。

    Args:
        file: multipart/form-data 中的 file 字段，最大 20 MB。

    Returns:
        文件编号、去除路径后的原文件名和实际保存的字节数。
    """
    filename = Path(file.filename or "").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="仅支持 .xlsx 和 .xls 文件")

    file_id = uuid4().hex
    destination = UPLOAD_DIRECTORY / f"{file_id}{extension}"
    UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    size = 0
    output = destination.open("xb")
    try:
        with output:
            while chunk := file.file.read(CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="文件不能超过 20 MB")
                output.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="不能上传空文件")
    except (HTTPException, OSError):
        destination.unlink(missing_ok=True)
        raise

    return UploadResult(file_id=file_id, filename=filename, size=size)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=7546)
