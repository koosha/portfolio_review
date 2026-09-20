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

// Starts `serverScript` (Python source receiving a data directory as argv[1]) and reads
// the first JSON line it prints, which must include `port`. Pass `directory` to serve an
// existing data directory — a restarted application rather than a fresh one — in which
// case the caller owns that directory and this never removes it. `stop()` ends the
// process without waiting for the test to finish, which is how a restart is staged.
export async function boot(t, serverScript, {directory: given = null} = {}) {
  const directory = given || (await mkdtemp(join(tmpdir(), 'portfolio-review-dom-http-')));
  const child = spawn(python, ['-u', '-c', serverScript, directory], {
    cwd: project, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '', errors = '', spawnError = null;
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { errors = (errors + chunk).slice(-8000); });
  child.on('error', error => { spawnError = error; });
  const stop = async (signal = 'SIGTERM') => {
    if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
    const exited = new Promise(resolve => child.once('exit', resolve));
    child.kill(signal);
    await exited;
  };
  t.after(async () => {
    await stop();
    if (!given) await rm(directory, {recursive: true, force: true});
  });
  await until(() => {
    if (spawnError) throw new Error(`Python test server could not start: ${spawnError.message}`);
    if (child.exitCode !== null) throw new Error(`Python test server failed: ${errors}`);
    return output.includes('\n');
  }, 'synthetic server initialization', 60000);
  const info = JSON.parse(output.split('\n')[0]);
  return {child, directory, info, origin: `http://127.0.0.1:${info.port}`, stop,
    stderr: () => errors};
}

// Loads the page served at `origin` into JSDOM with the same shims as the browser-free
// integration tests, evaluates research.js and app.js, then waits for `ready($, window)`.
// Called twice against one origin, it models two tabs open on the same application.
//
// `intercept(path, options)` sees every request the page makes before it is sent: return
// a Response to answer it without reaching the service, or nothing to let it through.
// That is how a case models a service that fails a request the page depends on, which no
// amount of driving a healthy page can reach. `expectError` accepts the error banner such
// a case is checking for; every other caller still asserts the banner stays hidden.
export async function open(t, origin, {
  ready = savedRunRendered, intercept = null, expectError = false,
} = {}) {
  let dom;
  t.after(() => dom?.window.close());
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
  // jsdom performs no layout, so scrolling is modelled as the offset the page last asked
  // for. That is enough to assert a view change returns to the top; it says nothing about
  // whether anything would actually move, or about smooth-scroll behavior.
  Object.defineProperty(window, 'scrollY', {value: 0, writable: true, configurable: true});
  window.scrollTo = (...args) => {
    const top = typeof args[0] === 'object' && args[0] !== null ? args[0].top : args[1];
    window.scrollY = Number(top) || 0;
  };
  window.fetch = async (path, options = {}) => {
    const answer = intercept ? await intercept(String(path), options) : undefined;
    if (answer !== undefined && answer !== null) return answer;
    return fetch(new URL(path, origin), {...options, headers: {...options.headers, Origin: origin}});
  };
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
  // Every status line the page announces, not just the one a poll happened to sample.
  // announce() writes one line over the last, so a message can be replaced between two
  // 25 ms ticks of `until` and a case waiting for it would wait forever. Recording each
  // mutation makes "the page said this" a fact about the run rather than about timing.
  const announcements = [];
  window.__announcements = announcements;
  const status = $('research-status');
  if (status) {
    const observer = new window.MutationObserver(records => {
      for (const record of records) {
        const added = [...record.addedNodes].map(node => node.textContent).join('');
        const text = record.type === 'characterData' ? record.target.textContent : added;
        if (text) announcements.push(text);
      }
    });
    observer.observe(status, {childList: true, characterData: true, subtree: true});
    t.after(() => observer.disconnect());
  }
  await until(() => ready($, window), 'initial page render');
  if (!expectError) assert.equal($('research-error').hidden, true, $('research-error').textContent);
  return {window, $, origin, runtimeErrors, announcements};
}

// One disposable application with one page open on it: the shape every existing caller
// uses. `options` are shared by both halves, so `directory` and `ready` both apply.
export async function launch(t, serverScript, options = {}) {
  const server = await boot(t, serverScript, options);
  return {...server, ...(await open(t, server.origin, options))};
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
