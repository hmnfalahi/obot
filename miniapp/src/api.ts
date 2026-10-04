import { initData } from './telegram'

export type Lang = 'fa' | 'en'
export type ToolName = 'web_search' | 'read_url' | 'rss' | 'memory' | 'ask_bots'
export const TOOLS: ToolName[] = ['web_search', 'read_url', 'rss', 'memory', 'ask_bots']

export interface Quota {
  used: number
  limit: number | null
}

export interface Me {
  tg_user_id: number
  model_tiers: string[]
  language: Lang
  timezone: string
  plan: string
  credit_balance: number
  max_bots: number
  max_tasks: number
  max_apis: number
  messages_today: Quota
  searches_today: Quota
}

export interface BotSummary {
  id: number
  username: string
  display_name: string
  status: 'active' | 'suspended' | 'disabled'
  description: string
  tools: ToolName[]
  owner_started: boolean
}

export interface Chat {
  id: number
  title: string | null
  type: string
  is_admin: boolean
}

export type TriggerKind =
  | 'schedule'
  | 'rss'
  | 'page_change'
  | 'price'
  | 'group_keyword'
  | 'new_member'
  | 'webhook'

export interface Task {
  id: number
  summary: string
  instruction: string
  trigger: TriggerKind
  hook_url: string | null
  schedule_kind: 'cron' | 'once' | null
  cron: string | null
  timezone: string
  destination_chat_id: number
  to_owner: boolean
  status: 'active' | 'waiting_start' | 'paused' | 'done' | 'cancelled'
  next_run_at: string | null
  last_run_at: string | null
  last_error: string | null
}

export type RuleKind = 'keyword' | 'regex' | 'topic' | 'new_member'
export const RULE_KINDS: RuleKind[] = ['topic', 'keyword', 'new_member', 'regex']

export interface ReplyRule {
  kind: RuleKind
  keywords?: string[]
  pattern?: string | null
  topic?: string | null
  instruction?: string
}

export type Provider = 'openrouter' | 'openai' | 'anthropic'
export const PROVIDERS: Provider[] = ['openrouter', 'openai', 'anthropic']

export interface Byok {
  provider: Provider
  model: string
  key_hint: string
}

export interface ByokIn {
  provider: Provider
  model: string
  /** Omit to keep the stored key of the same provider. */
  api_key?: string
}

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
export const METHODS: HttpMethod[] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']

export interface ApiEndpoint {
  id: number
  name: string
  description: string
  method: HttpMethod
  url: string
  header_names: string[]
}

export interface ApiIn {
  name: string
  description: string
  method: HttpMethod
  url: string
  /** Omit on update to keep the stored (secret) headers. */
  headers?: Record<string, string>
}

export interface BotDetail extends BotSummary {
  reply_rules: ReplyRule[]
  system_prompt: string
  language: string
  model_tier: string
  byok: Byok | null
  apis: ApiEndpoint[]
  daily_credit_cap: number | null
  chats: Chat[]
  tasks: Task[]
}

export interface BotUpdate {
  display_name?: string
  description?: string
  system_prompt?: string
  language?: string
  tools?: ToolName[]
  model_tier?: 'cheap' | 'strong'
  reply_rules?: ReplyRule[]
  daily_credit_cap?: number
  clear_daily_credit_cap?: boolean
}

export interface UsageDay {
  day: string
  messages: number
  credits: number
}

export interface TaskAdd {
  scheduled: Task[]
  rejected: { summary: string; reason: string }[]
}

export interface NewBot {
  name: string
  description: string
  system_prompt: string
  language: string
  tools: ToolName[]
}

export interface StarPackage {
  id: string
  stars: number
  credits: number
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    headers: {
      Authorization: `tma ${initData()}`,
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const data = await response.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      // keep statusText
    }
    throw new ApiError(response.status, detail)
  }
  return response.status === 204 ? (undefined as T) : response.json()
}

export const api = {
  me: () => request<Me>('GET', '/me'),
  updateMe: (body: Partial<Pick<Me, 'language' | 'timezone'>>) => request<Me>('PATCH', '/me', body),
  bots: () => request<BotSummary[]>('GET', '/bots'),
  bot: (id: number) => request<BotDetail>('GET', `/bots/${id}`),
  updateBot: (id: number, body: BotUpdate) => request<BotDetail>('PATCH', `/bots/${id}`, body),
  setByok: (id: number, body: ByokIn) => request<BotDetail>('PUT', `/bots/${id}/byok`, body),
  deleteByok: (id: number) => request<BotDetail>('DELETE', `/bots/${id}/byok`),
  addApi: (botId: number, body: ApiIn) => request<BotDetail>('POST', `/bots/${botId}/apis`, body),
  updateApi: (id: number, body: ApiIn) => request<BotDetail>('PUT', `/apis/${id}`, body),
  deleteApi: (id: number) => request<BotDetail>('DELETE', `/apis/${id}`),
  usage: (id: number, days = 7) => request<UsageDay[]>('GET', `/bots/${id}/usage?days=${days}`),
  addTask: (id: number, text: string) =>
    request<TaskAdd>('POST', `/bots/${id}/tasks`, { request: text }),
  pauseTask: (id: number, paused: boolean) => request<Task>('PATCH', `/tasks/${id}`, { paused }),
  deleteTask: (id: number) => request<void>('DELETE', `/tasks/${id}`),
  packages: () => request<StarPackage[]>('GET', '/billing/packages'),
  invoice: (packageId: string) =>
    request<{ link: string }>('POST', '/billing/invoice', { package_id: packageId }),
  newBot: (body: NewBot) =>
    request<{ create_link: string; suggested_username: string }>('POST', '/bots/drafts', body),
}
