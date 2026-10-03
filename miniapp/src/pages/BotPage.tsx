import { useState } from 'react'
import { api, TOOLS, type BotDetail, type Me, type Task, type TaskAdd, type ToolName } from '../api'
import { formatDateTime, formatNumber, useI18n } from '../i18n'
import { haptic, openTelegramLink } from '../telegram'
import { useLoad } from '../hooks'
import { ErrorBox, Loading } from '../ui'

export function BotPage({ id, me }: { id: number; me: Me }) {
  const { t } = useI18n()
  const bot = useLoad(() => api.bot(id), [id])

  if (bot.error) return <ErrorBox message={bot.error} onRetry={bot.reload} />
  if (!bot.data) return <Loading />
  const data = bot.data

  return (
    <>
      <h1>{data.display_name}</h1>
      <p className="hint" style={{ margin: '0 4px 8px' }}>
        @{data.username}
        {data.status !== 'active' && ` · ${t(`status.${data.status}`)}`}
      </p>
      {!data.owner_started && (
        <div className="card">
          <p>{t('notStarted')}</p>
          <button className="btn secondary" onClick={() => openTelegramLink(`https://t.me/${data.username}?start=init`)}>
            {t('openBot')}
          </button>
        </div>
      )}
      <ProfileForm bot={data} onSaved={bot.set} />
      <Tasks bot={data} me={me} onChange={bot.reload} />
      <Chats bot={data} />
      <Usage id={id} />
    </>
  )
}

function ProfileForm({ bot, onSaved }: { bot: BotDetail; onSaved: (b: BotDetail) => void }) {
  const { t } = useI18n()
  const [form, setForm] = useState({
    display_name: bot.display_name,
    description: bot.description,
    system_prompt: bot.system_prompt,
    language: bot.language,
    tools: bot.tools,
    cap: bot.daily_credit_cap?.toString() ?? '',
  })
  const [status, setStatus] = useState<'idle' | 'saving' | 'saved' | string>('idle')

  const toggleTool = (tool: ToolName) =>
    setForm((f) => ({
      ...f,
      tools: f.tools.includes(tool) ? f.tools.filter((x) => x !== tool) : [...f.tools, tool],
    }))

  const save = async () => {
    setStatus('saving')
    try {
      const cap = form.cap.trim()
      const saved = await api.updateBot(bot.id, {
        display_name: form.display_name,
        description: form.description,
        system_prompt: form.system_prompt,
        language: form.language,
        tools: form.tools,
        ...(cap ? { daily_credit_cap: Number(cap) } : { clear_daily_credit_cap: true }),
      })
      onSaved(saved)
      setStatus('saved')
      haptic('success')
    } catch (e) {
      setStatus((e as Error).message)
      haptic('error')
    }
  }

  return (
    <div className="card">
      <label>{t('name')}</label>
      <input value={form.display_name} maxLength={64} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
      <label>{t('description')}</label>
      <input value={form.description} maxLength={120} onChange={(e) => setForm({ ...form, description: e.target.value })} />
      <label>{t('instructions')}</label>
      <textarea value={form.system_prompt} onChange={(e) => setForm({ ...form, system_prompt: e.target.value })} />
      <label>{t('language')}</label>
      <select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })}>
        <option value="fa">فارسی</option>
        <option value="en">English</option>
      </select>
      <label>{t('tools')}</label>
      {TOOLS.map((tool) => (
        <label key={tool} className="toggle">
          {t(`tool.${tool}`)}
          <input type="checkbox" checked={form.tools.includes(tool)} onChange={() => toggleTool(tool)} />
        </label>
      ))}
      <label>{t('dailyCap')}</label>
      <input inputMode="numeric" value={form.cap} onChange={(e) => setForm({ ...form, cap: e.target.value.replace(/\D/g, '') })} />
      <button className="btn block" disabled={status === 'saving'} onClick={save}>
        {status === 'saved' ? t('saved') : t('save')}
      </button>
      {!['idle', 'saving', 'saved'].includes(status) && <p className="error">{status}</p>}
    </div>
  )
}

function Tasks({ bot, me, onChange }: { bot: BotDetail; me: Me; onChange: () => void }) {
  const { t } = useI18n()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<TaskAdd | null>(null)
  const [error, setError] = useState<string | null>(null)

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChange()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const add = () =>
    run(async () => {
      const added = await api.addTask(bot.id, text)
      setResult(added)
      if (added.scheduled.length) setText('')
    })

  return (
    <>
      <h2>{t('tasks')}</h2>
      {bot.tasks.length === 0 && <p className="hint card">{t('noTasks')}</p>}
      {bot.tasks.map((task) => (
        <TaskCard
          key={task.id}
          task={task}
          bot={bot}
          me={me}
          busy={busy}
          onPause={(paused) => run(() => api.pauseTask(task.id, paused))}
          onDelete={() => run(() => api.deleteTask(task.id))}
        />
      ))}
      <div className="card">
        <textarea
          style={{ minHeight: 60 }}
          placeholder={t('addTaskPlaceholder')}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="btn block" disabled={busy || text.trim().length < 3} onClick={add}>
          {t('add')}
        </button>
        {result?.rejected.map((r) => (
          <p key={r.summary} className="error">
            {t('rejected')}: {r.summary} — {r.reason}
          </p>
        ))}
        {error && <p className="error">{error}</p>}
      </div>
    </>
  )
}

function TaskCard(props: {
  task: Task
  bot: BotDetail
  me: Me
  busy: boolean
  onPause: (paused: boolean) => void
  onDelete: () => void
}) {
  const { task, bot, me, busy } = props
  const { t, lang } = useI18n()
  const chat = bot.chats.find((c) => c.id === task.destination_chat_id)
  const destination = task.to_owner ? t('toOwner') : `${t('toChat')} ${chat?.title ?? task.destination_chat_id}`

  return (
    <div className="card">
      <div className="row between">
        <strong>{task.summary}</strong>
        {task.status !== 'active' && <span className="badge">{t(`status.${task.status}`)}</span>}
      </div>
      <div className="hint">{destination}</div>
      {task.next_run_at && task.status !== 'paused' && (
        <div className="hint">
          {t('next')}: {formatDateTime(task.next_run_at, lang, me.timezone)}
        </div>
      )}
      {task.last_error && (
        <div className="error">
          {t('lastError')}: {task.last_error}
        </div>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button className="btn secondary" disabled={busy} onClick={() => props.onPause(task.status !== 'paused')}>
          {task.status === 'paused' ? t('resume') : t('pause')}
        </button>
        <button className="btn danger" disabled={busy} onClick={props.onDelete}>
          {t('delete')}
        </button>
      </div>
    </div>
  )
}

function Chats({ bot }: { bot: BotDetail }) {
  const { t } = useI18n()
  return (
    <>
      <h2>{t('chats')}</h2>
      <div className="card">
        {bot.chats.length === 0 && <span className="hint">{t('noChats')}</span>}
        {bot.chats.map((chat) => (
          <div key={chat.id} className="stat">
            <span>{chat.title ?? chat.id}</span>
            <span className="hint">{chat.is_admin ? 'admin' : chat.type}</span>
          </div>
        ))}
      </div>
    </>
  )
}

function Usage({ id }: { id: number }) {
  const { t, lang } = useI18n()
  const usage = useLoad(() => api.usage(id, 7), [id])

  if (usage.error) return <ErrorBox message={usage.error} onRetry={usage.reload} />
  // Until loaded, an empty chart would wrongly read as "no usage".
  if (!usage.data) return <Loading />
  const days = usage.data

  const max = Math.max(1, ...days.map((d) => d.credits))
  const total = days.reduce((sum, d) => ({ m: sum.m + d.messages, c: sum.c + d.credits }), { m: 0, c: 0 })

  return (
    <>
      <h2>{t('usage')}</h2>
      <div className="card">
        <div className="bars">
          {days.map((d) => (
            <div key={d.day} title={`${d.day}: ${d.credits}`} style={{ height: `${(d.credits / max) * 100}%` }} />
          ))}
        </div>
        <p className="hint">
          {formatNumber(total.m, lang)} {t('messages')} · {formatNumber(total.c, lang)} {t('credits')}
        </p>
      </div>
    </>
  )
}
