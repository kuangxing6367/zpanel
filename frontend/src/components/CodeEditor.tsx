import { useEffect, useRef } from 'react'
import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import JsonWorker from 'monaco-editor/language/json/json.worker.js?worker'

// monaco 的 worker 全部走**本地打包**（vite ?worker），不碰 CDN —— 内网/离线也能用。
// 只挂 editor + json 两个 worker：语法高亮在主线程，不需要语言服务 worker；
// JSON 的格式化/校验用得上。其它语言 worker 缺失只会少"校验"，不影响高亮。
self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string) {
    if (label === 'json') return new JsonWorker()
    return new EditorWorker()
  },
}

/** 扩展名 → monaco 语言 id（覆盖运维常见文件） */
const LANG: Record<string, string> = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
  ts: 'typescript', tsx: 'typescript',
  json: 'json', py: 'python', php: 'php', java: 'java', go: 'go', rb: 'ruby',
  html: 'html', htm: 'html', css: 'css', scss: 'scss', less: 'less',
  md: 'markdown', sh: 'shell', bash: 'shell', zsh: 'shell', bat: 'bat', ps1: 'powershell',
  yml: 'yaml', yaml: 'yaml', toml: 'ini', ini: 'ini', conf: 'ini', env: 'ini',
  xml: 'xml', sql: 'sql', lua: 'lua', c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp',
  cs: 'csharp', rs: 'rust', dockerfile: 'dockerfile',
}

export function langOf(filename: string): string {
  const base = filename.toLowerCase().split(/[\\/]/).pop() || ''
  if (base.startsWith('dockerfile')) return 'dockerfile'
  return LANG[base.split('.').pop() || ''] || 'plaintext'
}

/**
 * Monaco 代码编辑器（受控组件）。
 * 主题用 vs-dark 贴合面板暗色基调；automaticLayout 让它在模态里自适应。
 */
export default function CodeEditor({ value, filename, onChange, readOnly = false }: {
  value: string
  filename: string
  onChange?: (v: string) => void
  readOnly?: boolean
}) {
  const boxRef = useRef<HTMLDivElement | null>(null)
  const edRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    if (!boxRef.current) return
    const ed = monaco.editor.create(boxRef.current, {
      value,
      language: langOf(filename),
      theme: 'vs-dark',
      automaticLayout: true,
      fontSize: 12.5,
      fontFamily: 'Consolas, "Courier New", monospace',
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      readOnly,
      renderWhitespace: 'selection',
      tabSize: 4,
    })
    edRef.current = ed
    if (onChange) {
      ed.onDidChangeModelContent(() => onChangeRef.current?.(ed.getValue()))
    }
    return () => { ed.dispose(); edRef.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 外部 value 变化（重新打开文件）时同步进编辑器
  useEffect(() => {
    const ed = edRef.current
    if (ed && ed.getValue() !== value) ed.setValue(value)
  }, [value, filename])

  // 打开不同文件 → 换 model（保留各自的 undo 栈与视图状态）
  useEffect(() => {
    const ed = edRef.current
    if (!ed) return
    ed.setModel(monaco.editor.createModel(value, langOf(filename)))
    return () => { ed.getModel()?.dispose() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filename])

  return <div ref={boxRef} style={{ height: '56vh', border: '1px solid var(--border)' }} />
}
