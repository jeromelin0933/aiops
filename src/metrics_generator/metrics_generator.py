import time
import random
import threading
from prometheus_client import start_http_server, Gauge

# 定義 Prometheus 指標
METRIC_MEMORY = Gauge('system_memory_usage_pct', 'Memory usage percentage', ['service_name'])
METRIC_LATENCY = Gauge('api_p95_latency_ms', 'p95 API Latency', ['service_name', 'downstream_service'])
METRIC_QPS = Gauge('api_requests_per_sec', 'API Requests per second', ['service_name'])
METRIC_DB_POOL = Gauge('db_pool_active_connections', 'Active DB pool connections')

# 全域狀態
current_scenario = None
memory_leak_pool = 58.0

def metrics_worker():
    global current_scenario, memory_leak_pool
    print("🟢 [Metrics] Prometheus Exporter 已啟動於 Port 8000")
    
    while True:
        # 常態基線
        mem_pct = round(random.uniform(55, 60), 2)
        qps = random.randint(5, 15)
        db_latency = random.randint(15, 30)
        
        if current_scenario == "2": # DB 卡頓
            db_latency = random.randint(4500, 5500)
        elif current_scenario == "3": # OOM[cite: 3]
            memory_leak_pool += random.uniform(5.0, 10.0)
            if memory_leak_pool >= 100.0: memory_leak_pool = 60.0
            mem_pct = round(memory_leak_pool, 2)
        elif current_scenario == "6": # 流量打爆[cite: 3]
            qps = random.randint(250, 320)
        elif current_scenario is None:
            memory_leak_pool = 58.0

        # 更新指標數值
        METRIC_MEMORY.labels(service_name="payment-service").set(mem_pct)
        METRIC_QPS.labels(service_name="gateway-service").set(qps)
        METRIC_LATENCY.labels(service_name="payment-service", downstream_service="core-db").set(db_latency)
        METRIC_DB_POOL.set(random.randint(20, 80))
        
        time.sleep(1.0) # PRD 雖定 15s 抓取，但內部狀態每秒更新以求精準[cite: 3]

if __name__ == "__main__":
    start_http_server(8000) # 於 port 8000 暴露[cite: 3]
    threading.Thread(target=metrics_worker, daemon=True).start()
    
    while True:
        print("\n=== Metrics 模擬器 ===")
        print("[2] DB 卡頓 | [3] OOM 記憶體 | [6] 流量打爆 | [0] 恢復正常 | [q] 退出")
        choice = input("👉 選擇劇本改變指標: ").strip()
        if choice in ["2", "3", "6"]: current_scenario = choice
        elif choice == "0": current_scenario = None
        elif choice == "q": break