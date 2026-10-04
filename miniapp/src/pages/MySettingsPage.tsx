import { useState } from 'react'
import { api, ApiError, MODEL_EXAMPLES, PROVIDER_NAMES, type MySettings, type Provider } from '../api'
import type { Route } from '../App'
import { useLoad } from '../hooks'
import { useI18n } from '../i18n'
import { haptic } from '../telegram'
import { ErrorBox, Loading } from '../ui'

/** The user's own settings for one bot they use: their key and model, and a daily cap. */
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
  const [cap, setCap] = useState(settings.daily_credit_cap?.toString() ?? '')
  const [status, setStatus] = useState<'idle' | 'saving' | 'saved' | string>('idle')

  const save = async () => {
    setStatus('saving')
    try {
      onSaved(
        await api.putMine(settings.bot_id, {
          key_provider: provider || null,
          model: provider ? model.trim() : null,
          daily_credit_cap: cap ? Number(cap) : null,
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
        <label>{t('dailyCap')}</label>
        <input inputMode="numeric" value={cap} onChange={(e) => setCap(e.target.value.replace(/\D/g, ''))} />
        <button
          className="btn block"
          disabled={status === 'saving' || (provider !== '' && !model.trim())}
          onClick={save}
        >
          {status === 'saving' ? t('byokTesting') : status === 'saved' ? t('saved') : t('save')}
        </button>
        {!['idle', 'saving', 'saved'].includes(status) && <p className="error">{status}</p>}
      </div>
      {settings.api_keys.length > 0 && <ApiKeys settings={settings} onSaved={onSaved} />}
    </>
  )
}

/** The user's own values for the keys this bot's APIs need; never shown again once saved. */
function ApiKeys({ settings, onSaved }: { settings: MySettings; onSaved: (s: MySettings) => void }) {
  const { t } = useI18n()
  const [values, setValues] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)

  const run = async (action: () => Promise<MySettings>) => {
    setError(null)
    try {
      onSaved(await action())
      setValues({})
      haptic('success')
    } catch (e) {
      setError((e as Error).message)
      haptic('error')
    }
  }

  return (
    <>
      <h2>{t('apiKeys')}</h2>
      <div className="card">
        <p className="hint">{t('apiKeysHint')}</p>
        {settings.api_keys.map((k) => (
          <div key={k.name}>
            <label dir="ltr">
              {k.name} {k.is_set ? '✅' : ''}
            </label>
            <div className="row">
              <input
                dir="ltr"
                type="password"
                autoComplete="off"
                placeholder={k.is_set ? t('apiKeyIsSet') : ''}
                value={values[k.name] ?? ''}
                onChange={(e) => setValues({ ...values, [k.name]: e.target.value })}
              />
              <button
                className="btn"
                disabled={!(values[k.name] ?? '').trim()}
                onClick={() => run(() => api.putApiKey(settings.bot_id, k.name, values[k.name].trim()))}
              >
                {t('save')}
              </button>
              {k.is_set && (
                <button className="btn danger" onClick={() => run(() => api.deleteApiKey(settings.bot_id, k.name))}>
                  {t('delete')}
                </button>
              )}
            </div>
          </div>
        ))}
        {error && <p className="error">{error}</p>}
      </div>
    </>
  )
}
