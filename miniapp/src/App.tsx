import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type Lang, type Me } from './api'
import { I18n, translator } from './i18n'
import { BotPage } from './pages/BotPage'
import { BotsPage } from './pages/BotsPage'
import { CollectionPage } from './pages/CollectionPage'
import { DataPage } from './pages/DataPage'
import { MySettingsPage } from './pages/MySettingsPage'
import { NewBotPage } from './pages/NewBotPage'
import { SchemaPage } from './pages/SchemaPage'
import { SettingsPage } from './pages/SettingsPage'
import { TasksPage } from './pages/TasksPage'
import { startParam, webApp } from './telegram'
import { ErrorBox, Loading } from './ui'

export type Route =
  | { page: 'bots' }
  | { page: 'bot'; id: number }
  | { page: 'new' }
  | { page: 'settings' }
  | { page: 'tasks' }
  | { page: 'mine'; id: number }
  | { page: 'data'; botId: number }
  | { page: 'collection'; botId: number; collectionId: number }
  | { page: 'schema'; botId: number; collectionId: number }

/** The factory opens one bot with ?bot=<id> (web_app button) or start_param "bot_<id>" (link),
 * someone's own settings for a bot with ?mine=<id>, a bot's data with ?data=<id> and one of its
 * lists with start_param "data_<bot>_<collection>". */
function initialRoute(): Route {
  const search = new URLSearchParams(window.location.search)
  const mine = Number(search.get('mine'))
  if (mine > 0) return { page: 'mine', id: mine }
  const list = /^data_(\d+)_(\d+)$/.exec(startParam() ?? '')
  if (list) return { page: 'collection', botId: Number(list[1]), collectionId: Number(list[2]) }
  const data = Number(search.get('data'))
  if (data > 0) return { page: 'data', botId: data }
  const fromQuery = search.get('bot')
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

  // Telegram's back button returns to the bot list from any inner page, from a list to its bot's
  // data and from a list's editor to the list.
  useEffect(() => {
    const back = webApp?.BackButton
    if (!back) return
    const goBack = () => {
      if (route.page === 'collection') setRoute({ page: 'data', botId: route.botId })
      else if (route.page === 'schema')
        setRoute({ page: 'collection', botId: route.botId, collectionId: route.collectionId })
      else setRoute({ page: 'bots' })
    }
    if (route.page === 'bots') back.hide()
    else back.show()
    back.onClick(goBack)
    return () => back.offClick(goBack)
  }, [route])

  let content
  if (error) content = <ErrorBox message={error} onRetry={loadMe} />
  else if (!me) content = <Loading />
  else if (route.page === 'bot') content = <BotPage id={route.id} me={me} navigate={setRoute} />
  else if (route.page === 'mine') content = <MySettingsPage id={route.id} navigate={setRoute} />
  else if (route.page === 'data') content = <DataPage botId={route.botId} navigate={setRoute} />
  else if (route.page === 'collection')
    content = <CollectionPage botId={route.botId} collectionId={route.collectionId} navigate={setRoute} />
  else if (route.page === 'schema')
    content = <SchemaPage botId={route.botId} collectionId={route.collectionId} navigate={setRoute} />
  else if (route.page === 'new') content = <NewBotPage onDone={() => setRoute({ page: 'bots' })} />
  else if (route.page === 'settings') content = <SettingsPage me={me} onChange={setMe} />
  else if (route.page === 'tasks') content = <TasksPage me={me} />
  else content = <BotsPage me={me} navigate={setRoute} />

  return <I18n.Provider value={i18n}>{content}</I18n.Provider>
}
