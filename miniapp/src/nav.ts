/** Navigation as a stack of routes; the bottom of the stack is always a tab root. */

export type Tab = 'bots' | 'tasks' | 'credits' | 'settings'
export const TABS: Tab[] = ['bots', 'tasks', 'credits', 'settings']

export type Route =
  | { page: Tab }
  | { page: 'bot'; id: number }
  | { page: 'mine'; id: number }
  | { page: 'new' }

export type Stack = Route[]

export type NavAction =
  | { type: 'push'; route: Route }
  | { type: 'back' }
  | { type: 'replace'; route: Route }
  | { type: 'tab'; tab: Tab }

export function isTab(page: string): page is Tab {
  return (TABS as string[]).includes(page)
}

export function reduce(stack: Stack, action: NavAction): Stack {
  switch (action.type) {
    case 'push':
      return isTab(action.route.page) ? [action.route] : [...stack, action.route]
    case 'back':
      return stack.length > 1 ? stack.slice(0, -1) : stack
    case 'replace':
      if (isTab(action.route.page)) return [action.route]
      return stack.length > 1 ? [...stack.slice(0, -1), action.route] : [...stack, action.route]
    case 'tab':
      return [{ page: action.tab }]
  }
}

/** The tab a navigation picks (a tab tap, or going to a tab root), or null if it stays on the tab. */
export function chosenTab(action: NavAction): Tab | null {
  if (action.type === 'tab') return action.tab
  if (action.type === 'push' || action.type === 'replace')
    return isTab(action.route.page) ? action.route.page : null
  return null
}

export function currentTab(stack: Stack): Tab {
  return stack[0].page as Tab
}

/** The factory opens one bot with ?bot=<id> (web_app button) or start_param "bot_<id>" (link),
 * and someone's own settings for a bot with ?mine=<id>. */
export function initialStack(search: string, startParam: string | undefined): Stack {
  const params = new URLSearchParams(search)
  const home: Route = { page: 'bots' }
  const mine = Number(params.get('mine'))
  if (mine > 0) return [home, { page: 'mine', id: mine }]
  const fromStart = /^bot_(\d+)$/.exec(startParam ?? '')?.[1]
  const id = Number(params.get('bot') ?? fromStart)
  return id > 0 ? [home, { page: 'bot', id }] : [home]
}
