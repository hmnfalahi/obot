import { useState } from 'react'
import { api, type Me, type UsedBot } from '../api'
import type { Route } from '../App'
import { formatNumber, useI18n } from '../i18n'
import { useLoad } from '../hooks'
import { confirm, haptic } from '../telegram'
import { ErrorBox, Loading } from '../ui'

export function BotsPage({ me, navigate }: { me: Me; navigate: (r: Route) => void }) {
  const { t, lang } = useI18n()
  const bots = useLoad(() => api.bots(), [])
  const used = useLoad(() => api.usedBots(), [])
  const quota = me.messages_today

  return (
    <>
      <div className="row between">
        <h1>{t('myBots')}</h1>
        <div className="row">
          <button className="btn secondary" onClick={() => navigate({ page: 'tasks' })}>
            {t('myTasks')}
          </button>
          <button className="btn secondary" onClick={() => navigate({ page: 'settings' })}>
            {t('settings')}
          </button>
        </div>
      </div>

      <div className="card hint">
        {t('messagesToday')}: {formatNumber(quota.used, lang)}
        {quota.limit !== null && ` / ${formatNumber(quota.limit, lang)}`}
      </div>

      <h2>{t('madeByMe')}</h2>
      {bots.error && <ErrorBox message={bots.error} onRetry={bots.reload} />}
      {!bots.data && !bots.error && <Loading />}
      {bots.data?.length === 0 && <p className="hint">{t('noBots')}</p>}
      {bots.data?.map((bot) => (
        <button key={bot.id} className="list-item" onClick={() => navigate({ page: 'bot', id: bot.id })}>
          <div className="row between">
            <strong>{bot.display_name}</strong>
            {bot.status !== 'active' && <span className="badge warn">{t(`status.${bot.status}`)}</span>}
          </div>
          <div className="hint">@{bot.username}</div>
          {bot.description && <div>{bot.description}</div>}
        </button>
      ))}

      <button className="btn block" onClick={() => navigate({ page: 'new' })}>
        + {t('newBot')}
      </button>

      {used.data && used.data.length > 0 && (
        <>
          <h2>{t('usedBots')}</h2>
          <p className="hint">{t('usedBotsHint')}</p>
          {used.data.map((bot) => (
            <UsedBotItem key={bot.id} bot={bot} onRemoved={used.reload} navigate={navigate} />
          ))}
        </>
      )}
    </>
  )
}

function UsedBotItem({
  bot,
  onRemoved,
  navigate,
}: {
  bot: UsedBot
  onRemoved: () => void
  navigate: (r: Route) => void
}) {
  const { t } = useI18n()
  const [error, setError] = useState<string | null>(null)

  const remove = async () => {
    if (!(await confirm(t('removeUsedConfirm', { username: bot.username })))) return
    try {
      await api.removeUsedBot(bot.id)
      haptic('success')
      onRemoved()
    } catch (e) {
      setError((e as Error).message)
      haptic('error')
    }
  }

  return (
    <div className="list-item">
      <div className="row between">
        <strong>{bot.display_name}</strong>
        <div className="row">
          <button className="btn secondary" onClick={() => navigate({ page: 'mine', id: bot.id })}>
            {t('mySettings')}
          </button>
          <button className="btn secondary" onClick={remove}>
            {t('removeFromList')}
          </button>
        </div>
      </div>
      <div className="hint">@{bot.username}</div>
      {bot.description && <div>{bot.description}</div>}
      {error && <p className="error">{error}</p>}
    </div>
  )
}
