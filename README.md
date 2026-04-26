# LeaseGenie API — Multi-Agent Lease Extraction

Agentic lease data extraction API built with **FastAPI** and a **local Ollama** LLM, using a **multi-agent architecture** driven by decision-tree playbooks compiled from your BRD master abstraction guides.

---

## What this is

Five specialist agents execute the **strict IF YES / IF NO decision trees** from the BRD's `.docx` master abstraction guides. Each `QUESTION N: ... IF YES → ... IF NO → ...` step from the guides is compiled into a machine-readable `Playbook` — the LLM only answers one narrow question at a time, and **branching is done in code**, not by the model.

This is the key difference from the single-shot or free-form ReAct approaches: the LLM never freelances flow control. It gets asked one yes/no + extract question, the code picks the next question based on the answer, and on we go until a branch terminates with `FINALIZE`, `RECORD_NONE`, `RECORD_LITERAL` (e.g. `"No Recovery"`, `"undated"`), or `FLAG_REVIEW`.

**Result:** 71 field-level decision trees, 100+ questions, reliable branching, consistent never-blank outputs, and per-field audit trails that match the BRD exactly.

---

## The five specialist agents

| Specialist | Fields | Example cross-field rules |
|---|---|---|
| **BasicInfoAgent** | 17 fields — Tenant, Landlord, Building, Suite, Property Name, Address, Dates, Guarantor, RSF, Lease Term | Lease Term (yrs) derived from LCD+LED when playbook returns None; dates normalized to MM/DD/YYYY; `"undated"` literal fallback |
| **FinancialAgent** | 5 fields — Annual Base Rent, Future Rent Steps, Percentage Rent, Breakpoint, Security Deposit | Monthly × 12 arithmetic; currency normalization (`$120,000` → `120000`) |
| **ReimbursementAgent** | 17 fields — CAM, RE Taxes, Insurance, Utilities, Admin Fee, Base Year, Pro-Rata, Gross-Up, Caps, Inclusion/Exclusion | `"No Recovery"` propagation from CAM to dependent reimbursement fields |
| **CriticalAgent** | 16 fields — Terminations, Recapture, Go-Dark, Co-Tenancy, Renewal, ROFO/ROFR, Exclusive Use, Permitted Use, Purchase Option, Relocation | Clause-text preservation, verbatim extraction |
| **OtherClausesAgent** | 16 fields — Allowance, Alteration, Assignment, Casualty, Condemnation, Hazardous Materials, Holdover, Defaults, Parking, Subordination | Allowance output varies based on LCD age (< 1 yr → full disbursement terms; > 1 yr → amount only) |

On top of these runs a **ReconciliationAgent** that does cross-document + cross-field sweeps: RSF mismatch, suite conflict, tenant-name variation, LED < LCD, Lease-Term vs (LED − LCD) arithmetic check, rent changes across amendments, low-confidence rollup, manual-review rollup.

---

## Architecture

For detailed end-to-end flow diagrams (startup, extraction, playbook execution, override, red flags, etc.), see **[`docs/flows/`](./docs/flows/README.md)** — 9 mermaid diagrams covering every major flow in the system.

```
┌──────────┐    ┌────────────────────────────────────────────────────────┐
│  Client  │──▶│                      FastAPI                            │
└──────────┘   │                                                         │
               │  /orders  /tenants/*/documents  /tenants/*/extract      │
               │  /jobs    /tenants/*/abstraction  /fields  /playbooks   │
               │                                                         │
               │                ┌──────────────────┐                     │
               │                │ Celery extract   │                     │
               │                │       task       │                     │
               │                └────────┬─────────┘                     │
               │                         ▼                               │
               │         ┌────────────────────────────────┐              │
               │         │      Coordinator               │              │
               │         │  1. OCR every document         │              │
               │         │  2. Build BM25 corpus          │              │
               │         │  3. Dispatch specialists       │              │
               │         │     in dependency order        │              │
               │         │     with shared_facts cache    │              │
               │         │  4. Run ReconciliationAgent    │              │
               │         └────────┬───────────────────────┘              │
               │                  ▼                                      │
               │   ┌──────────────────────────────────────┐              │
               │   │  5 Specialists (1 per category)      │              │
               │   │  BasicInfo / Financial / Reimb /     │              │
               │   │  Critical / Other                    │              │
               │   └────────┬─────────────────────────────┘              │
               │            ▼                                            │
               │   ┌──────────────────────────────────────┐              │
               │   │    PlaybookExecutor                  │              │
               │   │    (per field, per document)         │              │
               │   │                                      │              │
               │   │  Q1 ── LLM JSON answer ──┐           │              │
               │   │   │                      │           │              │
               │   │   ├─ YES → yes_branch ───┤           │              │
               │   │   └─ NO  → no_branch ────┤           │              │
               │   │                          │           │              │
               │   │  Actions:                │           │              │
               │   │   GOTO Q3      (jump)    │           │              │
               │   │   EXTRACT+GOTO (capture+│           │              │
               │   │                  jump)   │           │              │
               │   │   FINALIZE     (stop)    │           │              │
               │   │   RECORD_NONE  ("None")  │           │              │
               │   │   RECORD_LITERAL ("No    │           │              │
               │   │        Recovery" etc)   │           │              │
               │   │   FLAG_REVIEW  (review)  │           │              │
               │   └──────────────────────────┘           │              │
               │            │                             │              │
               │            ▼                             │              │
               │   ┌──────────────────────────────────────┐              │
               │   │    Ollama (qwen2.5:32b-instruct…)    │              │
               │   │    one JSON answer per question      │              │
               │   └──────────────────────────────────────┘              │
               └─────────────────────────────────────────────────────────┘
```

### Storage layer

Three persistence tiers, each optimised for its access pattern:

| Tier | Backend | Purpose |
|---|---|---|
| **Relational** | **SQL Server 2022** (via pyodbc + ODBC Driver 18) — SQLite fallback for dev | Projects, Properties, Tenants, Documents, FieldValues, FieldOverrides, AuditLog, ExtractionJobs. All transactional writes, the authoritative source of truth. |
| **Vector** | **ChromaDB** (embedded, persists to disk at `data/vector_store/`) | One vector per lease clause, scoped by `tenant_id` + `document_id` + `clause_ref`. Populated on document upload via Ollama `nomic-embed-text` (768-dim). Powers the agent's `semantic_search` tool and acts as a cache to skip re-OCR on re-extraction. |
| **File** | Local filesystem under `uploads/` | Raw PDFs uploaded by users. |

The vector store is populated asynchronously by a separate Celery task (`leasegenie.index_document`) fired off when a document is uploaded, so the upload endpoint returns in milliseconds while OCR + embedding runs in the background. If the vector store is ever unavailable, the `semantic_search` tool silently falls back to BM25 lexical search so extraction never breaks.

---

## How playbooks work

### Source → compiled JSON

Input files (shipped in `data/playbooks_source/`):

- `BASIC_INFORMATION.docx` — 17 field decision trees
- `FINANCIAL_TERMS.docx` — 4 compound field decision trees (Annual Rent, Future Rent Steps, Percentage Rent + Breakpoint, Security Deposit)
- `REIMBURSEMENTS.docx` — 6 compound field decision trees (CAM, Taxes, Insurance, Other Income, Tenant Insurance, Utilities)
- `CRITICAL_LEASE_CLAUSES.docx` — 16 clause-existence flows
- `OTHER_LEASE_CLAUSES.docx` — 16 clause-existence flows with extras
- `Questions.xlsx` — structured metadata: Condition Type, Priority, Output (Currency/Text/Date/Number), Red Flag Logic, Property-Type applicability, 987 keyword mappings

The compiler (`python -m app.agents.playbooks.compiler`) produces `data/playbooks_compiled/<field_id>.json` — **one playbook per field, 71 total** — merging the docx decision tree with the xlsx metadata.

### To edit a field's extraction logic

1. Edit the `.docx` (add/change a QUESTION, tweak IF YES/IF NO branches, etc.) or edit the `Questions.xlsx` row
2. `python -m app.agents.playbooks.compiler`
3. Restart the API

**No code changes required** to add a new field, change a question, or add a new red flag pattern.

### Example compiled playbook (Annual Base Rent)

```json
{
  "field_id": "annual_base_rent",
  "field_name": "Annual Base Rent",
  "category": "Financial Clauses",
  "output_type": "Number",
  "questions": [
    {
      "id": "Q1",
      "condition_type": "Fixed Rent",
      "question_text": "Does the lease have a Summary page?",
      "search_scope": "summary",
      "yes_branch": {"type": "goto", "goto": "Q2"},
      "no_branch":  {"type": "goto", "goto": "Q3"}
    },
    {
      "id": "Q2",
      "condition_type": "Monthly Stated",
      "question_text": "Does the Summary reference base rent terms?",
      "yes_branch": {"type": "extract", "goto": "Q3", "also_extract": true},
      "no_branch":  {"type": "goto", "goto": "Q3"}
    },
    {
      "id": "Q3",
      "condition_type": "Rent Table in Summary or Main Lease",
      "question_text": "Does the body have base rent clauses?",
      "yes_branch": {"type": "extract", "goto": null, "also_extract": true},
      "no_branch":  {"type": "flag"}
    }
  ]
}
```

The executor walks `Q1 → Q2 → Q3`, flipping branches based on YES/NO, aggregating extracts along the way, and terminating on FINALIZE / FLAG / NONE / LITERAL.

---

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 16+ with pgvector** — primary backend (SQLite works as a dev fallback but loses pgvector)
- **Redis 7+** — Celery broker
- **Ollama** — [install](https://ollama.com/download) — serves both the chat model and the embedding model
- **Tesseract** (optional, for scanned PDFs) — `apt install tesseract-ocr` on Linux, [installer](https://github.com/UB-Mannheim/tesseract/wiki) on Windows

All four host services run as native installs. There is no Docker dependency.

### Pull Ollama models

```powershell
# Chat model (extraction)
ollama pull qwen2.5:32b-instruct-q5_K_M

# Embedding model (vector store)
ollama pull nomic-embed-text
```

| Chat model | VRAM | Notes |
|---|---|---|
| `qwen2.5:32b-instruct-q5_K_M` ⭐ | ~22 GB | Recommended |
| `qwen2.5:14b-instruct-q5_K_M` | ~10 GB | Faster |
| `llama3.1:70b-instruct-q4_K_M` | ~48 GB | Highest accuracy |

Embedding model is always `nomic-embed-text` (~768 MB, 768-dim vectors).

---

## Install & run

### Quick start (Windows native)

```powershell
cd leasegenie_api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env — set DATABASE_URL with your real Postgres creds, pick OLLAMA_MODEL
.\scripts\db.ps1 init        # creates extensions + tables
.\scripts\start.ps1          # background mode (default)
# or: .\scripts\start.ps1 -Foreground   # opens 2 new console windows
# API available on http://localhost:8000  (swagger: /docs)
```

`.\scripts\stop.ps1` to shut down. `.\scripts\restart.ps1 -Full` to reinstall requirements and restart. For production-grade auto-start / auto-restart, register as Windows Services with `.\deploy\windows\install-services.ps1` — see `deploy/windows/README.md`.

Precompile the playbooks (also runs automatically on first startup):

```bash
python -m app.agents.playbooks.compiler
# → Compiled 71 playbooks into data/playbooks_compiled/
```

Start Ollama and the API:

```bash
ollama serve &
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive Swagger UI.

### Production (manual — Celery worker + Redis + MSSQL)

```bash
# Ensure MSSQL, Redis, Ollama are running and reachable
# Set CELERY_TASK_ALWAYS_EAGER=false in .env

celery -A app.workers.celery_app:celery_app worker --loglevel=info --concurrency=2 &
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Quick start — one-call PDF extraction

The fastest path from "I have a lease PDF" to "I have the extracted data". The `/extract/pdf` endpoint creates the tenant hierarchy for you, uploads the PDFs, triggers extraction, and returns a job id. Poll for completion, then download the result as JSON or Excel.

### 1. Submit one or more PDFs

The first PDF is treated as the base lease, the rest as amendments in order. Always async.

```bash
curl -X POST "http://localhost:8000/api/v1/extract/pdf?property_type=Office&abstract_type=Full%20Abstract&tenant_name=TechCo" \
  -F "files=@./base_lease.pdf" \
  -F "files=@./amendment_1.pdf" \
  -F "files=@./amendment_2.pdf"

# → 202 Accepted
# {"id": "<job_id>", "tenant_id": "<tid>", "status": "queued", "progress": 0, ...}
```

### 2. Poll for completion

```bash
curl http://localhost:8000/api/v1/jobs/$JOB_ID
# {"status": "running", "progress": 42, "completed_fields": 30, "total_fields": 71, ...}
```

### 3. Fetch the result — JSON

```bash
curl "http://localhost:8000/api/v1/extract/jobs/$JOB_ID/result?format=json" | jq
```

### 3b. Or download as Excel

```bash
curl "http://localhost:8000/api/v1/extract/jobs/$JOB_ID/result?format=xlsx" \
     -o lease_abstraction.xlsx
open lease_abstraction.xlsx   # macOS
```

The Excel file has three sheets:
- **Summary** — tenant info + extraction quality rollup (mean confidence, high/medium/low counts)
- **Lease Abstraction** — the full grid: fields × documents, with concluded value, **confidence % and band**, source document, page, clause number, and supporting clause text for every field. Rows needing review are highlighted orange; confidence cells are colour-banded (green / amber / red).
- **Red Flags** — reconciliation + playbook-declared flags

---

## End-to-end example (stepwise — when you need the hierarchy)

This flow gives you explicit control over project/property/tenant organisation — useful for real portfolios with many tenants.

### 1. Create an order

```bash
curl -X POST http://localhost:8000/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "project_name": "Acme Portfolio Q2",
    "properties": [{
      "name": "123 Main St",
      "property_type": "Office",
      "tenants": [{
        "name": "TechCo Inc",
        "suite_number": "500",
        "abstract_type": "Full Abstract"
      }]
    }]
  }'
```

### 2. Upload lease + amendments

```bash
curl -X POST http://localhost:8000/api/v1/tenants/$TENANT_ID/documents \
  -F "document_type=base_lease" \
  -F "effective_date=2021-01-01" \
  -F "file=@./base_lease.pdf"

curl -X POST http://localhost:8000/api/v1/tenants/$TENANT_ID/documents \
  -F "document_type=amendment" \
  -F "effective_date=2023-06-01" \
  -F "file=@./amendment_1.pdf"
```

### 3. Trigger multi-agent extraction

```bash
curl -X POST http://localhost:8000/api/v1/tenants/$TENANT_ID/extract
# → {"id": "<job_id>", "status": "queued"|"complete", ...}
```

### 4. Poll progress

```bash
curl http://localhost:8000/api/v1/jobs/$JOB_ID
```

### 5. Get the abstraction

```bash
curl http://localhost:8000/api/v1/tenants/$TENANT_ID/abstraction
```

The response now carries **confidence + citation at both the concluded and per-document level**, plus a rolled-up `summary` block:

```json
{
  "tenant_id": "...",
  "tenant_name": "TechCo Inc",
  "fields": [
    {
      "field_id": "annual_base_rent",
      "name": "Annual Base Rent",
      "category": "Financial Clauses",
      "output_type": "Number",

      "concluded_value": "135000",
      "concluded_source": "amendment_1",
      "concluded_confidence": 0.92,
      "confidence_level": "high",
      "source_document_label": "Amendment 1",
      "source_document_id": "abc-123",
      "page_number": 2,
      "clause_number": "5.1",
      "clause_text": "Annual Base Rent is hereby amended to $135,000 …",

      "per_document": [
        {
          "document_label": "Base Lease",
          "value": "120000",
          "confidence": 0.88,
          "page_number": 3,
          "clause_number": "3.1",
          "clause_text": "Tenant shall pay Annual Base Rent of $120,000 …",
          "condition_type_taken": "Fixed Rent",
          "needs_review": false,
          "trace_summary": "Q1(YES)→yes→goto→Q2 | Q2(YES)→yes→extract→Q3 | Q3(YES)→yes→extract→"
        },
        {
          "document_label": "Amendment 1",
          "value": "135000",
          "confidence": 0.92,
          "page_number": 2,
          "clause_number": "5.1",
          "condition_type_taken": "Fixed Rent"
        }
      ]
    }
  ],
  "summary": {
    "total_fields": 71,
    "fields_extracted": 54,
    "fields_none": 17,
    "fields_overridden": 0,
    "fields_flagged_review": 3,
    "mean_confidence": 0.83,
    "high_confidence_count": 41,
    "medium_confidence_count": 11,
    "low_confidence_count": 2
  }
}
```

The `confidence_level` band is derived from `concluded_confidence`:
- `high` ≥ 0.8
- `medium` 0.5 – 0.8
- `low` > 0 but < 0.5
- `none` — 0 (concluded value is "None")

`source_document_label`, `page_number`, `clause_number` point at the **winning document** (the one the precedence rules selected) so the UI can render "Base rent: $135,000 — from Amendment 1, page 2, clause 5.1".

### 6. Introspect a playbook

```bash
curl http://localhost:8000/api/v1/playbooks/cam
# → The full decision tree for CAM: 13 questions, YES/NO branches,
#   "No Recovery" literal action, property applicability, etc.
```

### 7. LeaseLens red flags

```bash
curl http://localhost:8000/api/v1/tenants/$TENANT_ID/red-flags
```

Returns both reconciliation-agent flags (RSF mismatch, date inconsistency, lease-term math mismatch, rent changes across amendments) and per-field playbook-declared flags (the "Red Flag Logic" column from Questions.xlsx).

---

## API reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/extract/pdf` | **One-call wrapper** — PDFs → job_id (auto-creates hierarchy) |
| `GET` | `/api/v1/extract/jobs/{id}/result?format=json\|xlsx` | **Result wrapper** — JSON abstraction or Excel download |
| `POST` | `/api/v1/orders` | Create project → properties → tenants |
| `POST` | `/api/v1/tenants/{id}/documents` | Upload base lease / amendment (fires background vector indexing) |
| `DELETE` | `/api/v1/tenants/{id}/documents/{doc_id}` | Remove doc + vectors |
| `POST` | `/api/v1/tenants/{id}/extract` | Trigger multi-agent extraction |
| `GET` | `/api/v1/jobs/{id}` | Poll extraction progress |
| `GET` | `/api/v1/tenants/{id}/abstraction` | Main output — fields × docs + concluded + confidence + citation |
| `PATCH` | `/api/v1/tenants/{id}/fields/{field_id}` | Manual override (audit logged) |
| `GET` | `/api/v1/tenants/{id}/audit` | Audit log |
| `GET` | `/api/v1/tenants/{id}/red-flags` | LeaseLens |
| `GET` | `/api/v1/fields` | Field list (scoped by abstract × property type) |
| `GET` | `/api/v1/playbooks` | List all 71 compiled playbooks |
| `GET` | `/api/v1/playbooks/{field_id}` | Full decision tree |
| `GET` | `/health` | Service status + playbook counts + model info |

---

## Project layout

```
leasegenie_api/
├── app/
│   ├── main.py                         # FastAPI app; auto-compiles playbooks on startup
│   ├── config.py                       # MSSQL/SQLite + Ollama + ChromaDB + paths
│   ├── api/v1/
│   │   ├── orders.py
│   │   ├── documents.py                # Upload fires background vector indexing
│   │   ├── extraction.py
│   │   ├── abstraction.py
│   │   ├── fields.py
│   │   ├── playbooks.py                # Playbook introspection
│   │   └── extract_pdf.py              # NEW — one-call PDF → job_id + JSON/xlsx result
│   ├── agents/
│   │   ├── ollama_client.py            # JSON-mode Ollama wrapper
│   │   ├── tools.py                    # BM25 + semantic_search tools
│   │   ├── playbooks/
│   │   │   ├── schema.py               # Playbook / PlaybookQuestion / PlaybookAction
│   │   │   ├── compiler.py             # docx + xlsx → compiled JSON
│   │   │   └── loader.py               # compiled JSON → Playbook objects
│   │   ├── playbook_executor.py        # Strict IF YES/IF NO walker
│   │   ├── specialists/
│   │   │   ├── base.py
│   │   │   ├── basic_info.py           # Lease Term math, date normalization
│   │   │   ├── financial.py            # Currency post-processing
│   │   │   ├── reimbursement.py        # CAM "No Recovery" propagation
│   │   │   ├── critical.py
│   │   │   └── other.py                # Allowance LCD-age rule
│   │   ├── coordinator.py              # Multi-agent orchestrator
│   │   └── reconciliation_agent.py     # Cross-doc + cross-field sweeps
│   ├── services/
│   │   ├── ocr.py                      # pdfplumber + Tesseract
│   │   ├── keyword_matcher.py
│   │   ├── concluded_value.py          # override → amendment → base precedence
│   │   │                               #   + citation + confidence propagation
│   │   ├── abstraction.py              # Final grid + summary + red-flag assembly
│   │   ├── pipeline.py                 # DB ↔ Coordinator glue
│   │   ├── embeddings.py               # NEW — Ollama nomic-embed-text wrapper
│   │   ├── vector_store.py             # NEW — ChromaDB persistent store
│   │   ├── doc_indexer.py              # NEW — background OCR + vector upsert
│   │   └── excel_export.py             # NEW — BRD-format xlsx output
│   ├── core/reference_data.py
│   ├── models/orm.py
│   ├── schemas/models.py
│   ├── workers/
│   │   ├── celery_app.py
│   │   └── tasks.py                    # extract_tenant + index_document
│   └── db/session.py                   # MSSQL-aware engine config
├── data/
│   ├── LeaseGenie_BRD.xlsx             # Master field list
│   ├── playbooks_source/               # Source .docx + Questions.xlsx
│   ├── playbooks_compiled/             # Generated on startup if missing
│   └── vector_store/                   # ChromaDB on-disk persistence
├── docs/
│   └── flows/                          # 11 mermaid end-to-end diagrams
├── tests/
│   ├── test_reference_data.py
│   ├── test_concluded_value.py
│   ├── test_playbooks.py               # compiler + loader
│   ├── test_specialists.py             # cross-field rules
│   ├── test_reconciliation.py          # red flags
│   ├── test_vector_store.py            # NEW — vector + embeddings
│   └── test_excel_export.py            # NEW — xlsx layout
├── deploy/windows/                     # Windows Service installer (NSSM / sc.exe)
├── scripts/                            # PowerShell lifecycle: start / restart / stop / db
├── requirements.txt
├── .env.example
└── README.md
```

---

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./leasegenie.db` | Swap for `mssql+pyodbc://user:pass@host:1433/db?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes` in prod |
| `CELERY_TASK_ALWAYS_EAGER` | `true` | `false` = real async via Redis |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | |
| `OLLAMA_MODEL` | `qwen2.5:32b-instruct-q5_K_M` | Must follow JSON mode reliably |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | For vector store indexing |
| `OLLAMA_NUM_CTX` | `32768` | Context window in tokens |
| `OLLAMA_TEMPERATURE` | `0.0` | Deterministic by default |
| `VECTOR_STORE_PATH` | `./data/vector_store` | ChromaDB persistent directory |
| `VECTOR_STORE_COLLECTION` | `lease_clauses` | Collection name |
| `BRD_PATH` | `./data/LeaseGenie_BRD.xlsx` | 72-field catalogue |
| `UPLOAD_DIR` | `./uploads` | PDF storage |
| `EXPORT_DIR` | `./exports` | Generated xlsx storage |

Playbook source files live next to the BRD at `./data/playbooks_source/`. The compiled JSON goes to `./data/playbooks_compiled/`.

---

## BRD coverage

| Requirement | Covered? |
|---|---|
| Order → Project → Property → Tenant hierarchy | ✅ |
| Base lease + up to 7 amendments per tenant | ✅ |
| Field-based extraction via keywords + questions | ✅ Compiled from Questions.xlsx |
| Decision-tree branching from master abstraction guides | ✅ Strict executor, branching in code |
| Monthly→Annual arithmetic for rent | ✅ Deterministic post-processing |
| Summary-first search priority | ✅ `search_scope` per playbook question |
| Defined-terms awareness | ✅ `check_definitions` tool |
| Full-clause fallback when no direct answer | ✅ Never-blank rule |
| `"None"` / `"undated"` / `"No Recovery"` literal fallbacks | ✅ `RECORD_LITERAL` action |
| Cross-field dependencies (Allowance ↔ LCD, Term ↔ LCD+LED) | ✅ `depends_on` + specialist post-processing |
| Amendment supersedes base lease | ✅ Concluded-value engine |
| Property-type gating (retail-only, office-only) | ✅ Playbook applicability matrix |
| LeaseLens red flags (RSF, suite, date, rent, name variation) | ✅ ReconciliationAgent |
| Per-question red-flag patterns | ✅ Propagated from Questions.xlsx "Red Flag Logic" column |
| Hyperlinks to source page + clause | ✅ On every extracted value |
| Manual override + audit trail | ✅ PATCH endpoint + AuditLog |

---

## Troubleshooting

**`No compiled playbooks at data/playbooks_compiled`** — run `python -m app.agents.playbooks.compiler`. It's also run on startup if the directory is empty.

**LLM answers don't follow JSON mode** — qwen2.5 and llama3.1 follow JSON mode reliably. Older or smaller (<7B) models often don't; switch to a recommended model.

**Extraction is slow** — expected: 71 playbooks × N documents × ~5 questions per playbook = thousands of LLM calls per tenant. With `qwen2.5:32b` on an RTX 4090, budget ~40–60 min per tenant. Use `qwen2.5:14b` for ~3× speedup at modest accuracy cost. Use `abstract_type=Basic Economic Abstract` for a 15-field subset when speed matters.

**Agent keeps flagging manual review** — inspect the trace via the abstraction response; each field reports its `trace_summary` like `Q1(YES)→yes→goto→Q2 | Q2(NO)→no→flag`. Follow the last step to find the playbook question the agent stumbled on, then tune the `.docx` and recompile.

---

## License

Proprietary — internal use per LeaseGenie project.
