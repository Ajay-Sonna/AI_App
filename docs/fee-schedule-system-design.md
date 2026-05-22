# Fee schedule companion — technical system design

Short architectural view of **how state files and DST rows enter the system (Phase 1)** and **how mappings + compare use them (Phase 2)**. Implementation is **FastAPI + React** with **two SQL Server databases** (companion vs DST warehouse).

```
┌─────────────┐     HTTP      ┌──────────────────┐     pyodbc      ┌─────────────┐
│ React SPA    │ ───────────► │ FastAPI backend  │ ─────────────► │ DST MSSQL    │
└─────────────┘               │ (preview, APIs)   │               │ fee tables    │
                              └─────────┬────────┘               └─────────────┘
                                        │
                          pyodbc + disk │ Companion MSSQL +
                                        ▼ `FeeScheduleVault`
                              ┌──────────────────┐
                              │ dbo.* artifacts │
                              │ + mappings       │
                              └──────────────────┘
```

---

## Phase 1 — Ingest state files & read DST database

### 1A. State-side fee files → **artifacts**

| Concern | Approach |
|--------|----------|
| **Acquisition** | **Pipeline run** (`POST /run`): optional LLM/crawler resolves portal links; **`collect_file_urls_from_pipeline_result`** gathers HTTP(S) URLs. Or manual **`POST /app/artifacts/download`** with URL + metadata. |
| **Download** | **`requests`** (browser-like headers/referer/cookies). Size cap (**50 MiB**). |
| **Idempotency** | **`requests.head`** ETag vs DB `remote_etag`; after download **`content_sha256`** vs prior row → skip redundant writes. |
| **Storage layout** | Volatile **`ARTIFACT_ROOT`** (env, default **`C:/FeeScheduleVault`**): `{state}/{logical_schedule_slug}/{date}__{sha16}_{filename}` (see **`download_fee_schedule_artifact`**). |
| **Companion DB row** | **`dbo.fee_schedule_artifact`**: `state_code`, **`logical_schedule_key`** (portal folder identity), **`source_url`**, **`stored_rel_path`**, MIME, **`is_current`**, etag/LM (**`artifacts_repo`**). Older versions superseded before insert. |

**Example:** Portal returns `fee_jan.zip` URL for NC workbook “Medicaid Physician”. Run persists file under `fee_schedule_vault/nc/medicaid_physician/2026-03-06__abc123_fee.xlsx`; DB row **`artifact_id=42`**, **`logical_schedule_key="medicaid_physician"`**.

### 1B. Turning bytes into something the UI can browse

| Format | Module | Technique |
|--------|--------|-----------|
| **`.xlsx` / `.xlsm`** | **`openpyxl`** (`load_workbook`, **`data_only=True`**) | Read **`wb.active`** (rich path merges) or first sheet (**`read_only`** path). Expand merged cells; scan capped grid; heuristic **`_find_likely_grid_header_index`** + optional dual header merge → **`columns[]` + `rows[][]`**. |
| **PDF tables** | **`pdfplumber`** | Per page **`extract_tables()`**; merge dominated wide grids across pages (**`_merge_pdf_vectors`**). |
| **CSV/TSV** | **`csv` (stdlib)** | Sniff delimiter; normalize header row. |

Expose grid via **`GET /app/artifacts/{id}/preview-table`**. Optionally inline PDF/HTML preview uses same **`preview_service`** primitives.

### 1C. DST database — **tabular read model**

| Concern | Approach |
|--------|----------|
| **Connection** | **`pyodbc`** from **`MSSQL_ODBC_CONN`** / **`MSSQL_*`** builders (**`dst_db/service.py`**). |
| **Discover tables** | **`GET /dst/tables`** (`INFORMATION_SCHEMA` + **`state_code`** column filter). |
| **Sample rows** | **`GET /dst/rows?table=T&limit=N&state_code=NC`** → **`TOP (N)`** `SELECT *` with **`WHERE state_code`** when column exists (**`fetch_dst_table_rows`**). Identifier validation prevents SQL injection (**`validate_table_name`**). |
| **API shape** | Column list + **`List[dict]`** rows; nested JSON-ish cells may flatten to synthetic keys for the UI. |

**Example:** Frontend picks table **`Fees_NC_CA`**. API returns **`CODE`**, **`MOD`**, **`FAC`**, … for **TOP 8000** rows where **`STATE_CODE='NC'`** (if applicable).

Phase 1 delivers: **immutable state file on disk**, **indexed DB row keyed by `(state, logical_schedule_key)`**, **parseable preview grid**, and **DST slices** filtered by validated table/state.

---

## Phase 2 — Column mapping & compare

### 2A. Mapping as **persisted declarative wiring**

| Item | Detail |
|------|--------|
| **Stores** | **`dbo.fee_schedule_column_mapping`**, column **`column_map_json`**: **`{ "Procedure Code": "CODE", "Facility Rate": "FAC", … }`** (**state header string → logical DST header**). |
| **Lookup triple** | **`(state_code, state_logical_schedule_key, dst_fsname)`** (**`fee_column_mappings_repo.lookup_latest_mapping`** — newest **`updated_at`**). |
| **Schedule key resolution** | **`resolve_schedule_key_for_artifact`**: prefer artifact **`logical_schedule_key`** else **`artifact:{id}`**. |
| **Write path** | **`POST /app/fee-column-mappings/upsert`** validates JSON → upsert (**`artifacts_repo`** + mapping repo). **`GET …/latest`** hydrates SPA mapping editor. |

**Example:** Artifact 42 (“medicaid_physician”) + DST **`Fees_NC_CA`** ⇒ JSON maps **`Procedure Code`→`CODE`**, **`Modifier`→`MOD`**, **`Non-Facility Rate`→`NFC`**.

### 2B. Compare engine — **`compare_artifact_to_dst`**

1. **Load mapping** — fail closed if triple missing / empty JSON / **`CODE`** not wired.  
2. **Rebuild state grid** — **`build_artifact_table_preview(bytes)`** (same parsers as Phase 1) → **`st_dicts`**. Validate mapped state columns exist.  
3. **Fetch DST** — **`fetch_dst_table_rows`** (larger **`dst_row_limit`**, validated table). Resolve **physical** column names case-insensitively for every DST target referenced in mapping.  
4. **Join policy** — Index DST by normalized **`CODE`** (**`join_key_dst`**). Walk state rows (**`join_key_state`**). If duplicates exist on either side ⇒ require **`CODE+MOD`** (**`dup_code`** gate). Matches take **first DST row** per key.  
5. **Field diff matrix** — For each **`(state_col, dst_col)`**, **`_coerce_compare`**: trimmed string equality; mapped **money/rate-like** pairs use **`Decimal`** rounded to **2 dp** (`ROUND_HALF_UP`) then equality (Excel-style cents); other numeric pairs use **|`a−b`| ≤ 0.005**. Emit **`same` / `state_value` / `dst_value`**. Row **`match`** iff all pairs **`same`**; unmatched keys emit **`state_only`** / **`dst_only`**.  
6. **Envelope** — Summary counts + capped **`column_pairs`** + **`rows`** array (`status`, `join_key`, `field_diffs`, optional display maps), bounded by **`max_result_rows`** (default **5000**). Returned by **`POST /app/fee-schedules/compare`** to the SPA (**split-pane diff UI**).

**Example:** State row **`70240` + `MOD=26`** joins DST **`CODE='70240'`, `MOD='26'`**. Facility rates **100.126** vs **100.13** → **`same`** after **2 dp** alignment; non‑money numerics still use the **0.005** epsilon; one text column mismatched ⇒ row **`mismatch`**.

---

## Design principles (cross-cutting)

- **Separate concerns:** extraction (formats) vs domain (mapping/join/policy) vs transport (REST).  
- **Explicit identity:** artifacts keyed by **`logical_schedule_key`**; mappings keyed by **`dst_fsname`**.  
- **Safety caps:** artifact size, SQL **`TOP`**, compare output row cap, parameterized queries.  
- **Single parsing path:** preview and compare both call **`build_artifact_table_preview`** so grids stay consistent.

For package-level detail (**`openpyxl`**, **`pdfplumber`**, **`pyodbc`**) see **`backend/requirements.txt`** and **`preview/preview_service.py`**, **`compare_fee_schedules.py`**, **`dst_db/service.py`**.
