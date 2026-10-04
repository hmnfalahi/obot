import { useState } from 'react'
import { api, type Me, type Task } from '../api'
import { formatDateTime, useI18n } from '../i18n'
import { useLoad } from '../hooks'
import { haptic } from '../telegram'
import { ErrorBox, Loading } from '../ui'

/** The user's own scheduled tasks on every bot; they make them by asking a bot in private. */
export function TasksPage({ me }: { me: Me }) {
  const { t } = useI18n()
  const tasks = useLoad(() => api.tasks(), [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      haptic('success')
      tasks.reload()
    } catch (e) {
      setError((e as Error).message)
      haptic('error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <h1>{t('myTasks')}</h1>
      <p className="hint" style={{ margin: '0 4px 8px' }}>
        {t('myTasksHint')}
      </p>
      {tasks.error && <ErrorBox message={tasks.error} onRetry={tasks.reload} />}
      {!tasks.data && !tasks.error && <Loading />}
      {tasks.data?.length === 0 && <p className="hint card">{t('noTasks')}</p>}
      {tasks.data?.map((task) => (
        <TaskCard
          key={task.id}
          task={task}
          me={me}
          busy={busy}
          onPause={(paused) => run(() => api.pauseTask(task.id, paused))}
          onDelete={() => run(() => api.deleteTask(task.id))}
        />
      ))}
      {error && <p className="error">{error}</p>}
    </>
  )
}

function TaskCard(props: {
  task: Task
  me: Me
  busy: boolean
  onPause: (paused: boolean) => void
  onDelete: () => void
}) {
  const { task, me, busy } = props
  const { t, lang } = useI18n()
  const destination = task.to_me
    ? t('toMe')
    : `${t('toChat')} ${task.destination_title ?? task.destination_chat_id}`

  return (
    <div className="card">
      <div className="row between">
        <strong>{task.summary}</strong>
        {task.status !== 'active' && <span className="badge">{t(`status.${task.status}`)}</span>}
      </div>
      <div className="hint">
        <span dir="ltr">@{task.bot_username}</span> · {destination}
      </div>
      {task.steps.length > 0 && (
        <div className="hint">
          {t('chainSteps')}
          <ol style={{ margin: '4px 0', paddingInlineStart: 20 }}>
            {task.steps.map((step, i) => (
              <li key={i}>
                <span dir="ltr">{step.bot_username ? `@${step.bot_username}` : t('thisBot')}</span>: {step.instruction}
              </li>
            ))}
          </ol>
        </div>
      )}
      {task.next_run_at && task.status !== 'paused' && (
        <div className="hint">
          {t('next')}: {formatDateTime(task.next_run_at, lang, me.timezone)}
        </div>
      )}
      {task.last_error && (
        <div className="error">
          {t('lastError')}: {task.last_error}
        </div>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button className="btn secondary" disabled={busy} onClick={() => props.onPause(task.status !== 'paused')}>
          {task.status === 'paused' ? t('resume') : t('pause')}
        </button>
        <button className="btn danger" disabled={busy} onClick={props.onDelete}>
          {t('delete')}
        </button>
      </div>
    </div>
  )
}
