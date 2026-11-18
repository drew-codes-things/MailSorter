'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'mailsorter', 'static', 'app.js');
const SHORT_TIMER_MS = 1000;

function makeElement(id) {
  const listeners = {};
  const classes = new Set();
  return {
    id, hidden: false, disabled: false, textContent: '', src: '', dateTime: '', open: false, dataset: {}, style: {},
    listeners,
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    },
    addEventListener: (type, handler) => { listeners[type] = handler; },
    fire(type, event = {}) { return listeners[type] && listeners[type](event); },
    setAttribute() {}, replaceChildren() {}, append() {}, setPointerCapture() {}, showModal() {},
    querySelector: () => makeElement('inner'),
  };
}

function makeWorld() {
  const elements = {};
  const documentListeners = {};
  const requests = [];
  const pending = [];
  const assigned = [];
  const document = {
    getElementById: (id) => (elements[id] = elements[id] || makeElement(id)),
    querySelectorAll: () => [],
    createElement: () => makeElement('made'),
    addEventListener: (type, handler) => { documentListeners[type] = handler; },
  };
  const fetch = (address, options) => new Promise((resolve) => {
    const request = {
      method: options.method, path: address, body: options.body ? JSON.parse(options.body) : null,
      answer: (status, data) => resolve({ status, ok: status < 400, json: async () => data }),
    };
    requests.push(request);
    pending.push(request);
  });
  const sandbox = {
    document, fetch, window: { location: { assign: (where) => assigned.push(where), reload() {} } },
    HTMLElement: function HTMLElement() {}, Intl, Date, Math, Promise, JSON, console,
    setTimeout: (handler, ms) => (ms < SHORT_TIMER_MS ? setImmediate(handler) : 0), clearTimeout() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), sandbox);
  const press = (key) => documentListeners.keydown({ key, target: {}, preventDefault() {} });
  return { requests, pending, assigned, byId: document.getElementById, press };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

async function answerNext(world, status, data) {
  for (let tries = 0; tries < 5 && world.pending.length === 0; tries += 1) await settle();
  const request = world.pending.shift();
  if (!request) return null;
  request.answer(status, data);
  await settle();
  await settle();
  return request;
}

function message(id, subject) {
  return { id, from: { name: 'Shop', address: 'shop@example.com' }, subject, date: 1763000000000, attachments: [],
           remoteImages: 0, isLater: false };
}

const COUNTS = { left: 2, later: 0, more: false };
const SESSION = { account: 'drew@example.com', csrfToken: 'token', version: '1', lastChoice: null };

async function startWith(world, first) {
  await answerNext(world, 200, SESSION);
  await answerNext(world, 200, { message: first, counts: COUNTS });
}

function frameState(world) {
  const frame = world.byId('email-frame');
  return { hidden: frame.classList.contains('frame-waiting'), src: frame.src,
           choicesEnabled: !world.byId('choose-yes').disabled };
}

function posts(world) {
  return world.requests.filter((request) => request.method === 'POST').map((request) => request.path);
}

async function frameGate() {
  const world = makeWorld();
  await startWith(world, message('aaa1', 'First'));
  const beforeLoad = frameState(world);
  world.press('y');
  world.byId('choose-yes').fire('click');
  await settle();
  const postsBeforeLoad = posts(world).length;
  world.byId('email-frame').fire('load');
  const afterLoad = frameState(world);
  world.byId('choose-yes').fire('click');
  await answerNext(world, 200, { repeated: false });
  await answerNext(world, 200, { message: message('bbb2', 'Second'), counts: COUNTS });
  const secondBeforeLoad = { ...frameState(world), subject: world.byId('subject').textContent };
  world.press('n');
  await settle();
  const postsDuringSecondLoad = posts(world).length;
  world.byId('email-frame').fire('load');
  return { beforeLoad, postsBeforeLoad, afterLoad, secondBeforeLoad, postsDuringSecondLoad,
           secondAfterLoad: frameState(world) };
}

async function undoWhileTheNextEmailLoads() {
  const world = makeWorld();
  await startWith(world, message('aaa1', 'First'));
  world.byId('email-frame').fire('load');
  world.byId('choose-no').fire('click');
  await answerNext(world, 200, { repeated: false });
  const nextIsLoading = world.pending.length === 1 && world.pending[0].path.startsWith('/api/next');
  world.byId('toast-undo').fire('click');
  await settle();
  const whileLoading = { undoSent: posts(world).includes('/api/undo'), toastHidden: world.byId('toast').hidden,
                         toastText: world.byId('toast-text').textContent };
  await answerNext(world, 200, { message: message('bbb2', 'Second'), counts: COUNTS });
  const undoRequest = world.requests.find((request) => request.path === '/api/undo');
  const afterStep = { undoSent: Boolean(undoRequest), body: undoRequest ? undoRequest.body : null,
                      toastHidden: world.byId('toast').hidden };
  await answerNext(world, 200, { undone: 'no', id: 'aaa1' });
  await answerNext(world, 200, { message: message('aaa1', 'First'), counts: COUNTS });
  return { nextIsLoading, whileLoading, afterStep, toastAfterUndo: world.byId('toast-text').textContent };
}

async function signOutWith(revoked) {
  const world = makeWorld();
  await startWith(world, message('aaa1', 'First'));
  world.byId('sign-out').fire('click');
  await answerNext(world, 200, { signedOut: true, revoked });
  return world.assigned;
}

async function main() {
  const results = {
    frameGate: await frameGate(),
    undoWhileTheNextEmailLoads: await undoWhileTheNextEmailLoads(),
    signOutRevoked: await signOutWith(true),
    signOutNotRevoked: await signOutWith(false),
  };
  process.stdout.write(JSON.stringify(results));
}

main();
