// Manage Pantry (/ui/add) page logic. Lives in a static file, not inline in
// add.html, so the kiosk browser caches it (immutable, ?v= busted) and reuses
// its compiled bytecode across reloads instead of re-parsing ~28KB of inline
// script on every visit; the HTML the server renders and gzips per request
// shrinks by the same amount. Loaded as a classic script at the end of <body>,
// exactly where the inline version sat, so execution order and the globals the
// page's onclick= handlers use are unchanged.
//
// Server-rendered flags ride in on the #manage-pantry-config div: this file
// must stay template-free.
var _mpConfig = (document.getElementById('manage-pantry-config') || {}).dataset || {};
// Cache-buster for the lazily fetched camera decoder below.
var APP_VERSION = _mpConfig.version || '';
// Food Hub "Scan Next Item" (brief 3.2): when on, an Inventory-mode scan that
// matches a known barcode commits straight to Grocy and the camera keeps
// rolling for the next item, instead of stopping after every single scan.
var QUICK_ADD_MODE = _mpConfig.quickAddMode === '1';
// FoodHub-barcode-bridge: when set, the Stock Up tile sends a non-kiosk
// browser here instead of switching panes in-page (selectMode() below).
var BARCODE_BRIDGE_URL = _mpConfig.barcodeBridgeUrl || '';

// The camera decoder is 367 KB, and only two actions ever touch it: decoding
// a photo of a barcode, and running the live camera scanner. It used to be
// fetched and parsed on EVERY visit to this page, which on an appliance panel
// is pure waste: the kiosk scans with a wired scanner, and live camera
// scanning needs HTTPS so it could never run there anyway. Measured on a Pi 4,
// this page took ~1.4s to load and over a second to first paint, far slower
// than any other. Fetch the library the first time something actually needs
// it instead.
  var _qrLibPromise = null;
  function ensureQrLib() {
    if (window.Html5Qrcode) return Promise.resolve();
    if (!_qrLibPromise) {
      _qrLibPromise = new Promise(function (resolve, reject) {
        var s = document.createElement('script');
        s.src = 'static/vendor/html5-qrcode.min.js?v=' + APP_VERSION;
        s.onload = resolve;
        // Let a failed load be retried rather than poisoning every later try.
        s.onerror = function () { _qrLibPromise = null; reject(new Error('scanner library unavailable')); };
        document.head.appendChild(s);
      });
    }
    return _qrLibPromise;
  }

let scanner = null;
let pendingFile = null;
let queue = [];   // array of item objects
// Whether label printing is turned on (FoodAssistant-fb8x). Gates the
// "Print labels for this batch" affordance shown after an import.
const PRINTING_ENABLED = _mpConfig.printing === '1';

// ------------------------------------------------------------------ modes --
// The four mode tabs ARE the shared scanner mode (pending/scanner-mode): the
// same mode routes a USB scanner's scans on every device and shows on the
// Stream Deck's mode key. Picking a tab POSTs the mode; the page also polls
// so a mode changed from the deck (or another screen) moves the tab here.
const MODES = ['inventory', 'consume', 'shopping', 'audit'];
const MODE_LABELS = {
  inventory: 'Stock up', consume: 'Use stock',
  shopping: 'Shopping list', audit: 'Audit stock',
};
let currentMode = 'inventory';

function applyMode(mode) {
  if (!MODES.includes(mode)) mode = 'inventory';
  const changed = mode !== currentMode;
  currentMode = mode;
  MODES.forEach(m => {
    const tab = document.getElementById('mode-tab-' + m);
    const pane = document.getElementById('mode-pane-' + m);
    if (tab) {
      tab.classList.toggle('active', m === mode);
      tab.setAttribute('aria-checked', m === mode ? 'true' : 'false');
    }
    if (pane) pane.classList.toggle('d-none', m !== mode);
  });
  // Bounce the tile that just became active so a mode change (from a NeoKey,
  // the deck, or a tap) is unmistakable on the panel; restart the animation if
  // one is mid-flight so rapid presses each register.
  if (changed) {
    const active = document.getElementById('mode-tab-' + mode);
    if (active) {
      active.classList.remove('mode-bounce');
      void active.offsetWidth;
      active.classList.add('mode-bounce');
    }
  }
  const liveMode = document.getElementById('scanner-live-mode');
  if (liveMode) liveMode.textContent = MODE_LABELS[mode] || mode;
  if (mode === 'audit') refreshAuditSummary();
}

// A NeoKey press while already on this page fires a navigate event that does
// not reload us (same page); ha-events.js turns that into a 'pr-nav-signal' so
// we resync the mode and bounce the tile right away instead of waiting for the
// slower shared-loop reconcile.
window.addEventListener('pr-nav-signal', function () { refreshMode(); });

async function selectMode(mode) {
  // FoodHub-barcode-bridge: Stock Up on a real phone hands off to the
  // external scanning page instead of switching panes here -- that page has
  // a live camera scanner that works over plain http, unlike this page's own
  // (getUserMedia needs https). The kiosk is exempt: it has no camera of its
  // own, isKiosk means Stock Up here is driven by a wired USB scanner typing
  // into this page, and there's nothing for the bridge page to do with that.
  // The server-side mode still has to be set FIRST and awaited -- the bridge
  // page's scans hit /pending/scan, which acts on whatever mode is already
  // active, so leaving before this lands would scan into the wrong mode.
  if (mode === 'inventory' && !isKiosk && BARCODE_BRIDGE_URL) {
    try {
      await fetch('pending/scanner-mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ mode }),
      });
    } catch (e) { /* best effort; worst case the bridge acts on the old mode */ }
    window.location.href = BARCODE_BRIDGE_URL;
    return;
  }
  applyMode(mode);   // switch instantly; the POST confirms
  try {
    const r = await fetch('pending/scanner-mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ mode }),
    });
    if (r.ok) {
      const state = await r.json();
      if (state.mode && state.mode !== currentMode) applyMode(state.mode);
    }
  } catch (e) { /* offline: the poll will reconcile */ }
}

async function refreshMode() {
  try {
    const r = await fetch('pending/scanner-mode');
    if (!r.ok) return;
    const state = await r.json();
    if (state.mode && state.mode !== currentMode) applyMode(state.mode);
  } catch (e) { /* ignore */ }
}

// Prefer the consolidated kiosk poll so a mode changed from the deck or another
// screen moves the tab here; fall back to the dedicated 5s scanner-mode poll
// without the shared loop. Picking a tab still POSTs pending/scanner-mode above.
if (window.PRKioskStatus) {
  window.PRKioskStatus.subscribe((s) => {
    if (s && s.scanner_mode && s.scanner_mode.mode && s.scanner_mode.mode !== currentMode) {
      applyMode(s.scanner_mode.mode);
    }
  }, { interval: 5000, wants: ['scanner_mode'] });
} else {
  refreshMode();
  // The focus/visibility hooks below already resync a returning tab, so the
  // steady poll can skip hidden tabs entirely.
  setInterval(() => { if (!document.hidden) refreshMode(); }, 5000);
  window.addEventListener('focus', refreshMode);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshMode();
  });
}

// --------------------------------------------------------- scan session ----
// While this page is open it holds the scan session alive with a heartbeat, so
// a serial (UART) scanner reads continuously (the host reader watches
// scan_active in /gadgets/config). The ping fires on mount and every ~10s;
// closing the page stops the pings and the session expires on its own, which
// is what turns the scanner off. A confirmed-active heartbeat reveals the
// "Scanner live" banner so the user can see scanning is running.
async function pingScanSession() {
  if (document.hidden) return;
  try {
    const r = await fetch('pending/scan-session/ping', { method: 'POST' });
    if (!r.ok) return;
    const s = await r.json();
    const banner = document.getElementById('scanner-live-banner');
    if (banner) banner.classList.toggle('d-none', !s.active);
  } catch (e) { /* offline: the next ping retries; the session lapses meanwhile */ }
}
pingScanSession();
setInterval(pingScanSession, 10000);
window.addEventListener('focus', pingScanSession);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) pingScanSession();
});

// ---------------------------------------------------------- live scans -----
// Fast-ack scanning (FoodAssistant-x61t): an inventory scan is stored instantly
// as a placeholder and the name is looked up in the background. Each scan
// appends a row here reading "Saved, looking up...", and a short poll of the
// Pending list updates the row in place to the product name once enrichment
// lands. No reload, so a continuous scan just keeps stacking rows.
const liveScans = new Map();   // pending id -> {barcode, name, enriching}
let _enrichTimer = null;

function renderLiveScans() {
  const list = document.getElementById('scan-live-list');
  if (!list) return;
  if (liveScans.size === 0) { list.classList.add('d-none'); list.innerHTML = ''; return; }
  list.classList.remove('d-none');
  // Newest first, capped so a long session does not grow without bound.
  const rows = Array.from(liveScans.entries()).reverse().slice(0, 12);
  list.innerHTML = rows.map(([id, s]) => {
    const body = s.enriching
      ? `<span class="spinner-border spinner-border-sm me-1"></span>Saved, looking up… <span class="text-muted">(${esc(s.barcode)})</span>`
      : (s.failed
          ? `<i class="bi bi-question-circle text-warning me-1"></i>Saved as <strong>${esc(s.name)}</strong> <span class="text-muted">(fix on Pending)</span>`
          : `<i class="bi bi-check-circle text-success me-1"></i><strong>${esc(s.name)}</strong>`);
    return `<li class="list-group-item py-1 px-0 small bg-transparent">${body}</li>`;
  }).join('');
}

function trackLiveScan(item, enriching) {
  if (!item || item.id == null) return;
  liveScans.set(item.id, {
    barcode: item.barcode || '',
    name: item.name || '',
    enriching: !!enriching,
    failed: !!item.lookup_failed,
  });
  renderLiveScans();
  if (enriching) ensureEnrichPoll();
}

function ensureEnrichPoll() {
  if (_enrichTimer) return;
  _enrichTimer = setInterval(pollEnrichingRows, 2000);
}

async function pollEnrichingRows() {
  const pendingIds = Array.from(liveScans.entries())
    .filter(([, s]) => s.enriching).map(([id]) => id);
  if (pendingIds.length === 0) { clearInterval(_enrichTimer); _enrichTimer = null; return; }
  try {
    const r = await fetch('pending/');
    if (!r.ok) return;
    const data = await r.json();
    const byId = new Map((data.items || []).map(it => [it.id, it]));
    let changed = false;
    for (const id of pendingIds) {
      const it = byId.get(id);
      const cur = liveScans.get(id);
      if (!it) {
        // The row was deleted (committed or removed) before we saw the name:
        // stop tracking it rather than spinning forever.
        liveScans.delete(id); changed = true; continue;
      }
      if (!it.enriching) {
        cur.name = it.name; cur.enriching = false; cur.failed = !!it.lookup_failed;
        changed = true;
      }
    }
    if (changed) renderLiveScans();
  } catch (e) { /* transient: the next tick retries */ }
}

// -------------------------------------------------------- client adaption --
// A kiosk panel has no camera and no file picker: the USB scanner (or a
// phone via the QR code) is the input there, so the camera-scanner card and
// the Photo/Receipt tab are hidden. Elsewhere, camera controls stay but the
// live-scan button is feature-detected (getUserMedia needs a secure context).
const isKiosk = (() => {
  try { return localStorage.getItem('kioskMode') === 'true'; } catch (e) { return false; }
})();
const hasCamera = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);

document.addEventListener('DOMContentLoaded', () => {
  if (!hasCamera) {
    const b = document.getElementById('startScanBtn');
    if (b) b.classList.add('d-none');
    // A non-secure origin makes the browser drop getUserMedia entirely, which
    // used to look identical to "no camera hardware" -- the only sign anything
    // was different was a small muted caption underneath. The banner's link is
    // a fixed address (the Food Hub Scanner Netlify page, see add.html) rather
    // than anything looked up from Settings: Food Hub's own https address
    // forces a 2FA login wall for any off-network visit, which defeats the
    // point of pointing someone at it just to scan a barcode.
    if (!window.isSecureContext) {
      const notice = document.getElementById('insecure-camera-notice');
      const hint = document.getElementById('camera-hint-default');
      if (notice) {
        notice.classList.remove('d-none');
        if (hint) hint.classList.add('d-none');
      }
    }
  }
  if (isKiosk) {
    const card = document.getElementById('camera-scan-card');
    if (card) card.classList.add('d-none');
    const photoTab = document.getElementById('photoTabItem');
    if (photoTab) photoTab.classList.add('d-none');
    const hint = document.getElementById('kiosk-scan-hint');
    if (hint) hint.classList.remove('d-none');
  }
});

// ------------------------------------------------------------------- scan --
// One entry point for every barcode on this page (manual fields, wedge
// bursts, camera decodes). The server routes the scan by the shared mode;
// the response's status tells us which pane's feedback to update, so a scan
// that raced a mode change still lands its message somewhere sensible.
//
// Food Hub, Sept 2026 "scan, confirm date, add": an Inventory-mode scan (not
// a grocycode label, which always consumes) no longer goes straight to
// pending/scan -- it looks the barcode up first and, on a confirmed match,
// asks for a best-by date and commits straight to stock via
// confirmAndQuickAdd() below. Every other mode, and anything the lookup
// can't resolve, still uses the original queue-then-review path.
var _GROCYCODE_PREFIX = 'grcy:';

async function submitScan(code) {
  code = (code || '').trim();
  if (!code) return;
  if (currentMode === 'inventory' && !code.toLowerCase().startsWith(_GROCYCODE_PREFIX)) {
    return confirmAndQuickAdd(code);
  }
  return submitScanToPending(code);
}

// The original scan behaviour: queue to Pending for review. Still used for
// every non-inventory mode, for grocycode labels, and as the fallback when a
// lookup for the new confirm-date flow can't resolve the barcode.
async function submitScanToPending(code) {
  const statusId = {
    inventory: 'barcode-status', consume: 'consume-status',
    shopping: 'shopping-status', audit: 'audit-scan-status',
  }[currentMode] || 'barcode-status';
  const statusEl = document.getElementById(statusId);
  if (statusEl) showStatus(statusId, '<span class="spinner-border spinner-border-sm me-1"></span>Working...', 'info');
  let result;
  try {
    const r = await fetch('pending/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ barcode: code, quantity: 1, source: 'scanner' }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      if (statusEl) showStatus(statusId, `Error: ${esc(err.detail || r.statusText)}`, 'warning');
      return;
    }
    result = await r.json();
  } catch (e) {
    if (statusEl) showStatus(statusId, 'Scan failed: ' + esc(String(e)), 'danger');
    return;
  }
  handleScanResult(code, result);
}

function handleScanResult(code, result) {
  // A grocycode label scan consumes in ANY mode, so its feedback targets the
  // pane the user is actually looking at, not just the consume pane.
  const paneStatusId = {
    inventory: 'barcode-status', consume: 'consume-status',
    shopping: 'shopping-status', audit: 'audit-scan-status',
  }[currentMode] || 'barcode-status';
  switch (result.status) {
    case 'queued':
    case 'merged':
      showInventoryResult(result);
      break;
    case 'instant_added': {
      // Food Hub "Scan Next Item": committed straight to Grocy, no pending
      // row was ever created, so this skips showInventoryResult/trackLiveScan
      // (both key off a pending id) and appends its own row to the same
      // on-screen recent-scans list instead.
      const name = (result.item && result.item.name) || code;
      showStatus('barcode-status',
        `<i class="bi bi-lightning-charge-fill text-success me-1"></i>Added straight to stock: <strong>${esc(name)}</strong>`,
        'success');
      liveScans.set(`instant-${Date.now()}-${code}`, { barcode: code, name, enriching: false, failed: false });
      renderLiveScans();
      _barcodeSavedCount++;
      { const badge = document.getElementById('barcode-saved-badge');
        const wrap = document.getElementById('barcode-saved-count');
        if (badge) badge.textContent = _barcodeSavedCount;
        if (wrap) wrap.classList.remove('d-none'); }
      refreshPendingBadge();
      break;
    }
    case 'consumed':
      showStatus(result.message ? paneStatusId : 'consume-status',
        result.message ? esc(result.message)
          : `Consumed <strong>${esc(result.name || code)}</strong> (1 off stock)`, 'success');
      if (result.log_offer) offerLogAsEaten(result.log_offer);
      addRecent('consume-recent', `<i class="bi bi-check-circle text-success me-1"></i>${esc(result.name || code)}`);
      break;
    case 'consume_failed':
      showStatus(paneStatusId,
        `Could not consume <strong>${esc(result.name || code)}</strong>: ${esc(result.error || 'unknown barcode or no stock')}`, 'warning');
      break;
    case 'shopping_added':
      showStatus('shopping-status', `Added <strong>${esc(result.name || code)}</strong> to the shopping list`, 'success');
      addRecent('shopping-recent', `<i class="bi bi-cart-plus text-success me-1"></i>${esc(result.name || code)}`);
      break;
    case 'shopping_failed':
      showStatus('shopping-status', `Could not add <strong>${esc(result.name || code)}</strong>: ${esc(result.error || '')}`, 'warning');
      break;
    case 'matched':
      showStatus('audit-scan-status', `<strong>${esc(result.name || code)}</strong> matched (count ${result.count})`, 'success');
      refreshAuditSummary();
      break;
    case 'unexpected':
      showStatus('audit-scan-status', `<strong>${esc(result.name || code)}</strong> is not expected at this location (count ${result.count})`, 'warning');
      refreshAuditSummary();
      break;
    case 'no_audit_session':
      showStatus('audit-scan-status', 'No audit in progress. Pick a location first.', 'warning');
      refreshAuditSummary();
      break;
    case 'rejected':
      showStatus({
        inventory: 'barcode-status', consume: 'consume-status',
        shopping: 'shopping-status', audit: 'audit-scan-status',
      }[currentMode] || 'barcode-status', 'That scan looked like several barcodes run together; try again.', 'warning');
      break;
    default:
      showInventoryResult(result);
  }
}

// One-tap calorie logging after a use-up scan (FoodAssistant-4mi3): when the
// consume reply carries the product's real calorie data, offer to add it to
// the nutrition journal. No data means no button; nothing is ever guessed.
// The button is built as DOM (listener holds the offer), so a product name
// never lands inside an onclick attribute.
function offerLogAsEaten(offer) {
  const status = document.getElementById('consume-status');
  if (!status || !offer || offer.calories == null) return;
  const wrap = document.createElement('div');
  wrap.className = 'mt-2';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-sm btn-outline-success';
  btn.innerHTML = `<i class="bi bi-heart-pulse me-1"></i>Log as eaten: ${Math.round(offer.calories)} kcal`
    + (offer.basis ? ` <span class="text-secondary">(${esc(offer.basis)})</span>` : '');
  btn.title = `Add ${offer.name} to today's nutrition journal`;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r = await fetch('nutrition/log', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          name: offer.name, servings: 1, calories: offer.calories,
          protein: offer.protein, carbs: offer.carbs, fat: offer.fat,
          source: 'barcode',
        }),
      });
      await readJson(r);
      wrap.innerHTML = `<span class="text-success small"><i class="bi bi-check2-circle me-1"></i>`
        + `${esc(offer.name)} added to today's journal.</span>`;
    } catch (e) {
      // Keep the button so the tap can be retried; say why it failed beside it.
      btn.disabled = false;
      note.innerHTML = `<span class="text-warning small ms-2">Could not log it: ${esc(String(e.message || e))}</span>`;
    }
  });
  const note = document.createElement('span');
  wrap.appendChild(btn);
  wrap.appendChild(note);
  status.appendChild(wrap);
}

function addRecent(listId, html) {
  const list = document.getElementById(listId);
  if (!list) return;
  list.classList.remove('d-none');
  const li = document.createElement('li');
  li.className = 'list-group-item py-1 px-0 small bg-transparent';
  li.innerHTML = html;
  list.insertBefore(li, list.firstChild);
  while (list.children.length > 8) list.removeChild(list.lastChild);
}

// ---------------------------------------------- scan, confirm date, add ----
// Food Hub, Sept 2026: an Inventory-mode scan looks the barcode up (read-only,
// GET /pending/lookup) and, on a match, shows a small modal with the
// suggested best-by date so it can be confirmed or corrected before the item
// goes straight to stock (POST /pending/quick-add) -- no Pending row at all.
// Anything the lookup can't resolve, or that fails to commit, falls back to
// submitScanToPending() so a scan is never silently lost.
var _confirmModalEl = null;
var _confirmBsModal = null;
var _confirmPending = null; // { code } while the modal is open, else null
var _confirmCal = null;     // the open modal's calendar controller
var _confirmDateIso = '';   // the currently chosen date (may be '' = none)

// A real month-grid calendar, not a native <input type="date"> -- Will
// specifically asked for "an actual calendar to select the right date"
// rather than whatever the browser's own picker happens to look like (a
// spinning wheel on iOS, a plain text box with a tiny icon on desktop). It is
// shown open in the modal already, with no extra tap needed to reveal it.
var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December'];
var DOW_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function pad2(n) { return n < 10 ? '0' + n : '' + n; }
function isoDate(y, m, d) { return y + '-' + pad2(m + 1) + '-' + pad2(d); }
function parseIsoDate(iso) {
  const p = iso.split('-').map(Number);
  return new Date(p[0], p[1] - 1, p[2]);
}

function ensureCalendarStyles() {
  if (document.getElementById('qa-cal-styles')) return;
  const style = document.createElement('style');
  style.id = 'qa-cal-styles';
  style.textContent =
    '.qa-cal{user-select:none}' +
    '.qa-cal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;font-weight:600}' +
    '.qa-cal-nav{background:none;border:none;font-size:1.3rem;line-height:1;padding:2px 10px;cursor:pointer;border-radius:6px}' +
    '.qa-cal-nav:hover{background:#e9ecef}' +
    '.qa-cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;text-align:center}' +
    '.qa-cal-dow{font-size:.72rem;color:#888;padding:4px 0}' +
    '.qa-cal-day{padding:7px 0;border-radius:6px;cursor:pointer;font-size:.9rem}' +
    '.qa-cal-day:hover{background:#e9ecef}' +
    '.qa-cal-day.qa-muted{color:#c3c7cd;cursor:default}' +
    '.qa-cal-day.qa-muted:hover{background:none}' +
    '.qa-cal-day.qa-today{box-shadow:inset 0 0 0 1px #6c757d}' +
    '.qa-cal-day.qa-selected{background:#198754;color:#fff}';
  document.head.appendChild(style);
}

// Builds a month-grid calendar inside `container`. `initialIso` ("" or a
// YYYY-MM-DD string) is the day to start selected/showing. `onPick(iso)`
// fires on every tap of a real day in the grid. Returns a small controller
// so the caller can clear the selection (a "No date" option) without
// rebuilding the whole thing.
function buildCalendar(container, initialIso, onPick) {
  const today = new Date();
  let sel = initialIso ? parseIsoDate(initialIso) : null;
  let viewY = (sel || today).getFullYear();
  let viewM = (sel || today).getMonth();

  function render() {
    const startDow = (new Date(viewY, viewM, 1).getDay() + 6) % 7; // Monday=0
    const daysInM = new Date(viewY, viewM + 1, 0).getDate();
    const prevDays = new Date(viewY, viewM, 0).getDate();
    let cells = '';
    for (let i = 0; i < startDow; i++) {
      cells += '<div class="qa-cal-day qa-muted">' + (prevDays - startDow + 1 + i) + '</div>';
    }
    for (let d = 1; d <= daysInM; d++) {
      let cls = 'qa-cal-day';
      if (viewY === today.getFullYear() && viewM === today.getMonth() && d === today.getDate()) cls += ' qa-today';
      if (sel && viewY === sel.getFullYear() && viewM === sel.getMonth() && d === sel.getDate()) cls += ' qa-selected';
      cells += '<div class="' + cls + '" data-day="' + d + '">' + d + '</div>';
    }
    const trailing = (7 - ((startDow + daysInM) % 7)) % 7;
    for (let n = 1; n <= trailing; n++) cells += '<div class="qa-cal-day qa-muted">' + n + '</div>';
    container.innerHTML =
      '<div class="qa-cal">' +
      '<div class="qa-cal-head">' +
      '<button type="button" class="qa-cal-nav" data-nav="-1">&lsaquo;</button>' +
      '<span>' + MONTH_NAMES[viewM] + ' ' + viewY + '</span>' +
      '<button type="button" class="qa-cal-nav" data-nav="1">&rsaquo;</button>' +
      '</div><div class="qa-cal-grid">' +
      DOW_NAMES.map(n => '<div class="qa-cal-dow">' + n + '</div>').join('') +
      cells + '</div></div>';
  }

  // Assignment (not addEventListener) so re-opening the modal never stacks a
  // second listener on the same container.
  container.onclick = function (ev) {
    const nav = ev.target.closest('[data-nav]');
    if (nav) {
      viewM += parseInt(nav.dataset.nav, 10);
      if (viewM < 0) { viewM = 11; viewY--; } else if (viewM > 11) { viewM = 0; viewY++; }
      render();
      return;
    }
    const dayEl = ev.target.closest('.qa-cal-day:not(.qa-muted)');
    if (dayEl) {
      const day = parseInt(dayEl.dataset.day, 10);
      sel = new Date(viewY, viewM, day);
      onPick(isoDate(viewY, viewM, day));
      render();
    }
  };
  render();
  return { clear: function () { sel = null; render(); } };
}

function ensureConfirmModal() {
  if (_confirmModalEl) return;
  ensureCalendarStyles();
  const wrap = document.createElement('div');
  wrap.innerHTML =
    '<div class="modal fade" id="quickAddConfirmModal" tabindex="-1" ' +
    'data-bs-backdrop="static" data-bs-keyboard="false">' +
    '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
    '<div class="modal-header"><h5 class="modal-title" id="quickAddConfirmName">Item</h5></div>' +
    '<div class="modal-body">' +
    '<div id="quickAddCal"></div>' +
    '<div class="d-flex justify-content-between align-items-start mt-2">' +
    '<div class="form-text mb-0" id="quickAddConfirmHint"></div>' +
    '<button type="button" class="btn btn-sm btn-link p-0 flex-shrink-0 ms-2" id="quickAddNoDate">No date</button>' +
    '</div></div>' +
    '<div class="modal-footer">' +
    '<button type="button" class="btn btn-outline-secondary" id="quickAddConfirmLater">Review later instead</button>' +
    '<button type="button" class="btn btn-success" id="quickAddConfirmBtn">' +
    '<i class="bi bi-check-lg me-1"></i>Add to stock</button>' +
    '</div></div></div></div>';
  document.body.appendChild(wrap.firstElementChild);
  _confirmModalEl = document.getElementById('quickAddConfirmModal');
  _confirmBsModal = new bootstrap.Modal(_confirmModalEl);
  document.getElementById('quickAddConfirmBtn').addEventListener('click', onQuickAddConfirmed);
  document.getElementById('quickAddConfirmLater').addEventListener('click', onQuickAddDeferred);
  document.getElementById('quickAddNoDate').addEventListener('click', function () {
    _confirmDateIso = '';
    if (_confirmCal) _confirmCal.clear();
  });
}

async function confirmAndQuickAdd(code) {
  // A confirm prompt is already open for an earlier scan -- ignore repeats
  // (the camera/wedge can fire again before the person has answered).
  if (_confirmPending) return;
  showStatus('barcode-status',
    '<span class="spinner-border spinner-border-sm me-1"></span>Looking up...', 'info');
  let data;
  try {
    const r = await fetch('pending/lookup?barcode=' + encodeURIComponent(code));
    data = await r.json();
  } catch (e) {
    showStatus('barcode-status', 'Lookup failed: ' + esc(String(e)), 'danger');
    return;
  }
  if (!data || data.status !== 'found') {
    // Unknown / store-local / lookup trouble: fall back to the ordinary
    // queue-to-Pending flow rather than lose the scan.
    showStatus('barcode-status',
      "Couldn't identify that barcode -- sending to Pending for review.", 'warning');
    return submitScanToPending(code);
  }
  ensureConfirmModal();
  _confirmPending = { code };
  _confirmDateIso = data.best_by_date || '';
  document.getElementById('quickAddConfirmName').textContent = data.name || code;
  document.getElementById('quickAddConfirmHint').textContent = data.best_by_date
    ? 'Suggested from the product itself -- tap a different day if the pack in your hand differs.'
    : 'No date could be worked out -- pick the one on the pack, or leave it blank.';
  _confirmCal = buildCalendar(document.getElementById('quickAddCal'), _confirmDateIso,
    function (iso) { _confirmDateIso = iso; });
  showStatus('barcode-status', '', 'info');
  _confirmBsModal.show();
}

async function onQuickAddConfirmed() {
  if (!_confirmPending) return;
  const code = _confirmPending.code;
  const dateVal = _confirmDateIso || '';
  _confirmBsModal.hide();
  _confirmPending = null;
  showStatus('barcode-status',
    '<span class="spinner-border spinner-border-sm me-1"></span>Adding...', 'info');
  let result;
  try {
    const r = await fetch('pending/quick-add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ barcode: code, quantity: 1, best_by_date: dateVal }),
    });
    result = await r.json();
  } catch (e) {
    showStatus('barcode-status', 'Add failed: ' + esc(String(e)), 'danger');
    return;
  }
  if (result.status === 'instant_added') {
    handleScanResult(code, result);
  } else {
    showStatus('barcode-status',
      "Couldn't add it straight to stock (" + esc(result.error || result.status) +
      ') -- sending to Pending for review instead.', 'warning');
    await submitScanToPending(code);
  }
}

function onQuickAddDeferred() {
  if (!_confirmPending) return;
  const code = _confirmPending.code;
  _confirmBsModal.hide();
  _confirmPending = null;
  submitScanToPending(code);
}

var _barcodeSavedCount = 0;

// Pull the current pending count and update the navbar badge.
function refreshPendingBadge() {
  fetch('pending/count').then(r => r.json()).then(d => {
    const b = document.getElementById('pending-nav-badge');
    if (b && d.count > 0) { b.textContent = d.count; b.classList.remove('d-none'); }
  }).catch(() => {});
}

function showInventoryResult(result) {
  const item = result.item || {};
  const name = item.name || '';
  const merged = result.status === 'merged';
  // A fast-acked new scan is still resolving its name in the background, so the
  // status line says so honestly instead of flashing the "Unknown (...)"
  // placeholder; the live list row fills in the name when the lookup lands.
  const enriching = !merged && !!result.enriching;
  showStatus('barcode-status',
    merged ? `<strong>${esc(name)}</strong> (quantity bumped, already pending)`
    : enriching ? '<span class="spinner-border spinner-border-sm me-1"></span>Saved, looking up…'
                : `Saved: <strong>${esc(name)}</strong>`,
    'success');
  if (!merged) trackLiveScan(item, enriching);
  _barcodeSavedCount++;
  const badge = document.getElementById('barcode-saved-badge');
  const wrap = document.getElementById('barcode-saved-count');
  if (badge) badge.textContent = _barcodeSavedCount;
  if (wrap) wrap.classList.remove('d-none');
  refreshPendingBadge();
}

function lookupBarcode() {
  const input = document.getElementById('barcodeInput');
  const code = input.value.trim();
  if (!code) return;
  input.value = '';
  submitScan(code);
}

function consumeScan() {
  const input = document.getElementById('consumeInput');
  const code = input.value.trim();
  if (!code) return;
  input.value = '';
  submitScan(code);
}

function shoppingScan() {
  const input = document.getElementById('shoppingBarcodeInput');
  if (!input) return;
  const code = input.value.trim();
  if (!code) return;
  input.value = '';
  submitScan(code);
}

function auditScan() {
  const input = document.getElementById('auditInput');
  const code = input.value.trim();
  if (!code) return;
  input.value = '';
  submitScan(code);
}

// ------------------------------------------------------- shopping by name --
// As you type, offer matching item names in a datalist: your Grocy product
// names, or Mealie food names on a Mealie-backed list. This is a convenience
// only: the input stays a plain text box, so a slow or failed fetch just
// means no suggestions and free text still adds exactly as before.
let _shoppingSuggestTimer = null;
function shoppingSuggest(value) {
  const q = (value || '').trim();
  if (_shoppingSuggestTimer) clearTimeout(_shoppingSuggestTimer);
  const list = document.getElementById('shoppingNameSuggestions');
  if (!list) return;
  if (q.length < 2) { list.innerHTML = ''; return; }
  _shoppingSuggestTimer = setTimeout(async () => {
    try {
      const r = await fetch('mealie/foods/suggest?q=' + encodeURIComponent(q));
      if (!r.ok) return;
      const data = await r.json();
      const names = (data && data.suggestions) || [];
      list.innerHTML = names.map(n => `<option value="${esc(n)}"></option>`).join('');
    } catch (e) { /* fail soft: leave the input a plain text box */ }
  }, 250);
}

// Quick-add a named item straight to the default shopping list, the
// same list a shopping-mode scan lands on.
async function addShoppingByName() {
  const input = document.getElementById('shoppingNameInput');
  if (!input) return;
  const note = input.value.trim();
  if (!note) return;
  showStatus('shopping-status', '<span class="spinner-border spinner-border-sm me-1"></span>Adding...', 'info');
  try {
    const lr = await fetch('mealie/shopping');
    if (!lr.ok) throw new Error('shopping list unavailable');
    const data = await lr.json();
    // An outage arrives as an honest error field; show that rather than
    // claiming there is no list (FoodAssistant-2cmm).
    if (data.error) throw new Error(data.error);
    const listId = data.list && data.list.id;
    if (!listId) throw new Error('no shopping list yet');
    const r = await fetch('mealie/shopping/items', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ list_id: listId, note }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    showStatus('shopping-status', `Added <strong>${esc(note)}</strong> to the shopping list`, 'success');
    addRecent('shopping-recent', `<i class="bi bi-cart-plus text-success me-1"></i>${esc(note)}`);
    input.value = '';
  } catch (e) {
    showStatus('shopping-status', 'Could not add item: ' + esc(String(e.message || e)), 'warning');
  }
}

// ------------------------------------------------------------ audit state --
// The Audit tab is a front door to the existing audit flow: it shows whether
// a count is running (and where) and hands off to /ui/audit for the location
// pick and the full matched/missing/unexpected view.
async function refreshAuditSummary() {
  const inactive = document.getElementById('audit-inactive');
  const active = document.getElementById('audit-active');
  if (!inactive || !active) return;
  let s = { active: false };
  try {
    const r = await fetch('audit/status');
    if (r.ok) s = await r.json();
  } catch (e) { /* leave inactive */ }
  inactive.classList.toggle('d-none', !!s.active);
  active.classList.toggle('d-none', !s.active);
  if (s.active) {
    document.getElementById('audit-location').textContent = s.location || '';
    const c = s.counts || {};
    document.getElementById('audit-counts').textContent =
      `${c.seen || 0} of ${c.expected || 0} seen, ${c.missing || 0} missing, ${c.unexpected || 0} unexpected`;
  }
}

// ------------------------------------------------------- keyboard wedge ----
// Global keyboard-wedge capture. USB and Bluetooth HID barcode scanners type
// the code as a fast burst of keystrokes ending in Enter. Capture that burst
// anywhere on this page (not just when a field is focused) and route it
// through submitScan, so the scan follows the active mode tab. Only a rapid
// burst is treated as a scan, so ordinary typing is never hijacked.
(function () {
  var buf = '';
  var last = 0;
  var GAP_MS = 50;   // real keystrokes are slower than a scanner's output
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    var ae = document.activeElement;
    var inField = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT');
    var now = Date.now();
    if (now - last > GAP_MS) buf = '';   // a gap starts a fresh burst
    last = now;
    if (e.key === 'Enter') {
      if (!inField && buf.length >= 6) {
        submitScan(buf);
        e.preventDefault();
      }
      buf = '';
      return;
    }
    if (e.key.length === 1) buf += e.key;
  });
})();

async function startScanner() {
  document.getElementById('startScanBtn').classList.add('d-none');
  document.getElementById('stopScanBtn').classList.remove('d-none');
  try {
    await ensureQrLib();
  } catch (e) {
    showStatus('barcode-status', 'The camera scanner could not load. Use the barcode box or a scanner instead.', 'warning');
    document.getElementById('startScanBtn').classList.remove('d-none');
    document.getElementById('stopScanBtn').classList.add('d-none');
    return;
  }
  // Product barcodes only (plus QR): telling the decoder what to look for
  // makes each frame cheaper and misreads rarer.
  scanner = new Html5Qrcode("reader", {
    formatsToSupport: [
      Html5QrcodeSupportedFormats.EAN_13, Html5QrcodeSupportedFormats.EAN_8,
      Html5QrcodeSupportedFormats.UPC_A, Html5QrcodeSupportedFormats.UPC_E,
      Html5QrcodeSupportedFormats.CODE_128, Html5QrcodeSupportedFormats.CODE_39,
      Html5QrcodeSupportedFormats.QR_CODE,
    ],
    verbose: false,
  });
  scanner.start(
    { facingMode: "environment" },
    {
      fps: 10,
      qrbox: { width: 280, height: 120 },
      // The phone's native barcode detector (Android Chrome) decodes far
      // faster and more tolerantly than the JS fallback (FoodAssistant-fvuy).
      experimentalFeatures: { useBarCodeDetectorIfSupported: true },
      // The default camera stream is a soft VGA feed with no autofocus
      // request: exactly why phones "struggle to focus" on a barcode a few
      // inches away. Ask for a sharp feed and continuous focus up front.
      videoConstraints: {
        facingMode: "environment",
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        advanced: [{ focusMode: "continuous" }],
      },
    },
    (decodedText) => {
      // Quick Add keeps the camera rolling in Inventory mode so a shelf of
      // items can be scanned back-to-back without re-tapping Start each time;
      // every other mode/setting keeps the original stop-after-each-scan
      // behavior unchanged.
      const keepScanning = QUICK_ADD_MODE && currentMode === 'inventory';
      if (!keepScanning) stopScanner();
      submitScan(decodedText);
    },
    () => {}
  ).then(() => {
    // Where the hardware allows it, force continuous focus and a touch of
    // zoom: many phone cameras cannot focus closer than ~10cm, and 2x zoom
    // effectively shortens that so a label-filling barcode gets sharp.
    try {
      const caps = scanner.getRunningTrackCapabilities() || {};
      const tweak = {};
      if (Array.isArray(caps.focusMode) && caps.focusMode.includes('continuous')) {
        tweak.focusMode = 'continuous';
      }
      if (caps.zoom && caps.zoom.max >= 2) {
        tweak.zoom = Math.min(2, caps.zoom.max);
      }
      if (Object.keys(tweak).length) {
        scanner.applyVideoConstraints({ advanced: [tweak] }).catch(() => {});
      }
    } catch (e) { /* capability probing is best-effort */ }
  }).catch(err => {
    showStatus('barcode-status', 'Camera error: ' + err, 'danger');
    stopScanner();
  });
}

function stopScanner() {
  if (scanner) {
    scanner.stop().catch(() => {});
    scanner = null;
  }
  document.getElementById('startScanBtn').classList.remove('d-none');
  document.getElementById('stopScanBtn').classList.add('d-none');
}

// Photo Upload
function handleDrop(e) {
  e.preventDefault();
  e.currentTarget.style.borderColor = '';
  const file = e.dataTransfer.files[0];
  if (file) setPhotoFile(file);
}

function handleFileSelect(input) {
  if (input.files[0]) setPhotoFile(input.files[0]);
}

function setPhotoFile(file) {
  pendingFile = file;
  const reader = new FileReader();
  reader.onload = e => {
    document.getElementById('preview-img').src = e.target.result;
    document.getElementById('photo-preview').classList.remove('d-none');
  };
  reader.readAsDataURL(file);
  document.getElementById('analyzeBtn').disabled = false;
}

async function analyzePhoto() {
  if (!pendingFile) return;
  const type = document.querySelector('input[name=photoType]:checked').value;
  const endpoint = type === 'receipt' ? 'analyze/receipt' : 'analyze/food';

  const what = type === 'receipt' ? 'receipt' : 'photo';
  showStatus('photo-status', `<span class="spinner-border spinner-border-sm me-1"></span>Analyzing ${what}...`, 'info');
  document.getElementById('analyzeBtn').disabled = true;

  const fd = new FormData();
  fd.append('file', pendingFile);

  try {
    const r = await fetch(endpoint, { method: 'POST', body: fd });
    const result = await readJson(r);
    if (type === 'receipt') {
      // A receipt often has many items and takes a while to review, so save the
      // parsed items straight to Pending. They stay put even if you leave this
      // page, and you review, edit, and import them from the Pending list, the
      // same as a barcode scan (FoodAssistant-dq4j).
      const pr = await fetch('pending/items', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: result.items, source: 'receipt' }),
      });
      const saved = await readJson(pr);
      showStatus('photo-status',
        `Saved <strong>${saved.added}</strong> item(s) to <a href="ui/pending">Pending</a>: review and import from there.`,
        'success');
      refreshPendingBadge();
    } else {
      addToQueue(result.items);
      showStatus('photo-status',
        `Found <strong>${result.items.length}</strong> item(s). Added to import queue.`, 'success');
    }
    pendingFile = null;
    document.getElementById('photo-preview').classList.add('d-none');
  } catch(e) {
    showStatus('photo-status', 'Analysis failed: ' + (e && e.message ? e.message : e), 'danger');
    document.getElementById('analyzeBtn').disabled = false;
  }
}

// Manual Entry
function addManualItem() {
  const name = document.getElementById('manual-name').value.trim();
  if (!name) { alert('Name is required.'); return; }
  addToQueue([{
    name,
    quantity: parseFloat(document.getElementById('manual-qty').value) || 1,
    unit: document.getElementById('manual-unit').value || 'item',
    category: document.getElementById('manual-category').value,
    storage_type: document.getElementById('manual-storage').value,
    best_by_date: document.getElementById('manual-bestby').value || null,
    brand: document.getElementById('manual-brand').value || null,
    notes: document.getElementById('manual-notes').value || null,
    confidence: 1.0,
  }]);
  document.getElementById('manual-name').value = '';
  document.getElementById('manual-brand').value = '';
  document.getElementById('manual-notes').value = '';
  document.getElementById('manual-bestby').value = '';
}

// Queue Management
function addToQueue(items) {
  queue.push(...items);
  renderQueue();
  document.getElementById('results-section').classList.remove('d-none');
}

function removeFromQueue(i) {
  queue.splice(i, 1);
  renderQueue();
  if (queue.length === 0) document.getElementById('results-section').classList.add('d-none');
}

function clearQueue() {
  queue = [];
  renderQueue();
  document.getElementById('results-section').classList.add('d-none');
}

function renderQueue() {
  document.getElementById('queue-count').textContent = queue.length;
  const list = document.getElementById('results-list');
  if (queue.length === 0) { list.innerHTML = ''; return; }

  list.innerHTML = queue.map((item, i) => `
    <div class="card mb-2">
      <div class="card-body py-2">
        <div class="row g-2 align-items-center">
          <div class="col-md-3">
            <input class="form-control form-control-sm" value="${esc(item.name)}"
                   onchange="queue[${i}].name=this.value" placeholder="Name">
          </div>
          <div class="col-6 col-md-1">
            <input class="form-control form-control-sm" type="number" value="${item.quantity}"
                   onchange="queue[${i}].quantity=parseFloat(this.value)" placeholder="Qty" min="0.01" step="0.01">
          </div>
          <div class="col-6 col-md-1">
            <input class="form-control form-control-sm" value="${esc(item.unit||'item')}"
                   onchange="queue[${i}].unit=this.value" placeholder="Unit">
          </div>
          <div class="col-md-2">
            <select class="form-select form-select-sm" onchange="queue[${i}].category=this.value">
              ${categoryOptions(item.category)}
            </select>
          </div>
          <div class="col-md-2">
            <select class="form-select form-select-sm" onchange="queue[${i}].storage_type=this.value">
              ${storageOptions(item.storage_type)}
            </select>
          </div>
          <div class="col-md-2">
            <input class="form-control form-control-sm" type="date" value="${item.best_by_date||''}"
                   onchange="queue[${i}].best_by_date=this.value||null" title="Best by date">
          </div>
          <div class="col-md-1 text-end">
            <button class="btn btn-sm btn-outline-danger btn-icon" onclick="removeFromQueue(${i})" title="Remove">
              <i class="bi bi-x-lg"></i>
            </button>
          </div>
        </div>
        ${item.brand ? `<div class="text-muted small mt-1 ms-1">${esc(item.brand)}</div>` : ''}
      </div>
    </div>
  `).join('');
}

async function importAll() {
  if (queue.length === 0) return;
  showStatus('import-status', '<span class="spinner-border spinner-border-sm me-1"></span>Importing...', 'info');
  try {
    const r = await fetch('inventory/import', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ items: queue }),
    });
    const result = await r.json();
    const ok = result.results.filter(r => r.status === 'ok').length;
    const err = result.results.filter(r => r.status === 'error').length;
    // Product ids of everything that imported cleanly, for a batch label print.
    const printedIds = result.results
      .filter(r => r.status === 'ok' && r.product_id != null)
      .map(r => r.product_id);
    if (err === 0) {
      showStatus('import-status', `✅ Added ${ok} item(s) to your inventory.`, 'success');
      clearQueue();
    } else {
      const errs = result.results.filter(r => r.status === 'error').map(r => r.error).join('; ');
      showStatus('import-status', `⚠️ ${ok} imported, ${err} failed: ${errs}`, 'warning');
    }
    offerBatchLabels(printedIds);
  } catch(e) {
    showStatus('import-status', 'Could not add those items. ' + esc(prReason(e)), 'danger');
  }
}

// After an import, offer to print a label for the whole batch at once
// (FoodAssistant-fb8x). Only rendered when printing is turned on and something
// actually imported; clicking sends every product id to the batch endpoint.
//
// A batch bigger than BATCH_CONFIRM_THRESHOLD asks first (FoodAssistant-np6o):
// it is easy to end up with a big queue after a receipt scan, and running the
// label printer for dozens of labels by one stray click is worth a check. The
// server enforces the same threshold (POST /printing/label/batch answers
// {needs_confirmation: true, count} instead of printing), so confirmed: true
// is always sent once the user has agreed, and the server-side check below is
// a fallback in case the two thresholds ever drift.
const BATCH_CONFIRM_THRESHOLD = 5;

function offerBatchLabels(productIds) {
  if (!PRINTING_ENABLED || !productIds || productIds.length === 0) return;
  const holder = document.getElementById('import-status');
  const btn = document.createElement('button');
  btn.className = 'btn btn-outline-secondary btn-sm mt-2';
  btn.innerHTML = `<i class="bi bi-printer me-1"></i>Print labels for this batch (${productIds.length})`;
  btn.onclick = async () => {
    if (productIds.length > BATCH_CONFIRM_THRESHOLD &&
        !confirm(`Print ${productIds.length} labels now?`)) {
      return;
    }
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Printing...';
    try {
      const send = async (confirmed) => {
        const r = await fetch('/printing/label/batch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ product_ids: productIds, confirmed }),
        });
        return { r, d: await r.json().catch(() => ({})) };
      };
      let { r, d } = await send(true);
      if (r.ok && d.needs_confirmation) {
        // Fallback path: the server wanted a confirm we did not already give
        // (thresholds drifted). Ask once more, then send confirmed for real.
        if (!confirm(`Print ${d.count} labels now?`)) {
          btn.disabled = false;
          btn.innerHTML = `<i class="bi bi-printer me-1"></i>Print labels for this batch (${productIds.length})`;
          return;
        }
        ({ r, d } = await send(true));
      }
      if (!r.ok || !d.ok) throw new Error(d.detail || d.error || r.statusText);
      btn.className = 'btn btn-outline-success btn-sm mt-2';
      btn.innerHTML = `<i class="bi bi-check-lg me-1"></i>Sent ${d.printed} label(s)`;
    } catch (e) {
      btn.disabled = false;
      btn.className = 'btn btn-outline-danger btn-sm mt-2';
      btn.innerHTML = `<i class="bi bi-printer me-1"></i>Print failed: ${e.message}`;
    }
  };
  holder.appendChild(document.createElement('br'));
  holder.appendChild(btn);
}

// Helpers
const CATEGORIES = ['Poultry','Meat','Seafood','Dairy','Produce','Grains','Condiments','Beverages','Snacks','Frozen','Canned','Other'];
const STORAGES   = ['refrigerated','frozen','room_temp','dry'];
const STORAGE_LABELS = {refrigerated:'Refrigerated', frozen:'Frozen', room_temp:'Room Temp', dry:'Dry'};

function categoryOptions(selected) {
  return CATEGORIES.map(c =>
    `<option value="${c}" ${c===selected?'selected':''}>${c}</option>`
  ).join('');
}

function storageOptions(selected) {
  return STORAGES.map(s =>
    `<option value="${s}" ${s===selected?'selected':''}>${STORAGE_LABELS[s]}</option>`
  ).join('');
}

function showStatus(id, html, type) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = `<div class="alert alert-${type} py-1 px-2 mb-0 small">${html}</div>`;
}

// A human message for a reply we could not read as JSON, keyed on the HTTP
// status. This is what turns a bare "SyntaxError: The string did not match the
// expected pattern" (WebKit's JSON.parse failure) into something a cook can act
// on: an expired phone session over the tunnel, a photo too big for a proxy in
// front, or the server / AI taking too long (FoodAssistant-3w02).
function friendlyHttpError(status) {
  if (status === 401) return 'Your session has expired. Reload the page and sign in again.';
  if (status === 413) return 'That photo is too large to upload. Try again with a smaller photo.';
  if (status === 502 || status === 503 || status === 504) {
    return 'The server or AI service did not respond in time. Wait a moment, then try again.';
  }
  return `The server returned an unexpected response (HTTP ${status}). Try again in a moment.`;
}

// Pull a string message out of an error body, tolerating FastAPI's {detail},
// the demo/plain {message}/{error} shapes, and a nested {detail:{detail}}.
function errorDetail(data) {
  if (!data || typeof data !== 'object') return '';
  let d = data.detail;
  if (d == null) d = data.message || data.error;
  if (d && typeof d === 'object') d = d.detail || d.message || '';
  return typeof d === 'string' ? d : '';
}

// Read a fetch Response that is meant to be JSON WITHOUT letting a non-JSON
// body throw a raw parse error onto the banner. The body is read once as text
// and parsed defensively: a plain-text 500 from the app, or an HTML 413/502/504
// from a reverse proxy (Pangolin, on the tunnelled main server), no longer
// blows up JSON.parse. On an error status we prefer the server's own honest
// detail when it sent JSON, and fall back to a status-based message otherwise.
async function readJson(r) {
  const raw = await r.text();
  let data = null;
  try { data = raw ? JSON.parse(raw) : null; } catch (_) { data = null; }
  if (r.ok) {
    if (data === null) throw new Error(friendlyHttpError(r.status));
    return data;
  }
  if (r.status === 401) throw new Error(friendlyHttpError(401));
  throw new Error(errorDetail(data) || friendlyHttpError(r.status));
}

function esc(s) {
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
}
