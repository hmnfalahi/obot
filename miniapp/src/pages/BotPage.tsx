import { useState } from 'react'
import {
  api,
  ApiError,
  METHODS,
  PROVIDERS,
  TOOLS,
  type ApiEndpoint,
  type BotDetail,
  type HttpMethod,
  type Me,
  type Provider,
  type Task,
  type TaskAdd,
  type ToolName,
} from '../api'
import { formatDateTime, formatNumber, useI18n } from '../i18n'
import { confirm, haptic, openTelegramLink } from '../telegram'
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
      <ProfileForm bot={data} me={me} onSaved={bot.set} />
      <Access bot={data} me={me} onSaved={bot.set} />
      <OwnKey bot={data} onSaved={bot.set} />
      <Apis bot={data} me={me} onSaved={bot.set} />
      <Tasks bot={data} me={me} onChange={bot.reload} />
      <Chats bot={data} />
      <Usage id={id} />
    </>
  )
}

function Access({ bot, me, onSaved }: { bot: BotDetail; me: Me; onSaved: (b: BotDetail) => void }) {
  const { t, lang } = useI18n()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const update = async (body: Parameters<typeof api.updateBot>[1]) => {
    setBusy(true)
    setError(null)
    try {
      onSaved(await api.updateBot(bot.id, body))
      haptic('success')
    } catch (e) {
      setError((e as Error).message)
      haptic('error')
    } finally {
      setBusy(false)
    }
  }

  const makePublic = async () => {
    if (await confirm(t('publicConfirm'))) await update({ visibility: 'public' })
  }

  return (
    <div className="card">
      <div className="row between">
        <strong>{t('access')}</strong>
        <span className="badge">{t(`visibility.${bot.visibility}`)}</span>
      </div>
      <p className="hint">{t(`visibilityHint.${bot.visibility}`)}</p>
      <p className="hint">{t('botStats', { users: formatNumber(bot.users_count, lang), groups: formatNumber(bot.groups_count, lang) })}</p>
      {bot.visibility === 'private' ? (
        <>
          <label>{t('allowedUsers')}</label>
          {bot.allowed_users.length === 0 && <p className="hint">{t('noAllowedUsers')}</p>}
          {bot.allowed_users.map((u) => (
            <div key={u.id} className="row between">
              <span>{u.name}</span>
              <button
                className="btn secondary"
                disabled={busy}
                onClick={() => update({ allowed_user_ids: bot.allowed_users.filter((x) => x.id !== u.id).map((x) => x.id) })}
              >
                {t('delete')}
              </button>
            </div>
          ))}
          <button
            className="btn secondary block"
            onClick={() => openTelegramLink(`https://t.me/${me.factory_username}?start=access_${bot.id}`)}
          >
            {t('chooseAllowedUsers')}
          </button>
          <button className="btn block" disabled={busy} onClick={makePublic}>
            {t('makePublic')}
          </button>
        </>
      ) : (
        <button className="btn secondary block" disabled={busy} onClick={() => update({ visibility: 'private' })}>
          {t('makePrivate')}
        </button>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  )
}

function ProfileForm({ bot, me, onSaved }: { bot: BotDetail; me: Me; onSaved: (b: BotDetail) => void }) {
  const { t } = useI18n()
  const [form, setForm] = useState({
    display_name: bot.display_name,
    description: bot.description,
    system_prompt: bot.system_prompt,
    language: bot.language,
    tools: bot.tools,
    model_tier: bot.model_tier as 'cheap' | 'strong',
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
        ...(me.model_tiers.includes(form.model_tier) ? { model_tier: form.model_tier } : {}),
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
      <label>{t('model')}</label>
      {bot.byok ? (
        <p className="hint">{t('modelByByok', { model: bot.byok.model })}</p>
      ) : (
        <select
          value={form.model_tier}
          onChange={(e) => setForm({ ...form, model_tier: e.target.value as 'cheap' | 'strong' })}
        >
          {(['cheap', 'strong'] as const).map((tier) => (
            <option key={tier} value={tier} disabled={!me.model_tiers.includes(tier)}>
              {t(`model.${tier}`)}
            </option>
          ))}
        </select>
      )}
      <label>{t('dailyCap')}</label>
      <input inputMode="numeric" value={form.cap} onChange={(e) => setForm({ ...form, cap: e.target.value.replace(/\D/g, '') })} />
      <button className="btn block" disabled={status === 'saving'} onClick={save}>
        {status === 'saved' ? t('saved') : t('save')}
      </button>
      {!['idle', 'saving', 'saved'].includes(status) && <p className="error">{status}</p>}
    </div>
  )
}

const PROVIDER_NAMES: Record<Provider, string> = {
  openrouter: 'OpenRouter',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
}

const MODEL_EXAMPLES: Record<Provider, string> = {
  openrouter: 'anthropic/claude-sonnet-5.5',
  openai: 'gpt-5-mini',
  anthropic: 'claude-haiku-4-5',
}

function OwnKey({ bot, onSaved }: { bot: BotDetail; onSaved: (b: BotDetail) => void }) {
  const { t } = useI18n()
  const current = bot.byok
  const [editing, setEditing] = useState(false)
  const [provider, setProvider] = useState<Provider>(current?.provider ?? 'openrouter')
  const [model, setModel] = useState(current?.model ?? '')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The stored key can be kept when only the model changes.
  const canKeepKey = current?.provider === provider

  const run = async (action: () => Promise<BotDetail>) => {
    setBusy(true)
    setError(null)
    try {
      onSaved(await action())
      setEditing(false)
      setApiKey('')
      haptic('success')
    } catch (e) {
      const reason = e instanceof ApiError && e.status === 422 ? `byokError.${e.message}` : null
      setError(reason && t(reason) !== reason ? t(reason) : (e as Error).message)
      haptic('error')
    } finally {
      setBusy(false)
    }
  }

  const save = () =>
    run(() =>
      api.setByok(bot.id, {
        provider,
        model: model.trim(),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      }),
    )

  const remove = () => {
    setModel('')
    return run(() => api.deleteByok(bot.id))
  }

  return (
    <>
      <h2>{t('byok')}</h2>
      <div className="card">
        <p className="hint">{t('byokHint')}</p>
        {current && !editing ? (
          <>
            <p dir="ltr">
              {t('byokActive', {
                provider: PROVIDER_NAMES[current.provider],
                model: current.model,
                hint: current.key_hint,
              })}
            </p>
            <div className="row">
              <button className="btn secondary" disabled={busy} onClick={() => setEditing(true)}>
                {t('byokChange')}
              </button>
              <button className="btn danger" disabled={busy} onClick={remove}>
                {t('byokRemove')}
              </button>
            </div>
          </>
        ) : (
          <>
            <label>{t('byokProvider')}</label>
            <select value={provider} onChange={(e) => setProvider(e.target.value as Provider)}>
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {PROVIDER_NAMES[p]}
                </option>
              ))}
            </select>
            <label>{t('byokModel')}</label>
            <input
              dir="ltr"
              value={model}
              maxLength={128}
              placeholder={MODEL_EXAMPLES[provider]}
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              onChange={(e) => setModel(e.target.value)}
            />
            <label>{t('byokKey')}</label>
            <input
              dir="ltr"
              type="password"
              autoComplete="off"
              value={apiKey}
              maxLength={512}
              placeholder={canKeepKey ? t('byokKeepKey', { hint: current!.key_hint }) : ''}
              onChange={(e) => setApiKey(e.target.value)}
            />
            <div className="row">
              <button
                className="btn"
                disabled={busy || !model.trim() || (!apiKey.trim() && !canKeepKey)}
                onClick={save}
              >
                {busy ? t('byokTesting') : t('byokSave')}
              </button>
              {current && (
                <button className="btn secondary" disabled={busy} onClick={() => setEditing(false)}>
                  {t('cancel')}
                </button>
              )}
            </div>
          </>
        )}
        {error && <p className="error">{error}</p>}
      </div>
    </>
  )
}

function Apis({ bot, me, onSaved }: { bot: BotDetail; me: Me; onSaved: (b: BotDetail) => void }) {
  const { t } = useI18n()
  // null: no form open; 'new': adding; a number: editing that API
  const [open, setOpen] = useState<number | 'new' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const atLimit = bot.apis.length >= me.max_apis

  const remove = async (id: number) => {
    setError(null)
    try {
      onSaved(await api.deleteApi(id))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <>
      <h2>{t('apis')}</h2>
      <div className="card">
        <p className="hint">{t('apisHint')}</p>
      </div>
      {bot.apis.map((item) =>
        open === item.id ? (
          <ApiForm key={item.id} bot={bot} item={item} onDone={(b) => { if (b) onSaved(b); setOpen(null) }} />
        ) : (
          <div key={item.id} className="card">
            <div className="row between">
              <strong dir="ltr">{item.name}</strong>
              <span className="badge">{item.method}</span>
            </div>
            <div className="hint" dir="ltr" style={{ wordBreak: 'break-all' }}>{item.url}</div>
            <div className="hint">{item.description}</div>
            {item.header_names.length > 0 && (
              <div className="hint">{t('apiStoredHeaders', { names: item.header_names.join(', ') })}</div>
            )}
            <div className="row" style={{ marginTop: 8 }}>
              <button className="btn secondary" onClick={() => setOpen(item.id)}>{t('edit')}</button>
              <button className="btn danger" onClick={() => remove(item.id)}>{t('delete')}</button>
            </div>
          </div>
        ),
      )}
      {open === 'new' ? (
        <ApiForm bot={bot} item={null} onDone={(b) => { if (b) onSaved(b); setOpen(null) }} />
      ) : (
        <div className="card">
          {atLimit ? (
            <p className="hint">{t('apiLimit', { limit: me.max_apis })}</p>
          ) : (
            <button className="btn secondary block" onClick={() => setOpen('new')}>{t('addApi')}</button>
          )}
          {error && <p className="error">{error}</p>}
        </div>
      )}
    </>
  )
}

function ApiForm({
  bot,
  item,
  onDone,
}: {
  bot: BotDetail
  item: ApiEndpoint | null
  onDone: (saved: BotDetail | null) => void
}) {
  const { t } = useI18n()
  const [form, setForm] = useState({
    name: item?.name ?? '',
    description: item?.description ?? '',
    method: item?.method ?? ('GET' as HttpMethod),
    url: item?.url ?? '',
  })
  // Stored header values are secret: editing replaces all of them.
  const [replaceHeaders, setReplaceHeaders] = useState(item === null)
  const [headers, setHeaders] = useState<{ key: string; value: string }[]>(
    item ? item.header_names.map((key) => ({ key, value: '' })) : [],
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const setHeader = (i: number, field: 'key' | 'value', value: string) =>
    setHeaders((hs) => hs.map((h, j) => (j === i ? { ...h, [field]: value } : h)))

  const save = async () => {
    setBusy(true)
    setError(null)
    const body = {
      ...form,
      ...(replaceHeaders
        ? { headers: Object.fromEntries(headers.filter((h) => h.key.trim()).map((h) => [h.key.trim(), h.value])) }
        : {}),
    }
    try {
      onDone(await (item ? api.updateApi(item.id, body) : api.addApi(bot.id, body)))
      haptic('success')
    } catch (e) {
      setError((e as Error).message)
      haptic('error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <label>{t('apiName')}</label>
      <input
        dir="ltr"
        value={form.name}
        maxLength={40}
        autoCapitalize="off"
        spellCheck={false}
        onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })}
      />
      <label>{t('apiDescription')}</label>
      <input value={form.description} maxLength={500} onChange={(e) => setForm({ ...form, description: e.target.value })} />
      <label>{t('apiMethod')}</label>
      <select value={form.method} onChange={(e) => setForm({ ...form, method: e.target.value as HttpMethod })}>
        {METHODS.map((m) => (
          <option key={m} value={m}>{m}</option>
        ))}
      </select>
      <label>{t('apiUrl')}</label>
      <input
        dir="ltr"
        value={form.url}
        maxLength={2048}
        autoCapitalize="off"
        spellCheck={false}
        placeholder="https://"
        onChange={(e) => setForm({ ...form, url: e.target.value })}
      />
      <label>{t('apiHeaders')}</label>
      {item && (
        <label className="toggle">
          {t('apiChangeHeaders')}
          <input type="checkbox" checked={replaceHeaders} onChange={(e) => setReplaceHeaders(e.target.checked)} />
        </label>
      )}
      {replaceHeaders && (
        <>
          {headers.map((h, i) => (
            <div key={i} className="row">
              <input dir="ltr" placeholder={t('apiHeaderName')} value={h.key} onChange={(e) => setHeader(i, 'key', e.target.value)} />
              <input
                dir="ltr"
                type="password"
                autoComplete="off"
                placeholder={t('apiHeaderValue')}
                value={h.value}
                onChange={(e) => setHeader(i, 'value', e.target.value)}
              />
            </div>
          ))}
          <button className="btn secondary" onClick={() => setHeaders([...headers, { key: '', value: '' }])}>
            {t('apiAddHeader')}
          </button>
        </>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button
          className="btn"
          disabled={busy || !form.name.trim() || !form.description.trim() || !form.url.trim()}
          onClick={save}
        >
          {t('save')}
        </button>
        <button className="btn secondary" disabled={busy} onClick={() => onDone(null)}>
          {t('cancel')}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
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
      {task.steps.length > 0 && (
        <div className="hint">
          {t('chainSteps')}
          <ol style={{ margin: '4px 0', paddingInlineStart: 20 }}>
            {task.steps.map((step, i) => (
              <li key={i}>
                <span dir="ltr">{step.bot_username ? `@${step.bot_username}` : t('thisBot')}</span>: {step.instruction}
              </li>
            ))}
          </ol>
        </div>
      )}
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
