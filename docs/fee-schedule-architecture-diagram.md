# Fee Schedule Tool — reference architecture (updated)

This redraws the conceptual flow with: **companion vs DST storage split**, **explicit join keys**, **honest MVP vs planned scope**, **failure / fallback paths**, and **stack labels aligned to the current codebase** (FastAPI, React, Playwright where used, `requests`, LLM calls for block/file relevance — **not** implying BeautifulSoup or a specific LLM unless you standardize on them).

**How to view / export:** paste the Mermaid block into [Mermaid Live Editor](https://mermaid.live) → export PNG/SVG.

---

## Legend

| Style        | Meaning                                                          |
| ------------ | ---------------------------------------------------------------- |
| Solid boxes  | **MVP / implemented** in the companion + DST flow we described   |
| Dashed edge  | **Planned**, partial, or roadmap                                 |
| Thick border | **Second system of record** (DST warehouse vs companion)      |

---

## System diagram

```mermaid
flowchart TB
  %% -------- Styling --------
  classDef config fill:#cce5ff,stroke:#1e5a8e,stroke-width:2px,color:#0b2a4a
  classDef extract fill:#e8ecef,stroke:#495057,stroke-width:2px,color:#212529
  classDef ai fill:#e2e6ea,stroke:#495057,stroke-width:2px,color:#212529
  classDef companion fill:#cce5ff,stroke:#1e5a8e,stroke-width:2px,color:#0b2a4a
  classDef dst fill:#fff4e0,stroke:#c45c00,stroke-width:3px,color:#4a2c00
  classDef ui fill:#ffd6e8,stroke:#b0006a,stroke-width:2px,color:#4a1538
  classDef mapping fill:#d4edda,stroke:#1e7a3c,stroke-width:2px,color:#143d1f
  classDef compare fill:#fff9c4,stroke:#b59f00,stroke-width:2px,color:#524500
  classDef failure fill:#f8d7da,stroke:#a71d2a,stroke-width:1px,color:#5c111a
  classDef planned stroke-dasharray: 5 5,stroke-width:2px,fill:#f8f9fa,color:#495057

  subgraph S1["1 · INPUT CONFIG (MVP)"]
    direction TB
    SC[State portal URL registry] --> PDB[(Companion DB • state_portal_link)]
    KID["Keys: state_code · display_label · portal_url • (optional hints)"]
    SC --> KID
  end

  subgraph S2["2 · DATA EXTRACTION (MVP)"]
    direction TB
    F1["HTTP fetch (requests) • browser-like headers"]
    PW["Optional: Playwright / SPA observer when JS shell or APIs needed"]
    ST["DOM structure signals: tables · file links · site class"]
    F1 --> ST
    PW --> ST
    ST --> CAP["Captured: HTML ± XHR payloads for extractors"]
    CAP --> EDGE{{"Retries · manual URL · alerts"}}
    EDGE -.-> MF["User / ops: rerun · alternate URL"]

    subgraph FX["Extraction pitfalls"]
      BL["Blocked · CAPTCHA · 403"]:::failure
      PF["Ambiguous DOM · no usable table"]:::failure
    end

    CAP -.-> FX
  end

  subgraph S3["3 · AI / LLM LAYER (MVP — scoped)"]
    direction TB
    LLM["LLM: classify which page blocks drive extraction"]
    FLT["Optional LLM: prune noisy file-link rows"]
    LC["Local extractors choose path: SPA API catalog • HTML tables • pagination"]
    LLM --> LC
    FLT --> LC
    LC -.-> NBS["Parser note: BS4 not required • use openpyxl / pdfplumber / csv extractors"]
    NBS -.-> roadmapAI["Planned: richer LLM site understanding"]:::planned
  end

  subgraph S4["4 · STORAGE — two distinct systems"]
    direction LR

    subgraph CPN["Companion (MVP)"]
      ART[(fee_schedule_artifact • metadata)]
      HASH["identity: content_sha256 · ETag • logical_schedule_key · source_url"]
      VAULT[["Artifact vault: local path (ENV) · file bytes"]]
      ART --- HASH --- VAULT
    end

    subgraph WH["DST warehouse (MVP)"]
      SQL[(SQL Server dbo fee tables • pyodbc)]
      SF["often filtered by state_code column"]
      SQL --- SF
    end

    class CPN companion
    class WH dst
  end

  subgraph S5["5 · USER INTERFACE (MVP)"]
    direction TB
    RF[React SPA]
    BE[FastAPI backend · REST]
    RF <--> BE
    PV["Preview: openpyxl · pdfplumber · csv • same parsers as compare"]
    BE --> PV
  end

  subgraph S6["6 · MAPPING ENGINE (MVP)"]
    direction TB
    UIMAP["Mapping tab: user pairs state columns → DST names (CODE • MOD • rates…)"]
    JSON[(fee_schedule_column_mapping • column_map_json)]
    UIMAP --> JSON
    AM["Roadmap: assisted / suggested column pairing"]:::planned
    GOV["Governance (recommended): versioning · updated_by · audit"]:::planned
    UIMAP -.-> AM
    JSON -.-> GOV
  end

  subgraph S7["7 · COMPARISON (MVP)"]
    direction TB
    LD["Reload artifact bytes • build grid (preview pipeline)"]
    DS["FETCH TOP(N) DST rows • MSSQL companion filter rules"]
    JK["Join: normalized CODE • or CODE+MOD if duplicates on either side"]
    FD["Per mapped pair: text match or Decimal ε (e.g. half-cent rule)"]
    OUT["Side-by-side diff · summary counts · row cap • modal UI"]
    MERGE["Roadmap: merged workbook / CSV export pipeline"]:::planned
    LD --> JK
    DS --> JK
    JK --> FD --> OUT
    OUT -.-> MERGE
  end

  %% -------- Cross-links --------
  PDB --> F1
  CAP --> LLM
  LC --> ART
  BE --> ART
  BE --> SQL
  BE --> JSON
  JSON --> JK
  VAULT -.->|"read_bytes"| LD
  SQL -.-> DS
  BE -->|"preview-table"| PV

  class S1 config
  class S2,S3 extract,ai
  class S5,S6 ui,mapping
  class S7 compare
```

---

## Narrative checkpoints (same as poster, tighter)

1. **Truth layering:** Published **state file bytes** (+ parsed grid), **DST SQL** snapshot, and **saved mapping JSON** are three inputs to compare — not one blob.
2. **Join policy:** Rows align on **`CODE`**; when duplicate codes exist on state or DST, **`CODE + MOD`** is required — diagram makes that explicit inside Compare.
3. **Cloud:** If OneDrive/Azure blob is adopted, attach it **only under Companion storage** as a drop-in backend for **VAULT**, not mixed with DST.
4. **Security insert (diagram gap you can add on slides):** guard **DST ODBC**, **companion DB**, **artifact vault path**, **Secrets in env**, **no scraping behind auth without explicit session/cookie UX**.

---

## Optional one-slide slogan

**“DST = internal warehouse of record • State artifact = payer-published source • Mapping = audited bridge • Compare = join + numeric policy.”**
