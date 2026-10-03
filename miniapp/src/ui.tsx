import { useI18n } from './i18n'

export function Loading() {
  const { t } = useI18n()
  return <p className="hint">{t('loading')}</p>
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const { t } = useI18n()
  return (
    <div className="card">
      <p className="error">
        {t('error')}: {message}
      </p>
      {onRetry && (
        <button className="btn secondary" onClick={onRetry}>
          {t('retry')}
        </button>
      )}
    </div>
  )
}
