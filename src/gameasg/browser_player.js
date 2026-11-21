/* Independent reactive Player. Policies cannot execute code inside the game. */
'use strict';
const fs = require('fs');
const vm = require('vm');
const http = require('http');
const {spawn} = require('child_process');
const sleep = ms => new Promise(r => setTimeout(r, ms));
// A reactive policy may evaluate many legal moves before choosing one action.
// Keep each decision bounded while allowing that search to finish.
const POLICY_STEP_TIMEOUT_MS = 1000;

function validateAction(a) {
  if (!a || !['wait', 'key', 'click', 'move', 'drag', 'touch', 'done'].includes(a.type))
    throw new Error('Unknown Player action');
  if (a.type === 'done') return {type: 'done', value: String(a.value || '')};
  const duration = a.duration_ms === undefined ? 250 : a.duration_ms;
  if (!Number.isFinite(duration) || duration < 0 || duration > 10000)
    throw new Error('duration_ms must be in [0,10000]');
  const value = String(a.value || '');
  if (['click', 'move', 'drag', 'touch'].includes(a.type)) {
    const parts = value.split(',').map(Number);
    if (parts.length !== (['drag', 'touch'].includes(a.type) ? 4 : 2) ||
        parts.some(v => !Number.isFinite(v) || v < 0 || v > 1))
      throw new Error('Pointer coordinates must be normalized to [0,1]');
  }
  if (a.type === 'key' && (!value || value.length > 40)) throw new Error('Invalid key');
  return {type: a.type, value, duration_ms: duration};
}

function compilePolicy(source) {
  const context = vm.createContext({memory: {}}, {codeGeneration: {strings: false, wasm: false}});
  new vm.Script(source + '\nif(typeof step!=="function") throw Error("Define step(observation,memory)");')
    .runInContext(context, {timeout: 200});
  return observation => {
    context.observation = JSON.parse(JSON.stringify(observation));
    const result = new vm.Script('JSON.stringify(step(observation,memory))')
      .runInContext(context, {timeout: POLICY_STEP_TIMEOUT_MS});
    return validateAction(JSON.parse(result));
  };
}

async function main(html, output, configPath) {
  const WebSocket = require('ws');
  const cfg = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  if (!(cfg.max_seconds >= 1 && cfg.max_seconds <= 600 && cfg.max_actions >= 1 && cfg.max_actions <= 2000))
    throw new Error('Invalid playtest bounds');
  fs.mkdirSync(output, {recursive: true});
  const chrome = spawn('/usr/bin/chromium', ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
    '--use-gl=swiftshader', '--remote-allow-origins=*', '--remote-debugging-port=9222',
    '--user-data-dir=/tmp/gameasg-player', '--window-size=1280,720', 'about:blank'],
    {stdio: ['ignore', 'ignore', 'pipe']});
  let stderr = '';
  chrome.stderr.on('data', x => { stderr = (stderr + x.toString()).slice(-3000); });
  let ws;
  const frames = [], actions = [], errors = [];
  const started = Date.now();
  let stopReason = 'budget';
  try {
    const step = compilePolicy(cfg.policy);
    let target;
    for (let i = 0; i < 100 && !target; i++) {
      try {
        const rows = await new Promise((resolve, reject) => {
          http.get('http://127.0.0.1:9222/json/list', r => {
            let body = ''; r.on('data', d => body += d);
            r.on('end', () => { try { resolve(JSON.parse(body)); } catch (e) { reject(e); } });
          }).on('error', reject);
        });
        target = rows.find(x => x.type === 'page');
      } catch (_) {}
      if (!target) await sleep(100);
    }
    if (!target) throw new Error('Chromium unavailable: ' + stderr);
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); });
    let sequence = 0;
    const pending = new Map();
    ws.on('message', raw => {
      const e = JSON.parse(raw.toString());
      if (e.id && pending.has(e.id)) {
        const p = pending.get(e.id); clearTimeout(p.timer); pending.delete(e.id);
        e.error ? p.reject(Error(JSON.stringify(e.error))) : p.resolve(e.result);
      } else if (e.method === 'Runtime.exceptionThrown') errors.push(e.params.exceptionDetails);
      else if (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error')
        errors.push(e.params.args.map(x => x.value || x.description).join(' '));
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(Error('CDP timeout: ' + method)); }, 15000);
      pending.set(id, {resolve, reject, timer}); ws.send(JSON.stringify({id, method, params}));
    });
    const evaluate = async expression => {
      const result = await send('Runtime.evaluate', {expression, returnByValue: true});
      if (result.exceptionDetails) throw Error(result.exceptionDetails.text);
      return result.result.value;
    };
    await send('Runtime.enable'); await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride', {width: 1280, height: 720, deviceScaleFactor: 1, mobile: false});
    await send('Page.navigate', {url: 'file://' + html});
    await sleep(800);
    const observation = async () => evaluate(`(() => {
      let snapshot=null; try {snapshot=window.__gameTest?.getSnapshot?.()??null;}catch(e){snapshot={error:String(e)};}
      const controls=[...document.querySelectorAll('button,input,select,[role="button"]')].map(el=>{
        const r=el.getBoundingClientRect();const s=getComputedStyle(el);
        if(!r.width||!r.height||s.visibility==='hidden'||s.display==='none')return null;
        return {text:(el.innerText||el.getAttribute('aria-label')||el.id||'').slice(0,120),
          x:(r.x+r.width/2)/innerWidth,y:(r.y+r.height/2)/innerHeight,disabled:!!el.disabled};
      }).filter(x=>x&&x.x>=0&&x.x<=1&&x.y>=0&&x.y<=1);
      return {snapshot,controls,width:innerWidth,height:innerHeight,assets:window.GameASGAssets?.evidence?.()??{}};
    })()`);
    async function capture(label) {
      const obs = await observation();
      const shot = await send('Page.captureScreenshot', {format: 'jpeg', quality: 80, captureBeyondViewport: false});
      const image = `frame-${String(frames.length).padStart(5, '0')}.jpg`;
      fs.writeFileSync(`${output}/${image}`, Buffer.from(shot.data, 'base64'));
      frames.push({label, image, time_ms: Date.now() - started, ...obs});
      return obs;
    }
    await capture('normal-entry');
    if (cfg.launch !== null) {
      await evaluate('window.__gameTest.loadScenario(' + JSON.stringify(cfg.launch) + ')');
      await sleep(100); await capture('public-scenario-precondition');
    }
    const deadline = Date.now() + cfg.max_seconds * 1000;
    async function pause(ms) {
      const end = Math.min(Date.now() + ms, deadline);
      while (Date.now() < end) {
        await sleep(Math.min(250, end - Date.now()));
        await capture('in-action');
      }
    }
    for (let index = 0; index < cfg.max_actions && Date.now() < deadline; index++) {
      const obs = await observation();
      const a = step({...obs, elapsed_ms: Date.now() - started, decision: index});
      actions.push({index, time_ms: Date.now() - started, action: a, observation: obs});
      if (a.type === 'done') { stopReason = a.value || 'policy_done'; break; }
      const coords = a.value.split(',').map(Number);
      const point = (x, y) => ({x: Math.min(1279, x * 1280), y: Math.min(719, y * 720)});
      if (a.type === 'key') {
        const code = a.value === ' ' ? 'Space' : /^[0-9]$/.test(a.value) ? 'Digit' + a.value :
          /^[a-z]$/i.test(a.value) ? 'Key' + a.value.toUpperCase() : a.value;
        await send('Input.dispatchKeyEvent', {type: 'keyDown', key: a.value, code});
        await pause(a.duration_ms);
        await send('Input.dispatchKeyEvent', {type: 'keyUp', key: a.value, code});
      } else if (a.type === 'touch') {
        await send('Emulation.setTouchEmulationEnabled', {enabled: true});
        await send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{...point(coords[0], coords[1]), id: 0}]});
        await send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{...point(coords[2], coords[3]), id: 0}]});
        await pause(a.duration_ms);
        await send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
      } else if (['click', 'move', 'drag'].includes(a.type)) {
        await send('Input.dispatchMouseEvent', {type: 'mouseMoved', ...point(coords[0], coords[1])});
        if (a.type !== 'move') await send('Input.dispatchMouseEvent', {type: 'mousePressed', ...point(coords[0], coords[1]), button: 'left', clickCount: 1});
        if (a.type === 'drag') await send('Input.dispatchMouseEvent', {type: 'mouseMoved', ...point(coords[2], coords[3]), button: 'left', buttons: 1});
        await pause(a.duration_ms);
        if (a.type !== 'move') await send('Input.dispatchMouseEvent', {type: 'mouseReleased', ...point(coords[a.type === 'drag' ? 2 : 0], coords[a.type === 'drag' ? 3 : 1]), button: 'left', clickCount: 1});
      } else await pause(a.duration_ms);
      await capture('action-' + index);
      if (errors.length) { stopReason = 'runtime_error'; break; }
    }
    await capture('final');
  } catch (error) {
    errors.push(String(error.stack || error)); stopReason = 'probe_error';
  } finally {
    fs.writeFileSync(`${output}/trace.json`, JSON.stringify({frames, actions, errors, stop_reason: stopReason,
      launch: cfg.launch, elapsed_ms: Date.now() - started}, null, 2));
    if (ws) ws.close(); chrome.kill('SIGTERM');
  }
  if (errors.length) process.exitCode = 1;
}

module.exports = {compilePolicy, validateAction};
if (require.main === module) {
  main(...process.argv.slice(2)).catch(e => { console.error(e); process.exitCode = 1; });
}
