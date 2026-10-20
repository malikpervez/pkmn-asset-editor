// Save points (git commits) for every page: a "💾 Save point" and a "🕘 History" button in the header.
(function () {
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const call = async (path, body) => {
    const r = await fetch(path, body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'failed');
    return d;
  };
  const header = document.querySelector('header');
  if (!header) return;
  const wrap = document.createElement('span');
  wrap.innerHTML = `<button id="hist-save" title="remember everything as it is now, so you can always come back to it">💾 Save point</button>
    <button id="hist-open" title="your save points: see what changed, or go back to one">🕘 History</button>`;
  wrap.style.display = 'contents';
  const anchor = header.querySelector('#save, #btn-build');
  header.insertBefore(wrap, anchor || null);
  const box = document.createElement('div');
  box.id = 'hist-box';
  box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;align-items:center;justify-content:center;z-index:50';
  box.innerHTML = `<div style="background:var(--panel);color:var(--text);border-radius:12px;padding:14px 16px;width:min(720px,96vw);max-height:90vh;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center"><b style="font-size:15px">🕘 Save points</b><button id="hist-close">Close</button></div>
    <div id="hist-body" style="margin-top:8px">Loading…</div></div>`;
  document.body.appendChild(box);
  const note = (t, ok = true) => {
    const m = document.createElement('div');
    m.textContent = t;
    m.style.cssText = `position:fixed;bottom:12px;left:12px;z-index:60;padding:8px 12px;border-radius:8px;background:var(--panel);border:1px solid ${ok ? 'var(--ok)' : 'var(--accent)'};max-width:420px`;
    document.body.appendChild(m); setTimeout(() => m.remove(), 5000);
  };
  async function render() {
    const body = document.getElementById('hist-body');
    try {
      const r = await call('/api/history');
      if (!r.available) { body.innerHTML = '<p>Save points need your game folder to use git. Run <code>git init</code> in it once (see ❓ Help).</p>'; return; }
      let h = `<p class="muted" style="margin:0 0 8px">${r.unsaved.length ? `${r.unsaved.length} file(s) changed since your last save point.` : 'Everything is saved in a save point.'}
        Going back to an older save point never deletes anything: it becomes a new save point, so you can come forward again.</p>`;
      h += r.points.map((p, i) => `<div style="border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin:6px 0">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="flex:1">${esc(p.message)}</b>
        <span class="muted">${esc(p.date)} · ${p.files} file(s)</span>
        <button data-show="${p.sha}">What changed</button>${i ? `<button data-back="${p.sha}" title="put every file back the way it was here">Go back to this</button>` : '<span class="muted">(latest)</span>'}</div>
        <div data-files="${p.sha}"></div></div>`).join('') || '<p class="muted">No save points yet.</p>';
      body.innerHTML = h;
      body.querySelectorAll('[data-show]').forEach(b => b.onclick = async () => {
        const d = await call('/api/history/show?sha=' + b.dataset.show);
        body.querySelector(`[data-files="${b.dataset.show}"]`).innerHTML = `<div class="muted" style="font-size:12px">${esc(d.kinds.join(', '))}</div>` +
          d.files.slice(0, 80).map(f => `<div style="font-size:12px"><code>${esc(f.state)}</code> ${esc(f.file)}</div>`).join('');
      });
      body.querySelectorAll('[data-back]').forEach(b => b.onclick = async () => {
        if (!confirm('Put every file back the way it was at this save point? (Your current work is kept in a save point first.)')) return;
        try { await call('/api/history/back', { sha: b.dataset.back }); note('Went back. Reloading…'); setTimeout(() => location.reload(), 900); }
        catch (e) { note(e.message, false); }
      });
    } catch (e) { body.textContent = e.message; }
  }
  document.getElementById('hist-open').onclick = () => { box.style.display = 'flex'; render(); };
  document.getElementById('hist-close').onclick = () => { box.style.display = 'none'; };
  document.getElementById('hist-save').onclick = async () => {
    const dirtyBtn = document.querySelector('#save:not([disabled])');
    if (dirtyBtn && !confirm('You have changes that are not saved yet. Make the save point anyway? (Press Save first to include them.)')) return;
    const message = prompt('Name this save point (what did you change?)', '');
    if (message === null) return;
    try { const p = await call('/api/history/save', { message }); note(`Save point made: ${p.message}`); }
    catch (e) { note(e.message, false); }
  };
})();
