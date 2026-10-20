// The block builder: make what happens in an event from blocks ("Say…", "Give an item", "If…") instead of a script.
// openBlockBuilder({ map, label, kind: 'npc'|'sign'|'trigger', localIds: [...], onSaved(label) })
(function () {
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const call = async (path, body) => {
    const r = await fetch(path, body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'failed');
    return d;
  };
  // what each block is called, what it needs, and the fields it shows
  const KINDS = {
    say: { label: '💬 Say something', make: () => ({ text: '' }) },
    ask: { label: '❓ Ask yes or no', make: () => ({ text: '', yes: [], no: [] }), branches: [['yes', 'If they say YES'], ['no', 'If they say NO']] },
    give_item: { label: '🎁 Give an item', make: () => ({ item: 'ITEM_POTION', amount: 1 }) },
    give_mon: { label: '🔴 Give a Pokémon', make: () => ({ species: 'SPECIES_PICHU', level: 5 }) },
    trainer: { label: '⚔️ Battle (this trainer)', make: () => ({ trainer: '', intro: 'Let\'s battle!', defeat: 'You win!' }), hint: 'Put this first. What comes after it is said after the battle.' },
    wild: { label: '🌿 Battle a wild Pokémon', make: () => ({ species: 'SPECIES_ZIGZAGOON', level: 5 }) },
    flag: { label: '🚩 Turn a flag on/off', make: () => ({ flag: 'FLAG_TEMP_1', on: true }) },
    var: { label: '🔢 Set a variable', make: () => ({ var: 'VAR_TEMP_1', value: 1 }) },
    if_flag: { label: '🔀 If a flag is on…', make: () => ({ flag: 'FLAG_TEMP_1', on: true, then: [], else: [] }), branches: [['then', 'Then'], ['else', 'Otherwise']] },
    if_var: { label: '🔀 If a variable is…', make: () => ({ var: 'VAR_TEMP_1', value: 1, then: [], else: [] }), branches: [['then', 'Then'], ['else', 'Otherwise']] },
    move: { label: '🚶 Move someone', make: () => ({ who: 'player', steps: [] }) },
    hide: { label: '👻 Hide a person', make: () => ({ who: '' }) },
    show: { label: '👀 Show a person', make: () => ({ who: '' }) },
    heal: { label: '❤️ Heal the team', make: () => ({}) },
    sound: { label: '🔔 Play a sound', make: () => ({ se: 'SE_PIN' }) },
    music: { label: '🎵 Change the music', make: () => ({ bgm: 'MUS_ENCOUNTER_GIRL' }) },
    wait: { label: '⏳ Wait', make: () => ({ frames: 30 }) },
    fade: { label: '🌑 Fade the screen', make: () => ({ out: true }) },
    warp: { label: '🚪 Go to another map', make: () => ({ map: '', x: 0, y: 0 }) },
    custom: { label: '⌨️ Script line (expert)', make: () => ({ line: '' }) },
  };
  const STEPS = [['up', '▲'], ['down', '▼'], ['left', '◀'], ['right', '▶'], ['run_up', '⏫'], ['run_down', '⏬'], ['run_left', '⏪'], ['run_right', '⏩'],
    ['face_up', 'look ▲'], ['face_down', 'look ▼'], ['face_left', 'look ◀'], ['face_right', 'look ▶'], ['face_player', 'look at player'], ['jump_down', 'jump'], ['!', '❗'], ['?', '❓'], ['wait', 'wait']];
  const STEP_LABEL = Object.fromEntries(STEPS);
  const lists = {};
  async function datalist(prefix) {
    const id = 'bl-dl-' + prefix.replace(/_$/, '');
    if (lists[prefix]) return id;
    lists[prefix] = true;
    const dl = document.createElement('datalist'); dl.id = id; document.body.appendChild(dl);
    try { const c = await call('/api/constants?prefix=' + prefix); dl.innerHTML = c.map(x => `<option value="${esc(x)}">`).join(''); } catch (e) { /* none */ }
    return id;
  }
  ['ITEM_', 'SPECIES_', 'TRAINER_', 'FLAG_', 'VAR_', 'SE_', 'MUS_', 'MAP_'].forEach(datalist);

  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;align-items:center;justify-content:center;z-index:45';
  box.innerHTML = `<div style="background:var(--panel);color:var(--text);border-radius:12px;padding:12px 16px;width:min(1100px,97vw);height:min(860px,95vh);display:flex;flex-direction:column">
    <div style="display:flex;gap:8px;align-items:center"><b style="font-size:15px">🧩 What happens</b><span id="bl-title" class="muted"></span><span style="flex:1"></span>
      <span class="adv">script name <input id="bl-label" style="width:280px"></span>
      <button id="bl-save" class="primary">Save</button><button id="bl-test" title="save, then try it in the game">Save &amp; test</button><button id="bl-close">Close</button></div>
    <div style="display:grid;grid-template-columns:1.25fr 1fr;gap:14px;flex:1;min-height:0;margin-top:8px">
      <div style="overflow:auto;padding-right:4px"><div id="bl-blocks"></div></div>
      <div style="overflow:auto;border-left:1px solid var(--line);padding-left:12px">
        <div class="muted" style="font-size:12px">How the dialogue looks in game:</div><div id="bl-texts"></div>
        <details class="adv"><summary class="muted">The script it makes</summary><pre id="bl-script" style="font-size:11px;white-space:pre-wrap"></pre></details>
        <div id="bl-rec" class="muted" style="font-size:12px;margin-top:8px"></div>
      </div>
    </div></div>`;
  document.body.appendChild(box);
  const $ = (s) => box.querySelector(s);
  let B = null, previewTimer;

  function blockHtml(b, path) {
    const k = KINDS[b.type] || KINDS.custom;
    const f = (name, attrs = '') => `data-f="${name}" data-p="${path}" ${attrs}`;
    let h = `<div class="bl-card" style="border:1px solid var(--line);border-radius:10px;padding:6px 8px;margin:6px 0;background:var(--bg)">
      <div style="display:flex;gap:6px;align-items:center"><b style="font-size:12px">${k.label}</b><span style="flex:1"></span>
      <button data-act="up" data-p="${path}" title="move up">↑</button><button data-act="down" data-p="${path}" title="move down">↓</button><button data-act="del" data-p="${path}" title="remove">✕</button></div>`;
    const t = b.type;
    if (t === 'say' || t === 'ask') h += `<textarea ${f('text')} rows="2" style="width:100%" placeholder="what they say">${esc(b.text)}</textarea>`;
    if (t === 'give_item') h += `<div class="row"><input ${f('item', 'list="bl-dl-ITEM"')} value="${esc(b.item)}" style="flex:1"> × <input ${f('amount', 'type="number" min="1" max="99"')} value="${b.amount}" style="width:60px"></div>`;
    if (t === 'give_mon' || t === 'wild') h += `<div class="row"><input ${f('species', 'list="bl-dl-SPECIES"')} value="${esc(b.species)}" style="flex:1"> level <input ${f('level', 'type="number" min="1" max="100"')} value="${b.level}" style="width:60px"></div>`;
    if (t === 'trainer') h += `<div class="row"><input ${f('trainer', 'list="bl-dl-TRAINER"')} value="${esc(b.trainer)}" placeholder="TRAINER_X" style="flex:1"></div>
      <div class="row">before: <input ${f('intro')} value="${esc(b.intro)}" style="flex:1"></div><div class="row">when they lose: <input ${f('defeat')} value="${esc(b.defeat)}" style="flex:1"></div>`;
    if (t === 'flag' || t === 'if_flag') h += `<div class="row"><input ${f('flag', 'list="bl-dl-FLAG"')} value="${esc(b.flag)}" style="flex:1"><select ${f('on')}><option value="1" ${b.on ? 'selected' : ''}>${t === 'flag' ? 'turn on' : 'is on'}</option><option value="0" ${b.on ? '' : 'selected'}>${t === 'flag' ? 'turn off' : 'is off'}</option></select></div>`;
    if (t === 'var' || t === 'if_var') h += `<div class="row"><input ${f('var', 'list="bl-dl-VAR"')} value="${esc(b.var)}" style="flex:1"> ${t === 'var' ? '=' : 'is'} <input ${f('value', 'type="number"')} value="${b.value}" style="width:70px"></div>`;
    if (t === 'move') {
      h += `<div class="row">who <select ${f('who')}><option value="player">the player</option>${B.localIds.map(id => `<option ${b.who === id ? 'selected' : ''}>${esc(id)}</option>`).join('')}</select></div>
        <div class="row" style="flex-wrap:wrap">${(b.steps || []).map((s, i) => `<span style="background:var(--chip);border-radius:8px;padding:1px 6px;font-size:12px">${esc(STEP_LABEL[s] || s)} <a data-delstep="${i}" data-p="${path}" style="cursor:pointer">✕</a></span>`).join('') || '<span class="muted">no steps yet</span>'}</div>
        <div class="row" style="flex-wrap:wrap">${STEPS.map(([s, l]) => `<button data-step="${s}" data-p="${path}" style="padding:2px 6px;font-size:11px">${esc(l)}</button>`).join('')}
        <button data-userec="1" data-p="${path}" style="padding:2px 6px;font-size:11px" title="the steps you walked with 🎬 Record in the test window">🎬 use my recorded steps</button></div>`;
    }
    if (t === 'hide' || t === 'show') h += `<div class="row">who <select ${f('who')}>${B.localIds.map(id => `<option ${b.who === id ? 'selected' : ''}>${esc(id)}</option>`).join('')}</select></div>`;
    if (t === 'sound') h += `<input ${f('se', 'list="bl-dl-SE"')} value="${esc(b.se)}" style="width:100%">`;
    if (t === 'music') h += `<input ${f('bgm', 'list="bl-dl-MUS"')} value="${esc(b.bgm)}" style="width:100%">`;
    if (t === 'wait') h += `<div class="row"><input ${f('frames', 'type="number" min="1"')} value="${b.frames}" style="width:80px"> frames (60 = 1 second)</div>`;
    if (t === 'fade') h += `<select ${f('out')}><option value="1" ${b.out ? 'selected' : ''}>to black</option><option value="0" ${b.out ? '' : 'selected'}>back from black</option></select>`;
    if (t === 'warp') h += `<div class="row"><input ${f('map', 'list="bl-dl-MAP"')} value="${esc(b.map)}" style="flex:1"> x <input ${f('x', 'type="number"')} value="${b.x}" style="width:56px"> y <input ${f('y', 'type="number"')} value="${b.y}" style="width:56px"></div>`;
    if (t === 'custom') h += `<input ${f('line')} value="${esc(b.line)}" style="width:100%;font-family:ui-monospace,monospace">`;
    if (k.hint) h += `<div class="muted" style="font-size:11px">${esc(k.hint)}</div>`;
    for (const [key, name] of k.branches || []) {
      h += `<div style="margin:6px 0 0 10px;padding-left:8px;border-left:3px solid var(--sel)"><div class="muted" style="font-size:11px">${esc(name)}:</div>${listHtml(b[key] || [], `${path}.${key}`)}</div>`;
    }
    return h + '</div>';
  }
  function listHtml(list, path) {
    return list.map((b, i) => blockHtml(b, `${path}.${i}`)).join('') +
      `<select data-add="${path}" style="margin:4px 0"><option value="">+ add a step…</option>${Object.entries(KINDS).map(([k, v]) => `<option value="${k}">${v.label}</option>`).join('')}</select>`;
  }
  // a path like "b.2.yes.0" -> [list, index]
  function locate(path) {
    const parts = path.split('.').slice(1);
    let list = B.blocks;
    for (let i = 0; i < parts.length - 1; i += 2) list = list[+parts[i]][parts[i + 1]];
    return [list, +parts[parts.length - 1]];
  }
  const listAt = (path) => { const parts = path.split('.').slice(1); let list = B.blocks; for (let i = 0; i < parts.length; i += 2) list = list[+parts[i]][parts[i + 1]]; return list; };
  function render() {
    $('#bl-blocks').innerHTML = listHtml(B.blocks, 'b');
    const host = $('#bl-blocks');
    host.querySelectorAll('[data-add]').forEach(sel => sel.onchange = () => {
      if (!sel.value) return; listAt(sel.dataset.add).push({ type: sel.value, ...KINDS[sel.value].make() }); render(); preview();
    });
    host.querySelectorAll('[data-f]').forEach(inp => inp.oninput = inp.onchange = () => {
      const [list, i] = locate(inp.dataset.p), b = list[i], k = inp.dataset.f;
      b[k] = inp.type === 'number' ? +inp.value : (k === 'on' || k === 'out') ? inp.value === '1' : inp.value;
      clearTimeout(previewTimer); previewTimer = setTimeout(preview, 400);
    });
    host.querySelectorAll('[data-act]').forEach(btn => btn.onclick = () => {
      const [list, i] = locate(btn.dataset.p);
      if (btn.dataset.act === 'del') list.splice(i, 1);
      else if (btn.dataset.act === 'up' && i > 0) [list[i - 1], list[i]] = [list[i], list[i - 1]];
      else if (btn.dataset.act === 'down' && i < list.length - 1) [list[i + 1], list[i]] = [list[i], list[i + 1]];
      render(); preview();
    });
    host.querySelectorAll('[data-step]').forEach(btn => btn.onclick = () => { const [list, i] = locate(btn.dataset.p); (list[i].steps = list[i].steps || []).push(btn.dataset.step); render(); preview(); });
    host.querySelectorAll('[data-delstep]').forEach(a => a.onclick = () => { const [list, i] = locate(a.dataset.p); list[i].steps.splice(+a.dataset.delstep, 1); render(); preview(); });
    host.querySelectorAll('[data-userec]').forEach(btn => btn.onclick = () => {
      const rec = window.paeRecordedSteps || [];
      if (!rec.length) { alert('No recorded steps yet. In ▶ Test in game, press 🎬 Record, walk the path, then press it again.'); return; }
      const [list, i] = locate(btn.dataset.p); list[i].steps = [...(list[i].steps || []), ...rec]; render(); preview();
    });
    const rec = window.paeRecordedSteps || [];
    $('#bl-rec').textContent = rec.length ? `🎬 Recorded steps ready: ${rec.length} (use them in a 🚶 Move block)` : '';
  }
  async function preview() {
    try {
      const r = await call('/api/map/event_blocks_preview', { label: $('#bl-label').value.trim(), kind: B.kind, blocks: B.blocks });
      $('#bl-script').textContent = r.script;
      $('#bl-texts').innerHTML = r.texts.map(t => `<div style="margin:6px 0">${t.problems.length ? `<div style="color:var(--accent);font-size:11px">⚠ ${esc(t.problems.join('; '))}</div>` : ''}<img src="${t.image}" alt="" style="image-rendering:pixelated;width:100%;max-width:464px"></div>`).join('') || '<div class="muted">No dialogue yet.</div>';
    } catch (e) { $('#bl-texts').innerHTML = `<div style="color:var(--accent)">${esc(e.message)}</div>`; }
  }
  async function save(test) {
    try {
      const label = $('#bl-label').value.trim();
      const r = await call('/api/map/event_blocks?name=' + encodeURIComponent(B.map), { label, kind: B.kind, blocks: B.blocks });
      box.style.display = 'none';
      if (B.onSaved) await B.onSaved(r.label, test, r);
    } catch (e) { alert(e.message); }
  }
  $('#bl-save').onclick = () => save(false);
  $('#bl-test').onclick = () => save(true);
  $('#bl-close').onclick = () => { box.style.display = 'none'; };
  window.openBlockBuilder = async function (opts) {
    B = { map: opts.map, kind: opts.kind || 'npc', localIds: opts.localIds || [], onSaved: opts.onSaved, blocks: [] };
    $('#bl-label').value = opts.label;
    $('#bl-title').textContent = `${opts.map} · ${opts.label}`;
    try {
      const r = await call(`/api/map/event_blocks?name=${encodeURIComponent(opts.map)}&label=${encodeURIComponent(opts.label)}&kind=${B.kind}`);
      B.blocks = r.blocks.length ? r.blocks : [{ type: 'say', text: opts.kind === 'sign' ? 'This is a sign.' : 'Hello!' }];
      if (r.blocks.length && !r.built) $('#bl-title').textContent += ' (made from the existing script: check it before saving)';
    } catch (e) { B.blocks = [{ type: 'say', text: 'Hello!' }]; }
    box.style.display = 'flex';
    render(); preview();
  };
})();
