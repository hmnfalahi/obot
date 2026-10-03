import { useState } from 'react'
import { api, TOOLS, type ToolName } from '../api'
import { useI18n } from '../i18n'
import { haptic, openTelegramLink } from '../telegram'

export function NewBotPage({ onDone }: { onDone: () => void }) {
  const { t, lang } = useI18n()
  const [form, setForm] = useState({
    name: '',
    description: '',
    system_prompt: '',
    language: lang as string,
    tools: [] as ToolName[],
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggleTool = (tool: ToolName) =>
    setForm((f) => ({
      ...f,
      tools: f.tools.includes(tool) ? f.tools.filter((x) => x !== tool) : [...f.tools, tool],
    }))

  const create = async () => {
    setBusy(true)
    setError(null)
    try {
      const { create_link } = await api.newBot(form)
      haptic('success')
      // Telegram's own dialog creates the bot; the factory then applies this draft.
      openTelegramLink(create_link)
      onDone()
    } catch (e) {
      setError((e as Error).message)
      haptic('error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <h1>{t('newBot')}</h1>
      <div className="card">
        <label>{t('name')}</label>
        <input value={form.name} maxLength={64} onChange={(e) => setForm({ ...form, name: e.target.value })} />
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
        <button className="btn block" disabled={busy || !form.name.trim() || !form.system_prompt.trim()} onClick={create}>
          {t('createInTelegram')}
        </button>
        <p className="hint">{t('createHint')}</p>
        {error && <p className="error">{error}</p>}
      </div>
    </>
  )
}
