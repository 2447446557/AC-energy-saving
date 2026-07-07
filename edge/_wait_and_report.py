"""轮询等待 + 最终数据量确认"""
import time
import subprocess
import sys
from app.models.database import get_session
from sqlmodel import text

# 等待 120 秒看结果
for i in range(120):
    try:
        with open(r'd:\project\AC-energy-saving\edge\data\edge.db', 'rb') as f:
            pass
    except Exception:
        pass
    time.sleep(1)
    # 每 10 秒查一次数
    if i % 10 == 0:
        try:
            from app.models.database import get_session as _get_session
            with _get_session() as s:
                print(f"[{i}s] ", end="")
                for tname in ["runtime_data", "optimize_record", "alarm_log", "operation_log"]:
                    try:
                        c = s.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar_one()
                        print(f"{tname}={c} ", end="")
                    except Exception:
                        pass
                print()
        except Exception as e:
            print(f"[{i}s] query err: {e}")

# 最终统计
print("\n=== 最终 edge.db 统计 ===")
try:
    with get_session() as s:
        for tname in ["runtime_data", "optimize_record", "alarm_log", "operation_log"]:
            c = s.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar_one()
            print(f"  {tname}: {c} 条")
        # 按 source 分组
        print("\n=== runtime_data 按来源分组 ===")
        r = s.execute(text("SELECT source, COUNT(*) FROM runtime_data GROUP BY source")).all()
        for row in r:
            print(f"  {row[0]}: {row[1]} 条")

        # 按 status 分组 optimize_record
        print("\n=== optimize_record 按 status 分组 ===")
        r = s.execute(text("SELECT status, COUNT(*) FROM optimize_record GROUP BY status")).all()
        for row in r:
            print(f"  {row[0]}: {row[1]} 条")

        # 节能率统计
        print("\n=== optimize_record 节能率统计 ===")
        r = s.execute(text("SELECT MIN(energy_saving_rate), AVG(energy_saving_rate), MAX(energy_saving_rate) FROM optimize_record WHERE status='success'")).one()
        print(f"  min={r[0]:.1f}%  avg={r[1]:.1f}%  max={r[2]:.1f}%")

        # file size
        import os
        size_bytes = os.path.getsize(r'd:\project\AC-energy-saving\edge\data\edge.db')
        print(f"\nedge.db 文件大小: {size_bytes/1024:.1f} KB ({size_bytes/1024/1024:.2f} MB")

except Exception as e:
    print(f"ERR: {e}")

print("\nDONE")
