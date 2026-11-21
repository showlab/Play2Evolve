/* Independent browser playtest. It never loads benchmark tests or reports. */
'use strict';
const fs = require('fs');
const http = require('http');
const {spawn} = require('child_process');
const WebSocket = require('ws');

const [html, output, actionFile] = process.argv.slice(2);
if (!html || !output) throw new Error('Usage: node browser_probe.js HTML OUTPUT [ACTIONS_JSON]');
fs.mkdirSync(output, {recursive: true});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const chrome = spawn('/usr/bin/chromium', [
  '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--use-gl=swiftshader',
  '--remote-allow-origins=*', '--remote-debugging-port=9222',
  '--user-data-dir=/tmp/gameasg-probe-profile', '--window-size=1280,720',
  '--allow-file-access-from-files', `file://${html}`,
], {stdio: ['ignore', 'ignore', 'pipe']});
let chromeError = '';
chrome.stderr.on('data', chunk => { chromeError = (chromeError + chunk.toString()).slice(-4000); });

function targets() {
  return new Promise((resolve, reject) => {
    http.get('http://127.0.0.1:9222/json/list', response => {
      let body = '';
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => { try { resolve(JSON.parse(body)); } catch (error) { reject(error); } });
    }).on('error', reject);
  });
}

async function main() {
  let target;
  for (let attempt = 0; attempt < 80; attempt++) {
    try { target = (await targets()).find(row => row.type === 'page'); } catch (_) {}
    if (target) break;
    if (chrome.exitCode !== null) throw new Error(`Chromium exited: ${chromeError}`);
    await sleep(100);
  }
  if (!target) throw new Error(`Chromium debugging endpoint unavailable: ${chromeError}`);
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); });
  let sequence = 0;
  const pending = new Map();
  const errors = [];
  ws.on('message', raw => {
    const event = JSON.parse(raw.toString());
    if (event.id && pending.has(event.id)) {
      const {resolve, reject, timer} = pending.get(event.id);
      clearTimeout(timer); pending.delete(event.id);
      event.error ? reject(new Error(JSON.stringify(event.error))) : resolve(event.result);
    } else if (event.method === 'Runtime.exceptionThrown') {
      errors.push(event.params.exceptionDetails.text);
    } else if (event.method === 'Runtime.consoleAPICalled' && event.params.type === 'error') {
      errors.push(event.params.args.map(arg => arg.value || arg.description || '').join(' '));
    }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 20000);
    pending.set(id, {resolve, reject, timer});
    ws.send(JSON.stringify({id, method, params}));
  });
  const evaluate = async expression => {
    const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  };
  await send('Runtime.enable');
  await send('Page.enable');
  await sleep(900);
  const frames = [];
  async function capture(label) {
    const stateText = await evaluate('(() => { try { return JSON.stringify(window.__gameTest?.getSnapshot?.() ?? null); } catch (e) { return JSON.stringify({probeError:String(e)}); } })()');
    const screenshot = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    const image = `frame-${String(frames.length).padStart(2, '0')}.png`;
    fs.writeFileSync(`${output}/${image}`, Buffer.from(screenshot.data, 'base64'));
    let snapshot;
    try { snapshot = JSON.parse(stateText); } catch (_) { snapshot = {probeError: String(stateText)}; }
    frames.push({label, image, snapshot});
  }
  await capture('initial');
  const firstButton = await evaluate(`(() => { const visible=x=>{const r=x.getBoundingClientRect();return r.width>0&&r.height>0}; const buttons=[...document.querySelectorAll('button')].filter(visible); const b=buttons.find(x=>/start|play|launch|begin|enter|开始|游玩/i.test(x.id+' '+x.textContent))||buttons[0]; if (!b) return null; const r=b.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2,text:b.textContent.trim().slice(0,80)}; })()`);
  if (firstButton) {
    await send('Input.dispatchMouseEvent', {type: 'mousePressed', x: firstButton.x, y: firstButton.y, button: 'left', clickCount: 1});
    await send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: firstButton.x, y: firstButton.y, button: 'left', clickCount: 1});
    await sleep(800);
    await capture('first-button-click');
  }
  const actions = actionFile ? JSON.parse(fs.readFileSync(actionFile, 'utf8')).actions : [];
  if (!Array.isArray(actions) || actions.length > 8) throw new Error('Expected at most eight playtest actions');
  for (const [index, action] of actions.entries()) {
    const value = String(action.value || '');
    if (action.type === 'wait') {
      await sleep(Math.max(0, Math.min(3000, Number(value) || 500)));
    } else if (action.type === 'key') {
      await send('Input.dispatchKeyEvent', {type: 'keyDown', key: value, code: value});
      await sleep(120);
      await send('Input.dispatchKeyEvent', {type: 'keyUp', key: value, code: value});
    } else if (action.type === 'click' || action.type === 'move') {
      const [x, y] = value.split(',').map(Number);
      if (![x, y].every(Number.isFinite)) throw new Error(`Invalid pointer action ${value}`);
      const point = {x: Math.max(0, Math.min(1279, x * 1280)), y: Math.max(0, Math.min(719, y * 720))};
      await send('Input.dispatchMouseEvent', {type: 'mouseMoved', ...point});
      if (action.type === 'click') {
        await send('Input.dispatchMouseEvent', {type: 'mousePressed', ...point, button: 'left', clickCount: 1});
        await send('Input.dispatchMouseEvent', {type: 'mouseReleased', ...point, button: 'left', clickCount: 1});
      }
    } else if (action.type === 'drag') {
      const coords = value.split(',').map(Number);
      if (coords.length !== 4 || !coords.every(Number.isFinite)) throw new Error(`Invalid drag ${value}`);
      const [x1, y1, x2, y2] = coords.map((n, i) => Math.max(0, Math.min(i % 2 ? 719 : 1279, n * (i % 2 ? 720 : 1280))));
      await send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: x1, y: y1});
      await send('Input.dispatchMouseEvent', {type: 'mousePressed', x: x1, y: y1, button: 'left', clickCount: 1});
      await send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: x2, y: y2, button: 'left', buttons: 1});
      await send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: x2, y: y2, button: 'left', clickCount: 1});
    } else {
      throw new Error(`Unknown playtest action ${action.type}`);
    }
    await sleep(450);
    await capture(`action-${index + 1}-${action.type}`);
  }
  fs.writeFileSync(`${output}/trace.json`, JSON.stringify({firstButton, actions, frames, errors}, null, 2) + '\n');
  ws.close();
}

main().then(() => { chrome.kill('SIGTERM'); }).catch(error => {
  fs.writeFileSync(`${output}/probe-error.txt`, String(error.stack || error) + '\n' + chromeError);
  chrome.kill('SIGTERM');
  process.exitCode = 1;
});
