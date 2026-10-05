/**
 * 内核 API 客户端
 *
 * 与后端约定：所有响应为 JSON，成功带 `ok: true`，失败带 `ok: false` + `error`。
 * 令牌存 localStorage，随请求走 Authorization: Bearer <token>。
 */

const TOKEN_KEY = 'zp_token'

export class ApiError extends Error {
  status: number
  payload: any
  constructor(message: string, status: number, payload: any = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (t: string) => localStorage.setItem(TOKEN_KEY, t)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)

/** 401 时统一广播，由 App 层跳登录页 */
const AUTH_EVENT = 'zp:unauthorized'
export const onUnauthorized = (fn: () => void) => {
  window.addEventListener(AUTH_EVENT, fn)
  return () => window.removeEventListener(AUTH_EVENT, fn)
}

async function request<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((options.headers as Record<string, string>) || {}),
  }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  let res: Response
  try {
    res = await fetch(path, { ...options, headers })
  } catch (e: any) {
    throw new ApiError('无法连接内核 API，请确认服务已启动', 0, {})
  }

  // 204 / 空体
  const text = await res.text()
  let data: any = {}
  if (text) {
    try { data = JSON.parse(text) } catch { data = { raw: text } }
  }

  if (res.status === 401) {
    clearToken()
    window.dispatchEvent(new Event(AUTH_EVENT))
    throw new ApiError(data?.error || '登录已过期', 401, data)
  }
  if (!res.ok || data?.ok === false) {
    throw new ApiError(data?.error || `请求失败（${res.status}）`, res.status, data)
  }
  return data as T
}

/* ── 类型 ───────────────────────────────────────────── */
export interface SessionUser { id: number; username: string; role: string }

export interface LocalNode {
  name: string
  online: boolean
  is_local?: boolean
  platform?: string
  arch?: string
  hostname?: string
  python?: string
  version?: string
  uptime_seconds?: number
}

export interface ManagedNode {
  name: string
  host?: string
  port?: number
  tags?: string[]
  status: 'online' | 'offline' | 'unknown'
  version?: string
  uptime_seconds?: number
  memory_mb?: number
  last_ok_at?: string
  created_at?: string
  updated_at?: string
  is_local?: boolean
  platform?: string
  arch?: string
}

export interface ProviderInfo { name: string; desc: string; level: string }
export interface HandlerInfo { cmd: string; desc: string; level: string }

/** 内核任务队列里的任务（任务中心/安装进度共用） */
export interface PanelTask {
  id: string
  name: string
  state: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
  submitted_at: number
  started_at: number | null
  finished_at: number | null
  error: string | null
  result?: any
  meta?: Record<string, any>
  log?: string[]
}

export interface SystemInfo {
  core: Record<string, any>
  mode: string
  providers: ProviderInfo[]
  handlers: HandlerInfo[]
  nodes: number
  /** 服务层 zkg 包管理的加载全貌（内核只转述，不解释） */
  zkg?: ZkgInfo
}

export interface ApiToken {
  id: number; name: string; role: string
  created_at?: string; expires_at?: string; last_used_at?: string
}

export interface ExtensionRec {
  id: string
  name: string
  description: string
  version: string
  enabled: boolean
}

/* ── 接口 ───────────────────────────────────────────── */
export const api = {
  login: (username: string, password: string) =>
    request<{ token: string; user: SessionUser }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  logout: () => request('/api/auth/logout', { method: 'POST' }),

  me: () => request<{ user: SessionUser }>('/api/auth/me'),

  changePassword: (old_password: string, new_password: string) =>
    request('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ old_password, new_password }),
    }),

  health: () => request('/api/system/health'),
  systemInfo: () => request<SystemInfo>('/api/system/info'),

  /** 配置回显（密钥类字段已脱敏为 ***） */
  config: () => request<{ config: Record<string, any>; path: string }>('/api/system/config'),

  /** 写回配置（点路径；脱敏值 *** 不会被写回）。端口/监听地址等需重启生效 */
  saveConfig: (values: Record<string, any>) =>
    request<{ applied: Record<string, any>; missing: string[]; restart_required: boolean }>(
      '/api/system/config', { method: 'POST', body: JSON.stringify({ values }) }),

  /** 官方扩展清单与开关状态 */
  extensions: () =>
    request<{ extensions: ExtensionRec[]; count: number }>('/api/system/extensions'),

  /** 开关某个官方扩展（写 extensions.yaml，重启后生效） */
  toggleExtension: (id: string, enabled: boolean) =>
    request<{ id: string; enabled: boolean; restart_required: boolean }>(
      `/api/system/extensions/${encodeURIComponent(id)}`,
      { method: 'POST', body: JSON.stringify({ enabled }) }),

  /** 接口令牌（给外部程序用的长期令牌） */
  apiTokens: () => request<{ tokens: ApiToken[] }>('/api/auth/tokens'),
  createApiToken: (name: string, role = 'admin') =>
    request<{ token: string } & Record<string, any>>('/api/auth/tokens', {
      method: 'POST', body: JSON.stringify({ name, role }),
    }),
  revokeApiToken: (id: number) => request(`/api/auth/tokens/${id}`, { method: 'DELETE' }),

  handlers: () => request<{ handlers: HandlerInfo[] }>('/api/system/handlers'),
  providers: () => request<{ providers: ProviderInfo[] }>('/api/system/providers'),
  packages: () => request<{ packages: ZkgInfo }>('/api/system/packages'),

  nodes: () => request<{ nodes: ManagedNode[]; mode: string }>('/api/nodes'),

  /** 本机（hub）任务列表；远程节点的任务走节点命令通道 task.list/task.get */
  tasks: (state?: string, limit = 100) =>
    request<{ tasks: PanelTask[]; stats: Record<string, number> }>(
      `/api/tasks?limit=${limit}${state ? `&state=${state}` : ''}`),
  task: (id: string) => request<PanelTask>(`/api/tasks/${encodeURIComponent(id)}`),
  /** 取消任务（只有还在排队的能取消，执行中的服务端如实拒绝） */
  cancelTask: (id: string) =>
    request(`/api/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  localNode: () => request<{ node: LocalNode }>('/api/nodes/local'),
  nodeData: (name: string) =>
    request<{ node: string; status?: string; data: Record<string, any> }>(
      `/api/nodes/${encodeURIComponent(name)}/data`),

  addNode: (payload: { name: string; host?: string; port?: number; tags?: string }) =>
    request<{ name: string; secret: string }>('/api/nodes', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  removeNode: (name: string) =>
    request(`/api/nodes/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  rotateSecret: (name: string) =>
    request<{ name: string; secret: string }>(
      `/api/nodes/${encodeURIComponent(name)}/secret`, { method: 'POST' }),

  /** 向指定节点下发命令。**本机（localhost）也走这条通道** ——
   *  内核 send_cmd 对 localhost 直接本地路由，所以代码里「本机」不是特例。 */
  sendCmd: (name: string, cmd: string, args: Record<string, any> = {}, timeout = 30) =>
    request<{ ok: boolean; data: any }>(
      `/api/nodes/${encodeURIComponent(name)}/cmd`, {
        method: 'POST',
        body: JSON.stringify({ cmd, args, timeout }),
      }),

  /** 换实时推送的一次性票据（短时、绑定 topic；登录 token 不上 URL） */
  streamTicket: (topic: string, ttl = 0) =>
    request<{ ticket: string; topic: string; exp: number; ttl: number; ws_url: string }>(
      '/api/stream/ticket', {
        method: 'POST',
        body: JSON.stringify({ topic, ttl }),
      }),
}

/* ── 运行时管理（软件层扩展 runtime）─────────────── */
export interface RuntimeInstance {
  id: string
  name: string
  kind: string
  status: 'running' | 'stopped' | 'starting' | 'stopping' | 'busy'
  status_code: number
  cwd: string
  start_command: string
  stop_command?: string
  env?: Record<string, string>
  port: number
  pid: number | null
  started_at: string | null
  uptime_seconds: number
  start_count: number
  restart_count: number
  exit_code: number | null
  last_error?: string
  output_lines: number
  auto_start: boolean
  auto_restart: boolean
  max_restarts?: number
  runtime?: string
  tags: string[]
}

export interface RuntimeProbe {
  kind: string
  label: string
  exe: string
  available: boolean
  version: string
}

export interface LogLine { ts: string; stream: string; line: string }

export const runtimeApi = {
  list: () => request<{ instances: RuntimeInstance[]; count: Record<string, number> }>(
    '/api/runtime/instances'),
  create: (spec: Partial<RuntimeInstance>) =>
    request<{ instance: RuntimeInstance }>('/api/runtime/instances', {
      method: 'POST', body: JSON.stringify(spec),
    }),
  update: (id: string, spec: Partial<RuntimeInstance>) =>
    request<{ instance: RuntimeInstance }>(`/api/runtime/instances/${id}`, {
      method: 'PATCH', body: JSON.stringify(spec),
    }),
  remove: (id: string) =>
    request(`/api/runtime/instances/${id}`, { method: 'DELETE' }),
  start: (id: string) =>
    request(`/api/runtime/instances/${id}/start`, { method: 'POST' }),
  stop: (id: string) =>
    request(`/api/runtime/instances/${id}/stop`, { method: 'POST' }),
  restart: (id: string) =>
    request(`/api/runtime/instances/${id}/restart`, { method: 'POST' }),
  logs: (id: string, limit = 200) =>
    request<{ lines: LogLine[] }>(`/api/runtime/instances/${id}/logs?limit=${limit}`),
  stdin: (id: string, data: string) =>
    request(`/api/runtime/instances/${id}/stdin`, {
      method: 'POST', body: JSON.stringify({ data }),
    }),
  detect: () => request<{ runtimes: RuntimeProbe[] }>('/api/runtime/detect'),
  template: (kind: string, spec: Record<string, any> = {}) => {
    const q = new URLSearchParams({ kind, ...spec } as any).toString()
    return request<{ start_command: string }>(`/api/runtime/template?${q}`)
  },
}

/* ── 工具 ───────────────────────────────────────────── */
export function fmtUptime(seconds?: number): string {
  if (!seconds || seconds < 0) return '—'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}天 ${h}小时`
  if (h > 0) return `${h}小时 ${m}分`
  if (m > 0) return `${m}分`
  return `${Math.floor(seconds)}秒`
}

export function fmtBytes(mb?: number | null): string {
  if (mb == null) return '—'
  if (mb < 1024) return `${mb.toFixed(1)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}

/* ── zkg 机制包（服务层：按依赖加载，内核只转述）─────────
   机制包放在 repo/<id>/（manifest.toml + main.py），
   软件层在 software/extensions/<name>/manifest.toml 的 dependencies 里声明依赖；
   zkg 据此决定加载哪些包 —— 无人依赖的包不会被加载（剪枝）。 */
export interface ZkgStats {
  tools_total?: number
  tools_loaded?: number
  tools_skipped?: number
}
export interface ZkgInfo {
  repo?: string
  scan_roots?: string[]
  sources?: string[]
  stats?: ZkgStats
  loaded?: string[]
  skipped?: string[]
  missing?: string[]
  manifests?: number
  /** /api/system/packages 额外回填：已加载模块名 */
  tools?: string[]
}

export const packagesApi = {
  info: () => request<{ packages: ZkgInfo }>('/api/system/packages'),
}

/* ── 系统监控（扩展 monitor，数据来自机制包 sysres）───── */
export interface HostCpu { percent: number | null; count: number | null }
export interface HostMem {
  total: number | null; used: number | null; free: number | null; percent: number | null
}
export interface HostDisk {
  device: string; mountpoint: string; fstype: string
  total: number; used: number; free: number; percent: number
}
export interface HostNet {
  bytes_sent: number | null; bytes_recv: number | null
  packets_sent: number | null; packets_recv: number | null
}
export interface HostInfo {
  system: string; release: string; machine: string
  python: string; cpu_count: number | null; backend: string; pid: number
}
export interface HostSnapshot {
  info: HostInfo
  cpu: HostCpu
  memory: HostMem
  swap: HostMem
  disks?: HostDisk[]
  net?: HostNet
  uptime: number | null
  load: number[] | null
  sampled_at?: number
  cached?: boolean
}
export interface ProcessRow { pid: number; name: string; cpu: number; mem: number; user: string }

export const monitorApi = {
  host: () => request<HostSnapshot>('/api/system/host'),
  top: (n = 10, sort: 'cpu' | 'mem' = 'cpu') =>
    request<{ processes: ProcessRow[]; sort: string; available: boolean }>(
      `/api/system/host/top?n=${n}&sort=${sort}`),
}
