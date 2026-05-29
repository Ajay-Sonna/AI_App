import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as XLSX from 'xlsx'
import logo from './assets/logo.png'
import './App.css'

const API_BASE = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000'

/** In-memory cap for prefetched fee-schedule tables (artifact id → grid; state+table → DST rows). */
const SCHEDULES_PREVIEW_CACHE_LIMIT = 8

/**
 * Rows scanned to union DST JSON keys for the Mapping tab column list.
 * Small samples miss keys that only appear deeper in state-filtered slices (e.g. MOD).
 * Matches compare default DST slice size; backend caps at 10_000.
 */
const MAPPING_DST_COLUMN_SAMPLE_LIMIT = 8000

/** Bump when sampling changes so cached column lists refresh */
const MAPPING_DST_COLUMN_CACHE_VERSION = 'v2'

/** Max Mapping-tab cache entries for DST column lists (state|table → string[]). */
const MAPPING_DST_COLUMN_CACHE_LIMIT = 12

/** Canonical 2-letter USPS codes + display names (matches backend `resolve_us_state_code`). */
const US_STATE_NAMES_BY_CODE = {
  AL: 'Alabama',
  AK: 'Alaska',
  AZ: 'Arizona',
  AR: 'Arkansas',
  CA: 'California',
  CO: 'Colorado',
  CT: 'Connecticut',
  DE: 'Delaware',
  DC: 'District of Columbia',
  FL: 'Florida',
  GA: 'Georgia',
  HI: 'Hawaii',
  ID: 'Idaho',
  IL: 'Illinois',
  IN: 'Indiana',
  IA: 'Iowa',
  KS: 'Kansas',
  KY: 'Kentucky',
  LA: 'Louisiana',
  ME: 'Maine',
  MD: 'Maryland',
  MA: 'Massachusetts',
  MI: 'Michigan',
  MN: 'Minnesota',
  MS: 'Mississippi',
  MO: 'Missouri',
  MT: 'Montana',
  NE: 'Nebraska',
  NV: 'Nevada',
  NH: 'New Hampshire',
  NJ: 'New Jersey',
  NM: 'New Mexico',
  NY: 'New York',
  NC: 'North Carolina',
  ND: 'North Dakota',
  OH: 'Ohio',
  OK: 'Oklahoma',
  OR: 'Oregon',
  PA: 'Pennsylvania',
  RI: 'Rhode Island',
  SC: 'South Carolina',
  SD: 'South Dakota',
  TN: 'Tennessee',
  TX: 'Texas',
  UT: 'Utah',
  VT: 'Vermont',
  VA: 'Virginia',
  WA: 'Washington',
  WV: 'West Virginia',
  WI: 'Wisconsin',
  WY: 'Wyoming',
}

const US_STATES = Object.entries(US_STATE_NAMES_BY_CODE)
  .map(([code, name]) => ({ code, name }))
  .sort((a, b) => a.name.localeCompare(b.name))

function stateNameFromCode(code) {
  const c = (code || '').toString().trim().toUpperCase()
  return US_STATE_NAMES_BY_CODE[c] || c || '—'
}

/** All user-visible wall-clock timestamps use India Standard Time. */
const APP_DISPLAY_TIME_ZONE = 'Asia/Kolkata'
const APP_DISPLAY_LOCALE = 'en-IN'

function parseApiUtcIso(iso) {
  if (iso == null || iso === '') return null
  const s = String(iso).trim()
  if (!s) return null
  if (/[zZ]$/.test(s) || /[+-]\d{2}:\d{2}$/.test(s)) return new Date(s)
  return new Date(`${s}Z`)
}

function formatDateTimeIST(iso) {
  if (!iso) return '—'
  try {
    const d = parseApiUtcIso(iso)
    if (!d || Number.isNaN(d.getTime())) return '—'
    return `${d.toLocaleString(APP_DISPLAY_LOCALE, {
      timeZone: APP_DISPLAY_TIME_ZONE,
      dateStyle: 'medium',
      timeStyle: 'short',
    })} IST`
  } catch {
    return '—'
  }
}

function formatArtifactFetchedAt(iso) {
  return formatDateTimeIST(iso)
}

function formatPortalEffectiveDateShort(iso) {
  if (iso == null || iso === '') return '—'
  try {
    const d = new Date(String(iso).length === 10 ? `${iso}T12:00:00` : iso)
    if (Number.isNaN(d.getTime())) return '—'
    return d.toLocaleDateString(undefined, { dateStyle: 'medium' })
  } catch {
    return '—'
  }
}

function artifactVersionStatusLabel(a) {
  const cur = a?.is_current === true || a?.is_current === 1 || a?.is_current === '1'
  return cur ? 'Latest' : 'Historical'
}

function artifactIsCurrent(a) {
  return a?.is_current === true || a?.is_current === 1 || a?.is_current === '1'
}

/** Portal / stored labels that are not useful as a human title. */
function isGarbageLinkArtifactLabel(s) {
  const t = String(s || '')
    .trim()
    .toLowerCase()
  return t === '' || t === 'download' || t === 'download file' || t === 'click here' || t === 'here' || t === 'file'
}

/** Edition timestamp: prefer portal effective date, else when we fetched (matches backend recompute). */
function artifactEditionSortTs(a) {
  const ped = a?.portal_effective_date
  if (ped != null && ped !== '') {
    try {
      const d = new Date(String(ped).length === 10 ? `${ped}T12:00:00` : ped)
      if (!Number.isNaN(d.getTime())) return d.getTime()
    } catch {
      /* ignore */
    }
  }
  const ft = a?.fetched_at_utc
  if (!ft) return 0
  try {
    const d2 = parseApiUtcIso(ft)
    return d2 && !Number.isNaN(d2.getTime()) ? d2.getTime() : 0
  } catch {
    return 0
  }
}

/** Single-line fee schedule title for saved artifacts (logical key preferred; no hashes/dates in the label). */
function isLikelyStorageHash(stem) {
  const t = String(stem || '').trim()
  if (t.length < 20) return false
  return /^[a-f0-9]+$/i.test(t)
}

const LOWERCASE_TITLE_WORDS = new Set([
  'or',
  'and',
  'of',
  'the',
  'a',
  'an',
  'in',
  'on',
  'at',
  'to',
  'for',
  'vs',
  'v',
  'per',
  'as',
  'by',
])

const KNOWN_ACRONYMS = new Map([
  ['crna', 'CRNA'],
  ['np', 'NP'],
  ['pa', 'PA'],
  ['rn', 'RN'],
  ['lpn', 'LPN'],
  ['dd', 'DD'],
  ['tbi', 'TBI'],
  ['mr', 'MR'],
  ['mh', 'MH'],
  ['sa', 'SA'],
  ['cms', 'CMS'],
])

/** Portal logical keys like ``mh_sa`` / ``dd`` chains — prefer a longer ``source_label`` when present. */
function logicalLooksLikeAbbreviationSlug(phrase) {
  const w = String(phrase || '')
    .trim()
    .replace(/_/g, ' ')
    .split(/\s+/)
    .filter(Boolean)
  if (w.length < 2) return false
  const shortTokens = w.filter((t) => t.length <= 3).length
  return shortTokens >= w.length - 1 || (w.length === 2 && w.every((t) => t.length <= 4))
}

function titleCaseFeeSchedulePhrase(phrase) {
  const s = String(phrase || '')
    .trim()
    .replace(/\s+/g, ' ')
  if (!s) return s
  const words = s.split(' ')
  const capSeg = (seg) => {
    if (!seg) return seg
    if (seg.length === 1) return seg.toLocaleUpperCase()
    if (seg.includes('-')) return seg.split('-').map((x) => capSeg(x)).join('-')
    if (seg.length <= 2) return seg.toLocaleUpperCase()
    const low = seg.toLocaleLowerCase()
    return low.charAt(0).toLocaleUpperCase() + low.slice(1)
  }
  return words
    .map((w, i) => {
      const low = w.toLocaleLowerCase()
      if (KNOWN_ACRONYMS.has(low)) return KNOWN_ACRONYMS.get(low)
      if (LOWERCASE_TITLE_WORDS.has(low) && i > 0 && i < words.length - 1) return low
      return capSeg(w)
    })
    .join(' ')
}

function artifactFeeScheduleDisplayName(a) {
  if (!a || typeof a !== 'object') return 'Fee schedule'
  const archived =
    a.is_current === false || a.is_current === 0 || a.is_current === '0' ? ' · prior' : ''
  const rawLogical = String(a.logical_schedule_key || '').trim().replace(/_/g, ' ')
  const slabel = String(a.source_label || '').trim()
  let base = ''
  if (slabel && !isGarbageLinkArtifactLabel(slabel)) {
    base = slabel
  } else if (rawLogical && slabel && logicalLooksLikeAbbreviationSlug(rawLogical) && slabel.length > rawLogical.length + 2) {
    base = slabel
  } else if (rawLogical) {
    base = rawLogical
  } else if (slabel) {
    base = slabel
  } else {
    const fname = String(a.original_filename || '').trim()
    const fileBase = fname.replace(/^.*[/\\]/, '')
    const noExt = fileBase.replace(/\.[^.]+$/i, '')
    if (noExt && !isLikelyStorageHash(noExt)) base = noExt.replace(/_/g, ' ')
    else if (fileBase && !isLikelyStorageHash(fileBase.replace(/\.[^.]+$/i, ''))) base = fileBase
  }
  if (!base) {
    const id = Number(a.artifact_id)
    return `${Number.isFinite(id) ? `Fee schedule (${id})` : 'Fee schedule'}${archived}`
  }
  return `${titleCaseFeeSchedulePhrase(base)}${archived}`
}

/**
 * Readable label for snake_case / shorthand DB identifiers (shown in Mapping UI).
 * Stored JSON still uses raw names — compare API matches by actual column ids.
 */
function formatMappingDbColumnDisplay(raw) {
  const s = String(raw ?? '').trim()
  if (!s) return '—'
  const segs = s.split('_').filter(Boolean)
  if (segs.length === 0) return raw
  const capWord = (w) => {
    if (!w) return w
    return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
  }
  if (segs.length === 1) {
    const one = segs[0]
    if (one.length <= 5 && /^[a-z]+$/i.test(one)) return one.toUpperCase()
    return capWord(one)
  }
  return segs.map(capWord).join(' ')
}

/** Normalize API column_map to { stateHeader: dstColumn } (omit empty destinations). */
function normalizeColumnPairs(obj) {
  const out = {}
  if (!obj || typeof obj !== 'object') return out
  for (const [k, v] of Object.entries(obj)) {
    const sk = String(k || '').trim()
    const dk = typeof v === 'string' ? v.trim() : String(v ?? '').trim()
    if (sk && dk) out[sk] = dk
  }
  return out
}

/** Build JSON body for dbo.fee_schedule_column_mapping.column_map_json. */
function columnMapPayload(pairsByStateColumn) {
  const out = {}
  if (!pairsByStateColumn || typeof pairsByStateColumn !== 'object') return out
  for (const [k, v] of Object.entries(pairsByStateColumn)) {
    const sk = String(k || '').trim()
    const dk = typeof v === 'string' ? v.trim() : String(v ?? '').trim()
    if (sk && dk) out[sk] = dk
  }
  return out
}

/** Rows for the State fee `<select>` (saved artifacts only; list is shown after Get Data). */
function buildArtifactFeePickRows(artifacts) {
  const list = []
  for (const a of Array.isArray(artifacts) ? artifacts : []) {
    if (!a || typeof a !== 'object') continue
    const n = Number(a.artifact_id)
    if (!Number.isFinite(n)) continue
    list.push({
      key: `a:${n}`,
      label: artifactFeeScheduleDisplayName(a),
      artifactId: n,
      externalUrl: null,
    })
  }
  list.sort((a, b) =>
    String(a.label || '').localeCompare(String(b.label || ''), undefined, {
      sensitivity: 'base',
      numeric: true,
    }),
  )
  return list
}

/** Full timestamp for “last run” under the state name (IST). */
function formatPortalLastRunAt(iso) {
  return formatDateTimeIST(iso)
}

function formatLlmTokenUsageSummary(usage) {
  if (!usage || typeof usage !== 'object') return null
  const input = Number(usage.prompt_tokens) || 0
  const output = Number(usage.completion_tokens) || 0
  const total = Number(usage.total_tokens) || input + output
  const calls = Number(usage.call_count) || 0
  if (total <= 0 && input <= 0 && output <= 0) return null
  return `Groq tokens — input: ${input.toLocaleString(APP_DISPLAY_LOCALE)}, output: ${output.toLocaleString(APP_DISPLAY_LOCALE)}, total: ${total.toLocaleString(APP_DISPLAY_LOCALE)}${calls ? ` (${calls} LLM call${calls === 1 ? '' : 's'})` : ''}.`
}

/** Short relative time for dashboard cards (e.g. "2 mins ago"). */
function formatRelativeAgo(iso) {
  if (!iso) return '—'
  try {
    const d = parseApiUtcIso(iso)
    const t = d?.getTime()
    if (t == null || Number.isNaN(t)) return '—'
    const sec = Math.max(0, Math.floor((Date.now() - t) / 1000))
    if (sec < 45) return 'Just now'
    const min = Math.floor(sec / 60)
    if (min < 60) return `${min} min${min === 1 ? '' : 's'} ago`
    const hr = Math.floor(min / 60)
    if (hr < 48) return `${hr} hour${hr === 1 ? '' : 's'} ago`
    const day = Math.floor(hr / 24)
    if (day < 14) return `${day} day${day === 1 ? '' : 's'} ago`
    return formatPortalLastRunAt(iso)
  } catch {
    return '—'
  }
}

const FEE_TOOL_METRICS_LS_KEY = 'fee_tool_schedules_metrics_v1'

function readFeeToolSchedulesMetrics() {
  try {
    const raw = localStorage.getItem(FEE_TOOL_METRICS_LS_KEY)
    if (!raw) return { compareCountByState: {}, lastCompareByState: {} }
    const j = JSON.parse(raw)
    return {
      compareCountByState:
        typeof j.compareCountByState === 'object' && j.compareCountByState != null ? j.compareCountByState : {},
      lastCompareByState:
        typeof j.lastCompareByState === 'object' && j.lastCompareByState != null ? j.lastCompareByState : {},
    }
  } catch {
    return { compareCountByState: {}, lastCompareByState: {} }
  }
}

function persistFeeToolCompareSuccess(stateCode, detail) {
  if (!stateCode) return
  try {
    const m = readFeeToolSchedulesMetrics()
    const compareCountByState = { ...m.compareCountByState }
    compareCountByState[stateCode] = (Number(compareCountByState[stateCode]) || 0) + 1
    const lastCompareByState = {
      ...m.lastCompareByState,
      [stateCode]: {
        at: new Date().toISOString(),
        stateLabel: detail.stateLabel,
        dstLabel: detail.dstLabel,
        artifactId: detail.artifactId,
        dstTable: detail.dstTable,
      },
    }
    localStorage.setItem(FEE_TOOL_METRICS_LS_KEY, JSON.stringify({ compareCountByState, lastCompareByState }))
  } catch {
    /* ignore quota / privacy mode */
  }
}

/** Lightweight CSV/TSV → columns + row objects (schedules inline preview). */
function parseDelimitedText(text, maxRows = 100_000) {
  const lines = text.split(/\r?\n/).filter((line) => line.length > 0)
  if (lines.length === 0) return { cols: [], rows: [] }
  const first = lines[0]
  const tabCount = (first.match(/\t/g) || []).length
  const commaCount = (first.match(/,/g) || []).length
  const delim = tabCount > commaCount ? '\t' : ','
  const parseLine = (line) => {
    if (delim === '\t') return line.split('\t').map((s) => s.trim())
    const cells = []
    let cur = ''
    let quoted = false
    for (let i = 0; i < line.length; i += 1) {
      const ch = line[i]
      if (ch === '"') {
        quoted = !quoted
      } else if (!quoted && ch === ',') {
        cells.push(cur.trim())
        cur = ''
      } else {
        cur += ch
      }
    }
    cells.push(cur.trim())
    return cells.map((c) => c.replace(/^"|"$/g, ''))
  }
  const headerCells = parseLine(lines[0])
  const cols = headerCells.map((c, i) => (c || `col_${i + 1}`).trim() || `col_${i + 1}`)
  const rows = []
  for (let li = 1; li < lines.length && rows.length < maxRows; li += 1) {
    const cells = parseLine(lines[li])
    if (cells.length === 1 && cells[0] === '') continue
    const row = {}
    cols.forEach((col, j) => {
      row[col] = cells[j] != null ? String(cells[j]) : ''
    })
    rows.push(row)
  }
  return { cols, rows }
}

function formatBytes(n) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  const v = Number(n)
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${(v / (1024 * 1024)).toFixed(1)} MB`
}

/** Matches backend `RunRequest` defaults in app/main.py */
const RUN_DEFAULTS = Object.freeze({
  paginate: true,
  max_pages: 50,
  max_tables: 12,
  /** 0 = unlimited downloads for this run (backend); matches user expectation for “sync everything we found”. */
  maxArtifactDownloads: 0,
})

/** Comma-separated column keys to omit from the table (e.g. sys_id). Set env VITE_HIDE_TABLE_COLUMNS to '' to hide nothing. */
const HIDDEN_COLUMN_KEYS = new Set(
  String(
    import.meta.env.VITE_HIDE_TABLE_COLUMNS !== undefined && import.meta.env.VITE_HIDE_TABLE_COLUMNS !== null
      ? import.meta.env.VITE_HIDE_TABLE_COLUMNS
      : 'sys_id',
  )
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean),
)

function isValidHttpUrl(value) {
  try {
    const u = new URL(value.trim())
    return u.protocol === 'http:' || u.protocol === 'https:'
  } catch {
    return false
  }
}

const NAV = [
  { id: 'schedules', label: 'Fee Schedules', icon: 'layers' },
  { id: 'scheduleVersions', label: 'Schedule versions', icon: 'history' },
  { id: 'mapping', label: 'Mapping', icon: 'columns' },
  { id: 'notifications', label: 'Notifications', icon: 'bell' },
  { id: 'stateUrls', label: 'State URLs', icon: 'link' },
  { id: 'compare', label: 'Compare', icon: 'compare' },
  { id: 'dst', label: 'DST Data', icon: 'database' },
  { id: 'export', label: 'Export', icon: 'export' },
  { id: 'history', label: 'History', icon: 'history' },
]

function NavIcon({ name }) {
  const common = {
    width: 20,
    height: 20,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.75,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': true,
  }
  switch (name) {
    case 'columns':
      return (
        <svg {...common}>
          <rect x="4" y="3" width="6" height="18" rx="1" ry="1" />
          <rect x="14" y="3" width="6" height="18" rx="1" ry="1" />
        </svg>
      )
    case 'layers':
      return (
        <svg {...common}>
          <path d="M12 2L2 7l10 5 10-5-10-5z" />
          <path d="M2 17l10 5 10-5M2 12l10 5 10-5" />
        </svg>
      )
    case 'compare':
      return (
        <svg {...common}>
          <path d="M12 3v18M7 8l-4 4 4 4M17 8l4 4-4 4" />
        </svg>
      )
    case 'export':
      return (
        <svg {...common}>
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
        </svg>
      )
    case 'history':
      return (
        <svg {...common}>
          <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
          <path d="M3 3v5h5M12 7v5l4 2" />
        </svg>
      )
    case 'database':
      return (
        <svg {...common}>
          <ellipse cx="12" cy="5.5" rx="7.5" ry="3" />
          <path d="M4.5 5.5v4.75c0 1.52 3.58 3.25 7.5 3.25s7.5-1.73 7.5-3.25V5.5M4.5 10.25v4.75c0 1.52 3.58 3.25 7.5 3.25s7.5-1.73 7.5-3.25v-4.75" />
        </svg>
      )
    case 'link':
      return (
        <svg {...common}>
          <path d="M10 13a5 5 0 0 1 0-7l1-1a5 5 0 0 1 7 7l-1 1M14 11a5 5 0 0 1 0 7l-1 1a5 5 0 0 1-7-7l1-1" />
        </svg>
      )
    case 'bell':
      return (
        <svg {...common}>
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
      )
    default:
      return null
  }
}

function OverviewMetricCard({ accent, title, value, subtitle, children }) {
  return (
    <article className="app-overview-metric">
      <div
        className={`app-overview-metric__icon app-overview-metric__icon--${accent}`}
        aria-hidden
      >
        {children}
      </div>
      <div className="app-overview-metric__text">
        <p className="app-overview-metric__title">{title}</p>
        <p className="app-overview-metric__value">{value}</p>
        <p className="app-overview-metric__sub">{subtitle}</p>
      </div>
    </article>
  )
}

function formatCellValue(value) {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  if (typeof value === 'boolean') {
    return value ? 'Yes' : 'No'
  }
  if (typeof value === 'number') {
    return String(value)
  }
  if (typeof value === 'string') {
    return value
  }
  return JSON.stringify(value)
}

/** Header-based guess for Facility Rate / Allowed / etc. (preview grids + aligned with backend 2 dp compare for rate-like mapped pairs). */
function feePreviewColumnLooksMonetary(columnName) {
  const t = String(columnName || '').trim().toLowerCase()
  if (!t) return false
  if (/\b(procedure\s*)?(code|modifier|cpt|hcpcs)\b/.test(t) || /^code$/i.test(t.trim())) return false
  if (/\b(date|effective|expire|beg|begin|year|period|descr|describe|modifier)\b/.test(t)) return false
  if (/\brate\b|\bprice\b|\bamount\b|\ballow\b|reimburs|\bpayment\b|\bcost\b|\bcharge\b|\bfee\b/.test(t)) return true
  if (/facility/.test(t) && /rate|amount|fee|price/.test(t)) return true
  if (/non[-.\s_]fac(?:ility)?/.test(t)) return true
  if (/^(fac|nfc|alw)$/i.test(t.trim())) return true
  return false
}

/** Parse currency-like strings for preview; rejects plain integers unless already numeric type from API. */
function parseMoneyLikeForPreview(raw) {
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw
  if (typeof raw !== 'string') return null
  let s = raw.replace(/\u00a0/g, ' ').trim()
  const hadDollar = s.includes('$')
  s = s.replace(/^\$\s*/, '').replace(/,/g, '').trim()
  if (!s || /^[—\-–]+$/.test(s)) return null
  if (/^[-+]?\d+(\.\d+)?([eE][-+]?\d+)?$/i.test(s)) {
    const n = Number(s)
    return Number.isFinite(n) ? n : null
  }
  const n = Number(s)
  if (!Number.isFinite(n)) return null
  return hadDollar || String(raw).trim().includes('.') ? n : null
}

/**
 * Matches Excel-like money display on fee preview tables only — source rows / downloads stay full-precision.
 */
function formatFeeSchedulePreviewCell(columnName, value) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'boolean') return formatCellValue(value)
  const moneyCol = feePreviewColumnLooksMonetary(columnName)
  const n = parseMoneyLikeForPreview(typeof value === 'number' ? value : String(value))
  if (!moneyCol) return formatCellValue(value)
  if (n === null) return formatCellValue(value)
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function pickFeeSchedulePreviewSortColumn(columns) {
  const list = Array.isArray(columns) ? columns.map((c) => String(c)) : []
  for (const c of list) {
    const t = c.toLowerCase()
    if (/\bprocedure\s*code\b/.test(t) || (/\bprocedure\b/.test(t) && /\bcode\b/.test(t))) return c
    if (/^(hcpcs|cpt)\b/i.test(t)) return c
  }
  const direct = list.find((c) => /^code$/i.test(c.trim()))
  if (direct) return direct
  return list[0] ?? null
}

/** Stable alphabetical / numeric-first sort by best-guess procedure column; falls back to first column order. */
function sortFeeSchedulePreviewRows(columns, rows) {
  const r = Array.isArray(rows) ? rows : []
  const cols = Array.isArray(columns) ? columns : []
  if (!cols.length || r.length <= 1) return r
  const key = pickFeeSchedulePreviewSortColumn(cols)
  if (!key || !cols.includes(key)) return r
  return [...r].sort((a, b) =>
    String(a[key] ?? '')
      .trim()
      .localeCompare(String(b[key] ?? '').trim(), undefined, {
        numeric: true,
        sensitivity: 'base',
      }),
  )
}

function sortStringListLocale(arr) {
  return [...arr].sort((a, b) =>
    String(a || '').localeCompare(String(b || ''), undefined, { sensitivity: 'base', numeric: true }),
  )
}

/** Infer file / action kind from URL — state-agnostic (any portal). */
function inferLinkKindFromUrl(url) {
  const u = (url || '').trim().toLowerCase()
  if (!u) return 'empty'
  if (u.startsWith('javascript:') || u.includes('__dopostback')) return 'portal'
  if (u.includes('.pdf')) return 'pdf'
  if (u.includes('.xlsx')) return 'xlsx'
  if (u.includes('.xls')) return 'xls'
  if (u.includes('.csv')) return 'csv'
  if (u.includes('.zip')) return 'zip'
  if (u.startsWith('http://') || u.startsWith('https://')) return 'http'
  return 'other'
}

/** Map anchor URLs → backend document_hint (pdf | spreadsheet | csv | undefined). */
function previewDocumentHintFromLink(url, label) {
  const u = (url || '').trim()
  const ul = u.toLowerCase()
  const labL = (label || '').toLowerCase()

  const qpLooksPdf = /(^|[?&#])(format|filetype|type)=[^&#]*pdf|content[_-]?type=[^&#]*pdf/.test(ul)
  const looksPdfPath = /\.pdf(?:\?|#|$)/.test(ul) || ul.includes('.pdf?') || qpLooksPdf

  const k = inferLinkKindFromUrl(u)

  if (looksPdfPath || k === 'pdf' || labL.includes('pdf') || /\bacrobat\b/.test(labL)) return 'pdf'
  if (k === 'csv' || /\bcsv\b/.test(labL)) return 'csv'
  if (['xlsx', 'xls'].includes(k) || /\bexcel|xlsx|spreadsheet|workbook\b/.test(labL)) {
    return 'spreadsheet'
  }
  return null
}

function columnExpectsLinkKinds(columnName) {
  const t = (columnName || '').toLowerCase()
  const kinds = new Set()
  if (/\bexcel\b|spreadsheet|workbook|\bxls\b|\.xls/.test(t)) {
    kinds.add('xls')
    kinds.add('xlsx')
  }
  if (/\bcsv\b/.test(t)) kinds.add('csv')
  if (/pdf|acrobat|adobe/.test(t)) kinds.add('pdf')
  if (/zip|archive|compressed/.test(t)) kinds.add('zip')
  if (
    /download|attachment|document|\bfile\b|format|resource|manual|publication|hyperlink|\blink\b/.test(t)
  ) {
    kinds.add('any_http')
  }
  return kinds
}

function linkMatchesColumnKinds(url, kinds) {
  const ik = inferLinkKindFromUrl(url)
  if (kinds.has('pdf') && ik === 'pdf') return true
  if ((kinds.has('xls') || kinds.has('xlsx')) && (ik === 'xls' || ik === 'xlsx')) return true
  if (kinds.has('csv') && ik === 'csv') return true
  if (kinds.has('zip') && ik === 'zip') return true
  if (kinds.has('any_http') && ['pdf', 'xls', 'xlsx', 'csv', 'zip', 'http'].includes(ik)) return true
  return false
}

function firstMatchingLinkForColumn(column, row) {
  const kinds = columnExpectsLinkKinds(column)
  if (kinds.size === 0) return null
  const links = Array.isArray(row?._links) ? row._links : []
  for (const ln of links) {
    const u = (ln?.url || '').trim()
    if (u && linkMatchesColumnKinds(u, kinds)) return u
  }
  return null
}

/** Prefer a column whose header reads like the fee schedule title (state portals vary). */
function pickFeeScheduleNameColumn(cols) {
  if (!Array.isArray(cols) || cols.length === 0) return null
  const scored = cols.map((c) => ({ c, t: String(c).toLowerCase() }))
  for (const { c, t } of scored) {
    if (/\bfee\s*schedule\b/.test(t)) return c
  }
  for (const { c, t } of scored) {
    if (/\bschedule\b/.test(t) && !/\bprogram\b/.test(t)) return c
  }
  for (const { c, t } of scored) {
    if (/\bschedule\b/.test(t)) return c
  }
  for (const { c, t } of scored) {
    if (/\btitle\b|\bname\b|\bdocument\b|\bdescription\b/.test(t)) return c
  }
  if (cols.length >= 2) {
    const p0 = String(cols[0]).toLowerCase()
    if (p0.includes('program')) return cols[1]
  }
  return cols[0]
}

/**
 * Best download / preview target for a catalog row (Excel/PDF column first, then any HTTP, then portal actions).
 * @returns {{ url: string, docLabel: string, portal: boolean } | null}
 */
function primaryCatalogRowLink(row, columns) {
  if (!row || typeof row !== 'object') return null
  const cols = Array.isArray(columns) ? columns : []
  const ranked = [...cols].sort((a, b) => {
    const score = (c) => {
      const t = String(c).toLowerCase()
      let s = 0
      if (/\bexcel\b|\.xls|spreadsheet|workbook/.test(t)) s += 6
      if (/\bpdf\b|acrobat/.test(t)) s += 4
      if (/download|attachment|file|hyperlink/.test(t)) s += 2
      return s
    }
    return score(b) - score(a)
  })
  for (const col of ranked) {
    const u = firstMatchingLinkForColumn(col, row)
    if (u && isValidHttpUrl(u)) {
      const raw = row[col]
      const docLabel = typeof raw === 'string' ? raw.trim() : ''
      return { url: u, docLabel, portal: false }
    }
  }
  const links = Array.isArray(row._links) ? row._links : []
  for (const ln of links) {
    const raw = (ln?.url || '').trim()
    const docLabel = (ln?.text || '').trim()
    if (!raw) continue
    if (isPortalActionUrl(raw)) return { url: raw, docLabel, portal: true }
    if (isValidHttpUrl(raw)) return { url: raw, docLabel, portal: false }
  }
  return null
}

function feeScheduleLabelFromRow(dataRow, nameCol) {
  if (!nameCol || !dataRow) return ''
  const raw = dataRow[nameCol]
  if (typeof raw === 'string') return raw.trim()
  if (raw != null && raw !== '') {
    const s = String(formatCellValue(raw)).trim()
    return s === '—' ? '' : s
  }
  return ''
}

function base64ToBlob(base64, mime) {
  const byteChars = atob(base64)
  const bytes = new Uint8Array(byteChars.length)
  for (let i = 0; i < byteChars.length; i += 1) {
    bytes[i] = byteChars.charCodeAt(i)
  }
  return new Blob([bytes], { type: mime || 'application/octet-stream' })
}

/** Payload from `POST /preview/snippet` — format-agnostic beyond MIME/kind hints. */
async function fetchPreviewSnippet({ resourceUrl, referrerUrl, sessionId, documentHint }) {
  const res = await fetch(`${API_BASE}/preview/snippet`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      resource_url: resourceUrl,
      referrer_url: referrerUrl || null,
      session_id: sessionId || null,
      document_hint: documentHint || null,
    }),
  })
  let data = {}
  try {
    data = await res.json()
  } catch {
    /* plain text bodies */
  }
  if (!res.ok) {
    const msg =
      typeof data?.detail === 'string'
        ? data.detail
        : Array.isArray(data?.detail)
          ? data.detail.map((d) => d?.msg || String(d)).join('; ')
        : `Preview request failed (${res.status})`
    throw new Error(msg)
  }
  return data
}

async function downloadViaProxy({ resourceUrl, referrerUrl, sessionId, documentHint }) {
  const res = await fetch(`${API_BASE}/preview/proxy`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      resource_url: resourceUrl,
      referrer_url: referrerUrl || null,
      session_id: sessionId || null,
      document_hint: documentHint || null,
    }),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(txt.slice(0, 180) || `Download failed (${res.status})`)
  }
  const blob = await res.blob()
  let name =
    resourceUrl
      .replace(/\\/g, '/')
      .trim()
      .split('/')
      .pop() || 'download'
  if (!name.includes('.')) name = `${name}.bin`
  const objUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objUrl
  a.download = name
  a.rel = 'noreferrer'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(objUrl), 60_000)
}

/** Human-readable value for summary lines (objects / arrays as JSON). */
function formatSummaryValue(value) {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  if (typeof value === 'object') {
    return JSON.stringify(value, null, 2)
  }
  return String(value)
}

async function readHttpErrorMessage(res, fallback) {
  const text = await res.text()
  if (!text) return fallback
  try {
    const j = JSON.parse(text)
    if (typeof j.detail === 'string') return j.detail
    if (Array.isArray(j.detail)) {
      return j.detail
        .map((d) => (typeof d === 'string' ? d : d?.msg ?? JSON.stringify(d)))
        .join('; ')
    }
    if (j.detail != null) return String(j.detail)
    if (j.message != null) return String(j.message)
    if (j.error != null) return String(j.error)
  } catch {
    /* plain text */
  }
  return text || fallback
}

/**
 * Column order: API `columns` first (when present), then any extra keys seen in any row
 * so different states / ragged rows still render every cell.
 */
function getTableColumns(table) {
  const rows = Array.isArray(table?.rows) ? table.rows : []
  const keysFromRows = new Set()
  for (const row of rows) {
    if (row && typeof row === 'object') {
      for (const k of Object.keys(row)) {
        if (k !== '_links') keysFromRows.add(k)
      }
    }
  }

  const declared = Array.isArray(table?.columns) ? table.columns.map((c) => String(c)) : []
  const ordered = []
  const seen = new Set()

  for (const c of declared) {
    if (!seen.has(c)) {
      ordered.push(c)
      seen.add(c)
    }
  }
  for (const c of keysFromRows) {
    if (!seen.has(c)) {
      ordered.push(c)
      seen.add(c)
    }
  }

  return ordered.filter((c) => !HIDDEN_COLUMN_KEYS.has(c))
}

function downloadTableAsCsv(baseName, columns, rows) {
  const esc = (v) => {
    if (v === null || v === undefined) return ''
    if (typeof v === 'object') return `"${JSON.stringify(v).replace(/"/g, '""')}"`
    const s = String(v)
    if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`
    return s
  }
  const safeCols = Array.isArray(columns) ? columns : []
  const lineRows = Array.isArray(rows) ? rows : []
  const lines = [safeCols.join(',')]
  for (const row of lineRows) {
    lines.push(safeCols.map((c) => esc(row && typeof row === 'object' ? row[c] : '')).join(','))
  }
  const blob = new Blob([lines.join('\r\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  const name = (baseName || 'fee_schedule').replace(/[^\w.-]+/g, '_').slice(0, 120)
  a.href = url
  a.download = name.toLowerCase().endsWith('.csv') ? name : `${name}.csv`
  a.rel = 'noreferrer'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

function normalizeDstColKey(c) {
  return String(c ?? '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '_')
    .replace(/[^a-z0-9_]/g, '')
}

const DST_EFFECTIVE_DATE_COLUMN_KEYS = [
  'effective_date',
  'effectivedate',
  'eff_date',
  'effdate',
  'start_date',
  'startdate',
  'fee_effective_date',
  'schedule_effective_date',
]

/** Pick logical column holding row effective/start date for range filters / dropdowns. */
function pickDstEffectiveDateColumn(columns) {
  if (!Array.isArray(columns) || columns.length === 0) return null
  const normToOriginal = new Map()
  for (const col of columns) {
    const k = normalizeDstColKey(col)
    if (k && !normToOriginal.has(k)) normToOriginal.set(k, String(col))
  }
  for (const want of DST_EFFECTIVE_DATE_COLUMN_KEYS) {
    if (normToOriginal.has(want)) return normToOriginal.get(want)
  }
  for (const col of columns) {
    const k = normalizeDstColKey(col)
    if (k.includes('effective') && k.includes('date')) return String(col)
  }
  for (const col of columns) {
    const k = normalizeDstColKey(col)
    if (k.endsWith('_date') || k.endsWith('date')) {
      if (k.includes('term') || k.includes('end') || k.includes('expir')) continue
      return String(col)
    }
  }
  return null
}

function parseScheduleCellDate(raw) {
  if (raw == null || raw === '') return null
  if (raw instanceof Date && !Number.isNaN(raw.getTime())) {
    return new Date(raw.getFullYear(), raw.getMonth(), raw.getDate())
  }
  if (typeof raw === 'number' && Number.isFinite(raw) && raw > 1e12) {
    return parseScheduleCellDate(new Date(raw))
  }
  const s = String(raw).trim()
  if (!s) return null

  const isoDay = s.match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (isoDay) {
    const y = Number(isoDay[1])
    const mo = Number(isoDay[2]) - 1
    const d = Number(isoDay[3])
    if (y >= 1900 && y <= 2100 && mo >= 0 && mo <= 11 && d >= 1 && d <= 31) return new Date(y, mo, d)
  }
  const mdy = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/)
  if (mdy) {
    const mm = Number(mdy[1])
    const dd = Number(mdy[2])
    let yy = Number(mdy[3])
    if (yy < 100) yy += 2000
    if (mm >= 1 && mm <= 12 && dd >= 1 && dd <= 31 && yy >= 1900) return new Date(yy, mm - 1, dd)
  }
  const t = Date.parse(s)
  if (!Number.isNaN(t)) {
    const dt = new Date(t)
    return new Date(dt.getFullYear(), dt.getMonth(), dt.getDate())
  }
  return null
}

function formatIsoDayLocal(d) {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** Display US date similar to DST text (MM/DD/YYYY). */
function formatUsSlashDateLocal(d) {
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const yy = d.getFullYear()
  return `${mm}/${dd}/${yy}`
}

function displayLabelForIsoDay(isoDay) {
  if (!isoDay || !/^\d{4}-\d{2}-\d{2}$/.test(isoDay)) return isoDay
  const [y, m, d] = isoDay.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  if (Number.isNaN(dt.getTime())) return isoDay
  return formatUsSlashDateLocal(dt)
}

function uniqueSortedIsoDaysFromRows(rows, dateCol) {
  if (!dateCol || !Array.isArray(rows)) return []
  const seen = new Set()
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const dt = parseScheduleCellDate(row[dateCol])
    if (!dt) continue
    seen.add(formatIsoDayLocal(dt))
  }
  return Array.from(seen).sort()
}

function filterDstRowsByEffectiveRange(rows, dateCol, startIsoDay) {
  if (!dateCol || !startIsoDay || !Array.isArray(rows)) return []
  const m = startIsoDay.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (!m) return []
  const y0 = Number(m[1])
  const mo0 = Number(m[2]) - 1
  const d0 = Number(m[3])
  const start = new Date(y0, mo0, d0)
  const end = new Date(y0, 11, 31)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return []
  const startKey = formatIsoDayLocal(start)
  const endKey = formatIsoDayLocal(end)
  return rows.filter((row) => {
    if (!row || typeof row !== 'object') return false
    const dt = parseScheduleCellDate(row[dateCol])
    if (!dt) return false
    const k = formatIsoDayLocal(dt)
    return k >= startKey && k <= endKey
  })
}

function downloadTableAsXlsx(baseName, columns, rows) {
  const safeCols = Array.isArray(columns) ? columns : []
  const lineRows = Array.isArray(rows) ? rows : []
  const aoa = [safeCols, ...lineRows.map((r) => safeCols.map((c) => (r && typeof r === 'object' ? r[c] : '')))]
  const ws = XLSX.utils.aoa_to_sheet(aoa)
  const wb = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(wb, ws, 'FeeData')
  triggerXlsxDownload(baseName, wb)
}

/** Excel worksheet name constraints (avoid invalid workbook parts). */
function feeCompareExcelSheetName(title) {
  const t = String(title || 'sheet')
    .replace(/[\]\\/\*\?\:]/g, ' ')
    .replace(/\[/g, ' ')
    .trim()
    .slice(0, 31)
  return t.length ? t : 'Sheet'
}

function triggerXlsxDownload(baseName, wb) {
  const out = XLSX.write(wb, { bookType: 'xlsx', type: 'array' })
  const blob = new Blob([out], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  const name = (baseName || 'fee_schedule').replace(/[^\w.-]+/g, '_').slice(0, 160)
  a.href = url
  a.download = name.toLowerCase().endsWith('.xlsx') ? name : `${name}.xlsx`
  a.rel = 'noreferrer'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/** Same precedence as compare UI tables: ``field_diffs`` first (aligned with mapping), then row blobs. */
function compareExportStateCell(pairIndex, pair, row) {
  const fd = Array.isArray(row?.field_diffs) ? row.field_diffs[pairIndex] : null
  if (fd != null && 'state_value' in fd) return String(fd.state_value ?? '')
  const sr = row?.state_row && typeof row.state_row === 'object' ? row.state_row : {}
  const sk = String(pair.state_column)
  return feeCompareLoosePick(sr, sk)
}

/** DST side — ``field_diffs`` first so exports match modal cells. */
function compareExportDstCell(pairIndex, pair, row) {
  const fd = Array.isArray(row?.field_diffs) ? row.field_diffs[pairIndex] : null
  if (fd != null && 'dst_value' in fd) return String(fd.dst_value ?? '')
  const dr = row?.dst_row && typeof row.dst_row === 'object' ? row.dst_row : {}
  const dk = String(pair.dst_column)
  return feeCompareLoosePick(dr, dk)
}

/** Worksheet AoA — State fee file column names only. */
function buildStateFeeCompareSheetAoA(result, rowsSubset) {
  const pairs = Array.isArray(result?.column_pairs) ? result.column_pairs : []
  const list = Array.isArray(rowsSubset) ? rowsSubset : []
  const header = pairs.map((p) => String(p.state_column))
  const lines = []
  for (const row of list) {
    if (!row || typeof row !== 'object') continue
    lines.push(pairs.map((p, pi) => compareExportStateCell(pi, p, row)))
  }
  return header.length ? [header, ...lines] : [['(no mapped state columns)']]
}

/** Worksheet AoA — DST warehouse column names only. */
function buildDstFeeCompareSheetAoA(result, rowsSubset) {
  const pairs = Array.isArray(result?.column_pairs) ? result.column_pairs : []
  const list = Array.isArray(rowsSubset) ? rowsSubset : []
  const header = pairs.map((p) => String(p.dst_column))
  const lines = []
  for (const row of list) {
    if (!row || typeof row !== 'object') continue
    lines.push(pairs.map((p, pi) => compareExportDstCell(pi, p, row)))
  }
  return header.length ? [header, ...lines] : [['(no mapped DST columns)']]
}

/**
 * One workbook:
 * **Modified** & **Added in State** — state column layout only (`state_row`).
 * **DST not in State** — DST column layout only (`dst_row`).
 */
function downloadFeeCompareWorkbook(baseName, result, rowsSubset) {
  const list = Array.isArray(rowsSubset) ? rowsSubset.filter(Boolean) : []
  if (!list.length) {
    window.alert('Nothing to export for this selection.')
    return
  }
  const mod = list.filter((r) => r?.status === 'mismatch')
  const addedInState = list.filter((r) => r?.status === 'state_only')
  const dstNotInState = list.filter((r) => r?.status === 'dst_only')
  if (mod.length + addedInState.length + dstNotInState.length === 0) {
    window.alert('No modified, added-in-state, or DST-not-in-state rows in this selection.')
    return
  }

  const wb = XLSX.utils.book_new()
  /** @type {Array<[string, ReturnType<typeof buildStateFeeCompareSheetAoA> | ReturnType<typeof buildDstFeeCompareSheetAoA>]>} */
  const sheetSpecs = [
    ['Modified', buildStateFeeCompareSheetAoA(result, mod)],
    ['Added in State', buildStateFeeCompareSheetAoA(result, addedInState)],
    ['DST not in State', buildDstFeeCompareSheetAoA(result, dstNotInState)],
  ]

  for (const [title, aoa] of sheetSpecs) {
    const ws = XLSX.utils.aoa_to_sheet(aoa)
    XLSX.utils.book_append_sheet(wb, ws, feeCompareExcelSheetName(title))
  }

  triggerXlsxDownload(baseName, wb)
}

function dstSchedulesPreviewCacheKey(stateCode, fsName) {
  return `${(stateCode || '').trim().toUpperCase() || '-'}::${fsName}`
}

function trimSchedulesPreviewMap(map, maxSize) {
  while (map.size > maxSize) {
    const k = map.keys().next().value
    map.delete(k)
  }
}

/**
 * Tabular preview, PDF blob, or throws — fee-schedule modal + background prefetch.
 * @returns {Promise<{ kind: 'table', cols: string[], rows: Record<string, unknown>[] } | { kind: 'pdf', blob: Blob }>}
 */
async function fetchSchedulesArtifactPreview(id, artifacts, signal) {
  const art = artifacts.find((a) => Number(a.artifact_id) === Number(id))
  const name = ((art?.original_filename || '') + '').toLowerCase()
  const metaMime = ((art?.mime_type || '') + '').toLowerCase()

  let serverTableHint = ''
  try {
    const pr = await fetch(`${API_BASE}/app/artifacts/${id}/preview-table`, { signal })
    const raw = await pr.text()
    if (pr.ok) {
      const payload = JSON.parse(raw)
      const cols = Array.isArray(payload?.columns) ? payload.columns : []
      const grid = Array.isArray(payload?.rows) ? payload.rows : []
      if (cols.length > 0) {
        const rows = grid.map((cells) => {
          const row = {}
          cols.forEach((c, j) => {
            row[c] = cells[j] != null ? String(cells[j]) : ''
          })
          return row
        })
        return { kind: 'table', cols, rows }
      }
    } else {
      try {
        const j = JSON.parse(raw)
        if (typeof j.detail === 'string') serverTableHint = j.detail
        else if (j.detail != null) serverTableHint = String(j.detail)
      } catch {
        if (raw.trim()) serverTableHint = raw.trim().slice(0, 400)
      }
    }
  } catch {
    /* fall through */
  }

  const res = await fetch(`${API_BASE}/app/artifacts/${id}/file`, { signal })
  if (!res.ok) throw new Error(await readHttpErrorMessage(res, `File failed (${res.status})`))
  const ct = (res.headers.get('content-type') || metaMime || '').split(';')[0].trim().toLowerCase()
  const buf = await res.arrayBuffer()

  const u8 = new Uint8Array(buf.byteLength ? buf.slice(0, 5) : [])
  const magicPdf = buf.byteLength >= 5 && u8[0] === 0x25 && u8[1] === 0x50 && u8[2] === 0x44 && u8[3] === 0x46

  const looksPdf = magicPdf || ct.includes('pdf') || name.endsWith('.pdf')
  const looksCsv = ct.includes('csv') || name.endsWith('.csv')
  const looksXlsHint =
    ct.includes('spreadsheet') ||
    ct.includes('excel') ||
    name.endsWith('.xlsx') ||
    name.endsWith('.xlsm') ||
    name.endsWith('.xls')
  const looksJson = ct.includes('json') || name.endsWith('.json')
  const looksText = ct.startsWith('text/') || ct.includes('plain')

  if (looksPdf) {
    const blob = new Blob([buf], { type: 'application/pdf' })
    return { kind: 'pdf', blob }
  }

  const dec = new TextDecoder('utf-8')
  const text = dec.decode(buf)

  if (looksJson) {
    try {
      const j = JSON.parse(text)
      if (Array.isArray(j) && j.length > 0 && j[0] != null && typeof j[0] === 'object' && !Array.isArray(j[0])) {
        const cols = Object.keys(j[0])
        const rows = j.slice(0, 100_000).map((obj) => {
          const row = {}
          cols.forEach((c) => {
            row[c] = obj[c]
          })
          return row
        })
        return { kind: 'table', cols, rows }
      }
    } catch {
      /* fall through */
    }
  }
  if (looksCsv || looksText) {
    const { cols, rows } = parseDelimitedText(text, 100_000)
    if (cols.length > 0 && rows.length > 0) {
      return { kind: 'table', cols, rows }
    }
    if (looksCsv && cols.length > 0) {
      return { kind: 'table', cols, rows }
    }
  }
  throw new Error(
    serverTableHint ||
      (looksXlsHint
        ? 'Spreadsheet preview failed. Try Download, or confirm the API has openpyxl and pdfplumber installed.'
        : 'Preview is not available for this file type.'),
  )
}

async function fetchDstSchedulesPreviewRows(rawTable, fsName, stateCode, signal) {
  const q = new URLSearchParams({ table: rawTable, limit: '5000', fs_name: fsName })
  if (stateCode) q.set('state_code', stateCode)
  const res = await fetch(`${API_BASE}/dst/rows?${q}`, { signal })
  if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Rows failed (${res.status})`))
  const data = await res.json()
  return {
    columns: Array.isArray(data?.columns) ? data.columns : [],
    rows: Array.isArray(data?.rows) ? data.rows : [],
  }
}

/** ASP.NET / WebForms — not a fetchable URL from this app (needs original page state). */
function isPortalActionUrl(url) {
  if (!url || typeof url !== 'string') {
    return true
  }
  const u = url.trim().toLowerCase()
  return u.startsWith('javascript:') || u.includes('__dopostback')
}

/** Per-table fields other than tabular body — shown as a compact summary (structure varies by state / source). */
const TABLE_LAYOUT_KEYS = new Set(['rows', 'columns'])

function getTableSummaryEntries(table) {
  if (!table || typeof table !== 'object') {
    return []
  }
  return Object.entries(table).filter(([k]) => !TABLE_LAYOUT_KEYS.has(k))
}

/** Top-level JSON from `POST /run` except large blobs we render elsewhere. */
function getPipelineSummaryEntries(result) {
  if (!result || typeof result !== 'object') {
    return []
  }
  return Object.entries(result).filter(([k]) => {
    if (k === 'catalog_tables') return false
    if (k === 'preview_auth') return false
    if (k === 'analysis' && result.blocked) return false
    return true
  })
}

/** Joined-row exact-match % (mapped fields identical) — uses backend summary. */
function feeCompareJoinedMatchPct(summary) {
  if (!summary || typeof summary !== 'object') return null
  const mc = Number(summary.match_count)
  const mis = Number(summary.mismatch_count)
  if (!Number.isFinite(mc) || !Number.isFinite(mis)) return null
  const denom = mc + mis
  if (denom <= 0) return null
  return (100 * mc) / denom
}

/**
 * Fallback when no DST effective-date filter was chosen: pick the strongest matching column from row blobs.
 * Scores explicit EFFECTIVE_DATE above loose EFFECT so we don’t grab an unrelated “effect_*” field first.
 */
function guessEffectiveDateHintFromCompareRow(row) {
  if (!row || typeof row !== 'object') return ''
  const tryObj = row.dst_row && typeof row.dst_row === 'object' ? row.dst_row : row.state_row
  if (!tryObj || typeof tryObj !== 'object') return ''
  /** @type {{ score: number, value: string }[]} */
  const hits = []
  for (const k of Object.keys(tryObj)) {
    const rawKey = String(k).trim()
    const up = rawKey.toUpperCase().replace(/\s+/g, '_')
    let score = 0
    if (/^EFFECTIVE_?DATE$/i.test(up) || /^EFF_?DATE$/i.test(up)) score = 120
    else if (/EFFECTIVE.?DATE|EFFECTIVEDATE/i.test(up)) score = 110
    else if (/BEGIN_?DATE|START_?DATE/i.test(up)) score = 85
    else if (/EFFDATE/i.test(up)) score = 75
    else if (/EFFECT/i.test(up)) score = 35
    else continue
    const v = tryObj[k]
    const sv = v != null ? String(v).trim() : ''
    if (!sv) continue
    hits.push({ score, value: sv })
  }
  hits.sort((a, b) => b.score - a.score || b.value.length - a.value.length)
  return hits[0]?.value || ''
}

/** Compact display for Recent table — parsable timestamps → calendar date in local time (en-CA ≈ yyyy-mm-dd). */
function feeCompareFormatEffectiveHint(raw) {
  const s = String(raw ?? '').trim()
  if (!s) return '—'
  const t = Date.parse(s)
  if (!Number.isNaN(t)) return new Date(t).toLocaleDateString('en-CA')
  return s
}

const FEE_TOOL_RECENT_COMPARES_LS = 'fee_tool_recent_compares_v1'
const FEE_TOOL_RECENT_COMPARES_MAX = 35
/** Align with backend compare `max_result_rows` (5000) so Recent replay uses the same `rows` as a live compare. */
const FEE_TOOL_RECENT_COMPARE_CHANGED_CAP = 5000

/** Case / NFKC tolerant pick from API row blobs when keys drift from mapping labels. */
function feeCompareLoosePick(obj, preferredKey) {
  if (!obj || typeof obj !== 'object') return ''
  const pk = String(preferredKey).trim()
  if (pk in obj) return String(obj[pk] ?? '')
  const nk = pk.normalize ? pk.normalize('NFKC').toLowerCase() : pk.toLowerCase()
  for (const k of Object.keys(obj)) {
    const ks = String(k).trim()
    const cmp = ks.normalize ? ks.normalize('NFKC').toLowerCase() : ks.toLowerCase()
    if (cmp === nk) return String(obj[k] ?? '')
  }
  return ''
}

function readFeeToolRecentCompares() {
  try {
    const raw = localStorage.getItem(FEE_TOOL_RECENT_COMPARES_LS)
    if (!raw) return []
    const j = JSON.parse(raw)
    return Array.isArray(j) ? j : []
  } catch {
    return []
  }
}

function isLocalStorageQuotaError(e) {
  if (!e || typeof e !== 'object') return false
  const name = /** @type {Error} */ (e).name || ''
  const code = /** @type {DOMException} */ (e).code
  return name === 'QuotaExceededError' || name === 'NS_ERROR_DOM_QUOTA_REACHED' || code === 22
}

/** Remove row blobs so old entries still show “Run at” / summary while freeing quota. Keeps snapshotTierCounts as stored. */
function feeRecentEntryStripPreviewRows(entry) {
  if (!entry || typeof entry !== 'object') return entry
  const snap = entry.snapshot && typeof entry.snapshot === 'object' ? entry.snapshot : null
  if (!snap) return entry
  return {
    ...entry,
    snapshot: {
      ...snap,
      rows: [],
      truncatedNote: snap.truncatedNote || 'preview_cleared_storage',
    },
  }
}

function feeRecentRebuildTierCountsForRows(rows) {
  const r = Array.isArray(rows) ? rows : []
  return {
    mismatch: r.reduce((n, row) => n + (row?.status === 'mismatch' ? 1 : 0), 0),
    state_only: r.reduce((n, row) => n + (row?.status === 'state_only' ? 1 : 0), 0),
    dst_only: r.reduce((n, row) => n + (row?.status === 'dst_only' ? 1 : 0), 0),
  }
}

/** Split rows into compare buckets preserving within-bucket order (matches API semantics). Ignores `match` rows — not shown in reopen tabs anyway. */
function feeComparePartitionNonMatchRows(rows) {
  /** @type {unknown[]} */
  const mish = []
  /** @type {unknown[]} */
  const stateOnly = []
  /** @type {unknown[]} */
  const dstOnly = []
  for (const r of rows) {
    const st = r?.status
    if (st === 'mismatch') mish.push(r)
    else if (st === 'state_only') stateOnly.push(r)
    else if (st === 'dst_only') dstOnly.push(r)
  }
  return { mish, stateOnly, dstOnly }
}

/**
 * Apportion row budget across three buckets proportional to `(nM,nS,nD)` without exceeding caps.
 *
 * @returns {[number, number, number]}
 */
function feeTripleRowBudget(budget, nM, nS, nD) {
  nM = Math.max(0, Math.floor(Number(nM) || 0))
  nS = Math.max(0, Math.floor(Number(nS) || 0))
  nD = Math.max(0, Math.floor(Number(nD) || 0))
  const total = nM + nS + nD
  if (total === 0 || budget <= 0) return [0, 0, 0]
  const b = Math.min(Math.floor(Number(budget) || 0), total)

  /** @type {number[]} */
  const limits = [nM, nS, nD]
  if (b >= total) return /** @type {const} */ ([nM, nS, nD])

  const exact = [(nM * b) / total, (nS * b) / total, (nD * b) / total]
  /** @type {number[]} */
  let alloc = exact.map(Math.floor)
  alloc = alloc.map((a, i) => Math.min(Math.max(0, a), limits[i]))
  let assigned = alloc[0] + alloc[1] + alloc[2]
  const order = /** @type {const} */ ([0, 1, 2]).sort((i, j) => exact[j] - Math.floor(exact[j]) - (exact[i] - Math.floor(exact[i])))

  let guard = 0
  while (assigned < b && guard < total + 8) {
    guard += 1
    /** @type {boolean} */
    let progressed = false
    for (const i of order) {
      if (assigned >= b) break
      if (alloc[i] < limits[i]) {
        alloc[i] += 1
        assigned += 1
        progressed = true
      }
    }
    if (!progressed) break
  }
  while (assigned < b) {
    /** @type {boolean} */
    let stepped = false
    for (let i = 0; i < 3; i += 1) {
      if (assigned >= b) break
      if (alloc[i] < limits[i]) {
        alloc[i] += 1
        assigned += 1
        stepped = true
        break
      }
    }
    if (!stepped) break
  }

  return /** @type {const} */ ([alloc[0], alloc[1], alloc[2]])
}

function feeCompareBuildStratifiedNonMatchSlice(rows, budget) {
  const { mish, stateOnly, dstOnly } = feeComparePartitionNonMatchRows(rows)
  const lm = mish.length
  const ls = stateOnly.length
  const ld = dstOnly.length
  const combined = lm + ls + ld
  if (combined === 0) return { storedRows: [], diffCombined: 0 }
  const b = Math.min(Math.max(0, Math.floor(Number(budget) || 0)), combined)
  const [bm, bso, bdo] = feeTripleRowBudget(b, lm, ls, ld)
  const storedRows = [...mish.slice(0, bm), ...stateOnly.slice(0, bso), ...dstOnly.slice(0, bdo)]
  return { storedRows, diffCombined: combined }
}

/** Slim a saved entry under quota — must stay stratified; naive slice(0..N) can wipe Added / DST buckets. */
function feeRecentEntryRetainPreviewRows(entry, maxRows) {
  if (!entry?.snapshot?.rows || !Array.isArray(entry.snapshot.rows)) return entry
  const rs = entry.snapshot.rows
  if (!(Number(maxRows) >= 0)) return entry

  const { storedRows: nextFull, diffCombined } = feeCompareBuildStratifiedNonMatchSlice(rs, maxRows)

  const nextLen = nextFull.length
  if (!(nextLen < rs.length) && !(diffCombined > nextLen)) return entry

  return {
    ...entry,
    snapshotTierCounts: feeRecentRebuildTierCountsForRows(nextFull),
    snapshot: {
      ...entry.snapshot,
      rows: nextFull,
      truncatedNote:
        diffCombined > nextFull.length ? 'truncated_snapshot' : entry.snapshot?.truncatedNote ?? null,
    },
  }
}

/**
 * Persist recent compare list. Aggressively frees space when localStorage quota is exceeded
 * so “Latest compared” (tiny metrics blob) doesn’t drift ahead of this table silently.
 *
 * @returns {boolean}
 */
function pushFeeToolRecentCompare(payload) {
  if (!payload || typeof payload !== 'object') return false
  const writeList = (list) => {
    localStorage.setItem(
      FEE_TOOL_RECENT_COMPARES_LS,
      JSON.stringify(list.slice(0, FEE_TOOL_RECENT_COMPARES_MAX)),
    )
  }

  try {
    let cur = readFeeToolRecentCompares()
    cur.unshift(payload)
    cur = cur.slice(0, FEE_TOOL_RECENT_COMPARES_MAX)

    try {
      writeList(cur)
      return true
    } catch (e) {
      if (!isLocalStorageQuotaError(e)) throw e
    }

    for (let i = cur.length - 1; i >= 1; i -= 1) {
      cur[i] = feeRecentEntryStripPreviewRows(cur[i])
      try {
        writeList(cur)
        return true
      } catch (err) {
        if (!isLocalStorageQuotaError(err)) throw err
      }
    }

    while (cur.length > 1) {
      cur.pop()
      try {
        writeList(cur)
        return true
      } catch (err2) {
        if (!isLocalStorageQuotaError(err2)) throw err2
      }
    }

    const caps = [3000, 2000, 1000, 500, 200, 100, 50, 0]
    for (const cap of caps) {
      cur[0] = feeRecentEntryRetainPreviewRows(payload, cap)
      try {
        writeList(cur)
        return true
      } catch (err3) {
        if (!isLocalStorageQuotaError(err3)) throw err3
      }
    }

    return false
  } catch {
    return false
  }
}

/**
 * Persist reopen rows: proportional slice across Modified / Added in State / DST-not-in-State.
 * Plain `slice(0, N)` wipes later buckets because the API often groups rows by status.
 */
function snapshotCompareForRecentHistory(fullResult, context) {
  const rows = Array.isArray(fullResult?.rows) ? fullResult.rows : []
  const pairs = Array.isArray(fullResult?.column_pairs) ? fullResult.column_pairs : []
  const summary = fullResult.summary && typeof fullResult.summary === 'object' ? fullResult.summary : {}
  const { storedRows, diffCombined } = feeCompareBuildStratifiedNonMatchSlice(rows, FEE_TOOL_RECENT_COMPARE_CHANGED_CAP)

  const snapshotTierCounts = {
    mismatch: storedRows.filter((r) => r?.status === 'mismatch').length,
    state_only: storedRows.filter((r) => r?.status === 'state_only').length,
    dst_only: storedRows.filter((r) => r?.status === 'dst_only').length,
  }
  const effectiveRow = storedRows.find((r) => r?.status !== 'match') ?? storedRows[0]
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
    at: new Date().toISOString(),
    stateCode: String(fullResult?.state_code || context.stateCode || '').toUpperCase(),
    stateDisplay: context.stateDisplay || '',
    dstFsname: String(fullResult?.dst_fsname || context.dstTable || '').trim(),
    artifactId: Number(fullResult?.artifact_id) || context.artifactId,
    artifactLabel: context.artifactLabel || '',
    matchPct: feeCompareJoinedMatchPct(summary),
    summary,
    snapshotTierCounts,
    effectiveHint:
      String(context.effectiveDateIso ?? '').trim() ||
      guessEffectiveDateHintFromCompareRow(effectiveRow) ||
      '—',
    snapshot: {
      column_pairs: pairs,
      rows: storedRows,
      truncatedNote: diffCombined > storedRows.length ? 'truncated_snapshot' : null,
    },
  }
}

function mapCompareRunApiRow(row) {
  if (!row || typeof row !== 'object') return null
  const summary = row.summary && typeof row.summary === 'object' ? row.summary : {}
  const cid = Number(row.compare_run_id)
  if (!Number.isFinite(cid)) return null
  return {
    id: String(cid),
    compareRunId: cid,
    at: row.compared_at_utc || '',
    stateCode: String(row.state_code || '').toUpperCase(),
    artifactId: Number(row.artifact_id) || null,
    artifactLabel: String(row.artifact_label || row.logical_schedule_key || '').trim() || '—',
    dstFsname: String(row.dst_fsname || '').trim(),
    triggerSource: String(row.trigger_source || 'manual').toLowerCase(),
    status: String(row.status || '').toLowerCase(),
    hasWorkbook: row.has_workbook === true,
    hasSnapshot: row.has_snapshot === true,
    errorMessage: String(row.error_message || '').trim(),
    summary,
    matchPct: feeCompareJoinedMatchPct(summary),
  }
}

function cacheCompareReplayPayload(replay, cacheRef) {
  if (!replay || typeof replay !== 'object' || !cacheRef?.current) return
  const id = Number(replay.compare_run_id)
  if (!Number.isFinite(id) || id <= 0) return
  cacheRef.current.set(id, replay)
  while (cacheRef.current.size > 40) {
    const first = cacheRef.current.keys().next().value
    cacheRef.current.delete(first)
  }
}

function recentCompareEntryToResult(entry) {
  if (!entry || typeof entry !== 'object') return null
  return {
    ok: true,
    state_code: entry.stateCode,
    artifact_id: entry.artifactId,
    dst_fsname: entry.dstFsname,
    summary: entry.summary && typeof entry.summary === 'object' ? entry.summary : {},
    column_pairs: Array.isArray(entry.snapshot?.column_pairs) ? entry.snapshot.column_pairs : [],
    rows: Array.isArray(entry.snapshot?.rows) ? entry.snapshot.rows : [],
  }
}

function feeComparePairCellClass(rowStatus, side, fd) {
  if (!fd) return 'app-compare-cell'
  if (rowStatus === 'state_only') {
    if (side === 'dst') return 'app-compare-cell app-compare-cell--missing-dst'
    return 'app-compare-cell app-compare-cell--extra-state'
  }
  if (rowStatus === 'dst_only') {
    if (side === 'state') return 'app-compare-cell app-compare-cell--missing-state'
    return 'app-compare-cell app-compare-cell--extra-dst'
  }
  if (!fd.same) return 'app-compare-cell app-compare-cell--diff'
  return 'app-compare-cell'
}

function FeeCompareSplitTables({ pairs, rows }) {
  if (pairs.length === 0) {
    return (
      <p className="app-error" role="status">
        No column pairs were returned — save a mapping on the Mapping tab and compare again.
      </p>
    )
  }

  const bodyRows = rows.length === 0 ? [] : rows

  return (
    <div className="app-compare-split-outer app-compare-split-outer--panel">
      <div className="app-compare-split">
        <div className="app-compare-pane app-compare-pane--state">
          <div className="app-compare-pane-head">State fee file</div>
          <div className="app-compare-pane-scroll-x">
            <table className="app-data-table app-compare-table">
              <thead>
                <tr>
                  {pairs.map((p, i) => (
                    <th key={`st-h-${i}`} className="app-compare-th app-compare-th--statecol">
                      {p.state_column}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bodyRows.map((row, ri) => {
                  const fds = Array.isArray(row.field_diffs) ? row.field_diffs : []
                  return (
                    <tr key={`csr-${ri}`} className={`app-compare-tr app-compare-tr--${row.status}`}>
                      {pairs.map((_, pi) => {
                        const fd = fds[pi]
                        const sv = fd != null ? String(fd.state_value ?? '') : ''
                        return (
                          <td key={`cs-${ri}-${pi}`} className={feeComparePairCellClass(row.status, 'state', fd)} title={sv}>
                            {sv === '' ? '—' : sv}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
        <div className="app-compare-pane app-compare-pane--dst">
          <div className="app-compare-pane-head">DST</div>
          <div className="app-compare-pane-scroll-x">
            <table className="app-data-table app-compare-table">
              <thead>
                <tr>
                  {pairs.map((p, i) => (
                    <th key={`dst-h-${i}`} className="app-compare-th app-compare-th--dstcol">
                      {p.dst_column}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bodyRows.map((row, ri) => {
                  const fds = Array.isArray(row.field_diffs) ? row.field_diffs : []
                  return (
                    <tr key={`cdr-${ri}`} className={`app-compare-tr app-compare-tr--${row.status}`}>
                      {pairs.map((_, pi) => {
                        const fd = fds[pi]
                        const dv = fd != null ? String(fd.dst_value ?? '') : ''
                        return (
                          <td key={`cd-${ri}-${pi}`} className={feeComparePairCellClass(row.status, 'dst', fd)} title={dv}>
                            {dv === '' ? '—' : dv}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}

function FeeScheduleComparePanel({ result }) {
  const pairs = Array.isArray(result?.column_pairs) ? result.column_pairs : []
  const allRows = Array.isArray(result?.rows) ? result.rows : []
  const summary = result?.summary && typeof result.summary === 'object' ? result.summary : {}

  /** Compare modal — exactly three buckets (filters). */
  /** @typedef {'mismatch' | 'state_only' | 'dst_only'} FeeCompareThreeFilter */

  /** @type [FeeCompareThreeFilter, (x: FeeCompareThreeFilter) => void] */
  const [rowFilter, setRowFilter] = useState(
    /** @type {FeeCompareThreeFilter} */ ('mismatch'),
  )

  /** Start on the first filter that actually has rows (saved sessions can omit some buckets after truncation). */
  useEffect(() => {
    const r = Array.isArray(result?.rows) ? result.rows : []
    const nm = r.filter((x) => x?.status === 'mismatch').length
    const ns = r.filter((x) => x?.status === 'state_only').length
    const nd = r.filter((x) => x?.status === 'dst_only').length
    if (nm > 0) setRowFilter('mismatch')
    else if (ns > 0) setRowFilter('state_only')
    else if (nd > 0) setRowFilter('dst_only')
    else setRowFilter('mismatch')
  }, [result])

  /** Full-run identical pairs — still reported by API summary even when reopen payload omits matched rows. */
  const identicalTruth = Number(summary.match_count) || 0

  const matchPct = feeCompareJoinedMatchPct(summary)

  const filteredRows = useMemo(
    () => allRows.filter((r) => r?.status === rowFilter),
    [allRows, rowFilter],
  )

  const changedRowsAll = useMemo(() => allRows.filter((r) => r?.status !== 'match'), [allRows])

  const filteredDownloadRows = filteredRows

  const nMismatch = useMemo(() => allRows.filter((r) => r?.status === 'mismatch').length, [allRows])
  const nStateOnly = useMemo(() => allRows.filter((r) => r?.status === 'state_only').length, [allRows])
  const nDstOnly = useMemo(() => allRows.filter((r) => r?.status === 'dst_only').length, [allRows])

  const baseExportName =
    `compare_${result?.state_code || 'SC'}_${result?.dst_fsname || 'dst'}`.replace(/[^\w.-]+/g, '_')

  return (
    <div className="app-fee-compare-flow">
      {Array.isArray(result?.mapping_warnings) && result.mapping_warnings.length > 0 ? (
        <div className="app-fee-compare-mapping-warn" role="status">
          <strong>Mapping review suggested</strong>
          <ul>
            {result.mapping_warnings.map((w, i) => (
              <li key={`mw-${i}`}>{String(w)}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="app-fee-compare-summary">
        <div className="app-fee-compare-summary-stats">
          {matchPct != null ? (
            <div className="app-fee-compare-match">
              <span className="app-fee-compare-match-value">{`${matchPct.toFixed(1)}%`}</span>
              <span className="app-fee-compare-match-label">Match rate</span>
              <div className="app-fee-compare-match-bar-wrap" aria-hidden="true">
                <div className="app-fee-compare-match-bar-fill" style={{ width: `${Math.min(100, matchPct)}%` }} />
              </div>
            </div>
          ) : null}
          <p className="app-fee-compare-totals-line" aria-label="Compare totals">
            <strong>{String(identicalTruth)}</strong> identical
            {' · '}
            <strong>{String(nMismatch)}</strong> modified
            {' · '}
            <strong>{String(nStateOnly)}</strong> added in State
            {' · '}
            <strong>{String(nDstOnly)}</strong> DST not in State
          </p>
        </div>
        <div className="app-fee-compare-toolbar">
          <div className="app-fee-compare-filters">
            {([
              { id: 'mismatch', label: 'Modified', n: nMismatch },
              { id: 'state_only', label: 'Added in State', n: nStateOnly },
              { id: 'dst_only', label: 'DST not in State', n: nDstOnly },
            ]).map((b) => (
              <button
                key={b.id}
                type="button"
                className={`app-chip${rowFilter === b.id ? ' app-chip--on' : ''}`}
                onClick={() => setRowFilter(/** @type {FeeCompareThreeFilter} */ (b.id))}
              >
                {b.label}{' '}
                <span className="app-chip-meta">({String(b.n)})</span>
              </button>
            ))}
          </div>
          <div className="app-fee-compare-dl-group">
            <button
              type="button"
              className="app-btn app-btn--secondary app-btn--sm"
              onClick={() => downloadFeeCompareWorkbook(baseExportName, result, filteredDownloadRows)}
            >
              Download (.xlsx)
            </button>
            <button
              type="button"
              className="app-btn app-btn--secondary app-btn--sm"
              onClick={() => downloadFeeCompareWorkbook(baseExportName, result, changedRowsAll)}
              disabled={!changedRowsAll.length}
            >
              Changed workbook (.xlsx)
            </button>
          </div>
        </div>
      </div>
      {!result?.summary ? <p className="app-muted">No result.</p> : <FeeCompareSplitTables pairs={pairs} rows={filteredRows} />}
    </div>
  )
}

export default function App() {
  const [activeNav, setActiveNav] = useState('schedules')
  const [stateSourceUrl, setStateSourceUrl] = useState('')
  const [agentLoading, setAgentLoading] = useState(false)
  const [agentError, setAgentError] = useState(null)
  const [agentResult, setAgentResult] = useState(null)
  const [companionHealth, setCompanionHealth] = useState(null)
  const [stateUploadFile, setStateUploadFile] = useState(null)
  const [dstUploadFile, setDstUploadFile] = useState(null)
  const [compareMessage, setCompareMessage] = useState('')
  const [preview, setPreview] = useState(null)

  const [dstTables, setDstTables] = useState([])
  const [dstTablesLoading, setDstTablesLoading] = useState(false)
  const [dstTablesError, setDstTablesError] = useState(null)
  /** Distinct ``fs_name`` values for sidebar state (from configured raw DST table). */
  const [dstFeeSchedules, setDstFeeSchedules] = useState([])
  const [dstFeeScheduleTable, setDstFeeScheduleTable] = useState('')
  const [dstFeeSchedulesLoading, setDstFeeSchedulesLoading] = useState(false)
  const [dstFeeSchedulesError, setDstFeeSchedulesError] = useState(null)
  const [dstSelectedTable, setDstSelectedTable] = useState('')
  const [dstColumns, setDstColumns] = useState([])
  const [dstRows, setDstRows] = useState([])
  const [dstRowsLoading, setDstRowsLoading] = useState(false)
  const [dstRowsError, setDstRowsError] = useState(null)

  const [selectedStateCode, setSelectedStateCode] = useState(() => {
    try {
      return sessionStorage.getItem('feeToolStateCode') || ''
    } catch {
      return ''
    }
  })
  const [linkedPortalLinks, setLinkedPortalLinks] = useState([])
  const portalLinkRow = useMemo(
    () => linkedPortalLinks.find((r) => r.state_code === selectedStateCode) ?? null,
    [linkedPortalLinks, selectedStateCode],
  )
  const [portalEditorStateCode, setPortalEditorStateCode] = useState('')
  const [stateArtifacts, setStateArtifacts] = useState([])
  const [stateArtifactsLoading, setStateArtifactsLoading] = useState(false)
  const [stateArtifactsError, setStateArtifactsError] = useState(null)
  const [stateArtifactHistory, setStateArtifactHistory] = useState([])
  const [stateArtifactHistoryLoading, setStateArtifactHistoryLoading] = useState(false)
  const [stateArtifactHistoryError, setStateArtifactHistoryError] = useState(null)
  const [stateUrlsMessage, setStateUrlsMessage] = useState(null)
  const [stateUrlsSaving, setStateUrlsSaving] = useState(false)
  const [urlFormLabel, setUrlFormLabel] = useState('')
  const [urlFormPortalUrl, setUrlFormPortalUrl] = useState('')
  const [portalEditorLinkId, setPortalEditorLinkId] = useState(null)
  const stateArtifactTablePreviewCacheRef = useRef(new Map())
  /** In-memory replay payloads keyed by compare_run_id (instant View without refetch). */
  const compareReplayCacheRef = useRef(new Map())
  const dstSchedulesPreviewCacheRef = useRef(new Map())
  /** Snapshot at Preview click — filter modal rows to the same slice as Export when a start date is chosen. */
  const schedulesDstPreviewFilterStartRef = useRef('')
  const schedulesDstPreviewFilterDateColRef = useRef(null)
  const mappingDstColumnListCacheRef = useRef(new Map())
  const [schedulesDstModalTable, setSchedulesDstModalTable] = useState('')
  const [schedulesDstCols, setSchedulesDstCols] = useState([])
  const [schedulesDstRows, setSchedulesDstRows] = useState([])
  const [schedulesDstLoading, setSchedulesDstLoading] = useState(false)
  const [schedulesDstError, setSchedulesDstError] = useState(null)
  const [schedulesStatePreviewId, setSchedulesStatePreviewId] = useState(null)
  const [schedulesStateLoading, setSchedulesStateLoading] = useState(false)
  const [schedulesStateError, setSchedulesStateError] = useState(null)
  const [schedulesStateCols, setSchedulesStateCols] = useState([])
  const [schedulesStateRows, setSchedulesStateRows] = useState([])
  const [schedulesStatePdfUrl, setSchedulesStatePdfUrl] = useState('')
  const schedulesStatePdfRef = useRef('')
  const [schedulesStateFeeModalOpen, setSchedulesStateFeeModalOpen] = useState(false)
  const [schedulesDstFeeModalOpen, setSchedulesDstFeeModalOpen] = useState(false)
  const [schedulesCompareOpen, setSchedulesCompareOpen] = useState(false)
  const [schedulesCompareLoading, setSchedulesCompareLoading] = useState(false)
  /** Loading saved snapshot from DB (eye icon) — not a live compare run. */
  const [schedulesCompareReplayLoading, setSchedulesCompareReplayLoading] = useState(false)
  const [schedulesCompareError, setSchedulesCompareError] = useState(null)
  const [schedulesCompareResult, setSchedulesCompareResult] = useState(null)
  /** Invalidates schedules dashboard metrics (localStorage) after a successful compare. */
  const [schedulesMetricsTick, setSchedulesMetricsTick] = useState(0)
  /** Bumps after compare persisted (manual or sync) so Recent comparisons refreshes from API. */
  const [schedulesRecentComparesTick, setSchedulesRecentComparesTick] = useState(0)
  const [compareRunsRows, setCompareRunsRows] = useState([])
  const [compareRunsLoading, setCompareRunsLoading] = useState(false)
  const [compareRunsError, setCompareRunsError] = useState(null)
  /** State fee dropdown key (`a:id`, catalog row key, …) — independent from DST selection. */
  const [schedulesStateFeeKey, setSchedulesStateFeeKey] = useState('')
  /** DST fee dropdown key (`d:table`) — independent from State selection. */
  const [schedulesDstFeeKey, setSchedulesDstFeeKey] = useState('')
  /** ISO day (yyyy-mm-dd) chosen from distinct values in DST fee slice; drives export range through 12/31 of that year. */
  const [schedulesDstStartDateIso, setSchedulesDstStartDateIso] = useState('')
  /** Bumps when DST prefetch / modal fills `dstSchedulesPreviewCacheRef` so card date options re-read the cache. */
  const [schedulesDstPrefetchTick, setSchedulesDstPrefetchTick] = useState(0)
  /** Column mapping screen — uses sidebar `selectedStateCode` only (no second state picker). */
  const [mappingStateFeeKey, setMappingStateFeeKey] = useState('')
  const [mappingDstTable, setMappingDstTable] = useState('')
  const [mappingStateColumns, setMappingStateColumns] = useState([])
  const [mappingDstColumns, setMappingDstColumns] = useState([])
  const [mappingStateColumnsLoading, setMappingStateColumnsLoading] = useState(false)
  const [mappingStateColumnsError, setMappingStateColumnsError] = useState(null)
  const [mappingDstColumnsLoading, setMappingDstColumnsLoading] = useState(false)
  const [mappingDstColumnsError, setMappingDstColumnsError] = useState(null)
  const [mappingColumnPairs, setMappingColumnPairs] = useState({})
  const [mappingCommittedSnapshot, setMappingCommittedSnapshot] = useState(null)
  const [mappingPersistLoading, setMappingPersistLoading] = useState(false)
  const [mappingPersistError, setMappingPersistError] = useState(null)
  const [mappingMappingLoadError, setMappingMappingLoadError] = useState(null)
  /** Inventoried mappings for active state ({@link GET /app/fee-column-mappings}). */
  const [mappingSavedMappings, setMappingSavedMappings] = useState([])
  const [mappingSavedMappingsLoading, setMappingSavedMappingsLoading] = useState(false)
  const [mappingSavedMappingsError, setMappingSavedMappingsError] = useState(null)
  const [mappingSavedMappingsTick, setMappingSavedMappingsTick] = useState(0)
  const [mappingDeleteBusyId, setMappingDeleteBusyId] = useState(null)
  /** Bulk import workbook (CSV / Xlsx) → fee-column-mappings upserts. */
  const [mappingBulkBusy, setMappingBulkBusy] = useState(false)
  const [mappingBulkDryRun, setMappingBulkDryRun] = useState(false)
  const [mappingBulkClientError, setMappingBulkClientError] = useState(null)
  const [mappingBulkApiResult, setMappingBulkApiResult] = useState(null)
  const mappingBulkFileInputRef = useRef(null)
  const [mappingBulkScheduleNames, setMappingBulkScheduleNames] = useState([])
  const [mappingBulkScheduleNamesLoading, setMappingBulkScheduleNamesLoading] = useState(false)
  /** Tracks mapping row backing the composer (for invalidation after delete). */
  const [mappingActiveMappingId, setMappingActiveMappingId] = useState(null)
  /** { loading?, error?, detail? } for read-only view */
  const [mappingShowModal, setMappingShowModal] = useState(null)
  /** State/DST selectors + pairing editor (opened from Saved mappings). */
  const [mappingComposerModalOpen, setMappingComposerModalOpen] = useState(false)
  /** Per-state notification teams (companion DB); outbound mail not wired yet. */
  const [notifContacts, setNotifContacts] = useState([])
  const [notifContactsLoading, setNotifContactsLoading] = useState(false)
  const [notifContactsError, setNotifContactsError] = useState(null)
  const [notifContactsTick, setNotifContactsTick] = useState(0)
  const [notifSaving, setNotifSaving] = useState(false)
  const [notifFormError, setNotifFormError] = useState(null)
  const [notifEditingId, setNotifEditingId] = useState(null)
  const [notifFormName, setNotifFormName] = useState('')
  const [notifFormEmail, setNotifFormEmail] = useState('')
  const [notifFormTeam, setNotifFormTeam] = useState('')
  const [notifFormDept, setNotifFormDept] = useState('')
  const [notifFormEnabled, setNotifFormEnabled] = useState(true)
  const [notifFormNewFile, setNotifFormNewFile] = useState(true)
  const [notifFormCompare, setNotifFormCompare] = useState(true)
  const [notifDeleteBusyId, setNotifDeleteBusyId] = useState(null)
  const [notifTeamModalOpen, setNotifTeamModalOpen] = useState(false)
  /** Artifact dropdown: skip default /latest hydrate once (opening editor from saved list). */
  const mappingArtifactAutoLoadRef = useRef(true)
  const mappingSkipDstPairsFetchOnceRef = useRef(false)
  const selectedStateCodeRef = useRef(selectedStateCode)

  useEffect(() => {
    selectedStateCodeRef.current = selectedStateCode
  }, [selectedStateCode])

  useEffect(() => {
    setMappingShowModal(null)
    setMappingActiveMappingId(null)
  }, [selectedStateCode])

  const clearSchedulesArtifactPreview = useCallback(() => {
    setSchedulesStateFeeModalOpen(false)
    setSchedulesStatePreviewId(null)
    setSchedulesStatePdfUrl('')
    setSchedulesStateCols([])
    setSchedulesStateRows([])
    setSchedulesStateError(null)
    setSchedulesStateLoading(false)
    if (schedulesStatePdfRef.current) {
      URL.revokeObjectURL(schedulesStatePdfRef.current)
      schedulesStatePdfRef.current = ''
    }
  }, [])

  const closeSchedulesDstFeeModal = useCallback(() => {
    setSchedulesDstFeeModalOpen(false)
    setSchedulesDstModalTable('')
    setSchedulesDstCols([])
    setSchedulesDstRows([])
    setSchedulesDstError(null)
    setSchedulesDstLoading(false)
    schedulesDstPreviewFilterStartRef.current = ''
    schedulesDstPreviewFilterDateColRef.current = null
  }, [])

  /** ``state|portalUrl`` — when this changes, sync the State Data URL field from the saved portal. */
  const urlAutofillKeyRef = useRef('')

  useEffect(() => {
    if (!selectedStateCode) {
      setStateSourceUrl('')
      urlAutofillKeyRef.current = ''
      return
    }
    const incoming = (portalLinkRow?.portal_url || '').trim()
    const key = `${selectedStateCode}|${incoming}`
    if (urlAutofillKeyRef.current !== key) {
      urlAutofillKeyRef.current = key
      setStateSourceUrl(incoming)
    }
  }, [selectedStateCode, portalLinkRow?.portal_url])

  const refreshPortalLinks = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/app/state-portal-links?limit=500`)
      if (!res.ok) {
        // Do not clear active state or cached links — a 503 (e.g. DB hiccup) was wiping the whole Fee Schedules UI.
        return
      }
      const data = await res.json()
      const rows = Array.isArray(data?.links) ? data.links : []
      setLinkedPortalLinks(rows)
      const codes = rows.map((r) => r.state_code)
      setSelectedStateCode((prev) => {
        if (prev && codes.includes(prev)) return prev
        return rows[0]?.state_code || ''
      })
    } catch {
      /* keep existing links + selectedStateCode */
    }
  }, [])

  useEffect(() => {
    void refreshPortalLinks()
  }, [refreshPortalLinks])

  const refreshCompanionHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/app/health`)
      if (!res.ok) {
        setCompanionHealth(null)
        return
      }
      const data = await res.json()
      setCompanionHealth(data)
    } catch {
      setCompanionHealth(null)
    }
  }, [])

  useEffect(() => {
    if (activeNav !== 'schedules') return undefined
    void refreshCompanionHealth()
    return undefined
  }, [activeNav, refreshCompanionHealth])

  const refreshStateArtifacts = useCallback(async () => {
    if (!selectedStateCode) {
      setStateArtifacts([])
      setStateArtifactsError(null)
      return []
    }
    setStateArtifactsLoading(true)
    setStateArtifactsError(null)
    try {
      const res = await fetch(
        `${API_BASE}/app/artifacts?state_code=${encodeURIComponent(selectedStateCode)}&current_only=true&limit=2000`,
      )
      if (!res.ok) {
        throw new Error(await readHttpErrorMessage(res, `Artifacts failed (${res.status})`))
      }
      const data = await res.json()
      const rows = Array.isArray(data?.artifacts) ? data.artifacts : []
      setStateArtifacts(rows)
      return rows
    } catch (e) {
      setStateArtifacts([])
      setStateArtifactsError(e?.message || 'Could not load downloaded files list.')
      return []
    } finally {
      setStateArtifactsLoading(false)
    }
  }, [selectedStateCode])

  const refreshArtifactHistory = useCallback(async () => {
    if (!selectedStateCode) {
      setStateArtifactHistory([])
      setStateArtifactHistoryError(null)
      return []
    }
    setStateArtifactHistoryLoading(true)
    setStateArtifactHistoryError(null)
    try {
      const res = await fetch(
        `${API_BASE}/app/artifacts?state_code=${encodeURIComponent(selectedStateCode)}&current_only=false&limit=2000`,
      )
      if (!res.ok) {
        throw new Error(await readHttpErrorMessage(res, `Artifacts failed (${res.status})`))
      }
      const data = await res.json()
      const rows = Array.isArray(data?.artifacts) ? data.artifacts : []
      setStateArtifactHistory(rows)
      return rows
    } catch (e) {
      setStateArtifactHistory([])
      setStateArtifactHistoryError(e?.message || 'Could not load version history.')
      return []
    } finally {
      setStateArtifactHistoryLoading(false)
    }
  }, [selectedStateCode])

  const scheduleVersionsTableRows = useMemo(() => {
    const rows = [...(Array.isArray(stateArtifactHistory) ? stateArtifactHistory : [])]
    rows.sort((a, b) => {
      const lkA = String(a?.logical_schedule_key || '').trim().toLowerCase()
      const lkB = String(b?.logical_schedule_key || '').trim().toLowerCase()
      const c = lkA.localeCompare(lkB, undefined, { sensitivity: 'base', numeric: true })
      if (c !== 0) return c
      return artifactEditionSortTs(b) - artifactEditionSortTs(a)
    })
    return rows
  }, [stateArtifactHistory])

  useEffect(() => {
    if (activeNav !== 'scheduleVersions' || !selectedStateCode) return undefined
    void refreshArtifactHistory()
    return undefined
  }, [activeNav, selectedStateCode, refreshArtifactHistory])

  useEffect(() => {
    setSchedulesStateFeeKey('')
    setSchedulesDstFeeKey('')
    stateArtifactTablePreviewCacheRef.current.clear()
    dstSchedulesPreviewCacheRef.current.clear()
    mappingDstColumnListCacheRef.current.clear()
    closeSchedulesDstFeeModal()
    setSchedulesDstModalTable('')
    clearSchedulesArtifactPreview()
    setAgentResult(null)
    setAgentError(null)
    setAgentLoading(false)
    setStateArtifacts([])
    setStateArtifactsError(null)
    setPreview((prev) => {
      if (prev?.blobUrl) URL.revokeObjectURL(prev.blobUrl)
      return null
    })
    setMappingStateFeeKey('')
    setMappingDstTable('')
    setMappingStateColumns([])
    setMappingDstColumns([])
    setMappingStateColumnsError(null)
    setMappingDstColumnsError(null)
    setMappingColumnPairs({})
    setMappingCommittedSnapshot(null)
    setMappingPersistError(null)
    setMappingMappingLoadError(null)
    setStateArtifactHistory([])
    setStateArtifactHistoryError(null)
  }, [selectedStateCode, clearSchedulesArtifactPreview, closeSchedulesDstFeeModal])

  /** Saved fee files load automatically whenever Fee Schedules is active and an active state is chosen. */
  useEffect(() => {
    if (activeNav !== 'schedules' || !selectedStateCode) return undefined
    void refreshStateArtifacts()
    return undefined
  }, [activeNav, selectedStateCode, refreshStateArtifacts])

  useEffect(() => {
    if (activeNav !== 'mapping' || !selectedStateCode) return undefined
    void refreshStateArtifacts()
    return undefined
  }, [activeNav, selectedStateCode, refreshStateArtifacts])

  useEffect(() => {
    if (activeNav !== 'mapping') return undefined
    const m = mappingStateFeeKey.match(/^a:(\d+)$/)
    if (!m) {
      setMappingStateColumns([])
      setMappingStateColumnsLoading(false)
      setMappingStateColumnsError(null)
      return undefined
    }
    const id = Number(m[1], 10)
    const hit = stateArtifactTablePreviewCacheRef.current.get(id)
    if (hit?.cols?.length) {
      setMappingStateColumns(sortStringListLocale(hit.cols.map((c) => String(c))))
      setMappingStateColumnsError(null)
      setMappingStateColumnsLoading(false)
      return undefined
    }
    const ac = new AbortController()
    setMappingStateColumnsLoading(true)
    setMappingStateColumnsError(null)
    setMappingStateColumns([])
    fetch(`${API_BASE}/app/artifacts/${id}/preview-table`, { signal: ac.signal })
      .then(async (pr) => {
        const raw = await pr.text()
        if (!pr.ok) {
          let detail = `Preview failed (${pr.status})`
          try {
            const j = JSON.parse(raw)
            if (typeof j.detail === 'string') detail = j.detail
            else if (j.detail != null) detail = String(j.detail)
          } catch {
            if (raw.trim()) detail = raw.trim().slice(0, 400)
          }
          throw new Error(detail)
        }
        const payload = JSON.parse(raw)
        const cols = Array.isArray(payload?.columns) ? payload.columns : []
        if (cols.length === 0) {
          throw new Error('No tabular columns in preview — file may be PDF-only or unsupported for column mapping.')
        }
        setMappingStateColumns(sortStringListLocale(cols.map((c) => String(c))))
      })
      .catch((e) => {
        if (e.name !== 'AbortError') {
          setMappingStateColumns([])
          setMappingStateColumnsError(e?.message || 'Could not load state columns.')
        }
      })
      .finally(() => setMappingStateColumnsLoading(false))
    return () => ac.abort()
  }, [activeNav, mappingStateFeeKey])

  useEffect(() => {
    if (activeNav !== 'mapping') {
      setMappingDstColumns([])
      setMappingDstColumnsLoading(false)
      setMappingDstColumnsError(null)
      return undefined
    }
    const fsName = (mappingDstTable || '').trim()
    if (!fsName || !dstFeeScheduleTable) {
      setMappingDstColumns([])
      setMappingDstColumnsLoading(false)
      setMappingDstColumnsError(null)
      return undefined
    }
    const ac = new AbortController()
    const cacheKey = `${MAPPING_DST_COLUMN_CACHE_VERSION}|${selectedStateCode || ''}|${fsName}`
    const hit = mappingDstColumnListCacheRef.current.get(cacheKey)
    if (hit?.length) {
      setMappingDstColumns(sortStringListLocale(hit.map((c) => String(c))))
      setMappingDstColumnsError(null)
      setMappingDstColumnsLoading(false)
      return () => ac.abort()
    }
    setMappingDstColumnsLoading(true)
    setMappingDstColumnsError(null)
    setMappingDstColumns([])
    const q = new URLSearchParams({
      table: dstFeeScheduleTable,
      limit: String(MAPPING_DST_COLUMN_SAMPLE_LIMIT),
      fs_name: fsName,
    })
    if (selectedStateCode) q.set('state_code', selectedStateCode)
    q.set('response_row_limit', '0')
    fetch(`${API_BASE}/dst/rows?${q}`, { signal: ac.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `DST rows failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        const cols = Array.isArray(data?.columns) ? data.columns : []
        const asStrings = sortStringListLocale(cols.map((c) => String(c)))
        mappingDstColumnListCacheRef.current.set(cacheKey, asStrings)
        trimSchedulesPreviewMap(mappingDstColumnListCacheRef.current, MAPPING_DST_COLUMN_CACHE_LIMIT)
        setMappingDstColumns(asStrings)
      })
      .catch((e) => {
        if (e.name !== 'AbortError') {
          setMappingDstColumns([])
          setMappingDstColumnsError(e?.message || 'Could not load DST columns.')
        }
      })
      .finally(() => setMappingDstColumnsLoading(false))
    return () => ac.abort()
  }, [activeNav, mappingDstTable, selectedStateCode, dstFeeScheduleTable])

  useEffect(() => {
    if (activeNav !== 'mapping') {
      setMappingSavedMappings([])
      setMappingSavedMappingsLoading(false)
      setMappingSavedMappingsError(null)
      setMappingDeleteBusyId(null)
      return undefined
    }
    if (!selectedStateCode) {
      setMappingSavedMappings([])
      setMappingSavedMappingsLoading(false)
      setMappingSavedMappingsError(null)
      return undefined
    }
    const ac = new AbortController()
    setMappingSavedMappingsLoading(true)
    setMappingSavedMappingsError(null)
    fetch(
      `${API_BASE}/app/fee-column-mappings?state_code=${encodeURIComponent(selectedStateCode)}`,
      {
        signal: ac.signal,
      },
    )
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Mappings list failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        const rows = Array.isArray(data?.mappings) ? data.mappings : []
        setMappingSavedMappings(rows)
      })
      .catch((e) => {
        if (e.name !== 'AbortError')
          setMappingSavedMappingsError(e?.message || 'Could not load saved mappings.')
      })
      .finally(() => setMappingSavedMappingsLoading(false))
    return () => ac.abort()
  }, [activeNav, selectedStateCode, mappingSavedMappingsTick])

  useEffect(() => {
    if (activeNav !== 'mapping' || !selectedStateCode) {
      setMappingBulkScheduleNames([])
      setMappingBulkScheduleNamesLoading(false)
      return undefined
    }
    const ac = new AbortController()
    setMappingBulkScheduleNamesLoading(true)
    fetch(
      `${API_BASE}/app/fee-column-mappings/schedule-names?state_code=${encodeURIComponent(selectedStateCode)}`,
      { signal: ac.signal },
    )
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Schedule names failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        setMappingBulkScheduleNames(Array.isArray(data?.schedules) ? data.schedules : [])
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setMappingBulkScheduleNames([])
      })
      .finally(() => setMappingBulkScheduleNamesLoading(false))
    return () => ac.abort()
  }, [activeNav, selectedStateCode])

  useEffect(() => {
    if (activeNav !== 'mapping') {
      setMappingBulkApiResult(null)
      setMappingBulkClientError(null)
      setMappingBulkBusy(false)
    }
  }, [activeNav])

  useEffect(() => {
    setMappingBulkApiResult(null)
    setMappingBulkClientError(null)
  }, [selectedStateCode])

  useEffect(() => {
    setNotifEditingId(null)
    setNotifFormName('')
    setNotifFormEmail('')
    setNotifFormTeam('')
    setNotifFormDept('')
    setNotifFormEnabled(true)
    setNotifFormNewFile(true)
    setNotifFormCompare(true)
    setNotifFormError(null)
    setNotifDeleteBusyId(null)
    setNotifTeamModalOpen(false)
  }, [selectedStateCode])

  useEffect(() => {
    if (activeNav !== 'notifications') {
      setNotifContacts([])
      setNotifContactsLoading(false)
      setNotifContactsError(null)
      setNotifDeleteBusyId(null)
      setNotifTeamModalOpen(false)
      return undefined
    }
    if (!selectedStateCode) {
      setNotifContacts([])
      setNotifContactsLoading(false)
      setNotifContactsError(null)
      return undefined
    }
    const ac = new AbortController()
    setNotifContactsLoading(true)
    setNotifContactsError(null)
    fetch(`${API_BASE}/app/notification-contacts?state_code=${encodeURIComponent(selectedStateCode)}`, {
      signal: ac.signal,
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Load failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        const rows = Array.isArray(data?.contacts) ? data.contacts : []
        setNotifContacts(rows)
      })
      .catch((e) => {
        if (e.name !== 'AbortError') {
          setNotifContacts([])
          setNotifContactsError(e?.message || 'Could not load notification teams.')
        }
      })
      .finally(() => setNotifContactsLoading(false))
    return () => ac.abort()
  }, [activeNav, selectedStateCode, notifContactsTick])

  const mappingArtifactIdFromKey = useMemo(() => {
    const m = (mappingStateFeeKey || '').match(/^a:(\d+)$/)
    if (!m) return null
    const n = Number(m[1], 10)
    return Number.isFinite(n) ? n : null
  }, [mappingStateFeeKey])

  /** Load persisted mapping whenever the Mapping tab artifact changes. */
  useEffect(() => {
    if (activeNav !== 'mapping') return undefined
    setMappingMappingLoadError(null)
    if (!selectedStateCode || !mappingArtifactIdFromKey) {
      setMappingCommittedSnapshot(null)
      setMappingColumnPairs({})
      setMappingDstTable('')
      return undefined
    }
    if (!mappingArtifactAutoLoadRef.current) {
      mappingArtifactAutoLoadRef.current = true
      return undefined
    }
    setMappingCommittedSnapshot(null)
    setMappingColumnPairs({})
    setMappingDstTable('')
    const ac = new AbortController()
    const q = new URLSearchParams({
      state_code: selectedStateCode,
      artifact_id: String(mappingArtifactIdFromKey),
    })
    fetch(`${API_BASE}/app/fee-column-mappings/latest?${q}`, { signal: ac.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Mapping load failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        setMappingMappingLoadError(null)
        if (data?.found && data.mapping) {
          const dst = String(data.mapping.dst_fsname || '').trim()
          const pairs = normalizeColumnPairs(data.column_map)
          setMappingDstTable(dst)
          setMappingColumnPairs(pairs)
          setMappingCommittedSnapshot({ dstTable: dst, pairs: { ...pairs } })
        }
      })
      .catch((e) => {
        if (e.name !== 'AbortError') {
          setMappingMappingLoadError(e?.message || 'Could not load saved mapping.')
        }
      })
    return () => ac.abort()
  }, [activeNav, selectedStateCode, mappingArtifactIdFromKey])

  /** When editing (or no saved row yet), pull column_map for the selected DST table. */
  useEffect(() => {
    if (activeNav !== 'mapping') return undefined
    if (!selectedStateCode || !mappingArtifactIdFromKey) return undefined
    if (mappingSkipDstPairsFetchOnceRef.current) {
      mappingSkipDstPairsFetchOnceRef.current = false
      return undefined
    }
    const dt = (mappingDstTable || '').trim()
    if (!dt) {
      setMappingColumnPairs({})
      return undefined
    }
    const ac = new AbortController()
    const q = new URLSearchParams({
      state_code: selectedStateCode,
      artifact_id: String(mappingArtifactIdFromKey),
      dst_fsname: dt,
    })
    fetch(`${API_BASE}/app/fee-column-mappings/latest?${q}`, { signal: ac.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Mapping load failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        setMappingMappingLoadError(null)
        if (data?.found && data.column_map)
          setMappingColumnPairs(normalizeColumnPairs(data.column_map))
        else setMappingColumnPairs({})
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setMappingMappingLoadError(e?.message || 'Could not load mapping.')
      })
    return () => ac.abort()
  }, [activeNav, selectedStateCode, mappingArtifactIdFromKey, mappingDstTable])

  const mergedArtifactsForPreview = useMemo(() => {
    const m = new Map()
    for (const a of Array.isArray(stateArtifacts) ? stateArtifacts : []) {
      const id = Number(a?.artifact_id)
      if (Number.isFinite(id)) m.set(id, a)
    }
    for (const a of Array.isArray(stateArtifactHistory) ? stateArtifactHistory : []) {
      const id = Number(a?.artifact_id)
      if (Number.isFinite(id) && !m.has(id)) m.set(id, a)
    }
    return Array.from(m.values())
  }, [stateArtifacts, stateArtifactHistory])

  const stateFeePickRows = useMemo(() => buildArtifactFeePickRows(stateArtifacts), [stateArtifacts])

  const schedulesStateRowsForPreview = useMemo(() => schedulesStateRows, [schedulesStateRows])

  const schedulesDstRowsForPreview = useMemo(
    () => sortFeeSchedulePreviewRows(schedulesDstCols, schedulesDstRows),
    [schedulesDstCols, schedulesDstRows],
  )

  const dstExplorerRowsForPreview = useMemo(
    () => sortFeeSchedulePreviewRows(dstColumns, dstRows),
    [dstColumns, dstRows],
  )

  const mappingSavedMappingsSorted = useMemo(
    () =>
      [...mappingSavedMappings].sort((a, b) => {
        const sa = String(a.schedule_label || '').toLowerCase()
        const sb = String(b.schedule_label || '').toLowerCase()
        const c = sa.localeCompare(sb, undefined, { sensitivity: 'base', numeric: true })
        if (c !== 0) return c
        return String(a.dst_fsname || '').localeCompare(String(b.dst_fsname || ''), undefined, {
          sensitivity: 'base',
          numeric: true,
        })
      }),
    [mappingSavedMappings],
  )

  const stateFeeSelectValue = useMemo(
    () => (stateFeePickRows.some((r) => r.key === schedulesStateFeeKey) ? schedulesStateFeeKey : ''),
    [stateFeePickRows, schedulesStateFeeKey],
  )

  const selectedStateFeePickRow = useMemo(
    () => stateFeePickRows.find((r) => r.key === schedulesStateFeeKey) ?? null,
    [stateFeePickRows, schedulesStateFeeKey],
  )

  const statePickPortalActions = useMemo(() => {
    const row = selectedStateFeePickRow
    if (!row) return { preview: false, download: false }
    if (row.artifactId != null || row.externalUrl) return { preview: true, download: true }
    if (row.catalogTableIndex == null || row.catalogRowIndex == null || !agentResult) return { preview: false, download: false }
    const tables = Array.isArray(agentResult.catalog_tables) ? agentResult.catalog_tables : []
    const t = tables[row.catalogTableIndex]
    const dataRow = Array.isArray(t?.rows) ? t.rows[row.catalogRowIndex] : null
    if (!dataRow) return { preview: false, download: false }
    const prim = primaryCatalogRowLink(dataRow, getTableColumns(t))
    if (!prim) return { preview: false, download: false }
    return {
      preview: true,
      download: !prim.portal && !!prim.url,
    }
  }, [selectedStateFeePickRow, agentResult])

  const dstTablePickOptions = useMemo(
    () => dstFeeSchedules.map((fs) => ({ key: `d:${fs}`, label: fs })),
    [dstFeeSchedules],
  )

  const dstFeeSelectValue = useMemo(
    () => (dstTablePickOptions.some((o) => o.key === schedulesDstFeeKey) ? schedulesDstFeeKey : ''),
    [dstTablePickOptions, schedulesDstFeeKey],
  )

  /** Ref-backed cache fills async — `schedulesDstPrefetchTick` invalidates without mutating deps above. */
  const schedulesDstCardCache = useMemo(() => {
    void schedulesDstPrefetchTick
    const m = (schedulesDstFeeKey || '').match(/^d:(.+)$/)
    if (!m || !selectedStateCode) return null
    const table = m[1]
    const cacheKey = dstSchedulesPreviewCacheKey(selectedStateCode, table)
    return dstSchedulesPreviewCacheRef.current.get(cacheKey) || null
  }, [schedulesDstFeeKey, selectedStateCode, schedulesDstPrefetchTick])

  const schedulesDstDatePlan = useMemo(() => {
    const hit = schedulesDstCardCache
    if (!hit?.columns?.length) {
      return { dateCol: null, isoDays: [], columns: [], rows: [] }
    }
    const cols = Array.isArray(hit.columns) ? hit.columns : []
    const rows = Array.isArray(hit.rows) ? hit.rows : []
    const dateCol = pickDstEffectiveDateColumn(cols)
    const isoDays = uniqueSortedIsoDaysFromRows(rows, dateCol)
    return { dateCol, isoDays, columns: cols, rows }
  }, [schedulesDstCardCache])

  /** Full slice when no date chosen; filtered year-to-end when effective date is selected. */
  const schedulesDstRowsForExcel = useMemo(() => {
    const iso = schedulesDstStartDateIso.trim()
    const { dateCol, rows } = schedulesDstDatePlan
    const all = Array.isArray(rows) ? rows : []
    if (!iso || !dateCol) return all
    return filterDstRowsByEffectiveRange(all, dateCol, iso)
  }, [schedulesDstDatePlan, schedulesDstStartDateIso])

  const schedulesDstExcelDownloadDisabled =
    !dstFeeSelectValue || dstFeeSchedulesLoading || schedulesDstRowsForExcel.length === 0

  useEffect(() => {
    setSchedulesDstStartDateIso('')
  }, [schedulesDstFeeKey])

  useEffect(() => {
    const { isoDays } = schedulesDstDatePlan
    const cur = schedulesDstStartDateIso
    if (!cur) return
    if (isoDays.length && !isoDays.includes(cur)) setSchedulesDstStartDateIso('')
  }, [schedulesDstDatePlan, schedulesDstStartDateIso])

  const mappingStateFeePickRows = useMemo(() => buildArtifactFeePickRows(stateArtifacts), [stateArtifacts])

  const mappingStateFeeSelectValue = useMemo(
    () =>
      mappingStateFeePickRows.some((r) => r.key === mappingStateFeeKey) ? mappingStateFeeKey : '',
    [mappingStateFeePickRows, mappingStateFeeKey],
  )

  const bumpMappingSavedList = useCallback(() => {
    setMappingSavedMappingsTick((t) => t + 1)
  }, [])

  const submitMappingBulkImport = useCallback(
    async (e) => {
      e.preventDefault()
      setMappingBulkClientError(null)
      setMappingBulkApiResult(null)
      if (!selectedStateCode) {
        setMappingBulkClientError('Pick a state in the sidebar.')
        return
      }
      const inp = mappingBulkFileInputRef.current
      const file = inp?.files?.[0]
      if (!file?.name) {
        setMappingBulkClientError('Choose a .csv or .xlsx file.')
        return
      }
      const fd = new FormData()
      fd.append('state_code', selectedStateCode)
      fd.append('dry_run', mappingBulkDryRun ? 'true' : 'false')
      fd.append('file', file, file.name)
      setMappingBulkBusy(true)
      try {
        const res = await fetch(`${API_BASE}/app/fee-column-mappings/bulk-import`, {
          method: 'POST',
          body: fd,
        })
        const data = await res.json().catch(() => ({}))
        if (!res.ok) {
          const msg =
            typeof data?.detail === 'string'
              ? data.detail
              : await readHttpErrorMessage(res, `Bulk import failed (${res.status})`)
          throw new Error(msg)
        }
        setMappingBulkApiResult(data)
        if (data?.ok && !data?.dry_run) bumpMappingSavedList()
      } catch (err) {
        if (err.name !== 'AbortError') {
          setMappingBulkClientError(err?.message || 'Bulk import failed.')
        }
      } finally {
        setMappingBulkBusy(false)
      }
    },
    [selectedStateCode, mappingBulkDryRun, bumpMappingSavedList],
  )

  const resetMappingComposerToIdle = useCallback(() => {
    mappingArtifactAutoLoadRef.current = true
    mappingSkipDstPairsFetchOnceRef.current = false
    setMappingPersistError(null)
    setMappingMappingLoadError(null)
    setMappingActiveMappingId(null)
    setMappingStateFeeKey('')
    setMappingDstTable('')
    setMappingCommittedSnapshot(null)
    setMappingColumnPairs({})
  }, [])

  const handleMappingComposerNew = useCallback(() => {
    setMappingShowModal(null)
    resetMappingComposerToIdle()
  }, [resetMappingComposerToIdle])

  const closeMappingComposerModal = useCallback(() => {
    setMappingComposerModalOpen(false)
    resetMappingComposerToIdle()
  }, [resetMappingComposerToIdle])

  const openMappingComposerModalForAdd = useCallback(() => {
    handleMappingComposerNew()
    setMappingComposerModalOpen(true)
  }, [handleMappingComposerNew])

  const loadMappingComposerFromRow = useCallback(
    async (mappingId) => {
      if (!selectedStateCode || !mappingId) return
      try {
        const res = await fetch(
          `${API_BASE}/app/fee-column-mappings/${mappingId}?state_code=${encodeURIComponent(selectedStateCode)}`,
        )
        if (!res.ok)
          throw new Error(await readHttpErrorMessage(res, `Mapping failed to load (${res.status})`))
        const data = await res.json()
        const pairs = normalizeColumnPairs(data.column_map)
        const dst = String(data.mapping?.dst_fsname || '').trim()
        const aidRaw = data?.artifact_id
        const aid = Number.isFinite(Number(aidRaw)) ? Number(aidRaw) : null
        const midKnown = Number(data.mapping?.mapping_id ?? mappingId)
        mappingArtifactAutoLoadRef.current = false
        mappingSkipDstPairsFetchOnceRef.current = true
        setMappingActiveMappingId(Number.isFinite(midKnown) ? midKnown : mappingId)
        setMappingPersistError(null)
        setMappingMappingLoadError(null)
        setMappingDstTable(dst || '')
        setMappingColumnPairs(pairs)
        if (dst) {
          setMappingCommittedSnapshot({ dstTable: dst, pairs: { ...pairs } })
        } else {
          setMappingCommittedSnapshot(null)
        }
        if (aid != null) {
          setMappingStateFeeKey(`a:${aid}`)
          setMappingMappingLoadError(null)
        } else {
          setMappingStateFeeKey('')
          setMappingMappingLoadError(
            'This mapping key is not linked to the current artifact list — choose the matching saved file below.',
          )
        }
        setMappingComposerModalOpen(true)
      } catch (e) {
        setMappingPersistError(e?.message || 'Could not load this mapping.')
      }
    },
    [selectedStateCode],
  )

  const openMappingShowModalForRow = useCallback(
    async (mappingId) => {
      setMappingShowModal({ loading: true, error: null, detail: null })
      try {
        if (!selectedStateCode || !mappingId) throw new Error('No state selected.')
        const res = await fetch(
          `${API_BASE}/app/fee-column-mappings/${mappingId}?state_code=${encodeURIComponent(selectedStateCode)}`,
        )
        if (!res.ok)
          throw new Error(await readHttpErrorMessage(res, `Mapping failed to load (${res.status})`))
        const data = await res.json()
        const pairs = normalizeColumnPairs(data.column_map)
        setMappingShowModal({
          loading: false,
          error: null,
          detail: {
            mappingId: Number(data.mapping?.mapping_id ?? mappingId),
            scheduleLabel: String(data.schedule_label || '').trim(),
            dstTable: String(data.mapping?.dst_fsname || '').trim(),
            pairs,
            pairedCount:
              typeof data.paired_column_count === 'number' ? data.paired_column_count : Object.keys(pairs).length,
            updatedAtUtc: String(data.mapping?.updated_at_utc || data.mapping?.created_at_utc || '').trim(),
          },
        })
      } catch (e) {
        setMappingShowModal({ loading: false, error: e?.message || 'Could not load mapping.', detail: null })
      }
    },
    [selectedStateCode],
  )

  const deleteMappingSavedRow = useCallback(
    async (mappingId) => {
      if (!selectedStateCode || !mappingId) return
      const ok = window.confirm(`Delete mapping #${mappingId} for ${selectedStateCode}? This cannot be undone.`)
      if (!ok) return
      setMappingDeleteBusyId(mappingId)
      try {
        const res = await fetch(
          `${API_BASE}/app/fee-column-mappings/${mappingId}?state_code=${encodeURIComponent(selectedStateCode)}`,
          { method: 'DELETE' },
        )
        if (!res.ok)
          throw new Error(await readHttpErrorMessage(res, `Delete failed (${res.status})`))
        if (mappingActiveMappingId === mappingId) {
          setMappingComposerModalOpen(false)
          resetMappingComposerToIdle()
        }
        bumpMappingSavedList()
        setMappingShowModal(null)
      } catch (e) {
        setMappingPersistError(e?.message || 'Delete failed.')
      } finally {
        setMappingDeleteBusyId(null)
      }
    },
    [
      bumpMappingSavedList,
      resetMappingComposerToIdle,
      mappingActiveMappingId,
      selectedStateCode,
    ],
  )

  const handleMappingAccept = useCallback(async () => {
    if (!selectedStateCode || !mappingArtifactIdFromKey) {
      setMappingPersistError('Choose a saved state fee schedule file.')
      return
    }
    const dt = (mappingDstTable || '').trim()
    if (!dt) {
      setMappingPersistError('Choose a DST fee schedule.')
      return
    }
    if (mappingStateColumns.length === 0 || mappingDstColumns.length === 0) {
      setMappingPersistError('Wait for state and DST columns to load.')
      return
    }
    setMappingPersistError(null)
    setMappingPersistLoading(true)
    try {
      const body = {
        state_code: selectedStateCode,
        artifact_id: mappingArtifactIdFromKey,
        dst_fsname: dt,
        column_map_json: columnMapPayload(mappingColumnPairs),
      }
      const res = await fetch(`${API_BASE}/app/fee-column-mappings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Save failed (${res.status})`))
      await res.json()
      bumpMappingSavedList()
      closeMappingComposerModal()
    } catch (e) {
      setMappingPersistError(e?.message || 'Save failed.')
    } finally {
      setMappingPersistLoading(false)
    }
  }, [
    selectedStateCode,
    mappingArtifactIdFromKey,
    mappingDstTable,
    mappingColumnPairs,
    mappingStateColumns.length,
    mappingDstColumns.length,
    bumpMappingSavedList,
    closeMappingComposerModal,
  ])

  const mappingComposerStateFeeReadOnlyLabel = useMemo(() => {
    const row = mappingStateFeePickRows.find((r) => r.key === mappingStateFeeKey)
    return row ? String(row.label || '').trim() : ''
  }, [mappingStateFeePickRows, mappingStateFeeKey])

  const mappingNewFlowDuplicateBlock = useMemo(() => {
    if (mappingActiveMappingId != null) return false
    const aid = mappingArtifactIdFromKey
    const dt = (mappingDstTable || '').trim()
    if (!aid || !dt) return false
    return mappingSavedMappings.some(
      (row) => Number(row.artifact_id) === aid && String(row.dst_fsname || '').trim() === dt,
    )
  }, [mappingActiveMappingId, mappingArtifactIdFromKey, mappingDstTable, mappingSavedMappings])

  const mappingUiCanSave =
    Boolean(mappingArtifactIdFromKey) &&
    Boolean((mappingDstTable || '').trim()) &&
    mappingStateColumns.length > 0 &&
    mappingDstColumns.length > 0 &&
    !mappingPersistLoading &&
    (mappingActiveMappingId != null || !mappingNewFlowDuplicateBlock)

  const closePreview = useCallback(() => {
    setPreview((prev) => {
      if (prev?.blobUrl) URL.revokeObjectURL(prev.blobUrl)
      return null
    })
  }, [])

  useEffect(() => {
    if (!preview) return
    const onKey = (e) => {
      if (e.key === 'Escape') closePreview()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [preview, closePreview])

  useEffect(() => {
    try {
      if (selectedStateCode) sessionStorage.setItem('feeToolStateCode', selectedStateCode)
      else sessionStorage.removeItem('feeToolStateCode')
    } catch {
      /* ignore */
    }
  }, [selectedStateCode])

  useEffect(() => {
    if (activeNav !== 'stateUrls') {
      setPortalEditorStateCode('')
    }
  }, [activeNav])

  useEffect(() => {
    if (activeNav !== 'stateUrls') return undefined
    setPortalEditorStateCode((c) => {
      if (c) return c
      return selectedStateCode || US_STATES[0]?.code || ''
    })
    return undefined
  }, [activeNav, selectedStateCode])

  useEffect(() => {
    if (activeNav !== 'stateUrls' || !portalEditorStateCode) return undefined
    const ac = new AbortController()
    fetch(`${API_BASE}/app/state-portal-links?state_code=${encodeURIComponent(portalEditorStateCode)}&limit=1`, {
      signal: ac.signal,
    })
      .then(async (res) => {
        if (!res.ok) return { links: [] }
        return res.json()
      })
      .then((data) => {
        const links = Array.isArray(data?.links) ? data.links : []
        const row = links[0] || null
        setPortalEditorLinkId(row?.link_id != null ? Number(row.link_id) : null)
        setUrlFormLabel((row?.display_label || '').trim())
        setUrlFormPortalUrl((row?.portal_url || '').trim())
      })
      .catch(() => {
        if (!ac.signal.aborted) {
          setPortalEditorLinkId(null)
          setUrlFormLabel('')
          setUrlFormPortalUrl('')
        }
      })
    return () => ac.abort()
  }, [activeNav, portalEditorStateCode])

  useEffect(() => {
    if (activeNav !== 'dst') return undefined
    const ac = new AbortController()
    setDstTablesLoading(true)
    setDstTablesError(null)
    fetch(`${API_BASE}/dst/tables`, { signal: ac.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Tables failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        const raw = Array.isArray(data?.tables) ? data.tables : []
        const tables = [...raw].sort((a, b) =>
          String(a || '').localeCompare(String(b || ''), undefined, { sensitivity: 'base', numeric: true }),
        )
        setDstTables(tables)
      })
      .catch((e) => {
        if (e.name !== 'AbortError')
          setDstTablesError(e?.message || 'Could not load table list — check backend SQL Server settings.')
      })
      .finally(() => setDstTablesLoading(false))
    return () => ac.abort()
  }, [activeNav])

  useEffect(() => {
    if (activeNav !== 'schedules' && activeNav !== 'mapping') {
      setDstFeeSchedules([])
      setDstFeeScheduleTable('')
      setDstFeeSchedulesLoading(false)
      setDstFeeSchedulesError(null)
      return undefined
    }
    if (!selectedStateCode) {
      setDstFeeSchedules([])
      setDstFeeScheduleTable('')
      setDstFeeSchedulesLoading(false)
      setDstFeeSchedulesError(null)
      return undefined
    }
    const ac = new AbortController()
    setDstFeeSchedulesLoading(true)
    setDstFeeSchedulesError(null)
    fetch(`${API_BASE}/dst/fee-schedules?state_code=${encodeURIComponent(selectedStateCode)}`, {
      signal: ac.signal,
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Fee schedules failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        const raw = Array.isArray(data?.schedules) ? data.schedules : []
        const schedules = [...raw].sort((a, b) =>
          String(a || '').localeCompare(String(b || ''), undefined, { sensitivity: 'base', numeric: true }),
        )
        setDstFeeSchedules(schedules)
        setDstFeeScheduleTable(String(data?.table || '').trim())
      })
      .catch((e) => {
        if (e.name !== 'AbortError') {
          setDstFeeSchedules([])
          setDstFeeScheduleTable('')
          setDstFeeSchedulesError(e?.message || 'Could not load DST fee schedules for this state.')
        }
      })
      .finally(() => setDstFeeSchedulesLoading(false))
    return () => ac.abort()
  }, [activeNav, selectedStateCode])

  useEffect(() => {
    if (activeNav !== 'dst') return undefined
    if (!dstSelectedTable) {
      setDstColumns([])
      setDstRows([])
      setDstRowsError(null)
      setDstRowsLoading(false)
      return undefined
    }
    const ac = new AbortController()
    setDstRowsLoading(true)
    setDstRowsError(null)
    const q = new URLSearchParams({ table: dstSelectedTable, limit: '5000' })
    if (selectedStateCode) q.set('state_code', selectedStateCode)
    fetch(`${API_BASE}/dst/rows?${q}`, { signal: ac.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Rows failed (${res.status})`))
        return res.json()
      })
      .then((data) => {
        setDstColumns(Array.isArray(data?.columns) ? data.columns : [])
        setDstRows(Array.isArray(data?.rows) ? data.rows : [])
      })
      .catch((e) => {
        if (e.name !== 'AbortError') {
          setDstColumns([])
          setDstRows([])
          setDstRowsError(e?.message || 'Failed to fetch rows.')
        }
      })
      .finally(() => setDstRowsLoading(false))
    return () => ac.abort()
  }, [activeNav, dstSelectedTable, selectedStateCode])

  useEffect(() => {
    if (activeNav !== 'schedules') {
      closeSchedulesDstFeeModal()
      clearSchedulesArtifactPreview()
    }
  }, [activeNav, clearSchedulesArtifactPreview, closeSchedulesDstFeeModal])

  useEffect(() => {
    if (activeNav !== 'schedules' || !schedulesDstFeeModalOpen || !schedulesDstModalTable || !dstFeeScheduleTable)
      return undefined
    const fsName = schedulesDstModalTable
    const ac = new AbortController()
    const cacheKey = dstSchedulesPreviewCacheKey(selectedStateCode, fsName)
    const hit = dstSchedulesPreviewCacheRef.current.get(cacheKey)

    const applyPreviewRangeFilter = (columns, rows) => {
      const startIso = (schedulesDstPreviewFilterStartRef.current || '').trim()
      const dc = schedulesDstPreviewFilterDateColRef.current
      const cols = Array.isArray(columns) ? columns : []
      const r = Array.isArray(rows) ? rows : []
      if (startIso && dc && cols.length) {
        return { columns: cols, rows: filterDstRowsByEffectiveRange(r, dc, startIso) }
      }
      return { columns: cols, rows: r }
    }

    if (hit?.columns?.length) {
      const out = applyPreviewRangeFilter(hit.columns, hit.rows)
      setSchedulesDstCols(out.columns)
      setSchedulesDstRows(out.rows)
      setSchedulesDstError(null)
      setSchedulesDstLoading(false)
      setSchedulesDstPrefetchTick((n) => n + 1)
      return () => ac.abort()
    }
    setSchedulesDstLoading(true)
    setSchedulesDstError(null)
    setSchedulesDstCols([])
    setSchedulesDstRows([])
    void fetchDstSchedulesPreviewRows(dstFeeScheduleTable, fsName, selectedStateCode, ac.signal)
      .then((data) => {
        dstSchedulesPreviewCacheRef.current.set(cacheKey, { columns: data.columns, rows: data.rows })
        trimSchedulesPreviewMap(dstSchedulesPreviewCacheRef.current, SCHEDULES_PREVIEW_CACHE_LIMIT)
        const out = applyPreviewRangeFilter(data.columns, data.rows)
        setSchedulesDstCols(out.columns)
        setSchedulesDstRows(out.rows)
      })
      .catch((e) => {
        if (e.name !== 'AbortError') {
          setSchedulesDstCols([])
          setSchedulesDstRows([])
          setSchedulesDstError(e?.message || 'Failed to fetch rows.')
        }
      })
      .finally(() => {
        setSchedulesDstLoading(false)
        setSchedulesDstPrefetchTick((n) => n + 1)
      })
    return () => ac.abort()
  }, [activeNav, schedulesDstFeeModalOpen, schedulesDstModalTable, selectedStateCode, dstFeeScheduleTable])

  useEffect(() => {
    if (
      (activeNav !== 'schedules' && activeNav !== 'scheduleVersions') ||
      !schedulesStateFeeModalOpen ||
      schedulesStatePreviewId == null
    )
      return undefined
    const id = schedulesStatePreviewId
    const ac = new AbortController()
    const cached = stateArtifactTablePreviewCacheRef.current.get(id)
    if (cached?.cols?.length) {
      setSchedulesStateLoading(false)
      setSchedulesStateError(null)
      if (schedulesStatePdfRef.current) {
        URL.revokeObjectURL(schedulesStatePdfRef.current)
        schedulesStatePdfRef.current = ''
      }
      setSchedulesStatePdfUrl('')
      setSchedulesStateCols(cached.cols)
      setSchedulesStateRows(cached.rows)
      return () => ac.abort()
    }

    setSchedulesStateLoading(true)
    setSchedulesStateError(null)
    setSchedulesStateCols([])
    setSchedulesStateRows([])
    if (schedulesStatePdfRef.current) {
      URL.revokeObjectURL(schedulesStatePdfRef.current)
      schedulesStatePdfRef.current = ''
    }
    setSchedulesStatePdfUrl('')

    void fetchSchedulesArtifactPreview(id, mergedArtifactsForPreview, ac.signal)
      .then((out) => {
        if (out.kind === 'table') {
          stateArtifactTablePreviewCacheRef.current.set(id, { cols: out.cols, rows: out.rows })
          trimSchedulesPreviewMap(stateArtifactTablePreviewCacheRef.current, SCHEDULES_PREVIEW_CACHE_LIMIT)
          setSchedulesStateCols(out.cols)
          setSchedulesStateRows(out.rows)
        } else {
          const burl = URL.createObjectURL(out.blob)
          schedulesStatePdfRef.current = burl
          setSchedulesStatePdfUrl(burl)
        }
      })
      .catch((e) => {
        if (e.name !== 'AbortError') {
          setSchedulesStateError(e?.message || 'Preview failed.')
        }
      })
      .finally(() => {
        setSchedulesStateLoading(false)
      })
    return () => ac.abort()
  }, [activeNav, schedulesStateFeeModalOpen, schedulesStatePreviewId, mergedArtifactsForPreview])

  useEffect(() => {
    if (activeNav !== 'schedules' && activeNav !== 'scheduleVersions') return undefined
    const m = schedulesStateFeeKey.match(/^a:(\d+)$/)
    if (!m) return undefined
    const id = Number(m[1], 10)
    if (!Number.isFinite(id) || stateArtifactTablePreviewCacheRef.current.has(id)) return undefined
    const ac = new AbortController()
    void fetchSchedulesArtifactPreview(id, mergedArtifactsForPreview, ac.signal)
      .then((out) => {
        if (out.kind === 'table') {
          stateArtifactTablePreviewCacheRef.current.set(id, { cols: out.cols, rows: out.rows })
          trimSchedulesPreviewMap(stateArtifactTablePreviewCacheRef.current, SCHEDULES_PREVIEW_CACHE_LIMIT)
        }
      })
      .catch(() => {})
    return () => ac.abort()
  }, [activeNav, schedulesStateFeeKey, mergedArtifactsForPreview])

  useEffect(() => {
    if (activeNav !== 'schedules') return undefined
    const m = schedulesDstFeeKey.match(/^d:(.+)$/)
    if (!m || !dstFeeScheduleTable) return undefined
    const fsName = m[1]
    const cacheKey = dstSchedulesPreviewCacheKey(selectedStateCode, fsName)
    if (dstSchedulesPreviewCacheRef.current.has(cacheKey)) return undefined
    const ac = new AbortController()
    void fetchDstSchedulesPreviewRows(dstFeeScheduleTable, fsName, selectedStateCode, ac.signal)
      .then((data) => {
        dstSchedulesPreviewCacheRef.current.set(cacheKey, { columns: data.columns, rows: data.rows })
        trimSchedulesPreviewMap(dstSchedulesPreviewCacheRef.current, SCHEDULES_PREVIEW_CACHE_LIMIT)
        setSchedulesDstPrefetchTick((n) => n + 1)
      })
      .catch(() => {
        setSchedulesDstPrefetchTick((n) => n + 1)
      })
    return () => ac.abort()
  }, [activeNav, schedulesDstFeeKey, selectedStateCode, dstFeeScheduleTable])

  useEffect(() => {
    if (!schedulesStateFeeModalOpen && !schedulesDstFeeModalOpen) return undefined
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      if (schedulesDstFeeModalOpen) closeSchedulesDstFeeModal()
      else clearSchedulesArtifactPreview()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [schedulesStateFeeModalOpen, schedulesDstFeeModalOpen, closeSchedulesDstFeeModal, clearSchedulesArtifactPreview])

  const saveStatePortalUrl = useCallback(async () => {
    if (!portalEditorStateCode) {
      setStateUrlsMessage({ type: 'error', text: 'Choose which state you are configuring.' })
      return
    }
    const portalUrl = urlFormPortalUrl.trim()
    if (!isValidHttpUrl(portalUrl)) {
      setStateUrlsMessage({ type: 'error', text: 'Portal URL must be a valid http or https address.' })
      return
    }
    setStateUrlsSaving(true)
    setStateUrlsMessage(null)
    try {
      const res = await fetch(`${API_BASE}/app/state-portal-links`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          state_code: portalEditorStateCode,
          display_label:
            urlFormLabel.trim() || `${stateNameFromCode(portalEditorStateCode)} (${portalEditorStateCode})`,
          portal_url: portalUrl,
          sort_order: 0,
        }),
      })
      if (!res.ok) {
        const msg = await readHttpErrorMessage(res, `Save failed (${res.status})`)
        throw new Error(msg)
      }
      const data = await res.json()
      await refreshPortalLinks()
      setSelectedStateCode(portalEditorStateCode)
      setStateUrlsMessage({
        type: 'ok',
        text: data?.inserted ? 'Saved new portal URL for this state.' : 'Updated portal URL for this state.',
      })
    } catch (e) {
      setStateUrlsMessage({ type: 'error', text: e?.message || 'Could not save portal URL.' })
    } finally {
      setStateUrlsSaving(false)
    }
  }, [portalEditorStateCode, refreshPortalLinks, urlFormLabel, urlFormPortalUrl])

  const deleteStatePortalUrl = useCallback(async () => {
    if (!portalEditorStateCode) {
      setStateUrlsMessage({ type: 'error', text: 'Choose which state you are configuring.' })
      return
    }
    setStateUrlsSaving(true)
    setStateUrlsMessage(null)
    try {
      const res = await fetch(
        `${API_BASE}/app/state-portal-links/by-state/${encodeURIComponent(portalEditorStateCode)}`,
        { method: 'DELETE' },
      )
      if (!res.ok) {
        const msg = await readHttpErrorMessage(res, `Delete failed (${res.status})`)
        throw new Error(msg)
      }
      await refreshPortalLinks()
      setUrlFormLabel('')
      setUrlFormPortalUrl('')
      setStateUrlsMessage({ type: 'ok', text: 'Removed saved portal URL for this state.' })
    } catch (e) {
      setStateUrlsMessage({ type: 'error', text: e?.message || 'Could not delete portal URL.' })
    } finally {
      setStateUrlsSaving(false)
    }
  }, [portalEditorStateCode, refreshPortalLinks])

  const savedPortalUrl = useMemo(
    () => ((portalLinkRow?.portal_url || '') + '').trim(),
    [portalLinkRow],
  )

  const effectiveRunUrl = useMemo(() => {
    const manual = stateSourceUrl.trim()
    if (manual && isValidHttpUrl(manual)) return manual
    if (savedPortalUrl && isValidHttpUrl(savedPortalUrl)) return savedPortalUrl
    return ''
  }, [stateSourceUrl, savedPortalUrl])

  /** @deprecated use effectiveRunUrl — kept name for preview referrers */
  const agentUrl = effectiveRunUrl

  const runUrlValid = useMemo(() => isValidHttpUrl(effectiveRunUrl), [effectiveRunUrl])

  const runAgent = useCallback(async () => {
    if (!selectedStateCode || !runUrlValid) return
    const stateAtStart = selectedStateCode
    setAgentLoading(true)
    setAgentError(null)
    setAgentResult(null)
    setSchedulesStateFeeKey('')
    try {
      const res = await fetch(`${API_BASE}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          // Same URL the UI validates (saved portal when manual field is empty).
          url: effectiveRunUrl.trim(),
          state_code: selectedStateCode,
          persist_artifacts: true,
          paginate: RUN_DEFAULTS.paginate,
          max_pages: RUN_DEFAULTS.max_pages,
          max_tables: RUN_DEFAULTS.max_tables,
          max_artifact_downloads: RUN_DEFAULTS.maxArtifactDownloads,
        }),
      })
      if (!res.ok) {
        const msg = await readHttpErrorMessage(res, `Request failed (${res.status})`)
        throw new Error(msg)
      }
      const data = await res.json()
      if (data?.error) throw new Error(data.error)
      if (stateAtStart !== selectedStateCodeRef.current) return
      setAgentResult(data)
      await refreshStateArtifacts()
      await refreshArtifactHistory()
      void refreshPortalLinks()
      void refreshCompanionHealth()
      setSchedulesRecentComparesTick((t) => t + 1)
    } catch (e) {
      if (stateAtStart === selectedStateCodeRef.current) {
        setAgentError(e.message || 'Failed to reach agent')
      }
    } finally {
      if (stateAtStart === selectedStateCodeRef.current) {
        setAgentLoading(false)
      }
    }
  }, [selectedStateCode, runUrlValid, effectiveRunUrl, refreshStateArtifacts, refreshArtifactHistory, refreshPortalLinks, refreshCompanionHealth])

  const lastRunPersistSummary = useMemo(() => {
    if (!agentResult || agentResult.blocked) return null
    if (!('artifacts_saved' in agentResult)) return null
    const arr = Array.isArray(agentResult.artifacts_saved) ? agentResult.artifacts_saved : []
    const er = Array.isArray(agentResult.artifacts_errors) ? agentResult.artifacts_errors : []
    const cand = Number(agentResult.artifact_download_candidates)
    const skipped = arr.filter((x) => x && x.skipped).length
    const wrote = arr.length - skipped
    let msg
    if (Number.isFinite(cand) && cand === 0) {
      msg =
        'Last run: no file download URLs were detected for auto-save (catalog rows had no matching http(s) file links).'
      const discMsg = agentResult?.artifact_discovery?.user_message
      if (typeof discMsg === 'string' && discMsg.trim()) {
        msg = `${msg} ${discMsg.trim()}`
      }
    } else if (arr.length === 0 && er.length === 0) {
      msg = `Last run: ${cand} file URL(s) queued; nothing new written (all skipped as unchanged, or no bytes saved).`
    } else {
      msg = `Last run: ${cand} URL(s) considered · saved ${wrote} new file(s), skipped ${skipped} unchanged, ${er.length} download error(s).`
    }
    if (typeof msg === 'string' && Number.isFinite(cand) && cand > 0) {
      const attempts = Number(agentResult.artifact_download_attempts)
      const trunc = Number(agentResult.artifact_download_truncated)
      if (Number.isFinite(trunc) && trunc > 0) {
        msg = `${msg} (${trunc} URL(s) not attempted — downloads capped; raise max_artifact_downloads or set ARTIFACT_DOWNLOAD_MAX_PER_RUN to 0 for unlimited.)`
      } else if (Number.isFinite(attempts) && attempts >= 0 && attempts < cand) {
        msg = `${msg} (Only ${attempts} of ${cand} discovered URL(s) were downloaded this run.)`
      }
    }
    const pe = agentResult.artifacts_pruned_error
    if (pe) {
      msg = `${msg} Stale-file cleanup failed: ${String(pe)}`
    } else {
      const p = Number(agentResult.artifacts_pruned)
      if (Number.isFinite(p) && p > 0) {
        msg = `${msg} Removed ${p} saved file(s) that no longer appear on the portal.`
      }
    }
    const dur = Number(agentResult.run_duration_seconds)
    if (Number.isFinite(dur) && dur >= 0) {
      msg = `${msg} Run time: ${dur.toFixed(1)}s.`
    }
    const tok = formatLlmTokenUsageSummary(agentResult.llm_token_usage)
    if (tok) msg = `${msg} ${tok}`
    return msg
  }, [agentResult])

  const stateButtonsDisabled = !selectedStateCode || !runUrlValid || agentLoading
  const compareDisabled = !stateUploadFile || !dstUploadFile

  const requestPreview = useCallback(
    async (spec) => {
      if (spec.kind === 'portal' || spec.kind === 'bad') {
        setPreview((prev) => {
          if (prev?.blobUrl) URL.revokeObjectURL(prev.blobUrl)
          return spec
        })
        return
      }
      if (spec.kind !== 'http') return

      const refUrl = String(spec.referrerUrl || '').trim()
      const sid = spec.previewSessionId || agentResult?.preview_auth?.session_id || null
      const dh = spec.documentHint ?? null

      setPreview((prev) => {
        if (prev?.blobUrl) URL.revokeObjectURL(prev.blobUrl)
        return { kind: 'snippet_loading', title: spec.title }
      })

      try {
        const snippet = await fetchPreviewSnippet({
          resourceUrl: spec.url,
          referrerUrl: refUrl || agentResult?.resolved_url || agentUrl.trim() || null,
          sessionId: sid || null,
          documentHint: dh,
        })

        if (!snippet.ok) {
          setPreview((prev) => {
            if (prev?.blobUrl) URL.revokeObjectURL(prev.blobUrl)
            return {
              kind: 'snippet_rejected',
              title: spec.title,
              resourceUrl: spec.url,
              errorCode: snippet.error_code || 'preview_rejected',
              referrerUrl: refUrl || agentResult?.resolved_url || agentUrl.trim(),
              previewSessionId: sid,
              documentHint: dh,
              upstreamAttempts: Array.isArray(snippet.upstream_attempts) ? snippet.upstream_attempts : null,
            }
          })
          return
        }

        let blobUrl = null
        if (snippet.inline_base64 && snippet.mime) {
          blobUrl = URL.createObjectURL(base64ToBlob(snippet.inline_base64, snippet.mime))
        }

        setPreview({
          kind: 'snippet',
          title: spec.title,
          resourceUrl: spec.url,
          snippet,
          blobUrl,
          referrerUrl: refUrl || agentResult?.resolved_url || agentUrl.trim(),
          previewSessionId: sid,
          documentHint: dh,
        })
      } catch (e) {
        setPreview((prev) => {
          if (prev?.blobUrl) URL.revokeObjectURL(prev.blobUrl)
          return {
            kind: 'snippet_error',
            title: spec.title,
            resourceUrl: spec.url,
            message: e?.message || 'Preview failed',
            referrerUrl: refUrl || agentResult?.resolved_url || agentUrl.trim(),
            previewSessionId: sid,
            documentHint: dh,
          }
        })
      }
    },
    [agentResult, agentUrl],
  )

  const pageTitle = useMemo(() => {
    if (activeNav === 'scheduleVersions') return 'Schedule versions'
    if (activeNav === 'notifications') return 'Notifications'
    return 'Fee Schedules'
  }, [activeNav])
  const runCompare = useCallback(() => {
    if (!stateUploadFile || !dstUploadFile) {
      setCompareMessage('Please upload both files to start comparison.')
      return
    }
    setCompareMessage(`Ready to compare "${stateUploadFile.name}" with "${dstUploadFile.name}".`)
  }, [dstUploadFile, stateUploadFile])

  const schedulesCompareArtifactReady = /^a:\d+$/.test(schedulesStateFeeKey || '')
  const schedulesCompareDstReady = /^d:.+$/.test(schedulesDstFeeKey || '')
  const schedulesCompareDisabled =
    !selectedStateCode ||
    !schedulesCompareArtifactReady ||
    !schedulesCompareDstReady ||
    schedulesCompareLoading ||
    agentLoading ||
    stateArtifactsLoading

  const runSchedulesCompare = useCallback(async () => {
    const am = schedulesStateFeeKey.match(/^a:(\d+)$/)
    const dm = schedulesDstFeeKey.match(/^d:(.+)$/)
    if (!selectedStateCode || !am || !dm) return
    setSchedulesCompareLoading(true)
    setSchedulesCompareError(null)
    try {
      const res = await fetch(`${API_BASE}/app/fee-schedules/compare`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          state_code: selectedStateCode,
          artifact_id: Number(am[1], 10),
          dst_fsname: dm[1].trim(),
        }),
      })
      const raw = await res.text()
      if (!res.ok) {
        let detail = `Compare failed (${res.status})`
        try {
          const j = JSON.parse(raw)
          if (typeof j.detail === 'string') detail = j.detail
        } catch {
          if (raw.trim()) detail = raw.trim().slice(0, 400)
        }
        throw new Error(detail)
      }
      const data = JSON.parse(raw)
      cacheCompareReplayPayload(data, compareReplayCacheRef)
      setSchedulesCompareResult(data)
      setSchedulesCompareOpen(true)
      const stateRow = stateFeePickRows.find((r) => r.key === schedulesStateFeeKey)
      const dstRow = dstTablePickOptions.find((o) => o.key === schedulesDstFeeKey)
      persistFeeToolCompareSuccess(selectedStateCode.toUpperCase(), {
        stateLabel: stateRow?.label || `Saved file #${am[1]}`,
        dstLabel: dstRow?.label || dm[1].trim(),
        artifactId: Number(am[1], 10),
        dstTable: dm[1].trim(),
      })
      setSchedulesMetricsTick((t) => t + 1)
      setSchedulesRecentComparesTick((t) => t + 1)
    } catch (e) {
      setSchedulesCompareResult(null)
      setSchedulesCompareError(e?.message || 'Compare failed.')
      setSchedulesCompareOpen(true)
    } finally {
      setSchedulesCompareLoading(false)
    }
  }, [
    selectedStateCode,
    schedulesStateFeeKey,
    schedulesDstFeeKey,
    schedulesDstStartDateIso,
    stateFeePickRows,
    dstTablePickOptions,
  ])

  const saveNotificationContact = useCallback(
    async (e) => {
      e.preventDefault()
      const sc = (selectedStateCode || '').trim()
      if (!sc) return
      const nm = notifFormName.trim()
      const em = notifFormEmail.trim()
      if (!nm || !em) {
        setNotifFormError('Recipient name and email are required.')
        return
      }
      setNotifSaving(true)
      setNotifFormError(null)
      try {
        const body = {
          state_code: sc,
          contact_name: nm,
          email: em,
          team_name: notifFormTeam.trim() || null,
          department_name: notifFormDept.trim() || null,
          notifications_enabled: notifFormEnabled,
          notify_new_state_file: notifFormNewFile,
          notify_compare_result: notifFormCompare,
        }
        const path =
          notifEditingId != null
            ? `${API_BASE}/app/notification-contacts/${encodeURIComponent(String(notifEditingId))}`
            : `${API_BASE}/app/notification-contacts`
        const res = await fetch(path, {
          method: notifEditingId != null ? 'PUT' : 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Save failed (${res.status})`))
        setNotifContactsTick((t) => t + 1)
        setNotifTeamModalOpen(false)
        setNotifEditingId(null)
        setNotifFormName('')
        setNotifFormEmail('')
        setNotifFormTeam('')
        setNotifFormDept('')
        setNotifFormEnabled(true)
        setNotifFormNewFile(true)
        setNotifFormCompare(true)
      } catch (err) {
        setNotifFormError(err?.message || 'Could not save this team.')
      } finally {
        setNotifSaving(false)
      }
    },
    [
      selectedStateCode,
      notifEditingId,
      notifFormName,
      notifFormEmail,
      notifFormTeam,
      notifFormDept,
      notifFormEnabled,
      notifFormNewFile,
      notifFormCompare,
    ],
  )

  const cancelNotificationEdit = useCallback(() => {
    setNotifFormError(null)
    setNotifEditingId(null)
    setNotifFormName('')
    setNotifFormEmail('')
    setNotifFormTeam('')
    setNotifFormDept('')
    setNotifFormEnabled(true)
    setNotifFormNewFile(true)
    setNotifFormCompare(true)
    setNotifTeamModalOpen(false)
  }, [])

  const openAddNotificationTeamModal = useCallback(() => {
    setNotifFormError(null)
    setNotifEditingId(null)
    setNotifFormName('')
    setNotifFormEmail('')
    setNotifFormTeam('')
    setNotifFormDept('')
    setNotifFormEnabled(true)
    setNotifFormNewFile(true)
    setNotifFormCompare(true)
    setNotifTeamModalOpen(true)
  }, [])

  const deleteNotificationContactRow = useCallback(
    async (cid) => {
      const sc = (selectedStateCode || '').trim()
      if (!sc || cid == null) return
      setNotifDeleteBusyId(cid)
      setNotifContactsError(null)
      try {
        const res = await fetch(
          `${API_BASE}/app/notification-contacts/${encodeURIComponent(String(cid))}?state_code=${encodeURIComponent(sc)}`,
          { method: 'DELETE' },
        )
        if (!res.ok) throw new Error(await readHttpErrorMessage(res, `Delete failed (${res.status})`))
        if (notifEditingId === cid) {
          cancelNotificationEdit()
        }
        setNotifContactsTick((t) => t + 1)
      } catch (err) {
        setNotifContactsError(err?.message || 'Could not remove this team.')
      } finally {
        setNotifDeleteBusyId(null)
      }
    },
    [selectedStateCode, notifEditingId, cancelNotificationEdit],
  )

  const editNotificationContactRow = useCallback((row) => {
    const id = Number(row?.notification_contact_id)
    if (!Number.isFinite(id)) return
    const apiBit = (v) => v === true || v === 1 || v === '1'
    setNotifFormError(null)
    setNotifEditingId(id)
    setNotifFormName(String(row?.contact_name || ''))
    setNotifFormEmail(String(row?.email || ''))
    setNotifFormTeam(String(row?.team_name || ''))
    setNotifFormDept(String(row?.department_name || ''))
    setNotifFormEnabled(apiBit(row?.notifications_enabled))
    setNotifFormNewFile(apiBit(row?.notify_new_state_file))
    setNotifFormCompare(apiBit(row?.notify_compare_result))
    setNotifTeamModalOpen(true)
  }, [])

  const schedulesDashboardMetrics = useMemo(() => {
    void schedulesMetricsTick
    const sc = (selectedStateCode || '').trim().toUpperCase()
    if (!sc) {
      return {
        feeScheduleCountDisplay: '—',
        compareCountDisplay: '—',
        latestMain: '—',
        latestSub: 'Select a state',
        lastSyncMain: '—',
        lastSyncSub: '—',
      }
    }
    const m = readFeeToolSchedulesMetrics()
    const scLoose = (selectedStateCode || '').trim()
    const last = m.lastCompareByState[sc] || m.lastCompareByState[scLoose] || null
    const compareN = Number(m.compareCountByState[sc] || m.compareCountByState[scLoose]) || 0

    let feeDisp = '—'
    if (stateArtifactsLoading) feeDisp = '…'
    else feeDisp = String(stateArtifacts.length)

    const lastSyncIso = portalLinkRow?.last_agent_run_at_utc ?? null

    return {
      feeScheduleCountDisplay: feeDisp,
      compareCountDisplay: String(compareN),
      latestMain:
        last && (last.stateLabel || last.dstLabel)
          ? `${last.stateLabel || 'Saved file'} ↔ ${last.dstLabel || last.dstTable || 'DST'}`
          : '—',
      latestSub: last?.at ? formatPortalLastRunAt(last.at) : 'No comparisons yet',
      lastSyncMain: formatRelativeAgo(lastSyncIso),
      lastSyncSub: formatPortalLastRunAt(lastSyncIso),
    }
  }, [
    selectedStateCode,
    stateArtifacts.length,
    stateArtifactsLoading,
    schedulesMetricsTick,
    portalLinkRow?.last_agent_run_at_utc,
  ])

  const refreshCompareRuns = useCallback(async () => {
    const sc = (selectedStateCode || '').trim()
    if (!sc) {
      setCompareRunsRows([])
      setCompareRunsError(null)
      return
    }
    setCompareRunsLoading(true)
    setCompareRunsError(null)
    try {
      const res = await fetch(
        `${API_BASE}/app/compare-runs?state_code=${encodeURIComponent(sc)}&limit=50`,
      )
      if (!res.ok) {
        throw new Error(await readHttpErrorMessage(res, `Compare history failed (${res.status})`))
      }
      const data = await res.json()
      const raw = Array.isArray(data?.compare_runs) ? data.compare_runs : []
      setCompareRunsRows(raw.map(mapCompareRunApiRow).filter(Boolean))
    } catch (e) {
      setCompareRunsRows([])
      setCompareRunsError(e?.message || 'Could not load recent comparisons.')
    } finally {
      setCompareRunsLoading(false)
    }
  }, [selectedStateCode])

  useEffect(() => {
    if (activeNav !== 'schedules' && activeNav !== 'scheduleVersions') return undefined
    void refreshCompareRuns()
  }, [activeNav, refreshCompareRuns, schedulesRecentComparesTick])

  const feeSchedRecentCompareRows = useMemo(() => compareRunsRows, [compareRunsRows])

  const openSavedFeeComparison = useCallback(
    async (entry) => {
      if (entry?.snapshot?.rows?.length) {
        const r = recentCompareEntryToResult(entry)
        if (!r) return
        setSchedulesCompareError(null)
        setSchedulesCompareReplayLoading(false)
        setSchedulesCompareResult(r)
        setSchedulesCompareOpen(true)
        return
      }
      const cid = Number(entry?.compareRunId)
      const sc = entry?.stateCode || selectedStateCode
      if (!Number.isFinite(cid) || !sc) return

      const cached = compareReplayCacheRef.current.get(cid)
      if (cached) {
        setSchedulesCompareError(null)
        setSchedulesCompareReplayLoading(false)
        setSchedulesCompareResult(cached)
        setSchedulesCompareOpen(true)
        return
      }

      setSchedulesCompareReplayLoading(true)
      setSchedulesCompareError(null)
      setSchedulesCompareResult(null)
      setSchedulesCompareOpen(true)
      try {
        const res = await fetch(
          `${API_BASE}/app/compare-runs/${encodeURIComponent(String(cid))}?state_code=${encodeURIComponent(sc)}`,
        )
        const raw = await res.text()
        if (!res.ok) {
          let detail = `Could not load saved comparison (${res.status})`
          try {
            const j = JSON.parse(raw)
            if (typeof j.detail === 'string') detail = j.detail
          } catch {
            if (raw.trim()) detail = raw.trim().slice(0, 400)
          }
          throw new Error(detail)
        }
        const data = JSON.parse(raw)
        const replay = data?.replay
        if (!replay || typeof replay !== 'object') {
          throw new Error('Saved comparison snapshot is missing.')
        }
        cacheCompareReplayPayload(replay, compareReplayCacheRef)
        setSchedulesCompareResult(replay)
      } catch (e) {
        setSchedulesCompareResult(null)
        setSchedulesCompareError(e?.message || 'Could not open saved comparison.')
      } finally {
        setSchedulesCompareReplayLoading(false)
      }
    },
    [selectedStateCode],
  )

  const downloadSavedFeeComparison = useCallback((entry) => {
    if (entry?.compareRunId != null && entry?.hasWorkbook) {
      const sc = entry.stateCode || selectedStateCode || ''
      const url = `${API_BASE}/app/compare-runs/${encodeURIComponent(String(entry.compareRunId))}/download?state_code=${encodeURIComponent(sc)}`
      window.open(url, '_blank', 'noopener,noreferrer')
      return
    }
    const shell = recentCompareEntryToResult(entry)
    const rows = Array.isArray(entry?.snapshot?.rows) ? entry.snapshot.rows : []
    if (!shell || !rows.length) {
      window.alert('No changed workbook saved for this comparison.')
      return
    }
    const baseName = `compare_${entry?.stateCode || 'SC'}_${entry?.dstFsname || 'dst'}`.replace(/[^\w.-]+/g, '_')
    downloadFeeCompareWorkbook(baseName, shell, rows)
  }, [selectedStateCode])

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header__brand">
          <img src={logo} alt="Company logo" className="app-logo" width={160} height={40} decoding="async" />
          <div>
            <h1 className="app-header__title">Fee Schedule Comparison Tool</h1>
          </div>
        </div>
        <div className="app-header__meta">
          <span className="app-pill">Internal</span>
          <span className="app-pill app-pill--muted">v1.0</span>
        </div>
      </header>

      <div className="app-body">
        <aside className="app-sidebar" aria-label="Primary">
          <nav className="app-nav">
            {NAV.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`app-nav__item${activeNav === item.id ? ' app-nav__item--active' : ''}`}
                onClick={() => setActiveNav(item.id)}
              >
                <span className="app-nav__icon">
                  <NavIcon name={item.icon} />
                </span>
                {item.label}
              </button>
            ))}
          </nav>
          <div className="app-sidebar__footer">
            <label className="app-sidebar__footer-label" htmlFor="sidebar-linked-state">
              Active state
            </label>
            <select
              id="sidebar-linked-state"
              className="app-select app-sidebar__footer-select"
              value={selectedStateCode}
              onChange={(e) => setSelectedStateCode(e.target.value)}
              disabled={linkedPortalLinks.length === 0}
              aria-label="Configured state for runs, DST filter, and fee schedule list"
            >
              {linkedPortalLinks.length === 0 ? (
                <option value="">No portals saved</option>
              ) : (
                linkedPortalLinks.map((row) => (
                  <option key={row.state_code} value={row.state_code}>
                    {stateNameFromCode(row.state_code)} ({row.state_code})
                  </option>
                ))
              )}
            </select>
            {linkedPortalLinks.length > 0 && selectedStateCode ? (
              <div className="app-sidebar__sync">
                <div className="app-sidebar__sync-row" role="status">
                  <span
                    className={`app-sidebar__sync-dot${
                      portalLinkRow?.last_agent_run_at_utc ? ' app-sidebar__sync-dot--on' : ''
                    }`}
                    aria-hidden
                  />
                  <span className="app-sidebar__sync-label">
                    {portalLinkRow?.last_agent_run_at_utc ? 'Synced' : 'Not synced'}
                  </span>
                </div>
                <div className="app-sidebar__sync-card">
                  <div className="app-sidebar__sync-card-head">Last sync (IST)</div>
                  <p className="app-sidebar__sync-time">
                    {formatPortalLastRunAt(portalLinkRow?.last_agent_run_at_utc)}
                  </p>
                  <button
                    type="button"
                    className="app-btn app-btn--sidebar-sync"
                    disabled={stateButtonsDisabled}
                    onClick={() => void runAgent()}
                  >
                    {agentLoading ? 'Running…' : 'Sync now'}
                  </button>
                </div>
              </div>
            ) : null}
            {linkedPortalLinks.length === 0 ? (
              <p className="app-sidebar__footer-hint">Add a portal on State URLs.</p>
            ) : null}
          </div>
          <p className="app-sidebar__legal">© 2026 Centene Corporation</p>
        </aside>

        <main
          className="app-main"
          aria-busy={activeNav === 'schedules' && agentLoading ? 'true' : 'false'}
        >
          {activeNav === 'mapping' ? (
            <>
              <header className="app-main__header">
                <h2 className="app-main__title">Column mapping</h2>
              </header>

              {selectedStateCode ? (
                <>
                  <section
                  className="app-card app-mapping-inventory-card"
                  aria-labelledby="mapping-saved-title"
                  aria-busy={mappingSavedMappingsLoading ? 'true' : 'false'}
                >
                  <div className="app-card__head app-card__head--tight app-mapping-inventory-card__head">
                    <h3 id="mapping-saved-title" className="app-card__title">
                      Saved mappings
                    </h3>
                    <button
                      type="button"
                      className="app-btn app-btn--primary app-btn--sm"
                      disabled={!selectedStateCode}
                      onClick={openMappingComposerModalForAdd}
                    >
                      Add mapping
                    </button>
                  </div>
                  {mappingSavedMappingsError ? (
                    <p className="app-error" role="alert">
                      {mappingSavedMappingsError}
                    </p>
                  ) : null}
                  {mappingSavedMappings.length > 0 ? (
                    <div className="app-table-scroll">
                      <table className="app-data-table app-mapping-inventory-table">
                        <thead>
                          <tr>
                            <th scope="col">Schedule</th>
                            <th scope="col">DST fee schedule</th>
                            <th scope="col">Pairs</th>
                            <th scope="col">Updated</th>
                            <th scope="col" className="app-mapping-inventory-table__actions">
                              Actions
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {mappingSavedMappingsSorted.map((row) => {
                            const mid = Number(row.mapping_id)
                            const busy = mappingDeleteBusyId === mid
                            return (
                              <tr key={`map-saved-${mid}`}>
                                <td>
                                  <span className="app-mapping-inventory-table__sched">
                                    {String(row.schedule_label || '').trim() || '—'}
                                  </span>
                                </td>
                                <td>
                                  <code className="app-code-inline">{String(row.dst_fsname || '').trim() || '—'}</code>
                                </td>
                                <td>{Number(row.paired_column_count) || 0}</td>
                                <td className="app-muted">{formatPortalLastRunAt(row.updated_at_utc)}</td>
                                <td className="app-mapping-inventory-table__actions">
                                  <div className="app-mapping-inventory-actions">
                                    <button
                                      type="button"
                                      className="app-btn app-btn--secondary app-btn--sm"
                                      disabled={busy}
                                      onClick={() => void openMappingShowModalForRow(mid)}
                                    >
                                      Show
                                    </button>
                                    <button
                                      type="button"
                                      className="app-btn app-btn--secondary app-btn--sm"
                                      disabled={busy}
                                      onClick={() => void loadMappingComposerFromRow(mid)}
                                    >
                                      Edit
                                    </button>
                                    <button
                                      type="button"
                                      className="app-btn app-btn--secondary app-btn--sm"
                                      disabled={busy}
                                      onClick={() => void deleteMappingSavedRow(mid)}
                                    >
                                      {busy ? '…' : 'Delete'}
                                    </button>
                                  </div>
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  ) : null}
                </section>

                <section
                  className="app-card app-mapping-bulk-import-card"
                  aria-labelledby="mapping-bulk-import-title"
                  aria-busy={mappingBulkBusy ? 'true' : 'false'}
                >
                  <div className="app-card__head app-card__head--tight">
                    <h3 id="mapping-bulk-import-title" className="app-card__title">
                      Bulk import
                    </h3>
                  </div>
                  <p className="app-muted app-mapping-bulk-import-hint">
                    Upload CSV or Excel (first sheet). One row per column pair; rows with the same{' '}
                    <code className="app-code-inline">StateSchedule</code> +{' '}
                    <code className="app-code-inline">DstSchedule</code> become one saved mapping (schedule family —
                    all future file versions reuse it). Required:{' '}
                    <code className="app-code-inline">StateSchedule</code> (fee schedule name, e.g.{' '}
                    <code className="app-code-inline">Physician Assistant</code>),{' '}
                    <code className="app-code-inline">DstSchedule</code> (DST fsname),{' '}
                    <code className="app-code-inline">StateColumn</code>,{' '}
                    <code className="app-code-inline">DstColumn</code>. Optional{' '}
                    <code className="app-code-inline">Action</code> (
                    <code className="app-code-inline">replace</code> or{' '}
                    <code className="app-code-inline">merge</code>). Use{' '}
                    <code className="app-code-inline">ArtifactId</code> only when the name is ambiguous.
                  </p>
                  {mappingBulkScheduleNamesLoading ? (
                    <p className="app-muted app-mapping-bulk-import-hint">Loading schedule names…</p>
                  ) : mappingBulkScheduleNames.length > 0 ? (
                    <details className="app-mapping-bulk-schedule-names">
                      <summary className="app-muted">
                        {mappingBulkScheduleNames.length} schedule names accepted for{' '}
                        {stateNameFromCode(selectedStateCode)} in{' '}
                        <code className="app-code-inline">StateSchedule</code>
                      </summary>
                      <ul className="app-mapping-bulk-schedule-names__list">
                        {mappingBulkScheduleNames.slice(0, 40).map((s) => (
                          <li key={String(s.logical_schedule_key || s.schedule_name)}>
                            <code className="app-code-inline">{String(s.schedule_name || '').trim()}</code>
                          </li>
                        ))}
                        {mappingBulkScheduleNames.length > 40 ? (
                          <li className="app-muted">…and {mappingBulkScheduleNames.length - 40} more</li>
                        ) : null}
                      </ul>
                    </details>
                  ) : null}
                  <form className="app-mapping-bulk-import-form" onSubmit={(e) => void submitMappingBulkImport(e)}>
                    <div className="app-mapping-bulk-import-row">
                      <input
                        ref={mappingBulkFileInputRef}
                        type="file"
                        name="mapping_bulk_file"
                        accept=".csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv"
                        className="app-input"
                        disabled={mappingBulkBusy}
                      />
                    </div>
                    <div className="app-mapping-bulk-import-row app-mapping-bulk-import-actions">
                      <label className="app-field-label app-field-label--inline">
                        <input
                          type="checkbox"
                          checked={mappingBulkDryRun}
                          disabled={mappingBulkBusy}
                          onChange={(e) => setMappingBulkDryRun(e.target.checked)}
                        />{' '}
                        Dry run (validate only)
                      </label>
                      <button type="submit" className="app-btn app-btn--primary" disabled={mappingBulkBusy}>
                        {mappingBulkBusy ? 'Working…' : 'Run import'}
                      </button>
                    </div>
                  </form>
                  {mappingBulkClientError ? (
                    <p className="app-error" role="alert">
                      {mappingBulkClientError}
                    </p>
                  ) : null}
                  {mappingBulkApiResult ? (
                    <div className="app-mapping-bulk-import-result" role="status">
                      {mappingBulkApiResult.error ? (
                        <p className="app-error">{String(mappingBulkApiResult.error)}</p>
                      ) : null}
                      {mappingBulkApiResult.ok ? (
                        <p className={mappingBulkApiResult.dry_run ? 'app-muted' : undefined}>
                          {mappingBulkApiResult.dry_run
                            ? `Dry run OK — ${(mappingBulkApiResult.applied || []).length} group(s) valid.`
                            : `Saved ${(mappingBulkApiResult.applied || []).length} mapping group(s).`}
                        </p>
                      ) : !mappingBulkApiResult.error ? (
                        <p className="app-error">Import finished with issues — see details below.</p>
                      ) : null}
                      {Array.isArray(mappingBulkApiResult.warnings) && mappingBulkApiResult.warnings.length > 0 ? (
                        <div className="app-mapping-bulk-import-list">
                          <strong>Warnings</strong>
                          <ul>
                            {mappingBulkApiResult.warnings.map((w, i) => (
                              <li key={`mw-${i}`}>
                                {w?.row != null ? `Row ${w.row}: ` : ''}
                                {String(w?.message || w)}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {Array.isArray(mappingBulkApiResult.errors) && mappingBulkApiResult.errors.length > 0 ? (
                        <div className="app-mapping-bulk-import-list">
                          <strong>Errors</strong>
                          <ul>
                            {mappingBulkApiResult.errors.map((w, i) => (
                              <li key={`me-${i}`}>
                                {w?.row != null ? `Row ${w.row}: ` : ''}
                                {String(w?.message || w)}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {Array.isArray(mappingBulkApiResult.applied) && mappingBulkApiResult.applied.length > 0 ? (
                        <div className="app-mapping-bulk-import-list">
                          <strong>Applied</strong>
                          <ul>
                            {mappingBulkApiResult.applied.map((a, i) => (
                              <li key={`ma-${i}`}>
                                <strong>{String(a.schedule_name || 'Schedule').trim()}</strong>
                                {' → '}
                                <code className="app-code-inline">{String(a.dst_fsname || '')}</code>
                                {' — '}
                                {Number(a.pairs) || 0} pair(s)
                                {a.dry_run ? ' (dry run)' : a.mapping_id != null ? ` (mapping #${a.mapping_id})` : ''}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </section>
                </>
              ) : null}

              {selectedStateCode && mappingMappingLoadError && !mappingComposerModalOpen ? (
                <p className="app-error" role="alert">
                  {mappingMappingLoadError}
                </p>
              ) : null}

              {mappingComposerModalOpen && selectedStateCode ? (
                <div
                  className="app-modal-backdrop"
                  role="presentation"
                  onClick={() => closeMappingComposerModal()}
                >
                  <div
                    className="app-modal app-modal--mapping-composer"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="mapping-composer-title"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <div className="app-modal__head">
                      <h2 id="mapping-composer-title" className="app-modal__title">
                        {mappingActiveMappingId != null ? 'Edit column mapping' : 'New column mapping'}
                      </h2>
                      <button
                        type="button"
                        className="app-modal__close"
                        aria-label="Close"
                        onClick={() => closeMappingComposerModal()}
                      >
                        ×
                      </button>
                    </div>
                    <div className="app-modal__body app-modal__body--mapping-composer">
                      {mappingMappingLoadError ? (
                        <p className="app-error" role="alert">
                          {mappingMappingLoadError}
                        </p>
                      ) : null}

                      <section
                        className="app-card app-mapping-unified-card"
                        aria-labelledby="mapping-unified-title"
                        aria-busy={
                          Boolean(
                            mappingArtifactIdFromKey &&
                              (mappingStateColumnsLoading || mappingDstColumnsLoading) &&
                              (mappingStateColumns.length === 0 || mappingDstColumns.length === 0),
                          )
                            ? true
                            : undefined
                        }
                      >
                        <div className="app-card__head app-card__head--tight app-mapping-unified-card__head">
                          <h3 id="mapping-unified-title" className="app-card__title">
                            Column mapping
                          </h3>
                        </div>

                        <div className="app-mapping-unified-fields">
                          <div className="app-mapping-unified-field">
                            {mappingActiveMappingId != null ? (
                              <>
                                <div className="app-field-label" id="mapping-state-fee-readonly-label">
                                  State fee schedule
                                </div>
                                <div
                                  className="app-mapping-readonly-value"
                                  aria-labelledby="mapping-state-fee-readonly-label"
                                >
                                  {mappingComposerStateFeeReadOnlyLabel || '—'}
                                </div>
                              </>
                            ) : (
                              <>
                                <label className="app-field-label" htmlFor="mapping-state-fee-pick">
                                  State fee schedule
                                </label>
                                <select
                                  id="mapping-state-fee-pick"
                                  className="app-select app-select--lg"
                                  value={mappingStateFeeSelectValue}
                                  disabled={!selectedStateCode || stateArtifactsLoading}
                                  onChange={(e) => {
                                    mappingArtifactAutoLoadRef.current = true
                                    setMappingStateFeeKey(e.target.value)
                                  }}
                                >
                                  <option value="">—</option>
                                  {mappingStateFeePickRows.map((o) => (
                                    <option key={o.key} value={o.key}>
                                      {o.label}
                                    </option>
                                  ))}
                                </select>
                              </>
                            )}
                          </div>
                          <div className="app-mapping-unified-field">
                            <label className="app-field-label" htmlFor="mapping-dst-table-pick">
                              DST fee schedule
                            </label>
                            <select
                              id="mapping-dst-table-pick"
                              className="app-select app-select--lg"
                              value={mappingDstTable}
                              disabled={!selectedStateCode || dstFeeSchedulesLoading}
                              onChange={(e) => setMappingDstTable(e.target.value)}
                            >
                              <option value="">—</option>
                              {dstFeeSchedules.map((fs) => (
                                <option key={`map-dst-${fs}`} value={fs}>
                                  {fs}
                                </option>
                              ))}
                            </select>
                          </div>
                        </div>

                        {mappingNewFlowDuplicateBlock ? (
                          <p className="app-error" role="alert">
                            A mapping already exists for this fee schedule with DST{' '}
                            <code className="app-code-inline">{(mappingDstTable || '').trim()}</code>.
                          </p>
                        ) : null}

                        {stateArtifactsError ? (
                          <p className="app-error" role="alert">
                            {stateArtifactsError}
                          </p>
                        ) : null}
                        {dstFeeSchedulesError ? (
                          <p className="app-error" role="alert">
                            {dstFeeSchedulesError}
                          </p>
                        ) : null}

                        {mappingStateColumnsError ? (
                          <p className="app-error" role="alert">
                            {mappingStateColumnsError}
                          </p>
                        ) : null}
                        {mappingDstColumnsError ? (
                          <p className="app-error" role="alert">
                            {mappingDstColumnsError}
                          </p>
                        ) : null}

                        {mappingPersistError ? (
                          <p className="app-error" role="alert">
                            {mappingPersistError}
                          </p>
                        ) : null}

                        {mappingArtifactIdFromKey &&
                        mappingStateColumns.length > 0 &&
                        mappingDstColumns.length > 0 ? (
                          <div className="app-mapping-pairs-table-wrap app-mapping-pairs-table-wrap--unified">
                            <table
                              className={
                                mappingCommittedSnapshot
                                  ? 'app-mapping-pairs-table app-mapping-pairs-table--with-actions'
                                  : 'app-mapping-pairs-table'
                              }
                            >
                              <thead>
                                <tr>
                                  <th scope="col">State column</th>
                                  <th
                                    scope="col"
                                    className="app-mapping-pairs-table__arrow-col"
                                    aria-label="Maps to"
                                  >
                                    →
                                  </th>
                                  <th scope="col">DST column</th>
                                  {mappingCommittedSnapshot ? (
                                    <th scope="col" className="app-mapping-pairs-table__actions-col">
                                      Actions
                                    </th>
                                  ) : null}
                                </tr>
                              </thead>
                              <tbody>
                                {mappingStateColumns.map((stCol) => {
                                  const picked =
                                    (mappingColumnPairs[stCol] && String(mappingColumnPairs[stCol]).trim()) || ''
                                  const orphanPick =
                                    Boolean(picked) && !mappingDstColumns.some((dc) => dc === picked)
                                  return (
                                    <tr key={`pair-${stCol}`}>
                                      <td className="app-mapping-pairs-col app-mapping-pairs-col--state">
                                        <span className="app-mapping-pairs-cell-text">{stCol}</span>
                                      </td>
                                      <td className="app-mapping-pairs-table__arrow-col" aria-hidden="true">
                                        <span className="app-mapping-pairs-arrow">→</span>
                                      </td>
                                      <td className="app-mapping-pairs-col app-mapping-pairs-col--dst">
                                        <select
                                          className="app-select app-select--compact"
                                          aria-label={`DST column for ${stCol}`}
                                          value={picked || ''}
                                          onChange={(e) => {
                                            const v = e.target.value.trim()
                                            setMappingColumnPairs((prev) => {
                                              const next = { ...prev }
                                              if (!v) delete next[stCol]
                                              else next[stCol] = v
                                              return next
                                            })
                                          }}
                                        >
                                          <option value="">Not mapped</option>
                                          {orphanPick ? (
                                            <option value={picked} title={picked}>
                                              {formatMappingDbColumnDisplay(picked)} (saved — not in sample)
                                            </option>
                                          ) : null}
                                          {mappingDstColumns.map((dc) => (
                                            <option key={`opt-${stCol}-${dc}`} value={dc} title={dc}>
                                              {formatMappingDbColumnDisplay(dc)}
                                            </option>
                                          ))}
                                        </select>
                                      </td>
                                      {mappingCommittedSnapshot ? (
                                        <td className="app-mapping-pairs-table__actions-col">
                                          {picked ? (
                                            <button
                                              type="button"
                                              className="app-btn app-btn--secondary app-btn--sm app-mapping-pairs-rm-btn"
                                              title={`Remove pairing for "${stCol}"`}
                                              aria-label={`Remove pairing for ${stCol}`}
                                              onClick={() => {
                                                setMappingColumnPairs((prev) => {
                                                  const next = { ...prev }
                                                  delete next[stCol]
                                                  return next
                                                })
                                              }}
                                            >
                                              Delete
                                            </button>
                                          ) : (
                                            <span className="app-muted app-mapping-pairs-dash">—</span>
                                          )}
                                        </td>
                                      ) : null}
                                    </tr>
                                  )
                                })}
                              </tbody>
                            </table>
                          </div>
                        ) : null}

                        <div className="app-mapping-unified-footer">
                          <button
                            type="button"
                            className="app-btn app-btn--secondary"
                            onClick={closeMappingComposerModal}
                            disabled={mappingPersistLoading}
                          >
                            Cancel
                          </button>
                          <button
                            type="button"
                            className="app-btn app-btn--primary"
                            onClick={() => void handleMappingAccept()}
                            disabled={!mappingUiCanSave}
                          >
                            {mappingPersistLoading ? 'Saving…' : 'Save mapping'}
                          </button>
                        </div>
                      </section>

                    </div>
                  </div>
                </div>
              ) : null}

              {mappingShowModal ? (
                <div
                  className="app-modal-backdrop"
                  role="presentation"
                  onClick={() => setMappingShowModal(null)}
                >
                  <div
                    className="app-modal app-modal--mapping-view"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="mapping-view-title"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <div className="app-modal__head">
                      <h2 id="mapping-view-title" className="app-modal__title">
                        Column mapping detail
                      </h2>
                      <button
                        type="button"
                        className="app-modal__close"
                        aria-label="Close"
                        onClick={() => setMappingShowModal(null)}
                      >
                        ×
                      </button>
                    </div>
                    <div className="app-modal__body">
                      {mappingShowModal.loading ? (
                        <div className="app-mapping-inline-busy" aria-busy="true" />
                      ) : mappingShowModal.error ? (
                        <p className="app-error" role="alert">
                          {mappingShowModal.error}
                        </p>
                      ) : mappingShowModal.detail ? (
                        <div className="app-mapping-pairs-table-wrap">
                          <table className="app-mapping-pairs-table">
                            <thead>
                              <tr>
                                <th scope="col">State column</th>
                                <th
                                  scope="col"
                                  className="app-mapping-pairs-table__arrow-col"
                                  aria-label="Maps to"
                                >
                                  →
                                </th>
                                <th scope="col">DST column</th>
                              </tr>
                            </thead>
                            <tbody>
                              {Object.entries(mappingShowModal.detail.pairs)
                                .filter(([sk, dk]) => String(sk).trim() && String(dk).trim())
                                .sort(([a], [b]) => String(a).localeCompare(String(b)))
                                .map(([stCol, dstCol]) => (
                                  <tr key={`view-${stCol}`}>
                                    <td className="app-mapping-pairs-col app-mapping-pairs-col--state">
                                      <span className="app-mapping-pairs-cell-text">{stCol}</span>
                                    </td>
                                    <td className="app-mapping-pairs-table__arrow-col" aria-hidden="true">
                                      <span className="app-mapping-pairs-arrow">→</span>
                                    </td>
                                    <td className="app-mapping-pairs-col app-mapping-pairs-col--dst">
                                      <span
                                        className="app-mapping-pairs-cell-text app-mapping-pairs-dst-val"
                                        title={dstCol}
                                      >
                                        {formatMappingDbColumnDisplay(dstCol)}
                                      </span>
                                    </td>
                                  </tr>
                                ))}
                            </tbody>
                          </table>
                        </div>
                      ) : null}
                    </div>
                  </div>
                </div>
              ) : null}
            </>
          ) : activeNav === 'notifications' ? (
            <>
              <header className="app-main__header app-main__header--notif-only">
                <h2 className="app-main__title">Notifications</h2>
              </header>

              <div className="app-notif-page">
                {!selectedStateCode ? (
                  <p className="app-muted">Select a state in the sidebar to manage notification teams.</p>
                ) : (
                  <>
                    <div className="app-notif-toolbar">
                      <button
                        type="button"
                        className="app-btn app-btn--primary"
                        disabled={notifContactsLoading}
                        onClick={() => openAddNotificationTeamModal()}
                      >
                        Add new team
                      </button>
                      <button
                        type="button"
                        className="app-btn app-btn--secondary"
                        disabled={notifContactsLoading}
                        onClick={() => setNotifContactsTick((t) => t + 1)}
                      >
                        {notifContactsLoading ? 'Refreshing…' : 'Refresh'}
                      </button>
                    </div>

                    <section
                      className="app-notif-list"
                      aria-labelledby="notif-list-heading"
                      aria-busy={notifContactsLoading ? 'true' : 'false'}
                    >
                      <div className="app-notif-list__intro">
                        <h3 id="notif-list-heading" className="app-notif-list__title">
                          Notification teams
                        </h3>
                        <p className="app-notif-list__scope app-muted">
                          <strong>{stateNameFromCode(selectedStateCode)}</strong> ({selectedStateCode}) — recipients subscribed for
                          this state
                        </p>
                      </div>

                      {notifContactsError ? (
                        <p className="app-error app-notif-list__banner" role="alert">
                          {notifContactsError}
                        </p>
                      ) : null}

                      {!notifContactsError && notifContactsLoading ? (
                        <p className="app-muted app-notif-list__banner" role="status">
                          Loading notification teams…
                        </p>
                      ) : null}

                      {!notifContactsError && !notifContactsLoading && notifContacts.length === 0 ? (
                        <p className="app-muted app-notif-list__banner" role="status">
                          No teams added yet. Use <strong>Add new team</strong> to subscribe recipients for this state.
                        </p>
                      ) : null}

                      {!notifContactsLoading && !notifContactsError && notifContacts.length > 0 ? (
                        <div className="app-table-scroll app-notif-list__scroll">
                          <table className="app-data-table app-data-table--catalog">
                            <thead>
                              <tr>
                                <th scope="col">Recipient name</th>
                                <th scope="col">Email</th>
                                <th scope="col">Team</th>
                                <th scope="col">Department</th>
                                <th scope="col">State changes</th>
                                <th scope="col">Compared results</th>
                                <th scope="col">Actions</th>
                              </tr>
                            </thead>
                            <tbody>
                              {notifContacts.map((row) => {
                                const nid = Number(row.notification_contact_id)
                                const busyDel = notifDeleteBusyId === nid
                                return (
                                  <tr key={`notif-${nid}`}>
                                    <td>{String(row.contact_name || '').trim() || '—'}</td>
                                    <td>
                                      <code className="app-code-inline">{String(row.email || '').trim() || '—'}</code>
                                    </td>
                                    <td>{String(row.team_name || '').trim() || '—'}</td>
                                    <td>{String(row.department_name || '').trim() || '—'}</td>
                                    <td>{formatCellValue(row.notify_new_state_file)}</td>
                                    <td>{formatCellValue(row.notify_compare_result)}</td>
                                    <td>
                                      <div className="app-notif-row-actions">
                                        <button
                                          type="button"
                                          className="app-btn app-btn--secondary app-btn--sm"
                                          disabled={busyDel || notifSaving}
                                          onClick={() => editNotificationContactRow(row)}
                                        >
                                          Edit
                                        </button>
                                        <button
                                          type="button"
                                          className="app-btn app-btn--secondary app-btn--sm"
                                          disabled={busyDel || notifSaving}
                                          onClick={() => void deleteNotificationContactRow(nid)}
                                        >
                                          {busyDel ? '…' : 'Remove'}
                                        </button>
                                      </div>
                                    </td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>
                      ) : null}
                    </section>
                  </>
                )}
              </div>

              {notifTeamModalOpen && selectedStateCode ? (
                <div className="app-modal-backdrop" role="presentation" onClick={() => cancelNotificationEdit()}>
                  <div
                    className="app-modal app-modal--notif-team"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="notif-team-modal-title"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <div className="app-modal__head">
                      <h2 id="notif-team-modal-title" className="app-modal__title">
                        {notifEditingId != null ? 'Edit notification team' : 'Add notification team'}
                      </h2>
                      <button
                        type="button"
                        className="app-modal__close"
                        aria-label="Close"
                        disabled={notifSaving}
                        onClick={() => cancelNotificationEdit()}
                      >
                        ×
                      </button>
                    </div>
                    <div className="app-modal__body">
                      <form
                        className="app-notif-form app-notif-form--modal"
                        onSubmit={(e) => void saveNotificationContact(e)}
                      >
                        <div className="app-notif-form__grid" aria-label="Team details">
                          <div className="app-notif-form__group">
                            <label className="app-field-label" htmlFor="notif-modal-name">
                              Recipient name
                            </label>
                            <input
                              id="notif-modal-name"
                              type="text"
                              className="app-input app-input--lg"
                              autoComplete="name"
                              value={notifFormName}
                              onChange={(e) => setNotifFormName(e.target.value)}
                              disabled={notifSaving}
                            />
                          </div>
                          <div className="app-notif-form__group">
                            <label className="app-field-label" htmlFor="notif-modal-email">
                              Email
                            </label>
                            <input
                              id="notif-modal-email"
                              type="email"
                              className="app-input app-input--lg"
                              autoComplete="email"
                              spellCheck={false}
                              value={notifFormEmail}
                              onChange={(e) => setNotifFormEmail(e.target.value)}
                              disabled={notifSaving}
                            />
                          </div>
                          <div className="app-notif-form__group">
                            <label className="app-field-label" htmlFor="notif-modal-team">
                              Team{' '}
                              <span className="app-muted" style={{ fontWeight: '400' }}>
                                (optional)
                              </span>
                            </label>
                            <input
                              id="notif-modal-team"
                              type="text"
                              className="app-input app-input--lg"
                              value={notifFormTeam}
                              onChange={(e) => setNotifFormTeam(e.target.value)}
                              disabled={notifSaving}
                            />
                          </div>
                          <div className="app-notif-form__group">
                            <label className="app-field-label" htmlFor="notif-modal-dept">
                              Department{' '}
                              <span className="app-muted" style={{ fontWeight: '400' }}>
                                (optional)
                              </span>
                            </label>
                            <input
                              id="notif-modal-dept"
                              type="text"
                              className="app-input app-input--lg"
                              value={notifFormDept}
                              onChange={(e) => setNotifFormDept(e.target.value)}
                              disabled={notifSaving}
                            />
                          </div>
                        </div>

                        <div
                          className={`app-notif-form__simple-prefs${notifSaving ? ' app-notif-form__simple-prefs--disabled' : ''}`}
                          role="group"
                          aria-label="Alert preferences"
                        >
                          <div className="app-notif-form__pref-row">
                            <span className="app-notif-form__pref-row-label" id="notif-modal-pref-state-lbl">
                              State changes
                            </span>
                            <label className="app-notif-form__switch app-notif-form__switch--row">
                              <input
                                type="checkbox"
                                className="app-notif-form__switch-input"
                                checked={notifFormNewFile}
                                disabled={notifSaving}
                                onChange={(e) => setNotifFormNewFile(e.target.checked)}
                                aria-labelledby="notif-modal-pref-state-lbl"
                              />
                              <span className="app-notif-form__switch-track" />
                            </label>
                          </div>
                          <div className="app-notif-form__pref-row">
                            <span className="app-notif-form__pref-row-label" id="notif-modal-pref-compare-lbl">
                              Compared results
                            </span>
                            <label className="app-notif-form__switch app-notif-form__switch--row">
                              <input
                                type="checkbox"
                                className="app-notif-form__switch-input"
                                checked={notifFormCompare}
                                disabled={notifSaving}
                                onChange={(e) => setNotifFormCompare(e.target.checked)}
                                aria-labelledby="notif-modal-pref-compare-lbl"
                              />
                              <span className="app-notif-form__switch-track" />
                            </label>
                          </div>
                        </div>

                        {notifFormError ? (
                          <p className="app-error app-notif-form__feedback" role="alert">
                            {notifFormError}
                          </p>
                        ) : null}

                        <div className="app-notif-modal__footer">
                          <button
                            type="button"
                            className="app-btn app-btn--secondary"
                            disabled={notifSaving}
                            onClick={() => cancelNotificationEdit()}
                          >
                            Cancel
                          </button>
                          <button
                            type="submit"
                            className="app-btn app-btn--primary"
                            disabled={notifSaving || !notifFormName.trim() || !notifFormEmail.trim()}
                          >
                            {notifSaving ? 'Saving…' : notifEditingId != null ? 'Save changes' : 'Save team'}
                          </button>
                        </div>
                      </form>
                    </div>
                  </div>
                </div>
              ) : null}
            </>
          ) : activeNav === 'scheduleVersions' ? (
            <>
              <header className="app-main__header">
                <h2 className="app-main__title">Schedule versions</h2>
                <p className="app-main__lede">
                  Every saved download for the selected state, grouped by <strong>logical fee schedule</strong>. Within each group,
                  rows are ordered by <strong>portal edition date</strong> when we have it (otherwise by fetch time)—newest at the
                  top, older editions below (so you can see Jan&nbsp;5 after an update and still find Jan&nbsp;1 as{' '}
                  <strong>Historical</strong>). Rows marked <strong>Latest</strong> match the Fee Schedules tab (one winner per
                  schedule family after each sync).
                </p>
              </header>

              <section className="app-card" aria-labelledby="sched-ver-all-heading">
                <div className="app-card__head" style={{ alignItems: 'center', flexWrap: 'wrap', gap: '0.75rem' }}>
                  <h3 id="sched-ver-all-heading" className="app-card__title">
                    Version history
                  </h3>
                  <button
                    type="button"
                    className="app-btn app-btn--secondary"
                    disabled={!selectedStateCode || stateArtifactHistoryLoading}
                    onClick={() => void refreshArtifactHistory()}
                  >
                    {stateArtifactHistoryLoading ? 'Refreshing…' : 'Refresh'}
                  </button>
                </div>

                {!selectedStateCode ? (
                  <p className="app-muted">Select a state in the sidebar.</p>
                ) : stateArtifactHistoryLoading ? (
                  <p className="app-muted" role="status">
                    Loading versions…
                  </p>
                ) : stateArtifactHistoryError ? (
                  <p className="app-error" role="alert">
                    {stateArtifactHistoryError}
                  </p>
                ) : scheduleVersionsTableRows.length === 0 ? (
                  <p className="app-muted" role="status">
                    No saved files for this state yet. Run <strong>Sync now</strong> from the sidebar.
                  </p>
                ) : (
                  <div className="app-table-scroll" style={{ marginTop: '0.75rem' }}>
                    <table className="app-data-table app-data-table--catalog">
                      <thead>
                        <tr>
                          <th>Fee schedule</th>
                          <th>Effective date</th>
                          <th>Date source</th>
                          <th>Superseded (hint)</th>
                          <th>Fetched</th>
                          <th>Status</th>
                          <th>Preview</th>
                          <th>Download</th>
                        </tr>
                      </thead>
                      <tbody>
                        {scheduleVersionsTableRows.map((a) => {
                          const aid = Number(a.artifact_id)
                          const sup =
                            a.is_superseded_hint === true ||
                            a.is_superseded_hint === 1 ||
                            a.is_superseded_hint === '1'
                          const src = (a.effective_date_source && String(a.effective_date_source).trim()) || '—'
                          return (
                            <tr key={aid}>
                              <td>{artifactFeeScheduleDisplayName(a)}</td>
                              <td>{formatPortalEffectiveDateShort(a.portal_effective_date)}</td>
                              <td>{src}</td>
                              <td>{sup ? 'Yes' : 'No'}</td>
                              <td>{formatArtifactFetchedAt(a.fetched_at_utc)}</td>
                              <td>
                                <span
                                  className={`app-badge ${artifactIsCurrent(a) ? 'app-badge--neutral' : 'app-badge--neutral'}`}
                                  title={artifactIsCurrent(a) ? 'Chosen by date-primary rules' : 'Older or superseded edition'}
                                >
                                  {artifactVersionStatusLabel(a)}
                                </span>
                              </td>
                              <td>
                                <button
                                  type="button"
                                  className="app-btn app-btn--small"
                                  disabled={!Number.isFinite(aid)}
                                  onClick={() => {
                                    setSchedulesStateFeeKey(`a:${aid}`)
                                    setSchedulesStatePreviewId(aid)
                                    setSchedulesStateFeeModalOpen(true)
                                  }}
                                >
                                  Preview
                                </button>
                              </td>
                              <td>
                                <button
                                  type="button"
                                  className="app-btn app-btn--small"
                                  onClick={() => {
                                    window.open(`${API_BASE}/app/artifacts/${aid}/file`, '_blank', 'noopener,noreferrer')
                                  }}
                                >
                                  Download
                                </button>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </>
          ) : activeNav === 'compare' ? (
            <>
              <header className="app-main__header">
                <h2 className="app-main__title">Compare Fee Schedules</h2>
                <p className="app-main__lede">Upload one state file and one DST file, then start comparison.</p>
              </header>

              <section className="app-card" aria-labelledby="compare-upload-heading">
                <div className="app-card__head">
                  <h3 id="compare-upload-heading" className="app-card__title">
                    Step 1: Upload Files
                  </h3>
                </div>

                <div className="app-upload-grid">
                  <div className="app-upload-field">
                    <label className="app-field-label" htmlFor="state-file-upload">
                      State file
                    </label>
                    <input
                      id="state-file-upload"
                      className="app-file-input"
                      type="file"
                      accept=".csv,.xlsx,.xls,.json"
                      onChange={(e) => {
                        setStateUploadFile(e.target.files?.[0] ?? null)
                        setCompareMessage('')
                      }}
                    />
                    <p className="app-file-name">{stateUploadFile?.name ?? 'No file chosen'}</p>
                  </div>

                  <div className="app-upload-field">
                    <label className="app-field-label" htmlFor="dst-file-upload">
                      DST file
                    </label>
                    <input
                      id="dst-file-upload"
                      className="app-file-input"
                      type="file"
                      accept=".csv,.xlsx,.xls,.json"
                      onChange={(e) => {
                        setDstUploadFile(e.target.files?.[0] ?? null)
                        setCompareMessage('')
                      }}
                    />
                    <p className="app-file-name">{dstUploadFile?.name ?? 'No file chosen'}</p>
                  </div>
                </div>

                {compareMessage ? <p className="app-muted">{compareMessage}</p> : null}

                <div className="app-card__actions">
                  <button type="button" className="app-btn app-btn--primary" disabled={compareDisabled} onClick={runCompare}>
                    Compare
                  </button>
                </div>
              </section>
            </>
          ) : activeNav === 'stateUrls' ? (
            <>
              <header className="app-main__header">
                <h2 className="app-main__title">State portal URLs</h2>
                <p className="app-main__lede">
                  One saved fee-schedule page URL per state (stored as a 2-letter code: GA, NY, NC, …).{' '}
                  <strong>Sync now</strong> in the sidebar uses the saved URL when the override field on Fee Schedules is empty. Choose any state
                  below to add or edit its portal; the sidebar only lists states that already have a link.
                </p>
              </header>

              <section className="app-card" aria-labelledby="state-urls-form-heading">
                <div className="app-card__head">
                  <h3 id="state-urls-form-heading" className="app-card__title">
                    Configure portal
                  </h3>
                  <span className="app-badge app-badge--neutral">App DB</span>
                </div>

                <label className="app-field-label" htmlFor="portal-editor-state">
                  State
                </label>
                <select
                  id="portal-editor-state"
                  className="app-select app-select--lg"
                  value={portalEditorStateCode}
                  onChange={(e) => {
                    setPortalEditorStateCode(e.target.value)
                    setStateUrlsMessage(null)
                  }}
                >
                  {US_STATES.map(({ code, name }) => (
                    <option key={code} value={code}>
                      {name} ({code})
                    </option>
                  ))}
                </select>

                {/* <label className="app-field-label" htmlFor="state-urls-label" style={{ marginTop: '0.75rem' }}>
                  Label (optional)
                </label> */}
                {/* <input
                  id="state-urls-label"
                  type="text"
                  className="app-input app-input--lg"
                  placeholder={`e.g. ${portalEditorStateCode ? stateNameFromCode(portalEditorStateCode) : 'State'} Medicaid fee schedules`}
                  value={urlFormLabel}
                  onChange={(e) => setUrlFormLabel(e.target.value)}
                /> */}

                <label className="app-field-label" htmlFor="state-urls-portal" style={{ marginTop: '0.75rem' }}>
                  Portal page URL
                </label>
                <input
                  id="state-urls-portal"
                  type="url"
                  className="app-input app-input--lg"
                  placeholder="https://…"
                  autoComplete="url"
                  spellCheck={false}
                  value={urlFormPortalUrl}
                  onChange={(e) => setUrlFormPortalUrl(e.target.value)}
                />

                {portalEditorLinkId != null ? (
                  <p className="app-muted app-card__hint" role="status">
                    Saved as link_id <code>{portalEditorLinkId}</code> for <strong>{portalEditorStateCode}</strong> (
                    {stateNameFromCode(portalEditorStateCode)}).
                  </p>
                ) : (
                  <p className="app-muted app-card__hint" role="status">
                    No row stored yet for <strong>{portalEditorStateCode}</strong>. Save to create one.
                  </p>
                )}

                {stateUrlsMessage ? (
                  <p
                    className={stateUrlsMessage.type === 'error' ? 'app-error' : 'app-muted'}
                    role={stateUrlsMessage.type === 'error' ? 'alert' : 'status'}
                    style={{ marginTop: '0.75rem' }}
                  >
                    {stateUrlsMessage.text}
                  </p>
                ) : null}

                <div className="app-card__actions" style={{ marginTop: '1rem' }}>
                  <button
                    type="button"
                    className="app-btn app-btn--primary"
                    disabled={stateUrlsSaving || !isValidHttpUrl(urlFormPortalUrl.trim())}
                    onClick={saveStatePortalUrl}
                  >
                    {stateUrlsSaving ? 'Saving…' : 'Save portal URL'}
                  </button>
                  <button
                    type="button"
                    className="app-btn app-btn--secondary"
                    disabled={stateUrlsSaving || portalEditorLinkId == null}
                    onClick={deleteStatePortalUrl}
                  >
                    Remove for this state
                  </button>
                </div>
              </section>
            </>
          ) : activeNav === 'dst' ? (
            <>
              <header className="app-main__header">
                <h2 className="app-main__title">DST (SSMS)</h2>
                <p className="app-main__lede">
                  Load user tables from the <strong>DST</strong> database (<code>dbo</code>). Rows exclude{' '}
                  <code>dst_row_id</code>, <code>state_code</code>, and <code>inserted_at</code>. The <code>data_json</code>{' '}
                  column is flattened: JSON keys stay exactly as in the source, and nested{' '}
                  <code>FROM</code> wrappers are unwrapped to plain values.
                </p>
              </header>

              <section className="app-card" aria-labelledby="dst-table-heading">
                <div className="app-card__head">
                  <h3 id="dst-table-heading" className="app-card__title">
                    SQL Server tables
                  </h3>
                </div>

                <label className="app-field-label" htmlFor="dst-table-select">
                  Table
                </label>
                <select
                  id="dst-table-select"
                  className="app-input app-input--lg"
                  disabled={dstTablesLoading}
                  value={dstSelectedTable}
                  onChange={(e) => setDstSelectedTable(e.target.value)}
                >
                  <option value="">
                    {dstTablesLoading ? 'Loading tables…' : dstTables.length ? 'Choose a table' : 'No tables available'}
                  </option>
                  {dstTables.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>

                <p className="app-muted app-card__hint">
                  Configure <code>MSSQL_SERVER</code> / <code>MSSQL_DATABASE=DST</code> or a full <code>MSSQL_ODBC_CONN</code>{' '}
                  string in the backend environment, then restart the API.
                </p>

                {dstTablesError ? (
                  <p className="app-muted" style={{ color: '#c0392b', marginTop: '0.75rem' }} role="alert">
                    {dstTablesError}
                  </p>
                ) : null}
                {dstRowsError ? (
                  <p className="app-muted" style={{ color: '#c0392b', marginTop: '0.75rem' }} role="alert">
                    {dstRowsError}
                  </p>
                ) : null}

                {dstSelectedTable ? (
                  <p className="app-muted app-catalog-meta" style={{ marginTop: '0.75rem' }}>
                    {dstRowsLoading
                      ? 'Loading rows…'
                      : `${dstRows.length} row${dstRows.length === 1 ? '' : 's'} · ${dstColumns.length} columns`}
                  </p>
                ) : null}

                {dstColumns.length > 0 ? (
                  <div className="app-table-scroll" style={{ marginTop: '1rem' }}>
                    <table className="app-data-table app-data-table--catalog">
                      <thead>
                        <tr>
                          {dstColumns.map((col) => (
                            <th key={col} title={typeof col === 'string' ? col : String(col)}>
                              {col}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {dstExplorerRowsForPreview.map((row, ri) => (
                          <tr key={`dst-row-${ri}`}>
                            {dstColumns.map((col) => (
                              <td key={`dst-${ri}-${col}`}>{formatFeeSchedulePreviewCell(col, row[col])}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : null}
              </section>
            </>
          ) : activeNav !== 'schedules' ? (
            <section className="app-panel app-placeholder">
              <h2 className="app-main__title">{NAV.find((n) => n.id === activeNav)?.label}</h2>
              <p className="app-muted">
                This area is reserved for the next workflow. Fee schedule ingestion runs under <strong>Fee Schedules</strong>.
              </p>
            </section>
          ) : (
            <>
                <header className="app-main__header app-main__header--schedules-tight">
                  <div>
                    <h2 className="app-main__title">{pageTitle}</h2>
                    <p className="app-main__lede app-main__lede--schedules-hub">
                      Fetch, map, and compare fee schedules across state and DST systems.
                    </p>
                  </div>
                </header>

                {agentError ? (
                  <p className="app-error app-schedules-banner" role="alert">
                    {agentError}
                  </p>
                ) : null}

                {companionHealth && !companionHealth.app_database_configured ? (
                  <p className="app-error app-schedules-banner" role="alert">
                    <strong>No files are being saved from Sync now.</strong> The UI already sends{' '}
                    <code>persist_artifacts: true</code>, but the API only persists downloads when the companion app
                    database is configured (<code>MSSQL_APP_DATABASE</code> or <code>MSSQL_APP_ODBC_CONN</code>).
                    Set that in the API environment, restart the backend, then run sync again from the sidebar.
                  </p>
                ) : null}

                {lastRunPersistSummary ? (
                  <p className="app-muted app-schedules-banner" role="status">
                    {lastRunPersistSummary}
                  </p>
                ) : null}

                <section className="app-sched-overview" aria-label="State overview">
                  <div className="app-sched-overview__grid">
                    <OverviewMetricCard
                      accent="violet"
                      title="Saved fee schedules"
                      value={schedulesDashboardMetrics.feeScheduleCountDisplay}
                      subtitle="Fetched for active state"
                    >
                      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path
                          d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z"
                          stroke="currentColor"
                          strokeWidth="1.75"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                        <path d="M14 2v6h6M16 13H8M16 17H8M10 9H8" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    </OverviewMetricCard>
                    <OverviewMetricCard
                      accent="mint"
                      title="Comparisons"
                      value={schedulesDashboardMetrics.compareCountDisplay}
                      subtitle="Run from Fee Schedules (this browser)"
                    >
                      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path
                          d="M3 3v18h18M7 14l4-4 4 4 4-6"
                          stroke="currentColor"
                          strokeWidth="1.75"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </OverviewMetricCard>
                    <OverviewMetricCard
                      accent="amber"
                      title="Latest compared"
                      value={schedulesDashboardMetrics.latestMain}
                      subtitle={schedulesDashboardMetrics.latestSub}
                    >
                      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path
                          d="M12 3v18M7 12l5 5 5-5M5 21h14"
                          stroke="currentColor"
                          strokeWidth="1.75"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </OverviewMetricCard>
                    <OverviewMetricCard
                      accent="sky"
                      title="Last sync"
                      value={schedulesDashboardMetrics.lastSyncMain}
                      subtitle={schedulesDashboardMetrics.lastSyncSub}
                    >
                      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.75" />
                        <path d="M12 8v5l3 2" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
                      </svg>
                    </OverviewMetricCard>
                  </div>
                </section>

                <div className="app-fee-schedules-layout">
                  <div className="app-grid-fee-cards">
                  <section className="app-card" aria-label="State agency fee files">
                    {selectedStateCode ? (
                      <div className="app-card__state-meta app-card__state-meta--solo" style={{ marginTop: 0 }}>
                        <p className="app-card__state-heading">{stateNameFromCode(selectedStateCode)}</p>
                      </div>
                    ) : (
                      <p className="app-muted app-card__state-meta" style={{ marginTop: 0 }}>
                        Select a state in the sidebar to load this card.
                      </p>
                    )}

                    {selectedStateCode ? (
                      <>
                        <label className="app-field-label" htmlFor="sched-fee-pick-state" style={{ marginTop: '0.85rem' }}>
                          Fee schedule / file
                        </label>
                        <p className="app-muted app-schedules-count" role="status">
                          {agentLoading
                            ? 'Fetching schedules from the portal…'
                            : stateArtifactsLoading
                              ? 'Loading saved fee schedules…'
                              : `${stateFeePickRows.length} saved fee schedule${stateFeePickRows.length === 1 ? '' : 's'}`}
                        </p>
                        <select
                          id="sched-fee-pick-state"
                          className="app-select app-select--lg"
                          value={stateFeeSelectValue}
                          disabled={!selectedStateCode || agentLoading || stateArtifactsLoading}
                          onChange={(e) => setSchedulesStateFeeKey(e.target.value)}
                        >
                          <option value="">
                            {agentLoading
                              ? 'Run in progress…'
                              : stateArtifactsLoading
                                ? 'Loading…'
                              : stateFeePickRows.length
                                ? 'Choose a fee schedule…'
                                : 'No saved files — use Sync now in the sidebar'}
                          </option>
                          {stateFeePickRows.map((o) => (
                            <option key={o.key} value={o.key}>
                              {o.label}
                            </option>
                          ))}
                        </select>
                      </>
                    ) : null}

                    {stateArtifactsError ? (
                      <p className="app-error" role="alert" style={{ marginTop: '0.5rem' }}>
                        {stateArtifactsError}
                      </p>
                    ) : null}

                    {!agentLoading && selectedStateCode && !stateArtifactsLoading && stateFeePickRows.length === 0 ? (
                      <p className="app-muted" style={{ marginTop: '0.5rem' }} role="status">
                        No fee files are stored for this state yet. Use <strong>Sync now</strong> in the sidebar to scan the portal and save downloads.
                      </p>
                    ) : null}

                    {selectedStateCode ? (
                    <div className="app-card__actions">
                      <button
                        type="button"
                        className="app-btn"
                        disabled={!stateFeeSelectValue || !statePickPortalActions.preview}
                        onClick={() => {
                          const row = selectedStateFeePickRow
                          if (!row) return
                          if (row.catalogTableIndex != null && row.catalogRowIndex != null && agentResult?.catalog_tables) {
                            const t = agentResult.catalog_tables[row.catalogTableIndex]
                            const dataRow = Array.isArray(t?.rows) ? t.rows[row.catalogRowIndex] : null
                            if (!dataRow) return
                            const cols = getTableColumns(t)
                            const prim = primaryCatalogRowLink(dataRow, cols)
                            if (!prim) return
                            const refUrl = agentResult?.resolved_url || agentUrl.trim() || null
                            const sid = agentResult?.preview_auth?.session_id || null
                            if (prim.portal) {
                              void requestPreview({
                                kind: 'portal',
                                title: row.label || 'Portal action',
                                sourcePageUrl: refUrl,
                              })
                            } else {
                              void requestPreview({
                                kind: 'http',
                                url: prim.url,
                                title: prim.docLabel || row.label || 'Fee file',
                                referrerUrl: refUrl,
                                previewSessionId: sid,
                                documentHint: previewDocumentHintFromLink(prim.url, prim.docLabel || row.label),
                              })
                            }
                          } else if (row.artifactId != null) {
                            setSchedulesStateError(null)
                            setSchedulesStateCols([])
                            setSchedulesStateRows([])
                            if (schedulesStatePdfRef.current) {
                              URL.revokeObjectURL(schedulesStatePdfRef.current)
                              schedulesStatePdfRef.current = ''
                            }
                            setSchedulesStatePdfUrl('')
                            setSchedulesStateLoading(true)
                            setSchedulesStatePreviewId(row.artifactId)
                            setSchedulesStateFeeModalOpen(true)
                          } else if (row.externalUrl) {
                            void requestPreview({
                              kind: 'http',
                              url: row.externalUrl,
                              title: row.label.replace(/^Link · /, '') || 'Fee file',
                              referrerUrl: agentResult?.resolved_url || agentUrl.trim() || null,
                              previewSessionId: agentResult?.preview_auth?.session_id || null,
                              documentHint: previewDocumentHintFromLink(row.externalUrl, row.label),
                            })
                          }
                        }}
                      >
                        Preview
                      </button>
                      <button
                        type="button"
                        className="app-btn"
                        disabled={!stateFeeSelectValue || !statePickPortalActions.download}
                        onClick={() => {
                          const row = selectedStateFeePickRow
                          if (!row) return
                          if (row.catalogTableIndex != null && row.catalogRowIndex != null && agentResult?.catalog_tables) {
                            const t = agentResult.catalog_tables[row.catalogTableIndex]
                            const dataRow = Array.isArray(t?.rows) ? t.rows[row.catalogRowIndex] : null
                            if (!t || !dataRow) return
                            const cols = getTableColumns(t)
                            const prim = primaryCatalogRowLink(dataRow, cols)
                            if (prim && !prim.portal && prim.url) {
                              const refUrl = agentResult?.resolved_url || agentUrl.trim() || null
                              const sid = agentResult?.preview_auth?.session_id || null
                              const dh = previewDocumentHintFromLink(prim.url, prim.docLabel || row.label)
                              if (sid) {
                                void downloadViaProxy({
                                  resourceUrl: prim.url,
                                  referrerUrl: refUrl,
                                  sessionId: sid,
                                  documentHint: dh,
                                }).catch(() => {
                                  window.open(prim.url, '_blank', 'noopener,noreferrer')
                                })
                              } else {
                                window.open(prim.url, '_blank', 'noopener,noreferrer')
                              }
                              return
                            }
                            if (prim?.portal) {
                              window.alert('This row uses a portal action link. Use Preview to open it in the app.')
                              return
                            }
                            const base = `fee_schedule_${selectedStateCode || 'state'}_${row.catalogTableIndex + 1}_row_${row.catalogRowIndex + 1}`
                            downloadTableAsCsv(base, cols, [dataRow])
                          } else if (row.artifactId != null) {
                            window.open(`${API_BASE}/app/artifacts/${row.artifactId}/file`, '_blank', 'noopener,noreferrer')
                          } else if (row.externalUrl) {
                            window.open(row.externalUrl, '_blank', 'noopener,noreferrer')
                          }
                        }}
                      >
                        Download
                      </button>
                    </div>
                    ) : null}
                  </section>

                  <section className="app-card app-card--fee-dst" aria-label="DST warehouse fee table">
                    {selectedStateCode ? (
                      <div className="app-card__state-meta app-card__state-meta--compact" style={{ marginTop: 0 }}>
                        <div className="app-card__state-heading-row">
                          <div className="app-card__state-title-block">
                            <p className="app-card__state-heading">{stateNameFromCode(selectedStateCode)}</p>
                          </div>
                          <span className="app-badge app-badge--neutral app-card__fee-dst-pill">DST</span>
                        </div>
                      </div>
                    ) : (
                      <p className="app-muted app-card__state-meta" style={{ marginTop: 0 }}>
                        Select a state in the sidebar.
                      </p>
                    )}

                    <label className="app-field-label app-field-label--fee-dst-follow" htmlFor="sched-fee-pick-dst">
                      Fee schedule / file
                    </label>
                    <select
                      id="sched-fee-pick-dst"
                      className="app-select app-select--lg"
                      value={dstFeeSelectValue}
                      disabled={!selectedStateCode}
                      onChange={(e) => setSchedulesDstFeeKey(e.target.value)}
                    >
                      <option value="">
                        {dstFeeSchedulesLoading
                          ? 'Loading…'
                          : dstTablePickOptions.length
                            ? 'Choose…'
                            : 'Nothing listed'}
                      </option>
                      {dstTablePickOptions.map((o) => (
                        <option key={`dst-${o.key}`} value={o.key}>
                          {o.label}
                        </option>
                      ))}
                    </select>

                    {dstFeeSchedulesError ? (
                      <p className="app-error" role="alert" style={{ marginTop: '0.5rem' }}>
                        {dstFeeSchedulesError}
                      </p>
                    ) : null}

                    {dstFeeSelectValue ? (
                      <div className="app-fee-dst-range">
                        <label className="app-field-label" htmlFor="sched-dst-start-date">
                          Effective date
                        </label>
                        <select
                          id="sched-dst-start-date"
                          className="app-select app-select--lg"
                          value={schedulesDstStartDateIso}
                          disabled={!dstFeeSelectValue || dstFeeSchedulesLoading}
                          onChange={(e) => setSchedulesDstStartDateIso(e.target.value)}
                        >
                          <option value="">
                            {!schedulesDstCardCache?.columns?.length
                              ? 'Loading…'
                              : !schedulesDstDatePlan.dateCol
                                ? 'No date column'
                                : schedulesDstDatePlan.isoDays.length === 0
                                  ? 'No dates in slice'
                                  : 'Select date…'}
                          </option>
                          {schedulesDstDatePlan.isoDays.map((iso) => (
                            <option key={iso} value={iso}>
                              {displayLabelForIsoDay(iso)}
                            </option>
                          ))}
                        </select>
                        {schedulesDstCardCache?.columns?.length && !schedulesDstDatePlan.dateCol ? (
                          <p className="app-error app-fee-dst-msg" role="status">
                            No effective-date column in this fee schedule.
                          </p>
                        ) : null}
                      </div>
                    ) : null}

                    <div className="app-card__actions">
                      <button
                        type="button"
                        className="app-btn"
                        disabled={!dstFeeSelectValue || dstFeeSchedulesLoading}
                        onClick={() => {
                          const m = dstFeeSelectValue.match(/^d:(.+)$/)
                          if (m) {
                            schedulesDstPreviewFilterStartRef.current = schedulesDstStartDateIso.trim()
                            schedulesDstPreviewFilterDateColRef.current = schedulesDstDatePlan.dateCol
                            setSchedulesDstError(null)
                            setSchedulesDstCols([])
                            setSchedulesDstRows([])
                            setSchedulesDstLoading(true)
                            setSchedulesDstModalTable(m[1])
                            setSchedulesDstFeeModalOpen(true)
                          }
                        }}
                      >
                        Preview
                      </button>
                      <button
                        type="button"
                        className="app-btn"
                        disabled={schedulesDstExcelDownloadDisabled}
                        title={
                          schedulesDstExcelDownloadDisabled &&
                          schedulesDstStartDateIso.trim() &&
                          schedulesDstDatePlan.dateCol
                            ? 'No rows fall in this effective-date range.'
                            : undefined
                        }
                        onClick={() => {
                          const m = dstFeeSelectValue.match(/^d:(.+)$/)
                          if (!m) return
                          const table = m[1]
                          const iso = schedulesDstStartDateIso.trim()
                          const cols = schedulesDstDatePlan.columns
                          const dc = schedulesDstDatePlan.dateCol
                          const rowsOut = schedulesDstRowsForExcel
                          if (!rowsOut.length || !cols.length) return
                          const base = (
                            iso && dc
                              ? `${selectedStateCode}_${table}_${iso}_year_slice`
                              : `${selectedStateCode}_${table}_export`
                          )
                            .replace(/[^\w.-]+/g, '_')
                            .slice(0, 160)
                          downloadTableAsXlsx(base, cols, rowsOut)
                        }}
                      >
                        Download .xlsx
                      </button>
                    </div>
                  </section>
                  </div>

                  <div className="app-fee-compare-strip" role="group" aria-label="Compare state file to DST">
                    <button
                      type="button"
                      className="app-btn app-btn--primary app-fee-compare-strip-btn"
                      disabled={schedulesCompareDisabled}
                      onClick={() => void runSchedulesCompare()}
                    >
                      Compare
                    </button>
                    {schedulesCompareLoading ? (
                      <div className="app-fee-bridge-status" role="status" aria-live="polite">
                        <span className="app-fee-bridge-spinner" aria-hidden />
                        <span className="app-muted app-fee-bridge-processing">Comparing…</span>
                      </div>
                    ) : null}
                  </div>
                </div>

                <section className="app-card app-card--recent-compares" aria-label="Recent fee comparisons">
                  <div className="app-recent-compares-head">
                    <h3 className="app-card__title app-card__title--sm app-recent-compares-title">Recent comparisons</h3>
                    {selectedStateCode ? (
                      <p className="app-muted app-schedules-count" role="status">
                        {compareRunsLoading
                          ? 'Loading comparisons…'
                          : `${feeSchedRecentCompareRows.length} for ${stateNameFromCode(selectedStateCode) || selectedStateCode}`}
                      </p>
                    ) : null}
                  </div>
                  {compareRunsError ? (
                    <p className="app-error" role="alert">
                      {compareRunsError}
                    </p>
                  ) : null}
                  {!selectedStateCode ? (
                    <p className="app-muted">Select a state in the sidebar to see comparison history.</p>
                  ) : compareRunsLoading && !feeSchedRecentCompareRows.length ? (
                    <p className="app-muted">Loading recent comparisons…</p>
                  ) : feeSchedRecentCompareRows.length ? (
                    <div className="app-table-scroll app-table-scroll--recent-compares">
                      <table className="app-data-table app-data-table--recent-compares">
                        <thead>
                          <tr>
                            <th>Schedule</th>
                            <th>DST file</th>
                            <th>Source</th>
                            <th>Match %</th>
                            <th>Differences</th>
                            <th>Compared at</th>
                            <th>Status</th>
                            <th>Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          {feeSchedRecentCompareRows.map((entry) => {
                            const mp = typeof entry.matchPct === 'number' ? entry.matchPct : feeCompareJoinedMatchPct(entry.summary)
                            const summary = entry.summary && typeof entry.summary === 'object' ? entry.summary : {}
                            const mod = Number(summary.mismatch_count) || 0
                            const missDst = Number(summary.state_only_count) || 0
                            const missSt = Number(summary.dst_only_row_count) || 0
                            const totalDiff = mod + missDst + missSt
                            const dstNm = entry.dstFsname || '—'
                            const srcLabel = entry.triggerSource === 'sync' ? 'Auto' : 'Manual'
                            const st = entry.status || ''
                            const statusLabel =
                              st === 'success'
                                ? 'Changes'
                                : st === 'no_changes'
                                  ? 'No changes'
                                  : st === 'error'
                                    ? 'Error'
                                    : st || '—'
                            const statusClass =
                              st === 'success'
                                ? 'app-badge--success'
                                : st === 'no_changes'
                                  ? 'app-badge--muted'
                                  : st === 'error'
                                    ? 'app-badge--danger'
                                    : 'app-badge--muted'
                            return (
                              <tr key={entry.id}>
                                <td>
                                  <span className="app-recent-cell-file" title={entry.artifactLabel}>
                                    {entry.artifactLabel}
                                  </span>
                                </td>
                                <td>
                                  <span className="app-recent-cell-file" title={dstNm}>
                                    {dstNm}
                                  </span>
                                </td>
                                <td>{srcLabel}</td>
                                <td>
                                  {mp != null && st !== 'error' ? (
                                    <div className="app-recent-match-cell">
                                      <span className="app-recent-match-pct">{`${mp.toFixed(1)}%`}</span>
                                      <div className="app-fee-compare-match-bar-wrap app-recent-match-bar-wrap" aria-hidden="true">
                                        <div className="app-fee-compare-match-bar-fill" style={{ width: `${Math.min(100, mp)}%` }} />
                                      </div>
                                    </div>
                                  ) : (
                                    '—'
                                  )}
                                </td>
                                <td>
                                  <strong className="app-recent-diff-total" title="Total differing rows">
                                    {st === 'error' ? '—' : String(totalDiff)}
                                  </strong>
                                </td>
                                <td>{formatPortalLastRunAt(entry.at)}</td>
                                <td>
                                  <span className={`app-badge ${statusClass}`} title={entry.errorMessage || statusLabel}>
                                    {statusLabel}
                                  </span>
                                </td>
                                <td>
                                  <div className="app-recent-compares-actions">
                                    <button
                                      type="button"
                                      className="app-btn-icon"
                                      title={
                                        entry.hasSnapshot !== true
                                          ? 'No saved snapshot — run Compare again'
                                          : 'View saved comparison'
                                      }
                                      aria-label="View saved comparison"
                                      disabled={st === 'error' || entry.hasSnapshot !== true}
                                      onClick={() => void openSavedFeeComparison(entry)}
                                    >
                                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                        <path
                                          d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"
                                          stroke="currentColor"
                                          strokeWidth="1.75"
                                          strokeLinecap="round"
                                          strokeLinejoin="round"
                                        />
                                        <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.75" />
                                      </svg>
                                    </button>
                                    <button
                                      type="button"
                                      className="app-btn-icon"
                                      title="Download changed workbook"
                                      aria-label="Download changed workbook"
                                      disabled={!entry.hasWorkbook}
                                      onClick={() => downloadSavedFeeComparison(entry)}
                                    >
                                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                        <path
                                          d="M12 3v12m0 0 4-4m-4 4-4-4M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"
                                          stroke="currentColor"
                                          strokeWidth="1.75"
                                          strokeLinecap="round"
                                          strokeLinejoin="round"
                                        />
                                      </svg>
                                    </button>
                                  </div>
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  ) : !compareRunsLoading ? (
                    <p className="app-muted">No comparisons yet for this state — run Compare or sync a mapped schedule.</p>
                  ) : null}
                </section>

              {agentLoading ? (
                <section className="app-agent-loader" aria-busy="true" aria-live="polite">
                  <div className="app-agent-loader__row">
                    <div className="app-agent-loader__spinner" aria-hidden />
                    <div>
                      <h3 className="app-agent-loader__title">Fetching schedule data</h3>
                      <p className="app-agent-loader__text">
                        The agent is loading the public page and extracting tables. Busy state sites (pagination, portals) can
                        take up to a minute — please keep this tab open.
                      </p>
                    </div>
                  </div>
                  <ol className="app-agent-loader__track">
                    <li>Reaching the fee schedule URL</li>
                    <li>Analyzing layout and tables</li>
                    <li>Building your catalog rows</li>
                  </ol>
                </section>
              ) : null}
            </>
          )}
        </main>
      </div>

      {schedulesCompareOpen ? (
        <div
          className="app-modal-backdrop"
          role="presentation"
            onMouseDown={(e) => {
            if (e.target === e.currentTarget) {
              setSchedulesCompareOpen(false)
              setSchedulesCompareError(null)
              setSchedulesCompareReplayLoading(false)
            }
          }}
        >
          <div
            className="app-modal app-modal--compare"
            role="dialog"
            aria-modal="true"
            aria-labelledby="schedules-compare-result-title"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="app-modal__head">
              <h2 id="schedules-compare-result-title" className="app-modal__title">
                Compare results
              </h2>
              <button
                type="button"
                className="app-modal__close"
                onClick={() => {
                  setSchedulesCompareOpen(false)
                  setSchedulesCompareError(null)
                  setSchedulesCompareReplayLoading(false)
                }}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <div className="app-modal__body app-modal__body--compare">
              {schedulesCompareReplayLoading ? (
                <div className="app-fee-bridge-status" role="status" aria-live="polite">
                  <span className="app-fee-bridge-spinner" aria-hidden />
                  <span className="app-muted app-fee-bridge-processing">Loading saved comparison…</span>
                </div>
              ) : schedulesCompareError ? (
                <p className="app-error" role="alert">
                  {schedulesCompareError}
                </p>
              ) : (
                <FeeScheduleComparePanel result={schedulesCompareResult} />
              )}
            </div>
          </div>
        </div>
      ) : null}

      {schedulesStateFeeModalOpen ? (
        <div
          className="app-modal-backdrop app-modal-backdrop--fee"
          role="presentation"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) clearSchedulesArtifactPreview()
          }}
        >
          <div
            className="app-modal app-modal--fee-preview"
            role="dialog"
            aria-modal="true"
            aria-labelledby="fee-state-preview-title"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="app-modal__head">
              <h2 id="fee-state-preview-title" className="app-modal__title">
                State fee file
                {schedulesStatePreviewId != null ? (
                  <>
                    {' '}
                    ·{' '}
                    {(() => {
                      const a = stateArtifacts.find((x) => Number(x.artifact_id) === schedulesStatePreviewId)
                      return a ? artifactFeeScheduleDisplayName(a) : `#${schedulesStatePreviewId}`
                    })()}
                  </>
                ) : null}
              </h2>
              <button
                type="button"
                className="app-modal__close"
                onClick={() => clearSchedulesArtifactPreview()}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <div className="app-modal__body app-modal__body--fee-preview">
              {schedulesStateLoading ? (
                <p className="app-muted" role="status">
                  Loading preview…
                </p>
              ) : null}
              {!schedulesStateLoading && schedulesStateError ? (
                <p className="app-error" role="alert">
                  {schedulesStateError}
                </p>
              ) : null}
              {!schedulesStateLoading && schedulesStatePdfUrl ? (
                <iframe className="app-fee-modal-pdf-frame" title="PDF preview" src={schedulesStatePdfUrl} />
              ) : null}
              {!schedulesStateLoading && !schedulesStateError && schedulesStateCols.length > 0 ? (
                <>
                  <p className="app-muted app-catalog-meta" style={{ margin: '0 0 0.65rem' }}>
                    {schedulesStateRows.length} row{schedulesStateRows.length === 1 ? '' : 's'} · {schedulesStateCols.length}{' '}
                    columns
                  </p>
                  <div className="app-table-scroll app-modal-fee-table-scroll">
                    <table className="app-data-table app-data-table--catalog">
                      <thead>
                        <tr>
                          {schedulesStateCols.map((col) => (
                            <th key={col} title={typeof col === 'string' ? col : String(col)}>
                              {col}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {schedulesStateRowsForPreview.map((row, ri) => (
                          <tr key={`fee-state-${ri}`}>
                            {schedulesStateCols.map((col) => (
                              <td key={`${ri}-${col}`}>{formatFeeSchedulePreviewCell(col, row[col])}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : null}
              {!schedulesStateLoading &&
              !schedulesStateError &&
              !schedulesStatePdfUrl &&
              schedulesStateCols.length === 0 ? (
                <p className="app-muted">No preview content yet.</p>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}

      {schedulesDstFeeModalOpen ? (
        <div
          className="app-modal-backdrop app-modal-backdrop--fee"
          role="presentation"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) closeSchedulesDstFeeModal()
          }}
        >
          <div
            className="app-modal app-modal--fee-preview"
            role="dialog"
            aria-modal="true"
            aria-labelledby="fee-dst-preview-title"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="app-modal__head">
              <h2 id="fee-dst-preview-title" className="app-modal__title">
                DST fee schedule{schedulesDstModalTable ? ` · ${schedulesDstModalTable}` : ''}
              </h2>
              <button
                type="button"
                className="app-modal__close"
                onClick={() => closeSchedulesDstFeeModal()}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <div className="app-modal__body app-modal__body--fee-preview">
              {schedulesDstLoading ? (
                <p className="app-muted" role="status">
                  Loading rows…
                </p>
              ) : null}
              {!schedulesDstLoading && schedulesDstError ? (
                <p className="app-error" role="alert">
                  {schedulesDstError}
                </p>
              ) : null}
              {!schedulesDstLoading && schedulesDstCols.length > 0 ? (
                <>
                  <p className="app-muted app-catalog-meta" style={{ margin: '0 0 0.65rem' }}>
                    {schedulesDstRows.length} row{schedulesDstRows.length === 1 ? '' : 's'} · {schedulesDstCols.length} columns
                  </p>
                  <div className="app-table-scroll app-modal-fee-table-scroll">
                    <table className="app-data-table app-data-table--catalog">
                      <thead>
                        <tr>
                          {schedulesDstCols.map((col) => (
                            <th key={col} title={typeof col === 'string' ? col : String(col)}>
                              {col}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {schedulesDstRowsForPreview.map((row, ri) => (
                          <tr key={`fee-dst-${ri}`}>
                            {schedulesDstCols.map((col) => (
                              <td key={`${ri}-${col}`}>{formatFeeSchedulePreviewCell(col, row[col])}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : null}
              {!schedulesDstLoading && !schedulesDstError && schedulesDstCols.length === 0 ? (
                <p className="app-muted">No rows returned.</p>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}

      {preview ? (
        <div
          className="app-modal-backdrop"
          role="presentation"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) closePreview()
          }}
        >
          <div
            className="app-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="preview-dialog-title"
          >
            <div className="app-modal__head">
              <h2 id="preview-dialog-title" className="app-modal__title">
                Preview{preview.title ? ` — ${preview.title}` : ''}
              </h2>
              <button type="button" className="app-modal__close" onClick={() => closePreview()} aria-label="Close">
                ×
              </button>
            </div>
            <div className="app-modal__body">
              {preview.kind === 'snippet_loading' ? (
                <p className="app-muted" style={{ margin: 0 }} role="status">
                  Loading preview from the extracted link…
                </p>
              ) : null}
              {preview.kind === 'snippet_error' ? (
                <>
                  <p className="app-error" style={{ marginTop: 0 }}>
                    {preview.message}
                  </p>
                  {preview.resourceUrl ? (
                    <p className="app-muted">
                      Try{' '}
                      <a className="app-link" href={preview.resourceUrl} target="_blank" rel="noreferrer">
                        open in new tab
                      </a>{' '}
                      or{' '}
                      <button
                        type="button"
                        className="app-btn app-btn--sm app-btn--secondary"
                        onClick={() => {
                          downloadViaProxy({
                            resourceUrl: preview.resourceUrl,
                            referrerUrl: preview.referrerUrl,
                            sessionId: preview.previewSessionId,
                            documentHint: preview.documentHint,
                          }).catch((err) =>
                            window.alert(err?.message || 'Proxy download failed (session may have expired).'),
                          )
                        }}
                      >
                        download via app
                      </button>
                      {preview.previewSessionId ? (
                        <span className="app-muted">
                          {' '}
                          (uses the ephemeral session created when you last ran Extract for this portal)
                        </span>
                      ) : null}
                    </p>
                  ) : null}
                </>
              ) : null}
              {preview.kind === 'snippet_rejected' ? (
                <>
                  <p className="app-error" style={{ marginTop: 0 }}>
                    Preview not available
                    {preview.errorCode ? ` (${preview.errorCode})` : ''}.
                  </p>
                  {preview.errorCode === 'credential_fetch_requires_preview_session' ? (
                    <p className="app-muted">
                      Run <strong>Get Data</strong> on the agency page first — preview can proxy cookie-protected portals only
                      when a session snapshot exists from that run.
                    </p>
                  ) : null}
                  {preview.errorCode === 'host_not_in_preview_scope' ? (
                    <p className="app-muted">
                      This URL hostname is not authorized for your saved preview session — only the originating site (and its
                      shared cookie scopes) may be fetched through the proxy.
                    </p>
                  ) : null}
                  {preview.errorCode === 'upstream_auth_xml' ? (
                    <p className="app-muted">
                      The file URL hit a ServiceNow API that answered with “not authenticated” (guest downloads often use a
                      public <code>…/pubatt/dl/…</code> path instead of <code>/api/now/attachment/…</code>). Run{' '}
                      <strong>Get Data</strong> again so rows pick up the correct download links.
                    </p>
                  ) : null}
                  {Array.isArray(preview.upstreamAttempts) && preview.upstreamAttempts.length > 0 ? (
                    <details className="app-muted" style={{ marginTop: '0.35rem' }}>
                      <summary>Fetch attempts (debug)</summary>
                      <pre className="app-preview app-preview--tight" style={{ marginTop: '0.25rem', fontSize: '0.82rem' }}>
                        {JSON.stringify(preview.upstreamAttempts, null, 2)}
                      </pre>
                    </details>
                  ) : null}
                  {preview.resourceUrl ? (
                    <p style={{ marginBottom: 0 }}>
                      <a className="app-link" href={preview.resourceUrl} target="_blank" rel="noreferrer">
                        Open link in new tab
                      </a>
                      {preview.previewSessionId ? (
                        <>
                          {' · '}
                          <button
                            type="button"
                            className="app-btn app-btn--sm app-btn--secondary"
                            onClick={() => {
                              downloadViaProxy({
                                resourceUrl: preview.resourceUrl,
                                referrerUrl: preview.referrerUrl,
                                sessionId: preview.previewSessionId,
                                documentHint: preview.documentHint,
                              }).catch((err) =>
                                window.alert(err?.message || 'Proxy download failed (session may have expired).'),
                              )
                            }}
                          >
                            Try download via app
                          </button>
                        </>
                      ) : null}
                    </p>
                  ) : null}
                </>
              ) : null}
              {preview.kind === 'snippet' && preview.snippet?.ok ? (
                <>
                  {preview.snippet.hint ? <p className="app-muted" style={{ marginTop: 0 }}>{preview.snippet.hint}</p> : null}
                  {Array.isArray(preview.snippet.table_preview?.columns) &&
                  preview.snippet.table_preview.columns.length > 0 ? (
                    <>
                      <p className="app-muted">
                        Showing up to {preview.snippet.table_preview.rows?.length ?? 0} rows from the inferred main grid (not the
                        full document).
                      </p>
                      <div className="app-table-scroll" style={{ maxHeight: 'min(420px, 55vh)' }}>
                        <table className="app-data-table app-snippet-preview-table">
                          <thead>
                            <tr>
                              {preview.snippet.table_preview.columns.map((c) => (
                                <th key={String(c)}>{String(c)}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {(preview.snippet.table_preview.rows || []).map((r, ri) => (
                              <tr key={`sr-${ri}`}>
                                {(r || []).map((cell, ci) => (
                                  <td key={`sc-${ri}-${ci}`}>{cell === null || cell === undefined ? '—' : String(cell)}</td>
                                ))}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  ) : null}
                  {preview.snippet.text_preview && !preview.snippet.table_preview?.columns?.length ? (
                    <pre className="app-preview app-preview--tight" style={{ marginTop: 0, maxHeight: 'min(360px, 50vh)' }}>
                      {preview.snippet.text_preview}
                    </pre>
                  ) : null}
                  {preview.blobUrl &&
                  !(
                    Array.isArray(preview.snippet.table_preview?.columns) &&
                    preview.snippet.table_preview.columns.length > 0
                  ) ? (
                    <>
                      <p className="app-muted">
                        Showing an in-app blob copy of the fetched file ({preview.snippet.detected_kind || 'inline'}).
                      </p>
                      <div className="app-preview-frame-wrap">
                        <iframe title="Document preview" className="app-preview-frame" src={preview.blobUrl} />
                      </div>
                    </>
                  ) : null}
                  {(preview.snippet.detected_kind === 'binary_large' ||
                    preview.snippet.detected_kind === 'binary_small') &&
                  !preview.blobUrl &&
                  !preview.snippet.table_preview?.columns?.length ? (
                    <p className="app-muted">
                      Inline preview isn’t offered for this file type or size — fetch it through the app proxy instead.
                    </p>
                  ) : null}
                  <div className="app-preview-actions" style={{ marginTop: '0.75rem' }}>
                    {preview.resourceUrl ? (
                      <a className="app-link" href={preview.resourceUrl} target="_blank" rel="noreferrer">
                        Open original URL
                      </a>
                    ) : null}
                    {preview.resourceUrl && preview.previewSessionId ? (
                      <>
                        {' · '}
                        <button
                          type="button"
                          className="app-btn app-btn--sm app-btn--secondary"
                          onClick={() => {
                            downloadViaProxy({
                              resourceUrl: preview.resourceUrl,
                              referrerUrl: preview.referrerUrl,
                              sessionId: preview.previewSessionId,
                              documentHint: preview.documentHint,
                            }).catch((err) => window.alert(err?.message || 'Download failed'))
                          }}
                        >
                          Download via app
                        </button>
                      </>
                    ) : null}
                  </div>
                </>
              ) : null}
              {preview.kind === 'portal' ? (
                <>
                  <p style={{ marginTop: 0 }}>
                    This item uses a <code>javascript:</code> / <code>__doPostBack</code> control from the agency website. It is not
                    a stable file URL — it only works inside the original page (browser session, form state).
                  </p>
                  <p className="app-muted">Use the agency’s fee schedule page and its own buttons to open or download the file.</p>
                  {preview.sourcePageUrl ? (
                    <p style={{ marginBottom: 0 }}>
                      <a className="app-btn app-btn--primary" href={preview.sourcePageUrl} target="_blank" rel="noreferrer">
                        Open source page
                      </a>
                    </p>
                  ) : (
                    <p className="app-muted" style={{ marginBottom: 0 }}>
                      Paste the same URL you ran in State Data into your browser.
                    </p>
                  )}
                </>
              ) : null}
              {preview.kind === 'bad' ? (
                <p className="app-error" style={{ margin: 0 }}>
                  Could not preview this link{preview.raw ? ` (${preview.raw})` : ''}.
                </p>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
