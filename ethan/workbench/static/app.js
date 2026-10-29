/* Earth Online 工作台前端：不用框架，数据全从 /api/state、/api/quests 来，操作走 /api/act。 */
"use strict";

// 图片请求带不上 Authorization 头；Telegram 里 cookie 不一定在，就把令牌放进查询串（服务器只对 /img/ 认）
const imgUrl = (u) => S.token ? u + "?s=" + encodeURIComponent(S.token) : u;
const S = { state: null, quests: null, page: "main", tab: "main", overlay: null, back: null, token: null, busy: false, showDone: false };
const ICON = {
  home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  quest: "M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18zm0 5a4 4 0 1 0 0 8a4 4 0 0 0 0-8z",
  map: "M9 4l-6 2v14l6-2l6 2l6-2V4l-6 2zM9 4v14M15 6v14",
  cards: "M5 4h10a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM17 7l3 1a2 2 0 0 1 1 2.5l-3.5 10",
  ach: "M8 4h8v5a4 4 0 0 1-8 0zM8 6H5a3 3 0 0 0 3 4M16 6h3a3 3 0 0 1-3 4M12 13v4M8 20h8",
  brain: "M9 4a3 3 0 0 0-3 3a3 3 0 0 0-2 5a3 3 0 0 0 3 5a3 3 0 0 0 5 1V5a3 3 0 0 0-3-1zm6 0a3 3 0 0 1 3 3a3 3 0 0 1 2 5a3 3 0 0 1-3 5a3 3 0 0 1-5 1",
  team: "M9 11a3 3 0 1 0 0-6a3 3 0 0 0 0 6zM3 20a6 6 0 0 1 12 0M16 11a3 3 0 1 0 0-6M21 20a6 6 0 0 0-4.5-5.8",
  gear: "M12 9a3 3 0 1 0 0 6a3 3 0 0 0 0-6zM12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
};
const MENU = [["main", "今日", "home"], ["quests", "任务", "quest"], ["map", "世界", "map"], ["cards", "卡册", "cards"],
              ["ach", "成就", "ach"], ["brain", "大脑", "brain"], ["team", "团队", "team"], ["settings", "设置", "gear"]];
const READY = { main: true, quests: true, map: true, team: true, brain: true, cards: true, ach: true, settings: true };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (sel) => document.querySelector(sel);

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; }
  return null;
}

// 把走到哪一步报给服务器日志（手机上看不到控制台）
function trace(step, info) {
  try {
    fetch("/api/clientlog", { method: "POST", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({ step, info: String(info ?? "").slice(0, 400) }) }).catch(() => {});
  } catch (e) { /* 不影响页面 */ }
}

async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 20000);  // 20 秒没回就算连不上，不一直转
  let res;
  try {
    res = await fetch(path, { method: body ? "POST" : "GET", headers, credentials: "same-origin", signal: ctl.signal,
                              body: body ? JSON.stringify(body) : undefined });
  } finally { clearTimeout(timer); }
  if (res.status === 401) { const e = new Error("401"); e.code = 401; throw e; }
  return res.json();
}

async function boot() {
  trace("boot", navigator.userAgent + " w=" + window.innerWidth);
  S.token = store("eo_token");
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.initData) {
    tg.ready(); tg.expand();
    try {
      const r = await api("/api/auth/webapp", { initData: tg.initData });
      if (r.ok) { S.token = r.token; store("eo_token", r.token); }
    } catch (e) { /* 走 cookie */ }
  }
  await load();
  setInterval(() => { if (!S.overlay) load(true); }, 60000);
}

async function load(quiet) {
  try {
    const st = await api("/api/state");
    if (!st || st.ok === false) { if (!quiet) showError((st && st.error) || "服务器没给数据"); return; }
    S.state = st;
    if (S.page === "quests" || (S.overlay || "").startsWith("boss:")) S.quests = await api("/api/quests");
    if (S.page === "team") S.team = await api("/api/team");
    if (S.page === "map") S.map = await api("/api/map");
    if (S.page === "map") S.review = await api("/api/review");
    if (S.page === "settings") S.settings = await api("/api/settings");
    if (S.page === "territory") S.terr = await api("/api/territory?k=" + encodeURIComponent(S.tkey));
    if (S.page === "cards") S.cards = await api("/api/cards");
    if (S.page === "ach") S.ach = await api("/api/ach");
    if (S.page === "brain") S.brain = await api("/api/brain?" + new URLSearchParams(S.bq || { tab: "all" }));
  } catch (e) {
    if (e.code === 401) return renderLogin();
    if (!quiet) showError("连不上服务器：" + (e.name === "AbortError" ? "20 秒没有回应" : e.message));
    return;
  }
  if (!quiet) trace("state", "page=" + S.page + " today=" + S.state.today.length);
  try { render(); } catch (e) { trace("render-error", e.stack || e.message); showError("页面出错：" + (e.stack || e.message)); return; }
  if (!quiet) trace("rendered", "page html=" + $("#page").innerHTML.length + " nav=" + $("#nav").innerHTML.length);
}

function renderLogin() {
  $("#nav").innerHTML = "";
  $("#page").innerHTML = `<div class="login panel"><h2>Earth Online</h2>
    <p>在 Telegram 里点 小伴 的“打开工作台”按钮就能直接进来。</p>
    <p class="muted">用 Safari 打开的话：在 Telegram 里给 小伴 发“工作台”，点他回的登录链接（10 分钟内有效）。</p>
    <p class="muted">没有 Telegram 的设备（比如 iPad）：在手机上给 小伴 发“设备链接”，把链接拿到那台设备上打开，再“添加到主屏幕”。</p></div>`;
}

// 点头像换日 / 夜（头像和配色一起换）；到了下一次自动换（天亮或天黑）就回到自动
function themeKey(st) { return st.day + (st.night ? "n" : "d"); }
function isNight(st) {
  const saved = (store("eo_theme") || "").split(":");
  return saved[0] === themeKey(st) ? saved[1] === "night" : st.night;
}

function render() {
  const st = S.state;
  const night = isNight(st);
  document.body.className = night ? "night" : "day";
  $("#nav").innerHTML = MENU.map(([k, label, icon]) =>
    `<button type="button" class="${S.page === k || (k === "map" && S.page === "territory") ? "on" : ""}" data-act="go" data-page="${k}" ${READY[k] ? "" : "disabled title=\"第二、三批上线\""} aria-label="${label}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON[icon]}"></path></svg>${label}${(st.badges || {})[k] ? `<i class="navdot" aria-label="${st.badges[k]} 件要你处理">${st.badges[k]}</i>` : ""}</button>`).join("");
  const pages = { quests: questsPage, team: teamPage, map: mapPage, territory: territoryPage, brain: brainPage, cards: cardsPage, ach: achPage, settings: settingsPage };
  $("#page").innerHTML = (pages[S.page] || (() => mainPage(night)))();
  $("#page").classList.toggle("home", !pages[S.page]);
  $("#page").classList.toggle("fit", ["settings", "ach", "team"].includes(S.page));  // 设置页一屏放下，长的模块里面自己滚（2026-10-29）  // 今日页在 iPad 上一屏放下，任务区自己滚
  if (!S.overlay && st.celebrate && st.celebrate.length) { S.celebrating = st.celebrate; S.overlay = "celebrate"; }  // 不管在哪个页面
  renderOverlay();
}

/* ---------- 主界面 ---------- */
function mainPage(night) {
  const st = S.state, p = st.profile, nr = p.next_reward;
  const pct = nr ? Math.max(0, Math.min(100, Math.round(100 * (nr.cost - nr.gap) / nr.cost))) : 100;
  const card = st.card;
  const todo = st.today.filter((t) => !t.done), done = st.today.filter((t) => t.done);
  const cardThumb = card && card.image
    ? (store("eo_card_" + st.day) ? `<img src="${imgUrl(card.image)}" alt="今日卡：${esc(card.title)}"><span class="cap">${esc(card.rarity)} · ${esc(card.title)}</span>`
      : `<span class="cardback"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M12 3l2.2 5.6L20 9.3l-4.5 3.9L17 19l-5-3.1L7 19l1.5-5.8L4 9.3l5.8-.7z"></path></svg><b style="font-size:12px">今日卡</b><span style="font-size:10px;color:#ABA8CC">点我翻开</span></span>`)
    : `<span class="cardback"><span style="font-size:12px">今日卡</span><span style="font-size:10px;color:#ABA8CC">${st.card_making ? "正在画……" : "点一下现画"}</span></span>`;
  return `
  <header class="hud">
    <button type="button" class="avatar" data-act="face" aria-label="头像：白天白天的我，夜里夜晚的我，点一下换另一面">
      <img src="${imgUrl("/img/avatar/" + (night ? "night" : "day"))}" alt="Mia 的头像"><span>${night ? "夜晚的我" : "白天的我"}</span></button>
    <div class="hudgrid">
      <div class="row" style="align-items:baseline"><span class="name">Mia</span><span class="lv">Lv.${p.level} ${esc(p.title)}</span>${p.day_n ? `<span class="muted" style="font-size:12px">Earth Online 第 ${p.day_n} 天</span>` : ""}</div>
      <div class="slogan">${night ? "执行力不够强，是因为不够想。" : "Expect Nothing, Take Jumps!"}</div>
      <div class="row muted" style="font-size:12px">
        <span role="button" class="tap" data-act="xphelp">经验 <b style="color:var(--text);font-size:15px">${p.earned}</b> · 升级还差 ${Math.max(0, p.next_level_at - p.earned)}</span>
        <span role="button" class="tap" data-act="xphelp">· 积分 <b style="color:var(--text);font-size:15px">${p.balance}</b> ⓘ</span>
        <div class="bar" style="width:110px" title="离下一个惊喜"><i style="width:${pct}%"></i></div>
        ${nr ? `<span>离${esc(nr.title)}还差 <b class="gold">${nr.gap}</b> 积分</span>` : ""}
        <span>· 连续满勤 <b style="color:var(--text)">${p.streak}</b> 天</span></div>
      <div class="empty"></div>
      <div class="muted" style="font-size:12px">伦敦${st.weather ? " · " + esc(st.weather) : ""} · ${st.day.slice(5).replace("-", "月")}日 周${st.weekday} ${st.time}</div>
      <div class="empty"></div>
    </div>
    <button type="button" class="cardthumb ${card && card.image && store("eo_card_" + st.day) ? RAR(card.rarity) : ""}" data-act="card" aria-label="今日卡，点一下翻开">${cardThumb}</button>
  </header>
  <section class="panel season" aria-label="赛季进度">
    <b class="nowrap">${esc(st.season.name)}</b><div class="bar"><i style="width:${st.season.pct}%"></i></div>
    <b>${st.season.pct}%</b><span class="muted nowrap">剩 ${st.season.days_left} 天</span>
    ${st.season.line ? `<span class="gold" style="font-size:12px">${esc(st.season.line)}</span>` : ""}
  </section>
  <div class="cols">
    <section class="panel col" aria-label="今天时间线">
      <div class="row" style="justify-content:space-between"><h2>今天</h2><button type="button" class="btn gold" data-act="plan">日历 · 改计划</button></div>
      ${timeline()}
      ${st.detail && st.detail.train ? `<div class="row" style="gap:8px"><button type="button" class="btn" data-act="detail" data-k="train">🏋️ 训练详情</button><button type="button" class="btn" data-act="detail" data-k="meals">🍳 三餐</button></div>` : ""}
    </section>
    <section class="panel col taskpanel" aria-label="今日任务">
      <div class="row" style="justify-content:space-between"><h2>今日任务</h2>
        <span class="row"><span class="muted" style="font-size:12px">已完成 ${done.length} / ${st.today.length}</span>
        <button type="button" class="btn gold" data-act="plan">＋ 加 · 编辑</button></span></div>
      <div class="tasklist">${todo.length ? todo.map(taskRow).join("") : `<p class="muted">${done.length ? "今天的都做完了！" : st.plan_ready ? "今天还空着，想做点什么就点“＋ 加 · 编辑”。" : "计划排好后，今天要做的会出现在这儿。"}</p>`}
      ${done.length ? `<button type="button" class="chip" style="align-self:flex-start" data-act="showdone">已完成 ${done.length} 件 ${S.showDone ? "▾ 收起" : "▸ 展开"}</button>${S.showDone ? done.map(taskRow).join("") : ""}` : ""}</div>
      <div class="chips"><span class="muted" style="font-size:12px;margin-right:4px">日常</span>
        ${st.habits.daily.map((h) => `<button type="button" class="chip ${h.done ? "on" : ""}" data-act="habit" data-id="${h.id}" data-done="${h.done ? 1 : ""}">${h.done ? "✓ " : ""}${esc(short(h.title))}${h.easy ? "（简）" : ""}</button>`).join("")}</div>
      <div class="chips"><span class="muted" style="font-size:12px;margin-right:4px">周常</span>
        ${st.habits.weekly.map((h) => { const ok = h.count >= h.target; return `<button type="button" class="chip ${ok ? "on" : ""}" data-act="habit" data-id="${h.id}" data-done="${h.today ? 1 : ""}">${ok ? "✓ " : "○ "}${esc(short(h.title))} ${h.count}/${h.target}</button>`; }).join("")}
        <span class="muted" style="font-size:11px">达标 +50，多做一次再 +20</span></div>
      ${st.habits.rest_day ? `<div class="gold" style="font-size:12px">今天休整：做到 2 项日常就算满勤</div>` : ""}
    </section>
    <section class="panel col" aria-label="副本">
      <h2>副本进度</h2>
      ${st.bosses.map((b) => `<button type="button" class="boss ${b.main ? "" : "alt"}" data-act="boss" data-id="${b.goal_id}" aria-label="${esc(b.name)}详情">
        <div class="name"><b style="font-size:13px">${b.main ? "★ " : ""}${esc(b.name)}${b.main ? "（主副本）" : ""}</b><span class="muted" style="font-size:11px">${esc(b.line)}</span></div>
        <div class="bar"><i style="width:${Math.round(100 * (b.frac || 0))}%"></i></div>
        <div class="gold" style="font-size:11px">${b.next ? "下一步：" + esc(b.next) : ""}${b.status ? " · " + esc(b.status) : ""} ▸</div>${b.comment ? `<div class="muted one" style="font-size:11px">${esc(b.comment)}</div>` : ""}</button>`).join("") || '<p class="muted">这个赛季还没开副本</p>'}
    </section>
  </div>
  <footer class="foot">
    <button type="button" class="panel ethanline" data-act="ethanday" aria-label="看 小伴 的一天"><span class="gold">小伴</span>　${esc((st.card && st.card.line) || "今天也在。")}<span class="muted nowrap" style="margin-left:auto;font-size:12px">他的一天 ▸</span></button>
    <button type="button" class="panel gold nowrap" data-act="prep">小伴 在准备 ${st.prep.length} 件${(st.badges || {}).main ? ` <span class="rbadge">${st.badges.main} 件等你看</span>` : ""} ▸</button>
  </footer>`;
}

const short = (t) => t.replace(/：.*$/, "").replace("练英语", "英语");

function timeline() {
  const st = S.state;
  if (!st.plan_ready) return '<p class="muted">今天的计划 小伴 还没排出来，每天早上 6:30 他会排好。</p>';
  // 要出门的行点开看：去哪、路上多久、几点出发、几点提醒，带地图链接（2026-10-29 她要）
  const rows = st.timeline.map((t, i) => ({ at: t.at, text: t.text, trip: t.trip ? i : null }));
  rows.push({ at: st.time, text: "现在", now: true, trip: null });
  rows.sort((a, b) => a.at.localeCompare(b.at));
  if (rows.length === 1) rows.push({ at: "", text: "今天不用出门", trip: null });
  return rows.map((r) => r.trip !== null
    ? `<button type="button" class="tl trip" data-act="trip" data-i="${r.trip}"><span class="t">${esc(r.at)}</span><div class="b">${esc(r.text)} <span class="gold">▸</span></div></button>`
    : `<div class="tl ${r.now ? "now" : ""}"><span class="t">${esc(r.at)}</span><div class="b">${esc(r.text)}</div></div>`).join("");
}

const prog = (t) => (t.progress ? ` <span class="prog">（${t.progress[0]}/${t.progress[1]}）</span>` : "");
function taskRow(t) {
  const row = `<button type="button" class="task ${t.done ? "done" : ""}" data-act="task" data-id="${t.id}" data-done="${t.done ? 1 : ""}" data-points="${t.points}">
    <span class="dot">${t.done ? "✓" : ""}</span><span class="ttl">${esc(t.title)}${prog(t)}${t.hard ? ' <span class="gold" style="font-size:11px">硬骨头</span>' : ""}</span>
    <span class="tag">${esc(t.tag)} · +${t.points}</span></button>`;
  // 没做完的能按住 ⋮⋮ 拖着排（2026-10-29 她要）
  return t.done ? row : `<div class="trow" data-tid="${t.id}"><span class="drag" data-drag aria-label="拖动排序" title="按住拖动">⋮⋮</span>${row}</div>`;
}

/* ---------- 任务页 ---------- */
function questsPage() {
  const q = S.quests;
  if (!q) return '<p class="loading">加载中……</p>';
  const tabs = [["main", "主线"], ["side", "支线"], ["daily", "日常"], ["weekly", "周常"], ["shop", "商店"]];
  const p = S.state.profile;
  let body = "";
  if (S.tab === "main") {
    body = `<div class="grid3">${q.chapters.map((c) => `<section class="panel col"><h2>${c.main ? "★ " : ""}${esc(c.name)}${c.main ? "（主副本）" : ""}</h2>
      ${c.steps.map((s) => `<button type="button" class="step ${s.state}" data-act="task" data-id="${s.id}" data-done="${s.state === "done" ? 1 : ""}">
        <span class="gold" style="width:18px;text-align:center">${s.state === "done" ? "✓" : s.state === "now" ? "▶" : "○"}</span>
        <span style="flex:1">${esc(s.title)}${prog(s)}${s.hard ? ' <span class="gold" style="font-size:11px">硬骨头</span>' : ""}${s.milestone ? ' <span class="gold" style="font-size:11px">节点</span>' : ""}</span>
        <span class="gold" style="font-size:11px">+${s.points}</span></button>`).join("")}
</section>`).join("") || '<p class="muted">主线还没拆出来，小伴 会先拆个草稿给你看。</p>'}</div>`;
  } else if (S.tab === "side") {
    body = `<section class="panel col"><span class="muted" style="font-size:12px">支线：副本之外的事，临时冒出来的也算，做完一件 +20。</span>
      ${q.side.map((s) => `<div class="item"><span class="grow">${esc(s.title)}${prog(s)}</span><span class="gold" style="font-size:11px">+${s.points}</span>
        <button type="button" class="btn" data-act="task" data-id="${s.id}" data-done="" data-points="${s.points}">完成</button></div>`).join("") || '<p class="muted">没有支线。</p>'}</section>
      ${(q.later || []).length ? `<section class="panel col"><span class="muted" style="font-size:12px">给以后的副本铺垫：副本暂停了，事情照样可以做，只是不急。</span>
      ${q.later.map((s) => `<div class="item"><span class="grow">${esc(s.title)} <span class="muted" style="font-size:11px">${esc(s.goal)}</span></span><span class="gold" style="font-size:11px">+${s.points}</span>
        <button type="button" class="btn" data-act="task" data-id="${s.id}" data-done="" data-points="${s.points}">完成</button></div>`).join("")}</section>` : ""}`;
  } else if (S.tab === "daily") {
    body = `<section class="panel col"><div class="row"><span>连续满勤 <b class="gold">${q.streak.days}</b> 天 · 最长 ${q.streak.best} 天</span>
      <button type="button" class="btn" style="margin-left:auto" data-act="rest" ${q.rest_day || !q.rest_left ? "disabled" : ""}>${q.rest_day ? "今天休整中" : `今天开休整日（本周剩 ${q.rest_left} 次）`}</button></div>
      ${q.dailies.map((d) => { const done = d.week[6]; return `<div class="item"><span class="grow">${esc(d.title)}</span><span class="week">${d.week.map((x) => (x ? "●" : "○")).join("")}</span>
        ${done ? `<button type="button" class="btn" data-act="habit" data-id="${d.id}" data-done="1">撤销</button>`
               : `<button type="button" class="btn" data-act="habit" data-id="${d.id}" data-done="">打卡</button><button type="button" class="btn" data-act="habit" data-id="${d.id}" data-done="" data-easy="1">简单版</button>`}</div>`; }).join("")}
      <span class="muted" style="font-size:11px">状态不好就做简单版：一样算打卡、连续不断，拿一半分。</span></section>`;
  } else if (S.tab === "weekly") {
    body = `<section class="panel col">${q.weekly.map((h) => { const ok = h.count >= h.target; return `<div class="item"><span class="grow">${esc(h.title)}</span>
      <span class="muted">本周 ${h.count} / ${h.target}</span><button type="button" class="btn" data-act="habit" data-id="${h.id}" data-done="${h.today ? 1 : ""}">${h.today ? "撤销今天这次" : ok ? "又做了一次 +20" : "完成 +50"}</button></div>`; }).join("")}
      <span class="muted" style="font-size:11px">达标了还想做就接着做，每多一次 +20（一天记一次）。</span></section>`;
  } else {
    const TIER = { 小: ["t1", "银封"], 中: ["t2", "星封"], 大: ["t3", "金封"] };
    const have = Math.max(0, q.balance);
    body = `${q.balance < 0 ? `<p class="muted" style="font-size:12px">积分 ${q.balance}：拆完惊喜后撤销了几件拿过分的事，欠的这点之后挣回来就抵掉。</p>` : ""}
      <div class="grid3">${q.rewards.map((r) => { const [cls, seal] = TIER[r.tier] || ["t1", ""]; const gap = r.cost - have;
        const days = q.pace > 0 ? Math.ceil(gap / q.pace) : null;
        return `<section class="panel col shop ${cls} ${r.affordable ? "ready" : ""}">
        <div class="row" style="justify-content:space-between;align-items:flex-start"><div><div class="seal">${esc(seal)}</div><h2 style="font-size:18px">${esc(r.title)}</h2></div>
          <svg class="gift" viewBox="0 0 48 48" aria-hidden="true"><rect x="7" y="20" width="34" height="22" rx="3"/><rect x="4" y="13" width="40" height="9" rx="2"/><path d="M24 13v29M24 13c-4-8-13-8-11-2 1 3 7 3 11 2zm0 0c4-8 13-8 11-2-1 3-7 3-11 2z"/></svg></div>
        <div class="muted" style="font-size:12px">${esc(r.note || "")}</div>
        <div class="row" style="font-size:13px"><b class="cost">${r.cost}</b><span class="muted">积分 · 已攒 ${Math.min(have, r.cost)}</span></div>
        <div class="bar"><i style="width:${Math.min(100, Math.round(100 * have / r.cost))}%"></i></div>
        <span class="muted" style="font-size:12px">${r.affordable ? "攒够了，随时可以拆" : days ? `还差 ${gap} 积分，照最近 7 天的速度约 ${days} 天` : `还差 ${gap} 积分`}</span>
        ${r.affordable ? `<button type="button" class="btn primary" data-act="redeem" data-id="${r.id}">拆开</button>`
          : `<button type="button" class="btn short" data-act="shortof" data-gap="${gap}">还没攒够</button>`}</section>`; }).join("")}</div>
      <section class="panel col"><b>兑换记录</b>${q.history.map((h) => `<div class="hrow"><span class="muted" style="flex:none">${esc(h.at.slice(0, 10))}</span><b style="flex:none">${esc(h.title)}</b>
          <span class="muted one" style="min-width:0">${h.status === "待策划" ? "小伴 在准备，会主动来找你揭晓" : esc(h.note || "")}</span>
          ${h.status === "待策划" ? '<span class="hstate wait">待兑现</span>' : '<span class="hstate done">已兑现</span>'}</div>`).join("") || '<span class="muted">还没有兑换过</span>'}</section>`;
  }
  return `<header class="row" style="flex-wrap:wrap"><h2 style="font-size:22px">任务</h2><span class="lv">Lv.${p.level} ${esc(p.title)}</span>
    <span class="muted tap" role="button" data-act="xphelp">经验 <b style="color:var(--text)">${p.earned}</b>（升下一级还差 ${Math.max(0, p.next_level_at - p.earned)}）· 积分 <b style="color:var(--text)">${p.balance}</b> ⓘ</span>
    <button type="button" class="btn gold" style="margin-left:auto" data-act="addtodo">＋ 加待办</button></header>
    <div class="tabs" role="tablist">${tabs.map(([k, l]) => `<button type="button" role="tab" class="${S.tab === k ? "on" : ""}" data-act="tab" data-tab="${k}">${l}</button>`).join("")}</div>${body}`;
}

/* ---------- 领地地图和领地页（docs/design/territories.md；底图是她选的 Grok Imagine 第 2 张） ---------- */
const TERRS = [["body", "健康"], ["career", "事业"], ["money", "财务"], ["social", "社交"], ["study", "学习"], ["academic", "学术"], ["life", "生活体验"]];
// 卡的稀有度：边框不同，超稀有及以上有流光（2026-10-29 她要）
// 每块区域一个小图标（2026-10-29 她说只用一个字有点尴尬）
const TICON = {
  body: "M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10zM7.5 12h2.5l1.5-2.5 2 4.5 1.5-2h1.5",
  career: "M14 4c3.5 0 6 2.5 6 6l-7 7-6-6 7-7zM7 11l-3 1 3 3M13 17l-1 3-3-3M15.5 8.5h.01M4.5 19.5l2.5-2.5",
  money: "M12 3c4 0 7 1.3 7 3s-3 3-7 3-7-1.3-7-3 3-3 7-3zM5 6v4c0 1.7 3 3 7 3s7-1.3 7-3V6M5 10v4c0 1.7 3 3 7 3s7-1.3 7-3v-4M5 14v4c0 1.7 3 3 7 3s7-1.3 7-3v-4",
  social: "M4 5h11a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H9l-4 3v-3H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM16 9h4a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-1v3l-4-3h-3",
  study: "M3 5c3-1 6-1 9 1v14c-3-2-6-2-9-1zM21 5c-3-1-6-1-9 1v14c3-2 6-2 9-1z",
  academic: "M2 9l10-5 10 5-10 5zM6 11v5c3 2.5 9 2.5 12 0v-5M22 9v6",
  life: "M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18zM15.5 8.5l-2 5-5 2 2-5z",
  waste: "M10 21V6a2 2 0 0 1 4 0v15M10 13H8a2 2 0 0 1-2-2V9M14 11h2a2 2 0 0 0 2-2V7M6 21h12",
};
const tIcon = (k) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${TICON[k] || TICON.waste}"></path></svg>`;
const RAR = (r) => ({ 普通: "r1", 稀有: "r2", 超稀有: "r3", 传说: "r4" })[r] || "r1";
const LIGHT = { ok: "达标", warn: "副本落后", bad: "跌破保底", none: "等数据" };
const SEVER = { bad: 0, warn: 1, none: 2, ok: 3 };
// 标注位置：占底图宽高的百分比（按 map-night.jpg 量的）
const MAPPOS = { body: [21, 40], career: [42, 17], money: [72, 22], social: [80, 47], waste: [84, 77], study: [62, 70], academic: [39, 74], life: [16, 66], capital: [49.5, 62] };

// 世界页（2026-10-29 她说进来先看总览分析，而不是一张不变的地图）：总览 = 本周战报 + 各区域 + 缩小的地图（点开放大、再点进区域）；往期 = 过去几周、月度、赛季
const mapTabs = () => `<span class="row" style="gap:6px"><button type="button" class="chip ${S.mapTab !== "history" ? "on" : ""}" data-act="maptab" data-v="">总览</button><button type="button" class="chip ${S.mapTab === "history" ? "on" : ""}" data-act="maptab" data-v="history">📜 往期战报</button></span>`;
const wDots = (terrs) => `<span class="row" style="gap:6px;flex-wrap:wrap">${terrs.map((t) => `<span class="pill p-${t.light}" title="${esc(t.name)}">${esc(t.name)}</span>`).join("")}</span>`;
const wTally = (terrs) => `<table class="htbl"><thead><tr><th>区域</th><th>绿</th><th>黄</th><th>红</th><th>灰</th></tr></thead><tbody>${terrs.map((t) =>
    `<tr><td>${esc(t.name)}</td><td class="p-ok">${t.ok || ""}</td><td class="p-warn">${t.warn || ""}</td><td class="p-bad">${t.bad || ""}</td><td class="muted">${t.none || ""}</td></tr>`).join("")}</tbody></table>`;
const weekCard = (w, open) => `<section class="panel col">
      <div class="row" style="justify-content:space-between;flex-wrap:wrap"><h2>${w.live ? "本周战报" : esc(w.week.slice(5)) + " 那周"}<span class="muted" style="font-size:12px;font-weight:400"> ${esc(w.week.slice(5))}–${esc(w.end.slice(5))}</span></h2>
        <span class="muted" style="font-size:12px">经验 <b style="color:var(--text)">+${w.points}</b> · 满勤 ${w.full_days}/${w.days} 天</span></div>
      ${open ? "" : wDots(w.territories)}
      ${open ? `<div class="cols2">${w.territories.filter((t) => t.rows.some((x) => x.light !== "none")).map((t) => `<div class="col" style="gap:4px"><b style="font-size:13px">${esc(t.name)}</b>${t.rows.filter((x) => x.light !== "none").map((x) => `<span style="font-size:12px">${esc(x.name)}：${esc(x.actual)} <span class="pill p-${x.light}" style="font-size:10px">${LIGHT[x.light]}</span></span>`).join("")}</div>`).join("") || '<span class="muted" style="font-size:12px">这周还没有能算的数据，接上就有了。</span>'}</div>` : ""}
      ${w.bosses && w.bosses.length ? `<div class="col" style="gap:6px">${w.bosses.map((b) => `<div><span style="font-size:12px">${b.main ? "★ " : ""}${esc(b.name)} <span class="muted">${esc(b.status || b.line || "")}</span></span><div class="bar"><i style="width:${Math.round(100 * (b.frac || 0))}%"></i></div></div>`).join("")}</div>` : ""}
      ${w.ethan ? `<details><summary class="gold" style="font-size:13px;cursor:pointer">小伴 的周报</summary><div style="white-space:pre-wrap;font-size:13px;line-height:1.7;margin-top:6px">${esc(w.ethan)}</div></details>` : ""}</section>`;
const mapTags = (m) => m.territories.map((t) => `<button type="button" class="mtag l-${t.light}" data-act="terr" data-k="${t.key}" style="left:${MAPPOS[t.key][0]}%;top:${MAPPOS[t.key][1]}%"><span class="lamp">${LIGHT[t.light]}</span><b class="tname">${tIcon(t.key)}${esc(t.name)}</b><small>${esc(t.line)}</small></button>`).join("") +
  `<button type="button" class="mtag waste" data-act="terr" data-k="waste" style="left:${MAPPOS.waste[0]}%;top:${MAPPOS.waste[1]}%"><b>荒原</b><small>${m.waste ? m.waste + " 件没归类" : "没有没归类的"}</small></button>` +
  `<button type="button" class="mtag capital" data-act="go" data-page="brain" style="left:${MAPPOS.capital[0]}%;top:${MAPPOS.capital[1]}%"><b style="font-size:13px">第二大脑</b><small>${m.brain_ask ? m.brain_ask + " 条要你看" : "全部资料"}</small></button>`;
const mapImg = () => `<img src="/static/map-${isNight(S.state) ? "night" : "day"}.jpg" alt="Earth Online 大陆：七个区域围着中央王城，东南角是荒原" width="1280" height="800">`;

function mapPage() {
  if (S.mapTab === "history") return reviewPage();
  const m = S.map, r = S.review;
  if (!m) return '<p class="loading">加载中……</p>';
  const cards = [...m.territories].sort((a, b) => SEVER[a.light] - SEVER[b.light]).map((t) => `<button type="button" class="tcard2" data-act="terr" data-k="${t.key}">
      <span class="ico">${tIcon(t.key)}</span><span class="g"><b>${esc(t.name)}</b><span class="muted">${esc(t.line)}</span></span><span class="pill p-${t.light}">${LIGHT[t.light]}</span></button>`).join("") +
    `<button type="button" class="tcard2" data-act="terr" data-k="waste"><span class="ico muted">${tIcon("waste")}</span><span class="g"><b>荒原</b><span class="muted">${m.waste} 件没归类的待办</span></span><span class="pill p-none">${m.waste ? `<span class="rbadge">${m.waste}</span> ` : ""}去归类</span></button>`;
  const count = (l) => m.territories.filter((t) => t.light === l).length;
  const verdict = count("bad") ? `${m.territories.filter((t) => t.light === "bad").map((t) => t.name).join("、")}跌破保底，先救它。`
    : count("warn") ? `${m.territories.filter((t) => t.light === "warn").map((t) => t.name).join("、")}落后于副本标准，其余都在线上。`
    : count("ok") ? "有数据的区域都在线上。" : "各区域还在等数据接上。";
  const mini = `<button type="button" class="minimap" data-act="bigmap" aria-label="放大地图">${mapImg()}
      ${m.territories.map((t) => `<i class="mdot s-${t.light}" style="left:${MAPPOS[t.key][0]}%;top:${MAPPOS[t.key][1]}%"></i>`).join("")}<span class="cap">点开大地图 ⤢</span></button>`;
  const s = r && r.season;
  return `<header class="row" style="flex-wrap:wrap"><h2 style="font-size:22px">世界</h2>${mapTabs()}</header>
    <div class="worldtop">
      <section class="panel col"><div class="row" style="justify-content:space-between;flex-wrap:wrap"><h2>总览</h2>${s ? `<span class="muted" style="font-size:12px">${esc(s.name)} · 剩 ${s.days_left} 天 · 累计经验 ${s.points}</span>` : ""}</div>
        <div class="row" style="gap:14px;flex-wrap:wrap"><span><b class="big2 p-ok">${count("ok")}</b> <span class="muted">达标</span></span><span><b class="big2 p-warn">${count("warn")}</b> <span class="muted">副本落后</span></span>
          <span><b class="big2 p-bad">${count("bad")}</b> <span class="muted">跌破保底</span></span><span><b class="big2 muted">${count("none")}</b> <span class="muted">等数据</span></span></div>
        <span style="font-size:14px">${esc(verdict)}</span></section>
      ${mini}
    </div>
    ${r ? weekCard(r.weeks[0], true) : '<p class="loading">战报加载中……</p>'}
    <div class="mapcards">${cards}</div>
    <span class="muted" style="font-size:12px">颜色看这块最差的那一项，灰的是还没接上数据、不算。进度按“到昨天该做到多少”比，今天还没过完，不算你欠的。</span>`;
}

function weightChart(ws) {
  if (!ws || ws.length < 2) return '<p class="muted" style="font-size:12px">还没有体重记录。训练前称一下告诉 小伴，攒满一周就能看到曲线。</p>';
  const v = ws.map((w) => w.avg), lo = Math.floor(Math.min(...v, 56.5) - 0.5), hi = Math.ceil(Math.max(...v) + 0.5);
  const W = 560, H = 190, L = 40, R = 12, T = 12, B = 28;
  const x = (i) => L + (i / (v.length - 1)) * (W - L - R), y = (k) => T + (hi - k) / (hi - lo) * (H - T - B);
  let g = "";
  for (let k = lo; k <= hi; k++) g += `<line x1="${L}" x2="${W - R}" y1="${y(k)}" y2="${y(k)}" stroke="var(--line)"/><text x="${L - 6}" y="${y(k) + 4}" text-anchor="end">${k}</text>`;
  g += `<line x1="${L}" x2="${W - R}" y1="${y(56.5)}" y2="${y(56.5)}" stroke="var(--accent)" stroke-dasharray="5 5"/><text x="${W - R}" y="${y(56.5) - 6}" text-anchor="end">阶段目标 56.5</text>`;
  const pts = v.map((k, i) => `${x(i).toFixed(1)},${y(k).toFixed(1)}`).join(" ");
  g += `<polygon points="${x(0)},${H - B} ${pts} ${x(v.length - 1)},${H - B}" fill="var(--gold)" fill-opacity=".12"/><polyline points="${pts}" fill="none" stroke="var(--gold)" stroke-width="2.5"/>`;
  g += `<circle cx="${x(v.length - 1)}" cy="${y(v[v.length - 1])}" r="4.5" fill="var(--gold)"/><text x="${x(v.length - 1) - 8}" y="${y(v[v.length - 1]) - 10}" text-anchor="end">${v[v.length - 1]}</text>`;
  g += `<text x="${L}" y="${H - 8}">${ws[0].day.slice(5)}</text><text x="${W - R}" y="${H - 8}" text-anchor="end">${ws[ws.length - 1].day.slice(5)}</text>`;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="7 天平均体重，最新 ${v[v.length - 1]} 公斤">${g}</svg>`;
}

function territoryPage() {
  const t = S.terr;
  if (!t) return '<p class="loading">加载中……</p>';
  const back = `<button type="button" class="btn" data-act="terrback">← ${S.terrBack === "bigmap" ? "地图" : ({ brain: "大脑", main: "今日", quests: "任务" })[S.terrBack] || "世界"}</button>`;
  if (t.key === "waste") {
    return `<header class="row" style="flex-wrap:wrap">${back}<h2 style="font-size:22px">荒原</h2><span class="pill p-none">${t.tasks.length} 件没归类</span></header>
      <span class="muted" style="font-size:13px">还没找到家的待办都在这。小伴 按标题先猜了一块，你点头或改掉，归好的就回到那块区域。</span>
      <section class="panel col scrollbox tall">${t.tasks.map((x) => `<div class="item"><span class="grow">${esc(x.title)}${x.due ? `<span class="muted" style="display:block;font-size:11px">${esc(x.due)}</span>` : ""}</span>
        ${x.guess ? `<span class="muted" style="font-size:11px;color:var(--accent)">小伴 猜：${esc(TERRS.find((r) => r[0] === x.guess)[1])}</span>` : ""}
        <select data-change="domain" data-id="${x.id}" aria-label="归到哪个区域"><option value="">归到……</option>${TERRS.map(([k, n]) => `<option value="${k}"${k === x.guess ? " selected" : ""}>${n}</option>`).join("")}</select>
        ${x.guess ? `<button type="button" class="btn gold" data-act="domain-ok" data-id="${x.id}" data-k="${x.guess}">确认</button>` : ""}</div>`).join("") || '<p class="muted">荒原清空啦，每件事都有家了。</p>'}</section>`;
  }
  const summary = t.light === "warn" ? `${t.rows.filter((r) => r.light === "warn").map((r) => r.name).join("、")}落后于副本标准，保底线守住了。`
    : t.light === "bad" ? `${t.rows.filter((r) => r.light === "bad").map((r) => r.name).join("、")}跌破了保底线。`
    : t.light === "ok" ? "这周都在线上。" : "这块还没有数据来源，接上一个亮一个。";
  const rows = t.rows.map((r) => `<tr><td>${esc(r.name)}${r.note ? `<span class="muted" style="display:block;font-size:11px">${esc(r.note)}</span>` : ""}</td><td>${esc(r.quest)}</td><td>${esc(r.floor)}</td><td>${esc(r.actual)}</td><td><span class="pill p-${r.light}">${r.light === "none" && r.actual && r.actual !== "—" ? "不判灯" : LIGHT[r.light]}</span></td></tr>`).join("");
  const bosses = t.bosses.map((b, i) => `<div class="bossrow"><button type="button" class="boss ${b.main ? "" : "alt"}" data-act="boss" data-id="${b.goal_id}">
      <div class="name"><b style="font-size:13px">${b.main ? "★ " : ""}${esc(b.name)}${b.main ? "（主副本）" : ""}</b><span class="muted" style="font-size:11px">${esc(b.line)}</span></div>
      <div class="bar"><i style="width:${Math.round(100 * (b.frac || 0))}%"></i></div>
      <div class="gold" style="font-size:11px">${b.next ? "下一步：" + esc(b.next) : ""} ▸</div></button>${i > 0 ? `<button type="button" class="up" data-act="goal-up" data-id="${b.goal_id}" aria-label="${esc(b.name)}往前挪" title="排到前面">↑ 提前</button>` : ""}</div>`).join("") || '<p class="muted" style="font-size:12px">这个赛季没有副本，只看健康线。</p>';
  const habits = t.habits.map((h) => h.kind === "daily"
    ? `<button type="button" class="chip ${h.done ? "on" : ""}" data-act="habit" data-id="${h.id}" data-done="${h.done ? 1 : ""}">${h.done ? "✓ " : ""}${esc(short(h.title))}</button>`
    : `<button type="button" class="chip ${h.count >= h.target ? "on" : ""}" data-act="habit" data-id="${h.id}" data-done="${h.today ? 1 : ""}">${esc(short(h.title))} ${h.count}/${h.target}</button>`).join("");
  const tasks = t.tasks.map((x) => `<div class="item"><button type="button" class="tick" data-act="task" data-id="${x.id}" data-done="" aria-label="完成：${esc(x.title)}"></button><span class="grow">${esc(x.title)}</span><span class="muted" style="font-size:11px">${x.main ? "主线" : x.due ? esc(x.due) : "支线"}</span></div>`).join("") || '<p class="muted" style="font-size:12px">没有待办</p>';
  const ex = t.explore;
  const money = (v, c) => (c === "GBP" ? "£" : c === "CNY" ? "¥" : c + " ") + Math.abs(v).toFixed(2);
  const bp = t.budget || {}, yuan = (v) => "¥" + Number(v).toLocaleString();
  const extra = t.key === "money" ? `<form class="panel col" data-form="budget"><div class="row" style="justify-content:space-between;flex-wrap:wrap"><h2>每月预算</h2>
        <span class="muted" style="font-size:12px">${bp.now ? `本月 <b style="color:var(--text)">${yuan(bp.now)}</b> · 已锁定` : "还没定"}${bp.next ? ` · ${Number(bp.next_month.slice(5))} 月起 ${yuan(bp.next)}` : ""}</span></div>
      <div class="row" style="gap:8px;align-items:flex-end;flex-wrap:wrap"><label style="flex:1">${bp.now ? `从 ${Number(bp.next_month.slice(5))} 月起改成（人民币）` : "每月一共能花（人民币）"}<input type="text" inputmode="decimal" name="amount" value="${bp.next ?? ""}" placeholder="比如 8000"></label>
        <button class="btn primary">${bp.now ? "定下个月的" : "定下来"}</button></div>
      <span class="muted" style="font-size:11px">按人民币算，英镑按汇率折过来（现在 £1≈¥${(t.fx || 9.5).toFixed(2)}）。这个月定了就不动，想调从下个月起。花得比日子快会变黄，超了变红。</span></form>
    <section class="panel col"><h2>最近的账</h2><div class="scrollbox">${(t.transactions || []).map((x) => `<div class="item"><span class="grow">${esc(x.merchant || "（没认出商户）")}
        <span class="muted" style="display:block;font-size:11px">${esc(x.day.slice(5))} · ${esc({ monzo: "Monzo", cmb: "招行", bocom: "交行", chat: "小伴 记的" }[x.source] || x.source)}${x.category === "refund" ? " · 退款" : x.category === "transfers" ? " · 转账，不算花销" : x.category === "unparsed" ? " · 没认出来，不算钱" : x.category === "ignored" ? " · 你标了不算" : ""}</span></span>
        <b style="color:${x.amount > 0 ? "var(--ok, #6fcf97)" : "inherit"};${x.category === "ignored" ? "text-decoration:line-through;opacity:.5" : ""}">${x.amount > 0 ? "+" : x.amount < 0 ? "−" : ""}${money(x.amount, x.currency)}</b>
        ${x.category === "unparsed" ? "" : `<button type="button" class="btn" data-act="money-ignore" data-id="${x.id}" data-on="${x.category === "ignored" ? "" : 1}">${x.category === "ignored" ? "算回来" : "不算"}</button>`}</div>`).join("") || '<p class="muted" style="font-size:12px">还没有记录</p>'}</div></section>`
    : t.key === "body" ? `<section class="panel col"><h2>体重</h2>${weightChart(t.weights)}<span class="muted" style="font-size:11px">金线是 7 天平均体重，虚线是本赛季阶段目标。</span></section>`
    : t.key === "life" && ex ? `<section class="panel col"><h2>探索地图</h2><div class="row" style="gap:18px;flex-wrap:wrap">
        <span><b class="big2">${ex.countries}</b> <span class="muted">个国家已点亮</span></span><span><b class="big2">${ex.china}</b> <span class="muted">处国内去过</span></span>
        <span><b class="big2">${ex.goals_done}</b><span class="muted"> / ${ex.goals_total} 条人生 Goal 完成，${ex.goals_doing} 条进行中</span></span></div></section>` : "";
  return `<header class="row" style="flex-wrap:wrap;gap:12px">${back}<span class="crest">${tIcon(t.key)}</span>
      <span style="min-width:0"><h2 style="font-size:22px">${esc(t.name)} <span class="pill p-${t.light}" style="font-size:12px;vertical-align:3px">${LIGHT[t.light]}</span></h2>
      <span class="muted" style="font-size:13px">${summary}</span></span></header>
    <div class="cols2">
      <div class="col"><section class="panel col"><div class="row" style="justify-content:space-between;flex-wrap:wrap"><h2>健康线 · 本周</h2><span class="muted" style="font-size:11px">自动计算，不用打卡</span></div>
        <div style="overflow-x:auto"><table class="htbl fixed"><colgroup><col style="width:25%"><col style="width:21%"><col style="width:20%"><col style="width:16%"><col style="width:18%"></colgroup><thead><tr><th>项</th><th>副本标准</th><th>保底</th><th>本周</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></section>${extra}</div>
      <div class="col"><section class="panel col"><h2>副本</h2>${bosses}</section>
        ${habits ? `<section class="panel col"><h2>日常</h2><div class="chips">${habits}</div></section>` : ""}
        <section class="panel col"><h2>待办 <span class="muted" style="font-size:12px;font-weight:400">${t.tasks.length}</span></h2><div class="scrollbox">${tasks}</div></section>
        <section class="panel col"><div class="row" style="justify-content:space-between"><h2>资料</h2><button type="button" class="btn" data-act="brain-terr" data-k="${t.key}">在大脑里看全部 ▸</button></div>
          <div class="scrollbox">${(t.brain || []).map((x) => `<div class="item"><span class="grow">${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>` : esc(x.title)}
            <span class="muted" style="display:block;font-size:11px">${esc(x.kind_label)}${x.source ? " · " + esc(x.source) : ""}</span></span></div>`).join("") || '<p class="muted" style="font-size:12px">这块还没有资料</p>'}</div></section></div></div>`;
}

/* ---------- 战报：领地地图的第二个标签（数字全由代码算，ethan/review.py） ---------- */
function reviewPage() {
  const r = S.review;
  const head = `<header class="row" style="flex-wrap:wrap"><h2 style="font-size:22px">世界</h2>${mapTabs()}</header>`;
  if (!r) return head + '<p class="loading">加载中……</p>';
  const past = r.weeks.slice(1);
  return head + `
    <section class="panel col"><div class="row" style="justify-content:space-between;flex-wrap:wrap"><h2>${esc(r.season.name)} · 赛季结算</h2><span class="muted">剩 ${r.season.days_left} 天</span></div>
      <span class="muted" style="font-size:12px">从 ${esc(r.season.start)} 算起，已过完 ${r.season.weeks} 周 · 赛季累计经验 ${r.season.points}（含本周）· 满勤 ${r.season.full_days} 天</span>
      ${r.season.weeks ? wTally(r.season.territories) : '<span class="muted" style="font-size:12px">第一周还没过完，过完就有结算。本周的战报在“总览”里。</span>'}</section>
    ${past.length ? `<h2 style="font-size:16px">过去几周</h2>${past.map((w) => weekCard(w, false)).join("")}` : ""}
    ${r.months.length > 1 || (r.months[0] && r.months[0].weeks > 1) ? `<section class="panel col"><h2>月度</h2>${r.months.map((m) => `<div class="col" style="gap:4px"><b>${esc(m.month)}</b><span class="muted" style="font-size:12px">${m.weeks} 周 · 经验 ${m.points} · 满勤 ${m.full_days} 天</span>${wTally(m.territories)}</div>`).join("")}</section>` : ""}
    <form class="panel col" data-form="review"><h2>给这周的复盘说一句</h2>
      <span class="muted" style="font-size:12px">哪里不顺、下周想怎么调，随口写一句，小伴 排下周时照着改。</span>
      <textarea name="note" rows="2" placeholder="比如：这周训练太满，下周少排一次" style="background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit"></textarea>
      <div class="row" style="justify-content:flex-end"><button class="btn gold">发给 小伴</button></div></form>`;
}

/* ---------- 设置：提醒、数据连接、系统体检 ---------- */
function settingsPage() {
  const c = S.settings;
  if (!c) return '<p class="loading">加载中……</p>';
  const ok = (x) => `<span class="pill ${x ? "p-ok" : "p-none"}">${x ? "正常" : "没接上"}</span>`;
  return `<header class="row"><h2 style="font-size:22px">设置</h2></header>
    <div class="cols2 fitbody">
      <div class="col">
        <section class="panel col"><h2>提醒</h2>
          <div class="item"><span class="grow">小伴 主动找你<span class="muted" style="display:block;font-size:11px">早报、提醒、想你了来找你</span></span>
            <button type="button" class="btn ${c.prefs.proactive ? "gold" : ""}" data-act="setpro" data-v="${c.prefs.proactive ? "" : "1"}">${c.prefs.proactive ? "开着 · 点一下关掉" : "关着 · 点一下打开"}</button></div>
          <form class="item" data-form="quiet"><span class="grow">免打扰时段<span class="muted" style="display:block;font-size:11px">这段时间他不主动发消息（${esc(c.prefs.timezone)}）</span></span>
            <input name="quiet" value="${esc(c.prefs.quiet)}" style="width:130px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 8px"><button class="btn">保存</button></form>
        </section>
        <section class="panel col fitscroll"><h2>系统体检</h2>${c.health.map((h) => `<div class="item"><span class="grow">${esc(h.name)}<span class="muted" style="display:block;font-size:11px">${esc(h.detail)}</span></span>${ok(h.ok)}</div>`).join("")}</section>
      </div>
      <section class="panel col fitscroll"><h2>数据连接</h2>${c.connections.map((x) => `<div class="item"><span class="grow">${esc(x.name)}<span class="muted" style="display:block;font-size:11px">${esc(x.detail)}${!x.ok && x.fix ? " · 怎么接：" + esc(x.fix) : ""}</span></span>${!x.ok && x.link ? `<a class="btn gold" href="${esc(x.link)}">去授权</a>` : ""}${ok(x.ok)}</div>`).join("")}</section>
    </div>`;
}

/* ---------- 卡册：每天的今日卡都收在这里 ---------- */
function cardsPage() {
  const c = S.cards;
  if (!c) return '<p class="loading">加载中……</p>';
  // 2026-10-29 她要能筛、不同级别分行放：按稀有度从高到低一行一档，上面可按级别、类型、月份筛
  const f = S.cardF || (S.cardF = { r: "", k: "", m: "" });
  const months = [...new Set(c.cards.map((x) => x.day.slice(0, 7)))];
  const chip = (field, v, label) => `<button type="button" class="chip ${f[field] === v ? "on" : ""}" data-act="cardf" data-f="${field}" data-v="${v}">${label}</button>`;
  const shown = c.cards.map((x, i) => ({ ...x, i })).filter((x) => (!f.r || x.rarity === f.r) && (!f.k || x.kind === f.k) && (!f.m || x.day.startsWith(f.m)));
  const one = (x) => `<button type="button" class="ccard ${RAR(x.rarity)}" data-act="cardopen" data-i="${x.i}">
      <img src="${imgUrl(x.image)}" alt="${esc(x.day)} 今日卡：${esc(x.title)}" loading="lazy">
      <span class="cap"><b>${esc(x.title)}</b><span>${esc(x.day.slice(5))} · ${esc(x.rarity)}</span></span></button>`;
  const rows = ["传说", "超稀有", "稀有", "普通"].map((r) => [r, shown.filter((x) => x.rarity === r)]).filter(([, l]) => l.length)
    .map(([r, l]) => `<section class="col" style="gap:8px"><h2 style="font-size:15px">${r} <span class="muted" style="font-weight:400;font-size:12px">${l.length} 张</span></h2><div class="cardgrid">${l.map(one).join("")}</div></section>`).join("");
  return `<header class="row"><h2 style="font-size:22px">卡册</h2><span class="muted">${c.cards.length} 张</span></header>
    ${c.cards.length ? `<div class="col" style="gap:6px"><div class="chips">${chip("r", "", "全部级别")}${["传说", "超稀有", "稀有", "普通"].map((r) => chip("r", r, r)).join("")}</div>
      <div class="chips">${chip("k", "", "全部类型")}${chip("k", "日常", "天气日常")}${chip("k", "训练", "训练日")}${chip("k", "节点", "升级和满勤")}
        ${months.length > 1 ? `<span style="width:8px"></span>${chip("m", "", "全部月份")}${months.map((m) => chip("m", m, m.slice(0, 4) + " 年 " + Number(m.slice(5)) + " 月")).join("")}` : ""}</div></div>` : ""}
    ${rows || `<p class="muted">${c.cards.length ? "没有符合的卡，换个条件试试。" : "卡册还空着。每天翻开今日卡，它就会住进来。"}</p>`}`;
}

/* ---------- 成就：按真实数据算，奖励是解锁 小伴 的新玩法 ---------- */
function achPage() {
  // 2026-10-29 她说全平铺没差异：传说级用视觉区分、奖励单独一个标签页、解锁了要看得出来
  const a = S.ach;
  if (!a) return '<p class="loading">加载中……</p>';
  const r = a.rewards, fresh = r.plays.filter((p) => p.waiting).length;
  const tabs = `<div class="tabs" role="tablist"><button type="button" role="tab" class="${S.achTab !== "reward" ? "on" : ""}" data-act="achtab" data-v="">成就 ${a.got} / ${a.total}</button>
    <button type="button" role="tab" class="${S.achTab === "reward" ? "on" : ""}" data-act="achtab" data-v="reward">🎁 奖励 ${r.unlocked} / ${r.total}${fresh ? ` <span class="newdot">${fresh} 个待揭晓</span>` : ""}</button></div>`;
  const head = `<header class="row" style="flex-wrap:wrap"><h2 style="font-size:22px">成就</h2></header>${tabs}`;
  if (S.achTab === "reward") {
    return head + `<section class="panel col playbox"><span class="muted" style="font-size:13px">拿到一个铜级成就（“第一次”也算），就解开一个铜级奖励，银级、金级一样。解开了 小伴 会在聊天里亲口告诉你，这里也跟着亮。</span>
      <div class="playgrid">${r.plays.map((p) => p.waiting
        ? `<div class="play t-${p.tier} glow"><span class="medal m-${p.tier}">${p.tier}级</span><b>🎁 已解锁，等 小伴 揭晓</b><span class="muted" style="font-size:12px">下次和他聊天，他会亲口告诉你</span></div>`
        : p.unlocked
        ? `<div class="play t-${p.tier} open"><span class="medal m-${p.tier}">${p.tier}级</span><b style="font-size:16px">${esc(p.name)}</b><span style="font-size:13px;line-height:1.6">${esc(p.note)}</span>${p.at ? `<span class="muted" style="font-size:11px;margin-top:auto">${esc(p.at.slice(0, 10))} 解锁</span>` : ""}</div>`
        : `<div class="play locked"><span class="medal m-${p.tier}">${p.tier}级</span><b>？？？</b><span class="muted" style="font-size:12px">拿到一个${p.tier}级成就揭晓</span></div>`).join("")}</div></section>`;
  }
  const level = (i) => i.tier || (i.done ? "达成" : "");
  const tile = (i) => {
    if (i.riddle && !i.done) return `<div class="achtile riddle"><span class="badge" aria-hidden="true">?</span>
      <span class="grow"><b>？？？</b><span class="muted" style="display:block;font-size:12px;font-style:italic">${esc(i.riddle)}</span></span></div>`;
    const lv = level(i), val = i.value == null ? "—" : i.value;
    const prog = i.steps ? (i.source ? `${val}${i.value == null ? "" : i.unit}${i.next == null ? " · 全部达成" : ` / 下一档 ${i.next}${i.unit}`}` : esc(i.hint || "等记录")) : (lv ? "已达成" : esc(i.hint || ""));
    const pips = i.steps ? `<span class="pips">${i.steps.map((s, n) => `<i class="${["铜", "银", "金"].indexOf(i.tier) >= n ? "on m-" + ["铜", "银", "金"][n] : ""}" title="${s}${i.unit}"></i>`).join("")}</span>` : "";
    return `<div class="achtile ${lv ? "got lv-" + lv : ""}"><span class="badge" aria-hidden="true">${lv === "金" || lv === "达成" ? "★" : lv === "银" ? "✦" : lv === "铜" ? "●" : "○"}</span>
      <span class="grow"><b>${esc(i.name)}</b><span class="muted" style="display:block;font-size:11px">${esc(i.desc || "")}</span>
        <span style="display:flex;align-items:center;gap:6px;margin-top:4px;font-size:11px" class="muted">${pips}${prog}</span>
        ${i.steps && i.source && i.next != null ? `<span class="bar" style="display:block;margin-top:4px"><i style="width:${Math.round(100 * (i.frac || 0))}%"></i></span>` : ""}</span></div>`;
  };
  const cats = ["第一次", "连续", "累计", "领域", "作品", "人生 Goal", "隐藏"];
  // 达成度：分档的按“拿到几档 + 到下一档的进度”占总档数的比例，一次性的达成就是 1（2026-10-29 她要每格按达成度降序，不要“快达成了”）
  const degree = (i) => i.steps ? ((["铜", "银", "金"].indexOf(i.tier) + 1) + (i.next != null && i.source ? (i.frac || 0) : 0)) / i.steps.length : (i.done ? 1 : 0);
  return head + `
    <section class="legendhero" aria-label="传说级成就">${a.legend.map((l) => `<div class="legendcard ${l.done ? "got" : ""}"><span class="lcrown" aria-hidden="true">♛</span><b>${esc(l.name)}</b><span>${esc(l.desc)}</span><small>${l.done ? "已达成" : "传说 · 未解锁"}</small></div>`).join("")}</section>
    <div class="achgrid">${cats.map((c) => { const rows = a.items.filter((i) => i.cat === c); const got = rows.filter((i) => i.done || i.tier).length;
      rows.sort((x, y) => degree(y) - degree(x));
      return rows.length ? `<section class="panel col achbox"><div class="row" style="justify-content:space-between;align-items:baseline"><h2>${c}</h2><span class="muted" style="font-size:12px">已达成 <b style="color:var(--gold)">${got}</b> / ${rows.length}</span></div><div class="achlist">${rows.map(tile).join("")}</div></section>` : ""; }).join("")}</div>
    <span class="muted" style="font-size:12px">${esc(a.hidden)}</span>`;
}

/* ---------- 第二大脑：她的资料都在这里（链接、收藏、Grok 情报、学过的、人物卡、心愿） ---------- */
// 2026-10-29 她要 MECE：状态每条只有一个（要你看 / 在用 / 备查 / 整理中，回收站另算），类型按“从哪来”分三组，区域七块加没归类；
// 每个按钮上的数按其他已选条件算，和下面的卡片对得上。大部分 小伴 自己处理，只有“要你看”的要她动手（红点）。
const BRAIN_TABS = [["all", "全部"], ["ask", "要你看"], ["kept", "在用"], ["archived", "备查"], ["new", "整理中"], ["trash", "回收站"]];
const BSTATUS = { ask: "要你看", kept: "在用", archived: "备查", new: "小伴 整理中", trash: "在回收站" };
function brainPage() {
  const b = S.brain, q = S.bq || { tab: "all" };
  if (!b) return '<p class="loading">加载中……</p>';
  const c = b.counts, n = (v) => (v ? ` <span class="cnt">${v}</span>` : ` <span class="cnt">0</span>`);
  const tabs = BRAIN_TABS.map(([k, label]) => `<button type="button" class="chip ${q.tab === k || (!q.tab && k === "all") ? "on" : ""} ${k === "ask" && c.tabs.ask ? "hot" : ""}" data-act="bq" data-f="tab" data-v="${k}">${k === "ask" && c.tabs.ask ? '<i class="reddot"></i>' : ""}${label}${n(c.tabs[k])}</button>`).join("");
  const kchip = (k, label) => `<button type="button" class="chip ${(q.kind || "") === k ? "on" : ""}" data-act="bq" data-f="kind" data-v="${k}">${esc(label)}${n(c.kinds[k])}</button>`;
  const kinds = `<div class="chips">${kchip("", "全部类型")}${b.groups.map(([g, ks]) => `<span class="kgroup">${esc(g)}</span>${ks.map((k) => kchip(k, b.kinds[k])).join("")}`).join("")}</div>`;
  const terrs = `<select data-change="bq-k" aria-label="按区域筛"><option value="">全部区域（${c.terrs[""] || 0}）</option>${b.territories.map((t) => `<option value="${t.key}"${q.k === t.key ? " selected" : ""}>${esc(t.name)}（${c.terrs[t.key] || 0}）</option>`).join("")}<option value="waste"${q.k === "waste" ? " selected" : ""}>还没归类（${c.terrs.waste || 0}）</option></select>`;
  const card = (x) => `<button type="button" class="panel bcard ${x.status === "ask" ? "ask" : ""}" data-act="bopen" data-id="${x.id}">
      <span class="row" style="justify-content:space-between;gap:8px"><span class="row" style="gap:6px">${x.status === "ask" ? '<i class="reddot"></i>' : ""}<span class="pill p-none">${esc(x.kind_label)}</span>${x.fresh ? '<span class="newtag">新</span>' : ""}</span>
        <span class="muted one" style="font-size:11px">${esc(x.source || "")}${x.happened_at ? " · " + esc(String(x.happened_at).slice(0, 10)) : ""}</span></span>
      <b class="clamp2 btitle">${esc(x.title)}</b>
      ${x.status === "ask" && x.extra && x.extra.ask ? `<span class="askline clamp2">要你定：${esc(x.extra.ask)}</span>`
        : `<span class="muted clamp2 bsum">${esc((x.extra && x.extra.ai_summary) || (x.summary !== x.title ? x.summary : "") || "")}</span>`}
      <span class="muted one bfoot" style="font-size:11px">${esc([x.territory_name || "还没归类", BSTATUS[x.status], ...(x.tags || []).slice(0, 3)].filter(Boolean).join(" · "))}</span></button>`;
  const empty = q.tab === "ask" ? "没有要你处理的，小伴 都整理好了。" : q.tab === "new" ? "小伴 都整理完了。你发给他的链接、Grok 每天凌晨整理的收藏会先到这里。" : "这里是空的。";
  return `<header class="row" style="flex-wrap:wrap;gap:10px"><h2 style="font-size:22px">第二大脑</h2>
      <input type="search" class="bsearch" data-input="brain-q" value="${esc(q.q || "")}" placeholder="搜标题、内容、标签……" aria-label="搜索">${terrs}</header>
    <div class="chips">${tabs}</div>${kinds}
    <span class="muted" style="font-size:12px;line-height:1.6">这些都交给 小伴 打理：他写摘要、贴标签、分区域，常用的放<b>在用</b>（聊天、排计划、找新体验会用上），偶尔翻翻的放<b>备查</b>。他拿不准、得你拍板的放<b>要你看</b>，带红点。点开看全文。</span>
    <div class="bgrid">${b.items.map(card).join("") || `<p class="muted">${empty}</p>`}</div>`;
}

// 大脑里一条资料的详情（弹窗）：全文、小伴 的整理、改归属和标签、收下 / 归档 / 回收站
function brainDetail(x) {
  const b = S.brain, q = S.bq || { tab: "all" };
  {  // eslint-disable-line no-lone-blocks
    const place = (x.extra && x.extra.place) || null;
    const acts = q.tab === "trash"
      ? `<button type="button" class="btn" data-act="brain-set" data-id="${x.id}" data-status="kept" data-was="trash">恢复</button>
         <button type="button" class="btn danger" data-act="brain-purge" data-id="${x.id}">${S.purge === x.id ? "再点一次，永久删除" : "永久删除"}</button>`
      : `<span class="muted" style="font-size:11px;margin-right:auto">${BSTATUS[x.status] || ""}</span>
         ${x.status !== "kept" ? `<button type="button" class="btn gold" data-act="brain-set" data-id="${x.id}" data-status="kept" data-was="${x.status}">放进在用</button>` : ""}
         ${x.status !== "archived" ? `<button type="button" class="btn" data-act="brain-set" data-id="${x.id}" data-status="archived" data-was="${x.status}">放进备查</button>` : ""}
         <button type="button" class="x" data-act="brain-set" data-id="${x.id}" data-status="trash" data-was="${x.status}" aria-label="放进回收站">×</button>`;
    return `<section class="sheet small col" aria-label="资料详情" style="gap:10px">
      <div class="row" style="justify-content:space-between;gap:8px"><span class="pill p-none">${esc(x.kind_label)}</span><button type="button" class="btn" data-act="close">关闭</button></div>
      <div class="row" style="justify-content:space-between;gap:8px">
        <span class="muted" style="font-size:11px">${esc(x.source || "")}${x.happened_at ? " · " + esc(String(x.happened_at).slice(0, 10)) : ""}</span></div>
      <b style="font-size:18px;line-height:1.5">${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>` : esc(x.title)}</b>
      ${x.status === "ask" && x.extra && x.extra.ask ? `<span class="askline">小伴 拿不准，要你定：${esc(x.extra.ask)}<br><span class="muted" style="font-size:11px">定了就点下面“放进在用 / 备查”，或者直接在聊天里告诉他。</span></span>` : ""}
      ${x.note ? `<span style="font-size:12px">你当时说：${esc(x.note)}</span>` : ""}
      ${x.extra && x.extra.ai_summary ? `<span style="font-size:13px"><span class="gold">小伴：</span>${esc(x.extra.ai_summary)}</span>` : ""}
      ${x.kind === "knowledge" ? `<div class="prepresult md" style="max-height:none">${md(x.summary)}</div>`
        : x.summary && x.summary !== x.title ? `<span class="muted" style="font-size:13px;line-height:1.7;white-space:pre-wrap">${esc(x.summary)}</span>` : ""}
      ${x.extra && x.extra.topic ? `<span class="chip" style="align-self:flex-start;cursor:default;font-size:11px">专题：${esc(x.extra.topic)}</span>` : ""}
      ${place && place.name ? `<span style="font-size:12px">📍 ${esc([place.name, place.area, place.price, place.hours].filter(Boolean).join(" · "))}</span>` : ""}
      ${x.extra && x.extra.use ? `<span class="gold" style="font-size:12px">可以怎么用：${esc(x.extra.use)}</span>` : ""}
      <div class="row" style="gap:6px;flex-wrap:wrap">
        <select data-change="brain-terr" data-id="${x.id}" aria-label="归到哪个区域"><option value="">还没归类</option>${b.territories.map((t) => `<option value="${t.key}"${x.territory === t.key ? " selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
        <input class="tags btags" data-change="brain-tags" data-id="${x.id}" value="${esc((x.tags || []).join("，"))}" placeholder="加标签，用逗号隔开" aria-label="标签">
      </div>
      <div class="row" style="gap:6px;justify-content:flex-end;align-items:center">${acts}</div></section>`;
  }
}

// 很小的 Markdown：标题、列表、粗体，先转义再排版（小伴 写的成品和知识卡用）
// 长文本（小伴 准备好的东西、知识卡）按 Markdown 排版：表格、标题、列表、引用、代码块（2026-10-29 她说表格直接显示很乱）
function md(text) {
  const inline = (t) => esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines = String(text || "").replace(/\r/g, "").split("\n");
  const out = [];
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  for (let n = 0; n < lines.length; n++) {
    const l = lines[n];
    if (/^\s*```/.test(l)) {  // 代码块
      const body = [];
      for (n++; n < lines.length && !/^\s*```/.test(lines[n]); n++) body.push(lines[n]);
      out.push(`<pre class="mdpre">${esc(body.join("\n"))}</pre>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(l) && n + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[n + 1])) {  // 表格
      const head = cells(l), rows = [];
      for (n += 2; n < lines.length && /^\s*\|.*\|\s*$/.test(lines[n]); n++) rows.push(cells(lines[n]));
      n--;
      out.push(`<div class="mdtable"><table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => `<tr>${head.map((_, k) => `<td>${inline(r[k] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (/^#{1,4}\s/.test(l)) { out.push(`<div class="mdh">${inline(l.replace(/^#{1,4}\s+/, ""))}</div>`); continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(l)) { out.push('<hr class="mdhr">'); continue; }
    if (/^\s*>\s?/.test(l)) { out.push(`<div class="mdquote">${inline(l.replace(/^\s*>\s?/, ""))}</div>`); continue; }
    const li = l.match(/^(\s*)[-*•]\s+(.*)$/);
    if (li) { out.push(`<div class="mdli" style="padding-left:${Math.min(3, Math.floor(li[1].length / 2)) * 14 + 4}px">· ${inline(li[2])}</div>`); continue; }
    const ol = l.match(/^(\s*)(\d+[.、)])\s*(.*)$/);
    if (ol) { out.push(`<div class="mdli" style="padding-left:${Math.min(3, Math.floor(ol[1].length / 2)) * 14 + 4}px">${esc(ol[2])} ${inline(ol[3])}</div>`); continue; }
    out.push(l.trim() ? `<div class="mdp">${inline(l)}</div>` : '<div style="height:6px"></div>');
  }
  return out.join("");
}

/* ---------- 团队页：agent 成绩单和任务看板（docs/design/agent-team.md） ---------- */
function teamPage() {
  const t = S.team;
  if (!t) return '<p class="loading">加载中……</p>';
  const name = (a) => t.names[a] || a;
  const hours = (sec) => (sec >= 3600 ? (sec / 3600).toFixed(1) + " 小时" : Math.round(sec / 60) + " 分钟");
  const board = t.scoreboard.map((r) => `<section class="panel col agent">
      <div class="row" style="justify-content:space-between;align-items:baseline"><h2>${esc(name(r.agent))}</h2>
        <span class="muted" style="font-size:12px">${r.open ? `进行中 ${r.open} 件` : "没有进行中的活"}</span></div>
      <div class="row" style="align-items:baseline;gap:14px"><span><b class="big">${r.efficiency ?? "—"}</b><span class="muted" style="font-size:12px"> 效率分</span></span>
        <span><b style="font-size:18px">${r.output}</b><span class="muted" style="font-size:12px"> 产出分</span></span></div>
      <div class="muted" style="font-size:12px;line-height:1.7">通过 ${r.done} 件 · ${r.size} 点${r.first_pass !== null ? ` · 一次通过 ${r.first_pass}%` : ""}${r.reworks ? ` · 返工 ${r.reworks} 次` : ""}${r.failed ? ` · 失败 ${r.failed} 件` : ""}<br>
        ${r.cost_usd ? `花费 $${r.cost_usd}` : r.tokens ? `用量 ${Math.round(r.tokens / 1000)}k token` : "还没有用量数据"}${r.hours ? ` · 用时 ${r.hours} 小时` : ""}</div></section>`).join("");
  const card = teamCard;
  return teamBody(t, board, card, hours);
}
function teamHours(sec) { return sec >= 3600 ? (sec / 3600).toFixed(1) + " 小时" : Math.round(sec / 60) + " 分钟"; }
function teamCard(x, key) {
  const t = S.team, name = (a) => t.names[a] || a, hours = teamHours;
  const st = { queued: "queued", running: "running", review: "review", accepted: "done", failed: x.status === "dropped" ? "dropped" : "failed" }[key] || "";
  return `<div class="item tcard st-${st}"><i class="pulse" aria-hidden="true"></i>
      <button type="button" class="grow linkish topen" data-act="acard" data-id="${x.id}" aria-label="看这件活的详情"><span class="clamp2">${esc(x.title)}</span>
        <span class="muted one" style="display:block;font-size:11px">${esc(name(x.agent))}${x.project && !x.project.startsWith("Grok 回报") ? " · " + esc(x.project) : ""} · ${x.size} 点${x.reworks ? ` · 返工 ${x.reworks}` : ""}${x.work_seconds ? " · " + hours(x.work_seconds) : ""}</span>
        ${key === "failed" ? `<span class="muted one" style="display:block;font-size:11px">${x.status === "dropped" ? "你叫停的" : "没干成"}${x.review_note ? "：" + esc(x.review_note) : ""}</span>` : ""}</button>
      ${["queued", "running", "review"].includes(key) ? `<button type="button" class="x" data-act="agent-drop" data-id="${x.id}" title="不干了" aria-label="叫停这件活">⊘</button>` : ""}
      ${key === "accepted" || x.rating ? `<span class="row rate stars5" role="group" aria-label="打分 1 到 5">${[1, 2, 3, 4, 5].map((n) => `<button type="button" class="x ${(x.rating || 0) >= n ? "on" : ""}" data-act="rate" data-id="${x.id}" data-rating="${x.rating === n ? 0 : n}" aria-label="${n} 分">★</button>`).join("")}</span>` : ""}</div>`;
}
function teamBody(t, board, card) {
  // 2026-10-29：系统外也在干的活一起放进看板（Claude 会话、准备员算 小伴 的、小伴 派给 Grok 还没回的），卡片按状态带动效
  const name = (a) => t.names[a] || a;
  // 2026-10-29 她说只有 Grok 的卡能点开：所有卡统一一个样子，都能点开看详情（准备员的打开那一件）
  const liveCard = (x) => `<div class="item tcard st-${x.state}"><i class="pulse" aria-hidden="true"></i>
      <button type="button" class="grow linkish topen" ${x.prep ? `data-act="prepgo" data-id="${x.prep}"` : `data-act="alive" data-i="${(t.live || []).indexOf(x)}"`} aria-label="看这件活的详情"><span class="clamp2">${esc(x.title)}</span>
        <span class="muted one" style="display:block;font-size:11px">${esc(name(x.agent))} · ${esc(x.sub)}${x.at ? " · " + ago(x.at) : ""}</span></button></div>`;
  const LIVE_COL = { queued: "queued", running: "running", review: "review" };
  const cols = t.columns.map((c) => {
    const extra = (t.live || []).filter((x) => LIVE_COL[x.state] === c.key);
    const shown = extra.slice(0, 12);
    return `<section class="panel col tcol col-${c.key}"><div class="row" style="justify-content:space-between"><h2>${esc(c.label)}</h2><span class="muted">${c.tasks.length + extra.length}</span></div>
      <div class="tlist">${c.tasks.map((x) => card(x, c.key)).join("")}${shown.map(liveCard).join("")}${extra.length > shown.length ? `<span class="muted" style="font-size:11px">还有 ${extra.length - shown.length} 件</span>` : ""}
      ${c.tasks.length + extra.length ? "" : '<p class="muted" style="font-size:12px">暂无</p>'}</div></section>`; }).join("");
  return `<header class="row" style="flex-wrap:wrap"><h2 style="font-size:22px">团队</h2><span class="muted">最近 7 天 · 效率分按同样大小的活比，满分 100</span>
      <button type="button" class="btn" style="margin-left:auto" data-act="tarchive">归档 ${(t.archive || []).length}</button></header>
    <div class="grid4">${board}</div>
    <span class="muted" style="font-size:12px">效率分 = 质量 40%（返工一次减半）+ 成本 20% + 速度 20%（都和同样大小的活比）+ 你的打分 20%。通过的活打个 1–5 分（3 分是一般），打完自动进右上角“归档”，7 天没打就当 3 分。失败是系统判的（报错、验收没过、超时），你叫停的算“放弃”，不扣分。</span>
    <div class="grid5">${cols}</div>`;
}

function ago(iso) {
  const m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
  return m < 1 ? "刚刚" : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.round(m / 60)} 小时前` : `${Math.round(m / 1440)} 天前`;
}


// 训练详情：每个动作一张卡，左边小人循环演示（moves.js），金色是发力部位（2026-10-28 她要）
function trainView(t) {
  const reps = (e) => (e.reps[0] !== e.reps[1] ? `${e.reps[0]}–${e.reps[1]}` : `${e.reps[0]}`) + " " + e.unit + (e.per_side ? "（每侧）" : "");
  return `<div class="row" style="gap:8px;flex-wrap:wrap"><b style="font-size:15px">${esc(t.focus)}</b>${t.trip ? `<span class="muted" style="font-size:12px">${esc(t.trip)}</span>` : ""}</div>
    <section class="meal"><b class="gold" style="font-size:13px">热身 ${t.warmup.minutes} 分钟</b><ol class="steps">${t.warmup.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ol></section>
    ${t.exercises.map((e, n) => `<section class="meal move-card">
      <svg class="move" data-move="${esc(e.id)}" viewBox="0 0 200 160" role="img" aria-label="${esc(e.name)}动作演示">${typeof figureSVG === "function" ? figureSVG(e.id, 0) : ""}</svg>
      <div class="col" style="gap:6px;min-width:0">
        <b style="font-size:15px">${n + 1}. ${esc(e.name)}</b>
        <span class="row" style="gap:6px;flex-wrap:wrap"><span class="pill p-none">${e.sets} 组 × ${reps(e)}</span>${e.weight ? `<span class="pill p-none">${e.weight} kg</span>` : ""}${e.muscles ? `<span class="muted" style="font-size:12px">练${esc(e.muscles)}</span>` : ""}</span>
        <ul class="steps cues">${e.cues.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
        <span style="font-size:12px;color:var(--warn)">别：${esc(e.mistake)}</span>
        ${e.note ? `<span class="muted" style="font-size:12px">${esc(e.note)}</span>` : ""}</div></section>`).join("")}
    <span class="muted" style="font-size:12px">疼痛超过 3 分（满分 10）就停这个动作。练完把每组重量和次数告诉 小伴。</span>`;
}

function mealsView(m) {
  return `<div class="row" style="gap:8px;flex-wrap:wrap"><span class="pill p-none">约 ${m.kcal} 千卡</span><span class="pill p-none">蛋白质 ${m.protein} 克</span></div>
    ${m.note ? `<p class="muted" style="font-size:13px;margin:0">${esc(m.note)}</p>` : ""}
    ${m.meals.map((x) => `<section class="meal"><div class="row" style="justify-content:space-between;align-items:baseline;flex-wrap:wrap">
        <b style="font-size:15px"><span class="gold">${esc(x.meal)}</span>　${esc(x.name)}</b>
        <span class="muted" style="font-size:12px">${x.kcal ? x.kcal + " 千卡" : ""}${x.protein ? " · 蛋白质 " + x.protein + " 克" : ""}${x.minutes ? " · " + x.minutes + " 分钟" : ""}</span></div>
      ${x.items.length ? `<div class="chips">${x.items.map((i) => `<span class="chip" style="cursor:default">${esc(i)}</span>`).join("")}</div>` : ""}
      ${x.steps.length ? `<ol class="steps">${x.steps.map((i) => `<li>${esc(i)}</li>`).join("")}</ol>` : ""}</section>`).join("")}`;
}

/* ---------- 弹层 ---------- */
// 同一个弹窗重画时（拖完顺序、点完按钮后刷新）保持原来滚到的位置，不跳回顶部（2026-10-29 她说）
function renderOverlay() {
  const box = $("#overlay"), old = box.querySelector(".sheet"), same = S._ovShown === S.overlay;
  const top = same && old ? old.scrollTop : 0;
  renderOverlayInner();
  S._ovShown = S.overlay;
  const now = box.querySelector(".sheet");
  if (now && top) now.scrollTop = top;
}
function renderOverlayInner() {
  const box = $("#overlay"), st = S.state;
  if (!S.overlay) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  if (S.overlay === "card") {
    const c = st.card;
    box.innerHTML = `<button type="button" class="bigcard" data-act="close" aria-label="收起今日卡">
      <span class="img ${RAR(c.rarity)}"><img src="${imgUrl(c.image)}" alt="今日卡：${esc(c.title)}"></span>
      <span class="txt"><span class="muted" style="font-size:13px">${esc(c.day)} · 今日卡</span><span style="font-size:28px;font-weight:700">${esc(c.title)}</span>
        <span style="align-self:flex-start;font-size:13px;padding:3px 12px;border-radius:12px;border:1px solid #E2BE6A;color:#E2BE6A">${esc(c.rarity)}</span>
        <span style="font-size:14px;line-height:1.7">${esc(c.reason)}</span><span style="font-size:14px;line-height:1.7;color:#E2BE6A">小伴：${esc(c.line)}</span>
        <span style="font-size:12px;color:#ABA8CC">点任意处收起</span></span></button>`;
  } else if (S.overlay === "plan") {
    const pl = st.planner;
    box.innerHTML = `<section class="sheet" aria-label="计划"><div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap"><h2 style="font-size:18px">计划 · ${esc(st.day.slice(5))} 周${st.weekday}</h2>
        <span class="row" style="gap:8px"><button type="button" class="btn gold" data-act="replan" ${pl.replanning ? "disabled" : ""}>${pl.replanning ? "小伴 在重排……" : "✨ 让 AI 重排"}</button><button type="button" class="btn" data-act="close">关闭</button></span></div>
      <div class="plancols">
        <section class="col"><div class="row" style="justify-content:space-between"><h2>待办</h2><span class="muted" style="font-size:11px">★ = 今日任务，不限件数</span></div>
          ${pl.todos.map((t) => `<div class="item"><button type="button" class="star ${t.star ? "on" : ""}" data-act="star" data-id="${t.id}" data-on="${t.star ? "" : 1}" aria-label="设为或取消今日任务">★</button>
            <span class="grow"><span style="display:block">${esc(t.title)}</span><span class="muted" style="font-size:11px">${esc(t.sub)}${t.due ? " · " + esc(t.due.slice(5)) : ""}</span></span>
            <button type="button" class="x" data-act="rename" data-id="${t.id}" data-title="${esc(t.title)}" aria-label="改名字">✎</button><button type="button" class="btn" data-act="postpone" data-id="${t.id}">推到明天</button><button type="button" class="x" data-act="deltask" data-id="${t.id}" aria-label="删除">×</button></div>`).join("") || '<p class="muted">没有待办</p>'}
          ${(pl.skipped || []).length ? `<div class="col" style="gap:4px;margin-top:6px"><b style="font-size:13px">今天没排进</b>${pl.skipped.map((x) => `<span style="font-size:12px">· ${esc(x.title)}<span class="muted">：${esc(x.why)}</span></span>`).join("")}</div>` : ""}
          <button type="button" class="btn gold" data-act="addtodo">＋ 加待办</button>
        </section>
        <section class="col"><div class="row" style="justify-content:space-between"><h2>日程</h2><span class="muted" style="font-size:11px">固定时间和地点，会同步到苹果日历</span></div>
          ${pl.events.map((e) => `<div class="item"><label style="font-size:10px">开始<input type="time" value="${esc(e.start)}" data-change="time" data-id="${e.id}"></label>
            <span class="grow"><span style="display:block">${esc(e.title)}</span><span class="muted" style="font-size:11px">${esc(e.place)}${e.leave ? " · " + esc(e.leave) + " 出发" : ""}</span></span>
            <button type="button" class="x" data-act="delevent" data-id="${e.id}" aria-label="删除这个日程">×</button></div>`).join("") || '<p class="muted">今天没有要出门的事</p>'}
          <button type="button" class="btn gold" data-act="addevent">＋ 加日程</button>
        </section></div>
      <span class="muted" style="font-size:12px">改了马上生效，小伴 也会知道；懒得点就直接跟他说“把训练挪到 17:00”。</span></section>`;
  } else if (S.overlay === "cardview") {
    const c = S.cardView;
    box.innerHTML = `<button type="button" class="bigcard" data-act="close" aria-label="收起">
      <span class="img ${RAR(c.rarity)}"><img src="${imgUrl(c.image)}" alt="今日卡：${esc(c.title)}"></span>
      <span class="txt"><span class="muted" style="font-size:13px">${esc(c.day)} · 今日卡</span><span style="font-size:28px;font-weight:700">${esc(c.title)}</span>
        <span style="align-self:flex-start;font-size:13px;padding:3px 12px;border-radius:12px;border:1px solid #E2BE6A;color:#E2BE6A">${esc(c.rarity)}</span>
        <span style="font-size:14px;line-height:1.7">${esc(c.reason)}</span><span style="font-size:14px;line-height:1.7;color:#E2BE6A">小伴：${esc(c.line)}</span>
        <span style="font-size:12px;color:#ABA8CC">点任意处收起</span></span></button>`;
  } else if (S.overlay === "celebrate") {
    const all = S.celebrating || [];
    const list = all.filter((x) => !x.play && !x.shop), plays = all.filter((x) => x.play), shops = all.filter((x) => x.shop);  // 成就和揭晓的奖励（2026-10-29）分开写
    const top = list[list.length - 1] || {};
    const gift = list.reduce((n, x) => n + (x.gift || 0), 0);
    const TIERWORD = { 达成: "达成", 铜: "铜级", 银: "银级", 金: "金级" };
    box.innerHTML = `<button type="button" class="celebrate ${list.length ? "" : "reward"}" data-act="celebrate-done" aria-label="${list.length ? "成就达成" : "新奖励揭晓"}，点一下收起">
      <span class="burst" aria-hidden="true">${Array.from({ length: 18 }, (_, i) => `<i style="--a:${i * 20}deg;--d:${(i % 3) * 0.12}s"></i>`).join("")}</span>
      <span class="trophy" aria-hidden="true">${list.length ? "🏆" : "🎁"}</span>
      ${list.length ? `<span class="muted" style="font-size:13px;letter-spacing:.2em">${top.hidden ? "隐藏成就解锁" : "成就达成"}</span>` : ""}
      ${list.map((x) => `<span class="cname">${esc(x.name)} <span class="medal m-${x.tier === "达成" ? "金" : x.tier}">${TIERWORD[x.tier] || x.tier}</span></span>`).join("")}
      ${shops.length ? `<span class="muted" style="font-size:13px;letter-spacing:.2em;margin-top:${list.length ? 10 : 0}px">商店</span>${shops.map((x) => `<span class="cname">🎁 ${esc(x.name)}</span><span class="btn gold" role="button" data-act="redeemnow" data-id="${x.reward}" data-key="${esc(x.key)}">现在拆开</span>`).join("")}<span class="muted" style="font-size:12px">不想现在拆，点别处收起，之后在“任务 → 商店”里拆</span>` : ""}
      ${plays.length ? `<span class="muted" style="font-size:13px;letter-spacing:.2em;margin-top:${list.length ? 10 : 0}px">新奖励揭晓</span>${plays.map((x) => `<span class="cname">${esc(x.name)} <span class="medal m-${x.tier}">${x.tier}级奖励</span></span><span style="font-size:14px;line-height:1.6;color:#E2BE6A">小伴：${esc(x.note)}</span>`).join("")}` : ""}
      ${gift ? `<span class="gold" style="font-size:14px">🎁 还解锁了一个惊喜，去找 小伴 聊聊</span>` : ""}
      <span class="muted" style="font-size:12px">点任意处收起</span></button>`;
  } else if (S.overlay === "brain") {
    const x = S.brain && S.brain.items.find((i) => i.id === S.brainOpen);
    if (!x) { S.overlay = null; box.hidden = true; box.innerHTML = ""; return; }
    box.innerHTML = brainDetail(x);
  } else if (S.overlay === "tarchive") {
    const t = S.team;
    box.innerHTML = `<section class="sheet small col" aria-label="团队归档"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">归档的活</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <span class="muted" style="font-size:12px">打过分的活，和一周以前没干成的。想改分数直接再点星星。</span>
      <div class="col" style="gap:8px">${(t && t.archive || []).map((x) => teamCard(x, x.status)).join("") || '<p class="muted">还没有归档的活</p>'}</div></section>`;
  } else if (S.overlay === "detail") {
    box.innerHTML = `<section class="sheet small" aria-label="${S.detailK === "train" ? "训练详情" : "三餐"}"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">${S.detailK === "train" ? "今天的训练" : "今天的三餐"}</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      ${S.detailK === "meals" && st.meals && st.meals.meals.length ? mealsView(st.meals) : S.detailK === "train" && st.training ? trainView(st.training) : `<div style="white-space:pre-wrap;line-height:1.7;font-size:14px">${esc(st.detail[S.detailK] || "")}</div>`}</section>`;
  } else if (S.overlay === "prep") {
    box.innerHTML = `<section class="sheet small" aria-label="小伴 在准备的事"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">小伴 在替你准备</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <span class="muted" style="font-size:12px">这些是 小伴 替你跑的一次性的事。点亮 ☆ 的，他今天一定做完、最先做，拖动也排不到没星的后面；按住 ⋮⋮ 拖着排先后，他每天开工前会参考，再看截止日和你最近着急的事定先做哪件，要上网查的交给 Grok。做好了他会在聊天里跟你说，你也能在这看全文、通过或写意见；做没做完你自己点。要花钱、要预订的，他只准备到等你拍板。</span>
      ${(st.prep_today || []).length ? `<div class="preptoday"><b style="font-size:13px">今天 小伴 打算先做</b>${st.prep_today.map((o, n) => { const t = st.prep.find((x) => x.id === o.id); return t ? `<span style="font-size:12px">${n + 1}. ${esc(t.title)}<span class="muted">${o.why ? "：" + esc(o.why) : ""}</span></span>` : ""; }).join("")}</div>` : ""}
      <div class="preplist">${st.prep.map((t) => `<div class="prepitem ${S.prepOpen === t.id ? "open" : ""} ${t.state === "ready" ? "ready" : ""}" data-pid="${t.id}">
        <div class="row" style="gap:8px"><span class="drag" data-drag aria-label="拖动排序" title="按住拖动">⋮⋮</span><button type="button" class="x pstar ${t.star ? "on" : ""}" data-act="prepstar" data-id="${t.id}" data-on="${t.star ? "" : 1}" aria-label="${t.star ? "取消星星" : "点星星：今天必须做完"}" title="今天必须做完">${t.star ? "★" : "☆"}</button><button type="button" class="grow linkish" data-act="prepopen" data-id="${t.id}">${esc(t.title)}${t.summary ? `<span class="muted" style="display:block;font-size:11px">${esc(t.summary)}</span>` : ""}</button>
          <span class="pill ${t.state === "ready" ? "p-warn" : t.state === "approved" ? "p-ok" : "p-none"}">${esc(t.state_label)}</span></div>
        ${S.prepOpen === t.id ? `<div class="col" style="gap:8px;margin-top:8px">
          ${t.result ? `<div class="prepresult md">${md(t.result)}</div>` : `<span class="muted" style="font-size:12px">${t.state === "waiting_grok" ? "Grok 正在查，查回来 小伴 再整理。" : "还在排队，轮到就做。"}</span>`}
          ${t.decision ? `<span class="gold" style="font-size:13px">要你拍板：${esc(t.decision)}</span>` : ""}
          <div class="row" style="gap:6px;flex-wrap:wrap">
            ${t.state === "ready" ? `<button type="button" class="btn gold" data-act="prepok" data-id="${t.id}">通过</button>`
              : `<button type="button" class="btn" data-act="prepdone" data-id="${t.id}" title="他在聊天里已经做完了，就点这个">已经做完了</button>`}
            ${t.result ? `<input class="tags" style="flex:1;min-width:160px" data-prepnote="${t.id}" placeholder="要改哪里，写一句"><button type="button" class="btn" data-act="prepfix" data-id="${t.id}">让他改</button>` : ""}
            <button type="button" class="btn" data-act="dropprep" data-id="${t.id}">不要了</button></div></div>` : ""}</div>`).join("") || '<p class="muted">现在没有</p>'}</div>
      ${(st.prep_routines || []).length ? `<details class="preptrash"><summary class="muted" style="cursor:pointer;font-size:13px">🔁 小伴 的长期工作 ${st.prep_routines.length} 项（一直在做，不算在上面的清单里）</summary>
        ${st.prep_routines.map((t) => `<div class="item"><span class="grow" style="font-size:13px">${esc(t)}</span></div>`).join("")}</details>` : ""}
      ${(st.prep_trash || []).length ? `<details class="preptrash"><summary class="muted" style="cursor:pointer;font-size:13px">🗑 回收站 ${st.prep_trash.length} 件（点“不要了”的都在这，30 天内能放回）</summary>
        ${st.prep_trash.map((x) => `<div class="item"><span class="grow">${esc(x.title)}<span class="muted" style="display:block;font-size:11px">${esc(String(x.at).slice(5, 16).replace("T", " "))} 放进来的</span></span><button type="button" class="btn" data-act="preprestore" data-id="${x.id}">放回去</button></div>`).join("")}</details>` : ""}</section>`;
  } else if (S.overlay === "addtodo") {
    const goals = st.goals || [];
    box.innerHTML = `<form class="sheet small" data-form="addtodo" aria-label="加待办"><h2 style="font-size:18px">加一件待办</h2>
      <label>要做什么<input type="text" name="title" required></label>
      <label>放在哪<select name="goal_id"><option value="">支线（默认）</option>${goals.map((g) => `<option value="${g.id}" ${String(g.id) === S.goal ? "selected" : ""}>挂到副本：${esc(g.name)}</option>`).join("")}</select></label>
      <label>属于哪个区域<select name="territory"><option value="">让 小伴 猜（默认）</option>${TERRS.map(([k, n]) => `<option value="${k}">${n}</option>`).join("")}<option value="waste">先放荒原</option></select></label>
      <label>什么时候做<select name="when"><option value="today">今天（进今日任务 ★）</option><option value="week">这周</option><option value="later">不急，让 AI 排</option></select></label>
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-act="close">取消</button><button class="btn primary">加上</button></div></form>`;
  } else if (S.overlay === "addevent") {
    box.innerHTML = `<form class="sheet small" data-form="event" aria-label="加日程"><h2 style="font-size:18px">加一个日程</h2>
      <label>什么事<input type="text" name="title" required></label>
      <div class="row" style="align-items:flex-end"><label style="flex:1">开始<input type="time" name="start" required></label><label style="flex:1">结束（可不填，默认 1 小时）<input type="time" name="end"></label></div>
      <label>地点（可不填）<input type="text" name="place"></label>
      <span class="muted" style="font-size:12px">今天的日程，会同步到苹果日历。</span>
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-act="close">取消</button><button class="btn primary">加上</button></div></form>`;
  } else if (S.overlay.startsWith("boss:")) {
    const b = st.bosses.find((x) => String(x.goal_id) === S.overlay.split(":")[1]);
    const ch = S.quests && S.quests.chapters.find((c) => String(c.goal_id) === S.overlay.split(":")[1]);
    if (!b) { S.overlay = null; box.hidden = true; return; }
    const steps = ch ? ch.steps : null;
    const doneN = steps ? steps.filter((x) => x.state === "done").length : 0;
    box.innerHTML = `<section class="sheet small" aria-label="${esc(b.name)}"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">${b.main ? "★ " : ""}${esc(b.name)}${b.main ? "（主副本）" : ""}</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <div class="row muted" style="font-size:13px;flex-wrap:wrap"><span>${esc(b.line)}</span>${b.status ? `<span class="gold">· ${esc(b.status)}</span>` : ""}</div>
      <div class="bar" style="height:10px"><i style="width:${Math.round(100 * (b.frac || 0))}%"></i></div>
      ${b.comment ? `<span style="font-size:13px;line-height:1.6"><span class="gold">今日战况：</span>${esc(b.comment)}</span>` : ""}
      <details><summary class="muted" style="font-size:12px;cursor:pointer">改这个副本（名字、截止日${b.target != null ? "、目标值" : ""}）</summary>
        <form class="col" data-form="goalset" data-id="${b.goal_id}" style="gap:8px;margin-top:8px">
          <label>名字<input type="text" name="title" value="${esc(b.title || b.name)}"></label>
          <div class="row" style="gap:8px;align-items:flex-end"><label style="flex:1">截止日<input type="date" name="due" value="${esc(b.due || "")}"></label>
          ${b.target != null ? `<label style="flex:1">目标值${b.unit ? "（" + esc(b.unit) + "）" : ""}<input type="text" inputmode="decimal" name="target" value="${esc(String(b.target))}"></label>` : ""}</div>
          <div class="row" style="justify-content:flex-end"><button class="btn primary">保存</button></div></form></details>
      ${steps === null ? '<p class="muted">加载中……</p>' : `<b style="font-size:13px">主线 ${doneN} / ${steps.length} 步</b>
      ${steps.map((x) => `<button type="button" class="step ${x.state}" data-act="task" data-id="${x.id}" data-done="${x.state === "done" ? 1 : ""}" data-points="${x.points}">
        <span class="gold" style="width:18px;text-align:center">${x.state === "done" ? "✓" : x.state === "now" ? "▶" : "○"}</span>
        <span style="flex:1">${esc(x.title)}${x.hard ? ' <span class="gold" style="font-size:11px">硬骨头</span>' : ""}${x.milestone ? ' <span class="gold" style="font-size:11px">节点</span>' : ""}</span>
        <span class="gold" style="font-size:11px">+${x.points}</span></button>`).join("") || '<p class="muted">还没有拆主线。</p>'}
`}
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn gold" data-act="addtodo" data-goal="${b.goal_id}">＋ 给这个副本加一步</button></div></section>`;
  } else if (S.overlay === "ethanday") {
    const d = S.ethanDays;
    box.innerHTML = `<section class="sheet small col" aria-label="小伴 的一天"><div class="row" style="justify-content:space-between"><h2>小伴 的一天</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <span class="muted" style="font-size:12px">这是他自己的日子，每天早上写好，聊天时照着过。是他的，不是你的资料，所以不放进第二大脑。</span>
      ${!d ? '<p class="loading">加载中……</p>' : d.days.length ? d.days.map((x, i) => `<details ${i === 0 ? "open" : ""}><summary class="gold" style="cursor:pointer">${esc(x.day)}${i === 0 ? " · 今天" : ""}</summary><div class="prepresult md" style="max-height:none">${md(x.body)}</div></details>`).join("") : '<p class="muted">他今天的日子还没写好，晚点再来看。</p>'}</section>`;
  } else if (S.overlay === "trip") {
    const x = (st.timeline[S.tripI] || {}).trip || {};
    const map = x.place ? `https://maps.apple.com/?daddr=${encodeURIComponent(x.place)}&dirflg=r` : "";
    box.innerHTML = `<section class="sheet small col" aria-label="出门"><div class="row" style="justify-content:space-between"><h2>${esc(x.title || "出门")}</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <div class="col" style="gap:8px;font-size:14px;line-height:1.7">
        <span>📍 ${x.place ? esc(x.place) : '<span class="muted">日历里没写地点</span>'}</span>
        <span>🕐 ${esc(x.start || "")} 开始</span>
        ${x.trip_min ? `<span>🚌 路上约 ${x.trip_min} 分钟${x.route ? "（" + esc(x.route) + "）" : ""}</span>` : ""}
        ${x.leave ? `<span class="gold">🚶 ${esc(x.leave)} 出发（开始时间 − 路上 − 20 分钟）</span>` : ""}
        ${x.remind ? `<span class="muted">⏰ ${esc(x.remind)} 小伴 提醒你准备</span>` : ""}</div>
      ${map ? `<a class="btn gold" style="align-self:flex-start" href="${map}" target="_blank" rel="noopener">在地图里看路线</a>` : ""}</section>`;
  } else if (S.overlay === "bigmap") {
    box.innerHTML = `<section class="sheet wide col" aria-label="大地图"><div class="row" style="justify-content:space-between"><h2>大地图</h2><span class="row" style="gap:10px"><span class="legend"><span><i class="ldot s-ok"></i>达标</span><span><i class="ldot s-warn"></i>副本落后</span><span><i class="ldot s-bad"></i>跌破保底</span><span><i class="ldot s-none"></i>等数据</span></span><button type="button" class="btn" data-act="close">关闭</button></span></div>
      <div class="mapbox">${mapImg()}${S.map ? mapTags(S.map) : ""}</div><span class="muted" style="font-size:12px">点一块区域进去看详情。</span></section>`;
  } else if (S.overlay === "redeemed") {
    box.innerHTML = `<section class="sheet small quip"><svg class="giftopen" viewBox="0 0 120 110" aria-hidden="true">
        <g class="glow"><circle cx="60" cy="52" r="34" fill="url(#gg)"/></g>
        <defs><radialGradient id="gg"><stop offset="0" stop-color="#FFE7A3" stop-opacity=".95"/><stop offset="1" stop-color="#FFE7A3" stop-opacity="0"/></radialGradient></defs>
        <g class="spark" fill="#F3C24E"><path d="M60 14l2 6 6 2-6 2-2 6-2-6-6-2 6-2z"/><path d="M86 26l1.4 4 4 1.4-4 1.4-1.4 4-1.4-4-4-1.4 4-1.4z"/><path d="M36 28l1.2 3.4 3.4 1.2-3.4 1.2-1.2 3.4-1.2-3.4-3.4-1.2 3.4-1.2z"/></g>
        <rect x="26" y="58" width="68" height="44" rx="5" fill="#E7577F"/><rect x="55" y="58" width="10" height="44" fill="#F6D06B"/>
        <rect x="26" y="58" width="68" height="7" fill="#C83D66" opacity=".55"/>
        <g class="lid"><rect x="20" y="44" width="80" height="16" rx="4" fill="#F06C92"/><rect x="55" y="44" width="10" height="16" fill="#F6D06B"/>
          <path d="M60 44c-10-14-24-10-18-2 3 4 12 3 18 2zm0 0c10-14 24-10 18-2-3 4-12 3-18 2z" fill="#F6D06B"/></g></svg>
      <h2 style="font-size:18px">拆开了！</h2>
      <p>小伴 已经收到啦，正在给你准备——每次都不一样。<br>他会主动来聊天里找你揭晓、告诉你接下来怎么玩，一般就在今天；要花你钱的，他会先问你。</p>
      <span class="muted" style="font-size:12px">进度在“任务 → 商店 → 兑换记录”里能看到。</span>
      <button type="button" class="btn gold" data-act="close">等他来找我</button></section>`;
  } else if (S.overlay.startsWith("acard:")) {  // 团队页点开一件活看清楚是什么（2026-10-29 她说卡片看不出要评的是哪件）
    const t = S.team || {}, id = Number(S.overlay.split(":")[1]);
    const x = [...(t.columns || []).flatMap((c) => c.tasks), ...(t.archive || [])].find((y) => y.id === id);
    if (!x) { S.overlay = null; box.hidden = true; return; }
    const name = (t.names || {})[x.agent] || x.agent;
    const ST = { queued: "排队", running: "在干", review: "等审查", accepted: x.rating ? `已打分 ${x.rating} 分` : "通过 · 待打分", failed: "没干成", dropped: "你叫停的" };
    const when = (v) => (v ? String(v).slice(5, 16).replace("T", " ") : "");
    box.innerHTML = `<section class="sheet small col" aria-label="活的详情" style="gap:10px">
      <div class="row" style="justify-content:space-between;gap:8px"><span class="pill p-none">${esc(name)} · ${esc(ST[x.status] || x.status)}</span><button type="button" class="btn" data-act="close">关闭</button></div>
      <b style="font-size:17px;line-height:1.5">${esc(x.title)}</b>
      <span class="muted" style="font-size:12px">${[x.project && !x.project.startsWith("Grok 回报") ? x.project : "", `${x.size} 点`, x.created_at ? "派出 " + when(x.created_at) : "", x.finished_at ? "做完 " + when(x.finished_at) : "", x.reworks ? `返工 ${x.reworks} 次` : ""].filter(Boolean).map(esc).join(" · ")}</span>
      ${x.spec ? `<div class="col" style="gap:4px"><b style="font-size:13px">要做什么</b><div class="prepresult md">${md(x.spec)}</div></div>` : ""}
      ${x.review_note ? `<div class="col" style="gap:4px"><b style="font-size:13px">${x.agent === "grok" ? "Grok 回报的原文" : x.status === "failed" || x.status === "dropped" ? "为什么没成" : "交上来的说明"}</b><div class="prepresult md">${md(x.review_note)}</div></div>` : ""}
      ${!x.spec && !x.review_note ? '<span class="muted" style="font-size:13px">这件活没留下更多说明。</span>' : ""}
      ${x.status === "accepted" ? `<div class="row" style="gap:8px;align-items:center"><span style="font-size:13px">给它打个分</span><span class="row rate stars5 big" role="group" aria-label="打分 1 到 5">${[1, 2, 3, 4, 5].map((n) => `<button type="button" class="x ${(x.rating || 0) >= n ? "on" : ""}" data-act="rate" data-id="${x.id}" data-rating="${x.rating === n ? 0 : n}" aria-label="${n} 分">★</button>`).join("")}</span><span class="muted" style="font-size:11px">3 分是一般</span></div>` : ""}
    </section>`;
  } else if (S.overlay.startsWith("alive:")) {
    const t = S.team || {}, x = (t.live || [])[Number(S.overlay.split(":")[1])];
    if (!x) { S.overlay = null; box.hidden = true; return; }
    const ST = { running: "在干", review: "等你看", queued: "排队" };
    box.innerHTML = `<section class="sheet small col" aria-label="活的详情" style="gap:10px">
      <div class="row" style="justify-content:space-between;gap:8px"><span class="pill p-none">${esc((t.names || {})[x.agent] || x.agent)} · ${esc(x.status_label || ST[x.state] || "")}</span><button type="button" class="btn" data-act="close">关闭</button></div>
      <b style="font-size:17px;line-height:1.5">${esc(x.title)}</b>
      <span class="muted" style="font-size:12px">${esc(x.sub)}${x.at ? " · 最近动静 " + ago(x.at) : ""}</span>
      ${x.detail ? `<div class="col" style="gap:4px"><b style="font-size:13px">${esc(x.detail_label || "说明")}</b><div class="prepresult md">${md(x.detail)}</div></div>` : '<span class="muted" style="font-size:13px">它还没留下更多说明。</span>'}
      ${x.agent === "claude" ? '<span class="muted" style="font-size:11px">Claude 会话只报项目名、你的第一句话和它最后一段回复的开头，完整对话在 Claude App 里。</span>' : ""}
    </section>`;
  } else if (S.overlay === "xphelp") {
    box.innerHTML = `<section class="sheet small"><h2 style="font-size:18px">经验和积分</h2>
      <p>每做完一件事，<b>经验和积分一起涨，一比一</b>，涨的数一样。</p>
      <p><b>经验</b>只涨不减，用来升级、换称号。<br><b>积分</b>是钱包，在“任务 → 商店”拆惊喜会花掉，所以会比经验少。</p>
      <p class="muted" style="font-size:13px">怎么挣：主线一件 +40 / +100 / +200（看大小，节点再 +300，硬骨头 ×1.5）· 支线 +20 · 日常每项 +10（简单版 +5），全勤再 +20，连续满勤 7 天 +50、30 天 +300 · 周常达标 +50，超额每次 +20。撤销完成会把分收回。</p>
      <button type="button" class="btn gold" data-act="close">知道啦</button></section>`;
  } else if (S.overlay === "shortof") {
    box.innerHTML = `<section class="sheet small quip"><p>${esc(S.quip)}</p><button type="button" class="btn gold" data-act="close">知道啦</button></section>`;
  } else if (S.overlay.startsWith("redeem:")) {
    const r = S.quests.rewards.find((x) => String(x.id) === S.overlay.split(":")[1]);
    box.innerHTML = `<section class="sheet small"><h2 style="font-size:18px">确认用 ${r.cost} 分兑换「${esc(r.title)}」？</h2>
      <span class="muted">拆开就收不回啦。要花你钱的部分，小伴 会先问你。</span>
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-act="close">算了</button><button type="button" class="btn primary" data-act="redeemok" data-id="${r.id}">确认兑换</button></div></section>`;
  }
}

/* ---------- 操作 ---------- */
function pop(el, text) {
  const r = el.getBoundingClientRect();
  const n = document.createElement("span");
  n.className = "pop"; n.textContent = text;
  n.style.left = r.left + r.width / 2 + "px"; n.style.top = r.top + r.height / 2 + "px";
  $("#pops").appendChild(n);
  setTimeout(() => n.remove(), 1500);
}

// 商店里点“还没攒够”：每次一句不一样的，有鼓励也有挑衅（2026-10-29 她要的）
const QUIPS = [
  "还差 {g} 积分。盒子就在这儿，跑不掉的。", "{g} 积分而已，你可是连 80 岁都规划好了的人。", "想偷看？没门。先去把今天的日常清了。",
  "差 {g} 积分。小伴 说：我都准备好了，就等你了。", "这么着急，看来是真想要。那就去赚呀。", "还没够哦。不过照你最近的势头，快了。",
  "再摇它也不会自己打开的。", "{g} 积分 ≈ 几个日常 + 一个周常。你算算？", "这么多分都攒过来了，还差这一点？",
  "离拆开只差 {g} 积分，别在这儿盯着它了。", "攒分的样子也很好看，继续。", "小伴 偷偷说：里面那个，你会喜欢的。",
];
let lastQuip = -1;
function quip(gap) {
  let i; do { i = Math.floor(Math.random() * QUIPS.length); } while (i === lastQuip && QUIPS.length > 1);
  lastQuip = i;
  return QUIPS[i].replace("{g}", gap);
}

let undoTimer = null;
function showUndo(msg, undo) {
  const bar = $("#undo");
  bar.innerHTML = `<span>${esc(msg)}</span>${undo ? '<button type="button" class="btn gold" data-act="undo">撤销</button>' : ""}`;
  bar.hidden = false;
  S.pendingUndo = undo;
  clearTimeout(undoTimer);
  undoTimer = setTimeout(() => { bar.hidden = true; S.pendingUndo = null; }, undo ? 6000 : 2500);
}

async function act(action, payload, el, quiet) {
  if (S.busy) return;  // 上一个操作还没回来就不接新的，免得加成两条
  S.busy = true;
  document.body.classList.add("busy");
  let res;
  try { res = await api("/api/act", { action, ...payload }); }
  catch (e) { return e.code === 401 ? renderLogin() : showUndo("没连上服务器，再试一次"); }
  finally { S.busy = false; document.body.classList.remove("busy"); }
  if (!res.ok) { showUndo(res.msg || "没成功"); return; }
  if (res.gain > 0 && el) pop(el, "+" + res.gain);
  if (!quiet) showUndo(res.msg || "好了", res.undo);
  await load(true);
}

document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const a = el.dataset.act, id = el.dataset.id;
  if (a === "go") { S.page = el.dataset.page; S.quests = null; S.team = null; S.map = null; S.overlay = null;
    if (S.page === "brain") { S.bq = null; S.brain = null; }  // 2026-10-29：从导航进大脑看全部，不沿用上次从某个区域带过来的筛选
    if (S.page === "map") S.mapTab = "";  // 进世界先看总览
    // 从导航进来都回到默认筛选（2026-10-29 她说卡册的筛选没复原）
    if (S.page === "cards") S.cardF = null;
    if (S.page === "ach") S.achTab = "";
    if (S.page === "quests") S.tab = "main";
    load(); }
  else if (a === "terr") { S.terrBack = S.overlay === "bigmap" ? "bigmap" : S.page === "territory" ? S.terrBack : S.page; S.overlay = null; S.page = "territory"; S.tkey = el.dataset.k; S.terr = null; render(); load(); }
  else if (a === "rate") act("agent.rate", { id, rating: Number(el.dataset.rating) });
  else if (a === "agent-drop") { if (S.dropAsk === id) { S.dropAsk = null; act("agent.drop", { id }); } else { S.dropAsk = id; showUndo("再点一次 ⊘ 就放弃这件活（不算失败）"); } }
  else if (a === "domain-ok") act("task.domain", { id, key: el.dataset.k });
  else if (a === "maptab") { S.mapTab = el.dataset.v; S.review = null; render(); load(); }
  else if (a === "setpro") act("settings.set", { proactive: !!el.dataset.v });
  else if (a === "cardf") { S.cardF = { ...(S.cardF || {}), [el.dataset.f]: el.dataset.v }; render(); }
  else if (a === "cardopen") { S.cardView = S.cards.cards[Number(el.dataset.i)]; S.overlay = "cardview"; renderOverlay(); }
  else if (a === "prepopen") { S.prepOpen = S.prepOpen === Number(id) ? null : Number(id); renderOverlay(); }
  else if (a === "prepok") act("prep.approve", { id });
  else if (a === "prepfix") { const box = document.querySelector(`[data-prepnote="${id}"]`); act("prep.revise", { id, note: box ? box.value : "" }); }
  else if (a === "bq") {
    S.bq = { ...(S.bq || { tab: "all" }), [el.dataset.f]: el.dataset.v }; S.purge = null;
        load();
  }
  else if (a === "brain-terr") { S.page = "brain"; S.bq = { tab: "all", k: el.dataset.k }; S.brain = null; render(); load(); }
  else if (a === "brain-set") act("brain.set", { id, status: el.dataset.status, was: el.dataset.was });
  else if (a === "brain-purge") { if (S.purge === Number(id)) { S.purge = null; act("brain.purge", { id }); } else { S.purge = Number(id); render(); } }
  else if (a === "goal-up") act("goal.up", { id });
  else if (a === "replan") act("plan.replan", {});
  else if (a === "money-ignore") act("money.ignore", { id, on: el.dataset.on || "" });
  else if (a === "rename") { const t = window.prompt("改成什么？", el.dataset.title || ""); if (t && t.trim()) act("task.rename", { id, title: t.trim() }); }
  else if (a === "celebrate-done") { const keys = (S.celebrating || []).map((x) => x.key); S.celebrating = null; S.overlay = null; if (S.state) S.state.celebrate = []; renderOverlay(); api("/api/act", { action: "ach.seen", keys }).catch(() => {}); }
  else if (a === "bopen") { S.brainOpen = Number(id); S.purge = null; S.overlay = "brain"; renderOverlay(); }
  else if (a === "tarchive") { S.overlay = "tarchive"; renderOverlay(); }
  else if (a === "tab") { S.tab = el.dataset.tab; render(); }
  else if (a === "face") { store("eo_theme", themeKey(S.state) + ":" + (isNight(S.state) ? "day" : "night")); render(); }
  else if (a === "card") {
    if (S.state.card && S.state.card.image) openCard();
    else if (!S.state.card) act("card.make", {}).then(waitForCard);
  }
  else if (["plan", "prep"].includes(a)) { S.overlay = a; S.back = null; renderOverlay(); }
  else if (a === "detail") { S.overlay = "detail"; S.detailK = el.dataset.k; S.back = null; renderOverlay(); if (typeof animateMoves === "function") animateMoves(); }
  else if (a === "addtodo" || a === "addevent") {
    S.back = S.overlay && (S.overlay === "plan" || S.overlay.startsWith("boss:")) ? S.overlay : null;
    S.goal = el.dataset.goal || ""; S.overlay = a; renderOverlay();
  }
  else if (a === "boss") {
    S.overlay = "boss:" + id; renderOverlay();
    if (!S.quests) api("/api/quests").then((q) => { S.quests = q; if (S.overlay === "boss:" + id) renderOverlay(); }).catch(() => {});
  }
  else if (a === "close") { S.overlay = ["addtodo", "addevent"].includes(S.overlay) ? S.back : null; S.back = null; render(); }
  else if (a === "showdone") { S.showDone = !S.showDone; render(); }
  else if (a === "habit") act(el.dataset.done ? "habit.uncheck" : "habit.check", { id, easy: !!el.dataset.easy }, el);
  else if (a === "task") act(el.dataset.done ? "task.undo" : "task.done", { id }, el);
  else if (a === "star") act("task.star", { id, on: !!el.dataset.on });
  else if (a === "postpone") act("task.postpone", { id });
  else if (a === "deltask") act("task.delete", { id });
  else if (a === "delevent") act("event.delete", { id });
  else if (a === "dropprep") act("prep.drop", { id });
  else if (a === "preprestore") act("prep.restore", { id });
  else if (a === "prepstar") { const t = S.state.prep.find((x) => x.id === Number(id)); if (t) t.star = !!el.dataset.on; renderOverlay(); act("prep.star", { id: Number(id), on: el.dataset.on ? 1 : 0 }, el, true); }
  else if (a === "prepgo") { S.prepOpen = Number(id); S.overlay = "prep"; renderOverlay(); }  // 团队页点准备员的活，直接打开那一件
  else if (a === "prepdone") act("prep.done", { id });
  else if (a === "rest") act("rest.take", {});
  else if (a === "redeem") { S.overlay = "redeem:" + id; renderOverlay(); }
  else if (a === "achtab") { S.achTab = el.dataset.v; render(); }
  else if (a === "ethanday") { S.overlay = "ethanday"; S.ethanDays = null; renderOverlay(); api("/api/ethanday").then((d) => { S.ethanDays = d; if (S.overlay === "ethanday") renderOverlay(); }).catch(() => {}); }
  else if (a === "trip") { S.tripI = Number(el.dataset.i); S.overlay = "trip"; renderOverlay(); }
  else if (a === "terrback") {  // 从哪来回哪去：大地图点进来的回大地图（2026-10-29）
    const to = S.terrBack || "map";
    S.page = to === "bigmap" ? "map" : to; S.overlay = to === "bigmap" ? "bigmap" : null; S.map = null; load();
  }
  else if (a === "bigmap") { S.overlay = "bigmap"; renderOverlay(); }
  else if (a === "alive") { S.overlay = "alive:" + el.dataset.i; renderOverlay(); }
  else if (a === "acard") { S.overlay = "acard:" + id; renderOverlay(); }
  else if (a === "xphelp") { S.overlay = "xphelp"; renderOverlay(); }
  else if (a === "shortof") { S.quip = quip(Number(el.dataset.gap)); S.overlay = "shortof"; renderOverlay(); }
  else if (a === "redeemok") { S.overlay = "redeemed"; renderOverlay(); act("reward.redeem", { id }, el, true); }
  else if (a === "redeemnow") {  // 成就/商店弹窗里直接拆
    api("/api/act", { action: "ach.seen", keys: [el.dataset.key] }).catch(() => {});
    S.celebrating = null; if (S.state) S.state.celebrate = [];
    S.overlay = "redeemed"; renderOverlay(); act("reward.redeem", { id }, el, true);
  }
  else if (a === "undo" && S.pendingUndo) { const u = S.pendingUndo; S.pendingUndo = null; $("#undo").hidden = true; const { action, ...rest } = u; act(action, rest, null, true); }
});

document.addEventListener("change", (ev) => {
  const el = ev.target;
  if (el.dataset.change === "time" && el.value) act("event.time", { id: el.dataset.id, start: el.value });
  if (el.dataset.change === "domain" && el.value) act("task.domain", { id: el.dataset.id, key: el.value });
  if (el.dataset.change === "brain-terr") act("brain.set", { id: el.dataset.id, territory: el.value }, null, true);
  if (el.dataset.change === "brain-tags") act("brain.set", { id: el.dataset.id, tags: el.value }, null, true);
  if (el.dataset.change === "bq-k") { S.bq = { ...(S.bq || { tab: "all" }), k: el.value }; load(); }
});

document.addEventListener("submit", (ev) => {
  const f = ev.target.closest("[data-form]");
  if (!f) return;
  ev.preventDefault();
  const d = Object.fromEntries(new FormData(f).entries());
  if (S.busy) return;
  if (f.dataset.form === "review") { if (d.note && d.note.trim()) act("review.feedback", { note: d.note }); return; }
  if (f.dataset.form === "quiet") { act("settings.set", { quiet: d.quiet }); return; }
  if (f.dataset.form === "budget") { act("money.budget", { amount: d.amount }); return; }
  if (f.dataset.form === "goalset") { act("goal.set", { id: f.dataset.id, title: d.title, due: d.due, target: d.target }); return; }
  if (f.dataset.form === "event" && d.title && d.start) {
    S.overlay = S.back; S.back = null; render();
    act("event.add", { title: d.title, start: d.start, end: d.end || "", place: d.place });
  }
  if (f.dataset.form === "addtodo" && d.title) {
    S.overlay = S.back; S.back = null; render();
    act("task.add", { title: d.title, goal_id: d.goal_id || null, when: d.when, territory: d.territory || "" }).then(() => {
      if (S.page === "quests") { S.tab = d.goal_id ? "main" : "side"; render(); }
    });
  }
});

// 点弹窗外面的空白处也能关（2026-10-29 她要所有弹窗都这样）；加待办这类从别的弹窗开出来的，退回上一个
$("#overlay").addEventListener("click", (ev) => {
  if (ev.target !== ev.currentTarget || !S.overlay) return;
  if (S.overlay === "celebrate") { const b = document.querySelector('#overlay [data-act="celebrate-done"]'); if (b) b.click(); return; }
  S.overlay = ["addtodo", "addevent"].includes(S.overlay) ? S.back : null; S.back = null; render();
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && S.overlay) { S.overlay = null; S.back = null; render(); } });

// 先把大图解码好再翻牌，动画才不卡（2026-10-27 她说翻卡卡顿）
function openCard() {
  const img = new Image();
  img.src = imgUrl(S.state.card.image);
  const ready = img.decode ? img.decode() : new Promise((ok) => { img.onload = ok; img.onerror = ok; });
  ready.catch(() => {}).then(() => { store("eo_card_" + S.state.day, "1"); S.overlay = "card"; render(); });
}

// 现画今日卡要半分钟：每 5 秒看一次，画好了自动翻开
let cardTimer = null;
function waitForCard() {
  clearInterval(cardTimer);
  let n = 0;
  cardTimer = setInterval(async () => {
    n += 1;
    await load(true);
    if ((S.state.card && S.state.card.image) || n > 36) {
      clearInterval(cardTimer);
      if (S.state.card && S.state.card.image && !S.overlay) openCard();
    }
  }, 5000);
}

// 出错时把原因显示出来，不留白屏（Telegram 里看不到控制台）
function showError(msg) {
  trace("error", msg);
  const page = $("#page");
  if (page) page.innerHTML = `<div class="login panel"><h2>出错了</h2><p>${esc(msg)}</p><p class="muted">截个图发给 小伴，我们来修。</p></div>`;
}
window.addEventListener("error", (e) => showError(e.message + (e.filename ? ` (${e.filename.split("/").pop()}:${e.lineno})` : "")));
window.addEventListener("unhandledrejection", (e) => showError(String(e.reason && (e.reason.stack || e.reason.message) || e.reason)));
boot();

// 大脑搜索：停手 300 毫秒再查
let brainTimer = null;
document.addEventListener("input", (ev) => {
  const el = ev.target;
  if (el.dataset.input !== "brain-q") return;
  clearTimeout(brainTimer);
  brainTimer = setTimeout(async () => {
    S.bq = { ...(S.bq || { tab: "all" }), q: el.value };
        await load();
    const box = document.querySelector("[data-input=brain-q]");
    if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  }, 300);
});

// 小伴 在准备的事：按住 ⋮⋮ 拖动排序（鼠标和 iPad 手指都行），松手保存，准备员按这个顺序做（2026-10-29 她要）
(function prepDrag() {
  let item = null, list = null;
  document.addEventListener("pointerdown", (ev) => {
    const h = ev.target.closest("[data-drag]");
    if (!h) return;
    item = h.closest(".prepitem, .trow"); list = item && item.parentElement;
    if (!item) return;
    ev.preventDefault();
    item.classList.add("dragging");
    h.setPointerCapture && h.setPointerCapture(ev.pointerId);
  });
  document.addEventListener("pointermove", (ev) => {
    if (!item) return;
    ev.preventDefault();
    const cls = item.classList.contains("trow") ? ".trow" : ".prepitem";
    const others = [...list.querySelectorAll(cls + ":not(.dragging)")];
    const after = others.find((el) => { const r = el.getBoundingClientRect(); return ev.clientY < r.top + r.height / 2; });
    if (after) list.insertBefore(item, after); else list.appendChild(item);
  }, { passive: false });
  const drop = () => {
    if (!item) return;
    item.classList.remove("dragging");
    if (item.classList.contains("trow")) {  // 今日任务
      const ids = [...list.querySelectorAll(".trow")].map((el) => Number(el.dataset.tid));
      item = null; list = null;
      const old = S.state.today.filter((t) => !t.done).map((t) => t.id);
      if (ids.join() !== old.join()) {
        S.state.today = [...ids.map((i) => S.state.today.find((t) => t.id === i)), ...S.state.today.filter((t) => t.done)];
        act("today.order", { ids }, null, true);
      }
      return;
    }
    const ids = [...list.querySelectorAll(".prepitem")].map((el) => Number(el.dataset.pid));
    item = null; list = null;
    const old = (S.state.prep || []).map((t) => t.id);
    if (ids.join() !== old.join()) {
      S.state.prep = ids.map((i) => S.state.prep.find((t) => t.id === i));  // 先在页面上换好，再存
      act("prep.order", { ids }, null, true);
    }
  };
  document.addEventListener("pointerup", drop);
  document.addEventListener("pointercancel", drop);
})();
