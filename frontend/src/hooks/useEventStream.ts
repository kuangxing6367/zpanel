import { useEffect, useRef } from 'react'
import { api } from '../api'

export interface StreamEvent { event: string; data: any }
export type StreamStatus = 'idle' | 'connecting' | 'open' | 'closed'

/**
 * 实时推送订阅（内核 WebSocket，纯标准库自实现 RFC 6455）。
 *
 * 凭据问题：浏览器 WebSocket 不能带 Authorization 头，直接把登录 token 拼在
 * URL 上会进访问日志/浏览器历史 —— 不允许。流程是：
 *   ① 先 POST /api/stream/ticket（带鉴权）换一张**一次性短时票据**（60s、绑定 topic）；
 *   ② 用票据连 ws://…/ws/<topic>?ticket=…；
 *   ③ 断线重连必须重新换票（票据用一次即废）。
 */
export function useEventStream(
  topic: string | null,
  enabled: boolean,
  onEvent: (ev: StreamEvent) => void,
  onStatus?: (s: StreamStatus) => void,
) {
  const evRef = useRef(onEvent); evRef.current = onEvent
  const stRef = useRef(onStatus); stRef.current = onStatus

  useEffect(() => {
    if (!topic || !enabled) {
      stRef.current?.('idle')
      return
    }
    let ws: WebSocket | null = null
    let stop = false
    let timer: number | undefined
    let attempt = 0

    async function connect() {
      if (stop) return
      stRef.current?.('connecting')
      try {
        // ① 换票（带鉴权的普通接口）
        const t = await api.streamTicket(topic!)
        // ② 连接（一次性票据）
        ws = new WebSocket(`${t.ws_url}/ws/${encodeURIComponent(topic!)}?ticket=${encodeURIComponent(t.ticket)}`)
        ws.onopen = () => { attempt = 0; stRef.current?.('open') }
        ws.onmessage = (m) => {
          try { evRef.current(JSON.parse(m.data)) } catch { /* 非 JSON 忽略 */ }
        }
        ws.onclose = () => {
          stRef.current?.('closed')
          ws = null
          if (!stop) {
            attempt += 1
            timer = window.setTimeout(connect, Math.min(1000 * attempt, 8000))
          }
        }
        ws.onerror = () => { try { ws?.close() } catch { /* 已关 */ } }
      } catch {
        if (!stop) {
          attempt += 1
          timer = window.setTimeout(connect, Math.min(1000 * attempt, 8000))
        }
      }
    }

    connect()
    return () => {
      stop = true
      if (timer) window.clearTimeout(timer)
      try { ws?.close() } catch { /* 已关 */ }
    }
  }, [topic, enabled])
}
