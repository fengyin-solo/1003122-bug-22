"""监测分站导入、导出和链路运行报表的业务规则回归测试。

这些测试只依赖标准库，可在 backend 目录执行 `python3 -m unittest discover -s tests`。
"""
from __future__ import annotations

import unittest

from app.services.monitorstation import IMPORT_FIELDS, MonitorstationService


def csv_row(*values: str) -> str:
    return ",".join(values)


class MonitorstationImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MonitorstationService()

    def make_csv(self, *rows: str) -> bytes:
        return (",".join(IMPORT_FIELDS) + "\n" + "\n".join(rows) + "\n").encode("utf-8")

    def test_valid_row_imports_and_invalid_row_is_returned(self) -> None:
        valid = csv_row("TEST-OK", "有效站", "回风巷", "ADDR-OK", "S1、S2", "2", "强", "有", "正常运行", "2026-09-01")
        invalid = csv_row("TEST-BAD", "错误站", "回风巷", "ADDR-BAD", "S1、S2", "1", "强", "有", "正常运行", "2026-09-02")

        result = self.service.import_file(self.make_csv(valid, invalid), "stations.csv")

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(result["results"][1]["status"], "rejected")
        self.assertIn("传感器数量", result["results"][1]["message"])

    def test_repeated_import_keeps_first_record(self) -> None:
        row = csv_row("TEST-ONCE", "只导一次", "回风巷", "ADDR-ONCE", "S1、S2", "2", "强", "有", "正常运行", "2026-09-01")
        result = self.service.import_file(self.make_csv(row), "stations.csv")
        first = result
        second = self.service.import_file(self.make_csv(row), "stations.csv")

        self.assertEqual(first["imported_count"], 1)
        self.assertEqual(second["imported_count"], 0)
        self.assertEqual(second["duplicate_count"], 1)
        self.assertIn("仅保留首次导入记录", second["results"][0]["message"])

    def test_export_roundtrip_does_not_create_records(self) -> None:
        row = csv_row("TEST-ROUND", "往返导入", "回风巷", "ADDR-ROUND", "S1、S2", "2", "强", "有", "正常运行", "2026-09-01")
        self.service.import_file(self.make_csv(row), "stations.csv")
        count = len(self.service.list_entries(page=1, size=10000)[0])

        roundtrip = self.service.import_file(self.service.export_csv().encode("utf-8"), "export.csv")

        self.assertEqual(roundtrip["imported_count"], 0)
        self.assertEqual(roundtrip["rejected_count"], 0)
        self.assertEqual(len(self.service.list_entries(page=1, size=10000)[0]), count)

    def test_status_change_syncs_link_report(self) -> None:
        row = csv_row("TEST-REPORT", "报表同步", "回风巷", "ADDR-REPORT", "S1", "1", "强", "有", "正常运行", "2026-09-01")
        result = self.service.import_file(self.make_csv(row), "stations.csv")
        entry_id = result["results"][0]["entry"]["id"]

        self.service.run_action(entry_id, "通信排查")
        reports, total = self.service.list_reports(keyword="TEST-REPORT", page=1, size=10)

        self.assertEqual(total, 1)
        self.assertEqual(reports[0]["链路状态"], "通信中断")


if __name__ == "__main__":
    unittest.main()
