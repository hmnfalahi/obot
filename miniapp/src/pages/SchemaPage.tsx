import { useState } from 'react'
import {
  api,
  dataErrorOf,
  type CollectionEvents,
  type DataCollection,
  type FieldDef,
  type FieldType,
  type Lang,
  type Rules,
} from '../api'
import type { Route } from '../App'
import { formatNumber, hasText, titleOf, useI18n, type Translate } from '../i18n'
import { useLoad } from '../hooks'
import { haptic } from '../telegram'
import { ErrorBox, Loading } from '../ui'

const FIELD_TYPES: FieldType[] = ['text', 'number', 'money', 'date', 'time', 'datetime', 'choice', 'boolean', 'phone']
const KEY_PATTERN = /^[a-z][a-z0-9_]{0,39}$/
const MAX_REMIND_MINUTES = 30 * 24 * 60
/** Weekdays (0 = Monday) in each language's week order, as in backend data/describe.py. */
const WEEK_ORDER: Record<Lang, number[]> = { fa: [5, 6, 0, 1, 2, 3, 4], en: [0, 1, 2, 3, 4, 5, 6] }

type Notice = { card: 'schema' | 'events'; text: string; error: boolean; detail?: string }

/** The owner's editor for one list: its design (additive only), rules and notifications. */
export function SchemaPage(props: { botId: number; collectionId: number; navigate: (r: Route) => void }) {
  const { botId, collectionId, navigate } = props
  const { t, lang } = useI18n()
  const collections = useLoad(() => api.collections(botId), [botId])
  const [notice, setNotice] = useState<Notice | null>(null)
  const c = collections.data?.find((x) => x.id === collectionId)

  if (collections.error) return <ErrorBox message={collections.error} onRetry={collections.reload} />
  if (!collections.data) return <Loading />
  if (!c || !c.can_edit) {
    return (
      <div className="card">
        <p className="hint">{t('dataError.collection_not_found')}</p>
        <button className="btn secondary" onClick={() => navigate({ page: 'data', botId })}>
          {t('data')}
        </button>
      </div>
    )
  }

  // Each save returns the collection; the editors restart from it.
  const saved = (card: Notice['card']) => (next: DataCollection) => {
    collections.set(collections.data!.map((x) => (x.id === next.id ? next : x)))
    setNotice({ card, text: t('saved'), error: false })
  }
  const failed = (card: Notice['card']) => (e: unknown, labelOf: (key: string) => string) => {
    haptic('error')
    const detail = dataErrorOf(e)
    if (detail?.code === 'stale') collections.reload()
    setNotice({ card, error: true, ...explainSchemaError(e, t, labelOf) })
  }
  const shown = (card: Notice['card']) => (notice?.card === card ? notice : null)

  return (
    <>
      <h1>
        {t('editList')} · {titleOf(c.title, lang, c.key)}
      </h1>
      <SchemaEditor
        key={`${c.id}:${c.schema_version}`}
        botId={botId}
        c={c}
        notice={shown('schema')}
        onBusy={() => setNotice(null)}
        onSaved={saved('schema')}
        onFailed={failed('schema')}
      />
      <EventsEditor
        // A schema save restarts this editor only when what it shows changed.
        key={`${c.id}:${JSON.stringify(c.events)}:${eventFieldsKey(c)}`}
        botId={botId}
        c={c}
        notice={shown('events')}
        onBusy={() => setNotice(null)}
        onSaved={saved('events')}
        onFailed={failed('events')}
      />
    </>
  )
}

interface EditorProps {
  botId: number
  c: DataCollection
  notice: Notice | null
  onBusy: () => void
  onSaved: (c: DataCollection) => void
  onFailed: (e: unknown, labelOf: (key: string) => string) => void
}

/** A schema or events error in the user's language, with the rejected change when there is one. */
function explainSchemaError(e: unknown, t: Translate, labelOf: (key: string) => string) {
  const detail = dataErrorOf(e)
  if (!detail) return { text: (e as Error).message }
  const field = detail.fields[0] ? labelOf(detail.fields[0]) : ''
  const parts = [hasText(`dataError.${detail.code}`) ? t(`dataError.${detail.code}`) : detail.message]
  if (detail.change && hasText(`change.${detail.change}`)) parts.push(t(`change.${detail.change}`, { field }))
  // The server's own reason helps fix an invalid design; it is in English.
  return { text: parts.join(' '), detail: detail.code === 'invalid_schema' ? detail.message : undefined }
}

function NoticeLine({ notice }: { notice: Notice | null }) {
  if (!notice) return null
  if (!notice.error) return <p className="hint">{notice.text}</p>
  return (
    <>
      <p className="error">{notice.text}</p>
      {notice.detail && (
        <div className="field-error" dir="ltr">
          {notice.detail}
        </div>
      )}
    </>
  )
}

function lines(text: string): string[] {
  return [...new Set(text.split('\n').map((s) => s.trim()).filter(Boolean))]
}

interface Draft {
  key: string
  label: string
  type: FieldType
  options: string
}

const EMPTY_DRAFT: Draft = { key: '', label: '', type: 'text', options: '' }

function SchemaEditor({ botId, c, notice, onBusy, onSaved, onFailed }: EditorProps) {
  const { t, lang } = useI18n()
  const existing = new Set(c.fields.map((f) => f.key))
  const [title, setTitle] = useState(c.title[lang] ?? '')
  const [fields, setFields] = useState<FieldDef[]>(c.fields)
  // Options typed for existing choice fields; they are only ever added.
  const [moreOptions, setMoreOptions] = useState<Record<string, string>>({})
  const [rules, setRules] = useState<Rules>(c.rules)
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)
  const [busy, setBusy] = useState(false)

  const labelOf = (key: string) => {
    const f = fields.find((x) => x.key === key)
    return f ? titleOf(f.label, lang, f.key) : key
  }
  const update = (key: string, change: Partial<FieldDef>) =>
    setFields(fields.map((f) => (f.key === key ? { ...f, ...change } : f)))

  const saved = new Map(c.fields.map((f) => [f.key, f]))
  // The server refuses a hidden required field coming back as required, so it comes back optional.
  const unhidesOptional = (key: string) => !!saved.get(key)?.hidden && !!saved.get(key)?.required
  const toggleHidden = (f: FieldDef) =>
    update(
      f.key,
      f.hidden
        ? { hidden: false, required: unhidesOptional(f.key) ? false : f.required }
        : { hidden: true, required: saved.get(f.key)?.required ?? f.required },
    )

  const keyTaken = fields.some((f) => f.key === draft.key)
  const keyBad = draft.key !== '' && !KEY_PATTERN.test(draft.key)
  const draftOk =
    KEY_PATTERN.test(draft.key) &&
    !keyTaken &&
    draft.label.trim() !== '' &&
    (draft.type !== 'choice' || lines(draft.options).length > 0)

  const addField = () => {
    if (!draftOk) return
    setFields([
      ...fields,
      {
        key: draft.key,
        label: { [lang]: draft.label.trim() },
        type: draft.type,
        required: false,
        options: draft.type === 'choice' ? lines(draft.options) : [],
        min: null,
        max: null,
        currency: null,
        public: false,
        hidden: false,
      },
    ])
    setDraft(EMPTY_DRAFT)
  }

  const save = async () => {
    const original = new Map(c.fields.map((f) => [f.key, f]))
    const sent = fields.map((f) => {
      // An emptied name keeps the old one rather than sending an empty label.
      const name = (f.label[lang] ?? '').trim()
      const label = name ? { ...f.label, [lang]: name } : (original.get(f.key)?.label ?? { [lang]: f.key })
      const added = existing.has(f.key) ? lines(moreOptions[f.key] ?? '') : []
      const options = [...f.options, ...added.filter((o) => !f.options.includes(o))]
      return { ...f, label, options }
    })
    const name = title.trim()
    setBusy(true)
    onBusy()
    try {
      const next = await api.putSchema(botId, c.id, {
        fields: sent,
        rules: { unique: rules.unique ?? [], allowed_hours: rules.allowed_hours ?? null },
        ...(name ? { title: { [lang]: name } } : {}),
        version: c.schema_version,
      })
      haptic('success')
      onSaved(next)
    } catch (e) {
      onFailed(e, labelOf)
    } finally {
      setBusy(false)
    }
  }

  const unique = rules.unique ?? []
  const allowed = rules.allowed_hours

  return (
    <>
      <div className="card">
        <label htmlFor="list-title">{t('listTitle')}</label>
        <input id="list-title" maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} />
      </div>

      <h2>{t('fields')}</h2>
      <p className="hint" style={{ margin: '0 4px 8px' }}>
        {t('fieldsHint')}
      </p>
      {fields.map((f) => {
        const isNew = !existing.has(f.key)
        return (
          <div key={f.key} className="card">
            <div className="row between">
              <span className="hint" dir="ltr">
                {f.key}
              </span>
              <div className="row">
                <span className="badge">{t(`ftype.${f.type}`)}</span>
                {f.required && <span className="badge">{t('required')}</span>}
                {f.hidden && <span className="badge warn">{t('hiddenBadge')}</span>}
                {isNew && <span className="badge">{t('newBadge')}</span>}
              </div>
            </div>
            <label htmlFor={`label-${f.key}`}>{t('fieldLabel')}</label>
            <input
              id={`label-${f.key}`}
              value={f.label[lang] ?? ''}
              placeholder={titleOf(f.label, lang, f.key)}
              onChange={(e) => update(f.key, { label: { ...f.label, [lang]: e.target.value } })}
            />
            {f.type === 'choice' && (
              <>
                <label>{t('options')}</label>
                <div className="row" style={{ flexWrap: 'wrap' }}>
                  {f.options.map((o) => (
                    <span key={o} className="badge">
                      {o}
                    </span>
                  ))}
                </div>
                {!isNew && (
                  <>
                    <label htmlFor={`options-${f.key}`}>{t('addOptions')}</label>
                    <textarea
                      id={`options-${f.key}`}
                      style={{ minHeight: 70 }}
                      value={moreOptions[f.key] ?? ''}
                      onChange={(e) => setMoreOptions({ ...moreOptions, [f.key]: e.target.value })}
                    />
                  </>
                )}
              </>
            )}
            <div className="row" style={{ marginTop: 8 }}>
              {isNew ? (
                <button className="btn danger" onClick={() => setFields(fields.filter((x) => x.key !== f.key))}>
                  {t('delete')}
                </button>
              ) : (
                <button className="btn secondary" onClick={() => toggleHidden(f)}>
                  {f.hidden ? t('showField') : t('hideField')}
                </button>
              )}
            </div>
            {f.hidden && unhidesOptional(f.key) && <p className="hint">{t('unhideOptional')}</p>}
          </div>
        )
      })}

      <div className="card">
        <strong>{t('newField')}</strong>
        <label htmlFor="new-key">{t('fieldKey')}</label>
        <input
          id="new-key"
          dir="ltr"
          maxLength={40}
          value={draft.key}
          onChange={(e) => setDraft({ ...draft, key: e.target.value.trim() })}
        />
        {keyBad && <div className="field-error">{t('fieldKeyHint')}</div>}
        {keyTaken && <div className="field-error">{t('fieldKeyTaken')}</div>}
        <label htmlFor="new-label">{t('fieldLabel')}</label>
        <input id="new-label" value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
        <label htmlFor="new-type">{t('fieldType')}</label>
        <select
          id="new-type"
          value={draft.type}
          onChange={(e) => setDraft({ ...draft, type: e.target.value as FieldType })}
        >
          {FIELD_TYPES.map((type) => (
            <option key={type} value={type}>
              {t(`ftype.${type}`)}
            </option>
          ))}
        </select>
        {draft.type === 'choice' && (
          <>
            <label htmlFor="new-options">
              {t('options')} ({t('optionsHint')})
            </label>
            <textarea
              id="new-options"
              style={{ minHeight: 70 }}
              value={draft.options}
              onChange={(e) => setDraft({ ...draft, options: e.target.value })}
            />
          </>
        )}
        <button className="btn secondary block" disabled={!draftOk} onClick={addField}>
          {t('addField')}
        </button>
      </div>

      <h2>{t('rules')}</h2>
      <div className="card">
        {unique.length === 0 && !allowed && <p className="hint">{t('noRules')}</p>}
        {unique.map((group) => (
          <div key={group.join(',')} className="row between">
            <span>{t('uniqueRule', { fields: group.map(labelOf).join(' + ') })}</span>
            <button
              className="btn danger"
              onClick={() => setRules({ ...rules, unique: unique.filter((g) => g !== group) })}
            >
              {t('removeRule')}
            </button>
          </div>
        ))}
        {allowed && (
          <div className="row between">
            <span>
              {t('allowedHours')}: {windowsText(allowed.windows, lang, t)}
            </span>
            <button className="btn danger" onClick={() => setRules({ ...rules, allowed_hours: null })}>
              {t('removeAllowedHours')}
            </button>
          </div>
        )}
        <p className="hint">{t('rulesHint')}</p>
      </div>

      <button className="btn block" disabled={busy} onClick={save}>
        {t('save')}
      </button>
      <NoticeLine notice={notice} />
    </>
  )
}

function persianDigits(text: string, lang: Lang): string {
  return lang === 'fa' ? text.replace(/\d/g, (d) => '۰۱۲۳۴۵۶۷۸۹'[Number(d)]) : text
}

/** Persian and Arabic-Indic digits as ASCII ones. */
function asciiDigits(text: string): string {
  return text.replace(/[۰-۹٠-٩]/g, (d) => {
    const fa = '۰۱۲۳۴۵۶۷۸۹'.indexOf(d)
    return String(fa >= 0 ? fa : '٠١٢٣٤٥٦٧٨٩'.indexOf(d))
  })
}

/** Days in the language's week order; three or more consecutive days become a range. */
function daysText(weekdays: number[], lang: Lang, t: Translate): string {
  const order = WEEK_ORDER[lang]
  const positions = [...new Set(weekdays.map((d) => order.indexOf(d)))].filter((p) => p >= 0).sort((a, b) => a - b)
  const runs: number[][] = []
  for (const p of positions) {
    const last = runs[runs.length - 1]
    if (last && p === last[last.length - 1] + 1) last.push(p)
    else runs.push([p])
  }
  const parts = runs.flatMap((run) => {
    const names = run.map((p) => t(`weekday.${order[p]}`))
    return run.length >= 3 ? [t('ruleDays', { first: names[0], last: names[names.length - 1] })] : names
  })
  return parts.join(lang === 'fa' ? '، ' : ', ')
}

function windowsText(windows: { weekdays: number[]; start: string; end: string }[], lang: Lang, t: Translate) {
  const clock = (value: string) => persianDigits(value.slice(0, 5), lang)
  return windows
    .map((w) => t('ruleWindow', { days: daysText(w.weekdays, lang, t), start: clock(w.start), end: clock(w.end) }))
    .join(lang === 'fa' ? '؛ ' : '; ')
}

/** The date and time fields the events editor offers, as a key. */
function eventFieldsKey(c: DataCollection): string {
  return c.fields
    .filter((f) => f.type === 'date' || f.type === 'datetime' || f.type === 'time')
    .map((f) => `${f.key}:${f.type}:${f.hidden}`)
    .join(',')
}

type Remind = NonNullable<CollectionEvents['remind']>[number]
type Notify = NonNullable<CollectionEvents['notify']>[number]

const isOwnerNotice = (n: Notify) => n.on === 'create' && n.to === 'owner'
const isAuthorNotice = (n: Notify) => (n.on === 'update' || n.on === 'delete') && n.to === 'author'

function EventsEditor({ botId, c, notice, onBusy, onSaved, onFailed }: EditorProps) {
  const { t, lang } = useI18n()
  const shared = c.scope === 'shared'
  const notify = c.events.notify ?? []
  // The panel edits the first reminder; any others (the chat can make up to three) are kept.
  const [first, ...otherReminders] = c.events.remind ?? []
  const [notifyOwner, setNotifyOwner] = useState(notify.some(isOwnerNotice))
  const [notifyAuthor, setNotifyAuthor] = useState(notify.some(isAuthorNotice))
  const [noReminder, setNoReminder] = useState(!first)
  const dateFields = c.fields.filter(
    (f) => (f.type === 'date' || f.type === 'datetime') && (!f.hidden || f.key === first?.date_field),
  )
  const [dateField, setDateField] = useState(first?.date_field ?? dateFields[0]?.key ?? '')
  const [timeField, setTimeField] = useState(first?.time_field ?? '')
  const [minutes, setMinutes] = useState(String(first?.before_minutes ?? 60))
  const [busy, setBusy] = useState(false)

  const day = c.fields.find((f) => f.key === dateField)
  // Only a date field takes a separate time field; a datetime has its own time.
  const timeFields =
    day?.type === 'date' ? c.fields.filter((f) => f.type === 'time' && (!f.hidden || f.key === timeField)) : []
  const minutesText = asciiDigits(minutes.trim())
  const minutesNumber = Number(minutesText)
  const minutesOk = /^\d+$/.test(minutesText) && minutesNumber <= MAX_REMIND_MINUTES
  const reminderOk = noReminder || (day !== undefined && minutesOk)
  const labelOf = (key: string) => {
    const f = c.fields.find((x) => x.key === key)
    return f ? titleOf(f.label, lang, f.key) : key
  }

  const save = async () => {
    if (!reminderOk) return
    // Rules the panel doesn't show are kept as they are.
    let nextNotify = notify
    if (shared) {
      nextNotify = notify.filter((n) => !isOwnerNotice(n) && !isAuthorNotice(n))
      if (notifyOwner) nextNotify.push({ on: 'create', to: 'owner' })
      if (notifyAuthor) nextNotify.push({ on: 'update', to: 'author' }, { on: 'delete', to: 'author' })
    }
    const reminder: Remind | null = noReminder
      ? null
      : {
          date_field: dateField,
          time_field: timeFields.some((f) => f.key === timeField) ? timeField : null,
          before_minutes: minutesNumber,
          to: first?.to ?? 'author',
        }
    setBusy(true)
    onBusy()
    try {
      const next = await api.putEvents(botId, c.id, {
        notify: nextNotify,
        remind: reminder ? [reminder, ...otherReminders] : otherReminders,
      })
      haptic('success')
      onSaved(next)
    } catch (e) {
      onFailed(e, labelOf)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <h2>{t('events')}</h2>
      <div className="card">
        {shared && (
          <>
            <label className="toggle">
              {t('notifyOwner')}
              <input type="checkbox" checked={notifyOwner} onChange={(e) => setNotifyOwner(e.target.checked)} />
            </label>
            <label className="toggle">
              {t('notifyAuthor')}
              <input type="checkbox" checked={notifyAuthor} onChange={(e) => setNotifyAuthor(e.target.checked)} />
            </label>
          </>
        )}
        {dateFields.length === 0 && <p className="hint">{t('reminderNeedsDate')}</p>}
        {dateFields.length > 0 && (
          <>
            <strong>{t('reminder')}</strong>
            <label className="toggle">
              {t('noReminder')}
              <input type="checkbox" checked={noReminder} onChange={(e) => setNoReminder(e.target.checked)} />
            </label>
            {!noReminder && (
              <>
                <label htmlFor="remind-date">{t('reminderDate')}</label>
                <select id="remind-date" value={dateField} onChange={(e) => setDateField(e.target.value)}>
                  {dateFields.map((f) => (
                    <option key={f.key} value={f.key}>
                      {titleOf(f.label, lang, f.key)}
                    </option>
                  ))}
                </select>
                {timeFields.length > 0 && (
                  <>
                    <label htmlFor="remind-time">{t('reminderTime')}</label>
                    <select id="remind-time" value={timeField} onChange={(e) => setTimeField(e.target.value)}>
                      <option value="">{t('noTime')}</option>
                      {timeFields.map((f) => (
                        <option key={f.key} value={f.key}>
                          {titleOf(f.label, lang, f.key)}
                        </option>
                      ))}
                    </select>
                  </>
                )}
                <label htmlFor="remind-minutes">{t('reminderMinutes')}</label>
                <input
                  id="remind-minutes"
                  inputMode="numeric"
                  dir="ltr"
                  value={minutes}
                  onChange={(e) => setMinutes(e.target.value)}
                />
                {!minutesOk && <div className="field-error">{t('reminderMinutesHint')}</div>}
              </>
            )}
            {otherReminders.length > 0 && (
              <p className="hint">{t('otherReminders', { n: formatNumber(otherReminders.length, lang) })}</p>
            )}
          </>
        )}
        {(shared || dateFields.length > 0) && (
          <button className="btn block" disabled={busy || !reminderOk} onClick={save}>
            {t('saveEvents')}
          </button>
        )}
        <NoticeLine notice={notice} />
      </div>
    </>
  )
}
