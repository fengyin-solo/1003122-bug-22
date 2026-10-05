"""监测分站接口：维护分站台账、批量导入导出和链路运行报表。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.monitorstation import IMPORT_FIELDS, STATUS_ORDER, MonitorstationService

router = APIRouter(prefix="/api/monitorstation", tags=["监测分站"])

service = MonitorstationService()

LIST_FIELDS = IMPORT_FIELDS
STATUSES = STATUS_ORDER


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按分站编号检索"),
    status: str | None = Query(default=None, description="正常运行、通信中断、备用供电、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按分站编号与状态过滤监测分站列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/link-reports", response_model=PageResult[dict])
def list_link_reports(
    keyword: str | None = Query(default=None, description="按分站编号检索"),
    status: str | None = Query(default=None, description="链路状态"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取链路运行报表，报表随分站台账状态变化同步更新。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_reports(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/template")
def download_template() -> PlainTextResponse:
    """下载台账导入模板，表头字段顺序与台账明细、导出文件一致。"""
    return PlainTextResponse(
        service.export_csv(include_items=False),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=monitorstation-template.csv"},
    )


@router.post("/import")
async def import_entries(
    request: Request,
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """先逐行校验再提交有效行；重复行跳过、错误行退回，回执逐行说明。"""
    if file is not None:
        raw = await file.read()
        return service.import_file(raw, file.filename or "")

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return service.import_payload(await request.json())
    form = await request.form()
    uploaded = form.get("file")
    if hasattr(uploaded, "read"):
        return service.import_file(await uploaded.read(), getattr(uploaded, "filename", ""))
    raise HTTPException(status_code=400, detail="请上传 CSV/JSON 文件，或按接口约定提交 JSON 数据")


@router.get("/export")
def export_entries(format: str = Query(default="json", description="json 或 csv")) -> Response | dict[str, Any]:
    """导出监测分站清单；CSV 与导入模板字段顺序完全一致，可原样导回。"""
    if format == "csv":
        return Response(
            content=service.export_csv(include_items=True),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=monitorstation-export.csv"},
        )
    return service.export_payload()


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条监测分站明细；列表和详情均取自同一次落库后的同一条记录。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"监测分站 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条监测分站，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="监测分站已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条监测分站执行通信排查、切换供电、办理停用；状态变化同步到链路报表。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
