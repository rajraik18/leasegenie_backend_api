# Step-by-Step: Upload a PDF, Extract Lease Data

A walkthrough of what happens from the moment a lease PDF enters the system until the caller gets back structured data. Concrete example: **Sample_1.pdf** (Advance Stores / ProLogis Pine Timbers Distribution Center industrial lease, 28 pages).

See [`12_step_by_step_upload_to_extraction.mermaid`](./12_step_by_step_upload_to_extraction.mermaid) for the visual version.

---

## Step 1 — Upload the PDF

The caller hits the one-call wrapper:

```bash
curl -X POST "http://localhost:8000/api/v1/extract/pdf?property_type=Industrial&abstract_type=Full%20Abstract&tenant_name=Advance%20Stores" \
  -F "files=@Sample_1.pdf"
```

If there were amendments, they'd be additional `-F "files=@..."` flags in order. The first file is always treated as the base lease.

## Step 2 — API validates and sets up the hierarchy

Inside `extract_pdf.py`:

1. Validate `property_type` is one of the four allowed values (Retail / Industrial / Office / Mixed-Use)
2. Validate `abstract_type` is one of the five BRD-defined abstract types
3. Auto-create a throwaway **Project → Property → Tenant** chain in SQL Server so this PDF has a place to live
4. Write the PDF to disk: `uploads/<tenant_id>/00_Sample_1.pdf`
5. Insert a **Document** row, `ocr_status='pending'`, `document_type='base_lease'`, `document_order=0`

## Step 3 — Two background jobs queued

The API never waits. It queues **two Celery tasks** and returns `202 Accepted` with a `job_id`:

| Task | Purpose |
|---|---|
| `index_document_task` | OCR the PDF, chunk into clauses, embed each, upsert to ChromaDB |
| `extract_tenant_task` | Run the full multi-agent extraction for this tenant |

Response: `{"id": "<job_id>", "status": "queued", "progress": 0}`

## Step 4 — OCR (runs twice, cached where possible)

### 4a — OCR for the vector store

The `index_document_task` opens the PDF with **pdfplumber** to pull out text per page. If a page has less than ~30 characters of clean text (a strong signal that it's scanned), **Tesseract** rasterises the page at 200 dpi and OCRs the image. The resulting per-page text is then segmented into **Clauses** — numbered paragraphs, "Section X" headings, all-caps headings — each with `page_number`, `clause_number`, `heading`, and full text.

> **Real-world note from Sample_1.pdf:** This particular PDF has a *corrupted* text layer. `pdfplumber` gets back things like `Common Area Charges: w` instead of `Common Area Charges: $683.57`. The OCR fallback should actually trigger on pages where Tesseract gives a cleaner result, but that's an adjustment we'd make to the heuristic — by default, the fallback only kicks in when the text layer is *empty*, not when it's garbled.

### 4b — OCR for extraction

The extraction `Coordinator` builds its `DocumentContext` by running the same OCR pipeline. In the current build these don't share a cache; a small improvement would be to have the extraction task wait for the indexing task and reuse its clause list.

## Step 5 — Embed every clause

For every clause in the document, the indexer calls **Ollama's `/api/embeddings`** endpoint with the `nomic-embed-text` model. Returns a 768-dimensional vector per clause. Embedding calls are batched 32 at a time to avoid memory spikes on long leases.

A 28-page lease like Sample_1.pdf typically segments into ~80–150 clauses, so ~3–5 batched embedding calls.

## Step 6 — Upsert to ChromaDB

Each clause is upserted into the persistent ChromaDB collection with:

- **id**: `<tenant_id>:<document_id>:<clause_ref>` (idempotent — re-runs don't duplicate)
- **metadata**: `{tenant_id, document_id, document_label, document_type, page_number, clause_number, heading, clause_ref}`
- **document**: the clause text (truncated to 8,000 chars)
- **embedding**: 768-dim vector

Document's `ocr_status` flips to `indexed`. The vector store is now ready to answer **semantic searches** scoped to this tenant.

## Step 7 — Build BM25 lexical index

In parallel, the extraction task builds a **BM25Okapi** index across the tokenised clauses of all documents for this tenant (base lease + any amendments). BM25 handles exact-word matches (good for "base rent", "CAM", "operating expenses"), while the vector store handles semantic matches (good for paraphrased concepts).

Both indices are available to every agent as tools.

## Step 8 — Coordinator dispatches the five specialist agents

The `Coordinator` runs each specialist in dependency order so facts published by earlier agents are available to later ones:

| Order | Specialist | Fields | Notable cross-field rules |
|---|---|---|---|
| 1 | **BasicInfoAgent** | 17 | Publishes LCD, LED, RSF, Tenant Name to `shared_facts` |
| 2 | **FinancialAgent** | 5 | Currency normalisation, monthly×12 |
| 3 | **ReimbursementAgent** | 17 | CAM "No Recovery" propagates to dependent fields |
| 4 | **CriticalAgent** | 16 | Clause-text preservation; retail-only fields like Co-Tenancy gated |
| 5 | **OtherClausesAgent** | 16 | Allowance reads LCD age from shared_facts |

Each specialist loads only the playbooks applicable to this `abstract_type × property_type` combination. For Sample_1.pdf (Industrial, Full Abstract), **Co-Tenancy is skipped entirely** because the playbook's `property_applicability` matrix restricts it to Retail.

## Step 9 — For each applicable field × each document

Within each specialist, the loop is:

```
for each playbook in specialist.applicable_playbooks:
    for each document in tenant.documents:
        outcome = playbook_executor.run(playbook, context, doc_label)
        specialist.post_process(outcome, shared_facts)
        publish result via on_result callback
```

## Step 10 — Walk the playbook decision tree

This is the core innovation. For **one field on one document**, the `PlaybookExecutor` walks the compiled decision tree:

```
current_qid = playbook.first_question.id
while current_qid and steps < MAX_STEPS:
    q = playbook.question(current_qid)
    clauses = search(q, scope=summary|definitions|body|all)   # BM25 + semantic
    answer = ollama.json_chat(q, clauses)                      # YES/NO + extract
    branch = q.yes_branch if answer == YES else q.no_branch

    match branch.type:
        case GOTO:            current_qid = branch.goto
        case EXTRACT:         capture value, current_qid = branch.goto
        case FINALIZE:        break
        case RECORD_LITERAL:  best_value = branch.literal        # "No Recovery" / "undated"
        case RECORD_NONE:     best_value = "None"
        case FLAG_REVIEW:     needs_review = True, break
```

**The LLM never controls flow**. It only answers one narrow YES/NO + extract question at a time. Branching is deterministic.

## Step 11 — Concrete example: Annual Base Rent on Sample_1.pdf

The compiled playbook for `annual_base_rent` has 3 questions. Here's how it would walk:

| Step | Question | LLM answer | Branch | Next |
|---|---|---|---|---|
| Q1 | "Does the lease have a Summary page?" (scope: summary) | **YES** — found header block p.1 with "Initial Monthly Base Rent" | `yes_branch: goto Q2` | Q2 |
| Q2 | "Does the Summary reference base rent terms?" (scope: summary) | **YES** — "See Addendum 1" found p.1 | `yes_branch: extract + goto Q3` — captures partial ref | Q3 |
| Q3 | "Does the body have base rent clauses?" (scope: body) | **YES** — Addendum 1 table p.17 with $8,882.60 and $9,655.00 | `yes_branch: extract + finalize` | done |

Final result the executor returns:

```json
{
  "field_id": "annual_base_rent",
  "value": "106591.20",
  "raw_value": "$8,882.60 per month",
  "confidence": 0.92,
  "page_number": 17,
  "clause_number": "Addendum 1",
  "clause_text": "August 1, 2009 through July 31, 2012 $8,882.60",
  "output_type": "Number",
  "condition_type_taken": "Monthly Stated",
  "trace": ["Q1(YES)→goto→Q2", "Q2(YES)→extract→Q3", "Q3(YES)→extract→finalize"]
}
```

The FinancialAgent's post-processing then recognises `is_monthly=true` and computes `$8,882.60 × 12 = $106,591.20` as the annual value.

## Step 12 — Specialist post-processing

After the raw playbook result comes back, the specialist applies category-specific rules. Examples:

- **BasicInfoAgent**: If `lease_term_yrs` came back as `None` but LCD and LED are both in `shared_facts`, derive it from `(LED - LCD) / 365.25` and add a cross-field note
- **BasicInfoAgent**: If `lease_date` came back `None`, record the BRD-specified literal `"undated"`
- **BasicInfoAgent**: If `tenant_name` or `landlord_name` is blank, set `needs_review=True` with a red flag
- **ReimbursementAgent**: If `cam == "No Recovery"`, propagate that value to `cam_inclusion`, `cam_exclusion`, `caps_on_cam`, `base_year`
- **OtherClausesAgent**: `allowance` checks the LCD age — if < 1 year old, include full disbursement terms; if > 1 year, amount only

## Step 13 — Persist each result

Each field result is written to SQL Server immediately via the `on_result` callback so progress is visible during the run:

- **UPSERT FieldValue** row: value, confidence, page, clause, clause_text, trace_summary
- **INSERT AuditLog** entry: `action='extract'`, actor=`specialist:<category>`
- Update **ExtractionJob**.progress so the polling client sees advancement

## Step 14 — Reconciliation sweep

Once all specialists finish, the **ReconciliationAgent** runs cross-document and cross-field checks:

| Check | When it fires | Severity |
|---|---|---|
| `RSF_MISMATCH` | Leased RSF differs across documents | warning |
| `SUITE_CONFLICT` | Suite number varies | warning |
| `NAME_VARIATION` | Tenant name spelled differently | info |
| `DATE_INCONSISTENCY` | LED precedes LCD | **critical** |
| `LEASE_TERM_MATH_MISMATCH` | Stated term ≠ (LED − LCD) ± 0.5 yr | warning |
| `RENT_CHANGE_ACROSS_DOCS` | Annual rent differs between base lease and amendments (expected for amendments) | info |
| `LOW_CONFIDENCE` | Count of fields with confidence < 0.5 | info |
| `MANUAL_REVIEW_REQUIRED` | Count of fields with `needs_review=True` | warning |

Each flag is written to the AuditLog as `action='red_flag'` so it shows up in `GET /tenants/{id}/red-flags`.

## Step 15 — Mark job complete

`ExtractionJob.status = 'complete'`, `progress = 100`, `finished_at = now()`. The task returns.

## Step 16 — Client polls

While the extraction runs (roughly 40–60 min for a full lease with `qwen2.5:32b`, or 15–20 min with `qwen2.5:14b`), the client polls:

```bash
curl http://localhost:8000/api/v1/jobs/$JOB_ID
# {"status": "running", "progress": 42, "completed_fields": 30, "total_fields": 71}
```

Until it sees `"status": "complete"`.

## Step 17 — Fetch the result

### 17a — JSON

```bash
curl "http://localhost:8000/api/v1/extract/jobs/$JOB_ID/result?format=json"
```

Returns `TenantAbstractionOut` with every field carrying both per-document breakdown and concluded value, each with confidence + citation:

```json
{
  "fields": [
    {
      "field_id": "annual_base_rent",
      "concluded_value": "106591.20",
      "concluded_confidence": 0.92,
      "confidence_level": "high",
      "source_document_label": "Base Lease",
      "page_number": 17,
      "clause_number": "Addendum 1",
      "clause_text": "August 1, 2009 through July 31, 2012 $8,882.60",
      "per_document": [...]
    }
  ],
  "summary": {
    "total_fields": 71,
    "fields_extracted": 54,
    "mean_confidence": 0.83,
    "high_confidence_count": 41
  }
}
```

### 17b — Excel download

```bash
curl "http://localhost:8000/api/v1/extract/jobs/$JOB_ID/result?format=xlsx" -o lease_abstraction.xlsx
```

Returns a 3-sheet workbook:

- **Summary** — tenant header + extraction quality rollup
- **Lease Abstraction** — 17-column grid with confidence-banded colouring (green ≥ 0.8, amber 0.5–0.8, red < 0.5), full citations, needs_review rows highlighted
- **Red Flags** — reconciliation findings by severity

---

## What the user sees vs. what the system does

| What the user experiences | What actually happens |
|---|---|
| `POST /extract/pdf` returns immediately with a `job_id` | Files written to disk, 2 Celery tasks queued, 1 row in SQL Server |
| `GET /jobs/{id}` eventually says "complete" | ~80 clauses OCR'd and embedded, ~71 × N_documents playbook walks executed, each making ~3–13 Ollama calls, reconciliation run, ~350 AuditLog rows written |
| `GET /extract/jobs/{id}/result?format=xlsx` downloads a spreadsheet | SQL Server queried for every FieldValue and AuditLog entry for this tenant, concluded-value engine applied, Excel workbook built in memory, streamed as download |

The user's experience is "upload PDF, wait, download Excel". Underneath, the system did ~thousands of targeted LLM calls, all driven by the master abstraction guides your BRD already defined.
