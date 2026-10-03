import { api, type Me } from '../api'
import type { Route } from '../App'
import { formatNumber, useI18n } from '../i18n'
import { useLoad } from '../hooks'
import { ErrorBox, Loading } from '../ui'

export function BotsPage({ me, navigate }: { me: Me; navigate: (r: Route) => void }) {
  const { t, lang } = useI18n()
  const bots = useLoad(() => api.bots(), [])
  const quota = me.messages_today

  return (
    <>
      <div className="row between">
        <h1>{t('myBots')}</h1>
        <button className="btn secondary" onClick={() => navigate({ page: 'settings' })}>
          {t('settings')}
        </button>
      </div>

      <div className="card hint">
        {t('messagesToday')}: {formatNumber(quota.used, lang)}
        {quota.limit !== null && ` / ${formatNumber(quota.limit, lang)}`}
      </div>

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
    </>
  )
}
