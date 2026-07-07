"""最终验证：通过 HTTP API 读取数据，确认一切正常"""
import httpx, json

BASE = "http://127.0.0.1:8000"

print("=== 1. 健康检查 ===")
r = httpx.get(f"{BASE}/api/v1/system/health", timeout=10)
print(f"  status={r.status_code} body={r.text[:150]}")

print("\n=== 2. 实时数据 ===")
r = httpx.get(f"{BASE}/api/v1/data/realtime", timeout=10)
if r.status_code == 200:
    body = r.json()
    data = body.get("data", {})
    print(f"  code={body.get('code')}")
    if data:
        print(f"  室内温度: {data.get('indoor_temp')}")
        print(f"  负荷: {data.get('indoor_load')}%")
        print(f"  冷冻水温度: {data.get('chilled_water_temp')}")
        print(f"  冷冻泵频率: {data.get('chilled_pump_freq')}")
        print(f"  总功率: {data.get('total_power')}")
    else:
        print("  无数据")
else:
    print(f"  ERR: {r.status_code}")

print("\n=== 3. 触发一次寻优 ===")
# 获取实时数据
real = httpx.get(f"{BASE}/api/v1/data/realtime", timeout=10)
data = real.json().get("data")
if data:
    r = httpx.post(f"{BASE}/api/v1/optimize/run", json={"device_data": data}, timeout=60)
    if r.status_code == 200:
        body = r.json()
        result = body.get("data", {})
        print(f"  status={result.get('status')}")
        print(f"  节能率: {result.get('energy_saving_rate')}%")
        print(f"  最优冷冻水温度: {result.get('chilled_water_temp')}")
        print(f"  最优冷冻泵频率: {result.get('chilled_pump_freq')}")
        print(f"  最优冷却塔风机频率: {result.get('cooling_tower_fan_freq')}")
        print(f"  预计功率: {result.get('predicted_power')}")
        print(f"  耗时: {result.get('duration')}s")
    else:
        print(f"  ERR: {r.status_code} {r.text[:150]}")
else:
    print("  无法获取实时数据，用模拟数据替代")

print("\n=== 4. 最近一次寻优结果 ===")
r = httpx.get(f"{BASE}/api/v1/optimize/latest", timeout=10)
if r.status_code == 200:
    body = r.json()
    result = body.get("data")
    if result:
        print(f"  status={result.get('status')}, 节能率={result.get('energy_saving_rate')}%")
    else:
        print("  暂无寻优记录")

print("\n=== 5. 寻优历史（分页） ===")
r = httpx.get(f"{BASE}/api/v1/optimize/history", params={"page": 1, "page_size": 10}, timeout=10)
if r.status_code == 200:
    body = r.json()
    data = body.get("data", {})
    print(f"  total={data.get('total')}, page={data.get('page')}, page_size={data.get('page_size')}")
    items = data.get("items", [])
    print(f"  当前页 {len(items)} 条:")
    for item in items[:3]:
        print(f"    - id={item.get('id')} status={item.get('status')} rate={item.get('energy_saving_rate')}%")

print("\n=== 6. 模拟数据生成接口 ===")
r = httpx.post(f"{BASE}/api/v1/data/simulate", timeout=10)
if r.status_code == 200:
    body = r.json()
    data = body.get("data")
    print(f"  成功生成模拟数据: 室外温度={data.get('outdoor_temp')}")
else:
    print(f"  ERR: {r.status_code}")

r = httpx.get(f"{BASE}/api/v1/data/simulate/status", timeout=10)
print(f"  模拟器状态: {r.status_code}")

print("\n=== 7. 数据库中真实数据量（直接查询） ===")
from app.models.database import get_session
from sqlmodel import text
with get_session() as s:
    for tname in ["runtime_data", "optimize_record", "alarm_log", "operation_log"]:
        c = s.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar_one()
        print(f"  {tname}: {c} 条")

print("\n=== 8. 节能率分布 ===")
with get_session() as s:
    r = s.execute(text(
        "SELECT status, COUNT(*), ROUND(AVG(energy_saving_rate), 2) "
        "FROM optimize_record GROUP BY status"
    )).all()
    for row in r:
        print(f"  {row[0]}: {row[1]} 条, 平均节能率={row[2]}%")

print("\n=== DONE ===")
