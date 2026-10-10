import { useState } from 'react'
import {
  api,
  TOOLS,
  type BotDetail,
  type Me,
  type ToolName,
} from '../api'
import { formatNumber, useI18n } from '../i18n'
import { confirm, haptic, openTelegramLink } from '../telegram'
import type { Route } from '../App'
import { useLoad } from '../hooks'
import { ErrorBox, Loading } from '../ui'

export function BotPage({ id, me, navigate }: { id: number; me: Me; navigate: (r: Route) => void }) {
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
      <button className="btn secondary block" onClick={() => navigate({ page: 'mine', id: data.id })}>
        {t('mySettingsForBot')}
      </button>
      <button className="btn secondary block" onClick={() => navigate({ page: 'data', botId: data.id, from: 'bot' })}>
        {t('data')}
      </button>
      <ProfileForm bot={data} onSaved={bot.set} />
      <Avatar bot={data} />
      <Access bot={data} me={me} onSaved={bot.set} />
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

/** Uploads a profile photo; the server crops it to a square JPEG for Telegram. */
function Avatar({ bot }: { bot: BotDetail }) {
  const { t } = useI18n()
  const [status, setStatus] = useState<'idle' | 'uploading' | 'done' | string>('idle')

  const upload = async (file: File) => {
    setStatus('uploading')
    try {
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = () => resolve(reader.result as string)
        reader.onerror = () => reject(reader.error)
        reader.readAsDataURL(file)
      })
      await api.setAvatar(bot.id, dataUrl.slice(dataUrl.indexOf(',') + 1))
      setStatus('done')
      haptic('success')
    } catch (e) {
      setStatus((e as Error).message)
      haptic('error')
    }
  }

  return (
    <div className="card">
      <label>{t('avatar')}</label>
      <p className="hint">{t('avatarHint')}</p>
      <input
        type="file"
        accept="image/*"
        disabled={status === 'uploading'}
        onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
      />
      {status === 'uploading' && <p className="hint">{t('loading')}</p>}
      {status === 'done' && <p className="hint">{t('avatarDone')}</p>}
      {!['idle', 'uploading', 'done'].includes(status) && <p className="error">{status}</p>}
    </div>
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
    model_tier: bot.model_tier as 'cheap' | 'strong',
    recommended_model: bot.recommended_model ?? '',
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
      const saved = await api.updateBot(bot.id, {
        display_name: form.display_name,
        description: form.description,
        system_prompt: form.system_prompt,
        language: form.language,
        tools: form.tools,
        model_tier: form.model_tier,
        recommended_model: form.recommended_model.trim(),
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
      <select
        value={form.model_tier}
        onChange={(e) => setForm({ ...form, model_tier: e.target.value as 'cheap' | 'strong' })}
      >
        {(['cheap', 'strong'] as const).map((tier) => (
          <option key={tier} value={tier}>
            {t(`model.${tier}`)}
          </option>
        ))}
      </select>
      <p className="hint">{t('modelTierHint')}</p>
      <label>{t('recommendedModel')}</label>
      <input
        dir="ltr"
        value={form.recommended_model}
        maxLength={128}
        placeholder="claude-sonnet-5.5"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        onChange={(e) => setForm({ ...form, recommended_model: e.target.value })}
      />
      <button className="btn block" disabled={status === 'saving'} onClick={save}>
        {status === 'saved' ? t('saved') : t('save')}
      </button>
      {!['idle', 'saving', 'saved'].includes(status) && <p className="error">{status}</p>}
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
