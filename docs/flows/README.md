# End-to-End Flow Diagrams

This folder contains one mermaid diagram per major LeaseGenie flow. Every `.mermaid` file is self-contained — drop it into GitHub, VS Code (with the Mermaid extension), Notion, or [mermaid.live](https://mermaid.live) to render.

## Quick map

| # | File | Diagram type | What it shows |
|---|------|--------------|---------------|
| 1 | [`01_startup_compile.mermaid`](./01_startup_compile.mermaid) | flowchart | Service boot: init DB → load BRD → compile playbooks from `.docx` + `Questions.xlsx` → register routers |
| 2 | [`02_order_creation.mermaid`](./02_order_creation.mermaid) | flowchart | `POST /orders` — validation, Project → Property → Tenant hierarchy creation |
| 3 | [`03_document_upload.mermaid`](./03_document_upload.mermaid) | flowchart | `POST /tenants/{id}/documents` — file ingestion with base-lease-first + 7-amendment cap rules |
| 4 | [`04_extraction_lifecycle.mermaid`](./04_extraction_lifecycle.mermaid) | sequenceDiagram | `POST /tenants/{id}/extract` — API → Celery → Pipeline → Coordinator, with polling `GET /jobs/{id}` |
| 5 | [`05_multi_agent_extraction.mermaid`](./05_multi_agent_extraction.mermaid) | flowchart | **The core flow.** OCR → BM25 → 5 specialists in dependency order → per-field playbook execution → reconciliation. Shows `shared_facts` passing between specialists. |
| 6 | [`06_playbook_execution.mermaid`](./06_playbook_execution.mermaid) | flowchart | **Drill-down into one playbook.** The strict IF YES/IF NO decision-tree walker: per-question LLM call → action dispatch (GOTO, EXTRACT, FINALIZE, RECORD_NONE, RECORD_LITERAL, FLAG_REVIEW). |
| 7 | [`07_abstraction_retrieval.mermaid`](./07_abstraction_retrieval.mermaid) | flowchart | `GET /tenants/{id}/abstraction` — concluded-value computation with citation + confidence propagation from the winning document, confidence-band derivation, and the rolled-up `summary` block. |
| 8 | [`08_override_audit.mermaid`](./08_override_audit.mermaid) | sequenceDiagram | `PATCH /tenants/{id}/fields/{field_id}` — manual override set/clear with audit log entries, returning the recomputed abstraction. |
| 9 | [`09_red_flags.mermaid`](./09_red_flags.mermaid) | flowchart | Red-flag origination (4 sources: playbook Q red_flag column, specialist must-have rules, reconciliation cross-doc, reconciliation cross-field) → persistence → `GET /tenants/{id}/red-flags` assembly. |
| 10 | [`10_upload_with_vector_index.mermaid`](./10_upload_with_vector_index.mermaid) | sequenceDiagram | **Vector indexing flow.** `POST /tenants/{id}/documents` kicks off a background Celery task that OCRs the PDF, embeds every clause via Ollama `nomic-embed-text`, and upserts to ChromaDB so the agent's `semantic_search` tool can find clauses by meaning, not just keywords. |
| 11 | [`11_extract_pdf_wrapper.mermaid`](./11_extract_pdf_wrapper.mermaid) | flowchart | **The single-call wrapper.** `POST /extract/pdf` auto-creates the hierarchy, writes PDFs, fires extraction, returns `job_id`; client polls, then fetches result via `GET /jobs/{id}/result?format=json\|xlsx`. |
| 12 | [`12_step_by_step_upload_to_extraction.mermaid`](./12_step_by_step_upload_to_extraction.mermaid) + [`walkthrough.md`](./12_step_by_step_walkthrough.md) | flowchart + narrative | **Numbered step-by-step** from PDF upload to JSON/xlsx result, with Sample_1.pdf as worked example. Diagram + plain-English explanation side by side. |

## Reading order

For understanding the system end-to-end, read in this order:

1. **`01`** — what's alive at boot
2. **`02 → 03 → 04`** — how work arrives and gets queued
3. **`05`** — what the coordinator actually does (zoom out)
4. **`06`** — how ONE field is extracted (zoom in — this is the key innovation)
5. **`07`** — what the final answer looks like to the client
6. **`08 → 09`** — operational flows (overrides, red flags)

## How to render

### GitHub / GitLab
Mermaid renders automatically in `.md` files. To view a `.mermaid` file, copy its content into a markdown block:

````markdown
```mermaid
<paste file content here>
```
````

### VS Code
Install the **Markdown Preview Mermaid Support** extension, then open any `.mermaid` file.

### Standalone
[mermaid.live](https://mermaid.live) — paste and render.

### Export to PNG/SVG
```bash
npm install -g @mermaid-js/mermaid-cli
mmdc -i 05_multi_agent_extraction.mermaid -o 05.png
```

## Colour legend

Across the flowcharts the same colours mean the same things:

| Colour | Meaning |
|--------|---------|
| 🟦 Blue | Entry point / client request |
| 🟩 Green | Successful terminal (response returned) |
| 🟥 Red | Error terminal (4xx, critical flag) |
| 🟧 Orange | Decision gate / warning-level flag |
| 🟨 Yellow | Side-effect (disk write, cache seed) / info flag |
| 🟪 Purple | LLM call or significant in-memory computation |
| 🟦 Cyan | Summary / rollup step |
