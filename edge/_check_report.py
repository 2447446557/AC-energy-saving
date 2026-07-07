import json, time, os
p = r'd:\project\AC-energy-saving\edge\_final_report.json'
for attempt in range(30):
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding='utf-8'))
            print("REPORT:")
            print(json.dumps(d, indent=2, ensure_ascii=False))
            break
        except Exception as e:
            print(f"file exists but parse failed: {e}")
            time.sleep(2)
    else:
        print(f"waiting... {attempt}")
        time.sleep(3)
else:
    print("TIMEOUT - report not ready")
