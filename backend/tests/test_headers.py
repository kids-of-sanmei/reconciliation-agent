"""用真实工作簿覆盖格式兼容、合并表头和明细排除。"""

import json
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
import xlwt
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill

from app.api.dependencies import get_upload_service
from app.core.config import UploadSettings
from app.main import create_app
from app.services.headers import ExcelHeaderReader
from app.services.uploads import ExcelUploadService


def workbook_bytes(extension: str, *, plain: bool = False) -> bytes:
    output = BytesIO()
    if extension == ".xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "账表"
        if plain:
            sheet.append(["编码", "名称"])
            sheet.append(["PRIVATE_CODE", "PRIVATE_DETAIL"])
        else:
            sheet.merge_cells("A3:A4")
            sheet["A3"] = "序号"
            sheet.merge_cells("B3:C3")
            sheet["B3"] = "商品"
            sheet["B4"] = "名称"
            sheet["C4"] = "规格"
            sheet.merge_cells("D3:D4")
            sheet["D3"] = "金额"
            sheet.merge_cells("E3:E4")
            sheet.merge_cells("A5:A6")
            sheet["A5"] = "PRIVATE_MERGED_DETAIL"
            sheet["B5"] = "PRIVATE_DETAIL"
            sheet["C5"] = "PRIVATE_SPEC"
            sheet["D5"] = 98765.43
            sheet["D6"] = "=98765.43*2"
            second = workbook.create_sheet("另一张表")
            second.sheet_state = "hidden"
            second.append(["单号", "数量"])
            second.append(["PRIVATE_ORDER", 24680])
            workbook.create_sheet("空表")
        workbook.save(output)
        workbook.close()
    else:
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("账表")
        if plain:
            sheet.write(0, 0, "编码")
            sheet.write(0, 1, "名称")
            sheet.write(1, 0, "PRIVATE_CODE")
            sheet.write(1, 1, "PRIVATE_DETAIL")
        else:
            sheet.write_merge(2, 3, 0, 0, "序号")
            sheet.write_merge(2, 2, 1, 2, "商品")
            sheet.write(3, 1, "名称")
            sheet.write(3, 2, "规格")
            sheet.write_merge(2, 3, 3, 3, "金额")
            sheet.write_merge(2, 3, 4, 4, "")
            sheet.write_merge(4, 5, 0, 0, "PRIVATE_MERGED_DETAIL")
            sheet.write(4, 1, "PRIVATE_DETAIL")
            sheet.write(4, 2, "PRIVATE_SPEC")
            sheet.write(4, 3, 98765.43)
            sheet.write(5, 3, xlwt.Formula("98765.43*2"))
            second = workbook.add_sheet("另一张表")
            second.visibility = 1
            second.write(0, 0, "单号")
            second.write(0, 1, "数量")
            second.write(1, 0, "PRIVATE_ORDER")
            second.write(1, 1, 24680)
            workbook.add_sheet("空表")
        workbook.save(output)
    return output.getvalue()


@pytest.fixture
def client(tmp_path: Path):
    app = create_app()
    service = ExcelUploadService(settings=UploadSettings(directory=tmp_path))
    app.dependency_overrides[get_upload_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("extension", [".xlsx", ".xls"])
def test_upload_saves_only_headers_and_expands_merges(client, tmp_path, extension):
    original = workbook_bytes(extension)
    response = client.post("/api/upload", files={"file": (f"账表{extension}", original)})
    assert response.status_code == 201
    result = response.json()
    assert result["size"] == len(original)
    assert (tmp_path / f"{result['file_id']}{extension}").read_bytes() == original
    structure = (tmp_path / result["structure_filename"]).read_text(encoding="utf-8")
    assert "PRIVATE" not in structure
    assert "98765" not in structure
    assert "24680" not in structure
    headers = json.loads(structure)
    assert headers["file_id"] == result["file_id"]
    assert [sheet["name"] for sheet in headers["sheets"]] == ["账表", "另一张表", "空表"]
    sheet = headers["sheets"][0]
    assert (sheet["header_start_row"], sheet["header_end_row"]) == (3, 4)
    assert sheet["header_rows"] == [
        ["序号", "商品", "商品", "金额", None],
        ["序号", "名称", "规格", "金额", None],
    ]
    assert sheet["columns"] == [
        {"index": 1, "letter": "A", "header_path": ["序号"]},
        {"index": 2, "letter": "B", "header_path": ["商品", "名称"]},
        {"index": 3, "letter": "C", "header_path": ["商品", "规格"]},
        {"index": 4, "letter": "D", "header_path": ["金额"]},
        {"index": 5, "letter": "E", "header_path": []},
    ]
    assert sheet["merged_ranges"] == [
        {"range": "A3:A4", "source": "A3"},
        {"range": "B3:C3", "source": "B3"},
        {"range": "D3:D4", "source": "D3"},
        {"range": "E3:E4", "source": "E3"},
    ]
    assert headers["sheets"][1]["header_rows"] == [["单号", "数量"]]
    assert headers["sheets"][2]["header_rows"] == []
    assert headers["sheets"][2]["header_start_row"] is None


@pytest.mark.parametrize("extension", [".xlsx", ".xls"])
def test_text_only_detail_rows_are_never_inferred_as_headers(client, tmp_path, extension):
    response = client.post(
        "/api/upload", files={"file": (f"同名{extension}", workbook_bytes(extension, plain=True))},
    )
    assert response.status_code == 201
    structure = (tmp_path / response.json()["structure_filename"]).read_text(encoding="utf-8")
    assert "PRIVATE" not in structure
    sheet = json.loads(structure)["sheets"][0]
    assert sheet["header_rows"] == [["编码", "名称"]]


@pytest.mark.parametrize("extension", [".xlsx", ".xls"])
def test_explicit_multilevel_headers_and_rectangular_merge(client, tmp_path, extension):
    output = BytesIO()
    if extension == ".xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet["A1"] = "PRIVATE_TITLE"
        sheet.merge_cells("A2:C3")
        sheet["A2"] = "共同表头"
        sheet.append(["PRIVATE_DETAIL", "PRIVATE_DETAIL", "PRIVATE_DETAIL"])
        workbook.create_sheet("空表")
        workbook.save(output)
    else:
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("表头")
        sheet.write(0, 0, "PRIVATE_TITLE")
        sheet.write_merge(1, 2, 0, 2, "共同表头")
        sheet.write(3, 0, "PRIVATE_DETAIL")
        workbook.add_sheet("空表")
        workbook.save(output)
    response = client.post(
        "/api/upload", files={"file": (f"表头{extension}", output.getvalue())},
        data={"header_start_row": "2", "header_row_count": "2"},
    )
    assert response.status_code == 201
    structure = (tmp_path / response.json()["structure_filename"]).read_text(encoding="utf-8")
    assert "PRIVATE" not in structure
    sheet = json.loads(structure)["sheets"][0]
    assert sheet["header_rows"] == [["共同表头"] * 3] * 2
    assert sheet["header_start_row"] == 2
    assert sheet["header_end_row"] == 3


def test_multilevel_headers_without_vertical_merges_use_explicit_count(client, tmp_path):
    output = BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "商品"
    sheet.append(["名称", "规格"])
    sheet.append(["PRIVATE_NAME", "PRIVATE_SPEC"])
    workbook.save(output)
    response = client.post(
        "/api/upload", files={"file": ("表头.xlsx", output.getvalue())},
        data={"header_row_count": "2"},
    )
    assert response.status_code == 201
    structure = (tmp_path / response.json()["structure_filename"]).read_text(encoding="utf-8")
    assert "PRIVATE" not in structure
    assert json.loads(structure)["sheets"][0]["header_rows"] == [
        ["商品", "商品"], ["名称", "规格"],
    ]


def test_same_named_a_and_b_uploads_have_independent_structures(client, tmp_path):
    results = [
        client.post("/api/upload", files={"file": ("账表.xlsx", workbook_bytes(".xlsx"))}).json()
        for _ in range(2)
    ]
    assert results[0]["file_id"] != results[1]["file_id"]
    assert results[0]["structure_filename"] != results[1]["structure_filename"]
    assert len(list(tmp_path.iterdir())) == 4


@pytest.mark.parametrize("extension", [".xlsx", ".xls"])
def test_invalid_workbook_returns_422_and_cleans_upload(client, tmp_path, extension):
    response = client.post("/api/upload", files={"file": (f"损坏{extension}", b"not an excel file")})
    assert response.status_code == 422
    assert "无法读取 Excel" in response.json()["detail"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("data", [
    {"header_start_row": "0"}, {"header_row_count": "0"},
    {"header_start_row": "100"}, {"header_row_count": "100"},
])
def test_invalid_header_selection_returns_422_and_leaves_no_files(client, tmp_path, data):
    response = client.post(
        "/api/upload", files={"file": ("账表.xlsx", workbook_bytes(".xlsx", plain=True))}, data=data,
    )
    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("filename, content, expected_status", [
    ("empty.xlsx", b"", 400), ("text.csv", b"a,b", 415),
])
def test_existing_upload_validation(client, tmp_path, filename, content, expected_status):
    response = client.post("/api/upload", files={"file": (filename, content)})
    assert response.status_code == expected_status
    assert list(tmp_path.iterdir()) == []


def test_size_limit_still_cleans_incomplete_files(tmp_path):
    from app.services.errors import UploadSizeLimitError

    service = ExcelUploadService(settings=UploadSettings(directory=tmp_path, max_size_bytes=2))
    with pytest.raises(UploadSizeLimitError):
        service.save(filename="账表.xlsx", stream=BytesIO(b"123"))
    assert list(tmp_path.iterdir()) == []


def test_structure_collision_preserves_existing_file_and_removes_new_upload(tmp_path, monkeypatch):
    file_id = UUID(int=1)
    monkeypatch.setattr("app.services.uploads.uuid4", lambda: file_id)
    existing = tmp_path / f"{file_id.hex}.headers.json"
    existing.write_text("existing", encoding="utf-8")
    service = ExcelUploadService(settings=UploadSettings(directory=tmp_path))
    with pytest.raises(FileExistsError):
        service.save(filename="账表.xlsx", stream=BytesIO(workbook_bytes(".xlsx")))
    assert existing.read_text(encoding="utf-8") == "existing"
    assert list(tmp_path.iterdir()) == [existing]


def test_reader_does_not_modify_the_source_workbook(tmp_path):
    path = tmp_path / "账表.xlsx"
    original = workbook_bytes(".xlsx")
    path.write_bytes(original)
    ExcelHeaderReader().read(path=path, file_id="test", filename=path.name)
    assert path.read_bytes() == original
    workbook = load_workbook(path)
    try:
        assert "A3:A4" in workbook.worksheets[0].merged_cells
        assert workbook.worksheets[0]["A4"].value is None
    finally:
        workbook.close()


@pytest.mark.parametrize("extension", [".xlsx", ".xls"])
def test_download_returns_the_saved_header_json(client, tmp_path, extension):
    uploaded = client.post(
        "/api/upload", files={"file": (f"账表{extension}", workbook_bytes(extension))},
    ).json()
    response = client.get(uploaded["structure_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert "attachment" in response.headers["content-disposition"]
    assert uploaded["structure_filename"] in response.headers["content-disposition"]
    assert response.content == (tmp_path / uploaded["structure_filename"]).read_bytes()
    assert response.json()["sheets"][0]["header_rows"][0] == ["序号", "商品", "商品", "金额", None]
    assert "PRIVATE" not in response.text


def test_download_uses_saved_json_without_reparsing_excel(client, tmp_path):
    uploaded = client.post(
        "/api/upload", files={"file": ("账表.xlsx", workbook_bytes(".xlsx"))},
    ).json()
    (tmp_path / f"{uploaded['file_id']}.xlsx").unlink()
    assert client.get(uploaded["structure_url"]).status_code == 200
    (tmp_path / uploaded["structure_filename"]).unlink()
    assert client.get(uploaded["structure_url"]).status_code == 404


@pytest.mark.parametrize("file_id", ["0" * 32, "not-an-id", "a" * 31])
def test_nonexistent_download_returns_404(client, file_id):
    response = client.get(f"/api/uploads/{file_id}/headers.json")
    assert response.status_code == 404
    assert response.json()["detail"] == "表头 JSON 文件不存在"


def test_download_rejects_paths_outside_upload_directory(tmp_path):
    service = ExcelUploadService(settings=UploadSettings(directory=tmp_path / "uploads"))
    existing = tmp_path / "secret.headers.json"
    existing.write_text("private", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        service.get_structure_path(file_id="../secret")


@pytest.mark.parametrize("extension", [".xlsx", ".xls"])
def test_trailing_formatted_columns_do_not_expand_header_json(client, extension):
    output = BytesIO()
    labels = [f"表头{index}" for index in range(1, 10)]
    if extension == ".xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(labels)
        sheet.append(["PRIVATE_DETAIL"] * 9)
        sheet["XFD66"].fill = PatternFill(fill_type="solid", fgColor="FFFF00")
        assert sheet.max_column == 16384
        workbook.save(output)
    else:
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("表头")
        for column, label in enumerate(labels):
            sheet.write(0, column, label)
            sheet.write(1, column, "PRIVATE_DETAIL")
        sheet.write(65, 255, "", xlwt.easyxf("pattern: pattern solid, fore_colour yellow;"))
        workbook.save(output)
    uploaded = client.post(
        "/api/upload", files={"file": (f"格式空列{extension}", output.getvalue())},
    )
    assert uploaded.status_code == 201
    response = client.get(uploaded.json()["structure_url"])
    sheet = response.json()["sheets"][0]
    assert sheet["column_count"] == 9
    assert sheet["header_rows"] == [labels]
    assert len(sheet["columns"]) == 9
    assert len(response.content) < 5000
    assert "PRIVATE_DETAIL" not in response.text


def test_formatted_width_does_not_trigger_multilevel_header_size_limit(client):
    output = BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.merge_cells("A1:A7")
    sheet["A1"] = "编号"
    sheet["B1"] = "商品"
    sheet["B7"] = "名称"
    sheet["C1"] = "金额"
    sheet["A8"] = "PRIVATE_DETAIL"
    sheet["XFD66"].fill = PatternFill(fill_type="solid", fgColor="FFFF00")
    workbook.save(output)
    uploaded = client.post("/api/upload", files={"file": ("多行表头.xlsx", output.getvalue())})
    assert uploaded.status_code == 201
    response = client.get(uploaded.json()["structure_url"])
    sheet = response.json()["sheets"][0]
    assert sheet["column_count"] == 3
    assert sheet["header_end_row"] == 7
    assert len(sheet["header_rows"]) == 7
    assert all(row[0] == "编号" and len(row) == 3 for row in sheet["header_rows"])
    assert sheet["columns"][1]["header_path"] == ["商品", "名称"]
    assert "PRIVATE_DETAIL" not in response.text


def test_internal_blank_columns_keep_coordinates_while_detail_only_columns_are_ignored(client):
    output = BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "编号"
    sheet["C1"] = "金额"
    sheet["E2"] = "PRIVATE_DETAIL_WITHOUT_HEADER"
    workbook.save(output)
    uploaded = client.post("/api/upload", files={"file": ("内部空列.xlsx", output.getvalue())})
    assert uploaded.status_code == 201
    response = client.get(uploaded.json()["structure_url"])
    sheet = response.json()["sheets"][0]
    assert sheet["column_count"] == 3
    assert sheet["header_rows"] == [["编号", None, "金额"]]
    assert sheet["columns"] == [
        {"index": 1, "letter": "A", "header_path": ["编号"]},
        {"index": 2, "letter": "B", "header_path": []},
        {"index": 3, "letter": "C", "header_path": ["金额"]},
    ]
    assert "PRIVATE_DETAIL" not in response.text
