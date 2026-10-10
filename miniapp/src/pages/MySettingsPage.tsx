import { useState } from 'react'
import { api, ApiError, MODEL_EXAMPLES, PROVIDER_NAMES, type MySettings, type Provider } from '../api'
import type { Route } from '../App'
import { useLoad } from '../hooks'
import { useI18n } from '../i18n'
import { haptic } from '../telegram'
import { ErrorBox, Loading } from '../ui'

/** The user's own settings for one bot they use: their key and model. */
export function MySettingsPage({ id, navigate }: { id: number; navigate: (r: Route) => void }) {
  const mine = useLoad(() => api.mine(id), [id])

  if (mine.error) return <ErrorBox message={mine.error} onRetry={mine.reload} />
  if (!mine.data) return <Loading />
  return <Form key={mine.data.bot_id} settings={mine.data} navigate={navigate} onSaved={mine.set} />
}

function Form({
  settings,
  navigate,
  onSaved,
}: {
  settings: MySettings
  navigate: (r: Route) => void
  onSaved: (s: MySettings) => void
}) {
  const { t } = useI18n()
  const [provider, setProvider] = useState<Provider | ''>(settings.key_provider ?? '')
  const [model, setModel] = useState(settings.model ?? settings.recommended_model ?? '')
  const [status, setStatus] = useState<'idle' | 'saving' | 'saved' | string>('idle')

  const save = async () => {
    setStatus('saving')
    try {
      onSaved(
        await api.putMine(settings.bot_id, {
          key_provider: provider || null,
          model: provider ? model.trim() : null,
        }),
      )
      setStatus('saved')
      haptic('success')
    } catch (e) {
      const reason = e instanceof ApiError && e.status === 422 ? `byokError.${e.message}` : null
      setStatus(reason && t(reason) !== reason ? t(reason) : (e as Error).message)
      haptic('error')
    }
  }

  return (
    <>
      <h1>{settings.display_name}</h1>
      <p className="hint" style={{ margin: '0 4px 8px' }}>
        <span dir="ltr">@{settings.username}</span> · {t('mySettingsHint')}
      </p>
      <div className="card">
        <p className="hint">
          {t('recommendedBy', { tier: t(`model.${settings.model_tier}`) })}
          {settings.recommended_model && (
            <>
              {' · '}
              <span dir="ltr">{settings.recommended_model}</span>
            </>
          )}
        </p>
        <label>{t('answerWith')}</label>
        <select value={provider} onChange={(e) => setProvider(e.target.value as Provider | '')}>
          <option value="">{t('obotModels')}</option>
          {settings.wallet.map((p) => (
            <option key={p} value={p}>
              {t('ownKeyOf', { provider: PROVIDER_NAMES[p] })}
            </option>
          ))}
        </select>
        {settings.wallet.length === 0 && (
          <p className="hint">
            {t('walletEmptyHint')}{' '}
            <button className="btn secondary" onClick={() => navigate({ page: 'settings' })}>
              {t('openWallet')}
            </button>
          </p>
        )}
        {provider && (
          <>
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
          </>
        )}
        <button
          className="btn block"
          disabled={status === 'saving' || (provider !== '' && !model.trim())}
          onClick={save}
        >
          {status === 'saving' ? t('byokTesting') : status === 'saved' ? t('saved') : t('save')}
        </button>
        {!['idle', 'saving', 'saved'].includes(status) && <p className="error">{status}</p>}
      </div>
    </>
  )
}
