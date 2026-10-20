// Guided missions (see missions.py): a "🎯 Missions" button in the header and a panel with the next small task.
// Each mission ticks itself off when the tool sees it done; a note pops up when one is finished.
(function () {
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const bold = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  const call = async (path, body) => {
    const r = await fetch(path, body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'failed');
    return d;
  };
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
  const header = document.querySelector('header');
  if (!header) return;
  const btn = document.createElement('button');
  btn.id = 'mis-open';
  btn.title = 'small tasks that show you how to make your game, one at a time';
  btn.textContent = '🎯 Missions';
  const spacer = [...header.children].find(el => el.tagName === 'SPAN' && el.style.flex === '1');
  header.insertBefore(btn, spacer || null);

  const css = document.createElement('style');
  css.textContent = `
    #mis-panel { position: fixed; top: 0; right: 0; bottom: 0; width: min(360px, 100vw); z-index: 45; background: var(--panel); color: var(--text);
      border-left: 1px solid var(--line); box-shadow: -8px 0 24px rgba(0,0,0,.18); display: none; flex-direction: column; font: 13px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; }
    #mis-panel.open { display: flex; }
    #mis-panel .mis-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
    #mis-panel .mis-body { overflow: auto; padding: 10px 14px 16px; flex: 1; }
    #mis-panel .mis-bar { height: 6px; border-radius: 3px; background: var(--chip, #ececf1); overflow: hidden; margin: 2px 0 12px; }
    #mis-panel .mis-bar i { display: block; height: 100%; background: var(--ok, #1f8a4c); transition: width .4s; }
    #mis-panel .mis-now { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin-bottom: 12px; }
    #mis-panel .mis-now h3 { margin: 0 0 4px; font-size: 15px; }
    #mis-panel .mis-now ol { padding-left: 20px; margin: 8px 0; }
    #mis-panel .mis-now li { margin: 4px 0; }
    #mis-panel .mis-row { display: flex; gap: 8px; align-items: center; padding: 5px 0; border-top: 1px solid var(--line); cursor: pointer; }
    #mis-panel .mis-row.done { color: var(--muted); }
    #mis-panel .mis-muted { color: var(--muted); }
    #mis-panel a.mis-go { display: inline-block; padding: 6px 12px; border-radius: 7px; background: var(--accent); color: var(--accent-text, #fff); text-decoration: none; }
    #mis-panel .mis-acts { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
    #mis-panel .mis-wait { font-size: 12px; color: var(--muted); }
    .mis-toast { position: fixed; bottom: 16px; right: 16px; z-index: 70; padding: 10px 14px; border-radius: 10px; background: var(--panel); color: var(--text);
      border: 2px solid var(--ok, #1f8a4c); max-width: 360px; box-shadow: 0 8px 24px rgba(0,0,0,.2); font: 13px/1.45 system-ui, sans-serif; }
    /* on a wide screen the panel sits beside the page instead of covering it */
    @media (min-width: 900px) {
      html.mis-docked body { margin-right: 360px; }
      html.mis-docked #modal, html.mis-docked .modal, html.mis-docked .modalish, html.mis-docked #playbox,
      html.mis-docked #hist-box { right: 360px !important; }
      html.mis-docked .mis-toast { right: 376px; }
      #mis-panel { box-shadow: none; }
    }`;
  document.head.appendChild(css);
  const panel = document.createElement('div');
  panel.id = 'mis-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Missions');
  panel.innerHTML = `<div class="mis-head"><b style="font-size:15px;flex:1">🎯 Missions</b><button id="mis-close">Close</button></div><div class="mis-body" id="mis-body">Loading…</div>`;
  document.body.appendChild(panel);

  let data = null, openId = null, timer = null;
  function toast(html) {
    const t = document.createElement('div');
    t.className = 'mis-toast';
    t.innerHTML = html;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 7000);
  }
  function render() {
    const body = document.getElementById('mis-body');
    if (!data) return;
    const pct = Math.round(100 * data.done / data.total);
    const show = data.missions.find(m => m.id === (openId || data.current));
    let h = `<div class="mis-muted">${data.done} of ${data.total} done</div><div class="mis-bar"><i style="width:${pct}%"></i></div>`;
    if (show) {
      h += `<div class="mis-now"><h3>${show.done ? '✅ ' : ''}${esc(show.title)}</h3><div class="mis-muted">${esc(show.why)}</div>
        <ol>${show.steps.map(s => `<li>${bold(s)}</li>`).join('')}</ol>
        <div class="mis-acts">${show.link ? `<a class="mis-go" href="${esc(show.link.href)}">${esc(show.link.label)} →</a>` : ''}
          <a href="${esc(show.help)}" target="_blank">More help</a>
          ${show.done ? '' : `<button data-skip="${esc(show.id)}">${show.skipped ? 'Don\'t skip' : 'Skip'}</button>`}</div>
        ${show.done ? '' : '<div class="mis-wait" style="margin-top:8px">⏳ This ticks itself off when it\'s done.</div>'}</div>`;
    } else {
      h += `<div class="mis-now"><h3>🏆 All done!</h3><div class="mis-muted">You know the basics now. The <a href="/help" target="_blank">guides</a> show much more.</div></div>`;
    }
    if (data.git === false) h += `<p class="mis-muted">Tip: missions notice your changes best when your game folder uses git (see “Save points” in Help).</p>`;
    h += data.missions.map(m => `<div class="mis-row ${m.done ? 'done' : ''}" data-id="${esc(m.id)}">${m.done ? '✅' : m.skipped ? '⏭' : m.id === data.current ? '👉' : '⬜'} <span style="flex:1">${esc(m.title)}</span></div>`).join('');
    h += `<p style="margin-top:12px"><button id="mis-reset" title="start all missions again from the beginning">Start again</button></p>`;
    body.innerHTML = h;
  }
  async function refresh() {
    try {
      const d = await call('/api/missions');
      const fresh = d.just_done || [];
      data = d;
      btn.textContent = `🎯 Missions ${d.done}/${d.total}`;
      if (fresh.length && store('pae-missions-seen') !== null) {
        const m = d.missions.find(x => x.id === fresh[fresh.length - 1]);
        const next = d.missions.find(x => x.id === d.current);
        toast(`🎉 <b>Mission done:</b> ${esc(m.title)}${next ? `<br><span style="color:var(--muted)">Next: ${esc(next.title)}</span>` : '<br>That was the last one!'}`);
        if (openId === m.id) openId = null;
      }
      store('pae-missions-seen', '1');
      render();
    } catch (e) { btn.hidden = true; }
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(async () => { if (!document.hidden) await refresh(); schedule(); }, panel.classList.contains('open') ? 4000 : 20000);
  }
  const wide = () => window.matchMedia('(min-width: 900px)').matches;
  function setOpen(on) {
    panel.classList.toggle('open', on);
    document.documentElement.classList.toggle('mis-docked', on);
    const t = document.getElementById('toast');  // a page's own notes in the bottom-right corner move over too
    if (t && getComputedStyle(t).transform === 'none') t.style.right = on && wide() ? '372px' : '';
    window.dispatchEvent(new Event('resize'));  // pages that size canvases to the window
    store('pae-missions-open', on ? '1' : '0');
    if (on) refresh();
    schedule();
  }
  btn.onclick = () => setOpen(!panel.classList.contains('open'));
  document.getElementById('mis-close').onclick = () => setOpen(false);
  panel.addEventListener('click', async (ev) => {
    const row = ev.target.closest('.mis-row');
    if (row) { openId = row.dataset.id === data.current ? null : row.dataset.id; render(); return; }
    const skip = ev.target.closest('[data-skip]');
    if (skip) {
      const m = data.missions.find(x => x.id === skip.dataset.skip);
      data = await call('/api/missions/skip', { id: m.id, skip: !m.skipped }); openId = null; render(); return;
    }
    if (ev.target.id === 'mis-reset' && confirm('Start all missions again? (Your game is not changed.)')) {
      data = await call('/api/missions/reset', {}); openId = null; render();
    }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && panel.classList.contains('open')) setOpen(false); });
  refresh();
  // open by itself the first time, so beginners see where to start; after that it remembers
  const was = store('pae-missions-open');
  setOpen(was === null ? !store('pae-missions-seen') && wide() : was === '1');
})();
