"""HTTP API + 存储压力测试脚本"""
from __future__ import annotations

import json
import math
import random
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8000"
REPORT_PATH = Path(r"D:\project\AC-energy-saving\edge\_api_report.json")

issues = []


def log_issue(section, detail):
    msg = f"[{section}] {detail}"
    print(f"  !!! 问题: {msg}")
    issues.append(msg)


# ============ 1. 健康检查 ============
print("\n========== 1. 健康检查 ===========")
try:
    r = httpx.get(f"{BASE_URL}/api/v1/system/health", timeout=5)
    print(f"  status={r.status_code} body={r.text[:120]}")
except Exception as e:
    log_issue("health", f"无法连接服务: {e}")

try:
    r = httpx.get(f"{BASE_URL}/api/v1/system/version", timeout=5)
    print(f"  version: {r.status_code} {r.json() if r.status_code == 200 else r.text[:120]}")
except Exception as e:
    log_issue("version", str(e))

# ============ 2. 正常数据测试 ============
print("\n========== 2. 正常数据（模拟运行工况）写入 + 寻优 ===========")
good_cases = [
    # 典型夏季工况
    {
        "timestamp": "2026-07-06T14:00:00",
        "outdoor_temp": 32.5, "outdoor_humidity": 70.0,
        "indoor_temp": 25.5, "indoor_load": 85.0,
        "chiller_load": 72.0, "chiller_power": 25.0,
        "chilled_water_temp": 7.0, "cooling_water_temp": 30.0,
        "chilled_pump_freq": 40.0, "chilled_pump_power": 4.5,
        "cooling_pump_freq": 40.0, "cooling_pump_power": 4.2,
        "cooling_tower_fan_freq": 38.0, "cooling_tower_fan_power": 2.8,
        "terminal_fan_power": 3.0, "total_power": 39.5,
    },
    # 春秋过渡工况
    {
        "timestamp": "2026-07-06T14:01:00",
        "outdoor_temp": 20.0, "outdoor_humidity": 55.0,
        "indoor_temp": 24.2, "indoor_load": 40.0,
        "chiller_load": 35.0, "chiller_power": 10.0,
        "chilled_water_temp": 8.5, "cooling_water_temp": 22.0,
        "chilled_pump_freq": 30.0, "chilled_pump_power": 1.8,
        "cooling_pump_freq": 28.0, "cooling_pump_power": 1.5,
        "cooling_tower_fan_freq": 25.0, "cooling_tower_fan_power": 0.8,
        "terminal_fan_power": 1.2, "total_power": 15.3,
    },
    # 冬季低负荷
    {
        "timestamp": "2026-07-06T14:02:00",
        "outdoor_temp": 5.0, "outdoor_humidity": 50.0,
        "indoor_temp": 22.0, "indoor_load": 20.0,
        "chiller_load": 15.0, "chiller_power": 5.0,
        "chilled_water_temp": 10.0, "cooling_water_temp": 12.0,
        "chilled_pump_freq": 28.0, "chilled_pump_power": 1.0,
        "cooling_pump_freq": 26.0, "cooling_pump_power": 0.9,
        "cooling_tower_fan_freq": 22.0, "cooling_tower_fan_power": 0.3,
        "terminal_fan_power": 0.8, "total_power": 8.0,
    },
]

for i, case in enumerate(good_cases):
    try:
        # 1. 写入实时数据
        r = httpx.post(f"{BASE_URL}/api/v1/data/save", json=case, timeout=10)
        if r.status_code not in (200, 201):
            log_issue(f"data_save_good{i}", f"status={r.status_code} body={r.text[:200]}")
        # 2. 触发寻优
        r2 = httpx.post(f"{BASE_URL}/api/v1/optimize/run", json={"device_data": case}, timeout=30)
        if r2.status_code not in (200, 201):
            log_issue(f"optimize_good{i}", f"status={r2.status_code} body={r2.text[:200]}")
        else:
            body = r2.json()
            status = body.get("status", "?")
            params = body.get("optimal_params", {})
            print(f"  good_case{i}: optimize status={status} params={params}")
    except Exception as e:
        log_issue(f"good_case{i}", f"异常: {type(e).__name__}: {e}")

# ============ 3. 边界值 & 极端值测试 ============
print("\n========== 3. 边界值 / 极端值测试 ===========")
extreme_cases = [
    ("zero_load", {"timestamp": "2026-07-06T15:00:00", "outdoor_temp": 25.0, "outdoor_humidity": 50.0,
                    "indoor_temp": 25.0, "indoor_load": 0.0, "chiller_load": 0.0, "chiller_power": 0.0,
                    "chilled_water_temp": 7.0, "cooling_water_temp": 25.0,
                    "chilled_pump_freq": 25.0, "chilled_pump_power": 0.0,
                    "cooling_pump_freq": 25.0, "cooling_pump_power": 0.0,
                    "cooling_tower_fan_freq": 20.0, "cooling_tower_fan_power": 0.0,
                    "terminal_fan_power": 0.0, "total_power": 0.0}),
    ("max_load", {"timestamp": "2026-07-06T15:01:00", "outdoor_temp": 45.0, "outdoor_humidity": 95.0,
                  "indoor_temp": 28.0, "indoor_load": 100.0, "chiller_load": 100.0, "chiller_power": 50.0,
                  "chilled_water_temp": 6.0, "cooling_water_temp": 40.0,
                  "chilled_pump_freq": 50.0, "chilled_pump_power": 8.0,
                  "cooling_pump_freq": 50.0, "cooling_pump_power": 8.0,
                  "cooling_tower_fan_freq": 45.0, "cooling_tower_fan_power": 5.0,
                  "terminal_fan_power": 6.0, "total_power": 77.0}),
    ("negative_power", {"timestamp": "2026-07-06T15:02:00", "outdoor_temp": 30.0, "outdoor_humidity": 60.0,
                        "indoor_temp": 25.0, "indoor_load": 50.0, "chiller_load": 50.0, "chiller_power": -5.0,
                        "chilled_water_temp": 7.0, "cooling_water_temp": 28.0,
                        "chilled_pump_freq": 35.0, "chilled_pump_power": -1.0,
                        "cooling_pump_freq": 35.0, "cooling_pump_power": -1.0,
                        "cooling_tower_fan_freq": 30.0, "cooling_tower_fan_power": -0.5,
                        "terminal_fan_power": -0.3, "total_power": -8.0}),
    ("high_precision", {"timestamp": "2026-07-06T15:03:00", "outdoor_temp": 32.123456789, "outdoor_humidity": 65.123456789,
                        "indoor_temp": 25.123456789, "indoor_load": 77.7777777, "chiller_load": 66.6666666,
                        "chiller_power": 22.2222222, "chilled_water_temp": 7.7777777, "cooling_water_temp": 29.9999999,
                        "chilled_pump_freq": 38.1234567, "chilled_pump_power": 3.3333333,
                        "cooling_pump_freq": 37.9876543, "cooling_pump_power": 3.1111111,
                        "cooling_tower_fan_freq": 33.3333333, "cooling_tower_fan_power": 2.2222222,
                        "terminal_fan_power": 2.7777777, "total_power": 33.6666666}),
]

for name, case in extreme_cases:
    try:
        r = httpx.post(f"{BASE_URL}/api/v1/data/save", json=case, timeout=10)
        if r.status_code not in (200, 201):
            log_issue(f"data_save_{name}", f"status={r.status_code} body={r.text[:200]}")
        r2 = httpx.post(f"{BASE_URL}/api/v1/optimize/run", json={"device_data": case}, timeout=30)
        if r2.status_code not in (200, 201):
            log_issue(f"optimize_{name}", f"status={r2.status_code} body={r2.text[:200]}")
        else:
            body = r2.json()
            print(f"  {name}: status={body.get('status','?')} 最优参数={body.get('optimal_params',{})}")
    except Exception as e:
        log_issue(name, f"异常: {type(e).__name__}: {e}")

# ============ 4. 大量异常数据测试（NaN/Inf/字符串/缺失字段） ============
print("\n========== 4. 大量异常数据 POST 测试 ===========")
bad_cases = [
    ("all_nan", {"timestamp": "2026-07-06T16:00:00", "outdoor_temp": float("nan"), "outdoor_humidity": float("nan"),
                 "indoor_temp": float("nan"), "indoor_load": float("nan"), "chiller_load": float("nan"),
                 "chiller_power": float("nan"), "chilled_water_temp": float("nan"),
                 "cooling_water_temp": float("nan"), "chilled_pump_freq": float("nan"),
                 "chilled_pump_power": float("nan"), "cooling_pump_freq": float("nan"),
                 "cooling_pump_power": float("nan"), "cooling_tower_fan_freq": float("nan"),
                 "cooling_tower_fan_power": float("nan"), "terminal_fan_power": float("nan"),
                 "total_power": float("nan")}),
    ("all_inf", {"timestamp": "2026-07-06T16:01:00", "outdoor_temp": float("inf"), "outdoor_humidity": float("inf"),
                 "indoor_temp": float("inf"), "indoor_load": float("inf"), "chiller_load": float("inf"),
                 "chiller_power": float("inf"), "chilled_water_temp": float("inf"),
                 "cooling_water_temp": float("inf"), "chilled_pump_freq": float("inf"),
                 "chilled_pump_power": float("inf"), "cooling_pump_freq": float("inf"),
                 "cooling_pump_power": float("inf"), "cooling_tower_fan_freq": float("inf"),
                 "cooling_tower_fan_power": float("inf"), "terminal_fan_power": float("inf"),
                 "total_power": float("inf")}),
    ("all_negative_inf", {"timestamp": "2026-07-06T16:02:00", "outdoor_temp": float("-inf"),
                          "outdoor_humidity": float("-inf"), "indoor_temp": float("-inf"),
                          "indoor_load": float("-inf"), "chiller_load": float("-inf"),
                          "chiller_power": float("-inf"), "chilled_water_temp": float("-inf"),
                          "cooling_water_temp": float("-inf"), "chilled_pump_freq": float("-inf"),
                          "chilled_pump_power": float("-inf"), "cooling_pump_freq": float("-inf"),
                          "cooling_pump_power": float("-inf"), "cooling_tower_fan_freq": float("-inf"),
                          "cooling_tower_fan_power": float("-inf"), "terminal_fan_power": float("-inf"),
                          "total_power": float("-inf")}),
    ("string_values", {"timestamp": "2026-07-06T16:03:00", "outdoor_temp": "hot", "outdoor_humidity": "wet",
                       "indoor_temp": "warm", "indoor_load": "high", "chiller_load": "busy",
                       "chiller_power": "strong", "chilled_water_temp": "cold",
                       "cooling_water_temp": "warmish", "chilled_pump_freq": "fast",
                       "chilled_pump_power": "high", "cooling_pump_freq": "slow",
                       "cooling_pump_power": "low", "cooling_tower_fan_freq": "normal",
                       "cooling_tower_fan_power": "medium", "terminal_fan_power": "small",
                       "total_power": "big"}),
    ("empty_string", {"timestamp": "2026-07-06T16:04:00", "outdoor_temp": "", "outdoor_humidity": "",
                      "indoor_temp": "", "indoor_load": "", "chiller_load": "",
                      "chiller_power": "", "chilled_water_temp": "",
                      "cooling_water_temp": "", "chilled_pump_freq": "",
                      "chilled_pump_power": "", "cooling_pump_freq": "",
                      "cooling_pump_power": "", "cooling_tower_fan_freq": "",
                      "cooling_tower_fan_power": "", "terminal_fan_power": "",
                      "total_power": ""}),
    ("partial_fields", {"timestamp": "2026-07-06T16:05:00", "outdoor_temp": 30.0,
                        "indoor_temp": 25.0, "total_power": 30.0}),
    ("missing_total_power", {"timestamp": "2026-07-06T16:06:00", "outdoor_temp": 30.0,
                             "outdoor_humidity": 60.0, "indoor_temp": 25.0, "indoor_load": 70.0,
                             "chiller_load": 60.0, "chiller_power": 20.0,
                             "chilled_water_temp": 7.0, "cooling_water_temp": 28.0,
                             "chilled_pump_freq": 35.0, "chilled_pump_power": 3.0,
                             "cooling_pump_freq": 35.0, "cooling_pump_power": 3.0,
                             "cooling_tower_fan_freq": 30.0, "cooling_tower_fan_power": 2.0,
                             "terminal_fan_power": 2.0}),
]

for name, case in bad_cases:
    # POST data
    try:
        r = httpx.post(f"{BASE_URL}/api/v1/data/save", json=case, timeout=10)
        if r.status_code not in (200, 201):
            log_issue(f"data_save_{name}", f"status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        log_issue(f"data_save_{name}", f"异常: {type(e).__name__}: {e}")
    # POST optimize
    try:
        r = httpx.post(f"{BASE_URL}/api/v1/optimize/run", json={"device_data": case}, timeout=30)
        if r.status_code not in (200, 201):
            log_issue(f"optimize_{name}", f"status={r.status_code} body={r.text[:200]}")
        else:
            body = r.json()
            print(f"  {name}: status={body.get('status', '?')}")
    except Exception as e:
        log_issue(f"optimize_{name}", f"异常: {type(e).__name__}: {e}")

# ============ 5. 恶意构造 payload 测试 ============
print("\n========== 5. 恶意构造 payload 测试 ===========")
malicious_payloads = [
    ("empty_obj", {}),
    ("null", None),
    ("empty_list", []),
    ("huge_list", [1, 2, 3] * 10000),
    ("nested", {"device_data": {"timestamp": "2026-07-06T17:00:00", "nested": {"deep": {"value": 100}}}}),
    ("wrong_type_timestamp", {"device_data": {"timestamp": 1234567890, "outdoor_temp": 30.0,
                                                "outdoor_humidity": 60.0, "indoor_temp": 25.0,
                                                "indoor_load": 70.0, "chiller_load": 60.0,
                                                "chiller_power": 20.0, "chilled_water_temp": 7.0,
                                                "cooling_water_temp": 28.0, "chilled_pump_freq": 35.0,
                                                "chilled_pump_power": 3.0, "cooling_pump_freq": 35.0,
                                                "cooling_pump_power": 3.0, "cooling_tower_fan_freq": 30.0,
                                                "cooling_tower_fan_power": 2.0, "terminal_fan_power": 2.0,
                                                "total_power": 30.0}}),
]

for name, payload in malicious_payloads:
    for ep in ["/api/v1/optimize/run", "/api/v1/data/save"]:
        try:
            r = httpx.post(f"{BASE_URL}{ep}", json=payload, timeout=10)
            if r.status_code == 500:
                log_issue(f"{ep}_{name}", f"500 Internal Server Error: {r.text[:200]}")
        except Exception as e:
            log_issue(f"{ep}_{name}", f"异常: {type(e).__name__}: {e}")
        time.sleep(0.05)

# ============ 6. 批量写入大量模拟数据到 edge.db ============
print("\n========== 6. 批量写入 2000 条模拟数据到 edge.db ===========")
from app.services.hospital_simulator import HospitalDataGenerator, AnomalyConfig
from app.models.database import get_engine, init_db
from app.services.storage import StorageService
from app.models.runtime_data import RuntimeData

init_db()
storage = StorageService()
gen = HospitalDataGenerator(
    seed=2024,
    anomaly=AnomalyConfig(sensor_spike=0.15, data_dropout=0.10, load_surge=0.08),
)

saved = 0
failed = 0
start_t = time.time()

for i in range(2000):
    if i % 100 == 0:
        print(f"  已写入 {i}/2000 ...")
    try:
        d = gen.generate()
        storage.save_runtime_data(d.model_dump(mode="json"))
        saved += 1
    except Exception as e:
        failed += 1
        log_issue(f"storage_save_{i}", f"{type(e).__name__}: {e}")

print(f"  storage: saved={saved} failed={failed} elapsed={time.time()-start_t:.1f}s")

# ============ 7. 历史数据查询测试 ============
print("\n========== 7. 历史数据查询测试 ===========")
test_queries = [
    ("realtime", "/api/v1/data/realtime", {}),
    ("history_20", "/api/v1/data/history", {"limit": 20}),
    ("history_1000", "/api/v1/data/history", {"limit": 1000}),
    ("optimize_latest", "/api/v1/optimize/latest", {}),
    ("optimize_history_p1", "/api/v1/optimize/history", {"page": 1, "page_size": 20}),
    ("optimize_history_p10", "/api/v1/optimize/history", {"page": 10, "page_size": 50}),
    ("optimize_history_overflow", "/api/v1/optimize/history", {"page": 999999, "page_size": 100}),
    ("status_local", "/api/v1/status/local", {}),
    ("system_health", "/api/v1/system/health", {}),
]

for name, ep, params in test_queries:
    try:
        r = httpx.get(f"{BASE_URL}{ep}", params=params, timeout=10)
        if r.status_code not in (200, 201):
            log_issue(f"query_{name}", f"status={r.status_code} body={r.text[:200]}")
        else:
            print(f"  {name}: OK")
    except Exception as e:
        log_issue(f"query_{name}", f"异常: {type(e).__name__}: {e}")

# ============ 8. 再跑 500 次寻优（使用 edge.db 中数据） ============
print("\n========== 8. 500 次寻优压力测试 ===========")
opt_ok = 0
opt_fail = 0
opt_bad = 0
for i in range(500):
    d = gen.generate()
    try:
        r = httpx.post(f"{BASE_URL}/api/v1/optimize/run", json={"device_data": d.model_dump(mode="json")}, timeout=30)
        if r.status_code == 200:
            body = r.json()
            if body.get("status") == "success":
                opt_ok += 1
            else:
                opt_fail += 1
            # 检查最优参数
            params = body.get("optimal_params", {})
            for key, val in params.items():
                try:
                    if val is None or not math.isfinite(val):
                        opt_bad += 1
                        log_issue(f"optimize_{i}", f"{key}={val} 非有限值")
                except Exception:
                    pass
        else:
            opt_fail += 1
            log_issue(f"optimize_http_{i}", f"status={r.status_code} body={r.text[:150]}")
    except Exception as e:
        opt_fail += 1
        log_issue(f"optimize_http_{i}", f"异常: {type(e).__name__}: {e}")

print(f"  optimize: ok={opt_ok} failed={opt_fail} bad_params={opt_bad}")

# ============ 汇总 ============
print("\n\n============ API / 存储测试汇总 ============")
print(f"总问题数: {len(issues)}")
if issues:
    for issue in issues[:100]:
        print(f"  - {issue}")
else:
    print("无问题")

with open(REPORT_PATH, "w", encoding="utf-8") as f:
    json.dump({
        "issues": issues,
        "storage_saved": saved,
        "storage_failed": failed,
        "optimize_ok": opt_ok,
        "optimize_fail": opt_fail,
        "optimize_bad_params": opt_bad,
    }, f, indent=2, ensure_ascii=False)
print(f"\n报告已保存: {REPORT_PATH}")
