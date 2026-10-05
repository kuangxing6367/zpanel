import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNode } from '../node'
import { LiveTag, usePoll } from '../components/Live'
import CodeEditor from '../components/CodeEditor'
import { Badge, ConfirmModal, EmptyState, Field, Modal, PageHeader, Toolbar } from '../components/ui'

const POLL_MS = 8000
const MAX_EDIT = 2 * 1024 * 1024      // 在线编辑上限 2MB（再大浏览器也卡）
const CHUNK = 512 * 1024              // 二进制分块大小：命令通道单帧载荷 <1MB（hub 侧上限），
                                      // 512KB base64 后 ~683KB，留足信封余量

interface FsItem { name: string; path: string; dir: boolean; size: number; mtime: string }

/**
 * 文件管理 —— 对齐常见面板的文件管理器形态：
 *
 *   盘符切换 → 面包屑路径（每级可点）→ 工具栏（新建/上传/搜索）
 *   → 表格（目录置顶、双击进入、操作列：打开/编辑/重命名/摘要/删除）
 *   编辑、重命名、新建、删除确认都是模态。
 *
 * 所有路径操作都经服务端 `sandbox` 机制包（realpath + 根白名单），前端不做校验。
 * 上传/下载：任意二进制按 512KB 分块走 files.write_b64 / files.read_b64 命令，
 * 本机与远程节点同一条通道；GB 级大流要等流面 0x30 文件帧。
 */
export default function Files({ nodeName }: { nodeName?: string }) {
  const { current, call } = useNode()
  const name = nodeName || current

  const [roots, setRoots] = useState<{ path: string; name: string }[]>([])
  const [path, setPath] = useState('')
  const [items, setItems] = useState<FsItem[]>([])
  const [keyword, setKeyword] = useState('')
  const [at, setAt] = useState(0)
  const [auto, setAuto] = useState(true)
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const [view, setView] = useState<{ path: string; content: string; encoding: string;
                                    binary: boolean; size: number } | null>(null)
  const [renaming, setRenaming] = useState<FsItem | null>(null)
  const [renameTo, setRenameTo] = useState('')
  const [creating, setCreating] = useState<'dir' | 'file' | null>(null)
  const [createName, setCreateName] = useState('')
  const [deleting, setDeleting] = useState<FsItem | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [clip, setClip] = useState<{ mode: 'copy' | 'cut'; paths: string[] } | null>(null)
  const [sortKey, setSortKey] = useState<'name' | 'size' | 'mtime'>('name')
  const [sortDir, setSortDir] = useState<1 | -1>(1)
  const uploadRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(async (p?: string) => {
    const target = p ?? path
    if (!target) return
    setBusy('list')
    try {
      const d = await call<{ path: string; items: FsItem[] }>('files.list', { path: target }, 20)
      setItems(d.items || [])
      setPath(d.path || target)
      setSelected(new Set())      // 换了目录，选中集必须清掉（路径属于旧目录）
      setMsg(null)
      setAt(Date.now())
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '读取目录失败' })
    } finally { setBusy('') }
  }, [call, path])

  // 进页面：拿根目录清单，优先打开非系统盘（别一上来就怼 C:\ 根目录的 $Recycle.Bin）
  useEffect(() => {
    call<{ roots: { path: string; name: string }[] }>('files.roots', {}, 15)
      .then((r) => {
        const sorted = [...(r.roots || [])].sort((a, b) =>
          Number(/^c:\\?$/i.test(a.path)) - Number(/^c:\\?$/i.test(b.path)))
        setRoots(sorted)
        if (sorted.length) load(sorted[0].path)
      })
      .catch((e) => setMsg({ kind: 'err', text: e?.message || '读取根目录失败' }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [call])

  usePoll(() => load(), POLL_MS, auto && !!path)

  /** 面包屑分段：C:\apps\demo → [C:\, apps, demo]，每级可点 */
  const crumbs = useMemo(() => {
    if (!path) return []
    const parts = path.split(/[\\/]+/).filter(Boolean)
    const out: { name: string; path: string }[] = []
    let acc = ''
    for (let i = 0; i < parts.length; i++) {
      // 盘符（C:）自带冒号，拼上分隔符；普通段用 \ 连接
      acc = i === 0 ? parts[0] + (parts[0].endsWith(':') ? '\\' : '') : acc.replace(/\\$/, '') + '\\' + parts[i]
      out.push({ name: i === 0 ? roots.find((r) => r.path.toUpperCase() === parts[0].toUpperCase() + '\\')?.name || parts[0] : parts[i], path: acc })
    }
    return out
  }, [path, roots])

  /** 目录排前、按名称排序，再按关键词过滤 */
  const shown = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    const key = sortKey === 'size' ? 'size' : sortKey
    return items
      .filter((it) => !kw || it.name.toLowerCase().includes(kw))
      .sort((a, b) => {
        if (a.dir !== b.dir) return a.dir ? -1 : 1          // 目录永远在前
        let r = 0
        if (key === 'name') r = a.name.localeCompare(b.name, undefined, { numeric: true })
        else if (key === 'size') r = a.size - b.size
        else r = String(a.mtime).localeCompare(String(b.mtime))
        return r * sortDir
      })
  }, [items, keyword, sortKey, sortDir])

  function toggleSort(k: 'name' | 'size' | 'mtime') {
    if (sortKey === k) setSortDir((d) => (d === 1 ? -1 : 1))
    else { setSortKey(k); setSortDir(1) }
  }

  function read(p: string) {
    setBusy('read:' + p)
    call<any>('files.read', { path: p }, 20)
      .then((d) => setView({ path: p, content: d.content || '', encoding: d.encoding || 'utf-8',
                             binary: !!d.binary, size: d.size || 0 }))
      .catch((e) => setMsg({ kind: 'err', text: e?.message || '读取失败' }))
      .finally(() => setBusy(''))
  }

  function save() {
    if (!view) return
    setBusy('save')
    call('files.write', { path: view.path, content: view.content, encoding: view.encoding }, 30)
      .then(() => { setMsg({ kind: 'ok', text: '已保存' }); setView(null) })
      .catch((e) => setMsg({ kind: 'err', text: e?.message || '保存失败' }))
      .finally(() => setBusy(''))
  }

  function doCreate() {
    if (!createName.trim() || !creating) return
    const target = joinPath(path, createName.trim())
    setBusy('create')
    call(creating === 'dir' ? 'files.mkdir' : 'files.write',
         creating === 'dir' ? { path: target } : { path: target, content: '' }, 20)
      .then(() => {
        setMsg({ kind: 'ok', text: `已创建 ${createName.trim()}` })
        setCreating(null); setCreateName(''); load()
      })
      .catch((e) => setMsg({ kind: 'err', text: e?.message || '创建失败' }))
      .finally(() => setBusy(''))
  }

  function doRename() {
    if (!renaming || !renameTo.trim()) return
    setBusy('rename')
    call('files.rename', { path: renaming.path, new_name: renameTo.trim() }, 20)
      .then(() => { setRenaming(null); load() })
      .catch((e) => setMsg({ kind: 'err', text: e?.message || '重命名失败' }))
      .finally(() => setBusy(''))
  }

  function doDelete() {
    if (!deleting) return
    setBusy('del')
    call('files.delete', { paths: [deleting.path] }, 30)
      .then(() => { setMsg({ kind: 'ok', text: `已删除 ${deleting.name}` }); setDeleting(null); load() })
      .catch((e) => setMsg({ kind: 'err', text: e?.message || '删除失败' }))
      .finally(() => setBusy(''))
  }

  /** 粘贴：把剪贴板里的路径复制/移动到当前目录 */
  async function paste() {
    if (!clip || !path) return
    setBusy('paste')
    try {
      const r = await call<{ moved: unknown[]; failed: { path: string; error: string }[] }>(
        'files.copy', { paths: clip.paths, dst_dir: path, move: clip.mode === 'cut' }, 60)
      const fails = r.failed || []
      setMsg(fails.length
        ? { kind: 'err', text: `${clip.mode === 'cut' ? '移动' : '复制'}完成，${fails.length} 项失败: ${fails[0].error}` }
        : { kind: 'ok', text: `已${clip.mode === 'cut' ? '移动' : '复制'} ${r.moved?.length ?? 0} 项` })
      if (clip.mode === 'cut') setClip(null)
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '粘贴失败' })
    } finally { setBusy('') }
  }

  /** 批量删除选中项 */
  const [bulkDeleting, setBulkDeleting] = useState(false)
  async function doBulkDelete() {
    setBusy('bulkdel')
    try {
      await call('files.delete', { paths: [...selected] }, 60)
      setMsg({ kind: 'ok', text: `已删除 ${selected.size} 项` })
      setSelected(new Set()); setBulkDeleting(false); load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '批量删除失败' })
    } finally { setBusy('') }
  }

  function toggleSelect(p: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(p)) next.delete(p); else next.add(p)
      return next
    })
  }

  /** 上传：任意二进制按 512KB 分块顺序写 files.write_b64（本机/远程节点同通道）。 */
  async function onUpload(f: File) {
    if (!path) return
    const target = joinPath(path, f.name)
    setBusy('upload')
    try {
      let offset = 0
      while (offset < f.size) {
        const buf = await f.slice(offset, offset + CHUNK).arrayBuffer()
        const r = await call<{ offset: number }>('files.write_b64',
          { path: target, offset, data_b64: bufToB64(buf) }, 60)
        offset = r?.offset ?? offset + buf.byteLength
        setMsg({ kind: 'ok',
          text: `上传 ${f.name} ${Math.min(100, Math.round((offset / Math.max(f.size, 1)) * 100))}%` })
      }
      setMsg({ kind: 'ok', text: `已上传 ${f.name}（${gb(f.size)}）` })
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '上传失败' })
    } finally { setBusy('') }
  }

  /** 下载：按 512KB 分块读回 base64 → Blob → 触发浏览器保存（远程节点同样适用）。 */
  async function download(it: FsItem) {
    setBusy('dl:' + it.path)
    try {
      const parts: BlobPart[] = []
      let offset = 0
      for (;;) {
        const r = await call<{ data_b64: string; eof: boolean }>(
          'files.read_b64', { path: it.path, offset, length: CHUNK }, 60)
        if (r?.data_b64) {
          const bytes = b64ToBytes(r.data_b64)
          parts.push(bytes.buffer as ArrayBuffer)   // 辅助函数按精确大小分配，buffer 即数据本身
          offset += bytes.length
        }
        if (!r?.data_b64 || r.eof) break
      }
      const url = URL.createObjectURL(new Blob(parts, { type: 'application/octet-stream' }))
      const a = document.createElement('a')
      a.href = url
      a.download = it.name
      a.click()
      URL.revokeObjectURL(url)
      setMsg({ kind: 'ok', text: `已下载 ${it.name}（${gb(it.size)}）` })
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '下载失败' })
    } finally { setBusy('') }
  }

  function hash(it: FsItem) {
    setBusy('hash:' + it.path)
    call<any>('files.hash', { path: it.path, algo: 'sha256' }, 30)
      .then((d) => setMsg({ kind: 'ok', text: `sha256(${it.name}) = ${d.hash}` }))
      .catch((e) => setMsg({ kind: 'err', text: e?.message || '计算失败' }))
      .finally(() => setBusy(''))
  }

  return (
    <>
      <PageHeader title="文件" sub={<>
        节点 <span className="mono" style={{ color: 'var(--accent)' }}>{name}</span>
        {' '} · 双击进入目录 · 在线编辑 ≤2MB 文本 · 上传/下载支持二进制分块 · 路径全部经过沙箱校验
      </>} />

      {/* 盘符 + 主操作 */}
      <Toolbar
        left={roots.map((r) => (
          <button key={r.path}
                  className={'btn btn-sm ' + (path.toUpperCase().startsWith(r.path.toUpperCase()) ? 'btn-primary' : '')}
                  onClick={() => load(r.path)}>{r.name}</button>
        ))}
        right={<>
          <LiveTag at={at} enabled={auto} intervalMs={POLL_MS} onToggle={() => setAuto((v) => !v)} />
          <button className="btn btn-sm" disabled={!path} onClick={() => setCreating('dir')}>新建文件夹</button>
          <button className="btn btn-sm" disabled={!path} onClick={() => setCreating('file')}>新建文件</button>
          <button className="btn btn-sm" disabled={!path || selected.size === 0}
                  onClick={() => { setClip({ mode: 'copy', paths: [...selected] }); setSelected(new Set()) }}>
            复制{selected.size ? ` ${selected.size}` : ''}
          </button>
          <button className="btn btn-sm" disabled={!path || selected.size === 0}
                  onClick={() => { setClip({ mode: 'cut', paths: [...selected] }); setSelected(new Set()) }}>
            剪切{selected.size ? ` ${selected.size}` : ''}
          </button>
          <button className={'btn btn-sm' + (clip ? ' btn-primary' : '')}
                  disabled={!clip || !path || busy === 'paste'}
                  onClick={paste}>
            粘贴{clip ? ` ${clip.paths.length}` : ''}
          </button>
          <button className="btn btn-sm" disabled={!path || busy === 'list'} onClick={() => load()}>刷新</button>
          <input ref={uploadRef} type="file" style={{ display: 'none' }}
                 onChange={(e) => { const f = e.target.files?.[0]; if (f) onUpload(f); e.target.value = '' }} />
        </>}
      />

      {selected.size > 0 && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10,
          padding: '7px 12px', fontSize: 12.5,
          background: 'var(--accent-soft)', border: '1px solid rgba(158,203,255,.3)',
          borderRadius: 'var(--radius-sm)',
        }}>
          <span style={{ color: 'var(--accent)' }}>已选 {selected.size} 项</span>
          <button className="btn btn-sm btn-danger" disabled={busy === 'bulkdel'}
                  onClick={() => setBulkDeleting(true)}>批量删除</button>
          <button className="btn btn-sm" onClick={() => setSelected(new Set())}>取消选择</button>
        </div>
      )}

      {/* 面包屑 */}
      <div className="crumbs">
        {crumbs.map((c, i) => (
          <span key={c.path} className="crumb">
            {i > 0 && <span className="crumb-sep">›</span>}
            <span className={i === crumbs.length - 1 ? 'crumb-here' : 'crumb-link'}
                  onClick={() => i < crumbs.length - 1 && load(c.path)}>{c.name}</span>
          </span>
        ))}
      </div>

      {msg && (
        <div style={{
          marginBottom: 10, padding: '8px 12px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)', wordBreak: 'break-all',
          color: msg.kind === 'ok' ? 'var(--ok)' : 'var(--danger)',
          background: msg.kind === 'ok' ? 'var(--ok-soft)' : 'var(--danger-soft)',
        }}>{msg.text}</div>
      )}

      <Toolbar left={
        <input className="input input-sm" style={{ width: 220 }} placeholder="过滤当前目录…"
               value={keyword} onChange={(e) => setKeyword(e.target.value)} />
      } right={
        <span className="badge badge-muted">{shown.length} 项</span>
      } />

      <div className="card" style={{ padding: '4px 16px 8px' }}>
        {shown.length === 0 ? (
          <EmptyState title={items.length === 0 ? '空目录' : '没有匹配的项'}
                      hint={items.length === 0 ? '右上角可以新建文件夹 / 文件，或上传' : undefined}
                      action={items.length === 0 && !path ? undefined
                        : undefined} />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 30 }}>
                  <input type="checkbox" className="check-line"
                         checked={shown.length > 0 && shown.every((i) => selected.has(i.path))}
                         onChange={(e) => setSelected(e.target.checked
                           ? new Set(shown.map((i) => i.path)) : new Set())} />
                </th>
                <th className="th-sort" onClick={() => toggleSort('name')}>
                  名称{sortKey === 'name' && (sortDir === 1 ? ' ↑' : ' ↓')}
                </th>
                <th style={{ width: 110, textAlign: 'right' }} className="th-sort"
                    onClick={() => toggleSort('size')}>
                  大小{sortKey === 'size' && (sortDir === 1 ? ' ↑' : ' ↓')}
                </th>
                <th style={{ width: 170 }} className="th-sort" onClick={() => toggleSort('mtime')}>
                  修改时间{sortKey === 'mtime' && (sortDir === 1 ? ' ↑' : ' ↓')}
                </th>
                <th style={{ width: 210, textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((it) => (
                <tr key={it.path} className="row-link"
                    onDoubleClick={() => (it.dir ? load(it.path) : read(it.path))}>
                  <td onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={selected.has(it.path)}
                           onChange={() => toggleSelect(it.path)} />
                  </td>
                  <td>
                    <span className="mono cell-main" style={{ color: it.dir ? 'var(--accent)' : 'var(--text)' }}
                          onDoubleClick={(e) => { e.stopPropagation(); it.dir && load(it.path) }}>
                      {it.dir ? '▸ ' : ''}{it.name}
                    </span>
                  </td>
                  <td className="mono num" style={{ textAlign: 'right', color: 'var(--text-mute)' }}>
                    {it.dir ? '—' : gb(it.size)}
                  </td>
                  <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-mute)' }}>{it.mtime}</td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    {it.dir
                      ? <button className="btn-text" onClick={() => load(it.path)}>打开</button>
                      : <button className="btn-text"
                                disabled={busy === 'read:' + it.path}
                                onClick={() => read(it.path)}>{view?.path === it.path ? '已打开' : '打开'}</button>}
                    {!it.dir && (
                      <button className="btn-text dim" onClick={() => hash(it)}>摘要</button>
                    )}
                    {!it.dir && (
                      <button className="btn-text dim" disabled={busy === 'dl:' + it.path}
                              onClick={() => download(it)}>下载</button>
                    )}
                    <button className="btn-text dim"
                            onClick={() => { setRenaming(it); setRenameTo(it.name) }}>重命名</button>
                    <button className="btn-text danger"
                            onClick={() => setDeleting(it)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 编辑 / 查看 */}
      {view && (
        <Modal title={view.path} onClose={() => setView(null)} width={900}
               footer={view.binary
                 ? <button className="btn" onClick={() => setView(null)}>关闭</button>
                 : <>
                     <span style={{ flex: 1, fontSize: 11.5, color: 'var(--text-mute)' }}>
                       {gb(view.size)} · {view.encoding} · 保存时服务端会再次过沙箱校验
                     </span>
                     <button className="btn" onClick={() => setView(null)}>关闭</button>
                     <button className="btn btn-primary" disabled={busy === 'save'} onClick={save}>
                       {busy === 'save' ? '保存中…' : '保存'}
                     </button>
                   </>}>
          {view.binary ? (
            <div className="empty-state">
              <div className="empty-title">二进制文件，不提供在线编辑</div>
              <div className="empty-hint">只读预览不支持二进制内容（大小 {gb(view.size)}）</div>
            </div>
          ) : (
            <CodeEditor value={view.content} filename={view.path}
                        onChange={(v) => setView({ ...view, content: v })} />
          )}
        </Modal>
      )}

      {/* 新建 */}
      {creating && (
        <Modal title={creating === 'dir' ? '新建文件夹' : '新建文件'} onClose={() => setCreating(null)}
               width={440}
               footer={<>
                 <button className="btn" onClick={() => setCreating(null)}>取消</button>
                 <button className="btn btn-primary" disabled={busy === 'create'} onClick={doCreate}>
                   {busy === 'create' ? '创建中…' : '创建'}
                 </button>
               </>}>
          <Field label="名称" hint={`将创建在 ${path}`}>
            <input className="input" autoFocus value={createName} placeholder={creating === 'dir' ? 'logs' : 'notes.txt'}
                   onChange={(e) => setCreateName(e.target.value)}
                   onKeyDown={(e) => { if (e.key === 'Enter') doCreate() }} />
          </Field>
        </Modal>
      )}

      {/* 重命名 */}
      {renaming && (
        <Modal title="重命名" onClose={() => setRenaming(null)} width={440}
               footer={<>
                 <button className="btn" onClick={() => setRenaming(null)}>取消</button>
                 <button className="btn btn-primary" disabled={busy === 'rename'} onClick={doRename}>
                   {busy === 'rename' ? '处理中…' : '确定'}
                 </button>
               </>}>
          <Field label="新名称">
            <input className="input" autoFocus value={renameTo}
                   onChange={(e) => setRenameTo(e.target.value)}
                   onKeyDown={(e) => { if (e.key === 'Enter') doRename() }} />
          </Field>
        </Modal>
      )}

      {/* 删除确认 */}
      {bulkDeleting && (
        <ConfirmModal title="批量删除" busy={busy === 'bulkdel'}
                      onConfirm={doBulkDelete} onClose={() => setBulkDeleting(false)}
                      text={<>确定删除选中的 <b>{selected.size}</b> 项？
                        目录会连同其全部内容删除，此操作不可撤销。</>} />
      )}

      {deleting && (
        <ConfirmModal title="删除" busy={busy === 'del'} onConfirm={doDelete}
                      onClose={() => setDeleting(null)}
                      text={<>确定删除 <b className="mono">{deleting.name}</b>？
                        {deleting.dir && '目录及其**全部内容**会被删除，'}
                        此操作不可撤销。</>} />
      )}
    </>
  )
}

function gb(n: number): string {
  if (!n && n !== 0) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`
  return `${(n / 1073741824).toFixed(2)} GB`
}

/** ArrayBuffer → base64（分段转换，避开 String.fromCharCode 的参数数上限） */
function bufToB64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000)
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(bin)
}

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const buf = new ArrayBuffer(bin.length)   // 显式 ArrayBuffer 背衬：可直接作为 BlobPart
  const out = new Uint8Array(buf)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

/** 按服务端平台拼路径 —— files.list 返回的分隔符就是服务端平台自己的（硬编码 '\' 在 Linux 会生成带反斜杠的文件名） */
function joinPath(dir: string, name: string): string {
  const sep = dir.includes('\\') ? '\\' : '/'
  return dir.endsWith('/') || dir.endsWith('\\') ? dir + name : dir + sep + name
}
