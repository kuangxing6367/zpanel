import { useCallback, useEffect, useRef, useState } from 'react'
// 组件名与 xterm 的 Terminal 同名会让 minifier 把两者混成一个符号，
// new Terminal(...) 会被指向本组件，触发 React #321 白屏 —— 必须改名导入。
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { useNode } from '../node'
import { PageHeader } from '../components/ui'

/**
 * 终端 —— xterm.js 观感 + 命令模式后端。
 *
 * **诚实的边界**：后端 `terminal.exec` 是「一条命令一次执行」（限时 60s、
 * 输出 200KB 截断、cd 由服务端托管），**不是 PTY** —— 所以 vim / top 这类
 * 全屏交互程序用不了。做到交互式 PTY 要后端起伪终端并经 WebSocket 推流，
 * 是下一步的事，这里先把终端的观感与操作习惯对齐（xterm 渲染、ANSI 颜色、
 * 历史上下键、Ctrl+C 清行）。
 *
 * 本地行编辑在 xterm 里自己实现（逐字符回显、退格、历史），回车才真正执行。
 */
const PROMPT = (cwd: string) => `${cwd || '~'} $ `

export default function Terminal({ nodeName }: { nodeName?: string }) {
  const { current, call } = useNode()
  const name = nodeName || current

  const boxRef = useRef<HTMLDivElement | null>(null)
  const termRef = useRef<XTerm | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const cwdRef = useRef('')
  const lineRef = useRef('')
  const histRef = useRef<string[]>([])
  const histIdx = useRef(-1)
  const busyRef = useRef(false)

  const write = useCallback((s: string) => termRef.current?.write(s), [])

  const prompt = useCallback(() => {
    write(`\x1b[36m${PROMPT(cwdRef.current)}\x1b[0m`)
  }, [write])

  /** 本地行编辑的回显：只重画当前输入行 */
  const redrawLine = useCallback(() => {
    write(`\r\x1b[K\x1b[36m${PROMPT(cwdRef.current)}\x1b[0m${lineRef.current}`)
  }, [write])

  async function exec(cmd: string) {
    busyRef.current = true
    write('\r\n')
    try {
      const r = await call<{ stdout: string; stderr: string; cwd: string;
                             code: number | null; timed_out?: boolean }>(
        'terminal.exec', { cmd, cwd: cwdRef.current }, 65)
      if (r.stdout) write(r.stdout.replace(/\r?\n/g, '\r\n'))
      if (r.stderr) write(`\x1b[31m${r.stderr.replace(/\r?\n/g, '\r\n')}\x1b[0m`)
      if (r.code !== null && r.code !== 0 && !r.stdout && !r.stderr) {
        write(`\x1b[31m退出码 ${r.code}\x1b[0m`)
      }
      // 超时提示由服务端写在 stderr 里；r.timed_out 只是布尔标记
      if (r.timed_out) write(`\x1b[33m（已超时，进程树被终止）\x1b[0m`)
      if (r.cwd) cwdRef.current = r.cwd        // cd 由服务端托管，目录感连续
    } catch (e: any) {
      write(`\x1b[31m${e?.message || '执行失败'}\x1b[0m`)
    } finally {
      busyRef.current = false
      lineRef.current = ''
      histIdx.current = -1
      write('\r\n')
      prompt()
    }
  }

  // ── xterm 初始化（只挂一次）─────────────────────────────
  useEffect(() => {
    if (!boxRef.current || termRef.current) return
    const term = new XTerm({
      fontSize: 13,
      fontFamily: 'Consolas, "Courier New", monospace',
      cursorBlink: true,
      theme: {
        background: '#0b0e13', foreground: '#c9d1d9',
        cursor: '#9ecbff', selectionBackground: 'rgba(158,203,255,.25)',
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(boxRef.current)
    fit.fit()
    termRef.current = term
    fitRef.current = fit

    term.writeln('\x1b[90mZPanel 终端 · 命令模式（非 PTY：vim/top 等全屏程序不可用）\x1b[0m')
    term.writeln('\x1b[90m每条命令限时 60s · 上/下键翻历史 · Ctrl+C 清行\x1b[0m')

    call<{ cwd: string }>('terminal.exec', { cmd: 'pwd', cwd: '' }, 20)
      .then((r) => { cwdRef.current = r?.cwd || '' })
      .catch(() => {})
      .finally(() => prompt())

    term.onData((data) => {
      if (busyRef.current) return
      if (data === '\r') {                                  // 回车：执行
        const cmd = lineRef.current.trim()
        lineRef.current = ''
        if (cmd) {
          histRef.current.unshift(cmd)
          if (histRef.current.length > 100) histRef.current.pop()
          void exec(cmd)
        } else {
          write('\r\n')
          prompt()
        }
        return
      }
      if (data === '\x7f') {                                // 退格
        if (lineRef.current.length > 0) {
          lineRef.current = lineRef.current.slice(0, -1)
          redrawLine()
        }
        return
      }
      if (data === '\x03') {                                // Ctrl+C：清行
        lineRef.current = ''
        write('^C\r\n')
        prompt()
        return
      }
      if (data === '\x1b[A' || data === '\x1b[B') {         // 上/下：历史
        const h = histRef.current
        if (!h.length) return
        if (data === '\x1b[A') histIdx.current = Math.min(histIdx.current + 1, h.length - 1)
        else histIdx.current = Math.max(histIdx.current - 1, -1)
        lineRef.current = histIdx.current >= 0 ? h[histIdx.current] : ''
        redrawLine()
        return
      }
      if (data >= ' ') {                                    // 可打印字符
        lineRef.current += data
        write(data)
      }
    })

    const onResize = () => fitRef.current?.fit()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      term.dispose()
      termRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [call])

  return (
    <>
      <PageHeader title="终端" sub={<>
        节点 <span className="mono" style={{ color: 'var(--accent)' }}>{name}</span>
        {' '} · 命令模式（每条限时 60s）· cd 由服务端托管 · 全屏交互程序（vim/top）暂不支持
      </>} actions={
        <>
          <button className="btn btn-sm" onClick={() => {
            termRef.current?.clear()
            prompt()
          }}>清屏</button>
          <button className="btn btn-sm" onClick={() => fitRef.current?.fit()}>适应窗口</button>
        </>
      } />

      <div ref={boxRef} className="term-box" style={{ height: 'calc(100vh - 240px)', minHeight: 380 }} />
    </>
  )
}
