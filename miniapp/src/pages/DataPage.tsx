import { api } from '../api'
import type { Route } from '../App'
import { titleOf, useI18n } from '../i18n'
import { useLoad } from '../hooks'
import { ErrorBox, Loading } from '../ui'

/** The lists (collections) a bot keeps that this user can see. */
export function DataPage({ botId, navigate }: { botId: number; navigate: (r: Route) => void }) {
  const { t, lang } = useI18n()
  const collections = useLoad(() => api.collections(botId), [botId])

  return (
    <>
      <h1>{t('data')}</h1>
      <p className="hint" style={{ margin: '0 4px 8px' }}>
        {t('dataHint')}
      </p>
      {collections.error && <ErrorBox message={collections.error} onRetry={collections.reload} />}
      {!collections.data && !collections.error && <Loading />}
      {collections.data?.length === 0 && <p className="hint card">{t('noCollections')}</p>}
      {collections.data?.map((c) => (
        <button
          key={c.id}
          className="list-item"
          onClick={() => navigate({ page: 'collection', botId, collectionId: c.id })}
        >
          <div className="row between">
            <strong>{titleOf(c.title, lang, c.key)}</strong>
            <div className="row">
              {c.mine && <span className="badge">{t('mineBadge')}</span>}
              <span className="badge">{t(`scope.${c.scope}`)}</span>
            </div>
          </div>
        </button>
      ))}
    </>
  )
}
