import { useCallback, useState } from 'react'
import { PageHeader, Toolbar, Badge, EmptyState, Modal, Field } from '../components/ui'
import { LiveTag, usePoll } from '../components/Live'
import { useNode } from '../node'

export default function Firewall() {
  const { call } = useNode()
  const [st, setSt] = useState<any>(null)
  const [rules, setRules] = useState<any>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState('')
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ port: '', proto: 'TCP', name: '' })
  const [auto, setAuto] = useState(true)
  const [at, setAt] = useState(0)

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([
        call<any>('firewall.status', {}, 25),
        call<any>('firewall.rules', {}, 25),
      ])
      setSt(s); setRules(r); setMsg(null); setAt(Date.now())
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '读取失败' }) }
  }, [call])
  usePoll(load, 15000, auto)

  async function toggle() {
    if (!st) return
    setBusy('toggle')
    try {
      const r = await call<any>('firewall.enable', { on: !st.enabled }, 40)
      if (r.ok) setMsg({ kind: 'ok', text: `防火墙已${!st.enabled ? '开启' : '关闭'}` })
      else setMsg({ kind: 'err', text: r.error || r.data || '操作失败（权限不足？）' })
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '操作失败' }) }
    finally { setBusy('') }
  }

  async function allow(remove: boolean) {
    setBusy('allow')
    try {
      const r = await call<any>('firewall.allow', {
        port: Number(form.port), proto: form.proto, name: form.name, remove,
      }, 40)
      if (r.ok) setMsg({ kind: 'ok', text: `${remove ? '撤销' : '放行'} ${form.port}/${form.proto} 成功` })
      else setMsg({ kind: 'err', text: (r.data || r.error || '操作失败').toString() })
      setAdding(false); load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '操作失败' }) }
    finally { setBusy('') }
  }

  const ruleList: string[] = rules?.rules || []

  return (
    <>
      <PageHeader title="防火墙" sub="管系统自己的防火墙（Windows netsh · Linux ufw/firewalld/iptables），不做包过滤实现"
        actions={<button className="btn" disabled={busy === 'toggle' || !st} onClick={toggle}>
          {st?.enabled ? '关闭防火墙' : '开启防火墙'}</button>} />
      {msg && <div className={msg.kind === 'ok' ? 'msg-ok' : 'msg-err'} style={{ marginBottom: 10, fontSize: 13 }}>{msg.text}</div>}

      <div className="card" style={{ padding: 14, marginBottom: 12, display: 'flex', gap: 24, alignItems: 'center', fontSize: 13 }}>
        <span>后端：<b>{st?.backend || '…'}</b></span>
        <span>状态：<Badge tone={st?.enabled === true ? 'ok' : st?.enabled === false ? 'bad' : 'muted'}>
          {st?.enabled === true ? '已开启' : st?.enabled === false ? '已关闭' : '未知'}</Badge></span>
        <LiveTag at={at} enabled={auto} intervalMs={15000}
                 onToggle={() => setAuto((v) => !v)} />
      </div>

      <Toolbar right={<button className="btn btn-primary" onClick={() => setAdding(true)}>放行端口</button>} />
      <div className="card">
        {ruleList.length === 0 && rules?.raw ? (
          <pre className="mono" style={{ margin: 0, padding: 14, fontSize: 12, whiteSpace: 'pre-wrap', maxHeight: '52vh', overflow: 'auto' }}>{rules.raw}</pre>
        ) : ruleList.length === 0 ? (
          <EmptyState title="暂无规则" hint="点右上角「放行端口」添加第一条放行规则" />
        ) : (
          <table className="table">
            <thead><tr><th>规则</th></tr></thead>
            <tbody>{ruleList.map((r, i) => <tr key={i}><td className="mono" style={{ fontSize: 12.5 }}>{r}</td></tr>)}</tbody>
          </table>
        )}
      </div>

      {adding && (
        <Modal title="放行端口" onClose={() => setAdding(false)} width={460}
          footer={<>
            <button className="btn" onClick={() => { setForm(f => ({ ...f, remove: true } as any)); allow(true) }} disabled={busy === 'allow'}>撤销此端口</button>
            <button className="btn btn-primary" onClick={() => allow(false)} disabled={busy === 'allow'}>{busy === 'allow' ? '处理中…' : '放行'}</button>
          </>}>
          <Field label="端口" hint="1-65535">
            <input className="input" type="number" value={form.port}
              onChange={(e) => setForm({ ...form, port: e.target.value })} />
          </Field>
          <Field label="协议">
            <select className="input" value={form.proto} onChange={(e) => setForm({ ...form, proto: e.target.value })}>
              <option>TCP</option><option>UDP</option>
            </select>
          </Field>
          <Field label="规则名" hint="留空自动命名 zpanel-port-<端口>">
            <input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
        </Modal>
      )}
    </>
  )
}
