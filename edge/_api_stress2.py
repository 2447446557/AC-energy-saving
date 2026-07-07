"""修正后的 API / 存储压力测试：写入大量数据到 edge.db"""
from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8000"
REPORT_PATH = Path(r"D:\project\AC-energy-saving\edge\_api_report2.json")

issues = []

def log_issue(section, detail):
    issues.append(f"[{section}] {detail}")

def parse_resp(r):
    """解析统一返回体"""
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        body = r.json()
    except Exception as e:
        return None, f"JSON parse fail: {e}"
    if body.get("code") != 0:
        return None, f"code={body.get('code')} msg={body.get('message')}"
    return body.get("data"), None

# ========== 1. 基本健康检查 ==========
print("========== 1. 健康检查 ==========")
r = httpx.get(f"{BASE_URL}/api/v1/system/health", timeout=5)
print(f"  health: {r.status_code} {r.text[:100]}")

r = httpx.get(f"{BASE_URL}/api/v1/system/version", timeout=5)
data, err = parse_resp(r)
print(f"  version: {err or data}")

# ========== 2. 模拟生成 + 寻优正常流程 ==========
print("\n========== 2. 模拟数据生成 + 寻优正常流程 ==========")

# 用 simulate 接口生成数据
for i in range(30):
    r = httpx.post(f"{BASE_URL}/api/v1/data/simulate", timeout=10)
    data, err = parse_resp(r)
    if err:
        log_issue(f"simulate_{i}", err)
    # 拿到模拟数据后，触发寻优
    if data:
        r2 = httpx.post(f"{BASE_URL}/api/v1/optimize/run", json={"device_data": data}, timeout=30)
        d2, err2 = parse_resp(r2)
        if err2:
            log_issue(f"optimize_good_{i}", err2)
        elif d2:
            status = d2.get("status")
            if status not in ("success", "failed"):
                log_issue(f"optimize_status_{i}", f"unexpected status={status}")

print(f"  simulate+optimize 30次: issues={len(issues)}")

# ========== 3. 向 edge.db 写入大量数据（用真实 API: /simulate） ==========
print("\n========== 3. 写入 2000 条模拟数据到 edge.db ==========")

from app.services.hospital_simulator import HospitalDataGenerator, AnomalyConfig
from app.models.database import init_db
from app.services.storage import storage  # 实际的全局实例

init_db()
gen = HospitalDataGenerator(
    seed=2024,
    anomaly=AnomalyConfig(sensor_spike=0.12, data_dropout=0.10, load_surge=0.05),
)

saved = 0
failed = 0
start_t = time.time()
for i in range(2000):
    if i % 200 == 0:
        print(f"  写入 {i}/2000 ... (elapsed {time.time()-start_t:.1f}s)")
    try:
        d = gen.generate()
        # 正确的签名: save_runtime_data(data_time, source, raw_data)
        ts = datetime.fromisoformat(d.timestamp) if d.timestamp else datetime.now()
        raw = json.dumps(d.model_dump(mode="json"), ensure_ascii=False, default=str)
        rec = storage.save_runtime_data(ts, "stress_test", raw)
        if rec is None:
            failed += 1
        else:
            saved += 1
    except Exception as e:
        failed += 1
        if failed < 20:  # 只记录前 20 个
            log_issue(f"storage_save_{i}", f"{type(e).__name__}: {e}")

print(f"  storage: saved={saved} failed={failed} elapsed={time.time()-start_t:.1f}s")

# ========== 4. 再跑 300 次寻优（各种工况） ==========
print("\n========== 4. 300 次寻优压力测试（正常+极端+缺失） ==========")
opt_ok = 0
opt_fail = 0
opt_bad = 0

# 4a. 100 次正常工况
for i in range(100):
    d = gen.generate()
    try:
        r = httpx.post(f"{BASE_URL}/api/v1/optimize/run", json={"device_data": d.model_dump(mode="json")}, timeout=30)
        data, err = parse_resp(r)
        if err:
            opt_fail += 1
            if opt_fail < 20:
                log_issue(f"opt_normal_{i}", err)
            continue
        if data and data.get("status") == "success":
            opt_ok += 1
            # 检查最优参数是否越界
            params = {
                "chilled_water_temp": data.get("chilled_water_temp"),
                "chilled_pump_freq": data.get("chilled_pump_freq"),
                "cooling_pump_freq": data.get("cooling_pump_freq"),
                "cooling_tower_fan_freq": data.get("cooling_tower_fan_freq"),
            }
            bounds = {
                "chilled_water_temp": (6.0, 12.0),
                "chilled_pump_freq": (25.0, 50.0),
                "cooling_pump_freq": (25.0, 50.0),
                "cooling_tower_fan_freq": (20.0, 45.0),
            }
            for key, val in params.items():
                if val is None or not isinstance(val, (int, float)):
                    opt_bad += 1
                    if opt_bad < 20:
                        log_issue(f"opt_normal_{i}", f"{key}={val} 非法")
                elif not math.isfinite(val):
                    opt_bad += 1
                    if opt_bad < 20:
                        log_issue(f"opt_normal_{i}", f"{key}={val} 非有限值")
                else:
                    lo, hi = bounds[key]
                    if val < lo - 1e-6 or val > hi + 1e-6:
                        opt_bad += 1
                        if opt_bad < 20:
                            log_issue(f"opt_normal_{i}", f"{key}={val} 越界 [{lo},{hi}]")
        else:
            opt_fail += 1
    except Exception as e:
        opt_fail += 1
        if opt_fail < 20:
            log_issue(f"opt_normal_except_{i}", str(e))

# 4b. 100 次极端工况
extreme_templates = [
    {"outdoor_temp": 50.0, "outdoor_humidity": 99.0, "indoor_temp": 32.0, "indoor_load": 100.0,
     "chiller_load": 100.0, "chiller_power": 100.0, "chilled_water_temp": 12.0, "cooling_water_temp": 45.0,
     "chilled_pump_freq": 50.0, "chilled_pump_power": 8.0, "cooling_pump_freq": 50.0, "cooling_pump_power": 8.0,
     "cooling_tower_fan_freq": 45.0, "cooling_tower_fan_power": 5.0, "terminal_fan_power": 6.0, "total_power": 127.0},
    {"outdoor_temp": -30.0, "outdoor_humidity": 5.0, "indoor_temp": 10.0, "indoor_load": 0.0,
     "chiller_load": 0.0, "chiller_power": 0.0, "chilled_water_temp": 6.0, "cooling_water_temp": 10.0,
     "chilled_pump_freq": 25.0, "chilled_pump_power": 0.0, "cooling_pump_freq": 25.0, "cooling_pump_power": 0.0,
     "cooling_tower_fan_freq": 20.0, "cooling_tower_fan_power": 0.0, "terminal_fan_power": 0.0, "total_power": 0.0},
    {"outdoor_temp": 35.0, "outdoor_humidity": 80.0, "indoor_temp": 28.0, "indoor_load": 90.0,
     "chiller_load": 85.0, "chiller_power": 40.0, "chilled_water_temp": 9.0, "cooling_water_temp": 35.0,
     "chilled_pump_freq": 45.0, "chilled_pump_power": 6.0, "cooling_pump_freq": 45.0, "cooling_pump_power": 5.5,
     "cooling_tower_fan_freq": 40.0, "cooling_tower_fan_power": 3.5, "terminal_fan_power": 4.0, "total_power": 59.0},
]

for i in range(100):
    base = extreme_templates[i % len(extreme_templates)].copy()
    base["timestamp"] = (datetime.now() + timedelta(seconds=i)).isoformat()
    # 添加一些随机扰动
    for k in ["outdoor_temp", "indoor_temp", "chilled_water_temp"]:
        if k in base and isinstance(base[k], (int, float)):
            base[k] += random.uniform(-2.0, 2.0)
    try:
        r = httpx.post(f"{BASE_URL}/api/v1/optimize/run", json={"device_data": base}, timeout=30)
        data, err = parse_resp(r)
        if err:
            opt_fail += 1
            continue
        if data and data.get("status") == "success":
            opt_ok += 1
        else:
            opt_fail += 1
    except Exception as e:
        opt_fail += 1

# 4c. 100 次缺失/部分字段
partial_cases = []
for i in range(100):
    base = {
        "timestamp": (datetime.now() + timedelta(seconds=i)).isoformat(),
        "outdoor_temp": random.uniform(10, 40),
        "outdoor_humidity": random.uniform(30, 90),
        "indoor_temp": random.uniform(18, 30),
        "indoor_load": random.uniform(20, 95),
        "chiller_load": random.uniform(20, 90),
        "chiller_power": random.uniform(5, 50),
        "chilled_water_temp": random.uniform(6, 12),
        "cooling_water_temp": random.uniform(15, 40),
        "chilled_pump_freq": random.uniform(25, 50),
        "chilled_pump_power": random.uniform(1, 8),
        "cooling_pump_freq": random.uniform(25, 50),
        "cooling_pump_power": random.uniform(1, 8),
        "cooling_tower_fan_freq": random.uniform(20, 45),
        "cooling_tower_fan_power": random.uniform(0.5, 5),
        "terminal_fan_power": random.uniform(0.5, 6),
        "total_power": random.uniform(10, 80),
    }
    # 随机删除 2-4 个字段
    keys_to_del = random.sample([k for k in base.keys() if k != "timestamp"], random.randint(2, 4))
    for k in keys_to_del:
        del base[k]
    partial_cases.append(base)

for i, case in enumerate(partial_cases):
    try:
        r = httpx.post(f"{BASE_URL}/api/v1/optimize/run", json={"device_data": case}, timeout=30)
        data, err = parse_resp(r)
        if err:
            opt_fail += 1
            continue
        if data and data.get("status") == "success":
            opt_ok += 1
        else:
            opt_fail += 1
    except Exception as e:
        opt_fail += 1

print(f"  optimize 300次: ok={opt_ok} failed={opt_fail} bad_params={opt_bad}")

# ========== 5. 查询接口压力测试 ==========
print("\n========== 5. 查询接口压力测试 ==========")
queries = [
    ("realtime", f"{BASE_URL}/api/v1/data/realtime", {}),
    ("simulate_status", f"{BASE_URL}/api/v1/data/simulate/status", {}),
    ("optimize_latest", f"{BASE_URL}/api/v1/optimize/latest", {}),
    ("optimize_history_p1", f"{BASE_URL}/api/v1/optimize/history", {"page": 1, "page_size": 20}),
    ("optimize_history_p5", f"{BASE_URL}/api/v1/optimize/history", {"page": 5, "page_size": 50}),
    ("optimize_history_page_999", f"{BASE_URL}/api/v1/optimize/history", {"page": 999, "page_size": 100}),
]

query_ok = 0
query_fail = 0
for name, url, params in queries * 5:  # 每个查 5 次
    try:
        r = httpx.get(url, params=params, timeout=10)
        data, err = parse_resp(r)
        if err:
            query_fail += 1
            log_issue(f"query_{name}", err)
        else:
            query_ok += 1
    except Exception as e:
        query_fail += 1
        log_issue(f"query_{name}", str(e))

print(f"  query: ok={query_ok} failed={query_fail}")

# ========== 6. 直接向 edge.db 写入更多运行数据（模拟真实采集） ==========
print("\n========== 6. 追加 1500 条运行数据到 edge.db ==========")
saved2 = 0
for i in range(1500):
    if i % 300 == 0:
        print(f"  写入 {i}/1500 ...")
    try:
        d = gen.generate()
        ts = datetime.fromisoformat(d.timestamp) if d.timestamp else datetime.now()
        raw = json.dumps(d.model_dump(mode="json"), ensure_ascii=False, default=str)
        rec = storage.save_runtime_data(ts, "stress_test2", raw)
        if rec:
            saved2 += 1
        else:
            if saved2 + (i - saved2) < 20:
                log_issue(f"storage2_{i}", "返回 None")
    except Exception as e:
        if i < 20:
            log_issue(f"storage2_except_{i}", str(e))

print(f"  storage2: saved={saved2}")

# ========== 7. 校验 edge.db 中真实有数据 ==========
print("\n========== 7. edge.db 数据量校验 ==========")
try:
    latest = storage.get_latest_runtime_data()
    if latest:
        print(f"  最新一条 runtime_data: id={latest.id} time={latest.data_time} source={latest.source}")
        # 解析 raw_data
        try:
            parsed = json.loads(latest.raw_data) if latest.raw_data else {}
            print(f"  raw_data keys: {list(parsed.keys())[:10]}")
        except Exception as e:
            log_issue("raw_data_parse", f"{type(e).__name__}: {e}")
    else:
        log_issue("runtime_data_empty", "edge.db 中无 runtime_data 记录")
except Exception as e:
    log_issue("runtime_data_query", f"{type(e).__name__}: {e}")

try:
    latest_opt = storage.get_latest_optimize_record()
    if latest_opt:
        print(f"  最新一条 optimize_record: id={latest_opt.id} task_id={latest_opt.task_id} status={latest_opt.status}")
    else:
        log_issue("optimize_record_empty", "edge.db 中无 optimize_record 记录")
except Exception as e:
    log_issue("optimize_record_query", str(e))

# ========== 8. 汇总 ==========
print("\n============ 汇总 ============")
print(f"总问题数: {len(issues)}")
if issues:
    for issue in issues[:80]:
        print(f"  - {issue}")
else:
    print("  无问题")

with open(REPORT_PATH, "w", encoding="utf-8") as f:
    json.dump({
        "issues": issues,
        "storage1": {"saved": saved, "failed": failed},
        "storage2": {"saved": saved2},
        "optimize": {"ok": opt_ok, "fail": opt_fail, "bad_params": opt_bad},
        "query": {"ok": query_ok, "fail": query_fail},
    }, f, indent=2, ensure_ascii=False)
print(f"\n报告: {REPORT_PATH}")
