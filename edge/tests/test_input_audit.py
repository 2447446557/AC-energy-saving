"""输入溯源审计测试"""

from __future__ import annotations

from app.services.batch_import import parse_runtime_file
from tests.test_batch_upload import _site_trend_excel_bytes


def test_input_audit_tracks_outdoor_from_multi_header():
    parsed = parse_runtime_file(_site_trend_excel_bytes(), "trend.xlsx")
    row = parsed["rows"][1]
    audit = row["input_audit"]
    outdoor = next(f for f in audit["fields"] if f["field"] == "outdoor_temp")
    assert outdoor["source"] == "excel_multi_header"
    assert outdoor["value"] == 30.9
    assert "outdoor_temp" in audit["from_excel"]
