import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9333;
const OUT = "C:/Users/Administrator/AppData/Local/Temp/shots";
const PAGE = process.env.PAGE || "http://127.0.0.1:8000/";
const W = Number(process.env.W || 1600), H = Number(process.env.H || 900);
mkdirSync(OUT, { recursive: true });

const child = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${OUT}/profile`,
  `--window-size=${W},${H}`, "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
  "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--force-device-scale-factor=1",
  "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
  "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pageTarget() {
  for (let i = 0; i < 80; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const p = list.find((t) => t.type === "page");
      if (p?.webSocketDebuggerUrl) return p;
    } catch {}
    await sleep(300);
  }
  throw new Error("devtools 未就绪");
}

const target = await pageTarget();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res) => ws.addEventListener("open", res, { once: true }));

let seq = 0;
const pending = new Map();
const logs = [];
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled") logs.push("[" + m.params.type + "] " + m.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
  if (m.method === "Runtime.exceptionThrown") logs.push("[exception] " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
  if (m.method === "Log.entryAdded") logs.push("[log:" + m.params.entry.level + "] " + m.params.entry.text);
});
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq;
  pending.set(id, (m) => (m.error ? rej(new Error(method + ": " + JSON.stringify(m.error))) : res(m.result)));
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression, awaitPromise = false) =>
  (await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise })).result?.value;
const shoot = async (name) => {
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: Math.round(W / 2), y: Math.round(H / 3) });
  await sleep(150);
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(`${OUT}/${name}.png`, Buffer.from(data, "base64"));
  console.log("shot ->", name);
};

await send("Page.enable");
await send("Runtime.enable");
await send("Log.enable");
await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await send("Emulation.setFocusEmulationEnabled", { enabled: true });
await send("Page.bringToFront");
await send("Page.navigate", { url: PAGE });

await sleep(4500);
await shoot("00-login");

// 自动登录
const info = await evaluate(`(() => {
  const root = document.getElementById('root');
  const h1 = [...document.querySelectorAll('h1,h2,h3,.page-title,input')].slice(0,12).map(e => e.tagName+':'+(e.className||'')+':'+(e.placeholder||e.textContent||'').trim().slice(0,40));
  return { childCount: root?.childElementCount ?? -1, title: document.title, sample: h1, hasPwd: !!document.querySelector('input[type=password]') };
})()`);
console.log("PAGE INFO:", JSON.stringify(info, null, 2));

if (info.hasPwd) {
  // 直接调 API 登录并写 token，比模拟 React 受控表单可靠
  const ok = await evaluate(`(async () => {
    try {
      const res = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: 'admin', password: 'admin123' }) });
      const data = await res.json();
      if (data.token) { localStorage.setItem('zp_token', data.token); return data.user ? data.user.username : 'ok'; }
      return 'no-token: ' + JSON.stringify(data).slice(0,120);
    } catch (e) { return 'error: ' + e.message; }
  })()`, true);
  console.log("api login:", ok);
  await send("Page.navigate", { url: "http://127.0.0.1:8000/" });
  await sleep(4500);
  await shoot("01-dashboard");

  for (const { path, name } of [{ path: '/nodes/localhost', name: '02-node-detail' }, { path: '/runtime', name: '03-runtime' }, { path: '/files', name: '04-files' }]) {
    await send("Page.navigate", { url: `http://127.0.0.1:8000${path}` });
    await sleep(3500);
    await shoot(name);
  }
}

console.log("errors:", logs.slice(-20).join("\n") || "(无)");
ws.close();
child.kill();
