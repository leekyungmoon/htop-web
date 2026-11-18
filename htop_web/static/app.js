// htop-web page: draws htop-style panels from websocket updates.
// All host data is inserted with textContent, never as HTML.
'use strict';

(() => {
  // ---------------------------------------------------------------- formatting
  // These mirror htop_web/render.py so the page and /htop.txt agree.

  function human(nbytes) {
    let value = Math.max(Number(nbytes) || 0, 0) / 1024;
    let unit = 'K';
    for (const next of 'MGTPE') {
      if (value < 1024) break;
      value /= 1024;
      unit = next;
    }
    if (value === 0) return '0' + unit;
    if (value < 9.995) return value.toFixed(2) + unit;
    if (value < 99.95) return value.toFixed(1) + unit;
    return value.toFixed(0) + unit;
  }

  const pad2 = (n) => String(n).padStart(2, '0');

  function uptimeText(seconds) {
    let s = Math.floor(seconds);
    const days = Math.floor(s / 86400); s %= 86400;
    const hours = Math.floor(s / 3600); s %= 3600;
    const minutes = Math.floor(s / 60); s %= 60;
    const clock = `${pad2(hours)}:${pad2(minutes)}:${pad2(s)}`;
    if (days > 100) return `${days} days(!), ${clock}`;
    if (days > 1) return `${days} days, ${clock}`;
    if (days === 1) return `1 day, ${clock}`;
    return clock;
  }

  function timeText(cs) {
    const total = Math.floor(cs / 100);
    if (total < 3600) return `${Math.floor(total / 60)}:${pad2(total % 60)}.${pad2(cs % 100)}`;
    let hours = Math.floor(total / 3600);
    const rest = total % 3600;
    if (hours < 100) return `${hours}h${pad2(Math.floor(rest / 60))}:${pad2(rest % 60)}`;
    let days = Math.floor(hours / 24);
    hours %= 24;
    if (days < 365) return `${days}d${pad2(hours)}h`;
    const years = Math.floor(days / 365);
    days %= 365;
    return `${years}y${String(days).padStart(3, '0')}d`;
  }

  function memCell(nbytes) {  // 5 characters: 58372 241M 5596M 53.3G 1448G
    const kib = Math.floor(nbytes / 1024);
    if (kib < 1000) return [[String(kib).padStart(5), null]];
    if (kib < 100000) {
      return [[String(Math.floor(kib / 1000)).padStart(2), 'mega'],
              [String(kib % 1000).padStart(3, '0'), null]];
    }
    const mib = kib / 1024;
    if (mib < 1000) {
      return [[(mib >= 99.95 ? mib.toFixed(0) : mib.toFixed(1)).padStart(4) + 'M', 'mega']];
    }
    if (mib < 10000) {
      const whole = Math.floor(mib);
      return [[String(Math.floor(whole / 1000)), 'giga'],
              [String(whole % 1000).padStart(3, '0') + 'M', 'mega']];
    }
    const gib = mib / 1024;
    if (gib < 100) {
      const tenths = Math.floor(gib * 10);
      return [[String(Math.floor(tenths / 10)).padStart(2), 'giga'], ['.' + (tenths % 10), 'mega'],
              ['G', 'giga']];
    }
    if (gib < 1000) return [[String(Math.floor(gib)).padStart(4) + 'G', 'giga']];
    if (gib < 10000) {
      const whole = Math.floor(gib);
      return [[String(Math.floor(whole / 1000)), 'tera'],
              [String(whole % 1000).padStart(3, '0') + 'G', 'giga']];
    }
    return [[(gib / 1024).toFixed(1).padStart(4) + 'T', 'tera']];
  }

  // CPU fields: user nice system irq softirq iowait steal guest
  const cpuParts = (v) => [[v[1], 'nice'], [v[0], 'user'], [v[2] + v[3] + v[4], 'system'],
                           [v[6] + v[7], 'virt']];
  const cpuTotal = (v) => Math.min(cpuParts(v).reduce((sum, [x]) => sum + x, 0), 100);
  const memParts = (m) => [[m.used, 'mem_used'], [m.shared, 'mem_shared'],
                           [m.buffers, 'mem_buffers'], [m.cache, 'mem_cache']];

  function cpuColumns(ncpu) {
    const half = Math.ceil(ncpu / 2);
    return half <= 8 ? 1 : half <= 16 ? 2 : 4;
  }

  // The inside of a bar meter: each part fills ceil(value/total*width) cells with
  // '|'; the value text sits at the right and takes the color of the part under it.
  function bar(width, parts, total, text) {
    if (width <= 0) return [];
    text = text.slice(-width);
    const chars = Array(width - text.length).fill(' ').concat([...text]);
    const styles = Array(width).fill('shadow');
    let offset = 0;
    for (const [value, style] of parts) {
      if (total <= 0 || value <= 0) continue;
      const cells = Math.ceil(Math.min(value, total) / total * width - 1e-9);
      const end = Math.min(offset + cells, width);
      for (let i = offset; i < end; i++) {
        if (chars[i] === ' ') chars[i] = '|';
        styles[i] = style;
      }
      offset = end;
    }
    const segs = [];
    chars.forEach((ch, i) => {
      const last = segs[segs.length - 1];
      if (last && last[1] === styles[i]) last[0] += ch;
      else segs.push([ch, styles[i]]);
    });
    return segs;
  }

  function meter(label, width, parts, total, text) {
    return [[label, 'label'], ['[', 'border'], ...bar(width - label.length - 2, parts, total, text),
            [']', 'border']];
  }

  function cpuMeters(cpus, width, perRow, labelWidth) {
    const cell = Math.floor((width - (perRow - 1)) / perRow);
    const lines = [];
    for (let i = 0; i < cpus.length; i += perRow) {
      const line = [];
      cpus.slice(i, i + perRow).forEach((entry, j) => {
        if (j) line.push([' ', null]);
        const values = entry.slice(1);
        line.push(...meter(String(entry[0]).padStart(labelWidth), cell, cpuParts(values), 100,
                           cpuTotal(values).toFixed(1) + '%'));
      });
      lines.push(line);
    }
    return lines;
  }

  // -------------------------------------------------------------------- state

  const COLUMNS = [  // sort key, header, left-aligned (widths live in style.css)
    ['pid', 'PID'], ['user', 'USER', true], ['pri', 'PRI'], ['ni', 'NI'], ['virt', 'VIRT'],
    ['res', 'RES'], ['shr', 'SHR'], ['state', 'S', true], ['cpu', 'CPU%'], ['mem', 'MEM%'],
    ['time', 'TIME+'], ['command', 'Command', true],
  ];
  const SORT_KEYS = new Set(COLUMNS.map(([key]) => key));
  const ASCENDING_FIRST = new Set(['pid', 'user', 'state', 'command']);
  const ROW_CHOICES = [5, 10, 15, 25, 50, 100, 200];

  const S = {
    params: { nodes: null, sort: 'cpu', order: 'desc', filter: '', rows: 15, kthreads: false },
    paused: false,
    basename: false,
    interval: 2,
    f: null,  // column name -> index in a process row
    cards: new Map(),
    order: [],
    ws: null,
    retry: 1000,
    version: '',
    charW: 8,
    folded: new Set(),
  };

  const $ = (id) => document.getElementById(id);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function setSegs(node, segs) {
    node.replaceChildren(...segs.filter(([t]) => t).map(([text, style]) => {
      if (!style) return document.createTextNode(text);
      return el('span', 'c-' + style, text);
    }));
  }

  function store(key, value) {
    try { localStorage.setItem('htop-web.' + key, JSON.stringify(value)); } catch (e) { /* private mode */ }
  }

  function recall(key, fallback) {
    try {
      const raw = localStorage.getItem('htop-web.' + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) {
      return fallback;
    }
  }

  function loadSettings() {
    const p = S.params;
    p.sort = recall('sort', p.sort);
    p.order = recall('order', p.order);
    p.rows = recall('rows', p.rows);
    p.kthreads = recall('kthreads', p.kthreads);
    S.basename = recall('basename', false);
    S.folded = new Set(recall('folded', []));
    const q = new URLSearchParams(location.search);
    if (q.get('nodes')) p.nodes = q.get('nodes').split(',').map((s) => s.trim()).filter(Boolean);
    if (q.get('sort')) p.sort = q.get('sort');
    if (q.get('order')) p.order = q.get('order');
    if (q.get('rows')) p.rows = Number(q.get('rows')) || p.rows;
    if (q.get('filter')) p.filter = q.get('filter');
    if (q.get('kthreads')) p.kthreads = ['1', 'true', 'yes', 'on'].includes(q.get('kthreads'));
    if (!SORT_KEYS.has(p.sort)) p.sort = 'cpu';
    if (p.order !== 'asc' && p.order !== 'desc') p.order = 'desc';
    p.rows = Math.min(Math.max(Math.round(Number(p.rows)) || 15, 1), 500);
    p.kthreads = Boolean(p.kthreads);
  }

  function saveSettings() {
    store('sort', S.params.sort);
    store('order', S.params.order);
    store('rows', S.params.rows);
    store('kthreads', S.params.kthreads);
    store('basename', S.basename);
    store('folded', [...S.folded]);
  }

  // ----------------------------------------------------------------- network

  function send(extra) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.ws.send(JSON.stringify({ ...S.params, paused: S.paused, ...extra }));
    }
  }

  function changeParams(changes) {
    Object.assign(S.params, changes);
    saveSettings();
    syncControls();
    send();
    redrawAll();
  }

  function connect() {
    const base = location.pathname.replace(/[^/]*$/, '');
    const query = new URLSearchParams();
    if (S.params.nodes) query.set('nodes', S.params.nodes.join(','));
    const url = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${base}ws?${query}`;
    const ws = new WebSocket(url);
    S.ws = ws;
    setConn(S.paused ? 'paused' : 'connecting');
    ws.onopen = () => {
      S.retry = 1000;
      setConn(S.paused ? 'paused' : 'live');
      send();
    };
    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === 'hello') hello(msg);
      else if (msg.type === 'update') update(msg);
    };
    ws.onclose = () => {
      if (S.ws !== ws) return;
      S.ws = null;
      setConn('reconnecting', S.retry);
      setTimeout(connect, S.retry);
      S.retry = Math.min(S.retry * 2, 15000);
    };
  }

  function setConn(state, retry) {
    const node = $('conn');
    node.className = 'conn ' + state;
    node.textContent = state === 'reconnecting' ? `reconnecting in ${Math.round(retry / 1000)}s`
      : state === 'paused' ? 'paused' : state;
  }

  function hello(msg) {
    S.interval = msg.interval || 2;
    S.f = {};
    (msg.proc_fields || []).forEach((name, i) => { S.f[name] = i; });
    S.version = msg.version || '';
    arrange(msg.hosts || []);
    drawSummary();
  }

  function update(msg) {
    if (msg.full) arrange(msg.hosts.map((h) => h.key));
    for (const view of msg.hosts) {
      if (!S.cards.has(view.key)) arrange([...S.order, view.key]);
      const card = S.cards.get(view.key);
      card.view = view;
      card.receivedAt = performance.now();
      drawCard(card);
    }
    drawSummary();
  }

  // ------------------------------------------------------------------- cards

  function arrange(keys) {
    const main = $('hosts');
    for (const [key, card] of S.cards) {
      if (!keys.includes(key)) {
        card.root.remove();
        S.cards.delete(key);
      }
    }
    for (const key of keys) {
      if (!S.cards.has(key)) S.cards.set(key, makeCard(key));
      main.append(S.cards.get(key).root);  // append also reorders
    }
    S.order = keys.slice();
    $('empty').hidden = keys.length > 0;
  }

  function makeCard(key) {
    const card = { key, view: null, receivedAt: 0 };
    card.root = el('section', 'host');
    card.head = el('div', 'host-head');
    card.head.title = 'Fold or unfold this host';
    card.caret = el('span', 'caret', '▾');
    card.name = el('span', 'name', key);
    card.tag = el('span', 'tag');
    card.status = el('span', 'status loading', 'loading');
    card.age = el('span', 'age');
    card.kernel = el('span', 'kernel');
    card.head.append(card.caret, card.name, card.tag, card.status, card.age, card.kernel);
    card.head.addEventListener('click', () => {
      if (S.folded.has(key)) S.folded.delete(key); else S.folded.add(key);
      saveSettings();
      card.root.classList.toggle('folded', S.folded.has(key));
      drawCard(card);
    });
    card.msg = el('div', 'host-msg');
    card.body = el('div', 'host-body');
    card.meters = el('div', 'meters');
    card.left = el('div', 'mcol');
    card.right = el('div', 'mcol');
    card.meters.append(card.left, card.right);
    card.table = el('table', 'procs');
    card.thead = el('thead');
    card.headRow = el('tr');
    for (const [name, title, left] of COLUMNS) {
      const th = el('th', `k-${name}${left ? ' l' : ''}`, title);
      th.dataset.sort = name;
      th.title = `Sort by ${title}`;
      th.addEventListener('click', () => sortBy(name));
      card.headRow.append(th);
    }
    card.thead.append(card.headRow);
    card.tbody = el('tbody');
    card.table.append(card.thead, card.tbody);
    const wrap = el('div', 'table-wrap');
    wrap.append(card.table);
    card.foot = el('div', 'host-foot');
    card.body.append(card.meters, wrap, card.foot);
    card.root.append(card.head, card.msg, card.body);
    card.root.classList.toggle('folded', S.folded.has(key));
    return card;
  }

  const STATUS_TEXT = { ok: 'live', loading: 'loading', connecting: 'connecting', stale: 'stale',
                        error: 'error' };

  function drawCard(card) {
    const view = card.view;
    if (!view) return;
    card.name.textContent = view.host || view.key;
    card.name.title = view.key;
    const tags = [];
    if (view.host && view.key !== view.host && view.kind === 'ssh') tags.push(view.key);
    if (view.kind !== 'ssh') tags.push(view.kind);
    card.tag.textContent = tags.join(' ');
    card.status.className = 'status ' + view.status;
    card.status.textContent = STATUS_TEXT[view.status] || view.status;
    card.kernel.textContent = view.kernel ? `Linux ${view.kernel}` : '';
    card.msg.className = 'host-msg ' + view.status;
    card.msg.textContent = view.message || '';
    card.root.classList.toggle('dim', view.status !== 'ok' && Boolean(view.cpus));
    drawAge(card);
    if (!view.cpus || S.folded.has(card.key)) {
      card.body.hidden = !view.cpus;
      return;
    }
    card.body.hidden = false;
    drawMeters(card, view);
    drawTable(card, view);
    let note = `${view.procs.length} of ${view.matched} processes`;
    if (S.params.filter) note += ` matching "${S.params.filter}" (${view.shown} in all)`;
    if (!S.params.kthreads) note += '; kernel threads hidden';
    card.foot.textContent = note;
  }

  function drawAge(card) {
    const view = card.view;
    if (!view || view.age === null || view.age === undefined) {
      card.age.textContent = '';
      return;
    }
    const age = view.age + (S.paused ? 0 : (performance.now() - card.receivedAt) / 1000);
    card.age.textContent = S.paused ? 'paused' : `updated ${age < 10 ? age.toFixed(1) : Math.round(age)}s ago`;
  }

  function columnChars(node) {
    return Math.max(Math.floor(node.clientWidth / S.charW), 20);
  }

  function drawMeters(card, view) {
    const cpus = view.cpus;
    const half = Math.ceil(cpus.length / 2);
    const perRow = cpuColumns(cpus.length);
    const digits = String(Math.max(0, ...cpus.map((c) => c[0]))).length;
    const labelWidth = Math.max(perRow === 1 ? 3 : 2, digits);
    const lw = columnChars(card.left);
    const rw = columnChars(card.right);
    // Side by side: CPUs split in halves like htop. Stacked (narrow): all CPUs first.
    const stacked = getComputedStyle(card.meters).flexDirection === 'column';
    const left = cpuMeters(stacked ? cpus : cpus.slice(0, half), lw, perRow, labelWidth);
    const right = stacked ? [] : cpuMeters(cpus.slice(half), rw, perRow, labelWidth);
    const m = view.mem;
    const s = view.swap;
    const t = view.tasks;
    left.push(meter('Mem', lw, memParts(m), m.total, `${human(m.used + m.shared)}/${human(m.total)}`));
    left.push(meter('Swp', lw, [[s.used, 'swap_used'], [s.cache, 'swap_cache']], s.total,
                    `${human(s.used)}/${human(s.total)}`));
    right.push([['Tasks: ', 'label'], [String(t.procs), 'value'], [', ', 'label'],
                [String(t.threads), 'threads'], [' thr', 'label'], [`, ${t.kthreads} kthr`, 'shadow'],
                ['; ', 'label'], [String(t.running), 'running'], [' running', 'label']]);
    right.push([['Load average: ', 'label'], [view.load[0].toFixed(2) + ' ', 'load1'],
                [view.load[1].toFixed(2) + ' ', 'load5'], [view.load[2].toFixed(2), 'load15']]);
    right.push([['Uptime: ', 'label'], [uptimeText(view.uptime), 'uptime']]);
    drawLines(card.left, left);
    drawLines(card.right, right);
  }

  function drawLines(node, lines) {
    while (node.children.length < lines.length) node.append(el('div', 'mline'));
    while (node.children.length > lines.length) node.lastChild.remove();
    lines.forEach((segs, i) => setSegs(node.children[i], segs));
  }

  function drawTable(card, view) {
    const sort = S.params.sort;
    const arrow = S.params.order === 'desc' ? '▽' : '△';
    for (const th of card.headRow.children) {
      const sorted = th.dataset.sort === sort;
      const title = COLUMNS.find(([name]) => name === th.dataset.sort)[1];
      th.classList.toggle('sorted', sorted);
      th.textContent = sorted ? title + arrow : title;
    }
    const f = S.f;
    const rows = view.procs;
    const body = card.tbody;
    while (body.children.length < rows.length) body.append(makeRow());
    while (body.children.length > rows.length) body.lastChild.remove();
    rows.forEach((p, i) => {
      const cells = body.children[i].children;
      const pri = p[f.pri] <= -100 ? 'RT' : String(p[f.pri]);
      const ni = p[f.ni];
      const state = p[f.state];
      const cpu = p[f.cpu];
      cells[0].textContent = p[f.pid];
      cells[1].textContent = p[f.user];
      cells[2].textContent = pri;
      setSegs(cells[3], [[String(ni), ni < 0 ? 'prio_high' : ni > 0 ? 'prio_low' : 'shadow']]);
      setSegs(cells[4], memCell(p[f.virt]));
      setSegs(cells[5], memCell(p[f.res]));
      setSegs(cells[6], memCell(p[f.shr]));
      setSegs(cells[7], [[state, state === 'R' ? 'state_r' : state === 'D' ? 'state_d'
        : 'SI'.includes(state) ? 'shadow' : null]]);
      cells[8].textContent = cpu < 999.95 ? cpu.toFixed(1) : cpu.toFixed(0);
      cells[9].textContent = p[f.mem].toFixed(1);
      cells[10].textContent = timeText(p[f.time]);
      const cmd = p[f.cmd];
      const hs = p[f.hs];
      const he = p[f.he];
      if (S.basename && he > hs) {
        setSegs(cells[11], [[cmd.slice(0, hs), null], [cmd.slice(hs, he), 'basename'],
                            [cmd.slice(he), null]]);
      } else {
        cells[11].textContent = cmd;
      }
      cells[11].title = cmd;
    });
  }

  function makeRow() {
    const tr = el('tr');
    COLUMNS.forEach(([name, , left]) => {
      tr.append(el('td', `k-${name}${left ? ' l' : ''}${name === 'command' ? ' cmd' : ''}`));
    });
    return tr;
  }

  function drawSummary() {
    const views = [...S.cards.values()].map((c) => c.view).filter(Boolean);
    const live = views.filter((v) => v.status === 'ok').length;
    $('summary').textContent = `v${S.version} · ${live}/${S.cards.size} hosts live`;
  }

  function redrawAll() {
    for (const card of S.cards.values()) drawCard(card);
  }

  // ------------------------------------------------------------- controls

  function sortBy(name) {
    if (S.params.sort === name) {
      changeParams({ order: S.params.order === 'desc' ? 'asc' : 'desc' });
    } else {
      changeParams({ sort: name, order: ASCENDING_FIRST.has(name) ? 'asc' : 'desc' });
    }
  }

  function setRows(rows) {
    changeParams({ rows });
  }

  function stepRows(direction) {
    const i = ROW_CHOICES.indexOf(S.params.rows);
    const at = i < 0 ? ROW_CHOICES.findIndex((r) => r > S.params.rows) : i;
    const next = ROW_CHOICES[Math.min(Math.max(at + direction, 0), ROW_CHOICES.length - 1)];
    setRows(next);
  }

  function togglePause() {
    S.paused = !S.paused;
    send({ paused: S.paused });
    setConn(S.ws ? (S.paused ? 'paused' : 'live') : 'reconnecting', S.retry);
    syncControls();
    for (const card of S.cards.values()) drawAge(card);
  }

  function toggleKthreads() {
    changeParams({ kthreads: !S.params.kthreads });
  }

  function toggleBasename() {
    S.basename = !S.basename;
    saveSettings();
    syncControls();
    redrawAll();
  }

  let filterTimer = null;
  function onFilterInput() {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => changeParams({ filter: $('filter').value.trim() }), 200);
  }

  const KEYS = [  // key label, action label, help text, handler
    ['/', 'Filter', 'Filter by command or user', () => $('filter').focus()],
    ['P', 'CPU%', 'Sort by CPU%', () => changeParams({ sort: 'cpu', order: 'desc' })],
    ['M', 'MEM%', 'Sort by MEM%', () => changeParams({ sort: 'mem', order: 'desc' })],
    ['T', 'TIME+', 'Sort by TIME+', () => changeParams({ sort: 'time', order: 'desc' })],
    ['I', 'Invert', 'Invert the sort order', () => sortBy(S.params.sort)],
    ['K', 'Kthr', 'Show or hide kernel threads', toggleKthreads],
    ['B', 'Base', 'Highlight program names', toggleBasename],
    ['Z', 'Pause', 'Pause or resume updates', togglePause],
    ['-', 'Rows-', 'Show fewer process rows', () => stepRows(-1)],
    ['+', 'Rows+', 'Show more process rows', () => stepRows(1)],
    ['?', 'Help', 'Show this help', () => showHelp(true)],
  ];

  function buildKeys() {
    const bar = $('fkeys');
    const help = $('help-keys');
    for (const [key, label, text, run] of KEYS) {
      const button = el('button');
      button.type = 'button';
      button.title = text;
      button.dataset.key = key;
      button.append(el('span', 'key', key), el('span', 'act', label));
      button.addEventListener('click', run);
      bar.append(button);
      const tr = el('tr');
      tr.append(el('td', null, key), el('td', null, text));
      help.append(tr);
    }
    const tr = el('tr');
    tr.append(el('td', null, 'Esc'), el('td', null, 'Clear the filter, close this help'));
    help.append(tr);
  }

  function syncControls() {
    $('filter').value = S.params.filter;
    const rows = $('rows');
    if (![...rows.options].some((o) => Number(o.value) === S.params.rows)) {
      rows.append(el('option', null, String(S.params.rows)));
    }
    rows.value = String(S.params.rows);
    $('kthreads').checked = S.params.kthreads;
    const pause = $('pause');
    pause.textContent = S.paused ? 'Resume' : 'Pause';
    pause.classList.toggle('on', S.paused);
    for (const button of $('fkeys').children) {
      const key = button.dataset.key;
      button.classList.toggle('on', (key === 'Z' && S.paused) || (key === 'K' && S.params.kthreads)
        || (key === 'B' && S.basename));
    }
  }

  function showHelp(open) {
    $('help').hidden = !open;
    if (open) $('help-close').focus();
  }

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName);
    if (event.key === 'Escape') {
      if (!$('help').hidden) showHelp(false);
      else if (S.params.filter || document.activeElement === $('filter')) {
        $('filter').blur();
        changeParams({ filter: '' });
      }
      return;
    }
    if (typing) return;
    const key = event.key === '\\' ? '/' : event.key === '=' ? '+' : event.key.toUpperCase();
    const entry = KEYS.find(([k]) => k === key);
    if (entry) {
      event.preventDefault();
      entry[3]();
    }
  }

  function measure() {
    const probe = $('measure');
    S.charW = probe.getBoundingClientRect().width / probe.textContent.length || 8;
  }

  let resizeFrame = 0;
  function onResize() {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      measure();
      redrawAll();
    });
  }

  function start() {
    loadSettings();
    measure();
    buildKeys();
    syncControls();
    $('filter').addEventListener('input', onFilterInput);
    $('rows').addEventListener('change', (e) => setRows(Number(e.target.value)));
    $('kthreads').addEventListener('change', (e) => changeParams({ kthreads: e.target.checked }));
    $('pause').addEventListener('click', togglePause);
    $('help-close').addEventListener('click', () => showHelp(false));
    $('help').addEventListener('click', (e) => { if (e.target === $('help')) showHelp(false); });
    document.addEventListener('keydown', onKey);
    new ResizeObserver(onResize).observe($('hosts'));
    setInterval(() => { for (const card of S.cards.values()) drawAge(card); }, 1000);
    connect();
  }

  start();
})();
