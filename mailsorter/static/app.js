'use strict';

const SWIPE_DISTANCE = 90;
const SWIPE_UP_DISTANCE = 80;
const STAMP_FULL_AT = 120;
const LEAVE_MS = 220;
const TOAST_MS = 5000;
const KIB = 1024;
const MIB = KIB * KIB;
const ACTION_DONE_TEXT = { yes: 'Kept', no: 'Moved to the Bin', later: 'Saved for later' };
const DIRECTION_ACTION = { right: 'yes', left: 'no', up: 'later' };

const state = {
  csrf: '', view: 'inbox', message: null, counts: null, busy: false, showImages: false, retry: null,
  bodyShown: false, lastChoice: null, undoWaiting: false,
};
const byId = (id) => document.getElementById(id);
let toastTimer = null;

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

async function api(method, path, payload) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  const options = { method, headers, credentials: 'same-origin' };
  if (payload !== undefined) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(payload);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new ApiError('Could not reach Mail Sorter. Check the connection and try again.', 0, { retry: true });
  }
  const data = await response.json().catch(() => null);
  if (response.status === 401 && data && data.signIn) {
    window.location.assign('/?why=expired');
    throw new ApiError('signed out', 401, data);
  }
  if (!response.ok) {
    throw new ApiError(data && data.error ? data.error : `Mail Sorter answered ${response.status}`, response.status, data);
  }
  return data;
}

function showToast(text, { undo = false, error = false } = {}) {
  byId('toast-text').textContent = text;
  byId('toast-undo').hidden = !undo;
  byId('toast').classList.toggle('error', error);
  byId('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { byId('toast').hidden = true; }, TOAST_MS);
}

function setChoicesEnabled(enabled) {
  for (const id of ['choose-yes', 'choose-no', 'choose-later']) byId(id).disabled = !enabled;
}

function canChoose() {
  return Boolean(state.message) && state.bodyShown && !state.busy && !byId('card').hidden;
}

function refreshChoices() {
  setChoicesEnabled(canChoose());
}

function showOnly(sectionId) {
  for (const id of ['card', 'loading', 'empty', 'problem']) byId(id).hidden = id !== sectionId;
  refreshChoices();
}

function countText(counts) {
  if (!counts) return '';
  const more = counts.more ? '+' : '';
  const later = counts.later ? `, ${counts.later} later` : '';
  return `${counts.left}${more} left${later}`;
}

function renderCounts() {
  byId('counter').textContent = countText(state.counts);
}

function formatDate(milliseconds) {
  if (!milliseconds) return '';
  const when = new Date(milliseconds);
  const now = new Date();
  const sameDay = when.toDateString() === now.toDateString();
  const options = sameDay ? { hour: '2-digit', minute: '2-digit' }
    : { day: 'numeric', month: 'short', year: when.getFullYear() === now.getFullYear() ? undefined : 'numeric' };
  return new Intl.DateTimeFormat('en-GB', options).format(when);
}

function formatSize(bytes) {
  if (typeof bytes !== 'number') return '';
  if (bytes >= MIB) return `${(bytes / MIB).toFixed(1)} MB`;
  if (bytes >= KIB) return `${Math.round(bytes / KIB)} KB`;
  return `${bytes} B`;
}

function renderAttachments(list) {
  const holder = byId('attachments');
  holder.replaceChildren();
  for (const item of list) {
    const chip = document.createElement('li');
    chip.className = 'attachment';
    const name = document.createElement('span');
    name.className = 'attachment-name';
    name.textContent = item.name;
    chip.append(name);
    if (item.size !== null) {
      const size = document.createElement('span');
      size.className = 'attachment-size';
      size.textContent = formatSize(item.size);
      chip.append(size);
    }
    holder.append(chip);
  }
  holder.hidden = list.length === 0;
}

function frameAddress(message) {
  const images = state.showImages ? '?images=1' : '';
  return `/api/messages/${encodeURIComponent(message.id)}/body${images}`;
}

function renderImagesBar(message) {
  const blocked = message.remoteImages > 0 && !state.showImages;
  byId('images-bar').hidden = !blocked;
  if (blocked) {
    const noun = message.remoteImages === 1 ? 'image' : 'images';
    byId('images-text').textContent = `${message.remoteImages} remote ${noun} blocked, so the sender cannot tell you opened it.`;
  }
}

function resetCardPosition() {
  const card = byId('card');
  card.classList.remove('dragging', 'leaving-yes', 'leaving-no', 'leaving-later');
  card.style.transform = '';
  setStamps(0, 0);
}

function showFrameFor(message) {
  const frame = byId('email-frame');
  state.bodyShown = false;
  frame.classList.add('frame-waiting');
  frame.dataset.messageId = message.id;
  frame.src = frameAddress(message);
}

function frameLoaded() {
  const frame = byId('email-frame');
  if (!state.message || frame.dataset.messageId !== state.message.id) return;
  frame.classList.remove('frame-waiting');
  state.bodyShown = true;
  refreshChoices();
}

function renderMessage(message) {
  state.message = message;
  state.showImages = false;
  resetCardPosition();
  byId('from-name').textContent = message.from.name || message.from.address || 'Unknown sender';
  byId('from-address').textContent = message.from.address && message.from.address !== message.from.name ? message.from.address : '';
  byId('date').textContent = formatDate(message.date);
  if (message.date) byId('date').dateTime = new Date(message.date).toISOString();
  byId('subject').textContent = message.subject;
  byId('later-badge').hidden = !message.isLater;
  renderAttachments(message.attachments || []);
  renderImagesBar(message);
  showFrameFor(message);
  showOnly('card');
}

function renderEmpty() {
  state.message = null;
  const laterWaiting = state.view === 'inbox' && state.counts && state.counts.later > 0;
  byId('empty-title').textContent = state.view === 'later' ? 'Nothing saved for later' : 'All sorted';
  byId('empty-text').textContent = state.view === 'later' ? 'No emails are waiting in Later.' : 'Nothing left in your inbox to sort.';
  byId('empty-later').hidden = !laterWaiting;
  state.bodyShown = false;
  byId('email-frame').dataset.messageId = '';
  byId('email-frame').src = 'about:blank';
  showOnly('empty');
}

function showProblem(error, retry) {
  state.retry = retry;
  byId('problem-text').textContent = error.message;
  showOnly('problem');
}

async function loadNext() {
  const data = await api('GET', `/api/next?view=${state.view}`);
  state.counts = data.counts;
  renderCounts();
  if (data.message) renderMessage(data.message);
  else renderEmpty();
}

async function refresh() {
  showOnly('loading');
  try {
    await loadNext();
  } catch (error) {
    if (error.status !== 401) showProblem(error, refresh);
  }
}

function leaveAnimation(action) {
  const card = byId('card');
  card.classList.remove('dragging');
  card.style.transform = '';
  card.classList.add(`leaving-${action}`);
  return new Promise((resolve) => setTimeout(resolve, LEAVE_MS));
}

async function decide(action) {
  const message = state.message;
  if (!canChoose()) return;
  state.busy = true;
  setChoicesEnabled(false);
  const leaving = leaveAnimation(action);
  try {
    await api('POST', `/api/messages/${encodeURIComponent(message.id)}/${action}`);
    state.lastChoice = { id: message.id, action };
    await leaving;
    showToast(ACTION_DONE_TEXT[action], { undo: true });
    showOnly('loading');
    await loadNext();
  } catch (error) {
    await leaving;
    if (error.status === 401) return;
    if (error.data.gone) {
      showToast(error.message, { error: true });
      await refresh();
    } else {
      resetCardPosition();
      showProblem(error, () => { showOnly('card'); return decide(action); });
    }
  } finally {
    finishStep();
  }
}

function finishStep() {
  state.busy = false;
  refreshChoices();
  if (state.undoWaiting) {
    state.undoWaiting = false;
    undo();
  }
}

function waitToUndo() {
  state.undoWaiting = true;
  clearTimeout(toastTimer);
  byId('toast-text').textContent = 'Undo waits for the next email to load...';
  byId('toast-undo').hidden = true;
  byId('toast').hidden = false;
}

async function undo() {
  if (state.busy) {
    waitToUndo();
    return;
  }
  const choice = state.lastChoice;
  if (!choice) {
    showToast('There is nothing to undo.', { error: true });
    return;
  }
  state.busy = true;
  setChoicesEnabled(false);
  byId('toast').hidden = true;
  try {
    const result = await api('POST', '/api/undo', choice);
    state.lastChoice = null;
    showToast(`Undone: ${ACTION_DONE_TEXT[result.undone].toLowerCase()}`);
    showOnly('loading');
    await loadNext();
  } catch (error) {
    if (error.status === 401) return;
    if (error.status === 409) state.lastChoice = null;
    showToast(error.message, { error: true });
  } finally {
    finishStep();
  }
}

function showImages() {
  if (!state.message || state.showImages) return;
  state.showImages = true;
  renderImagesBar(state.message);
  byId('email-frame').src = frameAddress(state.message);
}

function setView(view) {
  if (state.busy || view === state.view) return;
  state.view = view;
  for (const button of document.querySelectorAll('[data-view]')) {
    button.setAttribute('aria-pressed', String(button.dataset.view === view));
  }
  refresh();
}

async function signOut() {
  let result;
  try {
    result = await api('POST', '/auth/signout');
  } catch (error) {
    if (error.status !== 401) showToast(error.message, { error: true });
    return;
  }
  window.location.assign(result.revoked ? '/?why=signedout' : '/?why=notrevoked');
}

function setStamps(dx, dy) {
  const card = byId('card');
  const strength = (distance) => Math.max(0, Math.min(1, distance / STAMP_FULL_AT));
  card.querySelector('.stamp-yes').style.opacity = strength(dx);
  card.querySelector('.stamp-no').style.opacity = strength(-dx);
  card.querySelector('.stamp-later').style.opacity = Math.abs(dx) < SWIPE_DISTANCE ? strength(-dy) : 0;
}

function swipeDirection(dx, dy) {
  if (dx >= SWIPE_DISTANCE && Math.abs(dx) >= Math.abs(dy)) return 'right';
  if (dx <= -SWIPE_DISTANCE && Math.abs(dx) >= Math.abs(dy)) return 'left';
  if (dy <= -SWIPE_UP_DISTANCE && Math.abs(dy) > Math.abs(dx)) return 'up';
  return null;
}

function watchSwipes() {
  const head = byId('card-head');
  const card = byId('card');
  let start = null;
  head.addEventListener('pointerdown', (event) => {
    if (!canChoose() || event.button !== 0) return;
    start = { x: event.clientX, y: event.clientY, id: event.pointerId };
    head.setPointerCapture(event.pointerId);
    card.classList.add('dragging');
  });
  head.addEventListener('pointermove', (event) => {
    if (!start || event.pointerId !== start.id) return;
    const dx = event.clientX - start.x;
    const dy = Math.min(0, event.clientY - start.y);
    card.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 24}deg)`;
    setStamps(dx, dy);
  });
  const finish = (event) => {
    if (!start || event.pointerId !== start.id) return;
    const direction = event.type === 'pointerup' ? swipeDirection(event.clientX - start.x, event.clientY - start.y) : null;
    start = null;
    if (direction) {
      decide(DIRECTION_ACTION[direction]);
    } else {
      resetCardPosition();
    }
  };
  head.addEventListener('pointerup', finish);
  head.addEventListener('pointercancel', finish);
}

function isTyping(target) {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
}

function watchKeys() {
  const keyAction = { ArrowRight: 'yes', y: 'yes', ArrowLeft: 'no', n: 'no', ArrowUp: 'later', l: 'later' };
  document.addEventListener('keydown', (event) => {
    if (isTyping(event.target) || event.altKey || event.metaKey || byId('keys-dialog').open) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    if ((event.ctrlKey && key === 'z') || (!event.ctrlKey && key === 'u')) {
      event.preventDefault();
      undo();
      return;
    }
    if (event.ctrlKey) return;
    if (keyAction[key] && !byId('card').hidden) {
      event.preventDefault();
      decide(keyAction[key]);
    } else if (key === 'i') {
      showImages();
    } else if (key === '?') {
      byId('keys-dialog').showModal();
    }
  });
}

function wireButtons() {
  byId('choose-yes').addEventListener('click', () => decide('yes'));
  byId('choose-no').addEventListener('click', () => decide('no'));
  byId('choose-later').addEventListener('click', () => decide('later'));
  byId('undo').addEventListener('click', undo);
  byId('toast-undo').addEventListener('click', undo);
  byId('email-frame').addEventListener('load', frameLoaded);
  byId('show-images').addEventListener('click', showImages);
  byId('refresh').addEventListener('click', refresh);
  byId('empty-later').addEventListener('click', () => setView('later'));
  byId('retry').addEventListener('click', () => {
    const retry = state.retry || refresh;
    state.retry = null;
    showOnly('loading');
    retry();
  });
  byId('keys-btn').addEventListener('click', () => byId('keys-dialog').showModal());
  byId('sign-out').addEventListener('click', signOut);
  for (const button of document.querySelectorAll('[data-view]')) {
    button.addEventListener('click', () => setView(button.dataset.view));
  }
}

async function start() {
  wireButtons();
  watchSwipes();
  watchKeys();
  try {
    const session = await api('GET', '/api/session');
    state.csrf = session.csrfToken;
    state.lastChoice = session.lastChoice || null;
  } catch (error) {
    if (error.status !== 401) showProblem(error, () => window.location.reload());
    return;
  }
  await refresh();
}

start();
