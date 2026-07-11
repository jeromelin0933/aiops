[README (2).md](https://github.com/user-attachments/files/29916856/README.2.md)
# AIOps Incident-driven Platform

> 東吳大學資訊管理學系 × 富邦人壽產學合作專題

## 專案簡介

本專案旨在建置一套以 **Incident-driven Architecture（事件驅動架構）**
為核心的 AIOps 維運平台。

系統透過 **Logs** 與 **Metrics** 的事件偵測，經由 **Alert Correlation
Engine** 整合為 Incident，再結合 **LLM + RAG** 進行 Root Cause
Analysis（RCA）與修復建議，並透過 **Email 告警通知** 即時通報維運人員，以降低 MTTR（Mean Time To Repair），提升維運效率。

---

## 系統架構

<img width="2084" height="2444" alt="image" src="https://github.com/user-attachments/assets/29211b19-7ae2-4d92-8aa1-fb8964bac6ad" />


---

## 核心功能

- Log Event Detection（Log 事件偵測）
- Metrics Event Detection（Metrics 事件偵測）
- Alert Correlation Engine（告警關聯分析）
- Incident Manager（事件管理）
- LLM + RAG 根因分析（RCA）
- Dashboard 視覺化監控
- Email 告警通知（Gmail SMTP）

---

## 技術架構暫定)

| 類別 | 技術 |
|---|---|
| 程式語言 | Python 3.11 |
| 容器化 | Docker Compose |
| Log 收集 | Grafana Loki + Promtail |
| Metrics 收集 | Prometheus |
| 觀測視覺化 | Grafana |
| AIOps Dashboard | FastAPI + Jinja2 |
| AI 推論 | Gemini 2.5 Flash API |
| RAG 框架 | 還不確定 |
| 機器學習 | Isolation Forest（scikit-learn） |
| Email 通知 | Gmail SMTP |
| 版本控制 | Git + GitHub |

---

## 專案目錄

```text
AIOps-Platform/
├── configs/
├── demo/
├── docker/
├── docs/
├── scripts/
├── src/
├── tests/
├── .gitignore
└── README.md
```

> `src/` 為核心程式碼目錄，後續將依 Phase 逐步建立各模組，不一次建立完整骨架，以降低維護成本並讓架構隨專案自然演進。

---

## Git Flow

```text
main
 │
 └── develop
      ├── feature/log-generator
      ├── feature/metrics
      ├── feature/event-detection
      ├── feature/incident
      ├── feature/rag
      └── feature/dashboard
```

---

## 快速啟動

```bash
# 1. 複製專案
git clone https://github.com/your-repo/AIOps-Platform.git

# 2. 設定環境變數
cp .env.example .env
# 填入 GEMINI_API_KEY、GMAIL_SENDER、GMAIL_APP_PASSWORD 等設定

# 3. 啟動所有服務
docker compose up -d

# 4. 開啟 AIOps Dashboard
# http://localhost:8080

# 5. 開啟 Grafana 觀測介面
# http://localhost:3000
```

---

## 專案工作流程

```text
情境分析與系統規劃
        │
        ▼
Mock Data 建立
        │
        ▼
Prototype 開發
        │
        ▼
階段性成果驗證
        │
        ▼
系統優化與功能擴充
        │
        ▼
PoC 成果驗證
        │
        ▼
專案成果展示
```

---

## 開發原則

- 採用 Incident-driven Architecture。
- 以模組化方式逐步開發，不過度設計。
- 每完成一個 Phase，再補齊對應的 `src` 模組。
- 文件（SDD、ADR、Meeting Minutes）與程式碼同步維護。

---

## 規劃演進方向（v2.0）

- 預測性告警（Predictive Alert）
- 自動修復（Auto Remediation）
- 串流架構升級（Kafka / Flink）

---

## 團隊資訊

- 東吳大學 資訊管理學系
- 富邦人壽產學合作專題
- 專題目標：降低 MTTR、提升維運效率、強化知識傳承。

---

## 授權

本專案僅供學術研究與專題展示使用。
