"""监测分站业务规则：导入校验、状态流转与链路运行报表同步都收在这里。"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from typing import Any

from app.store import store

MODULE = "monitorstation"
LINK_REPORT_MODULE = "monitorstation_link_report"

# 台账明细、导入模板、导出文件共用同一字段口径和顺序。
IMPORT_FIELDS = [
    "分站编号",
    "分站名称",
    "所在位置",
    "通信地址",
    "接入传感器",
    "传感器数量",
    "信号强度",
    "后备电源",
    "分站状态",
    "接入时间",
]
REQUIRED_FIELDS = ["分站编号", "分站名称", "所在位置", "通信地址", "接入传感器", "传感器数量"]
REPORT_FIELDS = [
    "分站编号",
    "分站名称",
    "通信地址",
    "接入时间",
    "链路状态",
    "最近变化时间",
    "传感器数量",
]
STATUS_ORDER = ["正常运行", "通信中断", "备用供电", "已停用"]
ACTION_RULES = {"通信排查": "通信中断", "切换供电": "备用供电", "办理停用": "已停用"}
NEGATIVE_ACTIONS = []
SPLIT_PATTERN = re.compile(r"[、,;；\n\r]+")


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _split_sensors(value: Any) -> list[str]:
    return [part.strip() for part in SPLIT_PATTERN.split(str(value or "")) if part.strip()]


def _valid_date(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return len(value) == 10 and parsed.strftime("%Y-%m-%d") == value


class MonitorstationService:
    def __init__(self) -> None:
        self._bootstrap()

    # ------------------------------------------------------------------
    # 列表与明细
    # ------------------------------------------------------------------
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
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.public_entry(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self.public_entry(entry) if entry else None

    def public_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """列表和详情都从同一条已落库记录投影，避免两个页面各取一套字段。"""
        sensors = str(entry.get("接入传感器") or "")
        sensor_count = entry.get("传感器数量")
        if sensor_count in (None, ""):
            sensor_count = len(_split_sensors(sensors))
        status = entry.get("分站状态") or entry.get("status") or STATUS_ORDER[0]
        access_time = entry.get("接入时间") or _today()

        result = {"id": int(entry.get("id", 0))}
        for field in IMPORT_FIELDS:
            if field == "接入传感器":
                result[field] = sensors
            elif field == "传感器数量":
                result[field] = int(sensor_count)
            elif field == "分站状态":
                result[field] = status
            elif field == "接入时间":
                result[field] = access_time
            else:
                result[field] = entry.get(field, "")
        return result

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in ["分站编号", "分站名称", "所在位置"] if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update(
            {
                "分站编号": str(values.get("分站编号", "")).strip(),
                "分站名称": str(values.get("分站名称", "")).strip(),
                "所在位置": str(values.get("所在位置", "")).strip(),
                "通信地址": str(values.get("通信地址", "")).strip(),
                "接入传感器": str(values.get("接入传感器", "")).strip(),
                "传感器数量": self._sensor_count(values.get("传感器数量"), values.get("接入传感器", "")),
                "信号强度": str(values.get("信号强度", "")).strip(),
                "后备电源": str(values.get("后备电源", "")).strip(),
                "分站状态": STATUS_ORDER[0],
                "接入时间": str(values.get("接入时间", "")).strip() or _today(),
            }
        )
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        self._create_report(entry)
        return self.public_entry(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"监测分站 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于监测分站可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        old_status = entry.get("status")
        entry["status"] = target
        entry["分站状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if old_status != target:
            self._sync_report(entry, _now())
        return self.public_entry(entry), f"监测分站已{action}"

    # ------------------------------------------------------------------
    # 导入导出
    # ------------------------------------------------------------------
    def import_payload(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and any(key in payload for key in ("file", "upload")):
            return self._file_error(["请使用 multipart/form-data 上传文件，不要把文件对象放进 JSON。"])
        if isinstance(payload, str):
            text = payload.strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    return self._file_error([f"JSON 格式无法解析：{exc.msg}"])
            else:
                return self.import_file(text.encode("utf-8"))
        if isinstance(payload, dict):
            for key in ("csv", "content", "text"):
                if payload.get(key):
                    return self.import_file(str(payload[key]).encode("utf-8"), str(payload.get("filename", "")))
        records = self._records_from_payload(payload)
        return self._import_records(records)

    def import_file(self, raw: bytes, filename: str = "") -> dict[str, Any]:
        text = raw.decode("utf-8-sig").strip()
        if not text:
            return self._file_error(["导入文件为空，请按模板填写后再上传。"])
        if filename.lower().endswith(".json"):
            try:
                return self.import_payload(json.loads(text))
            except json.JSONDecodeError as exc:
                return self._file_error([f"JSON 格式无法解析：{exc.msg}"])

        reader = csv.reader(io.StringIO(text))
        try:
            header = next(reader)
        except StopIteration:
            return self._file_error(["导入文件缺少表头。"])

        normalized_header = [cell.strip() for cell in header]
        if normalized_header != IMPORT_FIELDS:
            missing = [field for field in IMPORT_FIELDS if field not in normalized_header]
            extra = [field for field in normalized_header if field not in IMPORT_FIELDS]
            problems = []
            if missing:
                problems.append(f"缺少字段：{'、'.join(missing)}")
            if extra:
                problems.append(f"存在非模板字段：{'、'.join(extra)}")
            if not missing and not extra:
                problems.append(f"字段顺序与模板不一致，应为：{'、'.join(IMPORT_FIELDS)}")
            return self._file_error(problems)

        records: list[tuple[int, dict[str, Any]]] = []
        for line_number, cells in enumerate(reader, start=2):
            if not any(str(cell or "").strip() for cell in cells):
                continue
            if len(cells) != len(IMPORT_FIELDS):
                values = {IMPORT_FIELDS[i]: cells[i] if i < len(cells) else "" for i in range(len(IMPORT_FIELDS))}
                values["__字段数量错误"] = f"字段数量应为 {len(IMPORT_FIELDS)}，实际为 {len(cells)}"
                records.append((line_number, values))
            else:
                records.append((line_number, dict(zip(IMPORT_FIELDS, cells))))
        return self._import_records(records)

    def export_payload(self) -> dict[str, Any]:
        items, total = self.list_entries(page=1, size=10000)
        return {
            "module": MODULE,
            "total": total,
            "fields": IMPORT_FIELDS,
            "items": [{field: item[field] for field in IMPORT_FIELDS} for item in items],
        }

    def export_csv(self, include_items: bool = True) -> str:
        payload = self.export_payload()
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=IMPORT_FIELDS, lineterminator="\n")
        writer.writeheader()
        if include_items:
            for item in payload["items"]:
                writer.writerow(item)
        return "﻿" + buffer.getvalue()

    # ------------------------------------------------------------------
    # 链路运行报表
    # ------------------------------------------------------------------
    def list_reports(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.hidden_rows(LINK_REPORT_MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("分站编号", ""))]
        if status:
            rows = [row for row in rows if row.get("链路状态") == status]
        rows = sorted(rows, key=lambda row: (str(row.get("接入时间") or ""), int(row.get("station_id", 0))))
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.public_report(row) for row in rows[start:start + size]], total

    def public_report(self, report: dict[str, Any]) -> dict[str, Any]:
        result = {"id": int(report.get("id", 0)), "station_id": int(report.get("station_id", 0))}
        for field in REPORT_FIELDS:
            result[field] = report.get(field, "")
        return result

    # ------------------------------------------------------------------
    # 内部规则
    # ------------------------------------------------------------------
    def _bootstrap(self) -> None:
        """补齐存量分站字段，并按接入时间回填链路运行报表。"""
        rows = store.rows(MODULE)
        for row in rows:
            status = row.get("分站状态")
            if status not in STATUS_ORDER:
                status = row.get("status") if row.get("status") in STATUS_ORDER else STATUS_ORDER[0]
            row["分站状态"] = status
            row["status"] = status

            sensors = str(row.get("接入传感器") or "")
            row["接入传感器"] = sensors
            row["传感器数量"] = int(row.get("传感器数量") or 0) or len(_split_sensors(sensors))

            access_time = str(row.get("接入时间") or "").strip()
            if not _valid_date(access_time):
                access_time = _today()
            row["接入时间"] = access_time

        reports = store.hidden_rows(LINK_REPORT_MODULE)
        reports_by_station = {int(report.get("station_id", 0)): report for report in reports}
        next_report_id = max((int(report.get("id", 0)) for report in reports), default=0) + 1
        for station in sorted(rows, key=lambda row: (str(row.get("接入时间") or ""), int(row.get("id", 0)))):
            station_id = int(station.get("id", 0))
            report = reports_by_station.get(station_id)
            if report is None:
                report = self._build_report(station, next_report_id)
                next_report_id += 1
                reports.append(report)
            else:
                self._apply_station_to_report(report, station)
                report.setdefault("最近变化时间", station.get("接入时间") or _today())

    def _build_report(self, station: dict[str, Any], report_id: int) -> dict[str, Any]:
        report: dict[str, Any] = {
            "id": report_id,
            "station_id": int(station.get("id", 0)),
            "status": station.get("status"),
            "pending": False,
            "abnormal": station.get("status") == "通信中断",
            "最近变化时间": station.get("接入时间") or _today(),
        }
        self._apply_station_to_report(report, station)
        return report

    def _apply_station_to_report(self, report: dict[str, Any], station: dict[str, Any]) -> None:
        report["station_id"] = int(station.get("id", 0))
        report["分站编号"] = station.get("分站编号", "")
        report["分站名称"] = station.get("分站名称", "")
        report["通信地址"] = station.get("通信地址", "")
        report["接入时间"] = station.get("接入时间") or _today()
        report["传感器数量"] = int(station.get("传感器数量") or 0)
        report["链路状态"] = station.get("分站状态") or station.get("status") or STATUS_ORDER[0]
        report["status"] = report["链路状态"]
        report["abnormal"] = report["链路状态"] == "通信中断"

    def _create_report(self, station: dict[str, Any]) -> dict[str, Any]:
        reports = store.hidden_rows(LINK_REPORT_MODULE)
        report_id = max((int(row.get("id", 0)) for row in reports), default=0) + 1
        report = self._build_report(station, report_id)
        reports.append(report)
        return report

    def _sync_report(self, station: dict[str, Any], changed_at: str) -> None:
        reports = store.hidden_rows(LINK_REPORT_MODULE)
        report = next(
            (
                row
                for row in reports
                if int(row.get("station_id", 0)) == int(station.get("id", 0))
                or str(row.get("分站编号", "")) == str(station.get("分站编号", ""))
            ),
            None,
        )
        if report is None:
            report = self._create_report(station)
        else:
            self._apply_station_to_report(report, station)
        report["最近变化时间"] = changed_at

    def _sensor_count(self, declared: Any, sensors: Any) -> int:
        try:
            return int(str(declared).strip())
        except (TypeError, ValueError):
            return len(_split_sensors(sensors))

    def _records_from_payload(self, payload: Any) -> list[tuple[int, dict[str, Any]]]:
        if isinstance(payload, str):
            return []
        if isinstance(payload, list):
            return [(index, row) for index, row in enumerate(payload, start=1) if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        if any(field in payload for field in IMPORT_FIELDS):
            return [(1, payload)]
        for key in ("items", "rows", "records"):
            raw_rows = payload.get(key)
            if isinstance(raw_rows, list):
                fields = payload.get("fields") if isinstance(payload.get("fields"), list) else IMPORT_FIELDS
                records: list[tuple[int, dict[str, Any]]] = []
                for index, raw_row in enumerate(raw_rows, start=1):
                    if isinstance(raw_row, dict):
                        records.append((index, raw_row))
                    elif isinstance(raw_row, list):
                        records.append((index, dict(zip(fields, raw_row))))
                return records
        return []

    def _json_row_aliases(self, raw: dict[str, Any]) -> dict[str, str]:
        """兼容设备台账系统常见英文字段名；CSV 模板仍严格按中文字段口径校验。"""
        aliases = {
            "分站编号": ("分站编号", "station_code", "code"),
            "分站名称": ("分站名称", "station_name", "name"),
            "所在位置": ("所在位置", "location", "position"),
            "通信地址": ("通信地址", "communication_address", "address", "comm_address"),
            "接入传感器": ("接入传感器", "sensors", "sensor_list", "connected_sensors"),
            "传感器数量": ("传感器数量", "sensor_count", "sensor_total"),
            "信号强度": ("信号强度", "signal_strength", "signal"),
            "后备电源": ("后备电源", "backup_power", "power_backup"),
            "分站状态": ("分站状态", "status", "station_status"),
            "接入时间": ("接入时间", "access_time", "connected_at", "online_time"),
        }
        return {
            field: self._text_value(next((raw[alias] for alias in names if raw.get(alias) is not None), ""))
            for field, names in aliases.items()
        }

    def _normalize_values(self, values: dict[str, str]) -> dict[str, str]:
        normalized = {field: str(value or "").strip() for field, value in values.items()}
        normalized["接入传感器"] = self._text_value(normalized.get("接入传感器", ""))
        return normalized

    def _text_value(self, value: Any) -> str:
        if isinstance(value, list):
            return "、".join(str(item).strip() for item in value if str(item or "").strip())
        return str(value or "").strip()

    def _import_records(self, records: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
        rows = store.rows(MODULE)
        code_owner: dict[str, str] = {
            str(row.get("分站编号", "")).strip(): "已落库台账"
            for row in rows
            if str(row.get("分站编号", "")).strip()
        }
        address_owner: dict[str, str] = {
            str(row.get("通信地址", "")).strip(): "已落库台账"
            for row in rows
            if str(row.get("通信地址", "")).strip()
        }
        name_owner: dict[str, str] = {
            str(row.get("分站名称", "")).strip(): "已落库台账"
            for row in rows
            if str(row.get("分站名称", "")).strip()
        }

        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        valid: list[tuple[int, dict[str, Any]]] = []
        results: list[dict[str, Any]] = []

        for row_number, raw in records:
            values = {field: str(raw.get(field, "") or "").strip() for field in IMPORT_FIELDS}
            if not any(values.values()):
                values = self._json_row_aliases(raw)
            values = self._normalize_values(values)
            errors: list[str] = []
            warnings: list[str] = []
            duplicate_messages: list[str] = []

            for field in REQUIRED_FIELDS:
                if not values[field]:
                    errors.append(f"缺少必填字段「{field}」")

            field_count_error = str(raw.get("__字段数量错误", "")).strip()
            if field_count_error:
                errors.append(field_count_error)

            sensors = _split_sensors(values["接入传感器"])
            declared_count: int | None = None
            if values["传感器数量"]:
                try:
                    declared_count = int(values["传感器数量"])
                    if declared_count < 1:
                        errors.append("传感器数量必须大于 0")
                    elif declared_count != len(sensors):
                        errors.append(f"传感器数量为 {declared_count}，接入传感器清单解析出 {len(sensors)} 个，整行退回")
                except ValueError:
                    errors.append("传感器数量必须是整数")

            if values["接入传感器"] and not sensors:
                errors.append("接入传感器清单不能为空")

            station_status = values["分站状态"] or STATUS_ORDER[0]
            if values["分站状态"] and station_status not in STATUS_ORDER:
                errors.append(f"分站状态「{values['分站状态']}」无效，允许值：{'、'.join(STATUS_ORDER)}")

            access_time = values["接入时间"] or _today()
            if values["接入时间"] and not _valid_date(access_time):
                errors.append("接入时间需使用 YYYY-MM-DD 格式")

            code = values["分站编号"]
            address = values["通信地址"]
            name = values["分站名称"]
            is_duplicate = False
            if code and code in code_owner:
                is_duplicate = True
                duplicate_messages.append(f"分站编号与{code_owner[code]}记录重复，仅保留首次导入记录")
            if address and address in address_owner:
                is_duplicate = True
                duplicate_messages.append(f"通信地址与{address_owner[address]}记录重复，仅保留首次导入记录")
            if name and name in name_owner:
                is_duplicate = True
                duplicate_messages.append(f"分站名称与{name_owner[name]}记录重复，仅保留首次导入记录")

            if errors:
                status = "rejected"
                message = "；".join(errors + duplicate_messages + warnings)
            elif is_duplicate:
                status = "duplicate"
                message = "；".join(duplicate_messages + warnings) or "重复分站已跳过"
            else:
                entry = {
                    "id": next_id,
                    "status": station_status,
                    "pending": station_status != STATUS_ORDER[-1],
                    "abnormal": station_status == "通信中断",
                    "分站编号": code,
                    "分站名称": name,
                    "所在位置": values["所在位置"],
                    "通信地址": address,
                    "接入传感器": values["接入传感器"],
                    "传感器数量": declared_count if declared_count is not None else len(sensors),
                    "信号强度": values["信号强度"],
                    "后备电源": values["后备电源"],
                    "分站状态": station_status,
                    "接入时间": access_time,
                }
                valid.append((row_number, entry))
                code_owner[code] = f"第 {row_number} 行"
                address_owner[address] = f"第 {row_number} 行"
                name_owner[name] = f"第 {row_number} 行"
                next_id += 1
                status = "warning" if warnings else "imported"
                message = "；".join(warnings) if warnings else "已入账"

            results.append(
                {
                    "row": row_number,
                    "status": status,
                    "message": message,
                    "data": values,
                }
            )

        # 所有行先校验完，再一次性提交有效行；错误行不阻塞其他行。
        for row_number, entry in valid:
            store.rows(MODULE).append(entry)
            self._create_report(entry)
            for line in results:
                if line["row"] == row_number and line["status"] in {"imported", "warning"}:
                    line["entry"] = self.public_entry(entry)

        imported_count = len(valid)
        duplicate_count = sum(1 for line in results if line["status"] == "duplicate")
        rejected_count = sum(1 for line in results if line["status"] == "rejected")
        warning_count = sum(1 for line in results if line["status"] == "warning")
        ok = imported_count > 0
        message = f"导入完成：新增 {imported_count} 条，重复跳过 {duplicate_count} 条，整行退回 {rejected_count} 条。"
        if warning_count:
            message += f"其中 {warning_count} 条存在重名提示。"
        if not results:
            ok = False
            message = "导入文件没有可处理的数据行。"

        return {
            "ok": ok,
            "message": message,
            "total": len(results),
            "imported_count": imported_count,
            "duplicate_count": duplicate_count,
            "rejected_count": rejected_count,
            "warning_count": warning_count,
            "fields": IMPORT_FIELDS,
            "results": results,
        }

    def _file_error(self, problems: list[str]) -> dict[str, Any]:
        return {
            "ok": False,
            "message": "；".join(problems),
            "total": 0,
            "imported_count": 0,
            "duplicate_count": 0,
            "rejected_count": 0,
            "warning_count": 0,
            "fields": IMPORT_FIELDS,
            "file_errors": problems,
            "results": [],
        }
