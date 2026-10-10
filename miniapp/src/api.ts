import { initData } from './telegram'

export type Lang = 'fa' | 'en'
export type ToolName = 'web_search' | 'read_url' | 'rss' | 'memory' | 'personal_data'
export const TOOLS: ToolName[] = ['web_search', 'read_url', 'rss', 'memory', 'personal_data']

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
}

export interface MySettingsIn {
  key_provider: Provider | null
  model: string | null
  daily_credit_cap: number | null
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

export type FieldType =
  | 'text'
  | 'number'
  | 'money'
  | 'date'
  | 'time'
  | 'datetime'
  | 'choice'
  | 'boolean'
  | 'phone'

export interface FieldDef {
  key: string
  label: Record<string, string>
  type: FieldType
  required: boolean
  options: string[]
  min: string | null
  max: string | null
  currency: string | null
  public: boolean
  hidden: boolean
}

export interface Rules {
  unique?: string[][]
  allowed_hours?: {
    date_field: string
    time_field: string | null
    windows: { weekdays: number[]; start: string; end: string }[]
  } | null
}

export interface CollectionEvents {
  notify?: { on: 'create' | 'update' | 'delete'; to: 'owner' | 'author' }[]
  remind?: { date_field: string; time_field: string | null; before_minutes: number; to: 'owner' | 'author' }[]
}

/** One of a bot's lists, as the caller may see it. */
export interface DataCollection {
  id: number
  key: string
  title: Record<string, string>
  scope: 'personal' | 'shared' | 'group'
  mine: boolean
  /** The caller owns the bot, so sees everyone's records in a shared list. */
  is_owner: boolean
  can_edit: boolean
  schema_version: number
  fields: FieldDef[]
  rules: Rules
  events: CollectionEvents
  /** The zone the collection's dates and times are in. */
  timezone: string
}

export interface DataRecord {
  id: number
  /** Datetimes are UTC ("...Z"); numbers and money are decimal strings. */
  data: Record<string, unknown>
  version: number
  created_at: string
  mine: boolean
}

export interface DataFilter {
  field: string
  op: 'eq' | 'ne' | 'lt' | 'lte' | 'gt' | 'gte' | 'contains' | 'in'
  value: unknown
}

/** A structured data error from the API (see obot/api/data_routes.py). */
export interface DataErrorDetail {
  code: string
  message: string
  errors: { field: string; reason: string }[]
  fields: string[]
  change?: string | null
  limit?: string
}

export class ApiError extends Error {
  status: number
  /** The response's raw `detail`, e.g. a DataErrorDetail. */
  detail: unknown
  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

/** The structured data error behind `e`, if it is one. */
export function dataErrorOf(e: unknown): DataErrorDetail | null {
  if (!(e instanceof ApiError)) return null
  const detail = e.detail as Partial<DataErrorDetail> | null
  return detail && typeof detail === 'object' && typeof detail.code === 'string'
    ? { errors: [], fields: [], message: '', ...detail, code: detail.code }
    : null
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
    let message = response.statusText
    let detail: unknown
    try {
      const data = await response.json()
      detail = data.detail
      const text = (detail as { message?: unknown } | null)?.message
      message = typeof detail === 'string' ? detail : typeof text === 'string' ? text : JSON.stringify(detail)
    } catch {
      // keep statusText
    }
    throw new ApiError(response.status, message, detail)
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
  setAvatar: (botId: number, imageBase64: string) =>
    request<void>('POST', `/bots/${botId}/avatar`, { image: imageBase64 }),
  keys: () => request<WalletKey[]>('GET', '/keys'),
  putKey: (provider: Provider, apiKey: string) =>
    request<WalletKey[]>('PUT', `/keys/${provider}`, { api_key: apiKey }),
  deleteKey: (provider: Provider) => request<WalletKey[]>('DELETE', `/keys/${provider}`),
  mine: (botId: number) => request<MySettings>('GET', `/bots/${botId}/mine`),
  putMine: (botId: number, body: MySettingsIn) => request<MySettings>('PUT', `/bots/${botId}/mine`, body),
  usage: (id: number, days = 7) => request<UsageDay[]>('GET', `/bots/${id}/usage?days=${days}`),
  tasks: () => request<Task[]>('GET', '/tasks'),
  pauseTask: (id: number, paused: boolean) => request<Task>('PATCH', `/tasks/${id}`, { paused }),
  deleteTask: (id: number) => request<void>('DELETE', `/tasks/${id}`),
  packages: () => request<StarPackage[]>('GET', '/billing/packages'),
  invoice: (packageId: string) =>
    request<{ link: string }>('POST', '/billing/invoice', { package_id: packageId }),
  collections: (botId: number) => request<DataCollection[]>('GET', `/bots/${botId}/collections`),
  records: (
    botId: number,
    collectionId: number,
    params: { q?: string; sort?: string; desc?: boolean; filters?: DataFilter[]; offset?: number; limit?: number },
  ) => {
    const query = new URLSearchParams()
    if (params.q) query.set('q', params.q)
    if (params.sort) query.set('sort', params.sort)
    if (params.desc !== undefined) query.set('desc', String(params.desc))
    if (params.filters?.length) query.set('filters', JSON.stringify(params.filters))
    if (params.offset) query.set('offset', String(params.offset))
    if (params.limit) query.set('limit', String(params.limit))
    return request<{ records: DataRecord[]; has_more: boolean }>(
      'GET',
      `/bots/${botId}/collections/${collectionId}/records?${query}`,
    )
  },
  addRecord: (botId: number, collectionId: number, data: Record<string, unknown>) =>
    request<DataRecord>('POST', `/bots/${botId}/collections/${collectionId}/records`, { data }),
  updateRecord: (
    botId: number,
    collectionId: number,
    id: number,
    changes: Record<string, unknown>,
    version: number,
  ) =>
    request<DataRecord>('PATCH', `/bots/${botId}/collections/${collectionId}/records/${id}`, {
      changes,
      version,
    }),
  deleteRecord: (botId: number, collectionId: number, id: number) =>
    request<void>('DELETE', `/bots/${botId}/collections/${collectionId}/records/${id}`),
  putSchema: (
    botId: number,
    collectionId: number,
    body: { fields: FieldDef[]; rules: Rules; title?: Record<string, string>; version: number },
  ) => request<DataCollection>('PUT', `/bots/${botId}/collections/${collectionId}/schema`, body),
  putEvents: (botId: number, collectionId: number, events: CollectionEvents) =>
    request<DataCollection>('PUT', `/bots/${botId}/collections/${collectionId}/events`, events),
  exportLink: (botId: number, collectionId: number) =>
    request<{ url: string; file_name: string }>('POST', `/bots/${botId}/collections/${collectionId}/export`),
  newBot: (body: NewBot) =>
    request<{ create_link: string; suggested_username: string }>('POST', '/bots/drafts', body),
}
