import { useState } from 'react'
import { api, PROVIDER_NAMES, PROVIDERS, type Me, type Provider, type WalletKey } from '../api'
import { useLoad } from '../hooks'
import { formatNumber, useI18n } from '../i18n'
import { haptic, openInvoice } from '../telegram'

/** Credits and payments: balance, today's usage, Stars packages and the user's own provider keys. */
export function CreditsPage({ me, onChange }: { me: Me; onChange: (m: Me) => void }) {
  const { t, lang } = useI18n()
  const [error, setError] = useState<string | null>(null)
  const packages = useLoad(() => api.packages(), [])
  const [buying, setBuying] = useState<string | null>(null)

  const buy = async (packageId: string) => {
    setBuying(packageId)
    setError(null)
    try {
      const { link } = await api.invoice(packageId)
      const status = await openInvoice(link)
      if (status === 'paid') {
        haptic('success')
        // The factory bot records the payment; give it a moment, then refresh the balance.
        setTimeout(() => api.me().then(onChange), 1500)
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBuying(null)
    }
  }

  const quota = (q: Me['messages_today']) =>
    `${formatNumber(q.used, lang)}${q.limit === null ? '' : ` / ${formatNumber(q.limit, lang)}`}`

  return (
    <>
      <h1>{t('tab.credits')}</h1>
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

      <Wallet />

      <h2>{t('buyCredits')}</h2>
      <div className="card">
        <p className="hint">{t('buyHint')}</p>
        {packages.data?.map((p) => (
          <button key={p.id} className="btn block" disabled={buying !== null} onClick={() => buy(p.id)}>
            {t('starsFor', { stars: formatNumber(p.stars, lang), credits: formatNumber(p.credits, lang) })}
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
    </>
  )
}

/** The user's provider keys; each bot they use can answer them with one (its own settings). */
function Wallet() {
  const { t } = useI18n()
  const keys = useLoad(() => api.keys(), [])
  const [provider, setProvider] = useState<Provider>('openrouter')
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)

  const run = async (action: () => Promise<WalletKey[]>) => {
    setError(null)
    try {
      keys.set(await action())
      setApiKey('')
      haptic('success')
    } catch (e) {
      setError((e as Error).message)
      haptic('error')
    }
  }

  return (
    <>
      <h2>{t('wallet')}</h2>
      <div className="card">
        <p className="hint">{t('walletHint')}</p>
        {keys.data?.map((k) => (
          <div key={k.provider} className="row between">
            <span dir="ltr">
              {PROVIDER_NAMES[k.provider]} · {k.key_hint}
            </span>
            <button className="btn danger" onClick={() => run(() => api.deleteKey(k.provider))}>
              {t('delete')}
            </button>
          </div>
        ))}
        <label>{t('byokProvider')}</label>
        <select value={provider} onChange={(e) => setProvider(e.target.value as Provider)}>
          {PROVIDERS.map((p) => (
            <option key={p} value={p}>
              {PROVIDER_NAMES[p]}
            </option>
          ))}
        </select>
        <label>{t('byokKey')}</label>
        <input
          dir="ltr"
          type="password"
          autoComplete="off"
          value={apiKey}
          maxLength={512}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <button className="btn block" disabled={apiKey.trim().length < 8} onClick={() => run(() => api.putKey(provider, apiKey.trim()))}>
          {t('save')}
        </button>
        {error && <p className="error">{error}</p>}
      </div>
    </>
  )
}
