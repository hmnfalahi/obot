import { initData } from './telegram'

export type Lang = 'fa' | 'en'
export type ToolName = 'web_search' | 'read_url' | 'rss' | 'memory'
export const TOOLS: ToolName[] = ['web_search', 'read_url', 'rss', 'memory']

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
  factory_username: string
}

export interface BotSummary {
  id: number
  username: string
  display_name: string
  status: 'active' | 'suspended' | 'disabled'
  description: string
  tools: ToolName[]
  owner_started: boolean
  visibility: Visibility
}

export interface Chat {
  id: number
  title: string | null
  type: string
  is_admin: boolean
}

export interface TaskStep {
  instruction: string
  /** null: the task's own bot */
  bot_username: string | null
}

export interface Task {
  id: number
  bot_id: number
  bot_username: string
  summary: string
  instruction: string
  schedule_kind: 'cron' | 'once'
  cron: string | null
  timezone: string
  destination_chat_id: number
  /** Sent to the user's own chat with the bot; else to destination_title. */
  to_me: boolean
  destination_title: string | null
  status: 'active' | 'waiting_start' | 'paused' | 'done' | 'cancelled'
  next_run_at: string | null
  last_run_at: string | null
  last_error: string | null
  steps: TaskStep[]
}

export type Provider = 'openrouter' | 'openai' | 'anthropic'
export const PROVIDERS: Provider[] = ['openrouter', 'openai', 'anthropic']

export const PROVIDER_NAMES: Record<Provider, string> = {
  openrouter: 'OpenRouter',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
}

export const MODEL_EXAMPLES: Record<Provider, string> = {
  openrouter: 'anthropic/claude-sonnet-5.5',
  openai: 'gpt-5-mini',
  anthropic: 'claude-haiku-4-5',
}

/** A key in the user's wallet; the key itself never comes back. */
export interface WalletKey {
  provider: Provider
  key_hint: string
}

/** The user's own settings for a bot they use. */
export interface MySettings {
  bot_id: number
  username: string
  display_name: string
  is_owner: boolean
  model_tier: string
  recommended_model: string | null
  key_provider: Provider | null
  model: string | null
  daily_credit_cap: number | null
  wallet: Provider[]
  api_keys: { name: string; is_set: boolean }[]
}

export interface MySettingsIn {
  key_provider: Provider | null
  model: string | null
  daily_credit_cap: number | null
}

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
export const METHODS: HttpMethod[] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']

export interface ApiEndpoint {
  id: number
  name: string
  description: string
  method: HttpMethod
  url: string
  /** Templates, not secret: {name} slots take each user's own key. */
  headers: Record<string, string>
  key_slots: string[]
}

export interface ApiIn {
  name: string
  description: string
  method: HttpMethod
  url: string
  /** Values may name key slots like "Bearer {shop_token}"; never a real key. */
  headers: Record<string, string>
}

export type Visibility = 'private' | 'public'

/** Another person's bot that the user started. */
export interface UsedBot {
  id: number
  username: string
  display_name: string
  description: string
}

export interface AllowedUser {
  id: number
  name: string
}

export interface BotDetail extends BotSummary {
  allowed_users: AllowedUser[]
  users_count: number
  groups_count: number
  system_prompt: string
  language: string
  model_tier: string
  recommended_model: string | null
  apis: ApiEndpoint[]
  daily_credit_cap: number | null
  chats: Chat[]
}

export interface BotUpdate {
  display_name?: string
  description?: string
  system_prompt?: string
  language?: string
  tools?: ToolName[]
  model_tier?: 'cheap' | 'strong'
  recommended_model?: string
  visibility?: Visibility
  allowed_user_ids?: number[]
}

export interface UsageDay {
  day: string
  messages: number
  credits: number
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
  usedBots: () => request<UsedBot[]>('GET', '/used-bots'),
  removeUsedBot: (id: number) => request<void>('DELETE', `/used-bots/${id}`),
  bot: (id: number) => request<BotDetail>('GET', `/bots/${id}`),
  updateBot: (id: number, body: BotUpdate) => request<BotDetail>('PATCH', `/bots/${id}`, body),
  keys: () => request<WalletKey[]>('GET', '/keys'),
  putKey: (provider: Provider, apiKey: string) =>
    request<WalletKey[]>('PUT', `/keys/${provider}`, { api_key: apiKey }),
  deleteKey: (provider: Provider) => request<WalletKey[]>('DELETE', `/keys/${provider}`),
  mine: (botId: number) => request<MySettings>('GET', `/bots/${botId}/mine`),
  putMine: (botId: number, body: MySettingsIn) => request<MySettings>('PUT', `/bots/${botId}/mine`, body),
  putApiKey: (botId: number, name: string, value: string) =>
    request<MySettings>('PUT', `/bots/${botId}/mine/keys/${name}`, { value }),
  deleteApiKey: (botId: number, name: string) =>
    request<MySettings>('DELETE', `/bots/${botId}/mine/keys/${name}`),
  addApi: (botId: number, body: ApiIn) => request<BotDetail>('POST', `/bots/${botId}/apis`, body),
  updateApi: (id: number, body: ApiIn) => request<BotDetail>('PUT', `/apis/${id}`, body),
  deleteApi: (id: number) => request<BotDetail>('DELETE', `/apis/${id}`),
  usage: (id: number, days = 7) => request<UsageDay[]>('GET', `/bots/${id}/usage?days=${days}`),
  tasks: () => request<Task[]>('GET', '/tasks'),
  pauseTask: (id: number, paused: boolean) => request<Task>('PATCH', `/tasks/${id}`, { paused }),
  deleteTask: (id: number) => request<void>('DELETE', `/tasks/${id}`),
  packages: () => request<StarPackage[]>('GET', '/billing/packages'),
  invoice: (packageId: string) =>
    request<{ link: string }>('POST', '/billing/invoice', { package_id: packageId }),
  newBot: (body: NewBot) =>
    request<{ create_link: string; suggested_username: string }>('POST', '/bots/drafts', body),
}
