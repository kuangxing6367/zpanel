import { useCallback, useEffect, useMemo, useState } from 'react'
import { PageHeader, Toolbar, Modal, Field, EmptyState, Badge, ConfirmModal } from '../components/ui'
import { useNode } from '../node'

/* ============================================================
   计划任务 —— 面板只管两件事：
   ① 记下来（任务清单落库）
   ② 交给系统的调度器去做（Linux crontab / Windows schtasks）

   面板**不自养定时器**：真正到点执行的是系统调度器。
   表达式校验、下次触发时间都由 cron 机制包算，不在前端猜。
   ============================================================ */

interface Task {
  id: string; name: string; expr: string; command: string
  enabled: boolean; remark?: string; status?: string; note?: string
  created_at?: string; updated_at?: string; next_runs?: string[]
}

const STATUS: Record<string, { tone: any; text: string }> = {
  pending: { tone: 'muted', text: '未下发' },
  applied: { tone: 'ok', text: '已下发' },
  removed: { tone: 'muted', text: '已撤销' },
  error: { tone: 'bad', text: '下发失败' },
  unsupported: { tone: 'warn', text: '调度器不可用' },
  'no-backend': { tone: 'warn', text: '无系统调度器' },
}

/** 常用表达式：点一下就填，省得背 cron 字段顺序 */
const PRESETS: { label: string; expr: string }[] = [
  { label: '每分钟', expr: '* * * * *' },
  { label: '每 5 分钟', expr: '*/5 * * * *' },
  { label: '每小时', expr: '0 * * * *' },
  { label: '每天 03:00', expr: '0 3 * * *' },
  { label: '每周一 03:00', expr: '0 3 * * 1' },
  { label: '每月 1 日 03:00', expr: '0 3 1 * *' },
]

export default function Scheduler() {
  const { call } = useNode()
  const [tasks, setTasks] = useState<Task[]>([])
  const [backend, setBackend] = useState<any>(null)
  const [editing, setEditing] = useState<Task | null>(null)
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState<Task | null>(null)
  const [running, setRunning] = useState<Task | null>(null)
  const [output, setOutput] = useState<any>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      const r = await call<{ tasks: Task[]; count: any }>('scheduler.list', {}, 20)
      setTasks(r.tasks || [])
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '读取任务失败' }) }
    try { setBackend(await call<any>('scheduler.backends', {}, 20)) } catch { /* 无调度器时忽略 */ }
  }, [call])
  useEffect(() => { load() }, [load])

  async function doApply() {
    setBusy('apply')
    try {
      const r = await call<any>('scheduler.apply', {}, 90)
      const bad = (r.results || []).filter((x: any) => !x.ok)
      setMsg(bad.length
        ? { kind: 'err', text: `下发 ${r.count} 条，${bad.length} 条失败：${bad[0].output || bad[0].status}` }
        : { kind: 'ok', text: `已下发 ${r.count} 条到系统调度器（${r.backend || '无'}）` })
      if (r.missing_in_system?.length) {
        setMsg((m) => ({ kind: 'err', text: (m?.text || '') + `；系统里缺失 ${r.missing_in_system.length} 条` }))
      }
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '下发失败' }) }
    finally { setBusy('') }
  }

  async function doRun(t: Task) {
    setBusy('run'); setRunning(t); setOutput(null)
    try { setOutput(await call<any>('scheduler.run', { id: t.id }, 150)) }
    catch (e: any) { setOutput({ ok: false, error: e?.message || '执行失败' }) }
    finally { setBusy('') }
  }

  async function doDelete() {
    if (!deleting) return
    setBusy('del')
    try {
      await call('scheduler.remove', { id: deleting.id }, 60)
      setMsg({ kind: 'ok', text: `已删除任务「${deleting.name}」${deleting.status === 'applied' ? '（系统调度里的也一并撤销）' : ''}` })
      setDeleting(null); load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '删除失败' }) }
    finally { setBusy('') }
  }

  async function toggle(t: Task) {
    try {
      await call('scheduler.update', { id: t.id, enabled: !t.enabled }, 30)
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '修改失败' }) }
  }

  const onCount = useMemo(() => tasks.filter((t) => t.enabled).length, [tasks])

  return (
    <>
      <PageHeader title="计划任务"
        sub={backend?.backend
          ? `系统调度器：${backend.backend}（${backend.platform}）· 面板只负责登记，到点执行由系统调度器完成`
          : '本机没有可用的系统调度器（crontab / schtasks），任务只登记在面板内'}
        actions={
          <>
            <button className="btn" disabled={busy === 'apply' || tasks.length === 0} onClick={doApply}>
              {busy === 'apply' ? '下发中…' : '下发到系统调度器'}
            </button>
            <button className="btn btn-primary" onClick={() => setCreating(true)}>新建任务</button>
          </>
        } />

      {msg && <div className={msg.kind === 'ok' ? 'msg-ok' : 'msg-err'}
        style={{ marginBottom: 10, fontSize: 13, wordBreak: 'break-all' }}>{msg.text}</div>}

      <Toolbar left={
        <span style={{ fontSize: 13, color: 'var(--text-dim)' }}>
          共 {tasks.length} 个任务，启用 {onCount} 个
        </span>
      } right={<button className="btn btn-sm" onClick={load}>刷新</button>} />

      <div className="card">
        {tasks.length === 0 ? (
          <EmptyState title="还没有计划任务"
            hint="点右上角「新建任务」，填 cron 表达式与要执行的命令"
            action={<button className="btn btn-primary" onClick={() => setCreating(true)}>新建任务</button>} />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 150 }}>名称</th>
                <th style={{ width: 130 }}>表达式</th>
                <th>命令</th>
                <th style={{ width: 155 }}>下次触发</th>
                <th style={{ width: 90 }}>状态</th>
                <th style={{ width: 60 }}>启用</th>
                <th style={{ width: 170, textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t) => {
                const st = STATUS[t.status || 'pending'] || STATUS.pending
                return (
                  <tr key={t.id}>
                    <td className="cell-main" title={t.remark || ''}>{t.name}</td>
                    <td className="mono" style={{ fontSize: 12.5 }}>{t.expr}</td>
                    <td className="mono" style={{ fontSize: 12, maxWidth: 240, overflow: 'hidden',
                      textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={t.command}>{t.command}</td>
                    <td style={{ fontSize: 12.5 }}>
                      {t.enabled ? (t.next_runs?.[0] || '—') : <span style={{ color: 'var(--text-dim)' }}>已停用</span>}
                    </td>
                    <td><Badge tone={st.tone}>{st.text}</Badge></td>
                    <td>
                      <input type="checkbox" checked={!!t.enabled} onChange={() => toggle(t)} />
                    </td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      <button className="btn btn-sm" disabled={busy === 'run'} onClick={() => doRun(t)}>执行</button>{' '}
                      <button className="btn btn-sm" onClick={() => setEditing(t)}>编辑</button>{' '}
                      <button className="btn btn-sm btn-danger" onClick={() => setDeleting(t)}>删除</button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {(creating || editing) && (
        <TaskModal
          task={editing || undefined}
          call={call}
          onClose={() => { setCreating(false); setEditing(null) }}
          onSaved={(name, isNew) => {
            setMsg({ kind: 'ok', text: isNew ? `已创建「${name}」，记得点「下发到系统调度器」` : `已更新「${name}」` })
            setCreating(false); setEditing(null); load()
          }} />
      )}

      {deleting && (
        <ConfirmModal title="删除任务" busy={busy === 'del'} onConfirm={doDelete}
          onClose={() => setDeleting(null)}
          text={<>确定删除任务 <b>{deleting.name}</b>？
            {deleting.status === 'applied' && ' 它已下发到系统调度器，删除时会一并撤销。'}</>} />
      )}

      {running && (
        <Modal title={`执行「${running.name}」`} onClose={() => { setRunning(null); setOutput(null) }} width={720}
          footer={<button className="btn" onClick={() => { setRunning(null); setOutput(null) }}>关闭</button>}>
          {!output ? <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>执行中…</div> : (
            <>
              <div style={{ fontSize: 12.5, marginBottom: 8, display: 'flex', gap: 14 }}>
                <Badge tone={output.ok ? 'ok' : 'bad'}>{output.ok ? '执行成功' : '执行失败'}</Badge>
                <span>退出码 {output.code ?? '—'}</span>
                <span style={{ color: 'var(--text-dim)' }}>耗时 {output.duration_ms ?? '—'} ms</span>
              </div>
              <pre className="mono" style={{ fontSize: 12, lineHeight: 1.6, maxHeight: '46vh',
                overflow: 'auto', background: 'var(--bg-elev)', padding: 10,
                borderRadius: 'var(--radius-sm)', whiteSpace: 'pre-wrap', margin: 0 }}>
                {output.stdout || output.stderr || output.error || '（无输出）'}
              </pre>
            </>
          )}
        </Modal>
      )}
    </>
  )
}

/* ── 新建 / 编辑 ─────────────────────────────────────── */
function TaskModal({ task, call, onClose, onSaved }: {
  task?: Task; call: any; onClose: () => void
  onSaved: (name: string, isNew: boolean) => void
}) {
  const isNew = !task
  const [form, setForm] = useState({
    name: task?.name || '', expr: task?.expr || '0 3 * * *',
    command: task?.command || '', remark: task?.remark || '', enabled: task?.enabled ?? true,
  })
  const [check, setCheck] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  // 表达式变了就校验（防抖 350ms）—— 描述与下次触发都由后端 cron 机制包算
  useEffect(() => {
    if (!form.expr.trim()) { setCheck(null); return }
    const h = setTimeout(async () => {
      try { setCheck(await call('scheduler.validate', { expr: form.expr.trim() }, 20)) }
      catch (e: any) { setCheck({ ok: false, error: e?.message || '校验失败' }) }
    }, 350)
    return () => clearTimeout(h)
  }, [form.expr, call])

  async function save() {
    setBusy(true); setErr('')
    try {
      const payload = { name: form.name, expr: form.expr, command: form.command,
        remark: form.remark, enabled: form.enabled }
      if (isNew) await call('scheduler.create', payload, 40)
      else await call('scheduler.update', { id: task!.id, ...payload }, 40)
      onSaved(form.name, isNew)
    } catch (e: any) { setErr(e?.message || '保存失败') }
    finally { setBusy(false) }
  }

  const okToSave = form.name.trim() && form.command.trim() && check?.ok

  return (
    <Modal title={isNew ? '新建计划任务' : `编辑「${task!.name}」`} onClose={onClose} width={620}
      footer={<>
        <button className="btn" onClick={onClose}>取消</button>
        <button className="btn btn-primary" disabled={busy || !okToSave} onClick={save}>
          {busy ? '保存中…' : (isNew ? '创建' : '保存')}</button>
      </>}>
      {err && <div className="msg-err" style={{ marginBottom: 10, fontSize: 13 }}>{err}</div>}
      <Field label="任务名">
        <input className="input" value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="如：每日备份" />
      </Field>
      <Field label="cron 表达式" hint="分 时 日 月 周">
        <input className="input mono" value={form.expr}
          onChange={(e) => setForm({ ...form, expr: e.target.value })} placeholder="0 3 * * *" />
      </Field>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', margin: '6px 0 10px' }}>
        {PRESETS.map((p) => (
          <button key={p.expr} className="btn btn-sm" onClick={() => setForm({ ...form, expr: p.expr })}>
            {p.label}
          </button>
        ))}
      </div>
      {check && (
        <div style={{ fontSize: 12.5, marginBottom: 10, lineHeight: 1.7 }}>
          {check.ok ? (
            <>
              <div><Badge tone="ok">表达式有效</Badge> <span style={{ marginLeft: 6 }}>{check.describe}</span></div>
              <div style={{ color: 'var(--text-dim)' }}>接下来：{(check.next_runs || []).join(' · ')}</div>
            </>
          ) : <div className="msg-err" style={{ display: 'inline-block', padding: '3px 8px' }}>
            表达式无效：{check.error}</div>}
        </div>
      )}
      <Field label="要执行的命令" hint="整条交给系统 shell">
        <input className="input mono" value={form.command}
          onChange={(e) => setForm({ ...form, command: e.target.value })}
          placeholder="如：/usr/bin/python3 /opt/backup.py" />
      </Field>
      <Field label="备注" hint="选填">
        <input className="input" value={form.remark}
          onChange={(e) => setForm({ ...form, remark: e.target.value })} />
      </Field>
      <label style={{ fontSize: 13, display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
        <input type="checkbox" checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
        启用（停用的任务不会下发到系统调度器）
      </label>
      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 10, lineHeight: 1.7 }}>
        创建后还需点页头的「下发到系统调度器」，任务才会交给 crontab / schtasks 真正跑起来。
      </div>
    </Modal>
  )
}
