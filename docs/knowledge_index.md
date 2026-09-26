# SPEC-014 Candidate-C Knowledge Index Setup

本文件記錄current implemented Candidate-C setup：approved six-document corpus、governed manifest、Google embedding adapter、Chroma index adapter、build／validate／activate／retrieve lifecycle、Knowledge Snapshot及public provenance。這不宣告live-provider PASS、real-RAG PASS、final RCA E2E或Production Ready。

## Closure verification disclosure

**AC-014-X: NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE。** AC-014-X未執行，這不是PASS，也沒有four-member evidence；Approved AC-014-X持續為normative requirement。PM僅授權current repository在此explicit verification exception下closure。

## Setup and credential boundary

安裝`requirements.txt`後使用tracked non-secret config `configs/knowledge_index.yaml`。Provider固定為Google `text-embedding-004`，index固定為ChromaDB。Credential不寫入config、manifest、logs或durable identity。

Live commands使用Google Vertex AI／ADC environment boundary：

```text
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=<non-secret project id>
GOOGLE_CLOUD_LOCATION=<non-secret location>
GOOGLE_APPLICATION_CREDENTIALS=<untracked local credential path, when ADC needs it>
```

Candidate C不建立credential registry、不選key、不做credential failover。SDK retry固定為一次execution。

## Local workflow

```powershell
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml admit
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml rebuild
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml validate --build-id <kbld-id>
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml inspect --build-id <kbld-id>
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml activate --build-id <kbld-id> --operation-key <operation-id> --expected-generation <n>
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml retrieve --operation-key <operation-id> --query <text> --filter <key=value>
```

`admit`驗證schema 1.1 release、exact-six membership、Git source revision、raw bytes、metadata、contentfulness及security。`rebuild`建立immutable staged collection但不activate；`validate`成功後仍需explicit `activate`。`inspect`顯示SQLite semantic authority、Chroma artifact及local readiness。`retrieve`只查詢一次freeze的exact build。

## Authority and generated state

SQLite `var/knowledge_index/knowledge.sqlite3`是Candidate-C durable semantic authority。Chroma只提供index capability，位於`var/knowledge_index/chroma/`；collection listing、mtime或metadata不決定Active/LKG。整個`var/knowledge_index/`皆為generated local state且不得commit、分享或作team baseline。

不得修改或normalize `configs/knowledge_manifest.json`及`docs/knowledge/*.md`以規避hash mismatch。`.gitattributes`固定這些authority artifacts為LF checkout。

## Tests

Credential-free default regression：

```powershell
python -m pytest -q
```

Real Google／Chroma integration只有明確opt-in才執行：

```powershell
$env:RUN_SPEC014_REAL_INTEGRATION="1"
python -m pytest -q tests/test_knowledge_real_integration.py
```

只有actual live execution evidence才能宣稱live Google／real-provider validation PASS。Google adapter存在、Chroma adapter存在、credential-free default regression PASS或該test被skip，都不能冒充live-provider evidence，也不能證明real-RAG或final RCA E2E。
