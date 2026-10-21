'use strict';

const icons = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  phones: '<rect x="2" y="4" width="8" height="16" rx="2"/><rect x="14" y="4" width="8" height="16" rx="2"/><path d="M5 17h2m10 0h2M5 7h2m10 0h2"/>',
  arrows: '<path d="M4 7h15m-4-4 4 4-4 4M20 17H5m4-4-4 4 4 4"/>',
  undo: '<path d="M3 4v6h6M4 10a8 8 0 1 1 0 6"/>',
  shield: '<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z"/><path d="m8 12 3 3 5-6"/>',
  spark: '<path d="m12 2 2.4 7.6L22 12l-7.6 2.4L12 22l-2.4-7.6L2 12l7.6-2.4L12 2Z"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4m0 3h.01"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  wallet: '<path d="M20 8H5a2 2 0 0 1 0-4h12v4M4 8v10a2 2 0 0 0 2 2h14V8m0 4h-5v4h5m-2-2h.01"/>',
  download: '<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  coffee: '<path d="M4 9h12v8a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4V9Zm12 1h2a3 3 0 0 1 0 6h-2M7 2v3m6-3v3"/>',
  sync: '<path d="M20 4v6h-6M4 20v-6h6M4.5 8a8 8 0 0 1 13-4l2.5 3M4 17l2.5 3a8 8 0 0 0 13-4"/>',
  signal: '<path d="M3 19v2m5-6v6m5-11v11m5-16v16"/>',
  phone: '<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18h4M10 5h4"/>',
  receipt: '<path d="M5 3h14v18l-3-2-4 2-4-2-3 2V3Zm4 5h6m-6 4h6m-6 4h3"/>',
  disconnect: '<path d="m3 3 18 18M3 9a15 15 0 0 1 4-2m5-1a15 15 0 0 1 9 3M6 13a10 10 0 0 1 5-2m6 1 1 1M9 17a5 5 0 0 1 6 0m-3 4h.01"/>',
};
const icon = (name, cls='') => `<svg class="${cls}" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.receipt}</svg>`;
const $ = (s) => document.querySelector(s);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => new Intl.NumberFormat('en-IN', {style:'currency',currency:'INR',maximumFractionDigits:2,minimumFractionDigits:2}).format(value / 100);
const short = id => id ? `PS-${id.slice(0,8).toUpperCase()}` : '—';
const date = seconds => new Date(seconds * 1000).toLocaleString('en-IN', {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
const labels = {INITIATED:'Initiated',DEBITED:'Awaiting credit',CREDITED:'Credit confirmed',SETTLED:'Settled',FAILED:'Failed',REFUND_PENDING:'Refund pending',REFUNDED:'Refunded'};
const titles = {overview:'Overview',room:'Payment room',transactions:'Transactions',refunds:'Refunds',system:'System & guide'};
const scenarioNames = {instant:'Instant confirmation',delayed:'Delayed credit',offline:'Interrupted sync',declined:'Declined before debit'};
let state = null, view = 'overview', selected = null, csrf = '', filter = 'all', search = '', lastState = '', busy = false, connected = true, toastTimer = null, receiptId = null, decision = null;
let pending = null, agentReport = null;
try { pending = JSON.parse(sessionStorage.getItem('paysync-request') || 'null'); } catch { /* Browser storage is optional. */ }

function hydrate(root=document) {
  root.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
}
function status(s) { return s ? `<span class="status ${esc(s)}"><i></i>${esc(labels[s] || s)}</span>` : '<span class="status">Not received yet</span>'; }
function toast(message, error=false) {
  const el = $('#toast'); el.textContent = message; el.className = `toast${error ? ' error' : ''}`;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.add('hidden'), error ? 9000 : 6500);
}
function savePending(value) {
  pending = value;
  try { if (value) sessionStorage.setItem('paysync-request', JSON.stringify(value)); else sessionStorage.removeItem('paysync-request'); } catch { /* Keep in memory if storage is disabled. */ }
}
async function api(path, body) {
  const response = await fetch(path, {method: body ? 'POST':'GET', headers: body ? {'Content-Type':'application/json','X-CSRF-Token':csrf} : {}, body: body ? JSON.stringify(body):undefined, signal:AbortSignal.timeout(10000)});
  let result;
  try { result = await response.json(); } catch { throw new Error('The workspace returned an unreadable response. Refresh and check the server.'); }
  if (!response.ok) { const error = new Error(result.error || 'The request could not be completed.'); error.status = response.status; throw error; }
  return result;
}
function updateBanner() {
  const el = $('#connection-banner');
  const message = !connected ? 'Connection to the local server was lost. Displaying the last saved view. Start the server and reconnect; an uncertain payment request keeps its original request ID.' : state?.fault ? 'The workspace paused writes after a storage error. Inspect the server log before restarting.' : state?.stats?.conflicts ? 'A payment has conflicting terminal outcomes between the two devices. The local outcome was kept; review it manually.' : '';
  el.textContent = message; el.classList.toggle('hidden', !message);
}
async function loadAgent() { try { agentReport = await api('/api/agent'); } catch { /* agent panel is optional */ } }
function agentRows() {
  if (!agentReport) return '<p class="system-text">Loading findings…</p>';
  const open = agentReport.findings.filter(f => f.severity !== 'ok');
  if (!open.length) return '<p class="system-text">All payments look healthy. Nothing to propose.</p>';
  return open.map(f => `<div class="system-row"><span><strong>${esc(f.summary)}</strong><br>${esc(f.explanation)}</span>${f.proposal_id ? `<button class="button tiny" data-agent-approve="${esc(f.proposal_id)}">Approve: ${esc(f.action)}</button>` : '<span class="subtle-label">Needs a human</span>'}</div>`).join('');
}
async function refresh(force=false) {
  try {
    const next = await api('/api/state');
    if (view === 'system') await loadAgent();
    connected = true; state = next; updateBanner();
    // An acknowledged request stays pinned while its dialog is open, so a resubmit replays the SAME request ID
    // (never a new debit). It is released when the dialog closes or the replay returns.
    if (pending && !$('#payment-dialog').open && next.payments.some(p => p.id === pending.txn_id)) savePending(null);
    if (!selected && next.payments.length) selected = next.payments[0].id;
    const signature = JSON.stringify(next) + JSON.stringify(agentReport);
    if (signature !== lastState || force) { lastState = signature; render(); }
  } catch (error) {
    connected = false; updateBanner();
    if (!state) $('#main').innerHTML = `<div class="panel"><div class="empty"><div class="empty-icon">${icon('disconnect')}</div><h3>Your workspace is unavailable</h3><p>Start the Python server, then reconnect. Your saved payments stay in the local data folder.</p><button class="button primary" data-action="reconnect">Reconnect ${icon('sync')}</button></div></div>`;
  }
}
function navigate(next, focus=false) {
  if (!(next in titles)) next = 'overview';
  view = next; decision = null; render();
  if (next === 'system') loadAgent().then(render);
  if (focus) { $('#main').focus(); window.scrollTo({top:0,behavior:'instant'}); }
}
function heading(title, subtitle, actions=true) {
  return `<div class="page-heading"><div><h1>${title}</h1><p>${subtitle}</p></div>${actions ? `<div class="heading-actions"><a href="/api/export" class="button secondary-action">${icon('download')} Export</a><button class="button primary" data-action="new">${icon('plus')} New payment</button></div>` : ''}</div>`;
}
function empty(title, text, action='new', button='Create a payment') {
  return `<div class="empty"><div class="empty-icon">${icon('arrows')}</div><h3>${title}</h3><p>${text}</p>${action ? `<button class="button primary tiny" data-action="${action}">${button} ${icon('arrow')}</button>` : ''}</div>`;
}
function stats() {
  const s = state.stats;
  return `<div class="stats"><div class="stat"><div class="stat-head">Total settled<span class="stat-icon">${icon('wallet')}</span></div><div class="stat-value">${money(s.settled_minor)}</div><p class="stat-foot"><strong>${s.settled} confirmed</strong> ${s.settled === 1 ? 'payment':'payments'}</p></div><div class="stat"><div class="stat-head">Total payments<span class="stat-icon">${icon('arrows')}</span></div><div class="stat-value">${s.total.toString().padStart(2,'0')}</div><p class="stat-foot">Across this workspace</p></div><div class="stat"><div class="stat-head">Awaiting confirmation<span class="stat-icon">${icon('clock')}</span></div><div class="stat-value">${s.pending.toString().padStart(2,'0')}</div><p class="stat-foot">A clear next step for each</p></div><div class="stat"><div class="stat-head">Refunds to resolve<span class="stat-icon">${icon('undo')}</span></div><div class="stat-value">${s.refunds.toString().padStart(2,'0')}</div><p class="stat-foot">${s.refunds ? 'Ready for simulated refund' : 'Nothing needs attention'}</p></div></div>`;
}
function activity(p) {
  const s = p.customer.state;
  return `<button class="activity-row" data-detail="${esc(p.id)}"><span class="payment-symbol ${s === 'SETTLED' ? 'settled' : s.startsWith('REFUND') ? 'refund':''}">${icon(s.startsWith('REFUND') ? 'undo':'coffee')}</span><span class="activity-main"><strong>${esc(p.note || 'Payment to Studio Coffee')}</strong><small>${short(p.id)} · ${date(p.created)}</small></span><span class="activity-end"><strong>${money(p.amount)}</strong>${status(s)}</span></button>`;
}
function overview() {
  return heading('A good day to be in sync.', 'A little clarity for every payment. All in one place.') + `<section class="hero"><div class="hero-content"><span class="eyebrow">LESS GUESSWORK. MORE CERTAINTY.</span><h2>Payments, with<br>peace of mind.</h2><p>From “Did it go through?” to “All sorted.”<br>See the same story on both sides.</p><a href="#room" class="button">Open payment room ${icon('arrow')}</a></div><div class="hero-art" aria-hidden="true"><div class="orbit"></div><div class="orbit two"></div><span class="art-star">✳</span><span class="art-dot"></span><div class="art-card"><small>CUSTOMER</small><span class="art-avatar">A</span><strong>₹100.00</strong><span class="art-paid">✓ Payment sent</span></div><span class="art-join">${icon('arrows')}</span><div class="art-card"><small>MERCHANT</small><span class="art-avatar">S</span><strong>₹100.00</strong><span class="art-paid">✓ Payment received</span></div></div></section>${stats()}<div class="overview-lower"><section class="panel activity-panel"><div class="section-heading"><div><h2>Recent activity</h2><p>The latest from your payment workspace</p></div><a class="section-link" href="#transactions">View all ${icon('arrow')}</a></div>${state.payments.length ? state.payments.slice(0,4).map(activity).join('') : empty('Your first payment starts here.', 'Create a simulated payment and watch both sides find their way to the same outcome.')}</section><aside class="overview-aside"><div class="aside-card"><span class="eyebrow">BUILT FOR THE WHAT-IFS</span><h3>A retry shouldn’t<br>mean a worry.</h3><p>See whether a payment is already paid, still resolving, or ready for a fresh start.</p><a class="section-link" href="#room">Try the payment journey ${icon('arrow')}</a><span class="star-pattern" aria-hidden="true">✳</span></div><div class="aside-mini">${icon('shield')}<div><strong>Your core, connected.</strong>Two independent replicas. Durable local logs.<br>Built on the revised PaySync engine.</div></div></aside></div>`;
}
function message(s, merchant=false) {
  if (!s) return 'The payment hasn’t reached this device yet. Sync the devices to bring it up to date.';
  return {INITIATED:'Your payment has started. Waiting for the debit confirmation.',DEBITED:merchant ? 'The customer debit is confirmed. Confirm receipt before the timeout to settle.' : 'Your debit is confirmed. Waiting for the merchant to receive it.',CREDITED:'The merchant has received this payment. Settlement is being confirmed.',SETTLED:'All sorted. Both sides have the same payment confirmation.',FAILED:'This simulated payment was declined before debit. You can start a new payment.',REFUND_PENDING:'The debit was not matched with a credit in time. A simulated refund is now available.',REFUNDED:'The simulated refund is complete. This payment is closed.'}[s];
}
function phone(p, side) {
  const merchant = side === 'merchant', crdt = p[side], s = crdt?.state;
  const current = p.customer.state;
  const delay = Math.max(0, Math.ceil(p.created + state.timeout_s - Date.now()/1000));
  const hint = current === 'DEBITED' ? `<span data-countdown="${p.created + state.timeout_s}">${delay}s</span>` : '—';
  return `<div class="device-wrap"><div class="device-label">${icon('phone')} ${merchant ? 'MERCHANT VIEW':'CUSTOMER VIEW'}<span>Independent replica</span></div><section class="phone ${merchant ? 'merchant':''}" aria-label="${side} phone"><div class="phone-top"><span>9:41</span><div class="phone-notch"></div>${icon('signal')}</div><div class="phone-content"><div class="phone-app"><span class="mini-mark">↗</span> paysync<span class="sandbox-pill">TEST</span></div><div class="store-avatar">${merchant ? 'S':'A'}</div><p class="phone-caption">${merchant ? 'Receiving at':'Paying to'}</p><h2 class="phone-merchant">Studio Coffee</h2><div class="phone-amount">${money(p.amount)}</div><div class="phone-status">${status(s)}</div><p class="phone-message">${message(s,merchant)}</p><div class="phone-divider"></div><div class="phone-meta"><span>Payment ID</span><strong>${short(p.id)}</strong></div><div class="phone-meta"><span>${merchant ? 'Last known state':'Payment journey'}</span><strong>${merchant ? esc(s || 'NOT_RECEIVED') : scenarioNames[p.scenario]}</strong></div><div class="phone-meta"><span>${merchant ? 'Device connection':'Timeout check in'}</span><strong>${merchant ? (state.online ? 'Connected' : 'Disconnected') : hint}</strong></div><div class="phone-actions">${merchant ? (current === 'DEBITED' && (!s || s === 'DEBITED' || s === 'INITIATED') ? `<button class="button primary full" data-command="credit" data-id="${esc(p.id)}" ${!state.online ? 'disabled':''}>Confirm simulated receipt ${icon('check')}</button>` : `<div class="subtle-label">${s === 'SETTLED' ? 'Receipt confirmed. Nothing else to do.' : s === 'REFUNDED' ? 'Refund confirmed on this device.' : 'Waiting for the next payment event.'}</div>`) + `<button class="button full" data-command="sync" data-id="${esc(p.id)}" ${!state.online ? 'disabled':''}>Sync devices ${icon('sync')}</button>` : `<button class="button primary full" data-command="retry" data-id="${esc(p.id)}">Check payment & retry ${icon('arrow')}</button>${current === 'REFUND_PENDING' ? `<button class="button full" data-command="refund" data-id="${esc(p.id)}">Simulate refund ${icon('undo')}</button>` : `<button class="button full" data-detail="${esc(p.id)}">View payment details ${icon('receipt')}</button>`}`}</div><div class="phone-safe">${icon('lock')} Simulated money · Local workspace</div></div><div class="phone-home"></div></section></div>`;
}
function journey(p) {
  const s = p.customer.state, hist = p.history.filter(h => h.side === 'customer').map(h => h.state);
  const steps = s === 'FAILED' ? ['INITIATED','FAILED'] : s.startsWith('REFUND') ? ['INITIATED','DEBITED','REFUND_PENDING','REFUNDED'] : ['INITIATED','DEBITED','CREDITED','SETTLED'];
  return `<div class="journey">${steps.map((step,i) => `<div class="journey-step ${hist.includes(step) ? 'done':''} ${s === step ? 'current':''}"><span class="step-dot">${hist.includes(step) ? '✓' : i+1}</span><span class="step-text">${({INITIATED:'Started',DEBITED:'Debited',CREDITED:'Credited',SETTLED:'Settled',FAILED:'Declined',REFUND_PENDING:'Reversing',REFUNDED:'Refunded'})[step]}</span></div>`).join('')}</div>`;
}
function decisionMarkup(p) {
  const d = p.retry.decision;
  const content = {ALREADY_PAID:['Already paid. You’re all set.','This payment has credit confirmation. Another payment would be a separate charge.'],RESOLVING:['Let’s resolve this one first.','Your payment is awaiting confirmation. Check again after the timeout or ask the merchant to confirm receipt.'],SAFE_TO_RETRY_NEW_PAYMENT:['A fresh payment is safe to start.',p.customer.state === 'REFUND_PENDING' ? 'The original debit is awaiting refund. A new payment gets its own ID; the original refund stays in your queue.' : 'The original payment is closed or failed. Your next payment gets a new transaction ID.']}[d];
  return `<div class="decision-box"><strong>${content[0]}</strong><p>${content[1]}</p>${decision === p.id && d === 'SAFE_TO_RETRY_NEW_PAYMENT' ? `<button class="button soft tiny" data-retry-new="${esc(p.id)}">Start a new payment ${icon('arrow')}</button>` : ''}</div>`;
}
function room() {
  const p = state.payments.find(p => p.id === selected) || state.payments[0];
  let html = heading('One payment. Both sides.', 'A shared story, from the customer to the counter.');
  html += `<div class="room-toolbar"><div class="room-selection"><label for="payment-select">Viewing payment</label><select id="payment-select" ${!state.payments.length ? 'disabled':''}>${state.payments.length ? state.payments.map(x => `<option value="${esc(x.id)}" ${p?.id === x.id ? 'selected':''}>${short(x.id)} · ${money(x.amount)}</option>`).join('') : '<option>No payments yet</option>'}</select></div><div class="network-control"><button class="toggle" role="switch" aria-label="Devices connected" aria-checked="${state.online}" data-action="network"></button><span>${state.online ? 'Devices connected':'Devices disconnected'}</span></div></div>`;
  if (!p) return html + `<section class="room-empty">${empty('Two sides. One clear outcome.', 'Create your first payment to see the customer and merchant journey, side by side.')}</section><p class="view-note">Try instant confirmation, a delayed credit, an interrupted sync, or a declined payment. Each journey runs on the revised Python engine.</p>`;
  const matching = p.merchant && JSON.stringify(p.customer) === JSON.stringify(p.merchant);
  html += `<div class="room-grid">${phone(p,'customer')}<div class="room-connector" aria-label="${matching ? 'Replicas in sync':'Replicas awaiting sync'}"><span class="sync-circle ${matching ? '':'offline'}">${icon(matching ? 'arrows':'disconnect')}</span><small>${matching ? 'In sync':'Awaiting sync'}</small></div>${phone(p,'merchant')}</div><div class="room-bottom"><section class="panel"><div class="section-heading"><div><h2>The payment journey</h2><p>${short(p.id)} · ${date(p.created)}</p></div>${icon('arrows')}</div>${journey(p)}<p class="view-note">${p.customer.state === 'FAILED' ? 'Declined before debit. No simulated debit was created.' : 'Each transition is saved to the engine’s write-ahead log.'}</p></section><section class="panel"><div class="section-heading"><div><h2>A clearer next step</h2><p>${decision === p.id ? 'Retry decision checked against the engine' : 'Current guidance from the engine'}</p></div>${icon('shield')}</div>${decisionMarkup(p)}</section></div>`;
  return html;
}
function paymentTable() {
  let payments = state.payments.filter(p => {
    const s = p.customer.state;
    return (filter === 'all' || (filter === 'settled' && s === 'SETTLED') || (filter === 'pending' && ['INITIATED','DEBITED','CREDITED'].includes(s)) || (filter === 'refunds' && s.startsWith('REFUND'))) && `${p.id} ${short(p.id)} ${p.note} ${s} ${labels[s]}`.toLowerCase().includes(search.toLowerCase());
  });
  return `<div class="table-wrap"><table><thead><tr><th>Payment</th><th>Amount</th><th>Status</th><th>Journey</th><th>Created</th><th><span class="muted">Details</span></th></tr></thead><tbody>${payments.length ? payments.map(p => `<tr><td><button class="txn-link" data-detail="${esc(p.id)}">${short(p.id)}</button><span class="cell-note">${esc(p.note || 'Studio Coffee')}</span></td><td class="amount-cell">${money(p.amount)}</td><td>${status(p.customer.state)}</td><td>${scenarioNames[p.scenario]}</td><td>${date(p.created)}</td><td><button class="icon-button" data-open-room="${esc(p.id)}" aria-label="Open ${short(p.id)} in payment room">${icon('arrow')}</button></td></tr>`).join('') : `<tr class="no-results"><td colspan="6">${state.payments.length ? 'No payments match this view. Try a different search or filter.' : 'Your payment history will appear here.'}</td></tr>`}</tbody></table></div><div class="table-foot"><span>${payments.length} of ${state.payments.length} payments</span><a class="section-link export-csv" href="/api/export">${icon('download')} Export CSV</a></div>`;
}
function transactions() {
  return heading('Every payment, in perspective.', 'A complete history, with the details that matter.') + `<div class="filters"><div class="tabs" role="group" aria-label="Filter transactions">${[['all','All payments'],['settled','Settled'],['pending','Pending'],['refunds','Refunds']].map(([v,l]) => `<button aria-pressed="${filter === v}" class="${filter === v ? 'active':''}" data-filter="${v}">${l}</button>`).join('')}</div><label class="search-wrap">${icon('search')}<input id="search" type="search" placeholder="Search payment ID or note…" aria-label="Search payment ID or note" value="${esc(search)}"></label></div><section id="transaction-results" aria-live="polite">${paymentTable()}</section>`;
}
function refunds() {
  const pendingRefunds = state.payments.filter(p => p.customer.state === 'REFUND_PENDING'), refunded = state.payments.filter(p => p.customer.state === 'REFUNDED');
  return heading('A fresh start. A clear return.', 'Keep every reversal in view, right through to the refund.') + `<div class="refund-intro"><div><h2>A pending payment shouldn’t<br>be a permanent question.</h2><p>When a debit times out without a credit, it moves here. Simulate the return, then watch the confirmation reach both sides.</p></div><span class="refund-art" aria-hidden="true">↶</span></div><section><div class="section-heading"><h2>Awaiting refund <span class="muted">(${pendingRefunds.length})</span></h2><span class="subtle-label">Simulated refunds only</span></div><div class="refund-list">${pendingRefunds.length ? pendingRefunds.map(p => `<article class="panel refund-row"><div class="refund-info"><span class="payment-symbol refund">${icon('undo')}</span><div><h3>${esc(p.note || 'Payment to Studio Coffee')}</h3><p>${short(p.id)} · ${date(p.created)}<br>Original debit awaiting reversal confirmation</p></div></div><div class="refund-actions"><span class="refund-value">${money(p.amount)}</span><button class="button primary" data-command="refund" data-id="${esc(p.id)}">Simulate refund ${icon('undo')}</button></div></article>`).join('') : `<div class="panel refund-empty">${empty('Nothing left hanging.', 'There are no payments awaiting a refund. If a delayed credit times out, its reversal will appear here.',null)}</div>`}</div></section>${refunded.length ? `<section class="panel"><div class="section-heading"><h2>Completed refunds</h2><span class="subtle-label">${money(state.stats.refunded_minor)} returned in simulation</span></div>${refunded.map(activity).join('')}</section>` : ''}<p class="view-note">A refund action here records a simulated refund proof in the engine. It does not call a bank or transfer real funds.</p>`;
}
function system() {
  return heading('A little transparency goes a long way.', 'What is running, what is saved, and where the boundaries are.',false) + `<div class="system-banner">${icon('shield')}<div><h2>A revised core. Tested.</h2><p>The paysync/ package carries six reviewed fixes (see docs/CORE_CHANGES.md), each with a regression test. This workspace adds a local API and live-updating interface around it.</p></div></div><div class="system-grid"><section class="panel"><h3>Workspace health</h3><div class="system-row"><span>Mode</span><strong>Local sandbox</strong></div><div class="system-row"><span>Replica connection</span><strong>${state.online ? 'Connected':'Disconnected'}</strong></div><div class="system-row"><span>Reconciliation interval</span><strong>Every 1 second</strong></div><div class="system-row"><span>Pending timeout</span><strong>${state.timeout_s} seconds</strong></div><div class="system-row"><span>Money representation</span><strong>Integer paise · INR</strong></div><div class="system-row"><span>Write status</span><strong>${state.fault ? 'Paused — check log':'Available'}</strong></div><button class="button tiny" data-action="sweep">Run timeout check ${icon('sync')}</button></section><section class="panel"><h3>Made for the full journey</h3><p class="system-text">Create a payment, compare the two devices, disconnect their sync, check a retry, or complete a simulated refund.</p><p class="system-text">The engine stores transitions in separate customer and merchant write-ahead logs. Payment notes and request IDs are kept in a local SQLite workspace. Refreshing the page preserves that history.</p><button class="button tiny" data-action="guide">Read the workspace guide ${icon('arrow')}</button></section><section class="panel wide-panel"><h3>Reliability agent</h3><p class="system-text">Reads the payment log, explains what is stuck and proposes one safe action. Nothing runs until you approve it, and every approval is logged.</p>${agentRows()}</section><section class="panel wide-panel"><h3>Before real payments</h3><p class="system-text">This build simulates provider responses. Real debit, credit and refund integrations, identity and access controls, verified webhooks, deployment infrastructure and an independent review of the engine are required before handling money. See the included production assessment for reproduced engine limitations.</p><div class="code-line">Start: python server.py &nbsp; · &nbsp; Verify: python run_tests.py</div></section></div>`;
}
function render() {
  $('#breadcrumb').textContent = titles[view];
  document.title = `${titles[view]} · PaySync`;
  document.querySelectorAll('[data-view]').forEach(a => { a.classList.toggle('active', a.dataset.view === view); if (a.dataset.view === view) a.setAttribute('aria-current','page'); else a.removeAttribute('aria-current'); });
  if (!state) return;
  const searchFocus = document.activeElement?.id === 'search';
  const position = searchFocus ? $('#search').selectionStart : null;
  $('#main').innerHTML = ({overview,room,transactions,refunds,system})[view]();
  $('#refund-badge').textContent = state.stats.refunds;
  $('#refund-badge').classList.toggle('hidden', !state.stats.refunds);
  if (busy) $('#main').querySelectorAll('button[data-command],button[data-action="network"]').forEach(b => b.disabled = true);
  if (searchFocus && $('#search')) { $('#search').focus({preventScroll:true}); $('#search').setSelectionRange(position,position); }
  if (receiptId && $('#detail-dialog').open) renderReceipt(receiptId);
}
function showPayment(amount=10000) {
  const form = $('#payment-form');
  form.reset(); $('#amount').value = (amount / 100).toFixed(2);
  $('#form-error').classList.add('hidden');
  if (pending) {
    $('#amount').value = (pending.amount_minor/100).toFixed(2); $('#scenario').value = pending.scenario; $('#note').value = pending.note;
    $('#form-error').textContent = 'A previous request is unconfirmed. Submitting again uses that same request ID and amount to avoid creating a duplicate.';
    $('#form-error').classList.remove('hidden');
  }
  updateScenario();
  $('#payment-dialog').showModal(); $('#amount').focus(); $('#amount').select();
}
function updateScenario() {
  const hints = {instant:'Both sides confirm and settle together.',delayed:`Confirm from the merchant before ${state?.timeout_s || 20}s, or let the debit move to refund pending.`,offline:'Starts with devices disconnected. Reconnect and confirm receipt before the timeout.',declined:'A simulated provider decline happens before any debit. Check the safe retry decision.'};
  $('#scenario-hint').textContent = hints[$('#scenario').value] + (state && !state.online && $('#scenario').value !== 'offline' ? ' Devices are currently disconnected; reconnect to sync this payment.' : '');
}
function renderReceipt(id) {
  const p = state.payments.find(p => p.id === id); if (!p) return;
  $('#detail-content').innerHTML = `<div class="dialog-top"><span class="eyebrow">THE PAYMENT DETAILS</span><button class="icon-button" data-action="close-detail" aria-label="Close payment details">${icon('close')}</button></div><h2 id="detail-title">A clear record.</h2><div class="receipt-amount">${money(p.amount)}</div><div class="receipt-status">${status(p.customer.state)}</div><p class="receipt-note">${esc(p.note || 'Payment to Studio Coffee')}</p><div class="receipt-rows"><div class="receipt-row"><span>Transaction ID</span><strong>${esc(p.id)}</strong></div><div class="receipt-row"><span>Created</span><strong>${date(p.created)}</strong></div><div class="receipt-row"><span>Journey</span><strong>${scenarioNames[p.scenario]}</strong></div><div class="receipt-row"><span>Customer state</span><strong>${esc(p.customer.state)}</strong></div><div class="receipt-row"><span>Merchant state</span><strong>${esc(p.merchant?.state || 'NOT_RECEIVED')}</strong></div><div class="receipt-row"><span>Retry decision</span><strong>${esc(p.retry.decision)}</strong></div><div class="receipt-row"><span>Customer vector clock</span><strong class="clock-code">${esc(JSON.stringify(p.customer.clock))}</strong></div><div class="receipt-row"><span>Merchant vector clock</span><strong class="clock-code">${esc(JSON.stringify(p.merchant?.clock || {}))}</strong></div></div><section class="receipt-history"><h3>Engine journal <span class="muted">(${p.history.length} records)</span></h3><p class="subtle-label">Grouped by device; ordering is local to each journal.</p><div class="history-scroll">${p.history.map(h => `<div class="history-item"><span>${esc(h.side)} · ${esc(h.event)}</span><strong>${esc(h.state)}</strong></div>`).join('')}</div></section><div class="detail-actions"><button class="button primary" data-open-room="${esc(p.id)}">Open in payment room ${icon('arrow')}</button><button class="button" data-copy-id="${esc(p.id)}">Copy ID</button></div>`;
}
async function command(id, cmd) {
  if (busy) return;
  busy = true; render();
  try {
    const result = await api(`/api/payments/${encodeURIComponent(id)}/${cmd}`, {});
    if (cmd === 'retry') {
      decision = id; selected = id;
      if (result.decision === 'ALREADY_PAID') toast('Already paid. This payment has credit confirmation.');
      else if (result.decision === 'RESOLVING') toast('Still resolving. Wait for the timeout or ask the merchant to confirm.');
      else toast('A new payment is safe to start. The original keeps its own refund status.');
    } else toast(({credit:'Simulated receipt confirmed. Both devices are now settled.',refund:'Simulated refund recorded. The original payment is now closed.',sync:'Devices synced with the latest exported engine state.'})[cmd]);
    await refresh(true);
  } catch (error) { toast(error.message,true); await refresh(); }
  finally { busy = false; render(); flushStale(); }
}

document.addEventListener('click', async event => {
  const btn = event.target.closest('button,a'); if (!btn) return;
  if (btn.dataset.agentApprove) {
    if (busy) return; busy = true; render();
    try { const r = await api('/api/agent/approve', {proposal_id: btn.dataset.agentApprove}); toast(`Approved and executed: ${r.action}.`); await loadAgent(); await refresh(true); }
    catch (error) { toast(error.message, true); await loadAgent(); await refresh(true); }
    finally { busy = false; render(); flushStale(); }
    return;
  }
  if (btn.dataset.amount) { $('#amount').value = Number(btn.dataset.amount).toFixed(2); return; }
  if (btn.dataset.filter) { filter = btn.dataset.filter; render(); return; }
  if (btn.dataset.command) { await command(btn.dataset.id,btn.dataset.command); return; }
  if (btn.dataset.detail) { receiptId = btn.dataset.detail; renderReceipt(receiptId); $('#detail-dialog').showModal(); return; }
  if (btn.dataset.openRoom) { selected = btn.dataset.openRoom; $('#detail-dialog').close(); receiptId = null; location.hash = 'room'; navigate('room',true); return; }
  if (btn.dataset.retryNew) { const p=state.payments.find(p => p.id === btn.dataset.retryNew); if (p) showPayment(p.amount); return; }
  if (btn.dataset.copyId) {
    try { await navigator.clipboard.writeText(btn.dataset.copyId); toast('Transaction ID copied.'); } catch { toast('Clipboard is unavailable. Select the transaction ID in the receipt to copy it.',true); }
    return;
  }
  const action = btn.dataset.action;
  if (action === 'new') showPayment();
  if (action === 'close-dialog') $('#payment-dialog').close();
  if (action === 'close-detail') { $('#detail-dialog').close(); receiptId=null; }
  if (action === 'guide') $('#guide-dialog').showModal();
  if (action === 'close-guide') $('#guide-dialog').close();
  if (action === 'guide-start') { $('#guide-dialog').close(); location.hash='room'; navigate('room',true); }
  if (action === 'guide-system') { $('#guide-dialog').close(); location.hash='system'; navigate('system',true); }
  if (action === 'reconnect') { try { csrf=(await api('/api/session')).csrf_token; await refresh(true); } catch { toast('The server is still unavailable. Start python server.py and reconnect.',true); } }
  if (action === 'network' && !busy) {
    busy=true; render();
    try { await api('/api/network',{online:!state.online}); await refresh(true); toast(state.online ? 'Devices reconnected. Their payment states have synced.':'Device sync paused. Payments remain saved on the customer.'); }
    catch(error) { toast(error.message,true); }
    finally { busy=false; render(); flushStale(); }
  }
  if (action === 'sweep') {
    try { const result=await api('/api/reconcile',{}); await refresh(true); toast(result.actions.length ? `${result.actions.length} payment outcome(s) updated.` : 'Timeout check complete. No new reversals are due.'); }
    catch(error) { toast(error.message,true); }
  }
});
document.addEventListener('input', event => { if(event.target.id === 'search') { search=event.target.value; $('#transaction-results').innerHTML=paymentTable(); } });
document.addEventListener('change', event => { if(event.target.id === 'scenario') updateScenario(); if(event.target.id === 'payment-select') {selected=event.target.value;decision=null;render();} });
$('#payment-form').addEventListener('submit',async event => {
  event.preventDefault(); if(busy) return;
  const value=$('#amount').value.trim();
  if(!/^\d{1,6}(\.\d{1,2})?$/.test(value)) { $('#form-error').textContent='Enter a valid amount with up to two decimal places, such as 100.50.'; $('#form-error').classList.remove('hidden'); return; }
  const [whole,fraction='']=value.split('.');
  const amount=Number(whole)*100+Number(fraction.padEnd(2,'0'));
  if(amount<1 || amount>10000000) { $('#form-error').textContent='Choose an amount between ₹0.01 and ₹1,00,000.00.'; $('#form-error').classList.remove('hidden');return; }
  const body={txn_id:pending?.txn_id || crypto.randomUUID(),amount_minor:amount,scenario:$('#scenario').value,note:$('#note').value.trim()};
  if(pending && JSON.stringify({...body,txn_id:pending.txn_id}) !== JSON.stringify(pending)) { $('#form-error').textContent='Keep the original amount, note and journey while retrying the unconfirmed request. Refresh the workspace to check its result.'; $('#form-error').classList.remove('hidden'); return; }
  savePending(body); busy=true; const submit=$('#submit-payment'); submit.disabled=true; submit.textContent='Confirming request…';
  try {
    const result=await api('/api/payments',body); selected=result.txn_id; savePending(null);
    $('#payment-dialog').close(); location.hash='room'; view='room'; decision=null;
    await refresh(true); toast(result.replayed ? 'Original payment restored. No new payment was created.':'Payment created. Follow both sides in the payment room.');
  } catch(error) {
    if (error.status && error.status < 500) savePending(null);
    $('#form-error').textContent=`${error.message} If the response was lost, submit this same request again to check its original outcome.`; $('#form-error').classList.remove('hidden');
  } finally { busy=false;submit.disabled=false; submit.innerHTML=`Create payment ${icon('arrow')}`;render();flushStale(); }
});
window.addEventListener('hashchange',()=>{ if(location.hash === '#main') { $('#main').focus(); return; } navigate(location.hash.slice(1),true); });
$('#detail-dialog').addEventListener('close',()=>{receiptId=null;});
$('#payment-dialog').addEventListener('close',()=>{ if (pending && state?.payments.some(p => p.id === pending.txn_id)) savePending(null); });
window.addEventListener('keydown',event=>{ if(event.key === '/' && !['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName) && !document.querySelector('dialog[open]')) {event.preventDefault();location.hash='transactions';navigate('transactions');$('#search')?.focus();} });
hydrate();
view = titles[location.hash.slice(1)] ? location.hash.slice(1) : 'overview';
let staleWhileBusy = false;
function flushStale() { if (staleWhileBusy && !busy) { staleWhileBusy = false; refresh(); } }
function syncNow() { if (busy) staleWhileBusy = true; else refresh(); }
function connectEvents() {
  // Server-sent events: the server pushes a version bump only when something changed.
  // No steady polling. Without EventSource, fall back to a slow poll.
  if (!('EventSource' in window)) { setInterval(() => { if (!document.hidden && !busy) refresh(); }, 5000); return; }
  let version = null;
  const source = new EventSource('/api/events');
  source.addEventListener('state', event => { if (event.data !== version) { version = event.data; syncNow(); } });
  source.onopen = () => refresh();      // (re)connected: resync once
  source.onerror = () => refresh();     // lost: surfaces the banner; the browser reconnects itself
}
(async()=>{ try {csrf=(await api('/api/session')).csrf_token;} catch {} await refresh(true); connectEvents(); })();
setInterval(()=>{document.querySelectorAll('[data-countdown]').forEach(el=>{ const seconds=Math.max(0,Math.ceil(Number(el.dataset.countdown)-Date.now()/1000));el.textContent=seconds ? `${seconds}s`:'Checking…'; });},1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh(true);});
