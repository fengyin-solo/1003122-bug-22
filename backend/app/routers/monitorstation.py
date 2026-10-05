"""监测分站接口：台账导入导出、通信排查、切换供电、办理停用与链路运行报表。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionResult, EntryPayload, ImportPayload, ImportReceipt, PageResult
from app.services.monitorstation import MonitorstationService

router = APIRouter(prefix="/api/monitorstation", tags=["监测分站"])

service = MonitorstationService()

STATUSES = ["正常运行", "通信中断", "备用供电", "已停用"]


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


@router.get("/link-report")
def link_report() -> dict[str, Any]:
    """监测分站链路运行报表：与分站落库记录同步，按当前库内记录生成。"""
    items = service.link_report()
    return {"module": "linkstatus", "total": len(items), "items": items}


@router.get("/template")
def download_template() -> StreamingResponse:
    """下载导入模板：表头字段顺序与设备台账明细完全一致。"""
    return _csv_response(service.ledger_header(), [], "monitorstation_template.csv")


@router.post("/import", response_model=ImportReceipt)
def import_ledger(payload: ImportPayload) -> ImportReceipt:
    """导入设备台账导出的监测分站清单。

    文件先整体校验再落库：表头不符、空文件等文件级错误整批不落库并说明；
    行级问题只退回该行，其余行照常入账；回执逐行说明入账/跳过/退回原因。
    """
    try:
        receipt = service.import_ledger(payload.filename, payload.content)
    except ValueError as exc:
        # 文件级错误：整批未落库，422 让值班员立刻知道文件口径不对
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ImportReceipt(**receipt)


@router.get("/export")
def export_entries() -> StreamingResponse:
    """导出监测分站台账明细：字段顺序与导入模板一致，可原样导回（回环内容不变）。"""
    return _csv_response(service.ledger_header(), service.ledger_rows(), "monitorstation_ledger.csv")


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条监测分站明细；与列表取自同一次落库的记录。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"监测分站 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条监测分站，缺字段或与已有分站重名/重号时说明原因。"""
    entry, message = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="监测分站已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条监测分站执行通信排查、切换供电、办理停用；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


def _csv_response(header: list[str], rows: list[dict[str, str]], filename: str) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    # utf-8-sig BOM，Excel 直接打开不乱码；导入端会剥掉 BOM
    data = "\ufeff" + buffer.getvalue()
    return StreamingResponse(
        io.BytesIO(data.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
