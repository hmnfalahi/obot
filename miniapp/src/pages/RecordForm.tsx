import { useState } from 'react'
import { api, dataErrorOf, type DataCollection, type DataRecord, type FieldDef } from '../api'
import { formatDate, localDateTime, titleOf, useI18n } from '../i18n'
import { confirm, haptic } from '../telegram'

type Value = string | boolean

/** Error codes with their own text; others read as dataError.other. */
const KNOWN_ERRORS = ['duplicate', 'outside_allowed_hours', 'invalid', 'limit_reached', 'stale', 'not_allowed']

function initialValues(c: DataCollection, record: DataRecord | null): Record<string, Value> {
  const values: Record<string, Value> = {}
  for (const f of c.fields) {
    if (f.hidden) continue
    const v = record?.data[f.key]
    if (f.type === 'boolean') values[f.key] = v === true
    else if (v === null || v === undefined) values[f.key] = ''
    else if (f.type === 'datetime') values[f.key] = localDateTime(String(v), c.timezone)
    else values[f.key] = String(v)
  }
  return values
}

/** Adds a record, or edits or deletes `record`; the server checks every rule. */
export function RecordForm(props: {
  botId: number
  collection: DataCollection
  record: DataRecord | null
  onSaved: () => void
  /** `notice` explains why the form closed by itself. */
  onCancel: (notice?: string) => void
  onDeleted: () => void
}) {
  const { botId, collection: c, record } = props
  const { t, lang } = useI18n()
  const [initial] = useState(() => initialValues(c, record))
  const [values, setValues] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const fields = c.fields.filter((f) => !f.hidden)
  const labelOf = (key: string) => {
    const f = c.fields.find((x) => x.key === key)
    return f ? titleOf(f.label, lang, f.key) : key
  }

  const fail = (e: unknown) => {
    haptic('error')
    const detail = dataErrorOf(e)
    if (!detail) {
      setError((e as Error).message)
      return
    }
    const code = KNOWN_ERRORS.includes(detail.code) ? detail.code : 'other'
    const message = t(`dataError.${code}`, { fields: detail.fields.map(labelOf).join(' + ') })
    // A stale record closes the form; the parent reloads and shows why.
    if (code === 'stale') return props.onCancel(message)
    setError(message)
    setFieldErrors(Object.fromEntries(detail.errors.map((x) => [x.field, x.reason])))
  }

  const run = async (action: () => Promise<unknown>, done: () => void) => {
    setBusy(true)
    setError(null)
    setFieldErrors({})
    try {
      await action()
      haptic('success')
      done()
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  const save = () => {
    if (!record) {
      const data = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ''))
      return run(() => api.addRecord(botId, c.id, data), props.onSaved)
    }
    const changes = Object.fromEntries(
      Object.entries(values)
        .filter(([k, v]) => v !== initial[k])
        .map(([k, v]) => [k, v === '' ? null : v]),
    )
    if (Object.keys(changes).length === 0) return props.onCancel()
    return run(() => api.updateRecord(botId, c.id, record.id, changes, record.version), props.onSaved)
  }

  const remove = async () => {
    if (!record || !(await confirm(t('deleteRecordConfirm')))) return
    await run(() => api.deleteRecord(botId, c.id, record.id), props.onDeleted)
  }

  return (
    <div className="card">
      <strong>{record ? t('editRecord') : t('addRecord')}</strong>
      {fields.map((f) => (
        <div key={f.key}>
          <FieldInput
            field={f}
            value={values[f.key]}
            onChange={(v) => setValues({ ...values, [f.key]: v })}
          />
          {fieldErrors[f.key] && (
            <div className="field-error" dir="ltr">
              {fieldErrors[f.key]}
            </div>
          )}
        </div>
      ))}
      {error && <p className="error">{error}</p>}
      {Object.entries(fieldErrors)
        .filter(([key]) => !fields.some((f) => f.key === key))
        .map(([key, reason]) => (
          <div key={key} className="field-error" dir="ltr">
            {key ? `${key}: ${reason}` : reason}
          </div>
        ))}
      <div className="row" style={{ marginTop: 12 }}>
        <button className="btn" disabled={busy} onClick={save}>
          {t('save')}
        </button>
        <button className="btn secondary" disabled={busy} onClick={() => props.onCancel()}>
          {t('cancel')}
        </button>
        {record && (
          <button className="btn danger" disabled={busy} onClick={remove}>
            {t('deleteRecord')}
          </button>
        )}
      </div>
    </div>
  )
}

function FieldInput({ field: f, value, onChange }: { field: FieldDef; value: Value; onChange: (v: Value) => void }) {
  const { t, lang } = useI18n()
  const label = titleOf(f.label, lang, f.key)
  const text = typeof value === 'string' ? value : ''
  const set = (e: { target: { value: string } }) => onChange(e.target.value)

  if (f.type === 'boolean') {
    return (
      <label className="toggle">
        {label}
        <input type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />
      </label>
    )
  }

  let input
  switch (f.type) {
    case 'phone':
      input = <input type="tel" dir="ltr" value={text} onChange={set} />
      break
    case 'number':
    case 'money':
      input = <input inputMode="decimal" value={text} onChange={set} />
      break
    case 'date':
      input = <input type="date" value={text} onChange={set} />
      break
    case 'time':
      input = <input type="time" value={text} onChange={set} />
      break
    case 'datetime':
      input = <input type="datetime-local" value={text} onChange={set} />
      break
    case 'choice':
      input = (
        <select value={text} onChange={set}>
          <option value="" />
          {f.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      )
      break
    default:
      input = <input value={text} onChange={set} />
  }

  return (
    <>
      <label>
        {label}
        {f.type === 'money' && f.currency && ` (${f.currency})`}
        {f.required && ` · ${t('required')}`}
      </label>
      {input}
      {f.type === 'date' && lang === 'fa' && /^\d{4}-\d{2}-\d{2}$/.test(text) && (
        <div className="hint">{formatDate(text, lang)}</div>
      )}
    </>
  )
}
