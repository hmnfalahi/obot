import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type Lang, type Me } from './api'
import { I18n, translator } from './i18n'
import { BotPage } from './pages/BotPage'
import { BotsPage } from './pages/BotsPage'
import { NewBotPage } from './pages/NewBotPage'
import { SettingsPage } from './pages/SettingsPage'
import { startParam, webApp } from './telegram'
import { ErrorBox, Loading } from './ui'

export type Route =
  | { page: 'bots' }
  | { page: 'bot'; id: number }
  | { page: 'new' }
  | { page: 'settings' }

/** The factory opens one bot with ?bot=<id> (web_app button) or start_param "bot_<id>" (link). */
function initialRoute(): Route {
  const fromQuery = new URLSearchParams(window.location.search).get('bot')
  const fromStart = /^bot_(\d+)$/.exec(startParam() ?? '')?.[1]
  const id = Number(fromQuery ?? fromStart)
  return id > 0 ? { page: 'bot', id } : { page: 'bots' }
}

export default function App() {
  const [route, setRoute] = useState<Route>(initialRoute)
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadMe = useCallback(() => {
    api.me().then(
      (value) => {
        setMe(value)
        setError(null)
      },
      (e: Error) => setError(e.message),
    )
  }, [])
  useEffect(loadMe, [loadMe])

  const lang: Lang = me?.language ?? (webApp?.initDataUnsafe.user?.language_code === 'fa' ? 'fa' : 'en')
  const i18n = useMemo(() => ({ lang, t: translator(lang) }), [lang])

  useEffect(() => {
    document.documentElement.lang = lang
    document.documentElement.dir = lang === 'fa' ? 'rtl' : 'ltr'
  }, [lang])

  // Telegram's back button returns to the bot list from any inner page.
  useEffect(() => {
    const back = webApp?.BackButton
    if (!back) return
    const goHome = () => setRoute({ page: 'bots' })
    if (route.page === 'bots') back.hide()
    else back.show()
    back.onClick(goHome)
    return () => back.offClick(goHome)
  }, [route])

  let content
  if (error) content = <ErrorBox message={error} onRetry={loadMe} />
  else if (!me) content = <Loading />
  else if (route.page === 'bot') content = <BotPage id={route.id} me={me} />
  else if (route.page === 'new') content = <NewBotPage onDone={() => setRoute({ page: 'bots' })} />
  else if (route.page === 'settings') content = <SettingsPage me={me} onChange={setMe} />
  else content = <BotsPage me={me} navigate={setRoute} />

  return <I18n.Provider value={i18n}>{content}</I18n.Provider>
}
