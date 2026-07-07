"""详细排查：realtime API 实际返回的结构"""
import httpx, json

r = httpx.get("http://127.0.0.1:8000/api/v1/data/realtime", timeout=10)
body = r.json()
data = body.get("data", {})

print("=== /api/v1/data/realtime 返回结构 ===")
keys = list(data.keys())
print(f"  keys (first 5): {keys[:5]}")

rd = data.get("raw_data")
print(f"  raw_data type: {type(rd)}")
if isinstance(rd, str):
    print(f"  raw_data length: {len(rd)}")
    try:
        parsed = json.loads(rd)
        print(f"  parsed keys (first 10): {list(parsed.keys())[:10]}")
        print(f"  indoor_temp = {parsed.get('indoor_temp')}")
        print(f"  total_power = {parsed.get('total_power')}")
    except Exception as e:
        print(f"  cannot parse JSON: {e}")
elif isinstance(rd, dict):
    print(f"  indoor_temp = {rd.get('indoor_temp')}")
    print(f"  total_power = {rd.get('total_power')}")
else:
    print(f"  raw_data = {repr(rd)[:100]}")

# 显示非 raw_data 字段
for k, v in data.items():
    if k != "raw_data":
        print(f"  {k} = {repr(v)[:80]}")

# 测试寻优
print("\n=== 测试：用解析后的数据寻优 ===")
payload = {}
if isinstance(rd, str):
    try:
        payload = json.loads(rd)
    except Exception:
        pass
elif isinstance(rd, dict):
    payload = rd

if payload:
    print(f"  payload keys (first 10): {list(payload.keys())[:10]}")
    r2 = httpx.post("http://127.0.0.1:8000/api/v1/optimize/run",
                     json={"device_data": payload}, timeout=60)
    print(f"  optimize HTTP status: {r2.status_code}")
    if r2.status_code == 200:
        result = r2.json()
        res = result.get("data", {})
        print(f"  result status: {res.get('status')}")
        print(f"  energy_saving_rate: {res.get('energy_saving_rate')}%")
        print(f"  chilled_water_temp: {res.get('chilled_water_temp')}")
        print(f"  chilled_pump_freq: {res.get('chilled_pump_freq')}")
        print(f"  cooling_tower_fan_freq: {res.get('cooling_tower_fan_freq')}")
else:
    print("  无 payload")
