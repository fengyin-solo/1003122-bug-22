"""监测分站业务规则：台账导入导出、字段校验、状态流转与链路运行报表同步。

导入口径（与设备台账导出的明细一致）：
1. 文件先整体校验，再逐行校验，全部通过判定后才落库；文件级错误整批不落库。
2. 同一分站（按分站编号或分站名称识别）重复导入只认第一次：库内已存在则跳过，
   文件内重复也只留第一条，回执逐行说明。
3. 行内校验不通过（缺必填、通信地址与接入传感器数量不符）只退回该行，其余行照常入账。
4. 导出按 LEDGER_FIELDS 的字段顺序输出，导出去再导回来内容不变。

分站状态一旦变化，同步写入链路运行报表（linkstatus）；存量分站在服务首次加载时
按接入时间（接入时间）排序回填。
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from typing import Any

from app.store import store

MODULE = "monitorstation"
LINK_REPORT_MODULE = "linkstatus"

# 台账明细字段顺序：导出模板、列表、详情统一取这一份
LEDGER_FIELDS = ["分站编号", "分站名称", "所在位置", "通信地址", "接入传感器", "信号强度", "后备电源", "分站状态"]
REQUIRED_FIELDS = ["分站编号", "分站名称", "所在位置", "通信地址", "接入传感器"]
# 导入文件允许附带接入时间；导出模板不带，回环导一遍内容保持不变
EXTRA_IMPORT_FIELDS = ["接入时间"]

STATUS_ORDER = ["正常运行", "通信中断", "备用供电", "已停用"]
DEFAULT_STATUS = STATUS_ORDER[0]
ACTION_RULES = {"通信排查": "通信中断", "切换供电": "备用供电", "办理停用": "已停用"}
NEGATIVE_ACTIONS = []

_SPLIT_PATTERN = re.compile(r"[、,，;；\s]+")


def _split_items(value: Any) -> list[str]:
    """通信地址 / 接入传感器按常见分隔符拆成明细项。"""
    return [item.strip() for item in _SPLIT_PATTERN.split(str(value or "").strip()) if item.strip()]


class MonitorstationService:
    def __init__(self) -> None:
        # 存量分站规范化 + 按接入时间回填链路运行报表，只做一次
        self._normalize_seed_rows()
        self._backfill_link_report()

    # ------------------------------------------------------------------ 读取

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("分站编号", ""))]
        if status:
            rows = [row for row in rows if self._view(row).get("分站状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._view(row) if row is not None else None

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表和详情共用同一口径：都取自落库后的这一条记录。"""
        view = {"id": row.get("id")}
        for field in LEDGER_FIELDS:
            view[field] = row.get(field)
        view["接入时间"] = row.get("接入时间")
        return view

    # ------------------------------------------------------------------ 登记

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        rows = store.rows(MODULE)
        dup = self._find_duplicate(rows, values)
        if dup is not None:
            return None, f"分站已存在（分站编号/名称重复）：{dup.get('分站编号')} {dup.get('分站名称')}"
        error = self._validate_pairing(values)
        if error:
            return None, error
        entry = self._build_entry(rows, values)
        rows.append(entry)
        self._sync_link_report(entry)
        return self._view(entry), ""

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"监测分站 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于监测分站可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["分站状态"] = target
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS or target == "通信中断"
        self._sync_link_report(entry)
        return self._view(entry), f"监测分站已{action}"

    # ------------------------------------------------------------------ 导出

    def ledger_header(self) -> list[str]:
        """导出/模板表头，与设备台账明细字段顺序一致。"""
        return list(LEDGER_FIELDS)

    def ledger_rows(self) -> list[dict[str, str]]:
        rows = store.rows(MODULE)
        return [{field: "" if row.get(field) is None else str(row.get(field)) for field in LEDGER_FIELDS} for row in rows]

    # ------------------------------------------------------------------ 导入

    def import_ledger(self, filename: str | None, content: str) -> dict[str, Any]:
        """台账文件导入：先校验后落库，逐行回执。

        返回回执 dict；文件级错误（空文件、表头不匹配、JSON 解析失败）抛 ValueError，
        由路由层整批拒绝，不落任何数据。
        """
        records = self._parse_file(filename, content)

        receipt_rows: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []  # 校验通过、等待落库的行
        seen_codes: set[str] = set()       # 文件内分站编号去重
        seen_names: set[str] = set()       # 文件内分站名称去重（编号、名称任一重复即重复分站）
        imported = skipped = rejected = 0

        for index, raw in enumerate(records, start=2):  # 第 1 行是表头，数据行从 2 开始
            code = str(raw.get("分站编号") or "").strip()
            name = str(raw.get("分站名称") or "").strip()
            row_base = {"line": index, "分站编号": code, "分站名称": name}

            missing = [field for field in REQUIRED_FIELDS if not str(raw.get(field) or "").strip()]
            if missing:
                rejected += 1
                receipt_rows.append({**row_base, "result": "退回", "message": f"缺少必填字段：{'、'.join(missing)}"})
                continue

            error = self._validate_pairing(raw)
            if error:
                rejected += 1
                receipt_rows.append({**row_base, "result": "退回", "message": error})
                continue

            dup_existing = self._find_duplicate(store.rows(MODULE), raw)
            if code in seen_codes or name in seen_names or dup_existing is not None:
                skipped += 1
                if dup_existing is not None:
                    seen_codes.add(str(dup_existing.get("分站编号") or ""))
                    seen_names.add(str(dup_existing.get("分站名称") or ""))
                    message = f"分站已存在（库内第 {dup_existing.get('id')} 条），重复导入只认第一次"
                else:
                    message = "与本文件前面的分站重复，只保留第一条"
                receipt_rows.append({**row_base, "result": "跳过", "message": message})
                continue

            seen_codes.add(code)
            seen_names.add(name)
            pending.append(raw)
            receipt_rows.append({**row_base, "result": "待入账", "message": "校验通过"})

        # 校验完成后把合法行统一落库（不与校验交叉写库），落库失败可整体放弃
        rows = store.rows(MODULE)
        created: list[dict[str, Any]] = []
        for raw, receipt in zip(pending, (r for r in receipt_rows if r["result"] == "待入账")):
            entry = self._build_entry(rows, raw)
            rows.append(entry)
            created.append(entry)
            receipt["result"] = "入账"
            receipt["message"] = f"已入账（记录编号 {entry['id']}）"
            imported += 1
        for entry in created:
            self._sync_link_report(entry)

        message = f"共 {len(records)} 行：入账 {imported} 行，跳过 {skipped} 行，退回 {rejected} 行"
        return {
            "total": len(records),
            "imported": imported,
            "skipped": skipped,
            "rejected": rejected,
            "rows": receipt_rows,
            "message": message,
        }

    def _parse_file(self, filename: str | None, content: str) -> list[dict[str, Any]]:
        """解析 CSV / JSON 文件；表头必须与台账明细字段顺序一致，否则整批拒绝。"""
        text = content.strip().lstrip("\ufeff")
        if not text:
            raise ValueError("导入文件为空：请使用导出的台账模板填写后再导入")

        name = (filename or "").lower()
        if name.endswith(".json") or (not name.endswith(".csv") and text[:1] in {"{", "["}):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"文件不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列），整批未导入") from exc
            if isinstance(data, dict):
                data = data.get("items")
            if not isinstance(data, list):
                raise ValueError("JSON 文件需为台账明细数组（或含 items 数组），整批未导入")
            records = [dict(item) for item in data if isinstance(item, dict)]
            if records and list(records[0].keys())[:len(LEDGER_FIELDS)] != LEDGER_FIELDS:
                raise ValueError("JSON 字段顺序与台账明细不一致，请使用本系统导出的模板")
            return records

        reader = csv.reader(io.StringIO(text))
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError("导入文件为空：请使用导出的台账模板填写后再导入") from exc
        header = [column.strip().lstrip("\ufeff") for column in header]
        if header[:len(LEDGER_FIELDS)] != LEDGER_FIELDS:
            raise ValueError(
                "表头与台账明细不一致，整批未导入。"
                f"应为：{'、'.join(LEDGER_FIELDS)}；实际为：{'、'.join(header) or '（空）'}"
            )
        allowed = LEDGER_FIELDS + EXTRA_IMPORT_FIELDS
        extra = [column for column in header[len(LEDGER_FIELDS):] if column and column not in allowed]
        if extra:
            raise ValueError(f"存在台账以外的字段：{'、'.join(extra)}，整批未导入")

        records: list[dict[str, Any]] = []
        for line in reader:
            if not any(cell.strip() for cell in line):
                continue  # 跳过尾部空行，不算退回
            if len(line) < len(header):
                line = line + [""] * (len(header) - len(line))
            records.append({column: line[pos].strip() for pos, column in enumerate(header) if column})
        if not records:
            raise ValueError("文件只有表头没有数据行，整批未导入")
        return records

    def _validate_pairing(self, values: dict[str, Any]) -> str:
        """通信地址与接入传感器必须一一配对：数量相等且至少各一个。"""
        addresses = _split_items(values.get("通信地址"))
        sensors = _split_items(values.get("接入传感器"))
        if not addresses:
            return "通信地址为空"
        if not sensors:
            return "接入传感器为空"
        if len(addresses) != len(sensors):
            return f"通信地址({len(addresses)}个)与接入传感器({len(sensors)}个)数量不符，整行退回"
        return ""

    # ------------------------------------------------------------------ 内部

    def _build_entry(self, rows: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in LEDGER_FIELDS:
            entry[field] = str(values.get(field) or "").strip()
        status = entry["分站状态"] if entry["分站状态"] in STATUS_ORDER else DEFAULT_STATUS
        entry["分站状态"] = status
        entry["status"] = status
        entry["pending"] = status != STATUS_ORDER[-1]
        entry["abnormal"] = status == "通信中断"
        connected_at = str(values.get("接入时间") or "").strip()
        entry["接入时间"] = connected_at or date.today().isoformat()
        return entry

    def _find_duplicate(self, rows: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any] | None:
        code = str(values.get("分站编号") or "").strip()
        name = str(values.get("分站名称") or "").strip()
        for row in rows:
            if code and str(row.get("分站编号") or "").strip() == code:
                return row
        for row in rows:
            if name and str(row.get("分站名称") or "").strip() == name:
                return row
        return None

    # ---------------------------------------------------------- 链路运行报表

    def _normalize_seed_rows(self) -> None:
        """统一存量数据口径：分站状态以业务字段「分站状态」为准，补齐接入时间。"""
        rows = store.rows(MODULE)
        for offset, row in enumerate(rows):
            status = row.get("分站状态") or row.get("status") or DEFAULT_STATUS
            if status not in STATUS_ORDER:
                status = row.get("status") if row.get("status") in STATUS_ORDER else DEFAULT_STATUS
            row["分站状态"] = status
            row["status"] = status
            row["pending"] = status != STATUS_ORDER[-1]
            row["abnormal"] = status == "通信中断"
            if not str(row.get("接入时间") or "").strip():
                # 存量分站没有接入时间：以服务启用日为基准，按表内顺序倒排，越早接入的时间越早
                base = date.today().toordinal() - (len(rows) - 1 - offset)
                row["接入时间"] = date.fromordinal(base).isoformat()

    def _backfill_link_report(self) -> None:
        """存量分站按接入时间回填链路运行报表，已有报表记录不重复生成。"""
        report = store.rows(LINK_REPORT_MODULE)
        synced = {int(item.get("分站ID")) for item in report if item.get("分站ID") is not None}
        rows = sorted(store.rows(MODULE), key=lambda row: str(row.get("接入时间") or ""))
        for row in rows:
            if int(row.get("id", 0)) not in synced:
                report.append(self._report_row(row))

    def _sync_link_report(self, entry: dict[str, Any]) -> None:
        """分站落库或状态变化时，同步链路运行报表（同一次落库的同一条记录）。"""
        report = store.rows(LINK_REPORT_MODULE)
        for item in report:
            if int(item.get("分站ID", 0)) == int(entry.get("id", 0)):
                item.update(self._report_row(entry))
                return
        report.append(self._report_row(entry))

    def _report_row(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(entry.get("id", 0)),
            "分站ID": int(entry.get("id", 0)),
            "分站编号": entry.get("分站编号"),
            "分站名称": entry.get("分站名称"),
            "所在位置": entry.get("所在位置"),
            "通信地址": entry.get("通信地址"),
            "接入传感器": entry.get("接入传感器"),
            "接入时间": entry.get("接入时间"),
            "链路状态": entry.get("分站状态") or entry.get("status") or DEFAULT_STATUS,
            "更新时间": date.today().isoformat(),
        }

    def link_report(self) -> list[dict[str, Any]]:
        return list(store.rows(LINK_REPORT_MODULE))
