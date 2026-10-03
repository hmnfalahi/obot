import { useState } from 'react'
import { api, type Lang, type Me } from '../api'
import { formatNumber, useI18n } from '../i18n'

const TIMEZONES = [
  'Asia/Tehran',
  'UTC',
  'Europe/Istanbul',
  'Asia/Dubai',
  'Europe/Berlin',
  'Europe/London',
  'America/New_York',
  'America/Los_Angeles',
  'Asia/Kabul',
  'Asia/Tokyo',
]

export function SettingsPage({ me, onChange }: { me: Me; onChange: (m: Me) => void }) {
  const { t, lang } = useI18n()
  const [error, setError] = useState<string | null>(null)
  const zones = TIMEZONES.includes(me.timezone) ? TIMEZONES : [me.timezone, ...TIMEZONES]

  const update = async (body: { language?: Lang; timezone?: string }) => {
    setError(null)
    try {
      onChange(await api.updateMe(body))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const quota = (q: Me['messages_today']) =>
    `${formatNumber(q.used, lang)}${q.limit === null ? '' : ` / ${formatNumber(q.limit, lang)}`}`

  return (
    <>
      <h1>{t('settings')}</h1>
      <div className="card">
        <label>{t('appLanguage')}</label>
        <select value={me.language} onChange={(e) => update({ language: e.target.value as Lang })}>
          <option value="fa">فارسی</option>
          <option value="en">English</option>
        </select>
        <label>{t('timezone')}</label>
        <select value={me.timezone} onChange={(e) => update({ timezone: e.target.value })}>
          {zones.map((z) => (
            <option key={z} value={z}>
              {z}
            </option>
          ))}
        </select>
        {error && <p className="error">{error}</p>}
      </div>

      <div className="card">
        <div className="stat">
          <span>{t('plan')}</span>
          <strong>{me.plan}</strong>
        </div>
        <div className="stat">
          <span>{t('creditBalance')}</span>
          <strong>{formatNumber(me.credit_balance, lang)}</strong>
        </div>
        <div className="stat">
          <span>{t('messagesToday')}</span>
          <strong>{quota(me.messages_today)}</strong>
        </div>
        <div className="stat">
          <span>{t('searchesToday')}</span>
          <strong>{quota(me.searches_today)}</strong>
        </div>
      </div>
    </>
  )
}
