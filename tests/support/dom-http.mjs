// Shared in-memory DOM harness driving the real local HTTP service from a disposable
// Python process. This does not test browser layout, accessibility APIs, Chrome policy,
// extension behavior or browser security enforcement.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {JSDOM, VirtualConsole} from 'jsdom';
import * as stateFunctions from '../../static/research-state.js';

export const project = fileURLToPath(new URL('../..', import.meta.url));
export const python = process.env.PORTFOLIO_TEST_PYTHON || join(project, '.venv', 'bin', 'python');

export async function until(predicate, description, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error(`Timed out waiting for ${description}.`);
}

const savedRunRendered = $ => $('draft-state').textContent === 'Saved';

// Starts `serverScript` (Python source receiving a fresh temporary directory as argv[1]),
// reads the first JSON line it prints (which must include `port`), loads the served page
// into JSDOM with the same shims as the browser-free integration tests, evaluates
// research.js and app.js, then waits for `ready($, window)` before returning.
export async function launch(t, serverScript, {ready = savedRunRendered} = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'portfolio-review-dom-http-'));
  const child = spawn(python, ['-u', '-c', serverScript, directory], {
    cwd: project, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '', errors = '', spawnError = null;
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { errors = (errors + chunk).slice(-8000); });
  child.on('error', error => { spawnError = error; });
  let dom;
  t.after(async () => {
    dom?.window.close();
    if (child.pid && child.exitCode === null && child.signalCode === null) {
      const exited = new Promise(resolve => child.once('exit', resolve));
      child.kill('SIGTERM');
      await exited;
    }
    await rm(directory, {recursive: true, force: true});
  });
  await until(() => {
    if (spawnError) throw new Error(`Python test server could not start: ${spawnError.message}`);
    if (child.exitCode !== null) throw new Error(`Python test server failed: ${errors}`);
    return output.includes('\n');
  }, 'synthetic server initialization', 60000);
  const info = JSON.parse(output.split('\n')[0]);
  const origin = `http://127.0.0.1:${info.port}`;
  const response = await fetch(origin);
  assert.equal(response.status, 200);
  const runtimeErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => runtimeErrors.push(error.message));
  dom = new JSDOM(await response.text(), {
    url: origin, runScripts: 'outside-only', virtualConsole, pretendToBeVisual: true,
  });
  const {window} = dom;
  window.addEventListener('error', event => runtimeErrors.push(event.message));
  // These DOM method shims implement state transitions only; they do not model
  // focus trapping, layout, user activation or native download behavior.
  window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  window.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.fetch = (path, options = {}) => fetch(new URL(path, origin), {
    ...options,
    headers: {...options.headers, Origin: origin},
  });
  window.__stateFunctions = stateFunctions;
  const researchPath = window.document.querySelector('script[src$="research.js"]').src;
  const scriptResponse = await fetch(researchPath);
  assert.equal(scriptResponse.status, 200);
  const researchSource = (await scriptResponse.text()).replace(
    /import\s*\{([\s\S]*?)\}\s*from\s*['"]\.\/research-state\.js['"];?/,
    'const {$1} = window.__stateFunctions;',
  );
  window.eval(`(() => {\n${researchSource}\n})();`);
  const collectorResponse = await fetch(window.document.querySelector('script[src$="app.js"]').src);
  assert.equal(collectorResponse.status, 200);
  window.eval(`(() => {\n${await collectorResponse.text()}\n})();`);
  const $ = id => window.document.getElementById(id);
  await until(() => ready($, window), 'initial page render');
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
  return {window, $, origin, runtimeErrors, info};
}

export function namedInput(window, labelText) {
  const label = [...window.document.querySelectorAll('label')]
    .find(label => [...label.children].some(child => child.tagName === 'SPAN' && child.textContent === labelText));
  assert.ok(label, `Missing labeled input: ${labelText}`);
  const input = label.querySelector('input, select, textarea');
  assert.ok(input, `Label has no control: ${labelText}`);
  return input;
}

export function setInput(window, control, value) {
  if (control.type === 'checkbox') control.checked = value;
  else control.value = String(value);
  control.dispatchEvent(new window.Event(control.type === 'checkbox' || control.tagName === 'SELECT' ? 'change' : 'input', {bubbles: true}));
}
