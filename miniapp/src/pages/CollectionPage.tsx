import { useEffect, useState, type ReactNode } from 'react'
import { api, dataErrorOf, type DataCollection, type DataFilter, type DataRecord, type FieldDef, type Lang } from '../api'
import type { Route } from '../App'
import {
  formatDate,
  formatDateTime,
  formatNumber,
  hasText,
  localDateTime,
  titleOf,
  useI18n,
  type Translate,
} from '../i18n'
import { useLoad } from '../hooks'
import { ErrorBox, Loading } from '../ui'
import { RecordForm } from './RecordForm'

const PAGE = 50

/** One list of a bot: its records as cards or a week calendar, and a form to add or edit. */
export function CollectionPage(props: { botId: number; collectionId: number; navigate: (r: Route) => void }) {
  const { botId, collectionId, navigate } = props
  const { t } = useI18n()
  const collections = useLoad(() => api.collections(botId), [botId])
  const c = collections.data?.find((x) => x.id === collectionId)

  if (collections.error) return <ErrorBox message={collections.error} onRetry={collections.reload} />
  if (!collections.data) return <Loading />
  if (!c) {
    return (
      <div className="card">
        <p className="hint">{t('dataError.collection_not_found')}</p>
        <button className="btn secondary" onClick={() => navigate({ page: 'data', botId })}>
          {t('data')}
        </button>
      </div>
    )
  }
  return <CollectionView key={c.id} botId={botId} c={c} />
}

function CollectionView({ botId, c }: { botId: number; c: DataCollection }) {
  const { t, lang } = useI18n()
  const visible = c.fields.filter((f) => !f.hidden)
  const dateField = visible.find((f) => f.type === 'date' || f.type === 'datetime')
  const [view, setView] = useState<'table' | 'calendar'>('table')
  const [editing, setEditing] = useState<DataRecord | 'new' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [query, setQuery] = useState<TableQuery>({ q: '', sort: '', desc: true })
  const [weekStart, setWeekStart] = useState(() => weekStartOf(todayIn(c.timezone), lang))

  const open = (record: DataRecord | 'new') => {
    setNotice(null)
    setEditing(record)
  }
  const close = (message?: string) => {
    setNotice(message ?? null)
    setEditing(null)
  }

  return (
    <>
      <div className="row between">
        <h1>{titleOf(c.title, lang, c.key)}</h1>
        {!editing && (
          <button className="btn secondary" onClick={() => open('new')}>
            + {t('addRecord')}
          </button>
        )}
      </div>
      {editing ? (
        <RecordForm
          botId={botId}
          collection={c}
          record={editing === 'new' ? null : editing}
          onSaved={() => close()}
          onCancel={close}
          onDeleted={() => close()}
        />
      ) : (
        <>
          {notice && <p className="error card">{notice}</p>}
          {dateField && (
            <div className="tabs">
              <button className={view === 'table' ? 'active' : ''} onClick={() => setView('table')}>
                {t('table')}
              </button>
              <button className={view === 'calendar' ? 'active' : ''} onClick={() => setView('calendar')}>
                {t('calendar')}
              </button>
            </div>
          )}
          {view === 'calendar' && dateField ? (
            <CalendarView
              botId={botId}
              c={c}
              dateField={dateField}
              weekStart={weekStart}
              onWeek={setWeekStart}
              onOpen={open}
            />
          ) : (
            <TableView botId={botId} c={c} query={query} onQuery={setQuery} onOpen={open} />
          )}
        </>
      )}
    </>
  )
}

interface TableQuery {
  q: string
  /** '' is newest first */
  sort: string
  desc: boolean
}

function TableView(props: {
  botId: number
  c: DataCollection
  query: TableQuery
  onQuery: (q: TableQuery) => void
  onOpen: (r: DataRecord) => void
}) {
  const { botId, c, query, onQuery } = props
  const { t, lang } = useI18n()
  const [text, setText] = useState(query.q)
  const pages = useRecordPages(botId, c.id, {
    q: query.q || undefined,
    sort: query.sort || undefined,
    desc: query.sort ? query.desc : true,
  })
  const visible = c.fields.filter((f) => !f.hidden)

  useEffect(() => {
    if (text === query.q) return
    const timer = setTimeout(() => onQuery({ ...query, q: text }), 300)
    return () => clearTimeout(timer)
  }, [text, query, onQuery])

  return (
    <>
      <input placeholder={t('search')} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="row" style={{ margin: '8px 0' }}>
        <select value={query.sort} onChange={(e) => onQuery({ ...query, sort: e.target.value })}>
          <option value="">{t('newest')}</option>
          {visible.map((f) => (
            <option key={f.key} value={f.key}>
              {titleOf(f.label, lang, f.key)}
            </option>
          ))}
        </select>
        {query.sort && (
          <button className="btn secondary" onClick={() => onQuery({ ...query, desc: !query.desc })}>
            {query.desc ? '↓' : '↑'}
          </button>
        )}
      </div>
      <RecordPages pages={pages}>
        {(records) =>
          records.map((r) => <RecordCard key={r.id} c={c} record={r} onOpen={() => props.onOpen(r)} />)
        }
      </RecordPages>
    </>
  )
}

function RecordCard({ c, record, onOpen }: { c: DataCollection; record: DataRecord; onOpen: () => void }) {
  const { t, lang } = useI18n()
  const lines = c.fields
    .filter((f) => !f.hidden && !isEmpty(record.data[f.key]))
    .slice(0, 4)
    .map((f) => ({ f, text: formatValue(f, record.data[f.key], lang, c.timezone, t) }))

  return (
    <button className="list-item" onClick={onOpen}>
      {lines.map(({ f, text }, i) => (
        <div key={f.key} className={i === 0 ? 'row between' : 'hint'}>
          <span>
            {titleOf(f.label, lang, f.key)}: {text}
          </span>
          {i === 0 && record.mine && <span className="badge">{t('mineBadge')}</span>}
        </div>
      ))}
      {lines.length === 0 && record.mine && <span className="badge">{t('mineBadge')}</span>}
    </button>
  )
}

function CalendarView(props: {
  botId: number
  c: DataCollection
  dateField: FieldDef
  weekStart: string
  onWeek: (day: string) => void
  onOpen: (r: DataRecord) => void
}) {
  const { botId, c, dateField, weekStart, onWeek } = props
  const { t, lang } = useI18n()
  const weekEnd = addDays(weekStart, 6)
  const withTime = dateField.type === 'datetime'
  const filters: DataFilter[] = [
    { field: dateField.key, op: 'gte', value: withTime ? `${weekStart}T00:00` : weekStart },
    withTime
      ? { field: dateField.key, op: 'lt', value: `${addDays(weekEnd, 1)}T00:00` }
      : { field: dateField.key, op: 'lte', value: weekEnd },
  ]
  const pages = useRecordPages(botId, c.id, { filters, sort: dateField.key, desc: false })
  const visible = c.fields.filter((f) => !f.hidden)
  const timeField = visible.find((f) => f.type === 'time')
  const textField = visible.find((f) => f.type === 'text')
  const weekday = new Intl.DateTimeFormat(lang === 'fa' ? 'fa-IR' : 'en-GB', { weekday: 'long', timeZone: 'UTC' })

  // A record's day and time, in the collection's zone for datetimes.
  const when = (r: DataRecord): [string, string] => {
    const value = String(r.data[dateField.key] ?? '')
    if (withTime) {
      const local = localDateTime(value, c.timezone)
      return [local.slice(0, 10), local.slice(11, 16)]
    }
    return [value, timeField ? String(r.data[timeField.key] ?? '') : '']
  }

  return (
    <>
      <div className="row between">
        <button className="btn secondary" onClick={() => onWeek(addDays(weekStart, -7))}>
          {t('prevWeek')}
        </button>
        <button className="btn secondary" onClick={() => onWeek(addDays(weekStart, 7))}>
          {t('nextWeek')}
        </button>
      </div>
      <RecordPages pages={pages} hideEmpty>
        {(records) => {
          const timed = records.map((r) => ({ r, at: when(r) }))
          return Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)).map((day) => (
            <div key={day} className="cal-day">
              <h3>
                {weekday.format(new Date(`${day}T00:00:00Z`))} · {formatDate(day, lang)}
              </h3>
              {timed
                .filter(({ at }) => at[0] === day)
                .sort((a, b) => a.at[1].localeCompare(b.at[1]))
                .map(({ r, at }) => (
                  <button key={r.id} className="cal-row" onClick={() => props.onOpen(r)}>
                    {at[1] && (
                      <span className="hint" dir="ltr">
                        {at[1]}
                      </span>
                    )}
                    <span>{textField ? String(r.data[textField.key] ?? '') : ''}</span>
                    {r.mine && <span className="badge">{t('mineBadge')}</span>}
                  </button>
                ))}
            </div>
          ))
        }}
      </RecordPages>
    </>
  )
}

type Pages = ReturnType<typeof useRecordPages>

/** Loading, errors, the empty state and "Show more" around a list of records. */
function RecordPages(props: {
  pages: Pages
  hideEmpty?: boolean
  children: (records: DataRecord[]) => ReactNode
}) {
  const { pages } = props
  const { t } = useI18n()
  if (pages.error) return <ErrorBox message={pages.error} onRetry={pages.reload} />
  if (!pages.records) return <Loading />
  return (
    <>
      {pages.records.length === 0 && !props.hideEmpty && <p className="hint card">{t('noRecords')}</p>}
      {props.children(pages.records)}
      {pages.hasMore && (
        <button className="btn secondary block" disabled={pages.busy} onClick={pages.loadMore}>
          {t('loadMore')}
        </button>
      )}
    </>
  )
}

type RecordQuery = Omit<Parameters<typeof api.records>[2], 'offset' | 'limit'>

/** Pages of records for `params`; a change of params starts again from the first page. */
function useRecordPages(botId: number, collectionId: number, params: RecordQuery) {
  const { t } = useI18n()
  const key = JSON.stringify(params)
  const [loaded, setLoaded] = useState<{ key: string; records: DataRecord[]; hasMore: boolean } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    api.records(botId, collectionId, { ...(JSON.parse(key) as RecordQuery), limit: PAGE }).then(
      (page) => {
        if (!live) return
        setLoaded({ key, records: page.records, hasMore: page.has_more })
        setError(null)
      },
      (e: Error) => live && setError(explain(e, t)),
    )
    return () => {
      live = false
    }
  }, [botId, collectionId, key, nonce, t])

  const current = loaded?.key === key ? loaded : null
  const loadMore = () => {
    if (!current) return
    setBusy(true)
    api
      .records(botId, collectionId, { ...params, offset: current.records.length, limit: PAGE })
      .then(
        (page) =>
          setLoaded((s) =>
            s?.key === key ? { key, records: appendNew(s.records, page.records), hasMore: page.has_more } : s,
          ),
        (e: Error) => setError(explain(e, t)),
      )
      .finally(() => setBusy(false))
  }

  return {
    records: current?.records ?? null,
    hasMore: current?.hasMore ?? false,
    error,
    busy,
    loadMore,
    reload: () => {
      setError(null)
      setNonce((n) => n + 1)
    },
  }
}

/** Records shift while paging (a record added meanwhile); keep each one once. */
function appendNew(records: DataRecord[], more: DataRecord[]): DataRecord[] {
  const seen = new Set(records.map((r) => r.id))
  return [...records, ...more.filter((r) => !seen.has(r.id))]
}

/** A data error in the user's language, else the server's message. */
function explain(e: Error, t: Translate): string {
  const code = dataErrorOf(e)?.code
  return code && hasText(`dataError.${code}`) ? t(`dataError.${code}`) : e.message
}

function isEmpty(v: unknown): boolean {
  return v === null || v === undefined || v === ''
}

function formatValue(f: FieldDef, v: unknown, lang: Lang, timeZone: string, t: Translate): string {
  if (isEmpty(v)) return ''
  const n = Number(v)
  switch (f.type) {
    case 'date':
      return formatDate(String(v), lang)
    case 'datetime':
      return formatDateTime(String(v), lang, timeZone)
    case 'boolean':
      return v ? t('yes') : t('no')
    case 'number':
      return Number.isNaN(n) ? String(v) : formatNumber(n, lang)
    case 'money':
      return `${Number.isNaN(n) ? String(v) : formatNumber(n, lang)}${f.currency ? ` ${f.currency}` : ''}`
    default:
      return String(v)
  }
}

/** Today's date (`YYYY-MM-DD`) in `timeZone`. */
function todayIn(timeZone: string): string {
  return new Intl.DateTimeFormat('sv-SE', { timeZone }).format(new Date())
}

function addDays(day: string, n: number): string {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + n)
  return d.toISOString().slice(0, 10)
}

/** Weeks start on Saturday in Persian and on Monday in English. */
function weekStartOf(day: string, lang: Lang): string {
  const first = lang === 'fa' ? 6 : 1
  const weekday = new Date(`${day}T00:00:00Z`).getUTCDay()
  return addDays(day, -((weekday - first + 7) % 7))
}
