import random
import time
import json
import uuid
import os
import threading
from datetime import datetime, timezone

LOG_DIR = "./logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "aiops.json.log")

active_scenario = None
stop_event = threading.Event()

def get_timestamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

def generate_base_log():
    # 嚴格遵循 PRD 定義的 Schema 欄位[cite: 3]
    return {
        "timestamp": get_timestamp(),
        "level": "ERROR",
        "service_name": "payment-service",
        "trace_id": f"txn-{uuid.uuid4().hex[:8]}",
        "status_code": 500,
        "duration_ms": random.randint(10, 50),
        "error_type": "UnknownError",
        "error_message": "An error occurred",
        "source_ip": "192.168.1.100",
        "user_id": f"user_mock_{random.randint(100,999)}",
        "downstream_service": None,
        "external_service": None,
        "transaction_id": f"txn_mock_{random.randint(1000,9999)}",
        "memory_usage_pct": None,
        "target_service": None,
        "rate_limit_quota": None
    }

def write_log(log_dict):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_dict) + "\n")

def log_worker():
    print("🟢 [Logs] JSON 寫入引擎啟動")
    while True:
        if not stop_event.is_set():
            log = generate_base_log()
            
            if active_scenario == "1": # 單點爆破
                log.update({"service_name": "auth-api", "status_code": 401, "error_type": "AuthFailed", "error_message": "Login failed: incorrect password."})
            elif active_scenario == "2": # DB 卡頓引發雪崩
                trace = log["trace_id"]
                log.update({"downstream_service": "core-db", "error_type": "ConnectionTimeout", "duration_ms": 5000})
                write_log(log) # 寫入 DB 逾時
                log2 = generate_base_log()
                log2.update({"trace_id": trace, "service_name": "api-gateway", "status_code": 504, "error_type": "GatewayTimeout"})
                write_log(log2) # 寫入 Gateway 504
                time.sleep(1)
                continue
            elif active_scenario == "4": # 外部依賴
                log.update({"external_service": "Bank_Gateway_API", "error_type": "ExternalTimeout"})
            elif active_scenario == "5": # 網路瞬斷
                log.update({"downstream_service": "core-db", "error_type": "ConnectionRefused", "error_message": "DB connection refused"})
            elif active_scenario is None:
                # 🟢 正常基線：每隔幾秒發送一筆健康的 200 OK 日誌
                log.update({"level": "INFO", "status_code": 200, "error_type": None, "error_message": "Request successful", "duration_ms": random.randint(10, 50)})
            
            write_log(log)
            # 發生災難時每 0.5 秒噴一次，平時正常基線每 2 秒跳一次心跳
            time.sleep(0.5 if active_scenario else 2.0)
        else:
            time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=log_worker, daemon=True).start()
    while True:
        print("\n=== Logs 模擬器 ===")
        print("[1-6] 啟動各劇本 Log 轟炸 | [0] 停止發送 | [q] 退出")
        choice = input("👉 選擇指令: ").strip()
        if choice in ["1", "2", "3", "4", "5", "6"]:
            active_scenario = choice
            stop_event.clear()
        elif choice == "0": stop_event.set(); active_scenario = None
        elif choice == "q": break