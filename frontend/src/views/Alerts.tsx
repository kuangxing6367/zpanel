import { useCallback, useEffect, useState } from 'react'
import { PageHeader, Toolbar, Modal, Field, EmptyState, ConfirmModal, Badge } from '../components/ui'
import { useNode } from '../node'

const METRIC_LABEL: Record<string, string> = {
  cpu: 'CPU 使用率 %', mem: '内存使用率 %', disk: '磁盘使用率 %',
  load: '负载 (1m)', cert_days: '证书剩余天数',
}

const CH_TYPE_LABEL: Record<string, string> = { smtp: '邮件 SMTP', webhook: 'Webhook' }

const emptyChannel = () => ({
  id: '', name: '', type: 'smtp',
  host: '', port: '465', tls: 'ssl', user: '', password: '',
  sender: '', from_name: 'ZPanel 告警', to: '',
  url: '', bearer: '', timeout: '8',
})

/** 通道"发到哪"的一句话摘要（密钥不回显，只标"已配置"）。 */
function channelTarget(c: any): string {
  const cfg = c.config || {}
  if (c.type === 'smtp') return `${cfg.host || '—'}:${cfg.port || '—'} → ${cfg.to || '（未设收件人）'}`
  return cfg.url || '—'
}

export default function Alerts() {
  const { call } = useNode()
  const [rules, setRules] = useState<any[]>([])
  const [events, setEvents] = useState<any[]>([])
  const [channels, setChannels] = useState<any[]>([])
  const [delivs, setDelivs] = useState<any[]>([])
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState('')
  const [editing, setEditing] = useState<null | {
    name: string; metric: string; target: string; op: string; value: string
    hits: string; webhook: string; channels: string[] }>(null)
  const [chEditing, setChEditing] = useState<null | ReturnType<typeof emptyChannel>>(null)
  const [removing, setRemoving] = useState<any>(null)
  const [chRemoving, setChRemoving] = useState<any>(null)

  const load = useCallback(async () => {
    try {
      const [r, ev, ch, dv] = await Promise.all([
        call<any>('alerts.list', {}, 20),
        call<any>('alerts.events', { limit: 50 }, 20),
        call<any>('alerts.channels', {}, 20),
        call<any>('alerts.deliveries', { limit: 30 }, 20),
      ])
      setRules(r?.rules || []); setEvents(ev?.events || [])
      setChannels(ch?.channels || []); setDelivs(dv?.deliveries || [])
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '读取失败' }) }
  }, [call])
  useEffect(() => { load() }, [load])

  async function checkNow() {
    setBusy('check')
    try {
      const d: any = await call<any>('alerts.check', {}, 60)
      const fired = d?.fired || []
      const bad = fired.flatMap((f: any) => (f.delivered || []).filter((x: any) => !x.ok))
      setMsg({
        kind: bad.length ? 'err' : 'ok',
        text: `检查完成：${d?.checked ?? 0} 条规则，触发 ${fired.length} 条`
          + (bad.length ? `；${bad.length} 个通道投递失败（${bad.map((b: any) => b.channel).join('、')}）` : ''),
      })
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '检查失败' }) }
    finally { setBusy('') }
  }

  async function toggle(rule: any) {
    setBusy(rule.id)
    try {
      await call<any>('alerts.update', { id: rule.id, enabled: !rule.enabled }, 20)
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '操作失败' }) }
    finally { setBusy('') }
  }

  async function save() {
    if (!editing) return
    setBusy('save')
    try {
      const body = {
        name: editing.name, metric: editing.metric, target: editing.target, op: editing.op,
        value: Number(editing.value), hits: Number(editing.hits) || 1, webhook: editing.webhook,
        channels: editing.channels,
      }
      const r: any = await call<any>('alerts.create', body, 30)
      if (r?.ok) { setEditing(null); setMsg({ kind: 'ok', text: '规则已创建' }); load() }
      else setMsg({ kind: 'err', text: r?.error || '创建失败' })
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '创建失败' }) }
    finally { setBusy('') }
  }

  // 通道：config 按类型拼装；空密码/掩码保持不变（后端认掩码）
  function chConfig() {
    const e = chEditing!
    if (e.type === 'smtp') {
      return {
        host: e.host.trim(), port: Number(e.port) || 465, tls: e.tls,
        user: e.user.trim(), password: e.password,
        sender: e.sender.trim(), from_name: e.from_name.trim(), to: e.to.trim(),
      }
    }
    return { url: e.url.trim(), bearer: e.bearer, timeout: Number(e.timeout) || 8 }
  }

  async function saveChannel() {
    if (!chEditing) return
    setBusy('ch-save')
    try {
      const r: any = await call<any>('alerts.channel_save', {
        id: chEditing.id || undefined, name: chEditing.name, type: chEditing.type,
        config: chConfig(), enabled: true,
      }, 30)
      if (r?.ok) { setChEditing(null); setMsg({ kind: 'ok', text: '通道已保存' }); load() }
      else setMsg({ kind: 'err', text: r?.error || '保存失败' })
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '保存失败' }) }
    finally { setBusy('') }
  }

  async function testChannel(c: any) {
    setBusy('t-' + c.id)
    try {
      const r: any = await call<any>('alerts.channel_test', { id: c.id }, 40)
      if (r?.ok) setMsg({ kind: 'ok', text: `「${c.name}」测试已发出：${JSON.stringify(r.result || {})}` })
      else setMsg({ kind: 'err', text: `「${c.name}」测试失败：${r?.error || '未知原因'}` })
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '测试失败' }) }
    finally { setBusy('') }
  }

  async function toggleChannel(c: any) {
    setBusy('c-' + c.id)
    try {
      await call<any>('alerts.channel_save', {
        id: c.id, name: c.name, type: c.type, config: c.config, enabled: !c.enabled,
      }, 20)
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '操作失败' }) }
    finally { setBusy('') }
  }

  async function remove() {
    if (!removing) return
    setBusy('remove')
    try {
      await call<any>('alerts.remove', { id: removing.id }, 20)
      setRemoving(null); load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '删除失败' }) }
    finally { setBusy('') }
  }

  async function removeChannel() {
    if (!chRemoving) return
    setBusy('ch-remove')
    try {
      await call<any>('alerts.channel_remove', { id: chRemoving.id }, 20)
      setChRemoving(null); setMsg({ kind: 'ok', text: '通道已删除，规则上的绑定已自动摘除' }); load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '删除失败' }) }
    finally { setBusy('') }
  }

  function editChannel(c: any) {
    const cfg = c.config || {}
    setChEditing({
      ...emptyChannel(), ...Object.fromEntries(
        Object.entries(cfg).map(([k, v]) => [k, v == null ? '' : String(v)])),
      id: c.id, name: c.name, type: c.type,
      port: String(cfg.port ?? '465'), timeout: String(cfg.timeout ?? '8'),
    } as any)
  }

  return (
    <>
      <PageHeader title="告警"
        sub="阈值规则（CPU/内存/磁盘/负载/证书到期）→ 多通道通知（邮件 / Webhook），防抖后落事件与投递记录；定时执行由计划任务调 alerts.check"
        actions={<>
          <button className="btn" disabled={busy === 'check'} onClick={checkNow}>{busy === 'check' ? '检查中…' : '立即检查'}</button>
          <button className="btn" onClick={() => setChEditing(emptyChannel())}>新建通道</button>
          <button className="btn btn-primary" onClick={() => setEditing({
            name: '', metric: 'cpu', target: '', op: '>', value: '90', hits: '3', webhook: '',
            channels: channels.filter((c) => c.enabled).map((c) => c.id),
          })}>新建规则</button>
        </>} />
      {msg && <div className={msg.kind === 'ok' ? 'msg-ok' : 'msg-err'} style={{ marginBottom: 10, fontSize: 13 }}>{msg.text}</div>}

      <Toolbar left={<span style={{ fontSize: 13, color: 'var(--text-dim)' }}>通知通道（{channels.length}）</span>} />
      <div className="card" style={{ marginBottom: 12 }}>
        {channels.length === 0 ? (
          <EmptyState title="还没有通知通道" hint="建一个邮件或 Webhook 通道，触发时才会有人收到" />
        ) : (
          <table className="table">
            <thead><tr>
              <th style={{ width: 150 }}>通道</th><th style={{ width: 110 }}>类型</th>
              <th>发往</th><th style={{ width: 130 }}>最近测试</th>
              <th style={{ width: 190, textAlign: 'right' }}>操作</th>
            </tr></thead>
            <tbody>
              {channels.map((c) => (
                <tr key={c.id} style={{ opacity: c.enabled ? 1 : 0.5 }}>
                  <td>{c.name}</td>
                  <td>{c.type === 'smtp' ? <Badge tone="ok">邮件</Badge> : <Badge tone="muted">Webhook</Badge>}</td>
                  <td className="mono" style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                    {channelTarget(c)}{c.has_secret && <span> · 密钥已配置</span>}
                  </td>
                  <td style={{ fontSize: 12 }}>
                    {!c.last_test_at ? <span style={{ color: 'var(--text-dim)' }}>未测试</span>
                      : c.last_test_ok ? <Badge tone="ok">{new Date(c.last_test_at * 1000).toLocaleString()}</Badge>
                      : <span title={c.last_error}><Badge tone="bad">失败</Badge></span>}
                  </td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <button className="btn btn-sm" disabled={busy === 't-' + c.id} onClick={() => testChannel(c)}>
                      {busy === 't-' + c.id ? '发送中…' : '测试'}</button>{' '}
                    <button className="btn btn-sm" onClick={() => editChannel(c)}>编辑</button>{' '}
                    <button className="btn btn-sm" disabled={busy === 'c-' + c.id} onClick={() => toggleChannel(c)}>
                      {c.enabled ? '停用' : '启用'}</button>{' '}
                    <button className="btn btn-sm btn-danger" onClick={() => setChRemoving(c)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Toolbar left={<span style={{ fontSize: 13, color: 'var(--text-dim)' }}>规则（{rules.length}）</span>} />
      <div className="card" style={{ marginBottom: 12 }}>
        {rules.length === 0 ? (
          <EmptyState title="还没有告警规则" hint="例如：CPU > 90 连续 3 次告警" />
        ) : (
          <table className="table">
            <thead><tr>
              <th>规则</th><th>条件</th><th style={{ width: 60 }}>防抖</th>
              <th style={{ width: 90 }}>状态</th><th style={{ width: 110 }}>现值</th>
              <th style={{ width: 180 }}>通知</th>
              <th style={{ width: 150, textAlign: 'right' }}>操作</th>
            </tr></thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id} style={{ opacity: r.enabled ? 1 : 0.5 }}>
                  <td>{r.name}{r.target && <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-dim)' }}> ({r.target})</span>}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {METRIC_LABEL[r.metric] || r.metric} {r.op} {r.value}
                  </td>
                  <td style={{ fontSize: 12.5 }}>{r.hits} 次</td>
                  <td>{r.firing ? <Badge tone="bad">触发中</Badge>
                    : r.enabled ? <Badge tone="ok">监控中</Badge>
                    : <Badge tone="muted">已禁用</Badge>}</td>
                  <td style={{ fontSize: 12.5 }}>{r.last_value ?? '—'}</td>
                  <td style={{ fontSize: 12 }}>
                    {(r.channels || []).length === 0
                      ? <span style={{ color: 'var(--text-dim)' }}>
                          {(r.webhook ? 'Legacy Webhook' : channels.length ? '全部通道' : '仅记事件')}</span>
                      : (r.channels || []).map((id: string) => (
                        <span key={id} style={{ marginRight: 6 }}>
                          {channels.find((c) => c.id === id)?.name || <span style={{ color: 'var(--danger)' }}>已删除</span>}
                        </span>))}
                  </td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <button className="btn btn-sm" disabled={busy === r.id} onClick={() => toggle(r)}>{r.enabled ? '禁用' : '启用'}</button>{' '}
                    <button className="btn btn-sm btn-danger" onClick={() => setRemoving(r)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Toolbar left={<span style={{ fontSize: 13, color: 'var(--text-dim)' }}>事件历史（最近 {events.length} 条）</span>} />
      <div className="card" style={{ marginBottom: 12 }}>
        {events.length === 0 ? (
          <EmptyState title="暂无告警事件" hint="触发/恢复记录会落在这里" />
        ) : (
          <table className="table">
            <thead><tr><th style={{ width: 170 }}>时间</th><th style={{ width: 90 }}>级别</th><th>内容</th></tr></thead>
            <tbody>
              {events.map((ev) => (
                <tr key={ev.id}>
                  <td style={{ fontSize: 12.5 }}>{new Date(ev.created_at * 1000).toLocaleString()}</td>
                  <td>{ev.level === 'recover' ? <Badge tone="ok">恢复</Badge> : <Badge tone="warn">告警</Badge>}</td>
                  <td style={{ fontSize: 12.5 }}>{ev.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Toolbar left={<span style={{ fontSize: 13, color: 'var(--text-dim)' }}>投递记录（最近 {delivs.length} 条）</span>} />
      <div className="card">
        {delivs.length === 0 ? (
          <EmptyState title="暂无投递记录" hint="每个通道的成功/失败都会留痕，失败原因带上游原文" />
        ) : (
          <table className="table">
            <thead><tr>
              <th style={{ width: 170 }}>时间</th><th style={{ width: 90 }}>级别</th>
              <th style={{ width: 150 }}>通道</th><th style={{ width: 80 }}>结果</th><th>失败原因</th>
            </tr></thead>
            <tbody>
              {delivs.map((d) => (
                <tr key={d.id}>
                  <td style={{ fontSize: 12.5 }}>{new Date(d.created_at * 1000).toLocaleString()}</td>
                  <td><Badge tone={d.level === 'recover' ? 'ok' : d.level === 'test' ? 'muted' : 'warn'}>
                    {d.level === 'recover' ? '恢复' : d.level === 'test' ? '测试' : '告警'}</Badge></td>
                  <td style={{ fontSize: 12.5 }}>{d.channel_name}</td>
                  <td>{d.ok ? <Badge tone="ok">成功</Badge> : <Badge tone="bad">失败</Badge>}</td>
                  <td className="mono" style={{ fontSize: 11.5, color: 'var(--danger)' }}>{d.error || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {editing && (
        <Modal title="新建告警规则" onClose={() => setEditing(null)} width={480}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>取消</button>
            <button className="btn btn-primary" disabled={busy === 'save' || !editing.name.trim()} onClick={save}>
              {busy === 'save' ? '保存中…' : '创建'}</button>
          </>}>
          <Field label="规则名">
            <input className="input" value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
          </Field>
          <Field label="指标">
            <select className="input" value={editing.metric} onChange={(e) => setEditing({ ...editing, metric: e.target.value })}>
              {Object.entries(METRIC_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </Field>
          {editing.metric === 'disk' && (
            <Field label="挂载点" hint="留空取使用率最高的盘">
              <input className="input mono" value={editing.target} onChange={(e) => setEditing({ ...editing, target: e.target.value })} />
            </Field>
          )}
          {editing.metric === 'cert_days' && (
            <Field label="巡检域名" hint="直连 443 抓真实证书">
              <input className="input mono" placeholder="www.example.com" value={editing.target}
                onChange={(e) => setEditing({ ...editing, target: e.target.value })} />
            </Field>
          )}
          <div style={{ display: 'flex', gap: 10 }}>
            <Field label="比较">
              <select className="input" value={editing.op} onChange={(e) => setEditing({ ...editing, op: e.target.value })}>
                {['>', '>=', '<', '<=', '==', '!='].map((o) => <option key={o}>{o}</option>)}
              </select>
            </Field>
            <Field label="阈值">
              <input className="input" type="number" value={editing.value} onChange={(e) => setEditing({ ...editing, value: e.target.value })} />
            </Field>
            <Field label="连续命中" hint="防抖">
              <input className="input" type="number" value={editing.hits} onChange={(e) => setEditing({ ...editing, hits: e.target.value })} />
            </Field>
          </div>
          <Field label="通知通道" hint="全部不勾 = 用所有启用的通道">
            {channels.length === 0 ? (
              <div style={{ fontSize: 12.5, color: 'var(--text-dim)' }}>还没有通道，先去上面建一个</div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {channels.map((c) => (
                  <label key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                    <input type="checkbox" checked={editing.channels.includes(c.id)}
                      onChange={(e) => setEditing({
                        ...editing,
                        channels: e.target.checked
                          ? [...editing.channels, c.id]
                          : editing.channels.filter((x) => x !== c.id),
                      })} />
                    {c.name} <span style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>
                      {CH_TYPE_LABEL[c.type] || c.type}</span>
                  </label>
                ))}
              </div>
            )}
          </Field>
          <Field label="Legacy Webhook" hint="旧字段：只在不勾任何通道时生效，留空即可">
            <input className="input mono" value={editing.webhook} onChange={(e) => setEditing({ ...editing, webhook: e.target.value })} />
          </Field>
        </Modal>
      )}

      {chEditing && (
        <Modal title={chEditing.id ? '编辑通道' : '新建通道'} onClose={() => setChEditing(null)} width={480}
          footer={<>
            <button className="btn" onClick={() => setChEditing(null)}>取消</button>
            <button className="btn btn-primary" disabled={busy === 'ch-save' || !chEditing.name.trim()} onClick={saveChannel}>
              {busy === 'ch-save' ? '保存中…' : '保存'}</button>
          </>}>
          <Field label="通道名">
            <input className="input" value={chEditing.name}
              onChange={(e) => setChEditing({ ...chEditing, name: e.target.value })} />
          </Field>
          <Field label="类型">
            <select className="input" value={chEditing.type} disabled={!!chEditing.id}
              onChange={(e) => setChEditing({ ...chEditing, type: e.target.value })}>
              <option value="smtp">邮件 SMTP</option>
              <option value="webhook">Webhook（HTTP）</option>
            </select>
          </Field>

          {chEditing.type === 'smtp' ? (
            <>
              <div style={{ display: 'flex', gap: 10 }}>
                <Field label="服务器">
                  <input className="input mono" placeholder="smtp.qq.com" value={chEditing.host}
                    onChange={(e) => setChEditing({ ...chEditing, host: e.target.value })} />
                </Field>
                <Field label="端口" hint="465=SSL">
                  <input className="input" type="number" style={{ width: 100 }} value={chEditing.port}
                    onChange={(e) => setChEditing({ ...chEditing, port: e.target.value })} />
                </Field>
              </div>
              <Field label="加密">
                <select className="input" value={chEditing.tls}
                  onChange={(e) => setChEditing({ ...chEditing, tls: e.target.value })}>
                  <option value="ssl">SSL（465）</option>
                  <option value="starttls">STARTTLS（587）</option>
                  <option value="none">不加密</option>
                </select>
              </Field>
              <div style={{ display: 'flex', gap: 10 }}>
                <Field label="账号">
                  <input className="input mono" value={chEditing.user}
                    onChange={(e) => setChEditing({ ...chEditing, user: e.target.value })} />
                </Field>
                <Field label="密码 / 授权码" hint={chEditing.id ? '留空=不修改' : ''}>
                  <input className="input" type="password" placeholder={chEditing.id ? '••••••' : ''} value={chEditing.password}
                    onChange={(e) => setChEditing({ ...chEditing, password: e.target.value })} />
                </Field>
              </div>
              <div style={{ display: 'flex', gap: 10 }}>
                <Field label="发件人" hint="留空用账号">
                  <input className="input mono" value={chEditing.sender}
                    onChange={(e) => setChEditing({ ...chEditing, sender: e.target.value })} />
                </Field>
                <Field label="显示名">
                  <input className="input" value={chEditing.from_name}
                    onChange={(e) => setChEditing({ ...chEditing, from_name: e.target.value })} />
                </Field>
              </div>
              <Field label="默认收件人" hint="逗号分隔，可多个">
                <input className="input mono" placeholder="ops@a.com, sec@b.com" value={chEditing.to}
                  onChange={(e) => setChEditing({ ...chEditing, to: e.target.value })} />
              </Field>
            </>
          ) : (
            <>
              <Field label="URL" hint="触发时 POST JSON">
                <input className="input mono" placeholder="https://example.com/hook" value={chEditing.url}
                  onChange={(e) => setChEditing({ ...chEditing, url: e.target.value })} />
              </Field>
              <Field label="Bearer Token" hint={chEditing.id ? '留空=不修改；接 ZCMail 这类接口用' : '可留空'}>
                <input className="input mono" placeholder={chEditing.id ? '••••••' : ''} value={chEditing.bearer}
                  onChange={(e) => setChEditing({ ...chEditing, bearer: e.target.value })} />
              </Field>
              <Field label="超时（秒）">
                <input className="input" type="number" style={{ width: 100 }} value={chEditing.timeout}
                  onChange={(e) => setChEditing({ ...chEditing, timeout: e.target.value })} />
              </Field>
            </>
          )}
        </Modal>
      )}

      {removing && (
        <ConfirmModal title="删除告警规则" text={<>确定删除规则 <b>{removing.name}</b>？其历史事件保留。</>}
          busy={busy === 'remove'} onConfirm={remove} onClose={() => setRemoving(null)} />
      )}
      {chRemoving && (
        <ConfirmModal title="删除通知通道" text={<>确定删除通道 <b>{chRemoving.name}</b>？规则上的绑定会自动摘除。</>}
          busy={busy === 'ch-remove'} onConfirm={removeChannel} onClose={() => setChRemoving(null)} />
      )}
    </>
  )
}
