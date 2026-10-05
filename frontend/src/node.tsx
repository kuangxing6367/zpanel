import {
  createContext, useCallback, useContext, useEffect, useState, type ReactNode,
} from 'react'
import { api } from './api'
import { usePoll } from './components/Live'

/** 本机节点的固定名字（内核 core/nodes/manager.local() 里定的）。 */
export const LOCAL_NODE = 'localhost'

export interface NodeRec {
  name: string
  status?: string
  online?: boolean
  is_local?: boolean
  version?: string
  platform?: string
  hostname?: string
  last_ok_at?: string
  tags?: string[]
}

interface ScopeValue {
  nodes: NodeRec[]
  current: string
  node: NodeRec | null
  /** 节点清单是否已拿到（首次） */
  ready: boolean
  /** 向当前节点下发命令并返回其 data；失败抛错。
   *  本机也走同一条通道（内核 send_cmd 对 localhost 直接本地路由），
   *  所以「本机」在代码里不再是特例。 */
  call: <T = any>(cmd: string, args?: Record<string, any>, timeout?: number) => Promise<T>
  setCurrent: (name: string) => void
  refreshNodes: () => Promise<void>
}

const Ctx = createContext<ScopeValue | null>(null)

export function NodeScopeProvider({ children }: { children: ReactNode }) {
  const [nodes, setNodes] = useState<NodeRec[]>([])
  const [ready, setReady] = useState(false)
  const [current, setCurrentState] = useState(
    () => localStorage.getItem('zp_node') || LOCAL_NODE)

  const refreshNodes = useCallback(async () => {
    try {
      const r = await api.nodes()
      const list = r.nodes || []
      setNodes(list)
      setReady(true)
      // 选中的节点被移除了 → 回落到本机
      setCurrentState((cur) => {
        if (cur === LOCAL_NODE) return cur
        return list.some((n) => n.name === cur) ? cur : LOCAL_NODE
      })
    } catch {
      setReady(true)
    }
  }, [])

  // 节点在线状态由心跳驱动，5s 刷新一次清单
  usePoll(refreshNodes, 5000)

  const setCurrent = useCallback((name: string) => {
    const next = name || LOCAL_NODE
    localStorage.setItem('zp_node', next)
    setCurrentState(next)
  }, [])

  // 切换节点时广播一次，让页面重新取数（各页面监听 node 变化）
  useEffect(() => { /* current 变化即由各页面的依赖驱动重取 */ }, [current])

  const call = useCallback(async <T,>(cmd: string, args: Record<string, any> = {},
                                        timeout = 30): Promise<T> => {
    const name = current || LOCAL_NODE
    const r = await api.sendCmd(name, cmd, args, timeout) as any
    if (!r || r.ok !== true) {
      // 路由层把失败体的 data 原样塞进 error —— 可能是对象（业务错误字典），
      // 直接当字符串用会渲染成 "[object Object]"，所以非字符串一律转 JSON。
      const raw = typeof r?.data === 'string' ? r.data : (r?.error ?? r?.data)
      const msg = raw == null ? `${cmd} 失败`
        : typeof raw === 'string' ? raw : JSON.stringify(raw)
      throw new Error(msg)
    }
    return r.data as T
  }, [current])

  const node = nodes.find((n) => n.name === current) || null

  return (
    <Ctx.Provider value={{ nodes, current, node, ready, call, setCurrent, refreshNodes }}>
      {children}
    </Ctx.Provider>
  )
}

export function useNode(): ScopeValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useNode 必须在 NodeScopeProvider 内使用')
  return v
}
