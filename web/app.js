/* ==========================================================================
   PyTrace · 前端主逻辑
   1) 与后端 WebSocket 通信，实时接收运行时事件
   2) 把事件折叠成「调用树 + 瀑布图」模型
   3) 支持时间轴回放（seek / play / step）
   ========================================================================== */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const MAX_ROWS = 2500;
const MAX_EV_ROWS = 1500;

/* ---------------------------- 状态 ---------------------------- */
const S = {
  code: '', filename: 'main.py', demo: false,
  projectFiles: null,       // 多文件项目：[{name: 相对路径, code}]，null = 单文件模式
  frameVars: new Map(), stmtMap: new Map(),
  events: [], cursor: 0,
  mode: 'idle',            // idle | live | done
  following: true,
  playing: false, progress: 0, speed: 1,
  spans: new Map(), order: [], roots: [],
  imports: [], importAgg: new Map(),
  stdout: '', stderr: '', notes: [], errors: [],
  curLine: 0, lineOf: new Map(), stack: [],
  span: 1, endMs: 0, stats: null, wall: 0,
  sel: null, showLib: false, runId: null,
  ws: null, startedAt: 0,
  seen: new Set(), flowKey: '',
  teachLine: 0, teachRes: '', teachByLine: new Map(), flowLog: [], entryCard: '',
  subs: new Map(),          // 行号 → [{pid, depth, text, repr}]：表达式级中间值（birdseye 式）
  projectMap: null, readCard: '', navStack: [], crumbHtml: '',
  coverage: new Map(),      // 全景图运行覆盖：文件名 → {events, ok, ms, err, via}
  root: '', interpreter: '', interpList: [],
  // 断点 + 单步：让"跑完才能回放"变成"跑的过程中可以停下来问一句"
  breaks: new Set(),        // "文件名:行号"，localStorage 持久化
  debug: false,             // 下次运行是否以调试模式启动（停在第一行）
  paused: null,             // 当前暂停位置 {file,line,reason,depth}
  wasStopped: false,        // 用户主动停止 → done 时不再弹"失败诊断卡"
};

/* 阅读模式（逐段讲解）的本地状态：放在最前面定义，避免 loadCode 早于它执行时踩 TDZ */
const DOC = { data: null, key: '', loading: false, openAll: false, err: '', reqId: 0 };

/* ---------------------------- 语法高亮 ---------------------------- */
const KW = new Set(('False None True and as assert async await break class continue def del elif else except ' +
  'finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case').split(' '));

function highlightAll(code) {
  const lines = [];
  let buf = '';
  const push = (cls, txt) => { buf += cls ? `<span class="tk-${cls}">${esc(txt)}</span>` : esc(txt); };
  const flush = () => { lines.push(buf); buf = ''; };
  let i = 0;
  const isId = (c) => /[A-Za-z0-9_\u4e00-\u9fa5]/.test(c || '');
  while (i < code.length) {
    const c = code[i];
    if (c === '\n') { flush(); i++; continue; }
    if (c === '#') {
      let j = code.indexOf('\n', i); if (j < 0) j = code.length;
      push('com', code.slice(i, j)); i = j; continue;
    }
    if (c === '"' || c === "'") {
      const q3 = c.repeat(3);
      if (code.startsWith(q3, i)) {
        let j = code.indexOf(q3, i + 3);
        j = j < 0 ? code.length : j + 3;
        const seg = code.slice(i, j);
        seg.split('\n').forEach((s, k) => { if (k > 0) flush(); push('str', s); });
        i = j; continue;
      }
      let j = i + 1;
      while (j < code.length && code[j] !== c && code[j] !== '\n') { if (code[j] === '\\') j++; j++; }
      j = Math.min(j + 1, code.length);
      push('str', code.slice(i, j)); i = j; continue;
    }
    if (/[0-9]/.test(c)) {
      let j = i;
      while (j < code.length && /[0-9a-fA-FxXoObB._]/.test(code[j])) j++;
      push('num', code.slice(i, j)); i = j; continue;
    }
    if (isId(c)) {
      let j = i;
      while (j < code.length && isId(code[j])) j++;
      const w = code.slice(i, j);
      if (KW.has(w)) push('kw', w);
      else if (w === 'self' || w === 'cls') push('self', w);
      else if (code[j] === '(') push('fn', w);
      else if (/^[A-Z]/.test(w)) push('def', w);
      else push(null, w);
      i = j; continue;
    }
    if ('+-*/%=<>!&|^~:,.()[]{}@'.includes(c)) { push('op', c); i++; continue; }
    push(null, c); i++;
  }
  flush();
  return lines;
}

/* ---------------------------- 代码渲染 ---------------------------- */
function renderCode() {
  const lines = highlightAll(S.code || '');
  let html = '';
  lines.forEach((h, i) => {
    html += `<div class="ln" data-l="${i + 1}"><span class="no">${i + 1}</span><span class="txt">${h || ' '}</span></div>`;
  });
  $('#lines').innerHTML = html;
  $('#codeMeta').textContent = lines.length ? `${lines.length} 行` : '—';
  $('#fileChip').textContent = S.code ? S.filename : '未选择文件';
  $('#fileChip').classList.toggle('on', !!S.code);
  if (typeof fitTopbar === 'function') fitTopbar(true);   // chip 变宽/变窄都要重新收纳（flex 盒子不变，RO 察觉不到内容收缩）
  $('#dropzone').classList.toggle('show', !S.code);
  lastLine = -1; lastErr = -1;
  S.flowKey = '';
  paintBreaks();
  // 事件委托：点行号＝下/取消断点。只绑一次，换文件重渲染也不会重复绑
  if (!renderCode.bound) {
    renderCode.bound = true;
    $('#lines').addEventListener('click', (e) => {
      const no = e.target.closest('.no');
      if (!no || !no.parentElement) return;
      const l = parseInt(no.parentElement.dataset.l, 10);
      if (l > 0) toggleBreak(l);
    });
  }
  drawFlow();
  if (S.curLine) markLine(S.curLine);
}

let lastLine = -1, lastErr = -1;
let NAV = [];                    // 跨文件跟随的返回栈（面包屑）
function markLine(n) {
  if (n === lastLine) return;
  const prev = $('#lines .ln.hl');
  if (prev) prev.classList.remove('hl');
  const el = document.querySelector(`#lines .ln[data-l="${n}"]`);
  if (el) {
    el.classList.add('hl');
    scrollCodeTo(el);
  }
  lastLine = n;
}
function scrollCodeTo(el) {
  const box = $('#editor');
  const top = el.offsetTop;
  if (top < box.scrollTop + 40 || top > box.scrollTop + box.clientHeight - 60) {
    box.scrollTo({ top: Math.max(0, top - box.clientHeight / 2), behavior: 'smooth' });
  }
}
function markErrorLine(n) {
  if (n === lastErr) return;
  const prev = $('#lines .ln.err'); if (prev) prev.classList.remove('err');
  if (n > 0) { const el = document.querySelector(`#lines .ln[data-l="${n}"]`); if (el) { el.classList.add('err'); scrollCodeTo(el); } }
  lastErr = n;
}
function markCallSite(n) {
  $$('#lines .callmark').forEach((e) => e.remove());
  if (!n) return;
  const el = document.querySelector(`#lines .ln[data-l="${n}"]`);
  if (el) { const d = document.createElement('span'); d.className = 'callmark'; el.appendChild(d); }
}

/* ---------------------------- 断点 ---------------------------- */
/* 断点存在 localStorage：换了文件、关了页面再打开，红点还在。
   键用"文件名:行号"而不是绝对路径 —— PyTrace 里项目文件名就是用户看到的相对路径，
   换台机器打开同一个项目也能对上。 */
const BP_STORE = 'pytrace.breaks';
function baseName(p) {
  return String(p || '').replace(/\\/g, '/').split('/').pop();
}
function loadBreaks() {
  try {
    const a = JSON.parse(localStorage.getItem(BP_STORE) || '[]');
    S.breaks = new Set(Array.isArray(a) ? a.filter((x) => typeof x === 'string') : []);
  } catch (e) { S.breaks = new Set(); }
  try { S.debug = localStorage.getItem('pytrace.debug') === '1'; } catch (e) {}
}
function saveBreaks() {
  try { localStorage.setItem(BP_STORE, JSON.stringify([...S.breaks])); } catch (e) {}
}
function bpKey(name, line) { return baseName(name) + ':' + line; }
function paintBreaks() {
  const n = baseName(S.filename);
  $$('#lines .ln').forEach((el) => {
    const l = parseInt(el.dataset.l, 10);
    el.classList.toggle('bp', l > 0 && S.breaks.has(bpKey(n, l)));
  });
}
function toggleBreak(line) {
  const k = bpKey(S.filename, line);
  const on = !S.breaks.has(k);
  if (on) S.breaks.add(k); else S.breaks.delete(k);
  saveBreaks();
  const el = document.querySelector(`#lines .ln[data-l="${line}"]`);
  if (el) el.classList.toggle('bp', on);
  // 正在跑：立刻把增删同步给探针，不用重跑。这是"调试"和"回放"的分水岭。
  if (S.mode === 'live' && S.debug && S.ws && S.ws.readyState === 1) {
    S.ws.send(JSON.stringify({ cmd: 'break', file: baseName(S.filename), line, on }));
  }
  toast(on ? `第 ${line} 行已下断点${S.debug ? '' : '（下次点「调试」运行时生效）'}`
           : `已取消第 ${line} 行的断点`, '');
}

/* ---------------------------- 调试：控制与状态 ---------------------------- */
function dbgCmd(cmd) {
  if (!S.ws || S.ws.readyState !== 1) { toast('没有正在运行的调试会话', 'err'); return; }
  S.ws.send(JSON.stringify({ cmd }));
  hideDbgBar();
  document.body.classList.remove('paused');
}
function hideDbgBar() {
  const bar = $('#dbgBar');
  if (bar) bar.hidden = true;
}
const DBG_REASON = {
  breakpoint: '命中断点', pause: '已暂停', step: '单步跳过',
  stepin: '单步进入', stepout: '单步跳出',
};
function handleDbg(ev) {
  if (ev.kind === 'armed') {
    toast('调试已就绪：程序停在第一行。按「单步」一行行走，或「继续」直接跑到底', 'ok');
    return;
  }
  if (ev.kind === 'paused') {
    S.paused = ev;
    const bar = $('#dbgBar');
    const msg = $('#dbgMsg');
    if (msg) {
      const f = baseName(ev.file || S.filename);
      const same = f === baseName(S.filename);
      msg.innerHTML = `<b>${DBG_REASON[ev.reason] || '已暂停'}</b> · 第 ${ev.line} 行`
        + (same ? '' : ' · ' + f)
        + (ev.depth ? ` · 栈深 ${ev.depth}` : '');
    }
    if (bar) bar.hidden = false;
    if (ev.line) markLine(ev.line);
    document.body.classList.add('paused');
    if (ev.file && baseName(ev.file) !== baseName(S.filename)) {
      toast(`停在另一个文件 ${ev.file}（第 ${ev.line} 行）。打开它才能看到光标`, 'warn');
    }
    return;
  }
  if (ev.kind === 'resumed' || ev.kind === 'stopped' || ev.kind === 'timeout') {
    S.paused = null;
    hideDbgBar();
    document.body.classList.remove('paused');
    if (ev.kind === 'stopped') S.wasStopped = true;
    if (ev.kind === 'timeout') toast('暂停太久，已自动继续执行（防止子进程被吊死）', 'warn');
  }
}


/* ---------------------------- 执行流可视化 ---------------------------- */
function lineCenter(n) {
  const el = document.querySelector(`#lines .ln[data-l="${n}"]`);
  return el ? el.offsetTop + el.offsetHeight / 2 : null;
}

function updateCursor() {
  const cur = $('#execCursor');
  if (!cur) return;
  if (!S.curLine) { cur.classList.remove('on'); return; }
  const el = document.querySelector(`#lines .ln[data-l="${S.curLine}"]`);
  if (!el) { cur.classList.remove('on'); return; }
  cur.style.height = el.offsetHeight + 'px';
  cur.style.top = el.offsetTop + 'px';
  cur.classList.add('on');
}

/** 把"调用行 → 函数定义行"画成一条带流动光点的电流，最多同时显示 3 层 */
function drawFlow() {
  const svg = $('#flowLayer');
  if (!svg) return;
  const linesEl = $('#lines');
  if (!linesEl) return;
  const h = Math.max(linesEl.scrollHeight, 1);
  const w = Math.max(linesEl.clientWidth, 1);

  const frames = [];
  for (let i = S.stack.length - 1; i >= 0 && frames.length < 3; i--) {
    const sp = S.spans.get(S.stack[i]);
    if (sp && sp.isUser && sp.func !== '<模块>') frames.unshift(sp);
  }
  const key = frames.map((f) => f.id).join(',') + '|' + h + 'x' + w;
  if (key === S.flowKey) return;
  S.flowKey = key;

  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.style.height = h + 'px';
  if (!frames.length) { svg.innerHTML = ''; return; }

  let out = `<defs><linearGradient id="flowGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#a78bfa"/></linearGradient></defs>`;
  frames.forEach((sp, i) => {
    if (!sp.callerLine || !sp.line || sp.callerLine === sp.line) return;
    const y1 = lineCenter(sp.callerLine);
    const y2 = lineCenter(sp.line);
    if (y1 == null || y2 == null) return;
    const x = 9 + i * 6;
    const d = `M ${x} ${y1} C ${x - 5} ${y1}, ${x - 5} ${y2}, ${x} ${y2}`;
    out += `<path class="flow-path ghost" d="${d}"/>`;
    out += `<path class="flow-path" style="animation-delay:${(i * 0.12).toFixed(2)}s" d="${d}"/>`;
    out += `<circle class="flow-node" cx="${x}" cy="${y1}" r="3"/>`;
    out += `<circle class="flow-node ret" cx="${x}" cy="${y2}" r="3"/>`;
  });
  svg.innerHTML = out;
}


/* ---------------------------- 数据流粒子（入参 / 出参） ---------------------------- */
const FX = { items: [], raf: 0, prev: 0, lastIn: 0, lastOut: 0, silent: false };

function clip(s, n) {
  s = String(s == null ? '' : s).replace(/\s+/g, ' ');
  return s.length > n ? s.slice(0, n) + '…' : s;
}
function lineCenterOf(n) {
  const el = document.querySelector(`#lines .ln[data-l="${n}"]`);
  return el ? { x: 10, y: el.offsetTop + el.offsetHeight / 2 } : null;
}
function bez(p0, c, p1, t) {
  const m = 1 - t;
  return { x: m * m * p0.x + 2 * m * t * c.x + t * t * p1.x, y: m * m * p0.y + 2 * m * t * c.y + t * t * p1.y };
}

/** 调用时：参数从调用行流入函数体；返回时：结果从函数体流回调用行 */
function spawnValueFlow(sp, kind) {
  if (FX.silent) return;
  if (FX.items.length >= 15) return;
  const now = performance.now();
  if (kind === 'in') { if (now - FX.lastIn < 80) return; } else if (now - FX.lastOut < 80) return;
  const a = lineCenterOf(kind === 'in' ? sp.callerLine : sp.line);
  const b = lineCenterOf(kind === 'in' ? sp.line : sp.callerLine);
  if (!a || !b) return;
  if (kind === 'in') FX.lastIn = now; else FX.lastOut = now;

  const label = kind === 'in'
    ? (sp.args && sp.args.length
      ? sp.args.map((x) => `${x.name}=${clip(x.v.repr, 16)}`).join(', ')
      : '无入参')
    : clip(sp.ret ? sp.ret.repr : 'None', 44);

  const host = $('#editor');
  if (!host) return;
  for (let i = 0; i < 3; i++) {
    const el = document.createElement('div');
    el.className = 'fx-p' + (kind === 'out' ? ' out' : '');
    el.style.opacity = '0';
    if (i === 0) {
      const em = document.createElement('em');
      em.className = kind === 'out' ? 'ret' : '';
      em.textContent = (kind === 'in' ? '↓ ' : '↑ ') + (clip(label, 40) || '返回');
      el.appendChild(em);
    }
    host.appendChild(el);
    // 兜底：rAF 被节流（标签页切走/卡顿）时，气泡绝不能挂在半空
    setTimeout(() => {
      FX.items = FX.items.filter((x) => x.el !== el);
      if (el.isConnected) el.remove();
    }, 2600);
    FX.items.push({
      el, t: -i * 0.13, dur: 0.62,
      p0: a, p1: b, c: { x: a.x - 6, y: (a.y + b.y) / 2 }, kind,
    });
  }
  if (!FX.raf) { FX.prev = 0; FX.raf = requestAnimationFrame(tickFx); }
}

function tickFx(ts) {
  const dt = FX.prev ? Math.min(64, ts - FX.prev) : 16;
  FX.prev = ts;
  const host = $('#editor');
  FX.items = FX.items.filter((it) => {
    it.t += dt / (it.dur * 1000);
    const tt = Math.max(0, Math.min(1, it.t));
    const p = bez(it.p0, it.c, it.p1, tt);
    it.el.style.transform = `translate(${p.x}px,${p.y}px) translate(-50%,-50%)`;
    it.el.style.opacity = it.t < 0 ? '0' : (it.t > 0.86 ? String(Math.max(0, (1 - it.t) / 0.14)) : '1');
    if (it.t >= 1) {
      it.el.remove();
      if (host) rippleAt(host, p, it.kind);
      return false;
    }
    return true;
  });
  if (FX.items.length) FX.raf = requestAnimationFrame(tickFx);
  else { FX.raf = 0; FX.prev = 0; }
}

function rippleAt(host, p, kind) {
  const r = document.createElement('div');
  r.className = 'fx-ripple' + (kind === 'out' ? ' out' : '');
  r.style.setProperty('--x', p.x + 'px');
  r.style.setProperty('--y', p.y + 'px');
  host.appendChild(r);
  setTimeout(() => r.remove(), 700);
}

/* ---------------------------- 执行舞台：变量的诞生与流动 ---------------------------- */
const STG = {
  vars: new Map(),        // 当前作用域变量 name -> value
  scope: null,            // 当前帧 id
  stmt: null,             // 当前语句语义
  queue: [], busy: false, // 串行演出队列
  out: [],                // 舞台输出区
  silent: false,          // 回放重建时不演出
  gen: 0,                 // 代数：作用域切换/重置时作废旧动画
  animOff: false,          // 用户关闭逐帧动画
  lastLine: new Map(),     // 每帧上一条 line 事件的行号
  pace: 1,                 // 演出速度因子（事件多时 <1，动画加速）
  autoPace: 1,             // 按事件量自动计算的节奏（用户调速在此基础上缩放）
  userSpeed: 1,            // 用户手动调速：0.5 慢放 / 2、3 加速
  stepMode: false,         // 单步前进模式：舞台直接展示当前高亮行，不做上一行归并
  sampleN: 1,              // line 事件采样步长（事件很多时只演示关键帧）
  lineCnt: 0,
  activeNames: new Set(),
};

const OPS = { Add: '+', Sub: '−', Mult: '×', Div: '÷', FloorDiv: '//', Mod: '%', Pow: '**' };

function prettyVal(v) {
  if (!v) return null;
  if (/^<(list_iterator|tuple_iterator|set_iterator|dict_\w*iterator|generator|map|filter|zip|enumerate|range_\w*)/.test(v.repr || '')) return '⟳ 迭代器';
  let mm = /^<module\s+['"]([\w.]+)['"]/.exec(v.repr || '');
  if (mm) return '📦 ' + mm[1];
  if (v.kind === 'callable') {
    const m = /<(?:bound method|function|class)\s+([\w.]+)/.exec(v.repr || '');
    if (m) return m[1] + '()';
    return 'ƒ';
  }
  return v.repr;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- 项目入口识别：无入口文件 → 跨文件找"该先看谁" ---------- */
async function maybeEntrySuggest() {
  try {
    if (!S.code || !S.projectFiles || S.projectFiles.length < 2) return;
    if (/if\s+__name__\s*==\s*["']__main__["']/.test(S.code)) return;
    // 项目扫描已确认这是"被别人 import 的模块"→ 阅读卡已经说清用途和出处，
    // 不再叠一张"该先看谁"的卡（两张卡讲同一件事，新手会晕）
    const mm = mapOf(S.filename);
    if (mm && mm.kind === 'module' && (mm.imported_by || []).length) return;
    const r = await (await fetch('/api/entry_suggest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current: S.filename, files: S.projectFiles }),
    })).json();
    const sug = (r.suggestions || []).filter((x) => x.score > 0).slice(0, 4);
    // 当前文件自身的入口形态说明：顶层直接执行的命令行脚本（如 Tools 里的 2to3-script.py）
    const self = r.self || {};
    if (!sug.length && !self.kind) return;
    let selfCard = '';
    if (self.kind === 'module') {
      selfCard = `<div class="es-why">💡 <b>这个文件本身就是入口</b>：它没有 <code>__main__</code> 块，`
        + `但顶层就有直接执行的调用（import 完立刻运行）——直接点「运行」就能看到它的逐行执行。`
        + (self.cli_tool ? '它用 argparse 接收命令行参数：直接跑只会打印用法说明就退出，'
          + '请在顶栏<b>「参数」</b>里填上参数（如 -h）再运行。' : '')
        + `</div>`;
    }
    const rows = sug.map((s) => {
      const tags = [];
      if (s.entry) tags.push(`__main__ 入口 · 第 ${s.entry_line} 行`);
      else if (s.kind === 'module') tags.push('顶层直接执行（无 __main__ 块也能跑）');
      if (s.cli_tool) tags.push('命令行工具 · 要在顶栏「参数」填参数');
      if (s.imports_current) tags.push('import 了本文件');
      if (s.calls && s.calls.length) tags.push(`调用 ${(s.calls).join(' / ')}`);
      return `<div class="es-row">
        <span class="es-name">📄 ${esc(s.name)}</span>
        <span class="es-tags">${tags.map((t) => `<i>${esc(t)}</i>`).join('')}</span>
        <button class="es-btn" data-esopen="${esc(s.name)}">打开</button>
        <button class="es-btn pri" data-esrun="${esc(s.name)}">从它运行</button>
      </div>`;
    }).join('');
    // 常驻附加卡：教学卡每行都会刷新，把入口识别叠在它下面就不会被覆盖
    S.entryCard = `<div class="es-card">
      ${selfCard}
      ${rows ? `<div class="es-title">这个文件没有程序入口 —— 新手该先看谁？</div>
      <div class="es-why">刚才只执行了 import 和函数定义：Python 不会自动调用这些函数，`
        + `${esc((r.cur_defs || []).slice(0, 3).join('、') || '里面的函数')} 还没被用到。谁在调用它们，答案在项目里：</div>
      ${rows}
      <div class="es-why">从带 main 的入口文件开始，顺着调用链往下读，就能读懂整个项目。</div>` : ''}
    </div>`;
    const body = $('#stageExplainBody');
    showExplain(body && body.innerHTML ? body.innerHTML + S.entryCard : S.entryCard);
  } catch (e) { /* 静默：识别失败不影响主流程 */ }
}

function bindEntryButtons() {
  if (!S.entryCard) return;
  $$('#stageExplainBody [data-esopen]').forEach((b) => {
    b.onclick = () => openTreeFile(b.dataset.esopen);
  });
  $$('#stageExplainBody [data-esrun]').forEach((b) => {
    b.onclick = () => {
      openTreeFile(b.dataset.esrun);
      setTimeout(() => { const run = $('#btnRun'); if (run) run.click(); }, 500);
    };
  });
}

/* ---------- 舞台说明卡容器 ---------- */
function showExplain(html) {
  const el = $('#stageExplain');
  if (!el) return;
  // 面包屑（跨文件跟随）→ 阅读卡（这个文件是什么）→ 教学卡 → 入口识别卡
  const clean = String(html).replace(/<div class="es-card read-card">[\s\S]*?<\/div>\s*<\/div>/g, '')
    .replace(/<div class="crumb-bar">[\s\S]*?<\/div>/g, '');
  $('#stageExplainBody').innerHTML = (S.crumbHtml || '') + (S.readCard || '') + clean + (S.entryCard || '');
  el.hidden = false;
  bindEntryButtons();
  bindCrumbs();
}
function hideExplain() {
  const el = $('#stageExplain');
  if (el) el.hidden = true;
}

/* ---------- 教学卡片：做什么 / 为什么 / 结果 ---------- */
const TEACH_WHY = {
  assign: '= 是「赋值」不是「相等」：先算右边，再把结果放进左边的变量',
  augassign: '先读出变量原来的值，算完再写回同一个变量（不是新建变量）',
  print: 'print 把括号里的内容送到屏幕，它不改变任何变量',
  call: '括号里的实参按顺序传给形参；函数跑完把 return 的值交回这一行',
  return: 'return 立刻结束函数，把值交给调用它的那一行，后面的代码不再执行',
  for: 'for 会把序列从头取到尾，每取一个值就执行一次循环体；取完才往下走',
  while: 'while 每轮开始都重新判断一次条件，条件变假就退出循环',
  branch: '只走其中一个分支：条件为真走 if 里面，为假走 else（或直接跳过）',
  import: 'import 只是把模块加载进来，你自己的业务代码这时还没开始跑',
  def: 'def 只是把函数「登记」下来，函数体要等被调用时才真正执行',
  class: 'class 是创建对象的模板；真正创建对象是写「类名(...)」的时候',
  try: 'try 里的代码出错时不崩，直接跳到能处理这个错误的 except 继续',
  raise: 'raise 主动抛出异常，立刻打断正常流程，交给外层 except 处理',
  assert: 'assert 用来检查「这里必须成立」，不成立就立刻报错',
  block: 'with 进入时申请资源，离开时自动收尾（比如自动关文件）',
  stmt: '这一行按顺序执行完，才会走到下一行',
};

/** 把 flow 事件翻译成一句人话 */
function flowText(ev) {
  const k = ev.kind;
  if (k === 'cond') return `条件 ${ev.text} 算出来是 ${ev.repr} ${ev.value ? '→ 成立，进入 if 里面的语句' : '→ 不成立，跳过 if 里面的语句'}`;
  if (k === 'while') return `条件 ${ev.text} 算出来是 ${ev.repr}${ev.value ? ' → 成立，再来一轮' : ' → 不成立，退出循环'}`;
  if (k === 'loop') return `第 ${ev.iter} 轮：${ev.target || '循环变量'} = ${ev.repr}`;
  if (k === 'ret') return `算出 ${ev.text} = ${ev.repr}，作为返回值交回调用处`;
  if (k === 'assign') return `算出 ${ev.text} = ${ev.repr}，存进 ${ev.target}`;
  if (k === 'print') return `屏幕输出：${ev.text}`;
  return '';
}

/** 舞台顶部说明卡：做什么 / 为什么 / 结果 三件套 */
function teachFor(ev, stmt) {
  const what = (ev && ev.explain) || (stmt && stmt.explain) || '';
  const why = (stmt && TEACH_WHY[stmt.kind]) || '';
  const kindTxt = (stmt && {
    assign: '变量赋值', augassign: '原地累加', print: '输出', call: '函数调用',
    return: '返回', for: '循环', branch: '条件判断', import: '导入', def: '定义函数',
    class: '定义类', try: '异常处理', raise: '抛出异常', assert: '断言', block: '代码块',
  }[stmt.kind]) || '';
  if (!what && !why) { hideExplain(); return; }
  const lineNo = (ev && ev.line) || (stmt && stmt.line) || '';
  const cached = S.teachByLine.get(lineNo);
  const res = cached || (S.teachRes && S.teachLine === lineNo ? S.teachRes : '');
  const sameLine = S.teachLine === lineNo;
  S.teachLine = lineNo;
  S.teachShownLine = lineNo;
  // 跨文件跟随：这一行调用了项目内某个模块的函数 → 给一个「进入」入口
  const jumps = (mapOf(S.filename) || {}).calls || {};
  const jumpList = jumps[String(lineNo)] || [];
  const jumpHtml = jumpList.length
    ? `<div class="tr jump"><b>往下看</b><i>` + jumpList.slice(0, 3).map((j) =>
        `<button class="es-btn" data-jump="${esc(j.file)}" data-jline="${esc(String(j.line))}">进入 ${esc(j.label)}() →</button>`
      ).join(' ') + `<br/><span class="muted">它在 ${esc(jumpList[0].file)} 里定义</span></i></div>`
    : '';
  showExplain(
    `<div class="teach">`
    + `<div class="teach-h"><span class="tl">第 ${lineNo} 行</span>`
    + (kindTxt ? `<span class="tp">${esc(kindTxt)}</span>` : '') + `</div>`
    + (what ? `<div class="tr" style="--i:0"><b>做什么</b><i>${esc(what)}</i></div>` : '')
    + (why ? `<div class="tr" style="--i:1"><b>为什么</b><i>${esc(why)}</i></div>` : '')
    + `<div class="tr res" style="--i:2"><b>结果</b><i id="teachRes">${res ? esc(res) : '<span class="muted">执行到这一行时揭晓…</span>'}</i></div>`
    + `<div class="subs" id="teachSubs" hidden></div>`
    + jumpHtml
    + `</div>`
  );
  renderSubs(lineNo);
  $$('#stageExplainBody [data-jump]').forEach((b) => {
    b.onclick = () => navInto(b.dataset.jump, parseInt(b.dataset.jline || '1', 10));
  });
  if (!sameLine) {
    // 换行时给头部一道扫光，眼睛有个锚点
    const head = $('#stageHead');
    if (head) {
      head.classList.remove('refresh');
      void head.offsetWidth;
      head.classList.add('refresh');
      setTimeout(() => head.classList.remove('refresh'), 660);
    }
  }
}
/** 优先用事件自带的 explain，其次用静态语义里的 explain */
function explainFor(ev, stmt) {
  teachFor(ev, stmt);
}
/* ---- 表达式级子值（birdseye 式）：把一条语句的中间值按求值顺序摊开 ---- */
function collectSub(ev) {
  const line = ev.line;
  let arr = S.subs.get(line);
  if (!arr) { arr = []; S.subs.set(line, arr); }
  const item = { pid: ev.pid, depth: ev.depth || 0, text: ev.text || '', repr: ev.repr };
  // 同一插桩点在循环里会重复触发：保留位置、更新为最新一轮的值，避免列表无限变长
  const i = arr.findIndex((x) => x.pid === ev.pid);
  if (i >= 0) arr[i] = item; else arr.push(item);
  if (S.subs.size > 300) { const k = S.subs.keys().next().value; S.subs.delete(k); }
  if (S.teachLine === line) renderSubs(line);
}

function renderSubs(line) {
  const box = $('#teachSubs');
  if (!box) return;
  const arr = S.subs.get(line) || [];
  if (!arr.length) { box.hidden = true; box.innerHTML = ''; return; }
  const maxD = Math.max(...arr.map((x) => x.depth || 0), 0);
  box.hidden = false;
  box.innerHTML = `<div class="sub-h">算式拆解<span>按求值顺序 · 最后一行是整条表达式</span></div>`
    + arr.map((x, i) => {
      const last = i === arr.length - 1;
      const ind = Math.min(maxD, x.depth || 0) * 13;
      return `<div class="sub-i${last ? ' final' : ''}" style="padding-left:${ind}px">`
        + `<code>${esc(x.text)}</code><span class="arr">→</span><b>${esc(x.repr)}</b></div>`;
    }).join('');
}

/** flow 事件到达：把「条件真假 / 第几轮 / 算出来多少」写进结果区，并飞一个徽标过来 */
function teachFlow(ev) {
  if (ev.kind === 'sub') { collectSub(ev); return; }
  const txt = flowText(ev);
  if (!txt) return;
  // 按行缓存：来回跳行（递归、循环体）时，每行的结果不会互相覆盖
  S.teachByLine.set(ev.line, txt);
  if (S.teachLine === ev.line) {
    S.teachRes = txt;
    const el = $('#teachRes');
    if (el) {
      el.textContent = txt;
      el.classList.remove('reveal');
      void el.offsetWidth;
      el.classList.add('reveal');
    }
    flyFlowBadge(ev, el);
  }
  const last = S.flowLog[S.flowLog.length - 1];
  if (!last || last.text !== txt || last.line !== ev.line) {
    S.flowLog.push({ line: ev.line, text: txt, kind: ev.kind });
    if (S.flowLog.length > 400) S.flowLog.shift();
  }
}

/** 条件真假 / 循环轮次 → 一个短徽标从代码行飞到结果行 */
function flyFlowBadge(ev, target) {
  if (!target || STG.animOff) return;
  const spec = flowBadge(ev);
  if (!spec) return;
  if (STG.pace > 3.2) return;                       // 事件太密时省略，避免刷屏
  const lineEl = document.querySelector(`#lines .ln[data-l="${ev.line}"]`);
  flyBadge(lineEl, target, spec.text, spec.cls, { dur: 420 * Math.max(0.5, STG.pace) });
  if (spec.cls === 'loop') {
    const cv = document.querySelector('.stage-canvas');
    if (cv) { cv.classList.remove('loop'); void cv.offsetWidth; cv.classList.add('loop'); }
  }
}

/** 从 flow 事件提炼一个可盖章的短徽标 */
function flowBadge(ev) {
  const k = ev.kind;
  if (k === 'cond') return { text: ev.value ? '✓ True' : '✗ False', cls: ev.value ? '' : 'no' };
  if (k === 'while') return { text: ev.value ? '✓ 再来一轮' : '✗ 退出循环', cls: ev.value ? '' : 'no' };
  if (k === 'loop') return { text: `第 ${ev.iter} 轮`, cls: 'loop' };
  if (k === 'ret') return { text: `return ${clip(ev.repr, 14)}`, cls: 'ret' };
  if (k === 'assign') return { text: `${clip(ev.target || '', 10)} = ${clip(ev.repr, 14)}`, cls: '' };
  if (k === 'print') return { text: '输出', cls: 'loop' };
  return null;
}

/** 应用用户调速：值越大动画越快（pace 反比缩小），最小 0.08 防止 0 延迟 */
function applyUserSpeed() {
  STG.pace = Math.max(0.08, (STG.autoPace || 1) / (STG.userSpeed || 1));
}

function stageReset() {
  STG.vars.clear(); STG.vars = new Map();
  STG.scope = null; STG.stmt = null; STG.queue.length = 0; STG.busy = false;
  STG.out = []; STG.gen++; STG.activeNames = new Set(); STG.lastLine = new Map();
  STG.autoPace = 1; STG.sampleN = 1; STG.lineCnt = 0;
  applyUserSpeed();
  const v = $('#stageVars'); if (v) v.innerHTML = '';
  const e = $('#stageExpr'); if (e) { e.className = 'expr'; e.innerHTML = ''; }
  const o = $('#stageOutLines'); if (o) o.innerHTML = '';
  const f = $('#stageFx'); if (f) f.innerHTML = '';
  const h = $('#stageHead'); if (h) h.classList.remove('live');
  const sn = $('#shNo'); if (sn) sn.textContent = '—';
  const sc = $('#shCode'); if (sc) sc.textContent = '运行代码，这里会逐行演示变量的产生与计算过程';
  const sb = $('#shBadge'); if (sb) { sb.textContent = ''; sb.className = 'sh-badge'; }
  const ssc = $('#stageScope'); if (ssc) ssc.innerHTML = '';
  hideExplain();
  const si = $('#stageIn'); if (si) si.innerHTML = '<span class="in-ph">—</span>';
  const ob = $('#stageOutPh'); if (ob) ob.style.display = '';
  const sm = $('#stageMeta'); if (sm) sm.textContent = '等待运行';
  $('#stageEmpty') && $('#stageEmpty').classList.remove('hide');
}

/* ---------- 飞行气泡 ---------- */
function fxHost() { return $('#stageFx'); }

function elCenter(el) {
  const h = fxHost();
  if (!el || !h) return null;
  const a = el.getBoundingClientRect(), b = h.getBoundingClientRect();
  if (!a.width && !a.height) return null;
  return { x: a.left + a.width / 2 - b.left, y: a.top + a.height / 2 - b.top };
}

function drawTrail(p0, p1) {
  const host = fxHost();
  if (!host) return null;
  let svg = host.querySelector('.fx-trail');
  if (!svg) {
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'fx-trail');
    host.appendChild(svg);
  }
  const w = host.clientWidth, h = host.clientHeight;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
  const cx = (p0.x + p1.x) / 2, cy = Math.min(p0.y, p1.y) - 26;
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', `M ${p0.x} ${p0.y} Q ${cx} ${cy} ${p1.x} ${p1.y}`);
  path.style.opacity = '0.9';
  svg.appendChild(path);
  setTimeout(() => { path.style.transition = 'opacity .35s'; path.style.opacity = '0'; }, 620);
  setTimeout(() => path.remove(), 1000);
  return path;
}

function flyToken(fromEl, toEl, text, cls, opts) {
  const o = opts || {};
  const host = fxHost();
  const a = fromEl ? elCenter(fromEl) : (o.from || null);
  const b = toEl ? elCenter(toEl) : (o.to || null);
  if (!host || !b) return Promise.resolve();
  const p0 = a || { x: b.x, y: b.y + 90 };

  const el = document.createElement('div');
  el.className = 'fx-token ' + (cls || '');
  el.textContent = String(text == null ? '' : text);
  el.style.transform = `translate(${p0.x}px,${p0.y}px) translate(-50%,-50%)`;
  host.appendChild(el);
  drawTrail(p0, b);

  const mid = { x: (p0.x + b.x) / 2, y: Math.min(p0.y, b.y) - 26 };
  const dur = o.dur || 560;
  const anim = el.animate([
    { transform: `translate(${p0.x}px,${p0.y}px) translate(-50%,-50%) scale(.55)`, opacity: 0 },
    { transform: `translate(${p0.x}px,${p0.y - 10}px) translate(-50%,-50%) scale(1.06)`, opacity: 1, offset: 0.18 },
    { transform: `translate(${mid.x}px,${mid.y}px) translate(-50%,-50%) scale(1)`, opacity: 1, offset: 0.62 },
    { transform: `translate(${b.x}px,${b.y}px) translate(-50%,-50%) scale(.92)`, opacity: 1 },
  ], { duration: dur, easing: 'cubic-bezier(.35,.85,.3,1)', fill: 'forwards' });

  return new Promise((resolve) => {
    anim.onfinish = () => {
      el.style.opacity = '0';
      el.style.transition = 'opacity .18s';
      setTimeout(() => el.remove(), 200);
      if (toEl) landAt(toEl, b);
      resolve();
    };
    setTimeout(resolve, dur + 400);
  });
}

/** 值落到目标元素上：目标轻弹一下 + 一圈扩散波纹（让"到达"这件事有落点） */
function landAt(el, p) {
  if (!el) return;
  el.classList.remove('fx-land');
  void el.offsetWidth;
  el.classList.add('fx-land');
  setTimeout(() => el.classList.remove('fx-land'), 520);
  const host = fxHost();
  if (!host || STG.animOff) return;
  const c = p || elCenter(el);
  if (!c) return;
  const r = document.createElement('div');
  r.className = 'fx-ring';
  r.style.left = c.x + 'px';
  r.style.top = c.y + 'px';
  host.appendChild(r);
  setTimeout(() => r.remove(), 660);
}

/** 把 fx 层的节点数控制住：连跑长循环时不至于堆出几百个 DOM */
function fxTrim(keep) {
  const host = fxHost();
  if (!host) return;
  const kids = [...host.querySelectorAll('.fx-token,.fx-badge,.fx-ring')];
  const max = keep || 48;
  for (let i = 0; i < kids.length - max; i++) kids[i].remove();
}

/** 徽标飞入：条件真假 / 循环第几轮 / 返回值，盖到目标位置上 */
function flyBadge(fromEl, toEl, text, cls, opts) {
  const o = opts || {};
  const host = fxHost();
  if (!host) return Promise.resolve();
  const a = fromEl ? elCenter(fromEl) : (o.from || null);
  const b = toEl ? elCenter(toEl) : (o.to || null);
  if (!b) return Promise.resolve();
  const p0 = a || { x: b.x - 120, y: b.y + 40 };

  const el = document.createElement('div');
  el.className = 'fx-badge ' + (cls || '');
  el.textContent = String(text == null ? '' : text);
  el.style.transform = `translate(${p0.x}px,${p0.y}px) translate(-50%,-50%) scale(.7)`;
  host.appendChild(el);
  fxTrim();

  const mid = { x: (p0.x + b.x) / 2, y: Math.min(p0.y, b.y) - 34 };
  const dur = o.dur || 520;
  const anim = el.animate([
    { transform: `translate(${p0.x}px,${p0.y}px) translate(-50%,-50%) scale(.6) rotate(-12deg)`, opacity: 0 },
    { transform: `translate(${p0.x}px,${p0.y - 14}px) translate(-50%,-50%) scale(1.08) rotate(2deg)`, opacity: 1, offset: 0.2 },
    { transform: `translate(${mid.x}px,${mid.y}px) translate(-50%,-50%) scale(1) rotate(0)`, opacity: 1, offset: 0.6 },
    { transform: `translate(${b.x}px,${b.y}px) translate(-50%,-50%) scale(.9)`, opacity: 1 },
  ], { duration: dur, easing: 'cubic-bezier(.3,.9,.3,1)', fill: 'forwards' });

  return new Promise((resolve) => {
    anim.onfinish = () => {
      el.remove();
      if (toEl) landAt(toEl, b);
      resolve();
    };
    setTimeout(resolve, dur + 380);
  });
}

/* ---------- 变量卡片 ---------- */
function varEl(name) {
  const el = document.querySelector(`#stageVars .var[data-name="${CSS.escape(name)}"]`);
  // 已标记 dying 的卡片即将被定时器移走，不能复用——否则重建会在 220ms 内被误删
  // （rebuildTo 在多帧之间切换时，旧帧卡片标记删除后可能又被新帧同名复用）
  return (el && el.dataset.dying) ? null : el;
}

/* 这些 kind 的值「指着别的对象」，值得画引用图；基本类型没有引用结构 */
const REF_KINDS = new Set(['sequence', 'mapping', 'object', 'ndarray', 'dataframe']);

function varCard(name, v, opts) {
  const o = opts || {};
  const el = document.createElement('div');
  el.className = 'var' + (o.ghost ? ' ghost' : '');
  el.dataset.name = name;
  if (v && v.kind) el.dataset.kind = v.kind;
  const hasRef = v && REF_KINDS.has(v.kind);
  el.innerHTML = `<span class="vn">${esc(name)}</span>`
    + `<button class="ref${hasRef ? '' : ' hide'}" title="内存引用图：看这个值指向哪些对象、和谁共享">🔗</button>`
    + `<button class="vtt" title="时间旅行：看这个值的全部变化历史">⟲</button>`
    + `<span class="vv">${v ? esc(prettyVal(v)) : '<i class="vq">?</i>'}</span>`;
  return el;
}

/* ---------- 变量时间旅行：这个值什么时候被谁改的 ----------
   数据全部来自 S.events 里的 vars.changed 增量（runner 已带 old 旧值），
   不需要回源——按变量名过滤一遍就是完整轨迹；点击条目用 seek() 回到那一刻。 */
function collectVarTravel(name) {
  const rows = [];
  S.events.forEach((ev, ei) => {
    if (!ev.vars) return;
    (ev.vars.changed || []).forEach((c) => {
      if (c.name === name) rows.push({ ei, line: ev.line, old: c.old || null, v: c.v });
    });
    if ((ev.vars.removed || []).indexOf(name) >= 0) rows.push({ ei, line: ev.line, removed: true });
  });
  return rows;
}

function openVarTravel(name) {
  const panel = $('#vttPanel'), list = $('#vttList'), empty = $('#vttEmpty');
  if (!panel) return;
  $('#vttName').textContent = name;
  const rows = collectVarTravel(name);
  list.innerHTML = '';
  empty.hidden = rows.length > 0;
  rows.forEach((r, i) => {
    const row = document.createElement('div');
    row.className = 'vtt-i';
    const oldPart = r.removed
      ? `<span class="vtt-del">已删除</span>`
      : (r.old
          ? `<span class="vtt-old">${esc(r.old)}</span><span class="vtt-arrow">→</span><span class="vtt-new">${esc(r.v ? r.v.repr : '?')}</span>`
          : `<span class="vtt-new first">首次出现 = ${esc(r.v ? r.v.repr : '?')}</span>`);
    const typ = r.v && r.v.type ? ` <i style="color:var(--dim);font-style:normal;font-size:10px">${esc(r.v.type)}</i>` : '';
    row.innerHTML = `<span class="vtt-n">#${i + 1}</span>
      <div class="vtt-chg">${oldPart}${typ}</div>
      <span class="vtt-line">第 ${r.line} 行前</span>`;
    row.onclick = () => {
      panel.hidden = true;
      stopPlay();
      seek(r.ei + 1);              // 回到「这次变化刚发生」的那一刻
      toast(`已回到 ${name} 第 ${i + 1} 次变化那一刻（第 ${r.line} 行前）`, 'ok');
    };
    list.appendChild(row);
  });
  // 面板摆在变量卡片旁边（屏幕右缘兜底）
  const card = document.querySelector(`#stageVars .var[data-name="${CSS.escape(name)}"]`);
  if (card) {
    const r = card.getBoundingClientRect();
    panel.style.left = Math.max(8, Math.min(r.right - 60, window.innerWidth - 500)) + 'px';
    panel.style.top = Math.min(r.bottom + 8, window.innerHeight - 200) + 'px';
  } else {
    panel.style.left = '16px';
    panel.style.top = '64px';
  }
  panel.hidden = false;
}

function closeVarTravel() {
  const p = $('#vttPanel');
  if (p) p.hidden = true;
}

/* ---------- 内存引用图：这个值指向哪些对象、谁和谁共享（Python Tutor 式） ----------
   数据随 vars.changed / args 事件的 v.refs 增量携带（runner._ref_graph 生成），零回源；
   oid = id()（同帧快照内唯一）——前端用它检测别名：两个变量指着同一个对象。
   布局 = BFS 分层树：首次到达的边是实线，重复指向（共享/回边/自环）画橙色虚线。 */
function refsFor(name) {
  const m = S.frameVars.get(STG.scope);
  const v = m && m.get(name);
  return (v && v.refs && v.refs.nodes && v.refs.nodes.length) ? v.refs : null;
}

function refShortType(t) {
  const s = String(t || '?');
  return s.includes('.') ? s.split('.').pop() : s;
}

function renderRefsGraph(g, aliases) {
  const N = g.nodes.length;
  // 1) 从根 BFS：求每个节点的深度，同时标记「重复指向」的边（共享引用 → 虚线）
  const depth = new Array(N).fill(-1);
  depth[0] = 0;
  const bySrc = new Map();
  g.edges.forEach((e) => {
    if (!bySrc.has(e[0])) bySrc.set(e[0], []);
    bySrc.get(e[0]).push(e);
  });
  const reEdges = new Set();
  const seen = new Set([0]);
  const order = [0];
  for (let qi = 0; qi < order.length; qi++) {
    const cur = order[qi];
    (bySrc.get(cur) || []).forEach((e) => {
      if (seen.has(e[1])) reEdges.add(e);
      else { seen.add(e[1]); depth[e[1]] = depth[cur] + 1; order.push(e[1]); }
    });
  }
  // 2) 分层布局：列 = 深度，层内从上往下依次排
  const NW = 168, NH = 34, LW = 74, LH = 22, COL = 200, ROWG = 7, PAD = 14;
  const layers = [];
  order.forEach((idx) => {
    const d = depth[idx];
    if (!layers[d]) layers[d] = [];
    layers[d].push(idx);
  });
  const posX = new Array(N).fill(0), posY = new Array(N).fill(0);
  let W = 0, H = PAD;
  layers.forEach((ids, d) => {
    let y = PAD;
    ids.forEach((idx) => {
      const nd = g.nodes[idx];
      const h = nd.leaf ? LH : NH;
      posY[idx] = y;
      y += h + ROWG;
    });
    W = Math.max(W, d * COL + NW + PAD);
    const last = g.nodes[ids[ids.length - 1]];
    H = Math.max(H, posY[ids[ids.length - 1]] + (last.leaf ? LH : NH) + PAD);
  });
  // 3) 画边（先画边再画节点，节点盖在上面）
  const parts = [];
  g.edges.forEach((e) => {
    if (depth[e[1]] < 0) return;
    const s = g.nodes[e[0]], t = g.nodes[e[1]];
    const x1 = posX[e[0]] + (s.leaf ? LW : NW), y1 = posY[e[0]] + (s.leaf ? LH : NH) / 2;
    const x2 = posX[e[1]], y2 = posY[e[1]] + (t.leaf ? LH : NH) / 2;
    let d;
    if (e[0] === e[1]) {
      // 自环（循环引用指向自己）：右上角画一个小环
      d = `M ${x1} ${y1} C ${x1 + 46} ${y1 - 34}, ${x1 + 46} ${y1 + 30}, ${x1} ${y1 + 8}`;
      parts.push(`<g class="ref-e re"><path d="${d}"/><text x="${x1 + 34}" y="${y1 - 18}" text-anchor="middle">${esc(e[2])}</text></g>`);
      return;
    }
    d = `M ${x1} ${y1} C ${x1 + 34} ${y1}, ${x2 - 34} ${y2}, ${x2} ${y2}`;
    parts.push(`<g class="ref-e${reEdges.has(e) ? ' re' : ''}"><path d="${d}"/><text x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 4}" text-anchor="middle">${esc(e[2])}</text></g>`);
  });
  // 4) 画节点
  order.forEach((idx) => {
    const nd = g.nodes[idx];
    const isLeaf = !!nd.leaf, w = isLeaf ? LW : NW, h = isLeaf ? LH : NH;
    const cls = 'ref-n' + (isLeaf ? ' leaf' : '') + (idx === 0 ? ' root' : '') + (aliases.has(idx) ? ' alias' : '');
    const t2 = esc(String(nd.r).slice(0, isLeaf ? 10 : 21));
    const aliasTag = aliases.has(idx) ? `<text class="nt" x="${posX[idx] + w - 8}" y="${posY[idx] + 13}" text-anchor="end">= ${esc(aliases.get(idx).join('、'))}</text>` : '';
    parts.push(`<g class="${cls}"><rect x="${posX[idx]}" y="${posY[idx]}" width="${w}" height="${h}" rx="8"/>`
      + (isLeaf
        ? `<text x="${posX[idx] + 8}" y="${posY[idx] + h / 2 + 3.5}">${t2}</text>`
        : `<text class="nt" x="${posX[idx] + 8}" y="${posY[idx] + 13}">${esc(refShortType(nd.t))}</text><text x="${posX[idx] + 8}" y="${posY[idx] + 26}">${t2}</text>`)
      + aliasTag + `</g>`);
  });
  const sw = Math.max(W + 60, 300), sh = H;
  let html = `<svg width="${sw}" height="${sh}" viewBox="0 0 ${sw} ${sh}" xmlns="http://www.w3.org/2000/svg">${parts.join('')}</svg>`;
  if (g.trunc) html += `<div class="ref-note">对象太多，图已截断——只展示了部分引用</div>`;
  return html;
}

function openRefsGraph(name) {
  const g = refsFor(name);
  if (!g) {
    toast('这一刻没有记录到「这个值」的引用结构——引用图自 v2.8.0 起随事件记录，试着跑一次新任务', '');
    return;
  }
  $('#refsName').textContent = name;
  // 别名检测：当前帧其他变量的根对象，与图内非叶子节点比对（oid + repr 双重确认，防 id 复用误报）
  const aliases = new Map();
  const m = S.frameVars.get(STG.scope);
  if (m) {
    m.forEach((vv, other) => {
      if (other === name || !vv || !vv.refs || !vv.refs.nodes || !vv.refs.nodes.length) return;
      const r0 = vv.refs.nodes[0];
      if (!r0 || r0.leaf) return;
      g.nodes.forEach((nd, i) => {
        if (!nd.leaf && nd.oid === r0.oid && nd.r === r0.r) {
          if (!aliases.has(i)) aliases.set(i, []);
          if (aliases.get(i).indexOf(other) < 0) aliases.get(i).push(other);
        }
      });
    });
  }
  $('#refsBody').innerHTML = renderRefsGraph(g, aliases);
  const panel = $('#refsPanel');
  // 面板摆在变量卡片旁边（屏幕右缘兜底）
  const card = document.querySelector(`#stageVars .var[data-name="${CSS.escape(name)}"]`);
  if (card) {
    const r = card.getBoundingClientRect();
    panel.style.left = Math.max(8, Math.min(r.right - 60, window.innerWidth - 790)) + 'px';
    panel.style.top = Math.min(r.bottom + 8, window.innerHeight - 180) + 'px';
  } else {
    panel.style.left = '16px';
    panel.style.top = '64px';
  }
  panel.hidden = false;
}

function closeRefsGraph() {
  const p = $('#refsPanel');
  if (p) p.hidden = true;
}

function setCardValue(el, v, animate) {
  if (!el) return;
  const holder = el.querySelector('.vv');
  if (!holder) return;
  if (v && v.kind) el.dataset.kind = v.kind;
  // 值可能从基本类型变成容器（x = 5 → x = [1,2]）：同步引用图按钮的显隐
  const rb = el.querySelector('.ref');
  if (rb) rb.classList.toggle('hide', !(v && REF_KINDS.has(v.kind)));
  const old = (holder.textContent || '').trim();
  holder.innerHTML = v ? esc(prettyVal(v)) : '<i class="vq">?</i>';
  const nextTxt = v ? String(prettyVal(v)) : '';
  // 翻牌：旧值向上滑出、新值从下顶上来 —— 一眼看出"值变了"
  if (animate && old && nextTxt && old !== nextTxt) {
    const gh = document.createElement('span');
    gh.className = 'vv-ghost';
    gh.textContent = old;
    el.appendChild(gh);
    holder.classList.remove('flipin');
    void holder.offsetWidth;
    holder.classList.add('flipin');
    requestAnimationFrame(() => gh.classList.add('go'));
    setTimeout(() => gh.remove(), 480);
    el.classList.remove('vchanged');
    void el.offsetWidth;
    el.classList.add('vchanged');
    setTimeout(() => el.classList.remove('vchanged'), 640);
  } else if (v && animate) {
    holder.classList.remove('vland');
    void holder.offsetWidth;
    holder.classList.add('vland');
  }
}

/** 按当前变量表同步卡片结构（已有卡片保留 DOM，新变量先以 ? 出现） */
function stageSyncVars(snap) {
  const box = $('#stageVars');
  if (!box) return;
  const names = [...snap.vars.keys()];
  // 删除已消失（标记 dying：异步移走，复用方经由 varEl 会跳过它）
  [...box.children].forEach((el) => {
    if (!snap.vars.has(el.dataset.name) && !el.dataset.dying) {
      el.dataset.dying = '1';
      el.style.transition = 'opacity .2s,transform .2s';
      el.style.opacity = '0'; el.style.transform = 'scale(.9)';
      setTimeout(() => el.remove(), 220);
    }
  });
  names.forEach((name) => {
    let el = varEl(name);
    if (!el) {
      el = varCard(name, snap.newNames.has(name) ? null : snap.vars.get(name));
      box.appendChild(el);
    } else if (!snap.newNames.has(name)) {
      setCardValue(el, snap.vars.get(name), false);
    }
  });
}

function stageDim(except) {
  const keep = except || new Set();
  $$('#stageVars .var').forEach((el) => {
    const on = keep.has(el.dataset.name);
    el.classList.toggle('active', on);
    el.classList.toggle('dim', keep.size > 0 && !on);
  });
  // 输入块联动：本行用到的实参保持高亮，其余压暗
  $$('#stageIn .in-chip').forEach((el) => {
    const on = keep.has(el.dataset.name);
    el.classList.toggle('use', on);
    el.classList.toggle('dim', keep.size > 0 && !on);
  });
}

/* ---------- 输入块：当前作用域收到的实参 ---------- */
function stageInSet(title, args) {
  const box = $('#stageIn');
  if (!box) return;
  if (!args || !args.length) {
    box.innerHTML = `<span class="in-ph">${esc(title || '—')}</span>`;
    return;
  }
  box.innerHTML = args.map((a) => {
    const nm = /^\.?\d*$/.test(a.name) ? '⟳ 迭代对象' : a.name;
    return `<span class="in-chip" data-name="${esc(a.name)}"><i>${esc(nm)}</i> = <b>${esc(clip(prettyVal(a.v) || String(a.v && a.v.repr != null ? a.v.repr : a.v), 20))}</b></span>`;
  }).join('');
}

function stageInSync(frameId) {
  const sp = S.spans.get(frameId);
  if (sp && sp.isUser && sp.args && sp.args.length) {
    stageInSet(`${sp.func}() 收到的实参`, sp.args);
  } else if (sp && sp.isUser) {
    stageInSet(`${sp.func}()（无实参）`, null);
  } else {
    stageInSet('模块级：脚本直接启动，无入参', null);
  }
}

function stageClearDim() {
  $$('#stageVars .var').forEach((el) => el.classList.remove('dim', 'active'));
}

/* ---------- 头部 / 面包屑 ---------- */
function stageHead(stmt, line) {
  const head = $('#stageHead');
  if (!head) return;
  head.classList.add('live');
  $('#shNo').textContent = 'L' + line;
  $('#shCode').textContent = (stmt && stmt.text) || '';
  const badge = $('#shBadge');
  const label = {
    assign: '赋值', augassign: '运算赋值', print: '打印', return: '返回',
    call: '函数调用', for: '循环', branch: '分支', block: '块', stmt: '语句',
    import: '导入模块', def: '定义函数', class: '定义类',
    raise: '抛出异常', try: 'try 块', assert: '断言',
  }[stmt && stmt.kind] || '';
  badge.textContent = label;
  badge.className = 'sh-badge' + (label ? ' hot' : '');
}

let crumbIds = '';
function stageCrumbs(frameId) {
  const box = $('#stageScope');
  if (!box) return;
  const frames = [];
  for (let i = S.stack.length - 1; i >= 0 && frames.length < 4; i--) {
    const sp = S.spans.get(S.stack[i]);
    if (sp && sp.isUser) frames.unshift(sp);
  }
  const prev = new Set((crumbIds || '').split(',').filter(Boolean));
  crumbIds = frames.map((f) => String(f.id)).join(',');
  box.innerHTML = frames.map((f, i) => {
    const cur = i === frames.length - 1;
    const args = (f.args || []).map((a) => `${a.name}=${clip(prettyVal(a.v) || '', 10)}`).join(', ');
    const isNew = !prev.has(String(f.id));
    return `<span class="crumb${cur ? ' cur' : ''}${isNew ? ' enter' : ''}" data-fid="${f.id}">`
      + `${esc(f.func)}(${esc(args)})</span>`;
  }).join('<span class="crumb-sep">›</span>');
}

/* ---------- 表达式演出 ---------- */
function exprClear() {
  const e = $('#stageExpr');
  if (e) { e.className = 'expr'; e.innerHTML = '<span class="ph">·</span>'; }
}

function operandNode(n, snap, chips) {
  if (!n) return '<span class="opnd">?</span>';
  if (n.k === 'binop') {
    return operandNode(n.left, snap, chips)
      + `<span class="op">${OPS[n.op] || n.op}</span>`
      + operandNode(n.right, snap, chips);
  }
  if (n.k === 'unary') {
    return `<span class="op">${n.op === 'USub' ? '−' : (n.op === 'UAdd' ? '+' : n.op)}</span>`
      + operandNode(n.operand, snap, chips);
  }
  let c;
  if (n.k === 'name') {
    c = { kind: 'name', name: n.id, label: n.id, val: snap.vars.get(n.id) };
  } else if (n.k === 'attr') {
    c = { kind: 'attr', name: n.root || n.text, label: n.text, val: null };
  } else if (n.k === 'const' || n.k === 'lit') {
    c = { kind: 'lit', name: n.text, label: n.text, val: null };
  } else {
    c = { kind: 'expr', name: n.text || '?', label: (n.text != null ? n.text : '?'), val: null };
  }
  chips.push(c);
  const i = chips.length - 1;
  let valHtml = '';
  if (c.val) valHtml = ` <b>${esc(prettyVal(c.val) || '?')}</b>`;
  else if (c.kind === 'name' || c.kind === 'expr') valHtml = ' <b class="pq">?</b>';
  const dup = c.kind === 'lit' || c.kind === 'attr';
  return `<span class="opnd" data-opn="${i}" data-op="${esc(c.name || c.label)}" style="--i:${i}">${esc(c.label)}${dup ? '' : valHtml}</span>`;
}

function exprShell(stmt, snap) {
  const e = $('#stageExpr');
  const chips = [];
  if (!e) return chips;
  const rhs = stmt.rhs || null;
  e.className = 'expr hot';
  if (rhs && (rhs.k === 'binop' || rhs.k === 'unary')) {
    e.innerHTML = operandNode(rhs, snap, chips)
      + `<span class="eq">=</span><span class="res" data-res>?</span>`;
  } else if (rhs && rhs.k === 'name') {
    e.innerHTML = operandNode(rhs, snap, chips)
      + `<span class="eq">→</span><span class="res" data-res>?</span>`;
  } else {
    e.innerHTML = `<span class="opnd">${esc((rhs && rhs.text) || stmt.text)}</span>`
      + `<span class="eq">→</span><span class="res" data-res>?</span>`;
  }
  return chips;
}

/* ---------- 调用节点渲染：fn(参数...)，变量参数带值；返回 html 与可飞行芯片 ---------- */
function renderCallNode(c, snap, chips) {
  const argsHtml = (c.args || []).map((a) => {
    if (a && a.k === 'name' && snap.vars.has(a.id)) {
      chips.push({ kind: 'name', name: a.id, label: a.id, val: snap.vars.get(a.id) });
      const j = chips.length - 1;
      return `<span class="opnd" data-opn="${j}" data-op="${esc(a.id)}">${esc(a.id)} <b>${esc(prettyVal(snap.vars.get(a.id)) || '?')}</b></span>`;
    }
    if (a && a.k === 'call') {
      chips.push({ kind: 'attr', name: a.fn, label: `${a.fn}(…)`, val: null });
      const j = chips.length - 1;
      return `<span class="opnd imp" data-opn="${j}">${esc(a.fn)}(…)</span>`;
    }
    chips.push({ kind: 'lit', name: (a && a.text) || '?', label: (a && a.text) || '?', val: null });
    const j = chips.length - 1;
    return `<span class="opnd" data-opn="${j}">${esc((a && a.text) || '?')}</span>`;
  }).join('<span class="op">,</span> ');
  return `<span class="fnn">${esc(c.fn || '?')}</span>( ${argsHtml || '<span class="in-ph">无入参</span>'} )`;
}

/** 让芯片依次接收飞入的值；被作废（gen 变化）时返回 false */
async function flyChips(chips, g) {
  for (let i = 0; i < chips.length; i++) {
    const c = chips[i];
    const el = document.querySelector(`#stageExpr .opnd[data-opn="${i}"]`);
    if (!el) continue;
    if (c.val && c.kind === 'name' && varEl(c.name)) {
      flyToken(varEl(c.name), el, prettyVal(c.val) || '?', '');
    } else if (c.val) {
      flyToken(null, el, prettyVal(c.val) || '?', '', { from: { x: (elCenter(el) || { x: 0 }).x + 90, y: (elCenter(el) || { y: 0 }).y + 86 } });
    } else if (c.kind === 'attr' && c.name && varEl(c.name)) {
      flyToken(varEl(c.name), el, c.label, '');
    } else {
      flyToken($('#stageHead'), el, String(c.label), '');
    }
    await sleep(190 * STG.pace);
    if (g !== STG.gen) return false;
  }
  return true;
}

/* ---------- 主演出 ---------- */
/** 操作数都就位后，让运算符亮一下 —— 明确"是这一步把它们算到一起的" */
async function pulseOps(g) {
  const ops = $$('#stageExpr .op');
  for (const el of ops) {
    el.classList.remove('pulsing');
    void el.offsetWidth;
    el.classList.add('pulsing');
    setTimeout(() => el.classList.remove('pulsing'), 520);
    await sleep(130 * STG.pace);
    if (g !== STG.gen) return;
  }
}

function stageSnapshot(ev) {
  const frameId = ev.id;
  const full = S.frameVars.get(frameId) || new Map();
  const vars = new Map(full);
  const newNames = new Set((ev.vars && ev.vars.changed ? ev.vars.changed : []).map((c) => c.name));
  return { vars, newNames, frameId, line: ev.line, stmt: S.stmtMap.get(ev.line) || null };
}

function stageIngest(ev) {
  // 1) 更新该帧的变量表
  if (ev.vars) {
    let m = S.frameVars.get(ev.id);
    if (!m) { m = new Map(); S.frameVars.set(ev.id, m); }
    (ev.vars.changed || []).forEach((c) => m.set(c.name, c.v));
    (ev.vars.removed || []).forEach((n) => m.delete(n));
  }
  // 2) settrace 的 line 事件在"该行执行前"触发 → 本次变量变化其实属于上一行
  //    单步模式下不做归并偏移：舞台直接展示当前高亮行（变量快照已包含此前所有变化）
  const prev = STG.lastLine.get(ev.id);
  STG.lastLine.set(ev.id, ev.line);
  const hasChange = ev.vars && ((ev.vars.changed || []).length || (ev.vars.removed || []).length);
  if (hasChange && prev && !STG.stepMode) {
    stageOnLine({ ...ev, line: prev, vars: ev.vars, consumed: false, nextLine: ev.line });
  } else {
    stageOnLine({ ...ev, vars: null, nextLine: null });
  }
}

function stageFall(snap, ev) {
  STG.vars = snap.vars;
  stageSyncVars({ ...snap, newNames: new Set() });
  $$('#stageVars .var').forEach((el) => setCardValue(el, snap.vars.get(el.dataset.name), false));
  stageHead(snap.stmt, ev.line);
  explainFor(ev, snap.stmt);
  stageCrumbs(ev.id);
  stageInSync(ev.id);
  stageClearDim();
}

function stageOnLine(ev) {
  if (ev.line === undefined) return;
  const snap = stageSnapshot(ev);

  if (STG.silent || STG.animOff
      || (STG.sampleN > 1 && (++STG.lineCnt % STG.sampleN !== 0))) {   // 回放重建 / 关闭动画 / 采样跳帧：直接落到终态
    STG.scope = ev.id;
    STG.vars = snap.vars;
    stageSyncVars({ ...snap, newNames: new Set() });
    $$('#stageVars .var').forEach((el) => setCardValue(el, snap.vars.get(el.dataset.name), false));
    stageHead(snap.stmt, ev.line);
    stageCrumbs(ev.id);
    stageInSync(ev.id);
    if (snap.stmt) exprShell(snap.stmt, snap);
    const r = $('#stageExpr') && $('#stageExpr').querySelector('[data-res]');
    const tgt = (snap.stmt && snap.stmt.targets || [])[0];
    if (r) r.textContent = tgt && snap.vars.get(tgt) ? snap.vars.get(tgt).repr : (tgt ? '?' : '');
    stageClearDim();
    return;
  }

  // 作用域变化：清空舞台
  if (STG.scope !== ev.id) {
    STG.gen++;
    STG.scope = ev.id;
    STG.vars = new Map();
    const v = $('#stageVars'); if (v) v.innerHTML = '';
    exprClear();
    hideExplain();
    stageInSync(ev.id);
  }

  // 上一个动画还没演完、新事件又来了：作废旧代数，舞台立即落到当前行，
  // 保证右边舞台始终跟随左侧源码高亮（新动画重新排队，若下一个事件很快到来会被再次作废）
  if (STG.busy || STG.queue.length) {
    STG.gen++;
    STG.queue.length = 0;
    stageFall(snap, ev);
  }
  stageEnqueue(() => stageActLine(ev, snap));
}

async function stageActLine(ev, snap) {
  const g = STG.gen;
  const stmt = snap.stmt;
  STG.vars = snap.vars;
  $('#stageEmpty') && $('#stageEmpty').classList.add('hide');
  stageCrumbs(ev.id);
  stageHead(stmt, ev.line);
  stageSyncVars(snap);
  explainFor(ev, stmt);
  const sm = $('#stageMeta');
  if (sm) sm.textContent = ev.nextLine ? `第 ${ev.line} 行完成 → 进入 ${ev.nextLine} 行` : `第 ${ev.line} 行`;

  if (!stmt) { stageDim(new Set()); return; }

  const tgt = (stmt.targets || [])[0];

  /* --- 导入模块：明确展示「导入了什么」 --- */
  if (stmt.kind === 'import') {
    const mods = stmt.modules || [];
    const fromNames = stmt.names || [];
    const head = $('#stageHead');
    stageDim(new Set());
    const e = $('#stageExpr');
    if (e) {
      e.className = 'expr hot';
      e.innerHTML = mods.map((m) => `<span class="opnd imp">📦 ${esc(m)}</span>`).join(' ')
        + `<span class="eq">←</span><span class="res">导入模块</span>`;
    }
    await sleep(200 * STG.pace); if (g !== STG.gen) return;
    // 模块/名字逐个飞入，生成变量卡片
    const lands = fromNames.length ? fromNames : mods.map((m) => m.split('.')[0]);
    for (const nm of lands) {
      const card = varEl(nm) || $('#stageVars');
      if (head && card) flyToken(head, card, '📦 ' + nm, 'imp', { dur: 520 * STG.pace });
      await sleep(240 * STG.pace); if (g !== STG.gen) return;
    }
    // 新名字卡片创建时是 "?" 占位 —— 这里用本行的真实值补上（模块对象显示 📦 名字）
    for (const nm of lands) {
      const el = varEl(nm);
      const v = snap.vars.get(nm);
      if (el && v) setCardValue(el, v, false);
    }
    stageClearDim();
    return;
  }

  /* --- 定义函数：函数名 + 形参 + 默认值，必填参数先显示 ? --- */
  if (stmt.kind === 'def' || stmt.kind === 'class') {
    const head = $('#stageHead');
    stageDim(new Set());
    const e = $('#stageExpr');
    if (e) {
      const isDef = stmt.kind === 'def';
      const ps = isDef ? (stmt.params || []).map((p) => p.default != null
        ? `<span class="opnd defp" data-p="${esc(p.name)}">${esc(p.name)} = <b>${esc(p.default)}</b></span>`
        : `<span class="opnd defp req" data-p="${esc(p.name)}">${esc(p.name)} <b class="pq">?</b></span>`).join('<span class="op">,</span> ') : '';
      e.className = 'expr hot';
      e.innerHTML = isDef
        ? `<span class="kw">def</span> <span class="fnn">${esc(stmt.fn)}</span>( ${ps} )`
          + `<span class="eq">←</span><span class="res">定义函数</span>`
        : `<span class="kw">class</span> <span class="fnn">${esc(stmt.fn)}</span>`
          + `<span class="eq">←</span><span class="res">定义类</span>`;
      if (stmt.doc) {
        const d = document.createElement('div');
        d.className = 'expr-doc';
        d.textContent = `"""${stmt.doc}"""`;
        e.appendChild(d);
      }
    }
    await sleep(260 * STG.pace); if (g !== STG.gen) return;
    if (stmt.kind === 'def') {
      // 默认值逐个飞入形参芯片
      for (const p of (stmt.params || [])) {
        if (p.default == null) continue;
        const chip = e && e.querySelector(`[data-p="${CSS.escape(p.name)}"]`);
        if (chip && head) { await flyToken(head, chip, p.default, 'imp', { dur: 460 * STG.pace }); if (g !== STG.gen) return; }
      }
    }
    stageClearDim();
    return;
  }

  /* --- 赋值 / 运算赋值 --- */
  if (stmt.kind === 'assign' || stmt.kind === 'augassign') {
    const tEl = tgt ? varEl(tgt) : null;
    stageDim(new Set(tgt ? [tgt, ...(stmt.refs || []).filter((r) => snap.vars.has(r))] : []));
    const rhs = stmt.rhs || { k: 'expr', text: stmt.text };

    if (rhs.k === 'const' || rhs.k === 'lit') {
      await sleep(180 * STG.pace); if (g !== STG.gen) return;
      await flyToken(null, tEl, rhs.text, '', { from: { x: (elCenter(tEl) || { x: 0 }).x + 90, y: (elCenter(tEl) || { y: 0 }).y + 86 } });
      if (g !== STG.gen) return;
      setCardValue(tEl, snap.vars.get(tgt), true);
      exprClear();
    } else if (rhs.k === 'binop' || rhs.k === 'unary') {
      const chips = exprShell(stmt, snap);
      await sleep(160 * STG.pace); if (g !== STG.gen) return;
      // 每个操作数的值依次飞入芯片（变量从卡片飞入，字面量/属性从上方飞入）
      if (!await flyChips(chips, g)) return;
      await pulseOps(g); if (g !== STG.gen) return;
      await sleep(160 * STG.pace); if (g !== STG.gen) return;
      const res = snap.vars.get(tgt);
      const resEl = $('#stageExpr') && $('#stageExpr').querySelector('[data-res]');
      if (resEl && res) { resEl.textContent = res.repr; resEl.classList.remove('res'); void resEl.offsetWidth; resEl.classList.add('res'); }
      await sleep(260 * STG.pace); if (g !== STG.gen) return;
      await flyToken(resEl, tEl, res ? res.repr : '', 'res');
      if (g !== STG.gen) return;
      setCardValue(tEl, res, true);
    } else if (rhs.k === 'call') {
      // 调用赋值：先演调用的入参，再把返回值飞入目标卡片
      const e = $('#stageExpr');
      const chips = [];
      if (e) {
        e.className = 'expr hot';
        e.innerHTML = renderCallNode(rhs, snap, chips)
          + `<span class="eq">=</span><span class="res" data-res>?</span>`;
      }
      await sleep(180 * STG.pace); if (g !== STG.gen) return;
      if (!await flyChips(chips, g)) return;
      const res = snap.vars.get(tgt);
      const resEl = $('#stageExpr') && $('#stageExpr').querySelector('[data-res]');
      if (resEl && res) resEl.textContent = res.repr;
      await sleep(220 * STG.pace); if (g !== STG.gen) return;
      await flyToken(resEl, tEl, res ? res.repr : '', 'res');
      if (g !== STG.gen) return;
      setCardValue(tEl, res, true);
    } else if (rhs.k === 'name') {
      exprShell(stmt, snap);
      await sleep(160 * STG.pace); if (g !== STG.gen) return;
      const srcEl = varEl(rhs.id);
      const resEl = $('#stageExpr') && $('#stageExpr').querySelector('[data-res]');
      const res = snap.vars.get(tgt);
      if (resEl && res) resEl.textContent = res.repr;
      await flyToken(srcEl, tEl, res ? res.repr : '', 'res');
      if (g !== STG.gen) return;
      setCardValue(tEl, res, true);
    } else {
      await sleep(240 * STG.pace); if (g !== STG.gen) return;
      setCardValue(tEl, snap.vars.get(tgt), true);
      exprClear();
    }
    stageClearDim();
    if (tEl) { tEl.classList.add('active'); setTimeout(() => tEl && tEl.classList.remove('active'), 520); }
    return;
  }

  /* --- print：参数值飞入输出块 --- */
  if (stmt.kind === 'print') {
    const argIds = (stmt.refs || []).filter((r) => r !== 'print' && r !== 'pprint' && snap.vars.has(r));
    stageDim(new Set(argIds));
    const outBox = $('#stageOut');
    const e = $('#stageExpr');
    if (e) {
      // 算式带展示 print 的实参（变量带值，其余显示原文）
      const argHtml = (stmt.args && stmt.args.length ? stmt.args : (stmt.refs || []).slice(1).map((r) => ({ k: 'name', id: r })))
        .map((a) => {
          if (a && a.k === 'name' && snap.vars.has(a.id)) return `<span class="opnd">${esc(a.id)} <b>${esc(prettyVal(snap.vars.get(a.id)) || '?')}</b></span>`;
          return `<span class="opnd">${esc((a && a.text) || '?')}</span>`;
        }).join('<span class="op">,</span> ');
      e.innerHTML = `<span class="fnn">print</span>( ${argHtml} )<span class="eq">↳</span><span class="res">输出</span>`;
    }
    if (argIds.length && outBox) {
      await sleep(160 * STG.pace); if (g !== STG.gen) return;
      for (const id of argIds.slice(0, 3)) {
        const v = snap.vars.get(id);
        flyToken(varEl(id), outBox, v ? prettyVal(v) || v.repr : '', 'out', { dur: 520 * STG.pace });
        await sleep(160 * STG.pace); if (g !== STG.gen) return;
      }
      await sleep(240 * STG.pace);
    } else {
      await sleep(220 * STG.pace);
    }
    stageClearDim();
    return;
  }

  /* --- 函数调用语句：展示入参细节（logger / 自定义调用等） --- */
  if (stmt.kind === 'call') {
    stageDim(new Set((stmt.refs || []).filter((r) => snap.vars.has(r))));
    const e = $('#stageExpr');
    const chips = [];
    if (e) {
      e.className = 'expr hot';
      e.innerHTML = renderCallNode({ fn: stmt.fn, args: stmt.args || [] }, snap, chips)
        + `<span class="eq">↳</span><span class="res">调用</span>`;
    }
    await sleep(180 * STG.pace); if (g !== STG.gen) return;
    if (!await flyChips(chips, g)) return;
    await sleep(240 * STG.pace);
    stageClearDim();
    return;
  }
  /* --- return：返回值若来自一次函数调用，展示该调用的入参 --- */
  if (stmt.kind === 'return') {
    const rhs = stmt.rhs;
    stageDim(new Set((stmt.refs || []).filter((r) => snap.vars.has(r))));
    const e = $('#stageExpr');
    const chips = [];
    if (e) {
      e.className = 'expr hot';
      if (rhs && rhs.k === 'call') {
        // return round(s, 2)：先演 round 的入参，返回值由 return 事件落定
        e.innerHTML = renderCallNode(rhs, snap, chips)
          + `<span class="eq">↳</span><span class="res">返回 ?</span>`;
      } else if (rhs && (rhs.k === 'name' || rhs.k === 'binop' || rhs.k === 'unary')) {
        chips.push(...exprShell({ ...stmt, rhs }, snap));
      } else {
        e.innerHTML = `<span class="opnd">${esc((rhs && rhs.text) || 'return')}</span>`
          + `<span class="eq">↳</span><span class="res" data-res>?</span>`;
      }
    }
    await sleep(160 * STG.pace); if (g !== STG.gen) return;
    if (!await flyChips(chips, g)) return;
    await sleep(280 * STG.pace);
    stageClearDim();
    return;
  }
  /* --- raise / assert：异常路径的细节 --- */
  if (stmt.kind === 'raise' || stmt.kind === 'assert') {
    stageDim(new Set((stmt.refs || []).filter((r) => snap.vars.has(r))));
    const e = $('#stageExpr');
    const chips = [];
    if (e) {
      e.className = 'expr hot err';
      let inner = '<span class="in-ph">—</span>';
      if (stmt.rhs) {
        if (stmt.rhs.k === 'call') inner = renderCallNode(stmt.rhs, snap, chips);
        else inner = `<span class="opnd">${esc(stmt.rhs.text || '')}</span>`;
      }
      e.innerHTML = `<span class="raise-kw">${stmt.kind === 'raise' ? 'raise' : 'assert'}</span> ${inner}`
        + `<span class="eq">↳</span><span class="res err">${stmt.kind === 'raise' ? '抛出异常' : '断言失败'}</span>`;
    }
    await sleep(180 * STG.pace); if (g !== STG.gen) return;
    if (!await flyChips(chips, g)) return;
    await sleep(300 * STG.pace);
    stageClearDim();
    return;
  }
  /* --- try：进入受保护块 --- */
  if (stmt.kind === 'try') {
    const e = $('#stageExpr');
    if (e) {
      e.className = 'expr hot';
      e.innerHTML = `<span class="kw">try</span><span class="op">:</span>`
        + `<span class="opnd">进入受保护块，若抛出异常会走 except 分支</span>`;
    }
    await sleep(280 * STG.pace);
    stageClearDim();
    return;
  }

  /* 其它语句：只高亮相关变量 */
  stageDim(new Set((stmt.refs || []).filter((r) => snap.vars.has(r))));
  await sleep(220 * STG.pace);
  stageClearDim();
}

/* ---------- 输出区 ---------- */
function stagePushOut(text) {
  const box = $('#stageOutLines');
  if (!box) return;
  if (text == null) return;
  const ph = $('#stageOutPh'); if (ph) ph.style.display = 'none';
  STG.out.push(text);
  while (STG.out.length > 3) STG.out.shift();
  box.innerHTML = STG.out.map((l, i) => `<div class="${i === STG.out.length - 1 ? 'ln-new' : ''}">${esc(l)}</div>`).join('');
  box.scrollTop = box.scrollHeight;
}

/* ---------- 异常事件：舞台展示「抛了什么异常」 ---------- */
function stageOnException(ev) {
  stageEnqueue(async () => {
    const g = STG.gen;
    const t = ev.type || 'Exception';
    let msg = String(ev.msg || '');
    // probe 给的 msg 常是异常 repr（RuntimeError('xxx')），去掉与类型重复的部分
    if (msg.startsWith(t + '(')) msg = msg.slice(t.length + 1, -1);
    else if (msg.startsWith(t + ': ')) msg = msg.slice(t.length + 2);
    const e = $('#stageExpr');
    if (e) {
      e.className = 'expr hot err';
      e.innerHTML = `<span class="raise-kw">⚠ ${esc(t)}</span>`
        + `<span class="opnd">${esc(clip(msg, 56))}</span>`
        + `<span class="eq">@</span><span class="res err">L${ev.line || '?'}</span>`;
    }
    const sm = $('#stageMeta');
    if (sm) sm.textContent = `异常 ${t}: ${clip(msg, 44)}`;
    await sleep(420 * STG.pace);
    if (g !== STG.gen) return;
  });
}

/* ---------- 函数返回事件：舞台展示「谁返回了什么」 ---------- */function stageOnReturn(ev, sp) {
  const retTxt = clip((ev.ret && ev.ret.repr) || 'None', 36);
  stageEnqueue(async () => {
    const g = STG.gen;
    const e = $('#stageExpr');
    if (e) {
      e.className = 'expr hot';
      e.innerHTML = `<span class="fnn">${esc(sp.func)}</span><span class="op">()</span> 返回`
        + `<span class="eq">→</span><span class="res">${esc(retTxt)}</span>`;
    }
    const sm = $('#stageMeta');
    if (sm) sm.textContent = `${sp.func}() 执行完毕，回到调用行`;
    // 头部跟随调用行，避免左边高亮已回到调用处、右边还停在函数内最后一行
    const callerLine = S.curLine || sp.callerLine;
    if (callerLine) {
      const cstmt = S.stmtMap.get(callerLine);
      stageHead({ text: cstmt ? cstmt.text : '', kind: 'return' }, callerLine);
    }
    const head = $('#stageHead');
    if (head) flyToken(null, head, retTxt, 'out', { from: { x: 80, y: (elCenter(head) || { y: 120 }).y + 120 }, dur: 480 * STG.pace });
    await sleep(300 * STG.pace);
    if (g !== STG.gen) return;
  });
}

/* ---------- 队列 ---------- */
function stageEnqueue(job) {
  STG.queue.push(job);
  if (!STG.busy) stageDrain();
}

async function stageDrain() {
  STG.busy = true;
  while (STG.queue.length) {
    const job = STG.queue.shift();
    try { await job(); } catch (e) { /* 单帧失败不影响后续 */ }
  }
  STG.busy = false;
}

/* ---------- 函数传参：实参飞入形参 ---------- */
function stageOnCall(ev) {
  if (!ev.is_user || !ev.args || !ev.args.length) return;
  const snap = { vars: new Map(), newNames: new Set() };
  ev.args.forEach((a) => snap.vars.set(a.name, a.v));
  snap.newNames = new Set(snap.vars.keys());
  if (STG.silent) return;

  stageEnqueue(async () => {
    const g = STG.gen;
    STG.scope = ev.id;
    STG.vars = snap.vars;
    const v = $('#stageVars'); if (v) v.innerHTML = '';
    $('#stageEmpty') && $('#stageEmpty').classList.add('hide');
    stageCrumbs(ev.id);
    stageInSet(`${ev.func}() 收到的实参`, ev.args);
    stageHead({ text: `def ${ev.func}(...)`, kind: 'call' }, ev.line);
    const head = $('#stageHead');
    // 形参先以 ? 出现
    ev.args.forEach((a) => {
      const el = varCard(a.name, null);
      $('#stageVars') && $('#stageVars').appendChild(el);
    });
    const sm = $('#stageMeta'); if (sm) sm.textContent = `进入 ${ev.func}()`;
    const e = $('#stageExpr');
    const callTxt = `${ev.func}(${ev.args.map((a) => `${a.name}=${clip(prettyVal(a.v) || '', 12)}`).join(', ')})`;
    if (e) { e.className = 'expr hot'; e.innerHTML = `<span class="opnd">${esc(callTxt)}</span><span class="eq">←</span><span class="res">传参</span>`; }
    await sleep(150 * STG.pace); if (g !== STG.gen) return;
    for (const a of ev.args.slice(0, 4)) {
      const el = varEl(a.name);
      await flyToken(head, el, a.v.repr, '', { dur: 500 * STG.pace });
      if (g !== STG.gen) return;
      setCardValue(el, a.v, true);
      await sleep(60 * STG.pace);
    }
    stageDim(new Set());
  });
}

/* ---------------------------- 事件折叠 ---------------------------- */
function resetDerived() {
  S.cursor = 0; S.playing = false; S.progress = 0;
  S.spans = new Map(); S.order = []; S.roots = [];
  S.imports = []; S.importAgg = new Map();
  S.stdout = ''; S.stderr = ''; S.notes = []; S.errors = []; S.handledNotes = [];
  S.curLine = 0; S.lineOf = new Map(); S.stack = [];
  S.span = 1; S.endMs = 0; S.stats = null; S.wall = 0;
  S.sel = null; S.flowKey = '';
  S.frameVars = new Map();
  S.subs = new Map();
  stageReset();
  lastLine = -1; lastErr = -1;
  $$('#lines .ln').forEach((e) => e.classList.remove('hl', 'err'));
  markCallSite(0);
  const cb = $('#consoleBody');
  if (cb) cb.innerHTML = '<div class="empty"><p>暂无输出</p><small>程序里的 print() / stderr 会实时显示在这里</small></div>';
}

function resetState() {
  S.events = [];
  S.following = true;
  S.seen = new Set();
  S.teachLine = 0;
  S.teachRes = '';
  S.teachByLine.clear();
  S.flowLog = [];
  S.entryCard = '';
  // 上一轮的调试状态必须清干净：否则新一轮一开跑就顶着"已暂停"的工具条
  S.paused = null;
  S.wasStopped = false;
  const _db = $('#dbgBar'); if (_db) _db.hidden = true;
  document.body.classList.remove('paused');
  // 清掉上一轮残留的飞行气泡 / 徽标 / 波纹（否则切文件、重跑时旧元素会挂在半空）
  $$('.fx-p,.fx-ripple,.fx-badge,.fx-ring').forEach((e) => e.remove());
  FX.items.forEach((it) => { try { it.el.remove(); } catch (e) {} });
  FX.items = []; FX.raf = 0; FX.prev = 0;
  const sc = $('#simChip'); if (sc) sc.hidden = true;
  resetDerived();
}

function applyEvent(ev) {
  switch (ev.e) {
    case 'dbg': {
      handleDbg(ev);
      break;
    }
    case 'call': {
      const sp = {
        id: ev.id, pid: ev.pid, depth: ev.depth, func: ev.func,
        line: ev.line, callerLine: ev.caller_line, isUser: !!ev.is_user,
        isClass: !!ev.is_class, args: ev.args || [],
        th: ev.th || null, tk: ev.tk || null,     // 并发标注：线程 / asyncio 任务
        t0: ev.t, t1: null, dur: null, ret: null, locals: null, exc: null, children: [],
      };
      S.spans.set(ev.id, sp);
      const p = S.spans.get(ev.pid);
      if (p) p.children.push(sp); else S.roots.push(sp);
      S.order.push(ev.id);
      S.stack.push(ev.id);
      if (ev.is_user) { S.curLine = ev.line; S.lineOf.set(ev.id, ev.line); S.span = Math.max(S.span, ev.t + 1); }
      if (ev.is_user && ev.caller_line && ev.line !== ev.caller_line) spawnValueFlow(sp, 'in');
      stageOnCall(ev);
      break;
    }
    case 'line':
      S.lineOf.set(ev.id, ev.line);
      S.curLine = ev.line;
      S.span = Math.max(S.span, ev.t + 1);
      stageIngest(ev);
      break;
    case 'return': {
      const sp = S.spans.get(ev.id);
      if (sp) { sp.t1 = ev.t; sp.dur = ev.dur; sp.ret = ev.ret; sp.locals = ev.locals || null; }
      const i = S.stack.lastIndexOf(ev.id);
      if (i >= 0) S.stack.length = i;
      const top = S.stack[S.stack.length - 1];
      if (top && S.lineOf.has(top)) S.curLine = S.lineOf.get(top);
      S.span = Math.max(S.span, ev.t + 1);
      if (sp && sp.isUser && sp.callerLine && sp.callerLine !== sp.line && ev.dur != null) spawnValueFlow(sp, 'out');
      if (sp && sp.isUser && !STG.silent) stageOnReturn(ev, sp);
      break;
    }
    case 'exception': {
      const sp = S.spans.get(ev.id);
      if (sp) sp.exc = { type: ev.type, msg: ev.msg, line: ev.line };
      S.errors.push({ type: ev.type, msg: ev.msg, line: ev.line, fatal: false });
      if (!STG.silent) stageOnException(ev);
      break;
    }
    case 'import': {
      const rec = { name: ev.module || ev.name, from: ev.from, dur: ev.dur, file: ev.file, caller: ev.caller, callerFile: ev.caller_file, line: ev.line, t: ev.t, stub: !!ev.stub };
      if (S.imports.length < 800) S.imports.push(rec);
      const key = rec.name;
      const agg = S.importAgg.get(key) || { name: key, count: 0, dur: 0, file: rec.file, direct: false, stub: false };
      agg.count++; agg.dur += rec.dur;
      if (rec.stub) agg.stub = true;
      if (!agg.file && rec.file) agg.file = rec.file;
      if (rec.callerFile === S.filename) agg.direct = true;
      S.importAgg.set(key, agg);
      break;
    }
    case 'stdout':
      if (ev.stream === 'stderr') S.stderr += ev.data; else S.stdout += ev.data;
      if (ev.stream !== 'stderr' && !STG.silent) {
        String(ev.data || '').split('\n').forEach((l) => { if (l.trim()) stagePushOut(l); });
        // print 的输出本身就是这一行的"结果"，补进教学卡片
        S.printBuf = (S.printBuf || '') + String(ev.data || '');
        const pcs = S.printBuf.split('\n');
        S.printBuf = pcs.pop();
        if (pcs.length) {
          const line = pcs[pcs.length - 1];
          const st = S.stmtMap.get(S.teachLine);
          // 只有这一行确实是 print / 调用时，输出才算它的「结果」，
          // 否则会把别行的结论冲掉（比如赋值行被后续输出覆盖）
          if (line.trim() && st && (st.kind === 'print' || st.kind === 'call')) {
            teachFlow({ line: S.teachLine, kind: 'print', text: line });
          }
        }
      }
      break;
    case 'flow':
      teachFlow(ev);
      break;
    case 'note':
      if (ev.card && (ev.steps || []).length) {
        // 链式调用拆解卡：舞台顶部分步展示（不打进控制台输出，避免噪音）
        showExplain(`<b class="se-title">${esc(ev.title || '说明')}</b>`
          + `<ol>${ev.steps.map((s) => `<li>${esc(s)}</li>`).join('')}</ol>`);
      } else if (ev.type === 'Heartbeat') {
        // 执行中的进度心跳：只弹 toast，不写控制台输出，避免刷屏
        toast(ev.msg, 'warn');
      } else if (ev.type === 'CliTool') {
        toast(ev.msg, 'warn');
        S.notes.push(ev.msg);
      } else if (ev.type === 'Handled') {
        // 被 try/except 接住的异常：紧跟在异常提示后面渲染（澄清"不是中断"），用绿色
        S.handledNotes.push(ev.msg);
      } else {
        S.notes.push(ev.msg);
        if (ev.type === 'Project') toast(ev.msg, 'ok');
        else if (ev.type === 'Stub' || ev.type === 'Guard') toast(ev.msg, 'warn');
      }
      break;
    case 'error': {
      S.errors.push({ type: ev.type, msg: ev.msg, line: ev.line, fatal: true, tb: ev.traceback });
      if (ev.type === 'Crash') toast(ev.msg, 'err');   // 0xC0000005 等环境级崩溃：立刻弹提示，别让用户自己猜
      if (ev.line) markErrorLine(ev.line);
      break;
    }
    case 'end':
      S.endMs = ev.dur || 0;
      S.stats = ev.stats || null;
      if (ev.stats && ev.stats.imports_folded > 0) {
        S.notes.push(`库内部嵌套导入已聚合：折叠了 ${ev.stats.imports_folded} 次第三方库内部的 import`);
      }
      break;
  }
}

function rebuildTo(idx) {
  resetDerived();
  FX.silent = true;
  STG.silent = true;
  hideExplain();   // 静默重建期间说明卡不跟随，先隐藏，前进动画会重新给出
  for (let i = 0; i < idx && i < S.events.length; i++) applyEvent(S.events[i]);
  FX.silent = false;
  STG.silent = false;
  S.cursor = Math.min(idx, S.events.length);
}

/* ---------------------------- 渲染调度 ---------------------------- */
let flushTimer = null;
function schedule(delay = 70) {
  // 事件洪峰时自动放慢渲染频率，避免主线程被 DOM 重建占满而卡死
  if (delay === 70) {
    if (S.events.length > 6000) delay = 320;
    else if (S.events.length > 2500) delay = 180;
    else if (S.events.length > 1200) delay = 120;
  }
  if (flushTimer) return;
  flushTimer = setTimeout(() => { flushTimer = null; flush(); }, delay);
}

function flush() {
  renderTree();
  renderConc();
  renderKpi();
  renderInspector();
  renderConsole();
  renderImports();
  renderStats();
  renderEvents();
  renderClock();
  if (S.curLine) markLine(S.curLine);
  updateCursor();
  drawFlow();
}

/* ---------------------------- 调用树 / 瀑布 ---------------------------- */
function renderTree() {
  const body = $('#treeBody');
  if (!S.order.length) {
    body.innerHTML = `<div class="empty"><p>还没有运行记录</p><small>点击右上角「运行」，即可看到每个函数的入参、返回值与耗时</small></div>`;
    return;
  }
  const total = Math.max(S.span, 1);
  const rows = [];
  let shown = 0;
  for (const id of S.order) {
    const sp = S.spans.get(id);
    if (!sp) continue;
    if (!S.showLib && !sp.isUser) continue;
    if (shown >= MAX_ROWS) break;
    shown++;
    const fresh = !S.seen.has(id);
    if (fresh) S.seen.add(id);
    rows.push(rowHtml(sp, total, fresh));
  }
  if (shown >= MAX_ROWS) {
    rows.push(`<div class="empty" style="min-height:0;padding:14px"><small>调用次数超过 ${MAX_ROWS}，仅显示前 ${MAX_ROWS} 行（可减少循环次数或只看库调用开关）</small></div>`);
  }
  const sel = S.sel;
  const scroller = body;
  const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 60;
  const keepTop = scroller.scrollTop;
  body.innerHTML = rows.join('');
  if (atBottom && S.following) scroller.scrollTop = scroller.scrollHeight;
  else scroller.scrollTop = keepTop;
  if (sel) {
    const el = body.querySelector(`.tree-row[data-id="${sel}"]`);
    if (el) el.classList.add('sel');
  }
  const act = S.stack[S.stack.length - 1];
  if (act) {
    const el = body.querySelector(`.tree-row[data-id="${act}"]`);
    if (el) {
      el.classList.add('active');
      if (S.following && S.mode === 'live') {
        const r = el.offsetTop - scroller.clientHeight / 2;
        if (Math.abs(scroller.scrollTop - r) > scroller.clientHeight) scroller.scrollTop = r;
      }
    }
  }
}

function rowHtml(sp, total, fresh) {
  const left = Math.min(99.6, (sp.t0 / total) * 100);
  const w = Math.max(0.35, ((sp.dur || (S.span - sp.t0)) / total) * 100);
  const slow = (sp.dur || 0) > 50;
  let cls = 'tree-row';
  if (!sp.isUser) cls += ' ext';
  if (sp.exc) cls += ' exc';
  if (fresh) cls += ' enter';
  const args = sp.args.length
    ? '(' + sp.args.map((a) => `${a.name}=${a.v.repr}`).join(', ').replace(/\s+/g, ' ') + ')'
    : '()';
  const argTxt = args.length > 150 ? args.slice(0, 150) + '…' : args;
  const badge = sp.isClass ? '<span class="fn-badge cls">类</span>'
    : (!sp.isUser ? '<span class="fn-badge lib">库</span>' : '')
      + (sp.exc ? '<span class="fn-badge exc">异常</span>' : '')
      + (sp.tk ? `<span class="fn-badge th">${esc(sp.tk)}</span>` : '')
      + (sp.th ? `<span class="fn-badge th">${esc(sp.th)}</span>` : '');
  const name = sp.isClass ? `class ${sp.func}` : sp.func;
  return `<div class="${cls}" data-id="${sp.id}" style="--d:${sp.depth}">
    <div class="fn-cell"><span class="tw">${sp.children.length ? '▾' : '·'}</span>
      <span class="fn-name">${esc(name)}</span>${badge}
      <span class="fn-args">${esc(argTxt)}</span></div>
    <div class="bar-cell"><div class="bar"><i class="${sp.exc ? 'exc' : (!sp.isUser ? 'lib' : (slow ? 'slow' : ''))}"
      style="left:${left.toFixed(2)}%;width:${w.toFixed(2)}%"></i></div></div>
    <div class="dur ${slow ? 'slow' : ''}">${sp.dur == null ? '…' : fmtMs(sp.dur)}</div>
  </div>`;
}

function fmtMs(v) {
  if (v == null) return '—';
  if (v >= 1000) return (v / 1000).toFixed(2) + ' s';
  if (v >= 10) return v.toFixed(1) + ' ms';
  return v.toFixed(2) + ' ms';
}

/* ---------------------------- 并发泳道时间线 ----------------------------
   线程（th）/ asyncio 任务（tk）各占一条泳道，条 = 一次函数/协程调用的墙钟时间。
   交错一目了然：task A 的条和 task B 的条在同一时间段并排重叠 = 并发执行；
   依次排列 = 串行。单线程同步代码没有泳道，显示空态。 */
function renderConc() {
  const body = $('#concBody');
  if (!body) return;
  const lanes = new Map();            // 泳道名 -> [span]
  let hasConc = false;
  for (const id of S.order) {
    const sp = S.spans.get(id);
    if (!sp) continue;
    if (sp.th || sp.tk) hasConc = true;
    const key = sp.tk ? `task ${sp.tk}` : (sp.th || '主流程');
    if (!lanes.has(key)) lanes.set(key, []);
    if (lanes.get(key).length < 200) lanes.get(key).push(sp);
  }
  if (!hasConc) {
    if (!body.querySelector('.empty')) {
      body.innerHTML = `<div class="empty"><p>本次运行是单线程同步代码</p>
        <small>用到 threading / asyncio 时，这里会按「线程 / 任务」分泳道展示执行时间线——谁在什么时候跑、在哪里交错</small></div>`;
    }
    return;
  }
  const total = Math.max(S.span, 1);
  const act = S.stack[S.stack.length - 1];
  const html = [`<div class="conc-scale"><span>0 ms</span><span>${fmtMs(total)}</span></div>`];
  let laneIdx = 0;
  for (const [lane, items] of lanes) {
    const isTask = lane.startsWith('task ');
    html.push(`<div class="conc-lane"><span class="conc-name ${isTask ? 'tk' : 'th'}">${esc(lane)}</span><div class="conc-track">`);
    items.forEach((sp) => {
      const left = Math.min(99.5, (sp.t0 / total) * 100);
      const w = Math.max(0.4, ((sp.dur != null ? sp.dur : (S.span - sp.t0)) / total) * 100);
      const cls = (sp.exc ? ' exc' : (!sp.isUser ? ' lib' : '')) + (sp.id === act ? ' active' : '');
      const tip = `${sp.func}  ${sp.dur != null ? fmtMs(sp.dur) : '进行中…'}`;
      html.push(`<div class="conc-bar${cls}" data-id="${sp.id}" title="${esc(tip)}" style="left:${left.toFixed(2)}%;width:${w.toFixed(2)}%"><span>${esc(sp.func)}</span></div>`);
    });
    html.push(`</div></div>`);
    laneIdx++;
  }
  body.innerHTML = html.join('');
}

/* ---------------------------- KPI ---------------------------- */
function userCallCount() {
  // 「自己定义的函数被调用了几次」——真跑完时用来判断这份代码是不是只有模块级语句
  let n = 0;
  for (const id of S.order) {
    const sp = S.spans.get(id);
    if (sp.isUser && sp.func !== '<模块>') n++;
  }
  return n;
}

function renderKpi() {
  let calls = 0, maxDepth = 0;
  for (const id of S.order) {
    const sp = S.spans.get(id);
    if (sp.isUser && sp.func !== '<模块>') { calls++; maxDepth = Math.max(maxDepth, sp.depth); }
  }
  setKpi('#kpiCalls', calls);
  setKpi('#kpiDur', S.endMs ? S.endMs.toFixed(1) : (S.events.length ? S.span.toFixed(0) : 0));
  setKpi('#kpiDepth', maxDepth);
  setKpi('#kpiImports', S.importAgg.size);
}

function setKpi(sel, val) {
  const el = $(sel);
  if (!el || el.textContent === String(val)) return;
  el.textContent = val;
  el.classList.remove('bump');
  void el.offsetWidth;          // 强制重排以重播动画
  el.classList.add('bump');
}

/* ---------------------------- 检查器 ---------------------------- */
function typeShort(t) {
  if (!t) return '?';
  const p = String(t).split('.');
  return p[p.length - 1];
}
function kindCls(k) {
  return { number: 'n', str: 's', bool: 'b', none: 'none' }[k] || '';
}
function renderPairs(items) {
  if (!items || !items.length) return '<div class="vempty">（无参数）</div>';
  let h = '<div class="vtree">';
  for (const it of items) {
    const v = it.v || { type: '?', repr: '', kind: 'none' };
    h += `<div class="vrow"><span class="vk">${esc(it.name)}</span><span class="vtype">${esc(typeShort(v.type))}</span><span class="vval ${kindCls(v.kind)}">${esc(v.repr)}</span></div>`;
    if (v.children) {
      for (const c of v.children) {
        const cv = c.v || {};
        h += `<div class="vrow" style="padding-left:16px"><span class="vk">${esc(c.k)}</span><span class="vtype">${esc(typeShort(cv.type))}</span><span class="vval ${kindCls(cv.kind)}">${esc(cv.repr)}</span></div>`;
      }
    }
  }
  return h + '</div>';
}

function renderInspector() {
  const box = $('#inspectDetail');
  const title = $('#inspectTitle');
  const clearBtn = $('#btnClearSel');
  if (!S.sel || !S.spans.get(S.sel)) {
    clearBtn.hidden = true;
    title.textContent = '运行概览';
    let extra = `<div class="hint">点左侧调用树的任意一行，查看该次调用的<b>入参</b>、<b>返回值</b>与<b>局部变量</b>。</div>`;
    const errs = S.errors.filter((e) => e.fatal);
    if (S.mode === 'live') {
      extra += `<div class="hint" style="border-color:#1e3a4d;color:#8fb8d8">程序正在运行，事件实时到达中…</div>`;
    } else if (S.stats || S.endMs) {
      extra += `<div class="sec"><div class="sec-h">执行结果</div>
        <div class="call-card" style="margin:0">
          <div class="meta-grid">
            <div>实际耗时 <b>${S.wall ? S.wall.toFixed(0) + ' ms' : '—'}</b></div>
            <div>轨迹耗时 <b>${S.endMs.toFixed(2)} ms</b></div>
            <div>事件总数 <b>${S.events.length}</b></div>
            <div>库调用 <b>${S.stats ? S.stats.ext_calls : '—'}</b></div>
          </div>
        </div></div>`;
    }
    if (errs.length) {
      const e = errs[errs.length - 1];
      extra += `<div class="sec"><div class="sec-h">异常</div>
        <div class="call-card" style="border-color:#4a1f2b">
          <div class="call-title" style="color:var(--err)">${esc(e.type)}</div>
          <div class="call-sub" style="white-space:pre-wrap;line-height:1.6">${esc(e.msg)}</div>
          ${e.line ? `<div class="call-sub" style="margin:0">第 ${e.line} 行</div>` : ''}
        </div></div>`;
    }
    if (!S.events.length) {
      extra += `<div class="hint">把 .py 文件拖进左侧区域，或从「内置示例」里挑一个，然后点「运行」。</div>`;
    }
    box.innerHTML = extra;
    return;
  }
  clearBtn.hidden = false;
  const sp = S.spans.get(S.sel);
  title.textContent = sp.isClass ? `class ${sp.func}` : sp.func;
  const parent = S.spans.get(sp.pid);
  const self = Math.max(0, (sp.dur || 0) - sp.children.reduce((a, c) => a + (c.dur || 0), 0));
  let h = `
    <div class="call-card">
      <div class="call-title">${esc(sp.isClass ? 'class ' + sp.func : sp.func)}</div>
      <div class="call-sub">${esc(S.filename)} : 第 ${sp.line} 行${sp.callerLine ? `　← 调用自第 ${sp.callerLine} 行` : ''}</div>
      <div class="meta-grid">
        <div>总耗时 <b>${sp.dur == null ? '运行中…' : fmtMs(sp.dur)}</b></div>
        <div>自身耗时 <b>${sp.dur == null ? '—' : fmtMs(self)}</b></div>
        <div>子调用 <b>${sp.children.length}</b></div>
        <div>调用深度 <b>${sp.depth}</b></div>
      </div>
    </div>
    <div class="sec"><div class="sec-h">入参</div>${renderPairs(sp.args)}
      ${!sp.args.length && sp.func === '<模块>' ? '<div class="vempty">（模块顶层，无入参）</div>' : ''}
    </div>
    <div class="sec"><div class="sec-h">返回值</div>
      ${sp.ret ? renderPairs([{ name: 'return', v: sp.ret }]) : '<div class="vempty">执行中 / 尚未返回…</div>'}
    </div>`;
  if (sp.locals && sp.locals.length) {
    const argsName = new Set(sp.args.map((a) => a.name));
    const rest = sp.locals.filter((l) => !argsName.has(l.name));
    if (rest.length) h += `<div class="sec"><div class="sec-h">局部变量（返回时）</div>${renderPairs(rest)}</div>`;
  }
  if (sp.exc) {
    h += `<div class="sec"><div class="sec-h">异常</div>
      <div class="call-card" style="border-color:#4a1f2b;margin:0">
        <div class="call-title" style="color:var(--err);font-size:12px">${esc(sp.exc.type)}</div>
        <div class="call-sub" style="margin:4px 0 0">${esc(sp.exc.msg)}</div>
      </div></div>`;
  }
  if (parent) {
    h += `<div class="sec"><div class="sec-h">上层调用</div>
      <div class="call-card" style="cursor:pointer;margin:0" data-goto="${parent.id}">
        <div class="call-title" style="font-size:12px">${esc(parent.func)}()</div>
        <div class="call-sub" style="margin:0">第 ${parent.line} 行</div>
      </div></div>`;
  }
  box.innerHTML = h;
}

/* ---------------------------- 控制台 ---------------------------- */
function renderConsole() {
  const box = $('#consoleBody');
  if (!S.stdout && !S.stderr && !S.errors.length && !S.notes.length && !S.handledNotes.length) {
    box.innerHTML = '<div class="empty"><p>暂无输出</p><small>程序里的 print() / stderr 会实时显示在这里</small></div>';
    return;
  }
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  let h = '';
  if (S.stdout) h += esc(S.stdout);
  for (const n of S.notes) h += `\n<span class="warn">[提示] ${esc(n)}</span>`;
  if (S.stderr) h += `\n<span class="err">${esc(S.stderr)}</span>`;
  for (const e of S.errors) {
    h += `\n<span class="err">[${esc(e.type)}] ${esc(e.msg)}</span>`;
    if (e.line) {
      h += `\n<span class="warn">↑ 第 ${e.line} 行抛出的异常（下方有 ✔ = 已被 try/except 接住、程序继续跑；没有 ✔ = 程序在此中断）</span>`;
    }
    if (e.tb) h += `\n<span class="err">${esc(String(e.tb).split('\n').slice(-6).join('\n'))}</span>`;
  }
  // 被捕获的异常澄清：放在异常提示后面，先看到"抛了"，紧接着看到"接住了、继续跑"
  for (const n of S.handledNotes) h += `\n<span class="ok">✔ ${esc(n)}</span>`;
  box.innerHTML = h || '<div class="empty"><p>暂无输出</p></div>';
  if (atBottom) box.scrollTop = box.scrollHeight;
}

/* ---------------------------- 导入 ---------------------------- */
function classifyImport(rec) {
  const f = (rec.file || '').replace(/\\/g, '/');
  if (!f) return { cls: 'std', label: '内置' };
  if (/lib-dynload|\/lib\/python\d|site-packages|dist-packages/.test(f)) {
    return /site-packages|dist-packages/.test(f) ? { cls: 'local', label: '三方' } : { cls: 'std', label: '标准库' };
  }
  return { cls: 'local', label: '本地' };
}

function renderImports() {
  const box = $('#importsBody');
  if (!S.importAgg.size) {
    if (S.mode === 'idle') { box.innerHTML = '<div class="empty"><p>暂无导入记录</p><small>运行后这里会列出 import 的模块、耗时与来源路径</small></div>'; return; }
    box.innerHTML = '<div class="empty"><p>本次运行没有 import 任何模块</p></div>';
    return;
  }
  const list = [...S.importAgg.values()].sort((a, b) => b.dur - a.dur);
  let h = `<div class="sec-title" style="margin-top:0">运行时导入 ${list.length} 个模块
    <span style="color:var(--dim);font-weight:400;text-transform:none">（按耗时排序）</span></div>`;
  for (const r of list) {
    const c = classifyImport(r);
    h += `<div class="imp-card">
      <div class="imp-top">
        <span class="imp-name">${esc(r.name)}</span>
        <span class="imp-tag ${c.cls}">${c.label}</span>
        ${r.stub ? '<span class="imp-tag warn" title="本机没有安装这个依赖，用占位对象代替，相关值不是真实结果">未安装·占位</span>' : ''}
        ${r.direct ? '<span class="imp-tag">你直接 import</span>' : ''}
        ${r.count > 1 ? `<span class="imp-tag">×${r.count}</span>` : ''}
        <span class="imp-dur">${fmtMs(r.dur)}</span>
      </div>
      <div class="imp-path">${esc(r.file || '（内置模块，无文件路径）')}</div>
    </div>`;
  }
  box.innerHTML = h;
}

/* ---------------------------- 统计 ---------------------------- */
function computeStats() {
  const agg = new Map();
  for (const id of S.order) {
    const sp = S.spans.get(id);
    if (!sp.isUser || sp.func === '<模块>') continue;
    const rec = agg.get(sp.func) || { name: sp.func, count: 0, total: 0, self: 0, max: 0, line: sp.line };
    rec.count++;
    const total = sp.dur || 0;
    const child = sp.children.reduce((a, c) => a + (c.dur || 0), 0);
    rec.total += total;
    rec.self += Math.max(0, total - child);
    rec.max = Math.max(rec.max, total);
    agg.set(sp.func, rec);
  }
  return [...agg.values()].sort((a, b) => b.self - a.self);
}

function renderStats() {
  const box = $('#statsBody');
  if (!S.order.length) {
    box.innerHTML = '<div class="empty"><p>暂无统计数据</p><small>运行结束后生成调用耗时排行</small></div>';
    return;
  }
  const rows = computeStats();
  const totalSelf = rows.reduce((a, r) => a + r.self, 0) || 1;
  const slowest = [...S.order].map((i) => S.spans.get(i)).filter((s) => s.isUser && s.dur != null)
    .sort((a, b) => b.dur - a.dur).slice(0, 8);
  let h = `<div class="stat-cards">
    <div class="stat-card"><b>${S.endMs.toFixed(1)}</b><span>总耗时 ms</span></div>
    <div class="stat-card"><b>${S.events.length}</b><span>事件数</span></div>
    <div class="stat-card"><b>${rows.reduce((a, r) => a + r.count, 0)}</b><span>函数调用</span></div>
    <div class="stat-card"><b>${S.importAgg.size}</b><span>导入模块</span></div>
  </div>`;
  h += `<div class="sec-title" style="margin-top:4px">按自身耗时排行</div>`;
  for (const r of rows.slice(0, 12)) {
    h += `<div class="rank-row">
      <div><div class="name">${esc(r.name)}()</div><div class="cnt">调用 ${r.count} 次 · 平均 ${fmtMs(r.total / r.count)}</div></div>
      <div class="rank-bar"><i style="width:${Math.max(2, (r.self / totalSelf) * 100).toFixed(1)}%"></i></div>
      <div class="rank-time">${fmtMs(r.self)}</div>
    </div>`;
  }
  if (slowest.length) {
    h += `<div class="sec-title">最慢的单次调用</div>`;
    for (const sp of slowest) {
      h += `<div class="rank-row" data-jump="${sp.id}" style="cursor:pointer">
        <div><div class="name">${esc(sp.func)}()</div><div class="cnt">第 ${sp.line} 行</div></div>
        <div class="rank-bar"><i style="width:${Math.max(2, (sp.dur / slowest[0].dur) * 100).toFixed(1)}%;background:linear-gradient(90deg,#d97706,#fbbf24)"></i></div>
        <div class="rank-time">${fmtMs(sp.dur)}</div>
      </div>`;
    }
  }
  box.innerHTML = h;
}

/* ---------------------------- 事件流 ---------------------------- */
function evLabel(ev) {
  switch (ev.e) {
    case 'call': return `→ ${ev.func}(${(ev.args || []).map((a) => `${a.name}=${a.v.repr}`).join(', ').slice(0, 90)})`;
    case 'return': return `← 返回 ${ev.ret ? ev.ret.repr.slice(0, 80) : 'None'}　(${fmtMs(ev.dur)})`;
    case 'line': return `第 ${ev.line} 行`;
    case 'import': return `import ${ev.name}${ev.from && ev.from.length ? ' ← ' + ev.from.join(',') : ''} (${fmtMs(ev.dur)})${ev.stub ? '（未安装·占位）' : ''}`;
    case 'stdout': return `print: ${String(ev.data).replace(/\n/g, '⏎').slice(0, 80)}`;
    case 'exception': return `${ev.type}: ${ev.msg}`;
    case 'error': return `${ev.type}: ${ev.msg}`;
    case 'note': return ev.msg;
    case 'start': return '开始执行';
    case 'end': return '执行完成';
    default: return ev.e;
  }
}
const EV_KIND = { call: ['call', 'CALL'], return: ['ret', 'RET'], line: ['', 'LINE'], import: ['imp', 'IMPORT'], stdout: ['out', 'OUT'], exception: ['exc', 'EXC'], error: ['exc', 'ERR'], note: ['out', 'NOTE'], start: ['', 'START'], end: ['', 'END'] };

function renderEvents() {
  const box = $('#eventsBody');
  if (!S.events.length) {
    box.innerHTML = '<div class="empty"><p>暂无事件</p><small>记录每一次函数调用、返回、异常与导入</small></div>';
    return;
  }
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
  let h = '';
  const limit = Math.min(S.events.length, MAX_EV_ROWS);
  for (let i = 0; i < limit; i++) {
    const ev = S.events[i];
    const k = EV_KIND[ev.e] || ['', ev.e];
    const cur = i === S.cursor - 1 ? ' cur' : '';
    h += `<div class="ev-row${cur}"><span class="ev-t">${(ev.t || 0).toFixed(1)}</span><span class="ev-k ${k[0]}">${k[1]}</span><span class="ev-x">${esc(evLabel(ev))}</span></div>`;
  }
  if (S.events.length > MAX_EV_ROWS) h += `<div class="empty" style="min-height:0;padding:12px"><small>仅显示前 ${MAX_EV_ROWS} 条事件（共 ${S.events.length} 条）</small></div>`;
  box.innerHTML = h;
  if (atBottom && S.following) box.scrollTop = box.scrollHeight;
  else {
    const cur = box.querySelector('.ev-row.cur');
    if (cur && S.following) box.scrollTop = cur.offsetTop - box.clientHeight / 2;
  }
}

/* ---------------------------- 播放控制 ---------------------------- */
function renderClock() {
  const max = Math.max(0, S.events.length - 1);
  const cur = Math.max(0, Math.min(S.cursor - 1, max));
  $('#scrub').max = String(Math.max(max, 1));
  $('#scrub').value = String(cur);
  $('#scrub').style.setProperty('--p', (max ? (cur / Math.max(max, 1)) * 100 : 0).toFixed(1) + '%');
  const t = S.events[cur] ? (Number(S.events[cur].t) || 0) : 0;
  // 分母要钳到不小于分子：wall 时长（含进程收尾）可能略大于最后一个事件的时间戳，
  // 否则会出现 "2356.7 / 2351.5 ms" 这种进度倒挂
  const total = Math.max(t, Number(S.endMs || S.span || 0));
  $('#clock').textContent = `${t.toFixed(1)} / ${total.toFixed(1)} ms`;
  const ev = S.events[cur];
  $('#nowLabel').textContent = ev ? `${ev.e.toUpperCase()} · ${evLabel(ev)}` : '就绪';
  $('#liveDot').classList.toggle('on', S.mode === 'live');
  const icon = $('#playIcon');
  if (S.playing) icon.innerHTML = '<rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor"/><rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor"/>';
  else icon.innerHTML = '<path d="M8 5l11 7-11 7z" fill="currentColor"/>';
}

function seek(idx) {
  idx = Math.max(0, Math.min(idx, S.events.length));
  if (idx === S.cursor) return;
  rebuildTo(idx);
  const errs = S.errors.filter((e) => e.fatal);
  markErrorLine(errs.length ? errs[errs.length - 1].line : -1);
  flush();
}

let playTimer = null, playStart = 0, playFrom = 0;
async function stagePlayLoop() {
  S.playing = true;
  S.following = false;
  playStart = performance.now();
  playFrom = S.cursor;
  const pace = () => 1 / Math.max(0.25, S.speed || 1);
  while (S.playing && S.cursor < S.events.length) {
    applyEvent(S.events[S.cursor]); S.cursor++;
    flush();
    const limit = performance.now() + 1500 * STG.pace * pace();
    while (S.playing && performance.now() < limit) {
      if (!STG.busy && !STG.queue.length) break;
      await sleep(30);
    }
    await sleep(80 * STG.pace * pace());
  }
  if (S.cursor >= S.events.length) { stopPlay(); flush(); }
}

function togglePlay() {
  if (S.playing) { stopPlay(); return; }
  if (!S.events.length) return;
  if (S.mode === 'live') { S.following = false; }
  if (S.cursor >= S.events.length) seek(0);
  // 事件不多时，按舞台动画的节奏逐步演示；事件量很大时退回快速预览
  if (S.events.length <= 400 && !STG.animOff) {
    // 演出预算：总时长 ~26s，事件多时动画加速 + 采样
    const lineEvs = S.events.filter((x) => x.e === 'line').length;
    const est = lineEvs * 1250;
    const BUDGET = 26000;
    const pace = Math.max(0.3, Math.min(1, BUDGET / Math.max(est, 1)));
    STG.autoPace = pace;
    applyUserSpeed();
    STG.sampleN = pace < 0.34 ? Math.max(2, Math.round((est * 0.32) / BUDGET)) : 1;
    STG.lineCnt = 0;
    stagePlayLoop();
    return;
  }
  S.playing = true;
  playStart = performance.now();
  playFrom = S.cursor;
  const total = Math.max(S.events.length, 1);
  const remain = total - playFrom;
  const DURATION = 5200;                 // 1× 速度下约 5 秒放完
  const step = () => {
    if (!S.playing) return;
    const elapsed = performance.now() - playStart;
    const ratio = Math.min(1, elapsed / (DURATION / S.speed));
    const target = Math.min(S.events.length, Math.round(playFrom + remain * ratio));
    if (target !== S.cursor) {
      while (S.cursor < target) { applyEvent(S.events[S.cursor]); S.cursor++; }
      schedule(40);
    }
    if (ratio >= 1) { stopPlay(); flush(); maybeEntrySuggest(); return; }
    playTimer = requestAnimationFrame(step);
  };
  playTimer = requestAnimationFrame(step);
  renderClock();
}
function stopPlay() {
  S.playing = false;
  if (playTimer) cancelAnimationFrame(playTimer);
  playTimer = null;
  renderClock();
}
function stepBy(d) {
  if (S.playing) stopPlay();
  S.following = false;
  const idx = Math.max(0, Math.min(S.cursor + d, S.events.length));
  if (idx === S.cursor) return;
  if (d > 0 && S.events.length) {
    // 前进：前面的历史静默重放，最后一步带完整动画（舞台演出 + 数据流粒子指引）
    rebuildTo(idx - 1);
    FX.silent = false; STG.silent = false; STG.stepMode = true;
    try { applyEvent(S.events[idx - 1]); }
    finally { STG.stepMode = false; }
    S.cursor = idx;
    const errs = S.errors.filter((e) => e.fatal);
    markErrorLine(errs.length ? errs[errs.length - 1].line : -1);
    flush();
  } else {
    seek(idx);   // 后退：静默回放（不倒放动画）
  }
}

/* ---------------------------- 运行 ---------------------------- */
function setRunning(on) {
  $('#btnRun').disabled = on;
  $('#btnStop').hidden = !on;
  $('#btnRun').style.opacity = on ? .5 : 1;
}

/** 运行失败诊断卡：退出码 + 解释器 stderr 原文（截尾）+ 按内容匹配的常见原因。 */
function showDiagCard(doneEv) {
  const fatal = S.errors.filter((x) => x.fatal);
  const runnerErr = fatal.find((x) => x.type === 'RunnerError' || x.type === 'SpawnError');
  const otherErr = fatal.find((x) => x !== runnerErr);
  const code = doneEv.code;
  const noEvents = S.events.length <= 2;
  // 从 stderr 原文猜最可能的原因
  const stderrText = runnerErr ? String(runnerErr.msg || '').replace(/^解释器级别的错误输出：\n?/, '') : '';
  const s = stderrText.toLowerCase();
  let cause = '';
  if (/cannot?open|no such file|can't open file/.test(s)) {
    cause = '解释器找不到这个文件 —— 检查顶栏「项目目录」是否指向项目根，且文件在根目录下';
  } else if (/modulenotfounderror|importerror/.test(s)) {
    cause = '这个解释器缺依赖 —— 换一个解释器（顶栏「解释器」），或在它所在的虚拟环境里 pip install 缺的包';
  } else if (/syntaxerror/.test(s)) {
    cause = '文件存在语法错误 —— 你平时的 Python 也跑不了它；先在 IDE 里把语法错误解决';
  } else if (/unicodedecodeerror|gbk|encoding/.test(s)) {
    cause = '文件编码问题 —— 把文件另存为 UTF-8 再试';
  } else if (/permissionerror|permission denied/.test(s)) {
    cause = '权限不足 —— 检查文件/目录权限，或换一个项目目录';
  } else if (stderrText) {
    cause = '解释器在启动探针时就退出了（一个事件都没来得及产生），上面是它自己的报错原文';
  }
  const errHtml = otherErr
    ? `<div class="tr"><b>异常</b><i style="color:#ff9d9d">${esc(otherErr.type)}: ${esc(String(otherErr.msg).slice(0, 300))}</i></div>`
    : '';
  const stderrHtml = stderrText
    ? `<div class="tr"><b>原文</b><i><pre style="white-space:pre-wrap;font-size:10.5px;margin:2px 0;color:#e8b4b4;max-height:180px;overflow:auto">${esc(stderrText.slice(-900))}</pre></i></div>`
    : `<div class="tr"><b>原文</b><i>（解释器没有输出任何错误信息就退出了 —— 请确认你平时直接运行这个文件是否能成功）</i></div>`;
  const html = `<div class="es-card">
    <div class="es-title" style="color:#ff9d9d">⚠ 运行失败 —— 按下面信息定位（退出码 ${esc(String(code))}）</div>
    ${noEvents ? `<div class="es-why">一个执行事件都没产生：问题出在<b>解释器启动阶段</b>，还没跑到你的代码。</div>` : ''}
    ${errHtml}
    ${stderrHtml}
    ${cause ? `<div class="es-why">💡 最可能的原因：${esc(cause)}</div>` : ''}
    <div class="es-why">把上面的「原文」整段复制发出来，可以精确定位；也可以
      <button class="es-btn pri" data-selftest="1">一键自检整条链路</button></div>
  </div>`;
  showExplain(html);
  const stBtn = document.querySelector('[data-selftest]');
  if (stBtn) stBtn.onclick = () => selfTest();
}

/* 连接在「已接收」之前就断开：这是请求没送达，不是代码报错，诊断文案要分开写，
   否则用户会看到"解释器没输出任何错误信息"这种牛头不对马嘴的提示 */
function showLinkDiagCard() {
  const nFiles = S.projectFiles ? S.projectFiles.length : 0;
  const pyFiles = S.projectFiles ? S.projectFiles.filter((f) => f.name.endsWith('.py')).length : 0;
  const html = `<div class="es-card">
    <div class="es-title" style="color:#ffb35c">⚠ 运行请求没有送达执行器</div>
    <div class="es-why">前端只发出去了请求，但没收到后端的「已接收」，连接就被关闭了 ——
      所以不会有任何事件，也就没有动画。这不代表你的代码有问题。</div>
    <div class="tr"><b>当前项目</b><i>共 ${nFiles} 个文件（其中 ${pyFiles} 个 .py）</i></div>
    <div class="es-why">💡 按这个顺序排查（每步 10 秒）：
      <br>① <b>看启动服务的黑色命令行窗口</b>：出现了红色 traceback → 整段复制发出来，一图定位
      <br>② 窗口里<b>没有任何新输出</b> → 请求根本没到达 PyTrace：检查浏览器代理/杀毒软件，或端口是否被别的程序占用
      <br>③ 确认服务是<b>新启动的</b>（右上角版本徽章 ≥ v1.8.1）—— 改完代码不重启服务，后端还是旧的
      <br>④ 点「验证环境」确认解释器可用，再刷新页面重试</div>
    <div class="es-why">现在可以：
      <button class="es-btn pri" data-selftest="1">一键自检链路</button>
      <button class="es-btn" data-only-entry="1">只看入口文件再试</button></div>
  </div>`;
  showExplain(html);
  const stBtn = document.querySelector('[data-selftest]');
  if (stBtn) stBtn.onclick = () => selfTest();
  const onlyBtn = document.querySelector('[data-only-entry]');
  if (onlyBtn) onlyBtn.onclick = () => {
    S.projectFiles = null;
    S.projectMap = null;
    const esel = $('#entrySel'); if (esel) { esel.hidden = true; esel.innerHTML = ''; }
    renderFileTree();
    toast('已切到单文件模式，再点一次运行', 'ok');
  };
}

async function analyze() {
  try {
    const r = await fetch('/api/analyze', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: S.code }),
    });
    const data = await r.json();
    if (data && data.ok && Array.isArray(data.stmts)) {
      const map = new Map();
      data.stmts.forEach((st) => { if (!map.has(st.line)) map.set(st.line, st); });
      S.stmtMap = map;
    }
    if (!data.ok && data.error) {
      markErrorLine(data.error.line);
      toast(`语法错误：${data.error.msg}（第 ${data.error.line} 行）`, 'err');
    }
    return data;
  } catch (e) { return null; }
}

async function run() {
  if (!S.code.trim()) { toast('请先打开或拖入一个 .py 文件', 'err'); return; }
  if (!S.stmtMap.size) await analyze();
  if (S.mode === 'live') return;
  stopPlay();
  S.sim = false;
  S.pendingSim = false;
  const sc0 = $('#simChip'); if (sc0) sc0.hidden = true;
  resetState();
  flush();
  setRunning(true);
  document.body.classList.add('running');
  S.mode = 'live';
  S.startedAt = performance.now();
  $$('#tabs .tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === 'tree'));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === 'tree'));

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/run`);
  S.ws = ws;
  S.wsAccepted = false;
  S.wsEventCount = 0;
  ws.onopen = () => {
    const timeout = Math.max(1, Math.min(120, parseInt($('#timeoutInput').value || '30', 10)));
    try {
      ws.send(JSON.stringify(buildRunPayload(S.code, S.filename, timeout, false)));
    } catch (err) {
      toast('提交运行失败：' + (err && err.message ? err.message : err), 'err');
    }
  };
  // 后端事件攒批：一条 WS 消息可能是数组（一次 parse 处理一批）
  const onRunEvent = (ev) => {
    if (ev.e === 'accepted') { S.runId = ev.run; S.wsAccepted = true; return; }
    if (ev.e === 'done') {
      S.wsAccepted = true;
      S.mode = 'done';
      S.wall = ev.wall || (performance.now() - S.startedAt);
      S.following = true;
      setRunning(false);
      document.body.classList.remove('running');
      flush();
      renderKpi();
      if (!S.errors.some((x) => x.fatal)) toast(`执行完成 · ${S.events.length} 个事件 · ${S.wall.toFixed(0)} ms`, 'ok');
      if (S.events.length > 3) { S.progress = 1; }
      maybeEntrySuggest();
      // 运行失败诊断卡：不再只显示"失败"，把退出码 / 解释器 stderr 原文 / 常见原因直接放到舞台上，
      // 用户截一张图就能定位问题
      const failed = !S.wasStopped && (ev.code !== 0 || S.errors.some((x) => x.fatal));
      if (S.wasStopped) {
        hideDbgBar();
        document.body.classList.remove('paused');
        toast('已在断点处停止 —— 已经执行的部分都保留着，可以拖时间轴回看每一步', 'ok');
      }
      if (failed) {
        showDiagCard(ev);
      }
      // 注意：这里原来写的是 doneStats.calls —— doneStats 这个变量根本不存在，
      // 一旦进入这个分支就抛 ReferenceError，把它后面「自动回放动画」整段代码带走。
      // 表现就是：执行明明成功了，事件也有，但舞台上一点动画都不播。
      if (!failed && userCallCount() === 0 && S.events.length > 3) {
        toast('已执行完成。这份代码 0 次调用自己定义的函数 —— 只有模块级语句（import、赋值、顶层调用）。'
          + '如果它是命令行工具，直接跑只会打印用法就退出：在顶栏「参数」里填参数（如 -h）再运行。', 'warn');
      }
      // 真实执行只要几十毫秒，动画根本看不清 —— 自动从头上慢速演示一遍
      // 事件多的项目（框架导入会产生成百条）以前会被这个 400 的上限挡掉，导致"跑完了但没有动画"
      // 调试运行结束后不再自动回放：用户刚在断点上盯着某一行，
      // 一按「继续」就把画面倒回第 0 帧，等于把他刚看清的东西擦掉
      if (S.events.length > 2 && !STG.animOff && !S.wasStopped && !S.debug) {
        const many = S.events.length > 400;
        setTimeout(() => {
          if (S.playing || S.mode === 'live') return;
          seek(0);
          toast(many
            ? `这次执行产生了 ${S.events.length} 个事件，正在从头演示；嫌快可以调顶栏「速度」`
            : '正在逐帧演示执行过程…', '');
          togglePlay();
        }, 520);
      }
      return;
    }
    S.wsEventCount = (S.wsEventCount || 0) + 1;
    // 调试控制事件不进事件流：它不是"执行过程"，而是"控制信号"。
    // 混进 S.events 会让时间轴回放时把旧的暂停状态又弹出来。
    if (ev.e === 'dbg') { handleDbg(ev); return; }
    S.events.push(ev);
    if (ev.e === 'error' && /No module named|ModuleNotFoundError|ImportError/i.test(ev.msg || '')) {
      if (!S.envWarned) {
        S.envWarned = true;
        toast('这个解释器缺依赖：点「验证环境」看缺哪个包，或换一个解释器再运行', 'warn');
      }
    }
    if (S.following) { applyEvent(ev); S.cursor = S.events.length; schedule(80); }
  };
  ws.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    (Array.isArray(data) ? data : [data]).forEach(onRunEvent);
  };
  ws.onclose = () => {
    setRunning(false);
    document.body.classList.remove('running');
    // 关键：连 accepted 都没收到就断了 —— 以前这里静默收尾，用户只看到"点了运行没反应"。
    // 最常见原因是随项目传上去的文件太多、单条消息超过 WebSocket 上限被服务端丢弃。
    if (!S.wsAccepted) {
      toast('运行请求没能送达执行器：项目文件过多或服务异常。'
        + '试试①只打开入口文件所在目录 ②点「验证环境」确认解释器可用 ③刷新页面重试', 'err');
      showLinkDiagCard();
      if (S.mode === 'live') S.mode = 'done';
      return;
    }
    if (S.mode === 'live') { S.mode = 'done'; flush(); }
  };
  ws.onerror = () => { toast('与本地服务的连接中断，请确认程序仍在运行', 'err'); setRunning(false); };
}

function stopRun() {
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ cmd: 'stop' }));
  hideDbgBar();
  document.body.classList.remove('paused');
  toast('正在停止…', '');
}

/* ---------------------------- 逻辑推演模式 ---------------------------- */
/* 不执行代码、不需要任何依赖：后端纯 AST 静态推演，产出与真实探针同构的事件流 */
async function runLogic() {
  if (!S.code) return;
  if (S.ws) { try { S.ws.close(); } catch (e) {} S.ws = null; }
  if (S.playing) stopPlay();
  setRunning(false);
  document.body.classList.remove('running');
  const r = await fetch('/api/simulate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: S.code, filename: S.filename }),
  });
  const d = await r.json();
  if (!d.ok) {
    const err = d.error || {};
    toast(`推演失败：${err.type || ''} ${err.msg || ''}（第 ${err.line || '?'} 行）`, 'err');
    return;
  }
  if (!S.stmtMap.size) await analyze();
  resetState();
  S.sim = true;
  S.mode = 'done';
  S.startedAt = performance.now();
  S.events = d.events || [];
  S.following = true;
  const sc = $('#simChip');
  if (sc) sc.hidden = false;
  $$('#tabs .tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === 'tree'));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === 'tree'));
  flush();
  renderKpi();
  toast(`逻辑推演：${S.events.length} 步（未实际执行，无需安装依赖）`, 'ok');
  togglePlay();   // 自动播放推演动画
}

/* ---------------------------- 环境：解释器与依赖验证 ---------------------------- */
async function loadInterpreters() {
  const sel = $('#interpSel');
  if (!sel) return;
  try {
    const r = await fetch('/api/interpreters?root=' + encodeURIComponent(S.root || ''));
    const d = await r.json();
    S.interpList = d.items || [];
    if (!S.interpList.length) {
      sel.innerHTML = '<option value="">没找到可用的 Python</option>';
      return;
    }
    const saved = localStorage.getItem('pytrace.interp') || '';
    sel.innerHTML = S.interpList.map((it) => {
      const label = it.ver + (it.venv ? ' · venv' : '') + ' · ' + it.source;
      const pick = (saved ? it.path === saved : it.path === d.recommended) ? ' selected' : '';
      return '<option value="' + esc(it.path) + '"' + pick + ' title="' + esc(it.path) + '">' + esc(label) + '</option>';
    }).join('');
    S.interpreter = sel.value;
    const chip = $('#pyChip');
    const cur = S.interpList.find((x) => x.path === sel.value);
    if (chip && cur) {
      chip.hidden = false;
      chip.textContent = 'Python ' + cur.ver + (cur.venv ? ' · 虚拟环境' : ' · 系统');
      chip.title = cur.path;
    }
  } catch (e) {
    sel.innerHTML = '<option value="">检测失败</option>';
  }
}

async function envCheck() {
  if (!S.code.trim()) { toast('先打开一个 .py 文件', 'err'); return; }
  const btn = $('#btnEnvCheck');
  if (btn) btn.disabled = true;
  toast('正在用选中的解释器逐个验证 import…', '');
  try {
    const r = await fetch('/api/envcheck', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        root: S.root || '', code: S.code, filename: S.filename,
        interpreter: S.interpreter || '',
      }),
    });
    const d = await r.json();
    if (!d.ok) { toast(d.error || '环境自检失败', 'err'); return; }
    const miss = d.missing || [];
    const ok = !!d.ready;
    const pip = miss.map((m) => String(m.name).split('.')[0].replace(/_/g, '-')).join(' ');
    showExplain(
      '<div class="teach">'
      + '<div class="teach-h"><span class="tl">环境验证 · Python ' + esc(d.python) + '</span>'
      + '<span class="tp ' + (ok ? 'ok' : 'bad') + '">' + (ok ? '可以真实执行' : '缺依赖') + '</span></div>'
      + '<div class="tr"><b>解释器</b><i>' + esc(d.exe) + (d.venv ? '（虚拟环境）' : '（系统环境）') + '</i></div>'
      + '<div class="tr"><b>结果</b><i>' + esc(d.summary || '') + '</i></div>'
      + (miss.length
        ? '<div class="tr res"><b>缺失</b><i>' + miss.map((m) => esc(m.name)).join('、') + '</i></div>'
          + '<div class="tr"><b>修复</b><i class="mono">' + esc(d.interpreter) + ' -m pip install ' + esc(pip) + '</i>'
          + '<br/><span class="muted">包名可能和 import 名不一样，装完再点一次验证</span></div>'
        : '')
      + '</div>'
    );
    toast(ok ? '环境验证通过：这份代码在你这里能跑' : '缺 ' + miss.length + ' 个依赖，舞台上有修复命令',
      ok ? 'ok' : 'warn');
  } catch (e) {
    toast('验证失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------------------------- 一键自检 ---------------------------- */
async function selfTest() {
  const btn = $('#btnSelfTest');
  if (btn) btn.disabled = true;
  toast('正在自检：解释器 → 探针 → 最小脚本采集…', '');
  showExplain('<div class="teach"><div class="teach-h"><span class="tl">一键自检 · 进行中</span>'
    + '<span class="tp">…</span></div>'
    + '<div class="tr"><b>说明</b><i>正在用当前选中的解释器逐步验证整条链路，最多约 1 分钟</i></div></div>');
  try {
    const r = await fetch('/api/selftest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interpreter: S.interpreter || '' }),
    });
    const d = await r.json();
    if (!d.steps) { toast(d.error || '自检失败', 'err'); return; }
    const rows = d.steps.map((s, i) => {
      const mark = s.ok ? '✅' : '❌';
      const body = (s.stdout ? '<pre class="st-out">' + esc(String(s.stdout).slice(-800)) + '</pre>' : '')
        + (s.stderr ? '<pre class="st-out err">' + esc(String(s.stderr).slice(-800)) + '</pre>' : '');
      return '<div class="tr"><b>' + mark + ' 第' + (i + 1) + '步 ' + esc(s.name) + '</b>'
        + '<i>退出码 ' + esc(String(s.code)) + '<br/><span class="mono">' + esc(s.cmd) + '</span>'
        + (body ? '<br/>' + body : '') + '</i></div>';
    }).join('');
    const okAll = d.ok;
    showExplain(
      '<div class="teach">'
      + '<div class="teach-h"><span class="tl">一键自检 · Python ' + esc(d.python || '') + '</span>'
      + '<span class="tp ' + (okAll ? 'ok' : 'bad') + '">' + (okAll ? '链路全部正常' : '有步骤失败') + '</span></div>'
      + rows
      + (okAll
        ? '<div class="tr"><b>结论</b><i>整条链路正常。如果运行你的文件仍失败，问题出在文件本身'
          + '（依赖 / 编码 / 路径）—— 点「验证环境」继续定位</i></div>'
        : '<div class="tr"><b>下一步</b><i>把失败步骤的「原始输出」整段复制发给开发者，'
          + '即可精确定位（这是解释器自己说的话，不是猜测）</i></div>')
      + '</div>'
    );
    toast(okAll ? '自检通过：整条链路正常' : '自检发现失败步骤，舞台上有原始输出', okAll ? 'ok' : 'err');
  } catch (e) {
    toast('自检请求失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------------------------- 文件与示例 ---------------------------- */
function loadCode(code, name) {
  S.code = code;
  S.filename = name || 'main.py';
  S.stmtMap = new Map();
  S.sim = false;
  const sc = $('#simChip'); if (sc) sc.hidden = true;
  analyze();
  resetState();
  renderCode();
  renderFileTree();
  renderReadCard();          // 这个文件是什么（启动入口 / 模块 / 独立脚本）
  $('#fileChip').textContent = S.filename;
  $('#fileChip').classList.add('on');
  if (typeof fitTopbar === 'function') fitTopbar(true);   // 同上：内容变化 RO 察觉不到，主动量一次
  $('#dropzone').classList.remove('show');
  syncDocFileSel();
  if (typeof GV !== 'undefined' && GV.view === 'doc') loadExplain(true);
  flush();
}

function readFile(file) {
  if (!file) return;
  if (S.demo) { toast('在线演示版不支持上传文件，请从「内置示例」中选择', 'err'); return; }
  if (file.size > 2 * 1024 * 1024) { toast('文件超过 2MB', 'err'); return; }
  const rd = new FileReader();
  rd.onload = () => {
    loadProject([{ name: file.name, code: String(rd.result) }]);
    toast(`已载入 ${file.name}`, 'ok');
  };
  rd.readAsText(file, 'utf-8');
}

/* ---- 多文件 / 文件夹项目 ---- */
const TEXT_EXTS = ['.py', '.txt', '.cfg', '.toml', '.ini', '.json', '.csv', '.md', '.yaml', '.yml'];
/* 选目录时直接跳过的目录：这些目录里的文件既不是用户代码，也不是语义注解需要的内容。
   之前不加过滤，一个带 .git / venv / node_modules 的项目会被浏览器数成上万个文件
   （弹「是否上传 15,153 个文件」，确认后还会因为数量超限被拒），体验极差 */
const DIR_SKIP = new Set([
  '.git', '.svn', '.hg', '__pycache__', '.venv', 'venv', 'env', '.env',
  'node_modules', '.idea', '.vscode', '.mypy_cache', '.pytest_cache', '.ruff_cache',
  '.tox', '.eggs', '__pypackages__', 'site-packages', 'dist-packages',
  'build', 'dist', '.ipynb_checkpoints', '.gradle', 'target', 'runtime',
]);
const MAX_PROJECT_FILES = 2000;         // 与后端 /api/list_files 的扫描上限对齐（v3.0 大项目支持）
const MAX_SEND_FILES = 200;             // 随入口一起传上去的文件数上限（后端 extra[:200]）
const MAX_SEND_BYTES = 12 * 1024 * 1024; // 单条 WebSocket 消息的总字节预算（超过会被直接断连）

/* 路径里只要有一段命中 DIR_SKIP，整条丢弃 */
function inSkippedDir(name) {
  return String(name).split('/').some((seg) => DIR_SKIP.has(seg));
}

function entryOf(files) {
  // 优先：项目扫描识别出的启动入口 > 含 __main__ > main.py > 根目录第一个
  if (S.projectMap && S.projectMap.startups && S.projectMap.startups.length) {
    return S.projectMap.startups[0];
  }
  const py = files.filter((f) => f.name.endsWith('.py'));
  const withMain = py.filter((f) => /if\s+__name__\s*==\s*["']__main__["']/.test(f.code));
  if (withMain.length) return withMain[0].name;
  const mains = py.filter((f) => f.name === 'main.py');
  if (mains.length) return 'main.py';
  const root = py.filter((f) => !f.name.includes('/')).map((f) => f.name);
  return root[0] || (py[0] && py[0].name) || files[0].name;
}

/* ---- 项目结构扫描：启动入口 / 模块分级（入口 ≠ 所有 .py） ---- */
const KIND_META = {
  startup: { icon: '🚀', label: '启动入口', cls: 'start' },
  module: { icon: '📦', label: '模块', cls: 'mod' },
  orphan: { icon: '🧩', label: '独立脚本', cls: 'orph' },
  unknown: { icon: '❔', label: '无法解析', cls: 'unknown' },
};

function mapOf(name) {
  if (!S.projectMap || !S.projectMap.files) return null;
  return S.projectMap.files.find((x) => x.name === name) || null;
}

async function updateProjectMap() {
  const files = S.projectFiles;
  if (!files || files.length < 2) { S.projectMap = null; markGraphAvail(); return; }
  try {
    const r = await (await fetch('/api/project_map', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ files: files.map((f) => ({ name: f.name, code: f.code })) }),
    })).json();
    if (r && Array.isArray(r.files)) {
      if (!r.files.length) {
        toast('结构扫描没有解析到任何 .py 文件 —— 这个目录里可能只有其他语言的源码（ts/go/rs 等），全景图只支持 Python 项目', '');
        return;
      }
      S.projectMap = r;
      renderFileTree();          // 角标随分级刷新
      fillEntrySel();            // 下拉框分组刷新
      markGraphAvail();
      if (GV.view === 'graph') renderGraph();
      // 当前打开的文件不属于这个项目（或还没打开）→ 自动打开启动入口，用户从"门"开始
      const names = files.map((f) => f.name);
      if (!S.filename || !names.includes(S.filename)) {
        const entry = entryOf(files);
        const f = files.find((x) => x.name === entry);
        if (f) loadCode(f.code, f.name);
      } else {
        renderReadCard();        // 阅读卡随分级刷新
      }
    }
  } catch (e) {
    toast('项目结构扫描失败（' + (e && e.message ? e.message : '网络/服务异常') + '）—— 全景图暂时不可用，文件树与运行不受影响', '');
  }
}

function fillEntrySel() {
  const sel = $('#entrySel');
  if (!sel) return;
  const files = S.projectFiles;
  if (!files || files.length < 2) { sel.hidden = true; sel.innerHTML = ''; return; }
  const pyNames = files.filter((f) => f.name.endsWith('.py')).map((f) => f.name);
  // 顺序以项目扫描结果为准（启动入口在前、有 __main__ 的优先）
  const ordered = (S.projectMap && S.projectMap.files)
    ? S.projectMap.files.map((x) => x.name).filter((n) => pyNames.includes(n))
    : pyNames;
  const groups = { startup: [], module: [], orphan: [], unknown: [] };
  for (const n of ordered) {
    const m = mapOf(n);
    groups[m ? m.kind : 'unknown'].push(n);
  }
  const cur = S.filename;
  sel.hidden = false;
  sel.innerHTML = '';
  const defs = [
    ['startup', '🚀 启动入口（从这里开始读）'],
    ['module', '📦 模块（被导入，不需要运行）'],
    ['orphan', '🧩 独立脚本'],
    ['unknown', '❔ 无法解析'],
  ];
  for (const [k, label] of defs) {
    if (!groups[k].length) continue;
    const og = document.createElement('optgroup');
    og.label = label;
    for (const n of groups[k]) {
      const o = document.createElement('option');
      o.value = n;
      o.textContent = (k === 'startup' ? '🚀 ' : k === 'module' ? '📦 ' : '') + n;
      if (n === cur) o.selected = true;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  // 当前文件不属于项目（或没打开）时，默认落在第一个启动入口
  if (!cur || !pyNames.includes(cur)) {
    const first = sel.querySelector('option');
    if (first) first.selected = true;
  }
}

/* ---- 阅读卡：这一段代码是什么（不需要运行也能讲清楚） ---- */
function renderReadCard() {
  if (!S.filename || !S.projectMap) { S.readCard = ''; return; }
  const m = mapOf(S.filename);
  if (!m) { S.readCard = ''; return; }
  const meta = KIND_META[m.kind] || KIND_META.unknown;
  const defs = (m.defs || []).slice(0, 8);
  const usedBy = (m.imported_by || []).slice(0, 4);
  let head = '';
  if (m.kind === 'startup') {
    head = `<div class="es-title">${meta.icon} 这是项目的启动入口</div>
      <div class="es-why">${esc(m.reason || '')}${m.cli_tool ? '；它是命令行工具，参数填在顶栏「参数」里' : ''}。
      点右上角「运行」，就能看到它带着整个程序跑起来的过程。</div>`;
  } else if (m.kind === 'module') {
    head = `<div class="es-title">${meta.icon} 这是一个模块（软编码）—— 不需要运行它</div>
      <div class="es-why">${esc(m.reason || '')}。它的用途是被别的文件 import：真正执行它的是入口文件，
      你在这里要做的是<b>读懂它提供了什么</b>，而不是运行它。</div>`;
  } else {
    head = `<div class="es-title">${meta.icon} 独立脚本</div>
      <div class="es-why">${esc(m.reason || '')}。可以直接运行，也可以按阅读模式看它每一行在做什么。</div>`;
  }
  const provides = defs.length
    ? `<div class="tr"><b>提供</b><i>${defs.map((d) => `<code>${esc(d)}</code>`).join(' ')}</i></div>` : '';
  const users = usedBy.length
    ? `<div class="tr"><b>被谁用</b><i>${usedBy.map((u) => `<a href="#" class="rd-open" data-file="${esc(u)}">${esc(u)}</a>`).join('、')}</i></div>` : '';
  S.readCard = `<div class="es-card read-card">${head}${provides}${users}</div>`;
  $$('#stageExplainBody .rd-open').forEach((a) => {
    a.onclick = (e) => { e.preventDefault(); openTreeFile(a.dataset.file); };
  });
  // 舞台上有内容时立即刷新（showExplain 会做 read-card 去重）
  const body = $('#stageExplainBody');
  if (body && body.innerHTML) showExplain(body.innerHTML);
}

/* ---- 文件树：源码左侧显示已打开的文件/文件夹内容 ---- */
function renderFileTree() {
  const tree = $('#fileTree'), body = $('#ftBody');
  if (!tree || !body) return;
  const files = (S.projectFiles && S.projectFiles.length) ? S.projectFiles
    : (S.code ? [{ name: S.filename, code: S.code }] : null);
  const btn = $('#btnCloseFile');
  if (btn) btn.hidden = !S.code;
  if (!files || !files.length) { tree.hidden = true; syncFtHandle(); return; }
  tree.hidden = false;
  const cnt = $('#ftCount');
  if (cnt) cnt.textContent = files.length > 1 ? `${files.length} 个` : '';
  S.ftFold = S.ftFold || new Set();   // 记录被手动折叠的目录
  // 大项目（>600 文件）默认折叠所有子目录，只显示第一层——全展开的话 DOM 动辄上千行；
  // 当前打开文件所在的路径保持展开，用户点开任意目录后按手动状态走
  if (files.length > 600 && !S.ftFoldInit) {
    S.ftFoldInit = true;
    const dirs = new Set();
    for (const f of files) {
      const parts = f.name.split('/');
      for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join('/'));
    }
    const openDirs = new Set();
    if (S.filename) {
      const parts = S.filename.split('/');
      for (let i = 1; i < parts.length; i++) openDirs.add(parts.slice(0, i).join('/'));
    }
    for (const d of dirs) if (!openDirs.has(d)) S.ftFold.add(d);
  }
  // 构建嵌套树
  const root = {};
  for (const f of files) {
    const parts = f.name.split('/');
    let cur = root;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!cur[parts[i]] || cur[parts[i]].name) cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = f;
  }
  const html = [];
  const render = (node, path, depth) => {
    const keys = Object.keys(node).sort((a, b) => {
      const da = node[a].name ? 1 : 0, db = node[b].name ? 1 : 0;
      return da - db || a.localeCompare(b);   // 文件夹在前、文件在后
    });
    for (const k of keys) {
      const v = node[k];
      const full = path ? `${path}/${k}` : k;
      if (v.name) {
        const act = full === S.filename ? ' act' : '';
        const m = mapOf(full);
        const km = m ? (KIND_META[m.kind] || {}) : null;
        const tag = km && m.kind === 'startup'
          ? `<i class="ft-kind start" title="${esc(m.reason || '启动入口')}">🚀</i>`
          : km && m.kind === 'module'
            ? `<i class="ft-kind mod" title="${esc(m.reason || '模块')}">📦</i>` : '';
        const hasMain = !tag && /if\s+__name__\s*==\s*["']__main__["']/.test(v.code || '');
        html.push(`<div class="ft-row file${act}" data-f="${esc(full)}" title="${esc(full)}" style="padding-left:${10 + depth * 13}px">`
          + `${tag || (hasMain ? '<i class="ft-kind start">🚀</i>' : '')}📄 ${esc(k)}</div>`);
      } else {
        const folded = S.ftFold.has(full);
        html.push(`<div class="ft-row dir" data-d="${esc(full)}" style="padding-left:${10 + depth * 13}px">${folded ? '▸' : '▾'} 📁 ${esc(k)}</div>`);
        if (!folded) render(v, full, depth + 1);
      }
    }
  };
  render(root, '', 0);
  body.innerHTML = html.join('');
  syncFtHandle();
}

function syncFtHandle() {
  const tree = $('#fileTree'), h = $('#ftHandle');
  if (!tree || !h) return;
  h.classList.toggle('show', !tree.hidden);
  h.textContent = tree.classList.contains('folded') ? '›' : '‹';
  h.title = tree.classList.contains('folded') ? '展开文件树' : '收起文件树';
}

function openTreeFile(name) {
  const f = S.projectFiles && S.projectFiles.find((x) => x.name === name);
  if (!f || name === S.filename) return;
  if (S.playing) stopPlay();
  loadCode(f.code, f.name);
  const esel = $('#entrySel');
  if (esel && !esel.hidden) esel.value = name;
  toast(`已切换到 ${name}`, '');
}

/* ---- 跨文件跟随：进入函数 / 面包屑返回 ---- */
function bindCrumbs() {
  $$('#stageExplainBody [data-crumb]').forEach((b) => {
    b.onclick = () => {
      if (b.dataset.crumb === 'back') { navBack(); return; }
      const i = parseInt(b.dataset.crumb, 10);
      const target = NAV[i];
      NAV = NAV.slice(0, i);
      if (target) { openTreeFile(target.file); setTimeout(() => flashLine(target.line), 60); }
      renderCrumb();
      if ($('#stageExplainBody')) showExplain($('#stageExplainBody').innerHTML);
    };
  });
}

function renderCrumb() {
  if (!NAV.length) { S.crumbHtml = ''; return; }
  const items = NAV.map((n, i) =>
    `<button class="crumb" data-crumb="${i}">${esc(n.file.split('/').pop())} · 第 ${n.line} 行</button>`
  ).join('<span class="crumb-sep">›</span>');
  S.crumbHtml = `<div class="crumb-bar">
    ${items}<span class="crumb-sep">›</span><b>${esc((S.filename || '').split('/').pop())}</b>
    <button class="crumb back" data-crumb="back">← 返回主线</button>
  </div>`;
}

function navInto(file, line) {
  if (!file) return;
  if (S.filename && S.filename !== file) NAV.push({ file: S.filename, line: S.curLine || 1 });
  openTreeFile(file);
  setTimeout(() => flashLine(line), 90);
  renderCrumb();
  if ($('#stageExplainBody')) showExplain($('#stageExplainBody').innerHTML);
  toast(`跟着调用进入 ${file.split('/').pop()} 第 ${line} 行`, '');
}

function navBack() {
  const t = NAV.pop();
  if (!t) return;
  openTreeFile(t.file);
  setTimeout(() => flashLine(t.line), 60);
  renderCrumb();
  if ($('#stageExplainBody')) showExplain($('#stageExplainBody').innerHTML);
}

function flashLine(n) {
  const el = document.querySelector(`#lines .ln[data-l="${n}"]`);
  if (!el) return;
  markLine(n);
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1400);
}

function closeFile() {
  if (S.playing) stopPlay();
  if (S.ws && S.ws.readyState === 1) {
    try { S.ws.send(JSON.stringify({ cmd: 'stop' })); } catch (e) {}
    try { S.ws.close(); } catch (e) {}
    S.ws = null;
  }
  S.projectFiles = null;
  S.ftFold = new Set();
  S.code = '';
  S.filename = '';
  S.stmtMap = new Map();
  S.sim = false;
  const sc = $('#simChip'); if (sc) sc.hidden = true;
  const esel = $('#entrySel'); if (esel) { esel.hidden = true; esel.innerHTML = ''; }
  resetState();
  renderCode();
  renderFileTree();
  stageReset();
  DOC.data = null; DOC.key = '';
  if (typeof GV !== 'undefined' && GV.view === 'doc') setView('stage');
  toast('已关闭文件，拖入新的 .py / 文件夹可重新开始', '');
}

/* ---------- 最近项目列表：打开过的目录 / 单文件自动落账，一键恢复 ----------
   存 localStorage（单机应用，不需要后端）：
   - 目录项目只记磁盘路径 —— 恢复时调 /api/list_files 重扫磁盘，永远是最新内容；
   - 单文件把内容一并存下（≤400KB），点一下原样打开；超限只记名字。 */
const RECENT_KEY = 'pytrace.recent';
const RECENT_MAX = 8;
const RECENT_INLINE_MAX = 400 * 1024;   // 单文件随记录内嵌内容的大小上限

function recentGet() {
  try {
    const arr = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    return Array.isArray(arr) ? arr : [];
  } catch (e) { return []; }
}

function recentSave(list) {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX))); }
  catch (e) {
    // localStorage 满（5MB 上限）：把大内容记录瘦身再存一次，还不行就静默放弃
    try {
      const slim = list.slice(0, RECENT_MAX).map((it) => ({ ...it, code: '' }));
      localStorage.setItem(RECENT_KEY, JSON.stringify(slim));
    } catch (e2) { /* 忽略：最近记录是锦上添花，不该打扰用户 */ }
  }
}

function addRecent(entry) {
  if (S.demo) return;
  const key = entry.kind === 'dir' ? 'dir:' + entry.root
                                   : 'file:' + entry.filename + ':' + (entry.code || '').length;
  const list = recentGet().filter((it) => it.key !== key);
  list.unshift({ ...entry, key, ts: Date.now() });
  recentSave(list);
  renderRecent();
}

function timeAgo(ts) {
  const s = Math.max(1, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
  if (s < 86400 * 7) return Math.floor(s / 86400) + ' 天前';
  const d = new Date(ts);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function rootBase(p) {
  const norm = String(p || '').replace(/[\\/]+$/, '');
  const m = norm.split(/[\\/]/);
  return m[m.length - 1] || norm;
}

function renderRecent() {
  const list = recentGet();
  const box = $('#rpList'), empty = $('#rpEmpty');
  if (!box) return;
  box.innerHTML = '';
  empty.hidden = list.length > 0;
  for (const it of list) {
    const row = document.createElement('div');
    row.className = 'rp-i';
    const ico = it.kind === 'dir' ? '📁' : '📄';
    const sub = it.kind === 'dir'
      ? it.root + (it.nFiles ? ` · ${it.nFiles} 个文件` : '')
      : (it.code ? it.filename + ' · 已保存内容' : it.filename + ' · 需重新打开');
    row.innerHTML = `<span class="rp-ico">${ico}</span>
      <div class="rp-body"><div class="rp-name">${esc(it.name || it.filename || it.root)}</div>
      <div class="rp-sub">${esc(sub)}</div></div>
      <span class="rp-time">${timeAgo(it.ts)}</span>
      <button class="rp-del" title="从列表移除">×</button>`;
    row.querySelector('.rp-del').onclick = (e) => {
      e.stopPropagation();
      recentSave(recentGet().filter((x) => x.key !== it.key));
      renderRecent();
    };
    row.onclick = () => openRecent(it);
    box.appendChild(row);
  }
}

async function openRecent(it) {
  $('#recentPanel').hidden = true;
  if (it.kind === 'dir') {
    const rin = $('#rootInput');
    rin.value = it.root;
    S.root = it.root;
    localStorage.setItem('pytrace.root', it.root);
    loadInterpreters();
    const ok = await loadLocalProject();   // 成功时内部会刷新这条记录的时间戳
    if (!ok) toast('目录读不到文件：可能已被移动或删除 —— ' + it.root, 'err');
    return;
  }
  if (it.code) {
    loadProject([{ name: it.filename, code: it.code }]);
    toast(`已恢复 ${it.filename}`, 'ok');
  } else {
    toast('这条记录没保存内容（文件太大），请重新打开一次', 'err');
  }
}

/* ---------- 本地项目目录扫描：填路径即载入整个文件夹 ---------- */
async function loadLocalProject() {
  if (S.demo || !S.root) return false;
  try {
    const d = await (await fetch('/api/list_files?root=' + encodeURIComponent(S.root))).json();
    const items = d.items || [];
    if (!items.length) return false;
    filesToProject(items, d.truncated, d.skipped);
    addRecent({ kind: 'dir', name: rootBase(S.root), root: S.root, nFiles: items.length });
    return true;
  } catch (e) { return false; }
}

function filesToProject(files, truncated, skippedCount) {
  if (!files || !files.length) return;
  files.sort((a, b) => a.name.localeCompare(b.name));
  if (files.length > 1) {
    S.projectFiles = files;
    S.projectMap = null;
    S.coverage = new Map();      // 换项目 → 上一轮的运行覆盖作废
    S.ftFold = new Set(); S.ftFoldInit = false;   // 折叠状态随项目重置
    fillEntrySel();
    updateProjectMap();          // 内部会刷新角标/下拉/阅读卡，并在需要时自动打开启动入口
    let tip = `已载入本地项目：${files.length} 个文件`;
    if (truncated && skippedCount) tip += `（还有 ${skippedCount} 个文件超出上限，未载入）`;
    else if (truncated) tip += '（文件过多，已截断）';
    toast(tip, truncated ? '' : 'ok');
    const cur = files.find((f) => f.name === S.filename);
    if (cur) { cur.code = S.code; renderFileTree(); }
  }
}

function loadProject(files, skippedCount) {
  if (!files || !files.length) return;
  files.sort((a, b) => a.name.localeCompare(b.name));
  if (files.length > 1) {
    S.projectFiles = files;
    S.projectMap = null;
    S.coverage = new Map();      // 换项目 → 上一轮的运行覆盖作废
    S.ftFold = new Set(); S.ftFoldInit = false;   // 折叠状态随项目重置
    fillEntrySel();
    updateProjectMap();          // 内部处理自动打开启动入口 + 阅读卡
    toast(`已载入项目：${files.length} 个文件` + (skippedCount ? `（跳过 ${skippedCount} 个超 2MB 的文件）` : ''), 'ok');
    return;
  }
  // 单文件：直接打开它本身。
  // 这里以前写的是 files.find((f) => f.name === entry) —— entry 是个不存在的变量，
  // 一旦走到这个分支就抛 ReferenceError，表现就是「关掉文件后再打开，怎么点都没反应」。
  S.projectFiles = null;
  S.projectMap = null;
  S.coverage = new Map();
  const esel = $('#entrySel');
  if (esel) { esel.hidden = true; esel.innerHTML = ''; }
  if (skippedCount) toast(`跳过 ${skippedCount} 个超过 2MB 的文件`, '');
  const f = files[0];
  loadCode(f.code, f.name);
  // 落「最近」账：内容不大就随记录内嵌，恢复时点一下原样打开
  if (f.name && (f.code || '').length <= RECENT_INLINE_MAX) {
    addRecent({ kind: 'file', name: f.name, filename: f.name, code: f.code });
  } else if (f.name) {
    addRecent({ kind: 'file', name: f.name, filename: f.name, code: '' });
  }
}

/* 统一的「文件列表 → 项目」入口：过滤垃圾目录 / 超限 / 超大文件，再做一次明确的告知 */
function readFileList(list) {
  if (S.demo) { toast('在线演示版不支持上传文件，请从「内置示例」中选择', 'err'); return; }
  const all = [...list];
  // 先按「是不是垃圾目录」分流，报错时能把话说清楚：不是不支持上传，是这些目录不该看
  const keepName = (f) => {
    const n = (f.webkitRelativePath || f.name).replace(/^[^/]+\//, '');
    return n.endsWith('.py') || TEXT_EXTS.some((x) => n.endsWith(x));
  };
  const inJunk = all.filter((f) => {
    const n = (f.webkitRelativePath || f.name).replace(/^[^/]+\//, '');
    return inSkippedDir(n);
  });
  let files = all.filter((f) => keepName(f) && !inSkippedDir((f.webkitRelativePath || f.name).replace(/^[^/]+\//, '')));

  if (!files.length) {
    toast(all.length
      ? `这个目录里没有可读的代码文件（共 ${all.length} 个文件，其中 ${inJunk.length} 个在 .git/venv 等目录里）`
      : '未找到可读的代码/文本文件（支持 .py .txt .json .md 等）', 'err');
    return;
  }
  if (files.length > MAX_PROJECT_FILES) {
    toast(`过滤 .git/venv/node_modules 等目录后仍有 ${files.length} 个代码文件，超过 ${MAX_PROJECT_FILES} 个上限。`
      + '请选项目里的子目录（比如 src/）再导入', 'err');
    return;
  }
  let total = 0;
  for (const f of files) {
    if (f.size > 2 * 1024 * 1024) continue;
    total += f.size;
  }
  if (total > 32 * 1024 * 1024) { toast('项目总大小超过 32MB，请精简后再导入', 'err'); return; }
  const skippedBig = files.filter((f) => f.size > 2 * 1024 * 1024).length;
  let done = 0;
  const out = [];
  const totalN = files.length;
  for (const f of files) {
    if (f.size > 2 * 1024 * 1024) { done++; continue; }
    const rd = new FileReader();
    rd.onload = () => {
      out.push({ name: (f.webkitRelativePath || f.name).replace(/^[^/]+\//, ''), code: String(rd.result) });
      if (++done === totalN) loadProject(out, skippedBig);
    };
    rd.onerror = () => { if (++done === totalN) loadProject(out, skippedBig); };
    rd.readAsText(f, 'utf-8');
  }
  if (inJunk.length) {
    toast(`已自动跳过 ${inJunk.length} 个位于 .git / venv / node_modules 等目录的文件`, '');
  }
}

/* 文件夹选择（Chrome/Edge 的 File System Access API）：
   能一边遍历一边跳过垃圾目录，不会像 <input webkitdirectory> 那样让浏览器先把
   上万个文件全部数一遍再弹「是否上传 15,153 个文件」。不支持时回退到 input。 */
async function pickDirViaFSA() {
  if (S.demo) { toast('在线演示版不支持上传文件', 'err'); return; }
  if (!window.showDirectoryPicker) { $('#dirInput').click(); return; }
  let handle;
  try {
    handle = await window.showDirectoryPicker({ mode: 'read' });
  } catch (e) {
    if (e && e.name === 'AbortError') return;   // 用户取消，静默
    $('#dirInput').click();                     // 权限/浏览器限制 → 回退
    return;
  }
  const out = [];
  let skippedDirs = 0;
  let skippedBig = 0;
  let overLimit = false;
  const walk = async (dir, prefix) => {
    for await (const [name, h] of dir.entries()) {
      if (out.length >= MAX_PROJECT_FILES) { overLimit = true; return; }
      if (h.kind === 'directory') {
        if (DIR_SKIP.has(name)) { skippedDirs++; continue; }
        await walk(h, prefix ? `${prefix}/${name}` : name);
        continue;
      }
      if (!TEXT_EXTS.some((x) => name.endsWith(x))) continue;
      const file = await h.getFile();
      if (file.size > 2 * 1024 * 1024) { skippedBig++; continue; }
      try {
        out.push({ name: prefix ? `${prefix}/${name}` : name, code: await file.text() });
      } catch (e) { /* 单个文件读失败不影响整体 */ }
    }
  };
  toast('正在扫描文件夹…', '');
  try {
    await walk(handle, '');
  } catch (e) {
    toast('读取文件夹失败：' + (e && e.message ? e.message : e), 'err');
    return;
  }
  if (!out.length) { toast('这个文件夹里没有可读的代码文件', 'err'); return; }
  const notes = [];
  if (skippedDirs) notes.push(`跳过 ${skippedDirs} 个 .git/venv 等目录`);
  if (skippedBig) notes.push(`跳过 ${skippedBig} 个超过 2MB 的文件`);
  if (overLimit) notes.push(`代码文件超过 ${MAX_PROJECT_FILES} 个，已只取前 ${out.length} 个`);
  loadProject(out, skippedBig);
  if (notes.length) toast(notes.join(' · '), 'warn');
}

/* 拖拽支持整个文件夹（递归收集，同样跳过垃圾目录 + 数量上限） */
function collectDropped(items, out) {
  const entries = [];
  for (const it of items) {
    const en = it.webkitGetAsEntry && it.webkitGetAsEntry();
    if (en) entries.push(en);
  }
  if (!entries.length) return null;
  return new Promise((resolve) => {
    let pending = entries.length;
    if (!pending) return resolve([]);
    const walk = (entry, path) => {
      if (out.length >= MAX_PROJECT_FILES) { next(); return; }
      if (entry.isFile) {
        entry.file((f) => {
          const full = path ? `${path}/${f.name}` : f.name;
          if (TEXT_EXTS.some((x) => full.endsWith(x)) && f.size <= 2 * 1024 * 1024) {
            const rd = new FileReader();
            rd.onload = () => { out.push({ name: full, code: String(rd.result) }); next(); };
            rd.onerror = () => next();
            rd.readAsText(f, 'utf-8');
          } else next();
        }, () => next());
      } else if (entry.isDirectory) {
        if (DIR_SKIP.has(entry.name)) { next(); return; }   // .git / venv / node_modules 整棵子树跳过
        const rd = entry.createReader();
        rd.readEntries((es) => {
          pending += es.length;
          for (const e2 of es) walk(e2, path ? `${path}/${entry.name}` : entry.name);
          next();
        }, () => next());
      } else next();
    };
    const next = () => { if (--pending === 0) resolve(out); };
    for (const en of entries) walk(en, '');
  });
}

async function detectDemo() {
  try {
    const r = await fetch('/api/info');
    const d = await r.json();
    // 版本号常驻显示：避免"跑的是旧版还在报新问题"的排查困境
    if (d.version) {
      const vc = $('#verChip');
      if (vc) { vc.textContent = 'v' + d.version; vc.hidden = false; }
    }
    if (!d.demo) return;
    S.demo = true;
    document.body.classList.add('demo');
    const badge = $('#demoBadge');
    if (badge) badge.hidden = false;
    const open = $('#btnOpen');
    if (open) open.style.display = 'none';
    const openDir = $('#btnOpenDir');
    if (openDir) openDir.style.display = 'none';
    const btnRecent = $('#btnRecent');
    if (btnRecent) btnRecent.style.display = 'none';
    const to = $('#timeoutInput');
    if (to) { to.value = String(d.default_timeout || 6); to.disabled = true; }
    const dz = $('#dropzone');
    if (dz) dz.innerHTML = `<div class="dz-inner"><svg viewBox="0 0 24 24" width="34" height="34"><path d="M12 3l2.6 6.3L21 12l-6.4 2.7L12 21l-2.6-6.3L3 12l6.4-2.7z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg><p>从右上角 <b>示例</b> 里选一段代码</p><small>在线演示版仅内置示例可运行</small></div>`;
  } catch (e) { /* 离线/本地版忽略 */ }
}

async function loadSamples() {
  try {
    const r = await fetch('/api/samples');
    const d = await r.json();
    const sel = $('#sampleSel');
    for (const s of d.samples) {
      const o = document.createElement('option');
      o.value = s.file; o.textContent = s.title || s.name;
      sel.appendChild(o);
    }
    if (d.samples.length) sel.dataset.first = d.samples[0].file;
    // 多文件示例项目：在线演示版没有「打开文件夹」，靠它展示项目全景图 / 一键跑全项目
    const dp = await (await fetch('/api/demo_project')).json();
    if (dp && dp.files && dp.files.length) {
      const o = document.createElement('option');
      o.value = '__demo_project__';
      o.textContent = `📁 多文件示例项目（${dp.files.length} 个文件 · 看全景图）`;
      sel.insertBefore(o, sel.children[1] || null);
    }
  } catch (e) { /* ignore */ }
}

async function loadDemoProject() {
  try {
    const d = await (await fetch('/api/demo_project')).json();
    if (!d || !d.files || !d.files.length) { toast('示例项目不可用（samples/demo_project 缺失）', 'err'); return; }
    filesToProject(d.files.map((f) => ({ name: f.name, code: f.code })));
    toast(`已载入示例项目「${d.name}」：${d.files.length} 个文件`, 'ok');
    // 等扫描结果回来再切全景图，否则会落到"还没有项目"的空态
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 120));
      if (S.projectMap && S.projectMap.files) break;
    }
    if (S.projectMap) {
      setView('graph');
      toast('这就是「项目全景图」：左边是启动入口，右边是它用到的模块。点「跑全部入口」试试', '');
    }
  } catch (e) { toast('载入示例项目失败', 'err'); }
}

async function openSample(file) {
  if (!file) return;
  if (file === '__demo_project__') { await loadDemoProject(); return; }
  const r = await fetch('/api/sample/' + encodeURIComponent(file));
  const d = await r.json();
  S.projectFiles = null;
  const esel = $('#entrySel'); if (esel) { esel.hidden = true; esel.innerHTML = ''; }
  loadCode(d.code, d.name);
  analyze();
}

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('#toastWrap').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = '.3s'; setTimeout(() => el.remove(), 320); }, 3600);
}

/* ---------------------------- 事件绑定 ---------------------------- */
function bind() {
  $('#btnOpen').onclick = () => $('#fileInput').click();
  // 优先走 File System Access API：能边遍历边跳过 .git/venv/node_modules，
  // 避免浏览器把上万个文件全部数完再弹「是否上传 15,153 个文件」
  $('#btnOpenDir').onclick = () => { pickDirViaFSA(); };
  const bcf = $('#btnCloseFile');
  if (bcf) bcf.onclick = closeFile;
  const ftb = $('#ftBody');
  if (ftb) ftb.addEventListener('click', (e) => {
    const d = e.target.closest('.ft-row.dir');
    if (d) {
      S.ftFold = S.ftFold || new Set();
      const k = d.dataset.d;
      if (S.ftFold.has(k)) S.ftFold.delete(k); else S.ftFold.add(k);
      renderFileTree();
      return;
    }
    const f = e.target.closest('.ft-row.file');
    if (f) openTreeFile(f.dataset.f);
  });
  const fth = $('#ftHandle');
  if (fth) fth.onclick = () => {
    const tree = $('#fileTree');
    tree.classList.toggle('folded');
    fth.textContent = tree.classList.contains('folded') ? '›' : '‹';
    fth.title = tree.classList.contains('folded') ? '展开文件树' : '收起文件树';
    syncFtHandle();
  };
  const bl = $('#btnLogic');
  if (bl) bl.onclick = () => { if (S.demo) toast('演示版可正常使用逻辑推演（静态分析，不执行代码）', ''); runLogic(); };
  const blv = $('#btnLive');
  if (blv) blv.onclick = () => openLivePanel();
  const blc = $('#btnLiveClose');
  if (blc) blc.onclick = closeLivePanel;
  const lov = $('#liveOverlay');
  if (lov) lov.addEventListener('click', (e) => { if (e.target === lov) closeLivePanel(); });
  $$('[data-copy]').forEach((b) => {
    b.onclick = () => {
      const txt = document.querySelector(`#${b.dataset.copy} code`);
      const s = txt ? txt.textContent : '';
      if (s && navigator.clipboard) navigator.clipboard.writeText(s).then(
        () => toast('已复制', 'ok'), () => {});
    };
  });
  const be = $('#btnEnvCheck');
  if (be) be.onclick = () => envCheck();
  const bs = $('#btnSelfTest');
  if (bs) bs.onclick = () => selfTest();
  const isel = $('#interpSel');
  if (isel) {
    isel.onchange = () => {
      S.interpreter = isel.value;
      localStorage.setItem('pytrace.interp', isel.value);
      const t = isel.selectedOptions[0];
      toast('已切换解释器：' + (t ? t.textContent : isel.value), 'ok');
    };
  }
  const rin = $('#rootInput');
  if (rin) {
    rin.value = localStorage.getItem('pytrace.root') || '';
    S.root = rin.value.trim();
    rin.onchange = () => {
      S.root = rin.value.trim();
      localStorage.setItem('pytrace.root', S.root);
      loadInterpreters();
      loadLocalProject();          // 填项目目录 → 扫描整个文件夹（建文件树 + 入口识别）
    };
  }
  // 变量时间旅行 ⟲ / 内存引用图 🔗：卡片上的按钮（事件委托——卡片频繁重建，不能逐个绑）
  const sv = $('#stageVars');
  if (sv) {
    sv.addEventListener('click', (e) => {
      const btn = e.target.closest('.vtt');
      if (btn) {
        e.stopPropagation();
        const card = btn.closest('.var');
        if (card) openVarTravel(card.dataset.name);
        return;
      }
      const rbtn = e.target.closest('.ref');
      if (rbtn) {
        e.stopPropagation();
        const card = rbtn.closest('.var');
        if (card) openRefsGraph(card.dataset.name);
      }
    });
  }
  const vttClose = $('#vttClose');
  if (vttClose) vttClose.onclick = closeVarTravel;
  const refsClose = $('#refsClose');
  if (refsClose) refsClose.onclick = closeRefsGraph;
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeVarTravel(); closeRefsGraph(); }
  });
  document.addEventListener('click', (e) => {
    const p = $('#vttPanel');
    if (p && !p.hidden && !p.contains(e.target) && !e.target.closest('.vtt')) closeVarTravel();
    const rp2 = $('#refsPanel');
    if (rp2 && !rp2.hidden && !rp2.contains(e.target) && !e.target.closest('.ref')) closeRefsGraph();
  });
  // 最近项目：点按钮弹出面板；点面板外收起
  const btnRecent = $('#btnRecent'), rp = $('#recentPanel');
  if (btnRecent && rp) {
    btnRecent.onclick = () => {
      if (rp.hidden) {
        renderRecent();
        // 先重新收纳再定位：面板贴着按钮当前真实位置弹出。
        // 不先量的话，按钮若滞留在"曾被收纳挤压"的旧位置，面板会盖住别的控件
        if (typeof fitTopbar === 'function') fitTopbar(true);
        const r = btnRecent.getBoundingClientRect();
        rp.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 356)) + 'px';
        rp.style.top = (r.bottom + 6) + 'px';
        rp.hidden = false;
      } else {
        rp.hidden = true;
      }
    };
    document.addEventListener('click', (e) => {
      if (!rp.hidden && !$('#recentWrap').contains(e.target)) rp.hidden = true;
    });
    const clr = $('#rpClear');
    if (clr) clr.onclick = (e) => {
      e.stopPropagation();
      recentSave([]);
      renderRecent();
      toast('已清空最近记录', '');
    };
  }
  $('#fileInput').onchange = (e) => { readFileList(e.target.files); e.target.value = ''; };
  $('#dirInput').onchange = (e) => { readFileList(e.target.files); e.target.value = ''; };
  const esel = $('#entrySel');
  if (esel) esel.onchange = () => {
    if (!S.projectFiles) return;
    const f = S.projectFiles.find((x) => x.name === esel.value);
    if (f) { stopPlay(); loadCode(f.code, f.name); toast(`入口切换为 ${f.name}`, 'ok'); }
  };
  $('#btnRun').onclick = run;
  $('#btnStop').onclick = stopRun;
  // ---- 断点调试：模式开关 + 工具条 + 快捷键 ----
  const dbgBtn = $('#btnDebug');
  if (dbgBtn) {
    dbgBtn.classList.toggle('on', !!S.debug);
    dbgBtn.onclick = () => {
      S.debug = !S.debug;
      try { localStorage.setItem('pytrace.debug', S.debug ? '1' : '0'); } catch (e) {}
      dbgBtn.classList.toggle('on', S.debug);
      toast(S.debug
        ? '调试模式已开启：在行号上点红点下断点，再点「运行」——程序会真的停在断点处等你'
        : '调试模式已关闭：运行会一路跑到底（断点仍然保留）', S.debug ? 'ok' : '');
    };
  }
  const dbgBind = (sel, cmd) => { const b = $(sel); if (b) b.onclick = () => dbgCmd(cmd); };
  dbgBind('#dbgGo', 'continue');
  dbgBind('#dbgOver', 'step');
  dbgBind('#dbgInto', 'stepin');
  dbgBind('#dbgOut', 'stepout');
  const dbs = $('#dbgStop'); if (dbs) dbs.onclick = stopRun;
  // 快捷键避开 F11（浏览器全屏，拦不住）与 F5（刷新）
  document.addEventListener('keydown', (e) => {
    if (!S.paused) return;
    if (e.key === 'F8') { e.preventDefault(); dbgCmd('continue'); }
    else if (e.key === 'F9') {
      e.preventDefault();
      dbgCmd(e.shiftKey ? 'stepin' : (e.altKey ? 'stepout' : 'step'));
    }
  });
  $('#btnPlay').onclick = togglePlay;
  $('#btnPrev').onclick = () => stepBy(-1);
  $('#btnNext').onclick = () => stepBy(1);
  $('#speedSel').onchange = (e) => { S.speed = parseFloat(e.target.value); };
  $('#showLib').onchange = (e) => { S.showLib = e.target.checked; renderTree(); };
  const sa = $('#stageAnim');
  if (sa) sa.onchange = () => { STG.animOff = !sa.checked; };
  const spd = $('#stageSpeed');
  if (spd) {
    const saved = parseFloat(localStorage.getItem('pytrace.speed') || '');
    if (saved > 0) { spd.value = String(saved); }
    spd.onchange = () => {
      STG.userSpeed = parseFloat(spd.value) || 1;
      try { localStorage.setItem('pytrace.speed', spd.value); } catch (e) {}
      applyUserSpeed();
      const sm = $('#stageMeta');
      if (sm && STG.userSpeed !== 1) sm.textContent = `动画速度 ${STG.userSpeed}×`;
    };
    if (saved > 0) { STG.userSpeed = saved; applyUserSpeed(); }
  }
  const dt = $('#dockToggle');
  if (dt) dt.onclick = () => { $('#dock').classList.toggle('collapsed'); S.flowKey = ''; setTimeout(drawFlow, 240); };
  $('#sampleSel').onchange = (e) => { openSample(e.target.value); e.target.value = ''; };

  // ---- 可拖拽分隔条：左源码栏 / 右检查器宽度，双击复位，localStorage 记忆 ----
  (() => {
    const html = document.documentElement;
    const layout = document.querySelector('.layout');
    if (!layout) return;
    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    const savedL = localStorage.getItem('pytrace.splitL');
    const savedR = localStorage.getItem('pytrace.splitR');
    if (savedL) html.style.setProperty('--w-code', savedL);
    if (savedR) html.style.setProperty('--w-inspect', savedR);
    const hook = (id, prop, key, min, max, asPct) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('pointerdown', (e0) => {
        e0.preventDefault();
        el.classList.add('dragging');
        document.body.classList.add('drag-col');
        const move = (ev) => {
          const r = layout.getBoundingClientRect();
          if (asPct) {
            const v = clamp(((ev.clientX - r.left) / r.width) * 100, min, max);
            html.style.setProperty(prop, v.toFixed(2) + '%');
          } else {
            const v = clamp(r.right - ev.clientX, min, max);
            html.style.setProperty(prop, Math.round(v) + 'px');
          }
        };
        const up = () => {
          el.classList.remove('dragging');
          document.body.classList.remove('drag-col');
          window.removeEventListener('pointermove', move);
          window.removeEventListener('pointerup', up);
          window.removeEventListener('pointercancel', up);
          setTimeout(drawFlow, 60);
          try { localStorage.setItem(key, html.style.getPropertyValue(prop)); } catch (e) {}
        };
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
        window.addEventListener('pointercancel', up);
      });
      el.addEventListener('dblclick', () => {
        html.style.removeProperty(prop);
        try { localStorage.removeItem(key); } catch (e) {}
        setTimeout(drawFlow, 60);
      });
    };
    hook('dragL', '--w-code', 'pytrace.splitL', 15, 55, true);
    hook('dragR', '--w-inspect', 'pytrace.splitR', 220, 560, false);

    // ---- 底部面板区：上边缘水平拖拽条（与左右分隔条同款交互）----
    const dock = document.getElementById('dock');
    const hd = document.getElementById('dragDock');
    if (dock && hd) {
      const saved = localStorage.getItem('pytrace.dockH');
      if (saved) dock.style.height = saved;
      hd.addEventListener('pointerdown', (e0) => {
        e0.preventDefault();
        hd.classList.add('dragging');
        document.body.classList.add('drag-row');
        dock.classList.remove('collapsed');
        const move = (ev) => {
          const bottom = dock.getBoundingClientRect().bottom;
          const v = clamp(bottom - ev.clientY, 60, window.innerHeight * 0.72);
          dock.style.transition = 'none';
          dock.style.height = Math.round(v) + 'px';
        };
        const up = () => {
          hd.classList.remove('dragging');
          document.body.classList.remove('drag-row');
          dock.style.transition = '';
          window.removeEventListener('pointermove', move);
          window.removeEventListener('pointerup', up);
          window.removeEventListener('pointercancel', up);
          setTimeout(drawFlow, 60);
          try { localStorage.setItem('pytrace.dockH', dock.style.height); } catch (e) {}
        };
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
        window.addEventListener('pointercancel', up);
      });
      hd.addEventListener('dblclick', () => {
        dock.style.height = '';
        try { localStorage.removeItem('pytrace.dockH'); } catch (e) {}
        setTimeout(drawFlow, 60);
      });
    }
  })();

  $('#scrub').addEventListener('input', (e) => {
    S.following = false;
    if (S.playing) stopPlay();
    const idx = parseInt(e.target.value, 10) + 1;
    const t = S.events[Math.min(idx - 1, S.events.length - 1)];
    $('#clock').textContent = `${(t ? t.t : 0).toFixed(1)} / ${(S.endMs || S.span).toFixed(1)} ms`;
  });
  $('#scrub').addEventListener('change', (e) => {
    seek(parseInt(e.target.value, 10) + 1);
  });

  $('#treeBody').addEventListener('click', (e) => {
    const row = e.target.closest('.tree-row');
    if (!row) return;
    const id = parseInt(row.dataset.id, 10);
    const sp = S.spans.get(id);
    if (!sp) return;
    S.sel = id;
    S.following = false;
    $$('#treeBody .tree-row.sel').forEach((r) => r.classList.remove('sel'));
    row.classList.add('sel');
    if (sp.line) { markLine(sp.line); }
    markCallSite(sp.callerLine);
    renderInspector();
  });
  $('#btnClearSel').onclick = () => { S.sel = null; markCallSite(0); $$('#treeBody .tree-row.sel').forEach((r) => r.classList.remove('sel')); renderInspector(); };
  $('#inspectBody').addEventListener('click', (e) => {
    const g = e.target.closest('[data-goto]');
    if (g) { S.sel = parseInt(g.dataset.goto, 10); const sp = S.spans.get(S.sel); if (sp) { markLine(sp.line); markCallSite(sp.callerLine); } renderInspector(); renderTree(); }
  });
  $('#statsBody').addEventListener('click', (e) => {
    const g = e.target.closest('[data-jump]');
    if (g) {
      S.sel = parseInt(g.dataset.jump, 10);
      const sp = S.spans.get(S.sel);
      if (sp) { markLine(sp.line); markCallSite(sp.callerLine); }
      $$('#tabs .tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === 'tree'));
      $$('.panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === 'tree'));
      renderInspector(); renderTree();
    }
  });

  $$('#tabs .tab').forEach((t) => {
    t.onclick = () => {
      $$('#tabs .tab').forEach((x) => x.classList.toggle('active', x === t));
      $$('.panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === t.dataset.tab));
    };
  });

  // 拖拽
  const editor = $('.pane-code');
  ['dragenter', 'dragover'].forEach((k) => editor.addEventListener(k, (e) => {
    e.preventDefault();
    if (S.demo) return;
    $('#dropzone').classList.add('show');
  }));
  editor.addEventListener('dragleave', (e) => { if (!S.code) $('#dropzone').classList.remove('show'); });
  editor.addEventListener('drop', (e) => {
    e.preventDefault();
    if (S.demo) { toast('在线演示版不支持上传文件', 'err'); return; }
    const dt = e.dataTransfer;
    if (dt.items && dt.items.length && dt.items[0].webkitGetAsEntry) {
      const out = [];
      collectDropped([...dt.items], out).then((files) => {
        if (files && files.length) { loadProject(files); $('#dropzone').classList.remove('show'); }
        else if (dt.files && dt.files.length) readFileList(dt.files);
      });
    } else if (dt.files && dt.files.length) readFileList(dt.files);
    else $('#dropzone').classList.toggle('show', !S.code);
  });
  $('#dropzone').onclick = () => $('#fileInput').click();

  window.addEventListener('resize', () => { S.flowKey = ''; drawFlow(); updateCursor(); });
  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input,select,textarea')) return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.code === 'ArrowRight') { e.preventDefault(); stepBy(1); }
    else if (e.code === 'ArrowLeft') { e.preventDefault(); stepBy(-1); }
    else if (e.key === 'r' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); run(); }
  });
}

/* ---------------------------- 启动 ---------------------------- */
/* ==================== 接入我的 IDE（注入式探针） ==================== */
const LIVE = {
  ws: null,            // /ws/live 连接
  session: null,       // 当前探针会话 sid
  openedAt: 0,         // 面板打开时刻：之前就在跑的旧会话不回放，只接新的
  buffering: false,    // hello 处理期间（建语义索引）到达的事件先攒着
  buf: [],
};

function liveCopyText(which) {
  const root = window.PYTRACE_ROOT || '<PyTrace 目录>';
  if (which === 'cmdCli') return 'python -m pytrace main.py';
  if (which === 'cmdPath') {
    return (navigator.platform || '').toLowerCase().includes('win')
      ? `set PYTHONPATH=${root}&& python -m pytrace main.py`
      : `PYTHONPATH=${root} python -m pytrace main.py`;
  }
  return `import sys; sys.path.insert(0, r"${root}")\nimport pytrace; pytrace.attach()`;
}

function liveSetStatus(state, text) {
  const dot = document.querySelector('#liveStatus .lv-dot');
  const el = $('#liveStatusText');
  if (dot) dot.className = 'lv-dot ' + (state || 'wait');
  if (el) el.textContent = text || '';
  const b = $('#btnLive');
  if (b) b.classList.toggle('active', state === 'on');
}

async function handleLiveEvent(ev) {
  if (ev.e === 'hello') {
    // 面板打开前就在跑的旧会话不回放（避免突然覆盖你正在看的代码）
    if (ev.ts && LIVE.openedAt && ev.ts < LIVE.openedAt - 2) return;
    // 探针往往几毫秒就跑完：事件会挤在 hello 前后到达，
    // 而我们要先建语义索引（analyze），期间的事件先攒住，装好了一起上舞台
    LIVE.buffering = true;
    LIVE.buf = [];
    // 新会话：载入探针投来的源码，走与本地运行完全相同的渲染管线
    LIVE.session = ev.sid;
    S.filename = ev.name || 'live.py';
    S.code = ev.code || '';
    renderCode();
    await analyze();
    resetState();
    S.mode = 'live';
    STG.silent = false;
    setRunning(true);
    $('#btnStop').hidden = false;
    liveSetStatus('on', `已接入：${S.filename}（Python ${ev.py || ''}，你的进程 pid ${ev.pid || '?'}）`);
    toast(`已接入你的进程：${S.filename} —— 现在跑的就是你自己的环境`, 'ok');
    LIVE.buffering = false;
    const pend = LIVE.buf; LIVE.buf = [];
    for (const e2 of pend) await _liveApply(e2);
    schedule();
    return;
  }
  if (LIVE.buffering) { LIVE.buf.push(ev); return; }
  await _liveApply(ev);
}

async function _liveApply(ev) {
  if (ev.e === 'start') return;
  if (ev.e === 'end') {
    S.events.push(ev);
    if (ev.stats) { S.stats = ev.stats; renderKpi(); }
    setRunning(false);
    document.body.classList.remove('running');
    S.mode = 'done';
    flush();
    maybeEntrySuggest();
    liveSetStatus('wait', '这次运行结束了。再跑一次你的脚本（或运行另一个脚本），这里会继续接收。');
    return;
  }
  S.events.push(ev);
  if (S.following) {
    applyEvent(ev);
    S.cursor = S.events.length;
  }
  schedule();
}

function connectLive() {
  if (LIVE.ws) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  LIVE.ws = ws;
  ws.onmessage = (m) => {
    let data;
    try { data = JSON.parse(m.data); } catch (e) { return; }
    (Array.isArray(data) ? data : [data]).forEach(handleLiveEvent);
  };
  ws.onclose = () => {
    LIVE.ws = null;
    if (!$('#liveOverlay').hidden) liveSetStatus('off', '与 PyTrace 服务的连接断了，请确认服务还在运行。');
  };
  ws.onopen = () => liveSetStatus('wait', '已就绪。在你的 IDE / 终端里运行你的脚本，接入后自动开始演示。');
}

async function openLivePanel() {
  const ov = $('#liveOverlay');
  if (!ov) return;
  // 面板里的命令需要真实的 PyTrace 安装路径
  try {
    const d = await (await fetch('/api/info')).json();
    if (d && d.root) {
      window.PYTRACE_ROOT = d.root;
      const h = $('#cmdHome');
      if (h) h.textContent = d.root;
      // 主命令按当前载入的文件名生成，直接复制就能跑
      const cli = document.querySelector('#cmdCli code');
      if (cli) cli.textContent = `python -m pytrace ${S.filename || 'main.py'}`;
      // 跨目录执行的写法：按平台给出可直接粘贴的命令
      const p = $('#cmdPath');
      if (p) {
        const win = /Windows|Win32/i.test(navigator.userAgent);
        p.textContent = win
          ? `set PYTHONPATH=${d.root} && python -m pytrace main.py`
          : `PYTHONPATH=${d.root} python -m pytrace main.py`;
      }
    }
  } catch (e) { /* 拿不到就用占位 */ }
  LIVE.openedAt = Date.now() / 1000;
  ov.hidden = false;
  liveSetStatus('wait', '等待你的进程接入…照常运行你的脚本即可，接入后自动开始演示。');
  if (!LIVE.ws) connectLive();
}

function closeLivePanel() {
  const ov = $('#liveOverlay');
  if (ov) ov.hidden = true;
}

/* ==========================================================================
   项目全景图 + 一键跑全项目
   --------------------------------------------------------------------------
   为什么要有这块：逐行动画回答的是「这一行在做什么」，但新手打开一个项目时的
   第一个问题往往是「这么多文件，我该从哪个看起、谁在调用谁」。全景图就是答案：
   左边一列是启动入口，右边逐层是被它用到的模块，连线是真实 import / 调用关系，
   「跑全部入口」之后再把真正跑过的文件点亮 —— 一眼看出哪块代码是活的、哪块是死的。
   ========================================================================== */
const GV = {
  view: 'stage',        // stage | graph
  scale: 1, tx: 0, ty: 0,
  sel: '', W: 0, H: 0,
  running: false, stop: false,
  nodes: [], edges: [],
};
const GV_NW = 212, GV_NH = 46, GV_GAPY = 12, GV_PAD = 22, GV_TOP = 56, GV_MAX_COL = 11;

/* 一次运行请求的唯一组装处：舞台单跑、全景图批量跑共用 */
function pickExtraFiles(entryName, quiet) {
  if (!S.projectFiles) return undefined;
  const pool = S.projectFiles.filter((f) => f.name !== entryName && f.name.endsWith('.py'));
  const picked = [];
  let bytes = 0;
  // 整包塞进去会让单条 WebSocket 消息超过上限被直接掐断 —— 数量与体积双重封顶
  for (const f of pool) {
    const sz = (f.code || '').length;
    if (picked.length >= MAX_SEND_FILES || bytes + sz > MAX_SEND_BYTES) break;
    bytes += sz;
    picked.push({ name: f.name, code: f.code });
  }
  if (!quiet && pool.length > picked.length) {
    toast(`项目里有 ${pool.length} 个 .py，随入口传 ${picked.length} 个用于逐行讲解`
      + '（其余不影响运行，只影响跨文件跳转）', 'warn');
  }
  return picked.length ? picked : undefined;
}

function buildRunPayload(code, filename, timeout, quiet) {
  const argvStr = ($('#argvInput') ? $('#argvInput').value : '').trim();
  // 简单分词：支持用引号包住带空格的参数
  const argv = argvStr ? (argvStr.match(/"[^"]*"|'[^']*'|\S+/g) || [])
    .map((s) => s.replace(/^["']|["']$/g, '')) : [];
  // 断点只发"当前这个运行文件"的：探针只在该文件里逐行追踪，
  // 把别的文件的断点发过去只会让人以为"打了没生效"
  const me = baseName(filename);
  const breakpoints = [...S.breaks]
    .filter((k) => k.startsWith(me + ':'))
    .map((k) => ({ file: me, line: parseInt(k.slice(me.length + 1), 10) }))
    .filter((b) => b.line > 0);
  return {
    code, filename, timeout, argv,
    files: pickExtraFiles(filename, quiet),
    stub: true, guard: true, instrument: true,
    interpreter: S.interpreter || '', root: S.root || '',
    debug: !!S.debug,
    breakpoints,
  };
}

/* ==========================================================================
   阅读模式：不运行代码，把当前文件按结构逐段讲清楚
   数据来自 /api/explain（纯 AST，零执行）。文案优先级 docstring > 注释 > 事实拼装，
   所以界面上会标注这句话是"作者原话"还是"自动总结"，不混淆。
   ========================================================================== */
function docPyFiles() {
  return (S.projectFiles || []).filter((f) => /\.py$/i.test(f.name));
}

function docKey() {
  return (S.filename || '') + '§' + (S.code || '').length + '§' + (S.code || '').slice(0, 40);
}

async function loadExplain(force) {
  const main = $('#dvMain');
  if (!main) return;
  if (!S.code) {
    main.innerHTML = `<div class="dv-empty"><b>还没有打开文件</b>
      <span>先「打开文件」或「打开文件夹」，再点这里——<br/>讲解不需要运行代码，静态分析就能看。</span></div>`;
    if ($('#dvOutline')) $('#dvOutline').innerHTML = '';
    return;
  }
  const k = docKey();
  if (!force && DOC.data && DOC.key === k) { renderDoc(); return; }

  const myReq = ++DOC.reqId;
  DOC.loading = true;
  main.innerHTML = '<div class="dv-loading"><i></i><span>正在分析结构：切分段落 → 提取调用关系 → 逐行解释…</span></div>';
  $('#dvSub').textContent = `分析中 · ${S.filename}`;

  const files = docPyFiles().slice(0, 200).map((f) => ({ name: f.name, code: f.code }));
  let data = null;
  try {
    const r = await fetch('/api/explain', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: S.code, filename: S.filename, files }),
    });
    data = await r.json();
  } catch (e) {
    data = { ok: false, error: { type: 'Network', msg: '请求失败：' + e.message } };
  }
  if (myReq !== DOC.reqId) return;   // 期间又切了文件，丢弃旧结果
  DOC.loading = false;
  DOC.data = data;
  DOC.key = k;
  renderDoc();
}

function docSegCodeHtml(s) {
  const rows = s.lines || [];
  const codeOnly = rows.filter((r) => !r.more).map((r) => r.code).join('\n');
  const hl = highlightAll(codeOnly);
  let i = 0;
  return rows.map((r) => {
    if (r.more) {
      return `<div class="cl more"><span class="no">…</span><span class="tx">${esc(r.code)}</span></div>`;
    }
    const h = hl[i++] || ' ';
    const why = r.why ? `<span class="wy">${esc(r.why)}</span>` : '<span class="wy"></span>';
    return `<div class="cl" data-l="${r.n}"><span class="no" title="在左侧源码定位这一行">${r.n}</span>`
      + `<span class="tx">${h || ' '}</span>${why}</div>`;
  }).join('');
}

function docDetailHtml(s) {
  const rows = (s.detail || []).filter((d) => d.items && d.items.length);
  if (!rows.length) return '';
  return `<div class="dc-detail">` + rows.map((d) =>
    `<div class="dc-row"><b>${esc(d.t)}</b><div>`
    + d.items.map((it) => `<div class="li">${esc(it)}</div>`).join('')
    + `</div></div>`).join('') + `</div>`;
}

function docMembersHtml(s) {
  const ms = s.members || [];
  if (!ms.length) return '';
  return `<div class="dc-members">` + ms.map((m) => `
    <div class="dc-mem" data-line="${m.start || 0}">
      <span class="mn">${esc(m.name)}</span>
      <span class="ml">${m.start ? 'L' + m.start : ''}</span>
      <span class="ms">${esc(m.summary || '')}</span>
    </div>`).join('') + `</div>`;
}

function docRisksHtml(list, cls) {
  if (!list || !list.length) return '';
  return list.map((r) => `<div class="dc-rk ${cls || ''}">${esc(r)}</div>`).join('');
}

function docSegHtml(s) {
  const m = s.metrics || {};
  const meta = [];
  if (s.start) meta.push(`L${s.start}${s.end && s.end !== s.start ? '–' + s.end : ''}`);
  if (m.lines && m.lines > 1) meta.push(`${m.lines} 行`);
  if (m.cc && m.cc > 1) meta.push(`<span class="cc ${m.cc >= 8 ? 'high' : ''}" title="圈复杂度：分支越多越难读">复杂度 ${m.cc}</span>`);
  if (m.methods) meta.push(`${m.methods} 个方法`);

  const calledBy = (s.called_by || []).filter((x) => x && x.name);
  const cbHtml = calledBy.length
    ? `<div class="dc-row"><b>谁在调它</b><div>${calledBy.map((x) => `<div class="li">${esc(x.name)}`
        + (x.file ? `（${esc(x.file.split('/').pop())}${x.line ? ':' + x.line : ''}）` : '') + `</div>`).join('')}</div></div>`
    : '';

  const srcTag = s.src === 'docstring' ? '<span class="from-doc">作者原话</span>' : '';

  return `<article class="dcard" id="doc-${s.id}" data-id="${s.id}" data-start="${s.start || 0}">
    <header class="dc-head">
      <span class="dc-badge k-${esc(s.kind)}">${esc(s.badge)}</span>
      <h3 title="${esc(s.title)}">${esc(s.title)}</h3>
      <div class="dc-meta">${meta.join(' · ')}</div>
    </header>
    <p class="dc-what">${esc(s.what || '')}${srcTag}</p>
    ${docDetailHtml(s)}
    ${cbHtml ? `<div class="dc-detail">${cbHtml}</div>` : ''}
    ${docMembersHtml(s)}
    ${docRisksHtml(s.risks)}
    <details class="dc-code"${(DOC.openAll || (s.lines || []).length <= 26) ? ' open' : ''}>
      <summary>看代码（${(s.lines || []).length} 行）</summary>
      <div class="dc-lines">${docSegCodeHtml(s)}</div>
    </details>
  </article>`;
}

function renderDoc() {
  const main = $('#dvMain');
  const d = DOC.data;
  if (!main) return;
  if (!d) return;

  if (!d.ok) {
    const err = d.error || {};
    const where = err.line ? `第 ${err.line} 行${err.offset ? ` 第 ${err.offset} 列` : ''}` : '';
    main.innerHTML = `<div class="dv-empty">
      <b>${err.type === 'SyntaxError' ? '这份代码有语法错误，先修好才能讲解' : '这一次没能分析出来'}</b>
      ${where ? `<span>${esc(where)}</span>` : ''}
      <div class="dv-err">${esc((err.type || '') + '：' + (err.msg || '未知错误'))}${err.text ? '\n' + esc(err.text) : ''}</div>
      <span>语法错误的位置是确定的：先按提示改掉那一行，再点「重新分析」。</span>
    </div>`;
    if ($('#dvOutline')) $('#dvOutline').innerHTML = '';
    $('#dvSub').textContent = '分析失败';
    return;
  }

  const st = d.stats || {};
  $('#dvSub').textContent = `${(d.file || '').split('/').pop()} · ${d.kind_cn || ''} · ${st.lines || 0} 行`;

  // ---- 左：目录 ----
  const outline = $('#dvOutline');
  if (outline) {
    const items = (d.outline || []).map((o) =>
      `<button class="dvo" data-target="${esc(o.id)}" data-line="${o.start || 0}">
         <span class="dvo-b">${esc(o.badge)}</span>
         <span class="dvo-t">${esc(o.title)}</span>
         <span class="dvo-l">${o.start || ''}</span>
       </button>`).join('');
    const riskBtn = (d.risks && d.risks.length)
      ? `<div class="dv-warn" data-target="dvRisks">⚠ ${d.risks.length} 处值得留意，点这里看</div>` : '';
    outline.innerHTML = `<div class="dv-oh">导读与统计</div>
      <button class="dvo" data-target="docTop"><span class="dvo-b">导读</span><span class="dvo-t">这个文件是什么</span></button>
      ${riskBtn}<div class="dv-oh">段落（${(d.outline || []).length}）</div>${items}`;
    outline.querySelectorAll('.dvo,.dv-warn').forEach((b) => {
      b.onclick = () => {
        const t = b.dataset.target;
        const el = t === 'docTop' ? main.querySelector('.dv-intro')
          : (t === 'dvRisks' ? main.querySelector('.dv-risks') : document.getElementById('doc-' + t));
        if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' });
        if (b.dataset.line) flashLine(parseInt(b.dataset.line, 10));
      };
    });
  }

  // ---- 右：讲解稿 ----
  const intro = d.intro || {};
  const statsHtml = `<div class="dv-stats">
    <div class="dv-stat"><b>${st.lines || 0}</b><span>总行数</span></div>
    <div class="dv-stat"><b>${st.code || 0}</b><span>有效代码</span></div>
    <div class="dv-stat"><b>${st.functions || 0}</b><span>函数</span></div>
    <div class="dv-stat"><b>${st.classes || 0}</b><span>类</span></div>
    <div class="dv-stat"><b>${st.imports || 0}</b><span>依赖模块</span></div>
  </div>`;

  const introHtml = `<div class="dv-intro">
      <h2>${esc((d.file || '').split('/').pop())}<span class="it-k">${esc(d.kind_cn || '')}</span></h2>
      ${(intro.paras || []).map((p) => `<p>${esc(p)}</p>`).join('')}
      ${(intro.how_to_read || []).length ? `<div class="steps"><b>建议的阅读顺序</b>
        <ol>${intro.how_to_read.map((h) => `<li>${esc(h)}</li>`).join('')}</ol></div>` : ''}
    </div>`;

  const risksHtml = (d.risks && d.risks.length) ? `<div class="dv-risks">
      <h4>⚠ 这几处值得留意（点行号可定位）</h4>
      ${d.risks.map((r) => `<div class="rk"><i data-line="${r.line || 0}">${r.line ? 'L' + r.line : '整体'}</i>
        <em>${esc(r.text)}</em></div>`).join('')}
    </div>` : '';

  main.innerHTML = `<div class="dv-inner">${introHtml}${statsHtml}${risksHtml}
    ${(d.segments || []).map(docSegHtml).join('')}
    <div class="dv-empty" style="height:auto;padding:18px 0 30px">
      <span>以上是全文讲解。想看成"值怎么流动"的动画，切到「舞台」点运行；看文件之间谁调用谁，切到「全景图」。</span>
    </div></div>`;

  // 行号 / 成员 / 风险行点击 → 左侧源码定位
  main.querySelectorAll('.dc-lines .no, .dc-mem, .dv-risks .rk i').forEach((el) => {
    el.onclick = () => {
      const ln = parseInt(el.dataset.line || el.parentElement?.dataset.l || '0', 10);
      if (ln) flashLine(ln);
    };
  });
  // 卡片进入视野 → 目录高亮
  bindDocScroll();
  DOC.openAll = false;
  const tb = $('#dvToggleAll'); if (tb) tb.textContent = '展开代码';
}

let docScrollBound = false;
function bindDocScroll() {
  const main = $('#dvMain');
  if (!main || docScrollBound) return;
  docScrollBound = true;
  main.addEventListener('scroll', () => {
    if (!DOC.data || !DOC.data.ok) return;
    const cards = Array.from(main.querySelectorAll('.dcard'));
    if (!cards.length) return;
    const top = main.getBoundingClientRect().top + 70;
    let cur = cards[0];
    for (const c of cards) {
      if (c.getBoundingClientRect().top <= top) cur = c; else break;
    }
    const id = cur.dataset.id;
    main.parentElement.querySelectorAll('.dv-outline .dvo').forEach((b) => {
      b.classList.toggle('on', b.dataset.target === id);
    });
    cards.forEach((c) => c.classList.toggle('hot', c === cur));
  }, { passive: true });
}

/* 把整篇讲解导出成纯文本（给"讲给别人听"、贴到群里用） */
function docToText() {
  const d = DOC.data;
  if (!d || !d.ok) return '';
  const out = [];
  out.push(`# ${d.file} —— ${d.kind_cn}`);
  const st = d.stats || {};
  out.push(`（${st.lines} 行，${st.functions} 个函数，${st.classes} 个类，依赖 ${st.imports} 个模块）`);
  out.push('');
  (d.intro?.paras || []).forEach((p) => out.push(p));
  if (d.intro?.how_to_read?.length) {
    out.push('');
    out.push('建议阅读顺序：');
    d.intro.how_to_read.forEach((h, i) => out.push(`  ${i + 1}. ${h}`));
  }
  if (d.risks?.length) {
    out.push('');
    out.push('值得留意：');
    d.risks.forEach((r) => out.push(`  ${r.line ? 'L' + r.line : '整体'}：${r.text}`));
  }
  (d.segments || []).forEach((s) => {
    out.push('');
    out.push(`## [${s.badge}] ${s.title}${s.start ? `（L${s.start}-${s.end}）` : ''}`);
    out.push(s.what || '');
    (s.detail || []).forEach((g) => {
      out.push(`  · ${g.t}：${(g.items || []).join('；')}`);
    });
    (s.risks || []).forEach((r) => out.push(`  ! ${r}`));
    if (s.lines?.length) {
      out.push('  ```python');
      s.lines.forEach((r) => out.push(`  ${String(r.n).padStart(4)} ${r.code}${r.why ? '   # ' + r.why : ''}`));
      out.push('  ```');
    }
  });
  return out.join('\n');
}

/* ==========================================================================
   讲解报告导出：把「讲解稿 + 全景图 + 依赖表」拼成一份自包含 HTML

   设计取舍：
   - 纯前端拼装。讲解数据（DOC.data）和全景图（graphLayout）都已在内存里，
     后端再算一遍等于重复实现两套逻辑，还容易和界面看到的内容不一致。
   - 报告必须能离线打开、能发给别人：样式全部内联，不引外部字体/脚本/图片，
     全景图直接用内联 SVG（不是截图，放大不糊）。
   - 默认深色（和产品一致），但内置「浅色 / 打印」切换：报告多半会被打印或贴进
     Word，深色一打印就是一整页黑，这个按钮不是装饰。
   ========================================================================== */

const REPORT_CSS = `
:root{
  --rbg:#0b0f16; --rbg1:#10151f; --rbg2:#151b27; --rline:#242c3a; --rline2:#2f3a4b;
  --rtx:#dbe3ef; --rtx2:#a3b0c2; --rdim:#6b7684;
  --racc:#4ea1ff; --racc2:#22d3ee; --rok:#34d399; --rwarn:#fbbf24; --rerr:#fb7185; --rpur:#a78bfa;
  --rmono:"JetBrains Mono","Cascadia Code",Consolas,"SF Mono",Menlo,monospace;
  --rsans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
}
body.light{
  --rbg:#f7f9fc; --rbg1:#ffffff; --rbg2:#f1f4f9; --rline:#dfe5ee; --rline2:#cbd4e1;
  --rtx:#1d2533; --rtx2:#42505f; --rdim:#77838f;
  --racc:#1f6fd0; --racc2:#0e8fa8; --rok:#0e9f6e; --rwarn:#a16207; --rerr:#c02a3f; --rpur:#6d4bd0;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:
    radial-gradient(900px 460px at 12% -8%, rgba(78,161,255,.10), transparent 60%),
    radial-gradient(760px 400px at 90% 106%, rgba(34,211,238,.07), transparent 62%),
    var(--rbg);
  background-attachment:fixed;
  color:var(--rtx); font-family:var(--rsans); font-size:14px; line-height:1.7;
  -webkit-font-smoothing:antialiased;
}
body.light{background:var(--rbg)}
.wrap{max-width:980px;margin:0 auto;padding:0 22px 80px}

/* ---- 顶部工具条（打印时隐藏） ---- */
.rbar{
  position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:9px;
  padding:9px 22px;margin-bottom:22px;
  background:rgba(11,15,22,.92);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--rline);
}
body.light .rbar{background:rgba(255,255,255,.94)}
.rbar .rb-t{font-size:12px;color:var(--rdim);margin-right:auto;font-family:var(--rmono)}
.rbar button{
  font:inherit;font-size:12px;padding:5px 11px;border-radius:6px;cursor:pointer;
  background:var(--rbg2);color:var(--rtx2);border:1px solid var(--rline2);
  transition:.14s;
}
.rbar button:hover{color:var(--rtx);border-color:var(--racc);background:rgba(78,161,255,.10)}

/* ---- 封面 ---- */
.cover{
  position:relative;padding:26px 26px 22px;margin-bottom:26px;border-radius:14px;overflow:hidden;
  background:linear-gradient(135deg,rgba(78,161,255,.13),rgba(34,211,238,.055) 55%,transparent);
  border:1px solid rgba(78,161,255,.22);
}
.cover::after{
  content:'';position:absolute;right:-60px;top:-60px;width:220px;height:220px;border-radius:50%;
  background:radial-gradient(circle,rgba(34,211,238,.16),transparent 68%);
}
.cover .cv-k{font-size:11px;letter-spacing:2px;color:var(--racc2);text-transform:uppercase}
.cover h1{font-size:26px;line-height:1.35;margin:8px 0 6px;font-weight:600;word-break:break-word}
.cover .cv-sub{font-size:13.5px;color:var(--rtx2)}
.cover .cv-meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.cover .cv-meta span{
  font-size:11.5px;font-family:var(--rmono);padding:3px 9px;border-radius:20px;
  background:rgba(255,255,255,.05);border:1px solid var(--rline2);color:var(--rtx2);
}
body.light .cover .cv-meta span{background:#fff}

/* ---- 章节 ---- */
.sec{margin:34px 0 14px}
.sec h2{
  font-size:18px;font-weight:600;display:flex;align-items:center;gap:10px;
  padding-bottom:9px;border-bottom:1px solid var(--rline);
}
.sec h2 .no{
  flex:0 0 auto;font-family:var(--rmono);font-size:12px;color:var(--racc);
  background:rgba(78,161,255,.12);border:1px solid rgba(78,161,255,.26);
  padding:1px 8px;border-radius:6px;
}
.sec h2 small{font-size:12px;color:var(--rdim);font-weight:400;margin-left:auto;font-family:var(--rmono)}
.sec > p{margin-top:10px;color:var(--rtx2);font-size:13.5px}

/* ---- 导读 ---- */
.lead p{color:var(--rtx2);font-size:14px;margin:9px 0}
.lead ol{margin:10px 0 0 20px;color:var(--rtx2);font-size:13.5px}
.lead ol li{margin:5px 0}

/* ---- 统计格 ---- */
.grid{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.stat{
  flex:1 1 110px;padding:12px 14px;border-radius:10px;text-align:center;
  background:var(--rbg1);border:1px solid var(--rline);
}
.stat b{display:block;font-size:21px;font-family:var(--rmono);color:var(--rtx);line-height:1.25}
.stat span{font-size:11.5px;color:var(--rdim)}

/* ---- 风险 ---- */
.risks{margin-top:12px;border-radius:11px;border:1px solid rgba(251,113,133,.24);background:rgba(251,113,133,.05);padding:14px 16px}
.risks .rk{display:flex;gap:10px;font-size:13px;margin:7px 0;color:var(--rtx2)}
.risks .rk i{
  flex:0 0 auto;font-style:normal;font-family:var(--rmono);font-size:11px;
  color:var(--rdim);background:rgba(255,255,255,.06);padding:0 6px;border-radius:4px;
  height:20px;line-height:20px;
}
body.light .risks .rk i{background:rgba(0,0,0,.05)}

/* ---- 段落卡 ---- */
.seg{
  margin:14px 0;border-radius:11px;border:1px solid var(--rline);
  background:var(--rbg1);overflow:hidden;
}
.seg-h{
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:11px 15px;
  border-bottom:1px solid var(--rline);background:rgba(255,255,255,.018);
}
body.light .seg-h{background:var(--rbg2)}
.bg{
  flex:0 0 auto;font-size:11px;padding:2px 8px;border-radius:5px;letter-spacing:.3px;
  background:rgba(255,255,255,.07);color:var(--rtx2);border:1px solid rgba(255,255,255,.08);
}
body.light .bg{background:rgba(0,0,0,.045);border-color:rgba(0,0,0,.06)}
.bg.k-function{background:rgba(78,161,255,.14);color:#8fc4ff;border-color:rgba(78,161,255,.26)}
.bg.k-async_function{background:rgba(34,211,238,.13);color:#7fe3f3;border-color:rgba(34,211,238,.26)}
.bg.k-class{background:rgba(167,139,250,.14);color:#c0aaff;border-color:rgba(167,139,250,.28)}
.bg.k-main{background:rgba(52,211,153,.14);color:#7ce8b8;border-color:rgba(52,211,153,.28)}
.bg.k-import{background:rgba(148,163,184,.13);color:#a9b6c8;border-color:rgba(148,163,184,.24)}
.bg.k-config{background:rgba(251,191,36,.13);color:#e8c983;border-color:rgba(251,191,36,.26)}
body.light .bg.k-function{color:#12559e}
body.light .bg.k-class{color:#5b3aa8}
body.light .bg.k-main{color:#0a7a52}
body.light .bg.k-config{color:#8a5a04}
.seg-h h3{
  flex:1;min-width:0;font-size:14px;font-weight:500;font-family:var(--rmono);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.seg-h .m{flex:0 0 auto;font-size:11px;color:var(--rdim);font-family:var(--rmono)}
.seg-w{padding:12px 15px 2px;font-size:13.5px;color:var(--rtx2);line-height:1.8}
.seg-w .from-doc{font-size:10.5px;color:var(--rdim);margin-left:6px;font-family:var(--rmono)}

/* 细节行 */
.det{padding:4px 15px 2px}
.drow{display:flex;gap:11px;margin:6px 0;font-size:12.5px;line-height:1.75}
.drow > b{flex:0 0 72px;font-weight:400;font-size:12px;color:var(--rdim);padding-top:1px}
.drow > div{flex:1;min-width:0;color:var(--rtx2)}
.drow .li{position:relative;padding-left:12px;margin:1px 0;word-break:break-word}
.drow .li::before{content:'';position:absolute;left:2px;top:9px;width:4px;height:4px;border-radius:50%;background:var(--rline2)}
.drow code{font-family:var(--rmono);font-size:11.5px;padding:0 4px;border-radius:4px;background:rgba(255,255,255,.06)}
body.light .drow code{background:rgba(0,0,0,.055)}

.rkline{
  margin:9px 15px 0;padding:8px 11px;border-radius:7px;font-size:12px;line-height:1.7;
  background:rgba(251,191,36,.07);border:1px solid rgba(251,191,36,.2);color:#e5cb92;
}
body.light .rkline{color:#8a5a04;background:#fdf6e3;border-color:#f0dca8}

/* 代码 + 旁注 */
.code{margin:10px 15px 14px;border-radius:8px;border:1px solid var(--rline);overflow:hidden;background:rgba(8,11,17,.62)}
body.light .code{background:#fafbfd}
.rl{
  display:grid;grid-template-columns:44px minmax(0,1fr) minmax(0,1.05fr);gap:12px;
  padding:1px 12px 1px 0;font-family:var(--rmono);font-size:12px;line-height:1.7;
}
.rl:hover{background:rgba(78,161,255,.05)}
.rl .rn{text-align:right;color:#465061;font-size:11px;user-select:none}
body.light .rl .rn{color:#9aa5b1}
.rl .rc{white-space:pre-wrap;word-break:break-word;color:#c6d3e2}
body.light .rl .rc{color:#26303d}
.rl .rw{
  color:#75828f;font-family:var(--rsans);font-size:12px;line-height:1.7;
  border-left:1px solid rgba(255,255,255,.06);padding-left:11px;
}
body.light .rl .rw{color:#5d6a78;border-left-color:#e3e8ef}
.rl.more .rc{color:var(--rdim);font-style:italic}
.tk-kw{color:#c792ea}.tk-def{color:#82aaff}.tk-str{color:#c3e88d}.tk-num{color:#f78c6c}
.tk-com{color:#4b5a6b;font-style:italic}.tk-fn{color:#ffcb6b}.tk-op{color:#89ddff}
.tk-self{color:#f07178;font-style:italic}
body.light .tk-kw{color:#8b39b8}.tk-def{color:#1b5fb8}.tk-str{color:#2f7a37}
body.light .tk-num{color:#b1541a}.tk-com{color:#8b98a5}.tk-fn{color:#9a6a00}
body.light .tk-op{color:#0d7e93}.tk-self{color:#b0313f}

/* ---- 依赖表 ---- */
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:12.5px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--rline);vertical-align:top}
th{font-size:11.5px;font-weight:500;color:var(--rdim);background:rgba(255,255,255,.028);white-space:nowrap}
body.light th{background:var(--rbg2)}
td.mono,th.mono{font-family:var(--rmono);font-size:11.5px}
td .tag{
  display:inline-block;font-size:10.5px;padding:0 6px;border-radius:4px;margin:1px 3px 1px 0;
  background:rgba(255,255,255,.06);color:var(--rtx2);
}
body.light td .tag{background:rgba(0,0,0,.05)}
.st-ok{color:var(--rok)}.st-bad{color:var(--rerr)}.st-warn{color:var(--rwarn)}.st-none{color:var(--rdim)}

/* ---- 全景图 ---- */
.map{
  margin-top:14px;padding:12px;border-radius:11px;border:1px solid var(--rline);
  background:var(--rbg1);overflow:auto;
}
.map svg{display:block;height:auto;max-width:100%;min-width:520px}
.map-svg .gn .box{fill:url(#gvNodeBg);stroke:#2a3444;stroke-width:1}
.map-svg .gn.k-startup .box{stroke:#2f6a52}
.map-svg .gn.k-orphan .box{stroke:#3a3358}
.map-svg .gn .nm{font-family:var(--rmono);font-size:11.5px;fill:#dbe4f0;font-weight:500}
.map-svg .gn.k-module .nm{fill:#a9b6c8}
.map-svg .gn.k-orphan .nm{fill:#b9aeeb}
.map-svg .gn .sub{font-family:var(--rmono);font-size:9.5px;fill:#5d6878}
.map-svg .gn .ico{font-size:11px}
.map-svg .gn .cov{font-size:9.5px;font-family:var(--rsans)}
.map-svg .gn .cov.ok{fill:#34d399}
.map-svg .gn .cov.bad{fill:#fb7185}
.map-svg .gn .cov.none{fill:#4b5563}
.map-svg .gn .cov.warn{fill:#fbbf24}
.map-svg .gn.dimmed{opacity:.34}
.map-svg .gv-col-t{font-size:10.5px;fill:#5a6576;letter-spacing:.4px}
.map-svg .gv-col-b{stroke:#1d2634;stroke-width:1}
.map-svg .ge{fill:none;stroke:#2f3b4c;stroke-width:1.2;opacity:.75}
.map-svg .ge.call{stroke:#22d3ee;stroke-dasharray:4 3;opacity:.55}
.map-svg .ge.spawn{stroke:#f59e0b;stroke-dasharray:1 3;stroke-width:1.5;opacity:.6}
.map-svg .ge.hot{stroke:#34d399;opacity:.95;stroke-width:1.7}
.map-svg .ge.hot.call{stroke:#22d3ee}
body.light .map-svg .gn .nm{fill:#1d2533}
body.light .map-svg .gn .sub{fill:#7a8797}
body.light .map-svg .gn .box{fill:#fff;stroke:#cdd6e2}
body.light .map-svg .ge{stroke:#b9c4d3}

.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:11.5px;color:var(--rdim)}
.legend i{display:inline-block;width:22px;height:0;border-top:2px solid #2f3b4c;vertical-align:middle;margin-right:5px}
.legend i.call{border-top:2px dashed #22d3ee}
.legend i.spawn{border-top:2px dotted #f59e0b}
.legend i.ok{border:0;width:9px;height:9px;border-radius:50%;background:var(--rok);margin-right:6px}

/* ---- 页脚 ---- */
.foot{
  margin-top:44px;padding-top:18px;border-top:1px solid var(--rline);
  font-size:12px;color:var(--rdim);line-height:1.9;
}
.foot b{color:var(--rtx2);font-weight:500}

@media (max-width:820px){
  .rl{grid-template-columns:38px minmax(0,1fr)}
  .rl .rw{grid-column:2;border-left:0;padding-left:0;margin-bottom:3px;color:var(--rdim)}
  .drow{flex-direction:column;gap:2px}
}
@media print{
  body{background:#fff !important;color:#111;font-size:11.5pt}
  .rbar{display:none}
  .wrap{max-width:none;padding:0}
  .cover,.seg,.map,.stat,.risks,.code{break-inside:avoid;page-break-inside:avoid}
  .sec{break-after:avoid}
  a{color:inherit;text-decoration:none}
  /* 用户不点「浅色」直接 Ctrl+P 也能得到白纸黑字：代码块与 token 强制浅色 */
  .code{background:#fafbfd !important;border-color:#e3e8ef}
  .rl .rc{color:#26303d}
  .rl .rn{color:#9aa5b1}
  .tk-kw{color:#8b39b8}.tk-def{color:#1b5fb8}.tk-str{color:#2f7a37}
  .tk-num{color:#b1541a}.tk-com{color:#8b98a5}.tk-fn{color:#9a6a00}
  .tk-op{color:#0d7e93}.tk-self{color:#b0313f}
  .map-svg .gn .nm{fill:#1d2533}
  .map-svg .gn .sub{fill:#7a8797}
  .map-svg .gn .box{fill:#fff;stroke:#cdd6e2}
  .map-svg .ge{stroke:#b9c4d3}
  .map-svg .ge.call{stroke:#0e8fa8}
}
`;

/* 报告内嵌的小脚本：切换浅色 / 打印 / 回到顶部。内联，不依赖网络。 */
const REPORT_JS = `
var b = document.body;
document.getElementById('rbLight').onclick = function () {
  b.classList.toggle('light');
  this.textContent = b.classList.contains('light') ? '深色' : '浅色';
};
document.getElementById('rbPrint').onclick = function () {
  if (!b.classList.contains('light')) b.classList.add('light');
  setTimeout(function () { window.print(); }, 120);
};
document.getElementById('rbTop').onclick = function () { window.scrollTo({ top: 0, behavior: 'smooth' }); };
`;

function reportProjName() {
  const fs = S.projectFiles || [];
  if (!fs.length) return '单文件';
  const first = String(fs[0].name || '').replace(/\\/g, '/');
  return first.includes('/') ? first.split('/')[0] : first.replace(/\.py$/i, '');
}

function reportVer() {
  const t = (($('#verChip') || {}).textContent || '').trim();
  return t.replace(/^v/i, '') || '2.2';
}

function reportStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function reportFileStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

/* 全景图：直接取已渲染的 SVG 内容，包一层自适应外壳（内联 SVG，放大不糊）。
   从没进过全景图视图时，先在隐藏容器里生成一次。 */
function reportGraphSvg() {
  if (!S.projectFiles || !(S.projectMap && (S.projectMap.files || []).length)) return '';
  let inner = '';
  try {
    const el = $('#gvSvg');
    if (!el || !el.innerHTML.trim() || !GV.nodes.length) renderGraph(true);
    inner = ($('#gvSvg') || {}).innerHTML || '';
  } catch (e) { inner = ''; }
  if (!inner.trim()) return '';
  // 节点图标用的 emoji（🚀📦🧩）在打印和部分 PDF 字体里会缺字，换成几何符号
  inner = inner.replace(/\u{1F680}/gu, '\u25B6').replace(/\u{1F4E6}/gu, '\u25AA').replace(/\u{1F9E9}/gu, '\u25AB');
  const w = Math.max(GV.W || 800, 320);
  const h = Math.max(GV.H || 400, 220);
  return `<svg class="map-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}"
     width="100%" preserveAspectRatio="xMidYMid meet" role="img">${inner}</svg>`;
}

/* 依赖表：文件 / 类型 / 行数 / 依赖谁 / 被谁引用 / 运行结果 */
function reportDepTable() {
  const all = ((S.projectMap && S.projectMap.files) || []).filter((f) => !f.error);
  if (!all.length) return '';
  const edges = (S.projectMap && S.projectMap.edges) || [];
  const outOf = new Map(), inOf = new Map();
  for (const e of edges) {
    if (!outOf.has(e.from)) outOf.set(e.from, []);
    if (!inOf.has(e.to)) inOf.set(e.to, []);
    const o = outOf.get(e.from);
    if (!o.some((x) => x.name === e.to)) o.push({ name: e.to, kind: e.kind });
    const i = inOf.get(e.to);
    if (!i.some((x) => x.name === e.from)) i.push({ name: e.from, kind: e.kind });
  }
  const kindCn = { startup: '启动入口', module: '模块', orphan: '未接入' };
  const rows = all.slice().sort((a, b) => {
    const r = { startup: 0, module: 1, orphan: 2 };
    return (r[a.kind] ?? 3) - (r[b.kind] ?? 3) || a.name.localeCompare(b.name);
  }).map((f) => {
    const c = covOf(f.name);
    const lab = covLabel(f.name);
    const cur = f.name === S.filename ? ' style="background:rgba(78,161,255,.07)"' : '';
    const deps = (outOf.get(f.name) || []).map((x) =>
      `<span class="tag">${esc(x.name.split('/').pop())}</span>`).join('') || '<span class="st-none">—</span>';
    const users = (inOf.get(f.name) || []).map((x) =>
      `<span class="tag">${esc(x.name.split('/').pop())}</span>`).join('') || '<span class="st-none">—</span>';
    const st = c
      ? `<span class="st-${lab.cls === 'ok' ? 'ok' : lab.cls === 'bad' ? 'bad' : 'warn'}">${esc(lab.txt)}</span>`
      : '<span class="st-none">未运行</span>';
    return `<tr${cur}><td class="mono">${esc(f.name)}${f.name === S.filename ? ' ◀' : ''}</td>
      <td>${kindCn[f.kind] || f.kind}</td><td class="mono">${f.lines || 0}</td>
      <td>${deps}</td><td>${users}</td><td>${st}</td></tr>`;
  }).join('');
  return `<table><thead><tr>
      <th class="mono">文件</th><th>类型</th><th class="mono">行数</th>
      <th>它依赖谁</th><th>谁依赖它</th><th>运行结果</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

/* 当前文件在项目里的位置：进/出的边 + 谁在调它的函数 */
function prettyCaller(nm) {
  // 后端给模块级调用起的内部占位名，报告里要像给人看的话
  return String(nm || '').replace('<模块顶层>', '模块顶层（import 时执行）').replace('<module>', '模块顶层');
}

function reportPosition(d) {
  if (!S.projectFiles || !S.projectMap) return '';
  const edges = S.projectMap.edges || [];
  const dep = edges.filter((e) => e.from === S.filename);
  const used = edges.filter((e) => e.to === S.filename);
  const callers = [];
  (d.segments || []).forEach((s) => {
    (s.called_by || []).forEach((x) => {
      if (x && x.name) callers.push(`${prettyCaller(x.name)}${x.file ? '（' + x.file + (x.line ? ':' + x.line : '') + '）' : ''}`);
    });
  });
  const box = (title, list, emptyTxt) => `<div class="drow"><b>${title}</b><div>${
    list.length ? list.map((t) => `<div class="li">${esc(t)}</div>`).join('') : `<div class="li st-none">${emptyTxt}</div>`
  }</div></div>`;
  const kindCn = { import: 'import', call: '调用', spawn: '子进程启动' };
  const depTxt = dep.map((e) => `${e.to}（${kindCn[e.kind] || e.kind} ×${e.weight || 1}）`);
  const usedTxt = used.map((e) => `${e.from}（${kindCn[e.kind] || e.kind} ×${e.weight || 1}）`);
  const uniqCallers = [...new Set(callers)];
  return box('它依赖', depTxt, '没有依赖项目内的其他文件（只用标准库/第三方包）')
    + box('谁依赖它', usedTxt, '项目里没有别的文件 import 它')
    + box('函数调用者', uniqCallers.slice(0, 10), '没有被项目内的函数调用（可能是入口，或只被外部调用）');
}

function reportSegHtml(s) {
  const m = s.metrics || {};
  const meta = [];
  if (s.start) meta.push(`L${s.start}${s.end && s.end !== s.start ? '–' + s.end : ''}`);
  if (m.lines && m.lines > 1) meta.push(`${m.lines} 行`);
  if (m.cc && m.cc > 1) meta.push(`复杂度 ${m.cc}`);
  if (m.methods) meta.push(`${m.methods} 个方法`);

  const rows = s.lines || [];
  const codeOnly = rows.filter((r) => !r.more).map((r) => r.code).join('\n');
  const hl = codeOnly ? highlightAll(codeOnly) : [];
  let i = 0;
  const lines = rows.map((r) => {
    if (r.more) {
      return `<div class="rl more"><span class="rn">…</span><span class="rc">${esc(r.code)}</span><span class="rw"></span></div>`;
    }
    const h = hl[i++] || ' ';
    return `<div class="rl"><span class="rn">${r.n}</span><span class="rc">${h}</span>`
      + `<span class="rw">${esc(r.why || '')}</span></div>`;
  }).join('');

  const det = (s.detail || []).filter((x) => x.items && x.items.length).map((x) =>
    `<div class="drow"><b>${esc(x.t)}</b><div>${x.items.map((t) => `<div class="li">${esc(t)}</div>`).join('')}</div></div>`
  ).join('');

  const cbs = (s.called_by || []).filter((x) => x && x.name);
  const cbHtml = cbs.length
    ? `<div class="drow"><b>谁在调它</b><div>${cbs.map((x) => `<div class="li">${esc(prettyCaller(x.name))}`
      + (x.file ? `（${esc(x.file)}${x.line ? ':' + x.line : ''}）` : '') + `</div>`).join('')}</div></div>`
    : '';

  const risks = (s.risks || []).map((r) => `<div class="rkline">⚠ ${esc(r)}</div>`).join('');
  const members = (s.members || []).length
    ? `<div class="det"><div class="drow"><b>方法</b><div>${s.members.map((x) =>
      `<div class="li"><code>${esc(x.name)}</code>${x.summary ? ' — ' + esc(x.summary) : ''}</div>`).join('')}</div></div></div>`
    : '';

  return `<article class="seg" id="seg-${s.id}">
    <header class="seg-h">
      <span class="bg k-${esc(s.kind)}">${esc(s.badge)}</span>
      <h3>${esc(s.title)}</h3>
      <span class="m">${esc(meta.join(' · '))}</span>
    </header>
    <p class="seg-w">${esc(s.what || '')}${s.src === 'docstring' ? '<span class="from-doc">作者原话</span>' : ''}</p>
    ${det || members ? `<div class="det">${det}${cbHtml}</div>` : (cbHtml ? `<div class="det">${cbHtml}</div>` : '')}
    ${members}
    ${risks}
    <div class="code">${lines || '<div class="rl"><span class="rn"></span><span class="rc"></span><span class="rw"></span></div>'}</div>
  </article>`;
}

/* 拼出完整报告 HTML 文本 */
function buildReportHtml() {
  const d = DOC.data;
  if (!d || !d.ok) return '';
  const st = d.stats || {};
  const intro = d.intro || {};
  const file = d.file || S.filename;
  const proj = reportProjName();
  const svg = reportGraphSvg();
  const table = reportDepTable();
  const pos = reportPosition(d);

  /* 章节编号按文档出现顺序预计算（导读、统计、留意?、逐段、位置?、地图?）。
     不能用自增计数器：risks/pos/map 的模板字符串在定义时就求值，先于 return 里的
     固定章节，编号会和文档顺序颠倒。 */
  const no = { risks: 3, seg: 3, pos: 0, map: 0 };
  if ((d.risks || []).length) no.seg = 4; else no.risks = 0;
  if (pos) no.pos = no.seg + 1;
  if (svg || table) no.map = (no.pos || no.seg) + 1;
  const pad2 = (n) => String(n).padStart(2, '0');

  const risks = (d.risks || []).length
    ? `<section class="sec"><h2><span class="no">${pad2(no.risks)}</span>值得留意<small>${d.risks.length} 处</small></h2>
        <div class="risks">${d.risks.map((r) => `<div class="rk"><i>${r.line ? 'L' + r.line : '整体'}</i>`
      + `<span>${esc(r.text)}</span></div>`).join('')}</div></section>`
    : '';

  const posSec = pos
    ? `<section class="sec"><h2><span class="no">${pad2(no.pos)}</span>它在项目里的位置
        <small>${esc(proj)}</small></h2><div class="det" style="padding:4px 0">${pos}</div></section>`
    : '';

  const mapSec = (svg || table)
    ? `<section class="sec"><h2><span class="no">${pad2(no.map)}</span>项目地图
        <small>${(S.projectMap.files || []).length} 个文件</small></h2>
        <p>节点是文件，箭头是依赖方向。左起第一列是启动入口；右边逐层是被依赖的文件。
        绿色高亮表示这次真的跑过了（跑过 ≠ 每个函数都被执行，具体见下表）。</p>
        ${svg ? `<div class="map">${svg}</div>
        <div class="legend"><span><i></i>import</span><span><i class="call"></i>调用</span>
        <span><i class="spawn"></i>子进程启动</span><span><i class="ok"></i>已跑过</span></div>` : ''}
        ${table}</section>`
    : '';

  const foot = `<div class="foot">
    <b>这份报告怎么来的</b><br/>
    由 PyTrace v${esc(reportVer())} 生成于 ${esc(reportStamp())}。全部结论来自对源码的静态分析
    （AST 解析），<b>没有执行你的代码</b>；只有「运行结果」一列反映真实的执行记录（如果你跑过）。
    <br/><br/>
    讲解文字的优先级：<b>函数/类的 docstring 原文 &gt; 紧邻的注释 &gt; 依据 AST 事实的自动归纳</b>。
    标了「作者原话」的是原作者写的，其余是工具按代码结构归纳的，不编造业务含义。
  </div>`;

  return `<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PyTrace 讲解报告 · ${esc(file)}</title>
<style>${REPORT_CSS}</style>
</head><body>
<div class="rbar">
  <span class="rb-t">PyTrace 讲解报告 · ${esc(file)}</span>
  <button id="rbLight" type="button">浅色</button>
  <button id="rbPrint" type="button">打印 / 存 PDF</button>
  <button id="rbTop" type="button">回到顶部</button>
</div>
<div class="wrap">

  <div class="cover">
    <div class="cv-k">PyTrace · Code Reading Report</div>
    <h1>${esc(file)}</h1>
    <div class="cv-sub">${esc(d.kind_cn || '')} · 逐段讲解与项目位置${proj && proj !== '单文件' ? ' · 项目 ' + esc(proj) : ''}</div>
    <div class="cv-meta">
      <span>${st.lines || 0} 行</span>
      <span>${st.code || 0} 行有效代码</span>
      <span>${st.functions || 0} 个函数</span>
      <span>${st.classes || 0} 个类</span>
      <span>${st.imports || 0} 个依赖模块</span>
      <span>${esc(reportStamp())}</span>
    </div>
  </div>

  <section class="sec"><h2><span class="no">01</span>这个文件是什么<small>导读</small></h2>
    <div class="lead">
      ${(intro.paras || []).map((p) => `<p>${esc(p)}</p>`).join('')}
      ${(intro.how_to_read || []).length ? `<ol>${intro.how_to_read.map((h) => `<li>${esc(h)}</li>`).join('')}</ol>` : ''}
    </div>
  </section>

  <section class="sec"><h2><span class="no">02</span>一眼看规模<small>统计</small></h2>
    <div class="grid">
      <div class="stat"><b>${st.lines || 0}</b><span>总行数</span></div>
      <div class="stat"><b>${st.code || 0}</b><span>有效代码</span></div>
      <div class="stat"><b>${st.functions || 0}</b><span>函数</span></div>
      <div class="stat"><b>${st.classes || 0}</b><span>类</span></div>
      <div class="stat"><b>${st.imports || 0}</b><span>依赖模块</span></div>
      <div class="stat"><b>${st.max_nest || 0}</b><span>最深嵌套</span></div>
    </div>
  </section>

  ${risks}

  <section class="sec"><h2><span class="no">${pad2(no.seg)}</span>逐段讲解<small>${(d.segments || []).length} 段</small></h2>
    ${(d.segments || []).map(reportSegHtml).join('')}
  </section>

  ${posSec}
  ${mapSec}
  ${foot}
</div>
<script>${REPORT_JS}<\/script>
</body></html>`;
}

/* 导出：没有讲解数据就先分析一次，再下载 */
async function exportReport(btnEl) {
  if (!S.code) { toast('先打开一个 .py 文件，才有讲解可以导出', 'warn'); return; }
  const btn = btnEl || $('#dvExport');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  try {
    if (!DOC.data || !DOC.data.ok || DOC.data.file !== S.filename) {
      await loadExplain(true);
      await new Promise((r) => setTimeout(r, 60));
    }
    if (!DOC.data || !DOC.data.ok) { toast('讲解还没分析成功，先看提示把语法错误修掉', 'warn'); return; }
    const html = buildReportHtml();
    if (!html) { toast('报告是空的，先切到「讲解」看一眼再导出', 'warn'); return; }
    const name = `PyTrace讲解报告_${(S.filename || 'file').replace(/[\\/\\:*?"<>|]/g, '_')}_${reportFileStamp()}.html`;
    const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    const kb = Math.round(blob.size / 1024);
    toast(`已导出讲解报告（${kb}KB）：双击就能用浏览器打开，也能直接打印成 PDF`, 'ok');
  } catch (e) {
    toast('导出失败：' + (e && e.message ? e.message : e), 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || '导出报告'; }
  }
}

function bindDoc() {
  const tb = $('#dvToggleAll');
  if (tb) tb.onclick = () => {
    DOC.openAll = !DOC.openAll;
    $$('#dvMain .dc-code').forEach((d) => { d.open = DOC.openAll; });
    tb.textContent = DOC.openAll ? '收起代码' : '展开代码';
  };
  const cp = $('#dvCopy');
  if (cp) cp.onclick = async () => {
    const t = docToText();
    if (!t) { toast('还没有可复制的讲解', 'warn'); return; }
    try {
      await navigator.clipboard.writeText(t);
      toast(`已复制整篇讲解（${t.split('\n').length} 行）`, 'ok');
    } catch (e) {
      toast('浏览器不允许剪贴板，请用「重新分析」旁的按钮手动复制', 'warn');
    }
  };
  const rf = $('#dvRefresh');
  if (rf) rf.onclick = () => loadExplain(true);
  const ex = $('#dvExport');
  if (ex) ex.onclick = () => exportReport(ex);
  const fs = $('#dvFile');
  if (fs) fs.onchange = () => {
    const nm = fs.value;
    if (!nm || nm === S.filename) return;
    openTreeFile(nm);
  };
}

/* 项目模式下，讲解视图里也要能切文件 */
function syncDocFileSel() {
  const fs = $('#dvFile');
  if (!fs) return;
  const list = docPyFiles();
  if (list.length <= 1) { fs.hidden = true; return; }
  fs.hidden = false;
  fs.innerHTML = list.map((f) =>
    `<option value="${esc(f.name)}"${f.name === S.filename ? ' selected' : ''}>${esc(f.name)}</option>`).join('');
}

/* ---- 视图切换 ---- */
function markGraphAvail() {
  const seg = $('#stageSeg');
  if (!seg) return;
  const ok = !!(S.projectMap && S.projectMap.files && S.projectMap.files.length);
  seg.classList.toggle('dim', !ok);
  seg.title = ok ? '「舞台」看逐行执行；「全景图」看整个项目谁调用谁；「讲解」不运行也能逐段读懂'
    : '打开一个文件夹项目后才会有全景图；「讲解」任何时候都能用';
  return ok;
}

function syncSeg() {
  $$('#stageSeg .seg-b').forEach((b) => b.classList.toggle('active', b.dataset.view === GV.view));
}

function setView(v) {
  const stage = $('#stage');
  if (!stage) return;
  if (v === 'graph') {
    if (!markGraphAvail()) { toast('先「打开文件夹」载入一个项目，才能看全景图', 'warn'); GV.view = 'stage'; syncSeg(); return; }
    GV.view = 'graph';
    stage.classList.add('graph-on');
    stage.classList.remove('doc-on');
    $('#graphView').hidden = false;
    $('#docView').hidden = true;
    renderGraph();
  } else if (v === 'doc') {
    if (!S.code) { toast('先打开一个 .py 文件，才能看逐段讲解', 'warn'); return; }
    GV.view = 'doc';
    stage.classList.add('doc-on');
    stage.classList.remove('graph-on');
    $('#graphView').hidden = true;
    $('#docView').hidden = false;
    syncDocFileSel();
    loadExplain(false);
  } else {
    GV.view = 'stage';
    stage.classList.remove('graph-on', 'doc-on');
    $('#graphView').hidden = true;
    $('#docView').hidden = true;
  }
  syncSeg();
}

/* ---- 运行覆盖：某个文件在这次会话里被执行过吗 ---- */
function covOf(name) { return (S.coverage && S.coverage.get(name)) || null; }
// 「执行过」不等于「成功退出」：超时的常驻服务也真实跑了几千个事件，算跑过
const ranOf = (c) => !!c && (c.ok === true || !!c.via || (c.events || 0) > 0);

function covLabel(name) {
  const c = covOf(name);
  if (!c) return { cls: 'none', txt: '未跑' };
  if (c.err || c.ok === false) {
    // 超时 / 需参数 是「本来就这样」，用琥珀色；只有真报错才红
    const soft = c.errKind === 'timeout' || c.errKind === 'args';
    return { cls: soft ? 'warn' : 'bad', txt: c.err || '未通过' };
  }
  if (c.via) return { cls: 'ok', txt: '已触及' };
  return { cls: 'ok', txt: `${c.events} 事件` };
}

/* ---- 布局：按依赖深度分列，列内按目录聚拢 ---- */
function graphLayout() {
  let all = ((S.projectMap && S.projectMap.files) || []).filter((f) => !f.error);
  /* 大项目精简：孤立文件（没人引用也不启动）默认不进图 —— 139 个文件的图里
     它们占掉大半列宽却什么都不解释。>45 个文件时自动开启，可用「精简」按钮切换 */
  const autoTrim = GV.trim === undefined ? all.length > 45 : !!GV.trim;
  GV.hiddenCount = 0;
  let trimmed = false;
  if (autoTrim) {
    const hasEdge = new Set();
    for (const e of ((S.projectMap && S.projectMap.edges) || [])) {
      hasEdge.add(e.from); hasEdge.add(e.to);
    }
    const keep = all.filter((f) => f.kind === 'startup' || hasEdge.has(f.name));
    if (keep.length >= 8) {         // 剪完太稀就放弃精简，避免只剩孤零零几个点
      GV.hiddenCount = all.length - keep.length;
      trimmed = true;
      all = keep;
    }
  }
  // 大图用紧凑节点（176×38）：同屏能多放 40% 的节点，字号不变只是盒子更瘦
  const compact = all.length > 45;
  const NW = compact ? 176 : GV_NW, NH = compact ? 38 : GV_NH, GAPY = compact ? 8 : GV_GAPY;

  const names = new Set(all.map((f) => f.name));
  const kindOf = new Map(all.map((f) => [f.name, f.kind]));
  const byName = new Map(all.map((f) => [f.name, f]));
  const edges = ((S.projectMap && S.projectMap.edges) || [])
    .filter((e) => names.has(e.from) && names.has(e.to));

  // 层级：启动入口固定在第 0 列；模块取「引用它的文件的层级 + 1」，多次松弛取最深
  const lvl = new Map();
  all.forEach((f) => lvl.set(f.name, f.kind === 'startup' ? 0 : 1));
  for (let round = 0; round < 6; round++) {
    let moved = false;
    for (const e of edges) {
      if (kindOf.get(e.to) === 'startup') continue;
      const want = Math.min(4, (lvl.get(e.from) || 0) + 1);
      if (want > lvl.get(e.to)) { lvl.set(e.to, want); moved = true; }
    }
    if (!moved) break;
  }
  // 孤立文件（没人 import、也没启动点）单独放最后一列，别混进依赖链里
  const maxL = Math.max(0, ...all.map((f) => (kindOf.get(f.name) === 'orphan' ? 0 : lvl.get(f.name))));
  all.forEach((f) => { if (kindOf.get(f.name) === 'orphan') lvl.set(f.name, maxL + 1); });

  // 分组 → 每列最多 GV_MAX_COL 个，超出拆成并排子列（否则一列几十个节点缩到看不清）
  const groups = new Map();
  for (const f of all) {
    const L = lvl.get(f.name);
    if (!groups.has(L)) groups.set(L, []);
    groups.get(L).push(f);
  }
  const levels = [...groups.keys()].sort((a, b) => a - b);
  const cols = [];
  for (const L of levels) {
    const list = groups.get(L).sort((a, b) => {
      const da = a.name.includes('/') ? a.name.slice(0, a.name.lastIndexOf('/')) : '';
      const db = b.name.includes('/') ? b.name.slice(0, b.name.lastIndexOf('/')) : '';
      return da === db ? a.name.localeCompare(b.name) : da.localeCompare(db);
    });
    for (let i = 0; i < list.length; i += GV_MAX_COL) {
      cols.push({ level: L, first: i === 0, nodes: list.slice(i, i + GV_MAX_COL) });
    }
  }

  // 坐标
  const pos = new Map();
  let x = GV_PAD;
  let prevLevel = null;
  let maxRows = 1;
  for (const col of cols) {
    if (prevLevel !== null && col.level !== prevLevel) x += 46;   // 层级之间留更宽的空隙
    col.x = x;
    col.nodes.forEach((f, i) => {
      pos.set(f.name, { x, y: GV_TOP + i * (NH + GAPY), f, level: col.level });
    });
    maxRows = Math.max(maxRows, col.nodes.length);
    x += NW + 34;
    prevLevel = col.level;
  }
  const W = Math.max(x - 34 + GV_PAD, 480);
  const H = GV_TOP + maxRows * (NH + GAPY) + GV_PAD;
  // 边的端点
  const paths = [];
  for (const e of edges) {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) continue;
    const y1 = a.y + GV_NH / 2, y2 = b.y + GV_NH / 2;
    let x1, x2, d;
    if (b.x > a.x + 10) {
      x1 = a.x + GV_NW; x2 = b.x;
      const cx = Math.max(36, (x2 - x1) * 0.45);
      d = `M${x1} ${y1} C${x1 + cx} ${y1},${x2 - cx} ${y2},${x2} ${y2}`;
    } else {
      // 回边 / 同列：从右侧绕出去，避免和正向边糊在一起
      x1 = a.x + GV_NW; x2 = b.x + GV_NW;
      const bow = 62 + Math.abs(y2 - y1) * 0.18;
      d = `M${x1} ${y1} C${x1 + bow} ${y1},${x2 + bow} ${y2},${x2} ${y2}`;
    }
    paths.push({ d, kind: e.kind, from: e.from, to: e.to });
  }
  return { all, byName, cols, pos, paths, W, H: H, edges, kindOf, orphanLevel: maxL + 1, NW, NH, trimmed, compact };
}

/* ---- 渲染 SVG ---- */
function renderGraph(keepView) {
  const svg = $('#gvSvg'), canvas = $('#gvCanvas');
  if (!svg || !canvas) return;
  const empty = $('#gvEmpty');
  const model = graphLayout();
  if (!model.all.length) {
    svg.innerHTML = '';
    if (empty) { empty.hidden = false; empty.innerHTML = '<b>这个项目里没有可解析的 .py 文件</b><span>或者文件都解析失败了（语法错误）</span>'; }
    return;
  }
  if (empty) empty.hidden = true;
  GV.nodes = model.all;
  GV.cols = model.cols;
  GV.W = model.W; GV.H = model.H;

  const cov = (n) => covLabel(n);
  const shortName = (n) => {
    const b = n.split('/').pop();
    return b.length > 25 ? b.slice(0, 24) + '…' : b;
  };
  const dirOf = (n) => {
    if (!n.includes('/')) return '';
    const d = n.slice(0, n.lastIndexOf('/'));
    return d.length > 22 ? '…' + d.slice(-21) : d;
  };
  const iconOf = (k) => (k === 'startup' ? '🚀' : k === 'module' ? '📦' : '🧩');

  let s = `<defs>
    <linearGradient id="gvNodeBg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#1a2331"/><stop offset="1" stop-color="#121924"/>
    </linearGradient>
    <marker id="gvArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0 0 L8 4 L0 8 z" fill="#3d4a5c"/>
    </marker>
    <marker id="gvArrowHot" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0 0 L8 4 L0 8 z" fill="#34d399"/>
    </marker>
  </defs>`;

  // 列标题
  for (const col of model.cols) {
    if (!col.first) continue;
    const label = col.level === 0 ? '① 启动入口 · 从这里开始读'
      : (col.level === model.orphanLevel ? '未参与调用链的文件' : `第 ${col.level + 1} 层依赖`);
    s += `<text class="gv-col-t" x="${col.x}" y="${GV_TOP - 14}">${esc(label)}</text>`;
    s += `<line class="gv-col-b" x1="${col.x - 8}" y1="${GV_TOP - 30}" x2="${col.x - 8}" y2="${GV_TOP + Math.max(col.nodes.length, 1) * (GV_NH + GV_GAPY) - GV_GAPY}"/>`;
  }

  // 边（先画，压在节点下面）：两端都真跑过 → 高亮成「活链路」
  for (const p of model.paths) {
    const ca = covOf(p.from), cb = covOf(p.to);
    const hot = ranOf(ca) && ranOf(cb);
    const arrow = hot ? 'url(#gvArrowHot)' : 'url(#gvArrow)';
    s += `<path class="ge${p.kind === 'call' ? ' call' : p.kind === 'spawn' ? ' spawn' : ''}${hot ? ' hot' : ''}" d="${p.d}" marker-end="${arrow}" data-from="${esc(p.from)}" data-to="${esc(p.to)}"/>`;
  }

  // 节点
  for (const f of model.all) {
    const p = model.pos.get(f.name);
    if (!p) continue;
    const c = cov(f.name);
    const sub = [dirOf(f.name), f.lines ? `${f.lines} 行` : '', (f.imported_by || []).length ? `被 ${f.imported_by.length} 处引用` : '']
      .filter(Boolean).join(' · ');
    const busy = ranOf(c) && (c.events || c.via);
    s += `<g class="gn k-${f.kind}${GV.sel === f.name ? ' sel' : ''}${busy ? '' : (c ? ' dimmed' : '')}" data-file="${esc(f.name)}" transform="translate(${p.x},${p.y})">
      <rect class="box" width="${model.NW}" height="${model.NH}" rx="9"/>
      <text class="ico" x="9" y="${model.NH > 40 ? 19 : 17}">${iconOf(f.kind)}</text>
      <text class="nm" x="26" y="${model.NH > 40 ? 19 : 17}">${esc(shortName(f.name))}</text>
      <text class="sub" x="26" y="${model.NH > 40 ? 34 : 31}">${esc(sub).slice(0, 44)}</text>
      <text class="cov ${c ? c.cls : 'none'}" x="${model.NW - 9}" y="${model.NH > 40 ? 19 : 17}" text-anchor="end">${esc(c ? c.txt : '未跑')}</text>
    </g>`;
  }

  svg.setAttribute('width', model.W);
  svg.setAttribute('height', model.H);
  svg.setAttribute('viewBox', `0 0 ${model.W} ${model.H}`);
  svg.innerHTML = s;
  // 大图：整体压暗连线（选中节点时会聚焦提亮），小图保持原亮度
  svg.classList.toggle('big', !!model.compact);
  const tb = $('#gvTrim');
  if (tb) {
    tb.classList.toggle('on', !!model.trimmed);
    tb.textContent = model.trimmed ? '精简·开' : '精简';
  }

  // 顶部概览
  const sub = $('#gvSub');
  if (sub) {
    const st = S.projectMap.stats || {};
    const ran = S.coverage ? [...S.coverage.keys()].filter((k) => model.byName.has(k)).length : 0;
    sub.textContent = `${model.all.length} 个文件 · ${st.startup || 0} 个启动入口 · ${st.module || 0} 个模块`
      + (ran ? ` · 已跑过 ${ran} 个` : '')
      + (model.trimmed && GV.hiddenCount ? ` · 已隐藏 ${GV.hiddenCount} 个未参与调用的文件` : '');
  }
  if (!keepView) {
    fitGraph();
    // 大项目初始聚焦：几百个文件哪怕缩放可读也读不过来，默认选中第一个
    // 启动入口（高亮它 + 详情面板），用户一眼知道"从哪开始读"，其余部分靠平移
    try {
      const cvs = $('#gvCanvas');
      if (cvs && GV.W > cvs.clientWidth * 2 && !GV.sel) {
        const st = model.all.find((f) => f.kind === 'startup');
        if (st) selectGraphNode(st.name);
      }
    } catch (e) { /* 初始聚焦失败不影响看图 */ }
  }
  bindGraphNodes();
}

function applyGvTransform() {
  const svg = $('#gvSvg');
  if (!svg) return;
  svg.style.transform = `translate(${GV.tx}px,${GV.ty}px) scale(${GV.scale})`;
}

function fitGraph() {
  const cvs = $('#gvCanvas');
  if (!cvs || !GV.W) return;
  const r = cvs.getBoundingClientRect();
  if (r.width < 20) return;
  const pad = 26;
  // 大项目（几百个文件、几十列）不能把整张图压进屏幕：压进去每个节点只剩几个
  // 像素，用户看到的就是一条叠死的模糊横带（真实项目 faker 806 文件实测）。
  // 保证节点可读的下限缩放，初始停在「启动入口」那一列，其余靠平移/缩放看。
  const fit = Math.min(1.6, (r.width - pad * 2) / GV.W, (r.height - pad * 2) / GV.H);
  const huge = GV.W > (r.width - pad * 2) * 2;      // 画布远宽于视口 → 大项目
  GV.scale = huge ? Math.max(0.7, fit) : fit;
  const sW = GV.W * GV.scale, sH = GV.H * GV.scale;
  if (huge && sW > r.width - pad * 2) {
    // 横向锚定启动入口列（第 0 层第一列），纵向居中；边界留白兜底
    const c0 = (GV.cols || []).find((c) => c.level === 0) || (GV.cols || [])[0];
    const ax = c0 ? c0.x + 100 : 0;
    GV.tx = pad - ax * GV.scale;
    GV.ty = Math.max(pad / 2, (r.height - sH) / 2);
    if (GV.tx > pad) GV.tx = pad;                     // 画布其实没那么宽：靠左即可
    if (GV.tx + sW < r.width - pad) GV.tx = r.width - pad - sW;
  } else {
    GV.tx = Math.max(pad, (r.width - sW) / 2);
    GV.ty = Math.max(pad / 2, (r.height - sH) / 2);
  }
  applyGvTransform();
}

function zoomAt(clientX, clientY, k) {
  const cvs = $('#gvCanvas');
  if (!cvs) return;
  const r = cvs.getBoundingClientRect();
  const px = clientX - r.left, py = clientY - r.top;
  const ns = Math.min(2.6, Math.max(0.18, GV.scale * k));
  GV.tx = px - (px - GV.tx) * (ns / GV.scale);
  GV.ty = py - (py - GV.ty) * (ns / GV.scale);
  GV.scale = ns;
  applyGvTransform();
}

function bindGraphNodes() {
  $$('#gvSvg .gn').forEach((g) => {
    g.onclick = (e) => { e.stopPropagation(); selectGraphNode(g.dataset.file); };
    g.ondblclick = (e) => { e.stopPropagation(); openFromGraph(g.dataset.file); };
  });
}

function selectGraphNode(name) {
  GV.sel = name;
  const f = (S.projectMap && S.projectMap.files || []).find((x) => x.name === name);
  const detail = $('#gvDetail');
  if (!f || !detail) return;
  const svg = $('#gvSvg');
  if (svg) svg.classList.add('focus');        // 聚焦模式：其余节点/连线整体变暗
  $$('#gvSvg .gn').forEach((g) => g.classList.toggle('sel', g.dataset.file === name));
  // 相关边高亮
  $$('#gvSvg .ge').forEach((p) => p.classList.toggle('hot', p.dataset.from === name || p.dataset.to === name));

  const meta = KIND_META[f.kind] || KIND_META.unknown;
  const c = covOf(name);
  const deps = ((S.projectMap.edges || []).filter((e) => e.from === name).map((e) => e.to));
  const users = (f.imported_by || []);
  const covTxt = !c ? '还没跑过 —— 点下面「跑全部入口」或单独运行它'
    : c.err ? `${c.errKind === 'timeout' || c.errKind === 'args' ? '⚠' : '❌'} ${c.err}${c.ms ? ` · ${c.ms} ms` : ''}`
      : c.ok === false ? '❌ 未通过'
        : c.via ? `✔ 已被 ${c.via} 执行时用到`
          : `✔ 执行成功 · ${c.events} 个事件 · ${c.ms || 0} ms`;
  // 失败要说清「为什么」和「接下来怎么办」——只标个红点等于没说
  const failHtml = (c && c.err && c.errHint)
    ? `<div class="dr"><b>原因</b><i>${esc(c.errHint)}</i></div>`
      + (c.errText ? `<pre class="gv-err">${esc(String(c.errText).slice(-500))}</pre>` : '')
    : '';

  detail.innerHTML = `
    <button class="gv-close" data-close="1">×</button>
    <div class="dt">${meta.icon} ${esc(name.split('/').pop())}</div>
    <div class="dk">${esc(name)}</div>
    <div class="dr"><b>定位</b><i>${esc(meta.label)}${f.reason ? ' · ' + esc(f.reason) : ''}</i></div>
    <div class="dr"><b>规模</b><i>${f.lines || 0} 行${(f.defs || []).length ? ` · ${f.defs.length} 个函数/类` : ''}</i></div>
    <div class="dr"><b>依赖</b><i>${deps.length ? deps.map((d) => `<code>${esc(d.split('/').pop())}</code>`).join(' ') : '—（不依赖项目内其它文件）'}</i></div>
    <div class="dr"><b>被谁用</b><i>${users.length ? users.map((u) => `<code>${esc(u.split('/').pop())}</code>`).join(' ') : '—'}</i></div>
    <div class="dr"><b>运行</b><i>${esc(covTxt)}</i></div>
    ${failHtml}
    <div class="da">
      <button class="btn tiny ghost" data-open="${esc(name)}">打开文件</button>
      ${f.kind === 'startup' ? `<button class="btn tiny primary" data-run="${esc(name)}">▶ 运行它</button>` : ''}
    </div>`;
  detail.hidden = false;
  const cb = detail.querySelector('[data-close]');
  if (cb) cb.onclick = () => { detail.hidden = true; GV.sel = ''; $$('#gvSvg .gn').forEach((g) => g.classList.remove('sel')); $$('#gvSvg .ge').forEach((p) => p.classList.remove('hot')); };
  const ob = detail.querySelector('[data-open]');
  if (ob) ob.onclick = () => openFromGraph(ob.dataset.open);
  const rb = detail.querySelector('[data-run]');
  if (rb) rb.onclick = () => runSingleFromGraph(rb.dataset.run);
}

function openFromGraph(name) {
  if (S.filename !== name) openTreeFile(name);
  setView('stage');
  toast(`已打开 ${name.split('/').pop()} —— 舞台里可以逐行读它`, '');
}

/* ---- 批量执行：依次真实跑每个启动入口 ---- */
function setProg(done, total, txt) {
  const bar = $('#gvPbar'), t = $('#gvProgTxt');
  if (bar) bar.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
  if (t) t.textContent = txt || '';
}

function resolveProjName(p) {
  // 事件里的 file 有两种形态：trace 事件只给文件名（stats.py），
  // import 事件给的是磁盘绝对路径（D:/work/proj/helpers/cfg.py）—— 统一按文件名匹配
  const base = String(p || '').replace(/\\/g, '/').split('/').pop();
  if (!base) return '';
  const list = (S.projectFiles || []).filter((f) => f.name.split('/').pop() === base);
  if (!list.length) return '';
  return list.sort((a, b) => a.name.length - b.name.length)[0].name;
}

/* 包里的 __init__.py 是被「同包模块被 import」时自动执行的：
   事件流里看不到它的帧（importlib 的父帧被过滤），按语义补一次标记 */
function markPackageInits() {
  if (!S.projectFiles) return;
  for (const f of S.projectFiles) {
    const isInit = f.name === '__init__.py' || f.name.endsWith('/__init__.py');
    if (!isInit || S.coverage.get(f.name)) continue;
    const dir = f.name.includes('/') ? f.name.slice(0, f.name.lastIndexOf('/')) : '';
    const used = [...S.coverage.keys()].some((k) => k !== f.name
      && (dir ? k.startsWith(dir + '/') : !k.includes('/')) && !S.coverage.get(k).err);
    if (used) S.coverage.set(f.name, { events: 0, ok: true, via: '同包模块', ms: 0, err: '' });
  }
}

/* 运行失败的分类：退出码非 0 不等于「你的代码有 bug」。
   常驻服务超时、命令行工具没给参数、缺第三方包、脚本自检断言失败 —— 处理方式完全不同，
   一律标「报错」会让用户以为项目坏了，也会把真正的问题淹没。 */
function classifyRunFail(code, errMsg, errType, cliTool) {
  const s = String(errMsg || '');
  if (errType === 'Timeout' || code === -1 || /执行超过 .* 秒/.test(s)) {
    return {
      kind: 'timeout', label: '超时',
      hint: '跑满时间上限被终止。常驻服务（Web / 消息队列）或等待输入的脚本本来就跑不完，'
        + '这不是错误；想看完它，把顶栏「超时」调大，或换成「单独打开后逐行阅读」。',
    };
  }
  if (/No module named|ModuleNotFoundError/i.test(s)) {
    return {
      kind: 'deps', label: '缺依赖',
      hint: '这个解释器里没装它 import 的包。换一个装了依赖的解释器，或点「验证环境」看缺哪些。',
    };
  }
  if (/usage:|--help|unrecognized arguments|required: |Use --help to show usage|the following arguments are required/i.test(s)) {
    return {
      kind: 'args', label: '需参数',
      hint: '这是命令行工具：不给参数时它只会打印用法然后退出。在顶栏「参数」里填上参数'
        + '（如 -h 或它需要的输入文件）再运行。',
    };
  }
  // 静态已经认出它是命令行工具（import argparse / 读 sys.argv），且没有真 traceback：
  // 这类脚本不给参数本来就是"打印用法 + 非 0 退出"，不该标成代码报错
  if (cliTool && !/Traceback \(most recent call last\)/i.test(s)) {
    return {
      kind: 'args', label: '需参数',
      hint: '这是命令行工具：不带参数运行时它只打印用法就退出。在顶栏「参数」里填上它需要的参数'
        + '（先试 -h 看用法）再运行。',
    };
  }
  if (/ConnectionRefusedError|Connection refused|Max retries exceeded|NewConnectionError/i.test(s)) {
    return {
      kind: 'net', label: '连不上服务',
      hint: '脚本要连本地或外部服务，但连接被拒绝：确认那个服务（数据库 / API / 本地端口）正在运行，'
        + '或先把它启动再重跑。',
    };
  }
  if (/FileNotFoundError|No such file or directory/i.test(s)) {
    return {
      kind: 'io', label: '缺文件',
      hint: '脚本要读的文件不存在：检查运行目录对不对（脚本默认以所在目录为工作目录），'
        + '以及文件是不是没一起拷过来。',
    };
  }
  if (/PermissionError|Permission denied/i.test(s)) {
    return {
      kind: 'io', label: '权限不足',
      hint: '读写被系统拒绝：多半是杀毒软件 / 系统权限拦截，把项目目录加入白名单后重试。',
    };
  }
  if (/AssertionError|assert\s/i.test(s)) {
    return {
      kind: 'assert', label: '断言未过',
      hint: '脚本自己的自检没通过（assert 断言失败）。看下面 stderr 原文，通常是测试用例的预期不满足。',
    };
  }
  if (code === 0) return { kind: '', label: '', hint: '' };
  return {
    kind: 'error', label: '退出码 ' + code,
    hint: '脚本以非 0 退出。看下面 stderr 原文定位具体报错行。',
  };
}

function runEntryOnce(name, timeout) {
  return new Promise((resolve) => {
    const f = (S.projectFiles || []).find((x) => x.name === name);
    const res = { name, ok: false, events: 0, files: new Set(), ms: 0, err: '', accepted: false,
                  errKind: '', errHint: '', errText: '', errMsg: '', errType: '' };
    if (!f) { res.err = '文件已不在项目里'; return resolve(res); }
    let settled = false;
    const t0 = performance.now();
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/run`);
    const done = (patch) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      Object.assign(res, patch || {});
      try { ws.close(); } catch (e) { /* 已关闭 */ }
      resolve(res);
    };
    const timer = setTimeout(() => {
      const cf = classifyRunFail(-1, '', 'Timeout');
      done({ err: cf.label, errKind: cf.kind, errHint: cf.hint, ms: performance.now() - t0 });
    }, (timeout + 6) * 1000);
    ws.onopen = () => {
      try { ws.send(JSON.stringify(buildRunPayload(f.code, name, timeout, true))); }
      catch (e) { done({ err: '消息发送失败' }); }
    };
    const onEntryEvent = (ev) => {
      if (ev.e === 'accepted') { res.accepted = true; return; }
      if (ev.e === 'done') {
        const code = ev.code || 0;
        if (code !== 0) {
          const meta = ((S.projectMap && S.projectMap.files) || []).find((x) => x.name === name);
          const cf = classifyRunFail(code, res.errMsg, res.errType, !!(meta && meta.cli_tool));
          done({ ok: false, code, err: cf.label, errKind: cf.kind, errHint: cf.hint,
                 errText: String(res.errMsg || '').slice(-600), ms: ev.wall || (performance.now() - t0) });
        } else {
          done({ ok: true, code, ms: ev.wall || (performance.now() - t0) });
        }
        return;
      }
      res.events++;
      const fn = typeof ev.file === 'string' ? ev.file : '';
      if (fn && fn.endsWith('.py')) res.files.add(fn);
      // 记下第一条错误事件的原文与类型：退出码非 0 时靠它判断"到底是什么失败"
      if (ev.e === 'error' && !res.errMsg) {
        res.errMsg = String(ev.msg || '');
        res.errType = String(ev.type || '');
      }
    };
    // 攒批后端会发「数组」消息；单条消息也兼容（新旧后端通用）
    ws.onmessage = (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch { return; }
      (Array.isArray(data) ? data : [data]).forEach(onEntryEvent);
    };
    ws.onerror = () => done({ err: '连接失败', errKind: 'link', errHint: '与本地服务的连接出错，确认服务窗口还在运行' });
    ws.onclose = () => done(res.accepted
      ? {}
      : { err: '未送达', errKind: 'link', errHint: '请求没送到执行器就断开了，多半是文件过多或服务异常' });
  });
}

async function runAllEntries() {
  if (GV.running) return;
  if (!S.projectMap) { toast('先打开一个文件夹项目', 'warn'); return; }
  const limit = parseInt(($('#gvLimit') || {}).value || '10', 10);
  const all = S.projectMap.startups || [];
  if (!all.length) {
    toast('这个项目没有识别到启动入口（没有 __main__ 块，顶层也没有直接执行的语句）—— 点节点逐个阅读即可', 'warn');
    return;
  }
  const entries = all.slice(0, limit);
  S.coverage = S.coverage || new Map();
  GV.running = true; GV.stop = false;
  const btn = $('#gvRunAll'), stopBtn = $('#gvStop'), prog = $('#gvProg');
  if (btn) btn.hidden = true;
  if (stopBtn) { stopBtn.hidden = false; stopBtn.textContent = '停止'; }
  if (prog) prog.hidden = false;
  const timeout = Math.min(25, Math.max(5, parseInt($('#timeoutInput').value || '30', 10)));
  // 按「为什么没跑通」分类计数：超时 / 需参数 属于脚本本身的性质，不算项目有问题
  const tally = {};
  const bump = (k) => { tally[k] = (tally[k] || 0) + 1; };

  for (let i = 0; i < entries.length; i++) {
    if (GV.stop) break;
    const name = entries[i];
    setProg(i, entries.length, `正在跑 ${name.split('/').pop()}（${i + 1}/${entries.length}）…`);
    let r = await runEntryOnce(name, timeout);
    // 连接类失败（WS 中断/未送达）自动重试一次：几十次连跑中一次网络/服务抖动
    // 不该把整个入口判死刑 —— 用户实测"跑到 29/54 后全部连接失败"多属瞬时故障
    if (r.errKind === 'link' && !GV.stop) {
      setProg(i, entries.length, `${name.split('/').pop()} 连接抖动，重试中（${i + 1}/${entries.length}）…`);
      await new Promise((res) => setTimeout(res, 800));
      r = await runEntryOnce(name, timeout);
    }
    bump(r.ok ? 'ok' : (r.errKind || 'error'));
    S.coverage.set(name, { events: r.events, ok: r.ok, ms: Math.round(r.ms), err: r.err,
      errKind: r.errKind || '', errHint: r.errHint || '', errText: r.errText || '' });
    // 它执行时用到的其它项目文件也点亮（这些文件不需要单独运行，是入口带出来的）
    for (const bn of r.files) {
      const pn = resolveProjName(bn);
      if (pn && pn !== name && !S.coverage.get(pn)) {
        S.coverage.set(pn, { events: 0, ok: true, via: name, ms: 0, err: '' });
      }
    }
    if (GV.view === 'graph') renderGraph(true);
    // 批间缓冲：给服务端留出 GC / 落盘 / 子进程收尾的时间，几十次连跑更稳
    await new Promise((res) => setTimeout(res, 350));
  }

  GV.running = false;
  if (btn) btn.hidden = false;
  if (stopBtn) stopBtn.hidden = true;
  markPackageInits();
  const touched = [...(S.coverage || new Map()).keys()].filter((k) => ranOf(S.coverage.get(k))).length;
  const total = (S.projectMap.files || []).length;
  const KIND_LABEL = { ok: '跑通', timeout: '超时', args: '需参数', deps: '缺依赖', net: '连不上服务', io: '文件/权限', assert: '断言未过', error: '报错', link: '未送达' };
  const parts = ['ok', 'timeout', 'args', 'deps', 'net', 'io', 'assert', 'error', 'link']
    .filter((k) => tally[k]).map((k) => `${KIND_LABEL[k]} ${tally[k]}`);
  const realBad = (tally.error || 0) + (tally.deps || 0) + (tally.assert || 0) + (tally.link || 0)
    + (tally.net || 0) + (tally.io || 0);
  const summary = `${GV.stop ? '已停止' : '完成'}：${entries.length} 个入口 · ${parts.join(' · ')} · 触及 ${touched}/${total} 个文件`;
  setProg(entries.length, entries.length, summary);
  setTimeout(() => { if (!GV.running && prog) prog.hidden = true; }, 9000);
  if (GV.view === 'graph') renderGraph();
  toast(summary + (realBad ? '' : '（超时/需参数不是错误，点节点看原因）'), realBad ? 'warn' : 'ok');
}

async function runSingleFromGraph(name) {
  if (GV.running) return;
  const timeout = Math.max(1, Math.min(120, parseInt($('#timeoutInput').value || '30', 10)));
  GV.running = true;
  toast(`正在运行 ${name.split('/').pop()} …`, '');
  const r = await runEntryOnce(name, timeout);
  GV.running = false;
  S.coverage = S.coverage || new Map();
  S.coverage.set(name, { events: r.events, ok: r.ok, ms: Math.round(r.ms), err: r.err,
      errKind: r.errKind || '', errHint: r.errHint || '', errText: r.errText || '' });
  for (const bn of r.files) {
    const pn = resolveProjName(bn);
    if (pn && pn !== name && !S.coverage.get(pn)) S.coverage.set(pn, { events: 0, ok: true, via: name, ms: 0, err: '' });
  }
  markPackageInits();
  renderGraph();
  selectGraphNode(name);
  toast(r.ok ? `${name.split('/').pop()}：${r.events} 个事件，${Math.round(r.ms)} ms`
    : `${name.split('/').pop()} · ${r.err || '退出码非 0'}`
      + (r.errHint ? ` —— ${r.errHint.split('。')[0]}` : ''), r.ok ? 'ok' : (r.errKind === 'timeout' || r.errKind === 'args' ? 'warn' : 'err'));
}

/* ---- 绑定 ---- */
function bindGraph() {
  $$('#stageSeg .seg-b').forEach((b) => { b.onclick = () => setView(b.dataset.view); });
  bindDoc();
  const cvs = $('#gvCanvas'), svg = $('#gvSvg');
  if (!cvs || !svg) return;

  // 平移
  let drag = null;
  cvs.addEventListener('mousedown', (e) => {
    if (e.target.closest('.gn') || e.target.closest('.gv-detail')) return;
    drag = { x: e.clientX, y: e.clientY, tx: GV.tx, ty: GV.ty };
    cvs.classList.add('drag');
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    GV.tx = drag.tx + (e.clientX - drag.x);
    GV.ty = drag.ty + (e.clientY - drag.y);
    applyGvTransform();
  });
  window.addEventListener('mouseup', () => { drag = null; cvs.classList.remove('drag'); });

  // 滚轮缩放（只在全景图激活时接管）
  cvs.addEventListener('wheel', (e) => {
    if (GV.view !== 'graph') return;
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.12 : 0.89);
  }, { passive: false });

  // 点空白处取消选中
  svg.addEventListener('click', (e) => {
    if (e.target.closest('.gn')) return;
    const d = $('#gvDetail');
    if (d) d.hidden = true;
    GV.sel = '';
    svg.classList.remove('focus');
    $$('#gvSvg .gn').forEach((g) => g.classList.remove('sel'));
    $$('#gvSvg .ge').forEach((p) => p.classList.remove('hot'));
  });

  const fit = $('#gvFit'); if (fit) fit.onclick = () => fitGraph();
  const trm = $('#gvTrim');
  if (trm) trm.onclick = () => {
    // undefined = 自动（大项目默认精简）；点一下强制开，再点强制关
    GV.trim = (GV.trim === undefined) ? true : !GV.trim;
    renderGraph();
  };
  const gex = $('#gvExport'); if (gex) gex.onclick = () => exportReport(gex);
  const run = $('#gvRunAll'); if (run) run.onclick = () => runAllEntries();
  const stop = $('#gvStop');
  if (stop) stop.onclick = () => { GV.stop = true; stop.textContent = '正在停止…'; };
  const lim = $('#gvLimit');
  if (lim) lim.onchange = () => { if (S.projectMap) renderGraph(); };
  let _rszT = null;
  window.addEventListener('resize', () => {
    // 窗口尺寸变化后画面滞留旧布局（左列被裁在容器外）：防抖后强制重新适配
    if (GV.view !== 'graph') return;
    clearTimeout(_rszT);
    _rszT = setTimeout(() => { GV.tx = 0; GV.ty = 0; fitGraph(); }, 120);
  });
  markGraphAvail();
}

/* ================= 顶栏自适应收纳（JS 实测驱动，替代固定断点） =================
   原理：真的去量 topbar.scrollWidth 是否超过 clientWidth，溢出就按优先级
   从低到高逐个隐藏，窗口变宽再逐个放回。优点：
   ① exe 的 --app 窗口可以拖到任意宽度，固定断点永远追不上；
   ② 不依赖 :has()（旧版 Edge 不支持，断点规则会整段失效）；
   ③ 元素字体加载、下拉选项异步填充导致的内容变宽也能被 ResizeObserver 捕获。
   「示例」与「项目目录」二选一保命：没填项目目录的用户（在线预览 / 纯单文件）
   靠「示例」下拉载入代码，此时目录框先藏；填了项目目录的用户正相反。
   永远保留：解释器 / 自检 / 调试 / 运行 / 文件名 chip。 */
function tbBuildOrder() {
  const rootEl = document.querySelector('#rootInput');
  const rootBusy = !!(rootEl && rootEl.value);
  const common = ['#argvInput', '#timeoutInput', '#btnEnvCheck', '#btnOpen', '#btnRecent', '#btnOpenDir'];
  const last = rootBusy
    ? ['#sampleSel']                              // 有项目目录：示例先藏，目录保到最后
    : ['#rootInput'];                             // 无项目目录：目录先藏，示例永不藏
  // 最后两级兜底：副标题「Python 运行可视化工作台」→ 品牌文字只剩 Logo
  const brand = ['.brand-text small', '.brand-text'];
  // pyChip / verChip 是纯信息徽章（Python 版本 / PyTrace 版本），优先级最低；
  // 它们在解释器检测后被 JS 显示，若不纳管会在 1000px 左右凭空多占 220px+
  return ['#pyChip', '#verChip', '#btnLive', ...common, ...last, ...brand];
}
let _tbItems = null;
function fitTopbar(allowRestore) {
  const tb = document.querySelector('.topbar');
  if (!tb) return;
  // 每次都按当前状态重建优先级（项目目录填/清空会反转示例与目录的优先级）；
  // 收纳状态记在元素自己的 tb-hide 类上，重建不会丢
  _tbItems = tbBuildOrder()
    .map((s) => { const el = document.querySelector(s); return el ? (el.closest('.mini') || el) : null; })
    .filter(Boolean);
  const visible = () => _tbItems.filter((el) => !el.classList.contains('tb-hide'));
  // 溢出判定必须同时看三个层级：
  // ① topbar 自身（runbar 撑出）② filebar 内部 ③ brand 内部。
  // 只看 ① 会被 flex 欺骗：filebar/brand 被压窄后，里面的按钮不会跟着缩，
  // 而是溢出盒子叠到相邻区域——这正是此前反复出现的"顶栏重叠"的真正机制
  const _fb = document.querySelector('.filebar');
  const _br = document.querySelector('.brand');
  const over = () => tb.scrollWidth > tb.clientWidth + 1
    || (_fb && _fb.scrollWidth > _fb.clientWidth + 1)
    || (_br && _br.scrollWidth > _br.clientWidth + 1);
  let guard = 0;
  // squeeze 判定永远在「无 squeeze」基准上做（先摘掉再量），否则
  // squeeze 生效→溢出归零→摘掉→又溢出→再戴上 的 RO 反馈环会无限振荡
  const hadSqueeze = tb.classList.contains('tb-squeeze');
  if (hadSqueeze) tb.classList.remove('tb-squeeze');
  // 空间不足：从最低优先级的可见项开始逐个藏，直到放下为止
  while (over() && visible().length && guard++ < _tbItems.length + 2) {
    visible()[0].classList.add('tb-hide');
  }
  // 极窄兜底：所有收纳层级用尽仍放不下 → 两个下拉压到 120px
  const stillOverflow = over();
  tb.classList.toggle('tb-squeeze', stillOverflow);
  // 空间富余：从最高优先级的隐藏项开始逐个试放（放不进就退回并停止）。
  // 试放判定必须用完整 over()（含 filebar/brand 内部溢出）——只看顶栏的话，
  // 放回的项会被 flex 伪装成"放得下"，实际把 filebar 重新压出内部重叠
  if (allowRestore && !stillOverflow) {
    const hidden = _tbItems.filter((el) => el.classList.contains('tb-hide'));
    for (const el of hidden.reverse()) {
      el.classList.remove('tb-hide');
      if (over()) { el.classList.add('tb-hide'); break; }
    }
  }
}

(async function init() {
  loadBreaks();            // 断点与调试偏好：localStorage 持久化，重开页面还在
  bind();
  bindGraph();
  // 顶栏自适应收纳：初始量一次；窗口缩放与内容变化都允许"放回"——
  // 否则「目录项目 chip 变长时藏掉的按钮，换回单文件后永远回不来」，
  // 面板 anchored 在滞留的按钮上还会盖住别的控件。
  // restore 自带"放不下就退回"保护不会振荡；squeeze 振荡已用无 squeeze 基准测量解决。
  fitTopbar(true);
  let _tbRzT = null;
  window.addEventListener('resize', () => {
    clearTimeout(_tbRzT);
    _tbRzT = setTimeout(() => fitTopbar(true), 120);
  });
  const tbRO = new ResizeObserver(() => fitTopbar(true));
  const _fb = document.querySelector('.filebar'), _rb = document.querySelector('.runbar');
  if (_fb) tbRO.observe(_fb);
  if (_rb) tbRO.observe(_rb);
  document.fonts && document.fonts.ready.then(() => fitTopbar(true));
  await loadSamples();     // 仅填充「示例」下拉，不自动载入
  await detectDemo();
  await loadInterpreters(); // 找出本机能跑这份代码的 Python
  loadLocalProject();      // 上次填过的项目目录 → 自动扫描整个文件夹
  fitTopbar(true);         // 解释器列表 / 项目目录填充后可能变宽，重新量一次
  { const _ri = document.querySelector('#rootInput');
    if (_ri) _ri.addEventListener('change', () => fitTopbar(true)); }
  // 进入主程序默认为空状态：不显示内置示例内容，
  // 需要示例时从顶栏「示例」下拉中选择，或拖入自己的 .py / 文件夹
  renderCode();            // S.code 为空 → 显示拖入引导
  renderFileTree();
  renderClock();
})();
