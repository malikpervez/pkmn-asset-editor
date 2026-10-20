// Find & replace across all the game's text, for every page: a "🔎 Find text" button in the header.
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
  const btn = document.createElement('button');
  btn.id = 'find-open'; btn.textContent = '🔎 Find text';
  btn.title = 'find words in all dialogue, place names and trainer names, and replace them everywhere';
  const ref = header.querySelector('#hist-save, #save, #btn-build');
  (ref ? ref.parentNode : header).insertBefore(btn, ref || null);
  if (location.pathname === '/' || location.pathname === '/index.html') {  // the graphics page also opens ROM projects
    call('/api/summary').then(s => { if (s.mode !== 'decomp') btn.remove(); }).catch(() => btn.remove());
  }
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;align-items:center;justify-content:center;z-index:50';
  box.innerHTML = `<div style="background:var(--panel);color:var(--text);border-radius:12px;padding:14px 16px;width:min(900px,96vw);max-height:92vh;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center"><b style="font-size:15px">🔎 Find &amp; replace text</b><button id="find-close">Close</button></div>
    <div style="display:grid;grid-template-columns:110px 1fr;gap:6px;align-items:center;margin-top:10px">
      <label>find</label><input id="find-q" placeholder="e.g. Petalburg, or a character's name">
      <label>replace with</label><input id="find-r" placeholder="(optional)">
    </div>
    <div style="display:flex;gap:14px;flex-wrap:wrap;margin:8px 0;font-size:12px">
      <label><input type="checkbox" id="find-case"> match upper/lower case</label>
      <label><input type="checkbox" id="find-word" checked> whole words only</label>
      <label title="the names on the map pop-up and region map"><input type="checkbox" id="find-places" checked> place names</label>
      <label><input type="checkbox" id="find-trainers" checked> trainer names</label>
    </div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <button id="find-go">Find</button><button id="find-preview" title="see every change before anything is written">Preview replace</button>
      <button id="find-apply" class="primary" disabled>Replace in selected</button><span id="find-status" class="muted"></span>
    </div>
    <p class="muted" style="font-size:12px;margin:6px 0">UPPERCASE stays UPPERCASE ("PETALBURG" → "SUNNYVALE"). Text that no longer fits its box is re-wrapped to fit.</p>
    <div id="find-body"></div></div>`;
  document.body.appendChild(box);
  const $ = (id) => document.getElementById(id);
  let results = [], pending = null;  // pending: the selection a preview was made for
  const opts = () => ({ match_case: $('find-case').checked, whole_word: $('find-word').checked,
                        places: $('find-places').checked, trainers: $('find-trainers').checked });
  function mark(text, q) {
    const words = q.trim().split(/\s+/).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!words[0]) return esc(text);
    const rx = new RegExp('(' + words.join('\\s+') + ')', $('find-case').checked ? 'g' : 'gi');
    return esc(text).replace(rx, '<mark>$1</mark>');
  }
  const where = (r) => r.map ? `<a href="/maps?map=${encodeURIComponent(r.map)}&label=${encodeURIComponent(r.label)}">${esc(r.map)}</a>`
    : r.kind === 'trainer name' ? '<a href="/data?tab=trainers">trainers</a>' : esc(r.file);
  async function find() {
    const q = $('find-q').value.trim(); if (!q) return;
    pending = null;
    $('find-status').textContent = 'Looking…';
    try {
      const o = opts();
      const r = await call(`/api/text/search?q=${encodeURIComponent(q)}&case=${o.match_case ? 1 : 0}&word=${o.whole_word ? 1 : 0}&places=${o.places ? 1 : 0}&trainers=${o.trainers ? 1 : 0}`);
      results = r.results;
      $('find-status').textContent = `${r.total} match(es) in ${r.results.length} text(s)${r.more ? ` (and ${r.more} more)` : ''}`;
      $('find-body').innerHTML = r.results.map((x, i) => `<div style="border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin:6px 0">
        <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" data-i="${i}" checked>
        <b>${esc(x.kind === 'dialogue' ? x.label : x.kind)}</b><span class="muted" style="font-size:12px">${where(x)}</span></label>
        <div style="white-space:pre-wrap;font-size:12px;margin-top:4px">${mark(x.text, q)}</div></div>`).join('') || '<p class="muted">Not found.</p>';
      $('find-apply').disabled = !results.length || !$('find-r').value.trim();
    } catch (e) { $('find-status').textContent = e.message; }
  }
  const selected = () => [...document.querySelectorAll('#find-body input[data-i]:checked')].map(c => results[+c.dataset.i].id);
  async function replace(dry) {
    const q = $('find-q').value.trim(), r = $('find-r').value;
    if (!q || !r.trim()) { $('find-status').textContent = 'Type what to find and what to replace it with.'; return; }
    if (!results.length && !pending) await find();
    const boxes = document.querySelectorAll('#find-body input[data-i]');
    const only = boxes.length ? selected() : (pending || []);
    if (!only.length) { $('find-status').textContent = 'Nothing selected.'; return; }
    if (!dry && !confirm(`Replace "${q}" with "${r}" in ${only.length} text(s)?`)) return;
    try {
      const d = await call('/api/text/replace', { query: q, replacement: r, only, dry_run: dry, ...opts() });
      const prob = d.changes.filter(c => c.problems.length).length;
      $('find-status').textContent = dry ? `${d.changes.length} text(s) would change${prob ? `, ${prob} with a problem` : ''}. Nothing is written yet.`
        : `Changed ${d.changes.length} text(s) in ${d.files.length} file(s). Make a 💾 save point if you're happy with it.`;
      $('find-body').innerHTML = d.changes.map(c => `<div style="border:1px solid ${c.problems.length ? 'var(--accent)' : 'var(--line)'};border-radius:8px;padding:6px 10px;margin:6px 0">
        <b>${esc(c.kind === 'dialogue' ? c.label : c.kind)}</b> <span class="muted" style="font-size:12px">${esc(c.file)}${c.rewrapped ? ' · re-wrapped to fit' : ''}</span>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:12px;margin-top:4px">
          <div style="white-space:pre-wrap;opacity:.7">${esc(c.before)}</div><div style="white-space:pre-wrap">${esc(c.after)}</div></div>
        ${c.problems.map(p => `<div style="color:var(--accent);font-size:12px">⚠ ${esc(p)}</div>`).join('')}</div>`).join('');
      pending = dry ? only : null;
      if (!dry) results = [];
      $('find-apply').disabled = !dry;
    } catch (e) { $('find-status').textContent = e.message; }
  }
  $('find-go').onclick = find;
  $('find-q').onkeydown = (e) => { if (e.key === 'Enter') find(); };
  $('find-r').oninput = () => { $('find-apply').disabled = !results.length || !$('find-r').value.trim(); };
  $('find-preview').onclick = () => replace(true);
  $('find-apply').onclick = () => replace(false);
  btn.onclick = () => { box.style.display = 'flex'; $('find-q').focus(); };
  $('find-close').onclick = () => { box.style.display = 'none'; };
})();
