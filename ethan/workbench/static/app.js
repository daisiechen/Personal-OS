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
  me: "M12 12a4 4 0 1 0 0-8a4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  people: "M8 11a3.5 3.5 0 1 0 0-7a3.5 3.5 0 0 0 0 7zM2 20a6 6 0 0 1 12 0M16.5 8.5l1.5 1.5l3-3M15 14.5a5.5 5.5 0 0 1 7 5.5",
  team: "M9 11a3 3 0 1 0 0-6a3 3 0 0 0 0 6zM3 20a6 6 0 0 1 12 0M16 11a3 3 0 1 0 0-6M21 20a6 6 0 0 0-4.5-5.8",
  gear: "M12 9a3 3 0 1 0 0 6a3 3 0 0 0 0-6zM12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
};
// 2026-10-31 她定：卡册收成首页“第 N 天”右边的日记，成就并进世界页，导航里不再单列
// 大脑并进“我”页的“全部记忆 → 收藏和资料”（她 2026-10-31 定）
const MENU = [["main", "今日", "home"], ["map", "赛季", "map"],
              ["me", "我", "me"], ["relations", "关系", "people"], ["team", "团队", "team"], ["settings", "设置", "gear"]];
const READY = { relations: true, me: true, main: true, map: true, team: true, brain: true, cards: true, ach: true, settings: true };

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

async function api(path, body, wait) {
  const headers = { "Content-Type": "application/json" };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), wait || 20000);  // 问一问要等模型，给长一点  // 20 秒没回就算连不上，不一直转
  let res;
  try {
    res = await fetch(path, { method: body ? "POST" : "GET", headers, credentials: "same-origin", signal: ctl.signal,
                              body: body ? JSON.stringify(body) : undefined });
  } finally { clearTimeout(timer); }
  if (res.status === 401) { const e = new Error("401"); e.code = 401; throw e; }
  return res.json();
}

// 所有写操作都走这里（Codex 评审 11-05：保存失败时还提示成功、提前清草稿）：服务器回了 ok 才算成；
// 没连上、超时、服务器出错都变成 {ok:false, msg:具体原因}，不抛。调用的地方只在 ok 时提示成功、清草稿，失败时草稿留着
async function send(action, payload, wait) {
  try {
    const r = await api("/api/act", { action, ...payload }, wait);
    if (r && r.ok) return r;
    return { ...(r || {}), ok: false, msg: (r && (r.msg || r.error)) || "没成功" };
  } catch (e) {
    if (e.code === 401) { renderLogin(); return { ok: false, msg: "要重新登录", login: true }; }
    return { ok: false, msg: e.name === "AbortError" ? "等太久没回音，不知道存没存上，草稿还在" : "没连上服务器，草稿还在" };
  }
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
  // 切页面要快（她 2026-11-02：点完要好久才换过去）：首页那份大数据和这一页的数据同时拉，不再一个等一个
  const page = S.page, overlay = S.overlay || "";
  const want = [];
  if (page === "main" || /^(boss:|shop|side)/.test(overlay)) want.push(["quests", "/api/quests"]);
  if (page === "team") want.push(["team", "/api/team"]);
  if (page === "map") want.push(["map", "/api/map"], ["season", "/api/season"], ["review", "/api/review"], ["ach", "/api/ach"]);
  if (page === "settings") want.push(["settings", "/api/settings"]);
  if (page === "territory") want.push(["terr", "/api/territory?k=" + encodeURIComponent(S.tkey)]);
  if (page === "cards") want.push(["cards", "/api/cards"]);
  if (page === "me") want.push(["me", "/api/me"]);
  if (page === "relations") want.push(["rel", "/api/relations"]);
  if (page === "ach") want.push(["ach", "/api/ach"]);
  if (page === "brain") want.push(["brain", "/api/brain?" + new URLSearchParams(S.bq || { tab: "all" })]);
  try {
    const pageData = Promise.all(want.map(([k, u]) => api(u).then((v) => {
      if (S.page !== page) return;  // 拉回来时她已经去了别的页
      S[k] = v;
      if (!quiet && S.state && k !== "quests") { try { render(); } catch (e) { /* 等首页数据到了一起画 */ } }  // 这一页的数据先到就先画
    })));
    pageData.catch(() => {});  // 首页数据出错先返回时，不留没接住的报错；真出错下面 await 照样接住
    const st = await api("/api/state");
    if (!st || st.ok === false) { if (!quiet) showError((st && st.error) || "服务器没给数据"); return; }
    S.state = st;
    await pageData;
    if (S.page !== page) return;
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

// 导航上的数字：今日 = 小伴 准备好等你看的 + 邮件和留言里等你处理的（她 11-04：“邮件和留言有个需要我看的，但是今日没有红点”）
function navCount(st, k) {
  const b = st.badges || {};
  return k === "main" ? (b.main || 0) + (b.desk || 0) : b[k] || 0;
}

function render() {
  const st = S.state;
  const night = isNight(st);
  document.body.className = night ? "night" : "day";
  $("#nav").innerHTML = MENU.map(([k, label, icon]) =>
    `<button type="button" class="${S.page === k || (k === "map" && ["territory", "ach"].includes(S.page)) || (k === "me" && S.page === "brain") || (k === "main" && S.page === "cards") ? "on" : ""}" data-act="go" data-page="${k}" ${READY[k] ? "" : "disabled title=\"第二、三批上线\""} aria-label="${label}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON[icon]}"></path></svg>${label}${navCount(st, k) ? `<i class="navdot" aria-label="${navCount(st, k)} 件要你处理">${navCount(st, k)}</i>` : ""}</button>`).join("");
  const pages = { relations: relationsPage, me: mePage, team: teamPage, map: mapPage, territory: territoryPage, brain: brainPage, cards: cardsPage, ach: achPage, settings: settingsPage };
  // 同一页里重画时（比如勾掉一件任务）留在原来滚到的位置，不跳回顶端（2026-10-30 她说勾完还要接着勾）
  const keep = S._shownPage === S.page ? scrollSpots() : null;
  $("#page").innerHTML = (pages[S.page] || (() => mainPage(night)))();
  if (keep) restoreSpots(keep);
  S._shownPage = S.page;
  $("#page").classList.toggle("home", !pages[S.page]);
  $("#page").classList.toggle("fit", ["settings", "ach", "team"].includes(S.page));  // 设置页一屏放下，长的模块里面自己滚（2026-10-29）  // 今日页在 iPad 上一屏放下，任务区自己滚
  if (!S.overlay && st.celebrate && st.celebrate.length) {  // 不管在哪个页面
    // 积分够换惊喜单独问一句：立即兑换还是再攒一攒（她 2026-11-01）；成就照旧弹庆祝
    S.shopAsk = st.celebrate.filter((x) => x.shop);
    S.celebrating = st.celebrate.filter((x) => !x.shop);
    S.overlay = S.celebrating.length ? "celebrate" : "shopask"; S.askAt = Date.now();
  }
  renderOverlay();
}

function scrollSpots() {
  const spots = [[null, window.scrollY, $("#page").scrollTop]];
  const seen = {};
  $("#page").querySelectorAll("*").forEach((el) => {
    const key = el.tagName + "." + el.className;
    seen[key] = (seen[key] || 0) + 1;
    if (el.scrollTop > 0) spots.push([key, seen[key], el.scrollTop]);
  });
  return spots;
}
function restoreSpots(spots) {
  const [, winY, pageY] = spots[0];
  $("#page").scrollTop = pageY;
  window.scrollTo(0, winY);
  if (spots.length < 2) return;
  const seen = {}, want = new Map(spots.slice(1).map(([k, n, top]) => [k + "#" + n, top]));
  $("#page").querySelectorAll("*").forEach((el) => {
    const key = el.tagName + "." + el.className;
    seen[key] = (seen[key] || 0) + 1;
    const top = want.get(key + "#" + seen[key]);
    if (top) el.scrollTop = top;
  });
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
      <!-- 她 2026-11-01 定：左边第一行名字和等级、第二行 Earth Online 第几天和日记、第三行经验；右边口号、今天工作、天气时间，和每日卡在一起 -->
      <!-- 她 2026-11-01：升级还差多少和进度条放在等级旁边；第三行只放积分 -->
      <div class="row" style="align-items:center;flex-wrap:wrap"><span class="name">Mia</span><span class="lv">Lv.${p.level} ${esc(p.title)}</span>
        <span role="button" class="tap row muted" style="font-size:12px;gap:8px" data-act="xphelp"><div class="bar" style="width:110px" title="这一级走了多少"><i style="width:${p.next_level_at > (p.level_at || 0) ? Math.max(0, Math.min(100, Math.round(100 * (p.earned - (p.level_at || 0)) / (p.next_level_at - (p.level_at || 0))))) : 0}%"></i></div>升级还差 ${Math.max(0, p.next_level_at - p.earned)}</span></div>
      <div class="slogan">${night ? "执行力不够强，是因为不够想。" : "Expect Nothing, Take Jumps!"}</div>
      <!-- 她 2026-11-01 三次嫌日记、商店按钮不好看：不另放按钮，数字本身就是入口——天数点开日记，积分点开商店；前面小图标说明能点，悬停变金 -->
      <div><button type="button" class="hudlink" data-act="go" data-page="cards" aria-label="打开日记"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H6.5A2.5 2.5 0 0 0 4 21.5z"></path><path d="M4 5.5v16"></path><path d="M9 8h7"></path></svg><span>Earth Online 第 <b>${p.day_n || 1}</b> 天</span><i aria-hidden="true">›</i></button></div>
      ${st.work && st.work.has_data ? `<div class="muted right" style="font-size:12px">今天工作 <b style="color:var(--text);font-size:15px">${st.work.hours}</b> 小时 · 工作狂指数 <b class="${st.work.light === "bad" ? "warnc" : st.work.light === "warn" ? "gold" : "okc"}" style="font-size:15px">${st.work.index}</b><span class="muted"> ${esc(st.work.label || "")} · 健康 20–49</span></div>` : '<div class="empty"></div>'}
      <div class="row" style="gap:2px"><button type="button" class="hudlink" data-act="shop" aria-label="打开商店"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="9" width="17" height="11.5" rx="1.5"></rect><path d="M2.5 9h19M12 9v11.5M12 9c-1.8-3.6-6-3.8-5.3-1.2.4 1.3 3.3 1.4 5.3 1.2zm0 0c1.8-3.6 6-3.8 5.3-1.2-.4 1.3-3.3 1.4-5.3 1.2z"></path></svg><span>积分 <b>${p.balance}</b></span><i aria-hidden="true">›</i></button><button type="button" class="hudhelp" data-act="xphelp" aria-label="经验和积分怎么算">ⓘ</button></div>
      <div class="muted right" style="font-size:12px">伦敦${st.weather ? " · " + esc(st.weather) : ""} · ${st.day.slice(5).replace("-", "月")}日 周${st.weekday} ${st.time}</div>
    </div>
    <button type="button" class="cardthumb ${card && card.image && store("eo_card_" + st.day) ? RAR(card.rarity) : ""}" data-act="card" aria-label="今日卡，点一下翻开">${cardThumb}</button>
  </header>
  <section class="panel season" aria-label="赛季进度">
    <b class="nowrap">${esc(st.season.name)}</b><div class="bar"><i style="width:${st.season.pct}%"></i></div>
    <b role="button" class="tap" data-act="seasonhelp" title="赛季按时间走了多少">${st.season.pct}% <span class="muted" style="font-weight:400;font-size:11px">ⓘ</span></b><span class="muted nowrap">剩 ${st.season.days_left} 天</span>
  </section>
  <div class="cols">
    <section class="panel col" aria-label="今天时间线">
      <div class="row" style="justify-content:space-between"><h2>今天</h2><button type="button" class="btn gold" data-act="plan">日历 · 改计划</button></div>
      ${timeline()}
      ${st.detail && st.detail.train ? `<div class="row" style="gap:8px"><button type="button" class="btn" data-act="detail" data-k="train">🏋️ 训练详情</button><button type="button" class="btn" data-act="detail" data-k="meals">🍳 三餐</button></div>` : ""}
    </section>
    <section class="panel col taskpanel" aria-label="今日任务">
      <div class="row" style="justify-content:space-between"><h2>今日任务</h2>
        <span class="row">        <button type="button" class="btn gold" data-act="plan">＋ 加 · 编辑</button></span></div>
      <div class="tasklist">${todo.length ? todo.map(taskRow).join("") : `<p class="muted">${done.length ? "今天的都做完了！" : st.plan_ready ? "今天还空着，想做点什么就点“＋ 加 · 编辑”。" : "计划排好后，今天要做的会出现在这儿。"}</p>`}
      ${done.length ? `<button type="button" class="chip" style="align-self:flex-start" data-act="showdone">已完成 ${done.length} 件 ${S.showDone ? "▾ 收起" : "▸ 展开"}</button>${S.showDone ? done.map(taskRow).join("") : ""}` : ""}</div>
      <!-- 日常、周常的标题和“今日任务”一样大、一样粗（她 2026-11-01：像备注一样容易忽略） -->
      <div class="row subhead" style="justify-content:space-between"><span class="row" style="gap:10px;align-items:center"><h2>日常</h2>${st.habits.daily.some((h) => !h.yesterday) ? '<button type="button" class="btn small" data-act="makeup">补昨天的卡</button>' : ""}</span><span class="row" style="gap:10px">${st.habits.rest_day ? '<button type="button" class="btn small gold" data-act="restcancel" title="点一下取消今天的休整">休整中</button>' : `<button type="button" class="btn small" data-act="rest" ${st.habits.rest_left ? 'title="每周一次：今天做到 2 项日常就算满勤"' : 'disabled title="这周用过了"'}>休整</button>`}<span role="button" class="tap muted" style="font-size:12px" data-act="streakhelp">连续全勤 <b style="color:var(--text)">${p.streak}</b> 天 ⓘ</span></span></div>
      <div class="chips">
        ${st.habits.daily.map((h) => `<button type="button" class="chip ${h.done ? "on" : ""}" data-act="habit" data-id="${h.id}" data-done="${h.done ? 1 : ""}">${h.done ? "✓ " : ""}${esc(short(h.title))}</button>`).join("")}</div>
      <div class="row subhead" style="justify-content:space-between"><h2>周常</h2><span class="muted" style="font-size:12px">达标 +50，多做一次再 +20</span></div>
      <div class="chips">
        ${st.habits.weekly.map((h) => { const ok = h.count >= h.target; return `<button type="button" class="chip ${ok ? "on" : ""}" data-act="habit" data-id="${h.id}" data-done="${h.today ? 1 : ""}">${ok ? "✓ " : "○ "}${esc(short(h.title))} ${h.count}/${h.target}</button>`; }).join("")}
</div>
      ${st.habits.rest_day ? `<div class="gold" style="font-size:12px">今天休整：做到 2 项日常就算满勤</div>` : ""}
    </section>
    <section class="panel col" aria-label="副本" style="overflow:auto;min-height:0">
      <h2>赛季副本</h2>
      ${st.bosses.map((b) => `<button type="button" class="boss ${b.main ? "" : "alt"}" data-act="boss" data-id="${b.goal_id}" aria-label="${esc(b.name)}详情">
        <div class="name"><b style="font-size:13px">${b.main ? "★ " : ""}${esc(b.name)}${b.main ? "（主副本）" : ""}</b><span class="muted" style="font-size:11px">${esc(b.line)}</span></div>
        <div class="bar"><i style="width:${Math.round(100 * (b.frac || 0))}%"></i></div>
        <div class="bossfoot"><span class="muted one">${b.next ? "下一步：" + esc(b.next) : b.steps_total ? "主线都做完了" : "还没拆步骤"}</span>${bossPill(b)}</div>${b.comment ? `<div class="muted one" style="font-size:11px">${esc(b.comment)}</div>` : ""}</button>`).join("") || '<p class="muted">这个赛季还没开副本</p>'}
      ${sideMini()}
    </section>
  </div>
  <footer class="foot">  <!-- 她 2026-11-01 定：左邮件和留言、中 小伴 的一天、右 小伴 在准备，和上面三栏一样宽 -->
    <button type="button" class="panel nowrap deskbtn ${(st.badges || {}).desk ? "gold" : "quiet"}" data-act="desk" aria-label="今日邮件和留言">邮件和留言${(st.badges || {}).desk ? ` <span class="rbadge">${st.badges.desk}</span>` : ""} ▸</button>
    <button type="button" class="panel ethanline" data-act="ethanday" aria-label="看 小伴 的一天"><span class="gold">小伴</span>　${esc((st.card && st.card.line) || "今天也在。")}<span class="muted nowrap" style="margin-left:auto;font-size:12px">他的一天 ▸</span></button>
    <button type="button" class="panel gold nowrap" data-act="prep">小伴 今天在准备 ${st.prep.filter((t) => t.state !== "pending" && prepShown(st, t)).length} 件${(st.badges || {}).main ? ` <span class="rbadge">${st.badges.main} 件等你看</span>` : ""}${st.parked_remind ? ' <i class="navdot inline" aria-label="先放着的今天还没看">·</i>' : ""} ▸</button>
  </footer>`;
}

// 副本的任务列表：任务页和首页副本详情共用，编辑能力一样（她 2026-11-01：弹窗里比任务页少的都加上）
const stepList = (gid, steps, ed) => `<div class="steplist" data-goal="${gid}">${steps.map((s) => ed ? `<div class="step edit ${s.state}" data-stepid="${s.id}"><span class="grip" data-grip aria-label="按住拖动排序" title="按住拖动排序">⋮⋮</span><input type="text" value="${esc(s.title)}" data-steptitle="${s.id}" aria-label="任务名字">
          <button type="button" class="ib ${s.milestone ? "on" : ""}" data-act="stepnode" data-id="${s.id}" data-on="${s.milestone ? "" : 1}" title="标成节点（里程碑）">节点</button>
          <button type="button" class="ib" data-act="stepdel" data-id="${s.id}" aria-label="删掉这个任务">✕</button></div>`
        : `<button type="button" class="step ${s.state}" data-act="task" data-id="${s.id}" data-done="${s.state === "done" ? 1 : ""}" data-points="${s.points}">
        <span class="gold" style="width:18px;text-align:center">${s.state === "done" ? "✓" : s.state === "now" ? "▶" : "○"}</span>
        <span style="flex:1">${esc(s.title)}${prog(s)}${s.hard ? ' <span class="gold" style="font-size:11px">硬骨头</span>' : ""}${s.milestone ? ' <span class="gold" style="font-size:11px">节点</span>' : ""}</span>
        <span class="gold" style="font-size:11px">+${s.points}</span></button>`).join("") || '<p class="muted" style="font-size:12px">还没拆任务。</p>'}</div>`;

// 首页副本栏底下的支线：显示前 3 件，点开看全部、加支线（她 2026-11-01 去掉任务页）
function sideMini() {
  const q = S.quests;
  if (!q) return "";
  const n = q.side.length;
  return `<div class="row subhead" style="justify-content:space-between"><h2>其他</h2><span class="row" style="gap:6px"><button type="button" class="pen" data-act="addtodo" title="加一件">＋</button><button type="button" class="btn small" data-act="side">全部 ${n} ▸</button></span></div>
    <div class="col sidemini" style="gap:4px">${q.side.slice(0, 3).map((x) => `<button type="button" class="step locked" data-act="task" data-id="${x.id}" data-done="" data-points="${x.points}"><span class="gold" style="width:18px;text-align:center">○</span><span style="flex:1">${esc(x.title)}${prog(x)}</span><span class="gold" style="font-size:11px">+${x.points}</span></button>`).join("") || '<p class="muted" style="font-size:12px">没有别的事</p>'}</div>`;
}

// 新结构的副本详情：里程碑一个个过，当前的展开列阶段任务（频次、这一阶段的进度、前置），过去的折叠，以后的只写名字和截止日
// 编辑里能改里程碑（名字、截止日、目标值）和阶段任务（名字、频次、前置、删、拖动排序），底下加任务、加里程碑（quest-structure.md 第 4 节）
const CADS = [["once", "一次性"], ["daily", "每天"], ["weekly", "每周"], ["per_milestone", "每阶段一次"]];
const MSTATE = { done: ["达成", "ms-ok"], missed: ["没达成", "ms-miss"] };
// 小伴 起草的阶段复盘：复盘排进待办那天早上写好，她看完再勾“复盘”（她 2026-11-02）
const msDraft = (d) => `<div class="msdraft"><span class="muted">${d.by ? "小伴 起草的复盘" : "这一阶段的数"}</span><p>${esc(d.summary)}</p>${(d.suggest || []).length ? `<span class="muted">给下个阶段的建议</span><ul>${d.suggest.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</div>`;
function milestoneView(gid, ms, ed) {
  const passed = ms.filter((m) => m.state === "past").length;
  const habits = (S.quests && S.quests.habits) || [];
  const pill = (m) => m.state === "past" ? `<span class="mspill ${MSTATE[m.status][1]}">${m.status === "done" ? "✓ " : ""}${MSTATE[m.status][0]}</span>`
    : m.state === "now" ? '<span class="mspill ms-now">进行中</span>' : "";
  const meta = (m) => [m.due ? "截止 " + m.due.slice(5) : "", m.target != null ? "目标 ≤ " + m.target : ""].filter(Boolean).join("　");
  const head = (m) => ed
    ? `<div class="mshead"><input type="text" value="${esc(m.title)}" data-ms="${m.id}" data-msk="title" class="msname" aria-label="里程碑名字">
        <input type="date" value="${esc(m.due || "")}" data-ms="${m.id}" data-msk="due" aria-label="截止日">${m.target != null ? `<input type="text" inputmode="decimal" value="${m.target}" data-ms="${m.id}" data-msk="target" style="width:70px" aria-label="目标值">` : ""}</div>`
    : `<div class="mshead"><b class="msname">${esc(m.title)}</b>${pill(m)}<span class="muted msmeta">${esc(meta(m))}</span></div>`;
  const indent = (t) => t.depth ? `style="margin-left:${t.depth * 22}px;width:calc(100% - ${t.depth * 22}px)"` : "";
  const row = (t, m) => {
    if (ed) {  // 前置条件：按住 ⋮⋮ 拖到要等的那件上，就挂在它后面（可以一级级挂下去）；↰ 取消等待
      const hab = `<select data-stagehabit="${t.id}" aria-label="引用的日常周常"><option value="">不引用</option>${habits.map((h) => `<option value="${h.id}"${t.habit_id === h.id ? " selected" : ""}>引用：${esc(h.title)}</option>`).join("")}</select>`;
      return `<div class="step edit${t.depth ? " hung" : ""}" data-stepid="${t.id}" ${indent(t)}><span class="grip" data-grip aria-label="按住拖动：拖到另一件上就是等它做完" title="按住拖动排序；拖到另一件上 = 等它做完">⋮⋮</span><input type="text" value="${esc(t.title)}" data-steptitle="${t.id}" aria-label="任务名字">
        ${t.habit ? hab : `<select data-stagecad="${t.id}" aria-label="频次">${CADS.map(([v, l]) => `<option value="${v}"${t.cadence === v ? " selected" : ""}>${l}</option>`).join("")}</select>
        ${t.cadence === "weekly" ? `<input type="number" min="1" max="7" value="${t.per_week || 1}" data-stageweek="${t.id}" class="wk" aria-label="每周几次">` : ""}${t.review ? "" : hab}`}
        ${t.depth ? `<button type="button" class="ib" data-act="unhang" data-id="${t.id}" title="不用等了">↰</button>` : ""}
        <button type="button" class="ib" data-act="stepdel" data-id="${t.id}" aria-label="删掉这个任务">✕</button></div>`;
    }
    const on = t.cadence === "once" ? t.done : t.today;
    const mark = t.habit ? "↻" : on ? "✓" : t.blocked ? "⏸" : "○";
    const info = `${t.cadence === "once" ? "" : `<span class="muted tinfo">${esc(t.cadence_name)}${t.per_week && t.cadence === "weekly" ? " " + t.per_week + " 次" : ""} · ${esc(t.progress)}</span>`}${t.blocked && t.depends ? `<span class="muted tinfo">等「${esc(t.depends)}」</span>` : ""}${t.review ? '<span class="gold tinfo">复盘</span>' : ""}`;
    if (m.state === "past" && t.review && on)  // 勾错的复盘还能撤销：里程碑重新打开（她 2026-11-01）
      return `<button type="button" class="step tstep done" data-act="task" data-id="${t.id}" data-done="1" data-confirm="撤销会把这个里程碑重新打开，再点一次确认" ${indent(t)}><span class="gold mark">✓</span><span class="tbody">${esc(t.title)}${info}</span><span class="muted tinfo">点两次撤销</span></button>`;
    if (t.habit || m.state === "past" || t.carry) return `<div class="step locked tstep" ${indent(t)}><span class="gold mark">${t.habit ? "↻" : on ? "✓" : "○"}</span><span class="tbody">${esc(t.title)}${info}</span>${t.habit ? `<span class="muted tinfo">在日常「${esc(t.habit)}」里打卡</span>` : t.carry ? '<span class="muted tinfo">到这个阶段自动带过来</span>' : ""}</div>`;
    const confirm = t.review || m.state === "later";  // 结束阶段的复盘、以后阶段的任务：点两次才算（她 2026-11-01 不小心勾了复盘）
    return `<button type="button" class="step tstep ${on ? "done" : t.blocked ? "locked" : "now"}" data-act="task" data-id="${t.id}" data-done="${on ? 1 : ""}" data-points="${t.points}"${confirm && !on ? ` data-confirm="${t.review ? "勾了这件就结束这个里程碑，再点一次确认" : "这是后面阶段的事，再点一次确认已经做了"}"` : ""} ${t.blocked && !on ? "disabled" : ""} ${indent(t)}>
      <span class="gold mark">${mark}</span><span class="tbody">${esc(t.title)}${info}</span>${t.points ? `<span class="gold tinfo">+${t.points}</span>` : ""}</button>`;
  };
  const tasks = (m) => `<div class="steplist mstasks" data-goal="${gid}" data-ms="${m.id}">${m.tasks.filter((t) => !(ed && t.carry)).map((t) => row(t, m)).join("") || '<p class="muted" style="font-size:12px;margin:0">还没有阶段任务</p>'}</div>
    ${ed ? `<form class="stepadd" data-form="stageadd" data-ms="${m.id}"><input type="text" name="title" required placeholder="加一个阶段任务" aria-label="新任务">
      <select name="cadence" data-addcad aria-label="频次">${CADS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select><input type="number" name="per_week" min="1" max="7" value="1" class="wk" hidden aria-label="每周几次"><button class="btn gold">加上</button></form>` : ""}`;
  return `<div class="row" style="justify-content:space-between"><b style="font-size:14px">里程碑 ${passed} / ${ms.length}</b><button type="button" class="btn small ${ed ? "gold" : ""}" data-act="editgoal" data-id="${gid}">${ed ? "完成" : "✎ 编辑"}</button></div>
    <div class="mslist">${ms.map((m) => m.state === "now" || ed
      ? `<section class="ms ms-${m.state} ${m.state === "past" ? MSTATE[m.status][1] : ""}">${head(m)}${!ed && m.draft ? msDraft(m.draft) : ""}${tasks(m)}</section>`
      : `<details class="ms ms-${m.state} ${m.state === "past" ? MSTATE[m.status][1] : ""}"><summary>${head(m)}</summary>${m.review ? `<p class="msreview">${esc(m.review)}</p>` : ""}${tasks(m)}</details>`).join("")}</div>
    ${ed ? `<form class="stepadd" data-form="msadd" data-goal="${gid}"><input type="text" name="title" required placeholder="加一个里程碑" aria-label="新里程碑"><input type="date" name="due" aria-label="截止日"><button class="btn gold">加上</button></form>` : ""}`;
}

const short = (t) => t.replace(/：.*$/, "").replace("练英语", "英语");

function timeline() {
  const st = S.state;
  if (!st.plan_ready) return '<p class="muted">今天的计划 小伴 还没排出来，每天早上 6:30 他会排好。</p>';
  // 要出门的行点开看：去哪、路上多久、几点出发、几点提醒，带地图链接（2026-10-29 她要）
  const rows = st.timeline.map((t, i) => ({ at: t.at, text: t.text, trip: t.trip ? i : null, warn: t.icon === "⚠️" }));
  rows.push({ at: st.time, text: "现在", now: true, trip: null });
  rows.sort((a, b) => a.at.localeCompare(b.at));
  if (rows.length === 1) rows.push({ at: "", text: "今天不用出门", trip: null });
  // 赶不上、要早走、要挪训练的那一步标出来（她 2026-11-02：提醒我早走的应该 highlight）
  return rows.map((r) => r.trip !== null
    ? `<button type="button" class="tl trip ${r.warn ? "warn" : ""}" data-act="trip" data-i="${r.trip}"><span class="t">${esc(r.at)}</span><div class="b">${r.warn ? "⚠️ " : ""}${esc(r.text)} <span class="gold">▸</span></div></button>`
    : `<div class="tl ${r.now ? "now" : ""} ${r.warn ? "warn" : ""}"><span class="t">${esc(r.at)}</span><div class="b">${r.warn ? "⚠️ " : ""}${esc(r.text)}</div></div>`).join("");
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
/* ---------- 支线和商店（任务页去掉后放在首页，她 2026-11-01） ---------- */
function sideBody(q) {
  return `<section class="col" style="gap:4px">
      ${q.side.map((s) => `<div class="item"><span class="grow">${esc(s.title)}${prog(s)}</span><span class="gold" style="font-size:11px">+${s.points}</span>
        <button type="button" class="btn" data-act="task" data-id="${s.id}" data-done="" data-points="${s.points}">完成</button></div>`).join("") || '<p class="muted">没有别的事。</p>'}</section>
      ${false ? `<section class="col" style="gap:4px"><b style="font-size:13px">暂停副本的</b>
      ${q.later.map((s) => `<div class="item"><span class="grow">${esc(s.title)} <span class="muted" style="font-size:11px">${esc(s.goal)}</span></span><span class="gold" style="font-size:11px">+${s.points}</span>
        <button type="button" class="btn" data-act="task" data-id="${s.id}" data-done="" data-points="${s.points}">完成</button></div>`).join("")}</section>` : ""}`;
}

function shopBody(q) {
    const TIER = { 小: ["t1", "银封"], 中: ["t2", "星封"], 大: ["t3", "金封"] };
    const have = Math.max(0, q.balance);
    return `${q.balance < 0 ? `<p class="muted" style="font-size:12px">积分 ${q.balance}：拆完惊喜后撤销了几件拿过分的事，欠的这点之后挣回来就抵掉。</p>` : ""}
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
          <span class="muted one" style="min-width:0">${h.status === "待策划" ? "小伴 在准备，会主动来找你揭晓；之后他会问你玩了没有，由他记兑现" : esc(h.note || "")}</span>
          ${h.status === "待策划" ? '<span class="hstate wait">待兑现</span>' : '<span class="hstate done">已兑现</span>'}</div>`).join("") || '<span class="muted">还没有兑换过</span>'}</section>`;
}

/* ---------- 领地地图和领地页（docs/design/territories.md；底图是她选的 Grok Imagine 第 2 张） ---------- */
const TERRS = [["body", "健康"], ["career", "事业"], ["money", "财务"], ["social", "社交"], ["study", "学习"], ["academic", "学术"], ["life", "生活体验"]];
// 卡的稀有度：边框不同，超稀有及以上有流光（2026-10-29 她要）
// 每块领域一个小图标（2026-10-29 她说只用一个字有点尴尬）
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

// 世界页（2026-10-29 她说进来先看总览分析，而不是一张不变的地图）：总览 = 本周回顾 + 各领域 + 缩小的地图（点开放大、再点进领域）；往期 = 过去几周、月度、赛季
const wDots = (terrs) => `<span class="row" style="gap:6px;flex-wrap:wrap">${terrs.map((t) => `<span class="pill p-${t.light}" title="${esc(t.name)}">${esc(t.name)}</span>`).join("")}</span>`;
const wTally = (terrs) => `<table class="htbl"><thead><tr><th>领域</th><th>绿</th><th>黄</th><th>红</th><th>灰</th></tr></thead><tbody>${terrs.map((t) =>
    `<tr><td>${esc(t.name)}</td><td class="p-ok">${t.ok || ""}</td><td class="p-warn">${t.warn || ""}</td><td class="p-bad">${t.bad || ""}</td><td class="muted">${t.none || ""}</td></tr>`).join("")}</tbody></table>`;
const weekCard = (w, open) => `<section class="panel col">
      <div class="row" style="justify-content:space-between;flex-wrap:wrap"><h2>${w.live ? "本周回顾" : esc(w.week.slice(5)) + " 那周"}<span class="muted" style="font-size:12px;font-weight:400"> ${esc(w.week.slice(5))}–${esc(w.end.slice(5))}</span></h2>
        <span class="muted" style="font-size:12px">经验 <b style="color:var(--text)">+${w.points}</b> · 满勤 ${w.full_days}/${w.days} 天</span></div>
      ${open ? "" : wDots(w.territories)}
      ${open ? `<div class="wk3">${w.territories.filter((t) => t.rows.some((x) => x.light !== "none" || x.log)).map((t) => `<div class="col" style="gap:4px"><b style="font-size:13px">${esc(t.name)}</b>${t.rows.filter((x) => x.light !== "none" || x.log).map((x) => `<span style="font-size:12px">${esc(x.name)}：${esc(x.actual)} ${x.light !== "none" ? `<span class="pill p-${x.light}" style="font-size:10px">${LIGHT[x.light]}</span>` : ""}${x.log ? ` <button type="button" class="pen" data-act="lifelogopen" data-kind="${x.log}" title="记一笔">＋</button>` : ""}</span>`).join("")}</div>`).join("") || '<span class="muted" style="font-size:12px">这周还没有能算的数据，接上就有了。</span>'}</div>` : ""}
      ${w.bosses && w.bosses.length ? `<div class="col" style="gap:6px">${w.bosses.map((b) => `<div><span style="font-size:12px">${b.main ? "★ " : ""}${esc(b.name)} <span class="muted">${esc(b.status || b.line || "")}</span></span><div class="bar"><i style="width:${Math.round(100 * (b.frac || 0))}%"></i></div></div>`).join("")}</div>` : ""}
      ${w.ethan ? `<details><summary class="gold" style="font-size:13px;cursor:pointer">小伴 的周报</summary><div style="white-space:pre-wrap;font-size:13px;line-height:1.7;margin-top:6px">${esc(w.ethan)}</div></details>` : ""}</section>`;
const mapTags = (m) => m.territories.map((t) => `<button type="button" class="mtag l-${t.light}" data-act="terr" data-k="${t.key}" style="left:${MAPPOS[t.key][0]}%;top:${MAPPOS[t.key][1]}%"><span class="lamp">${LIGHT[t.light]}</span><b class="tname">${tIcon(t.key)}${esc(t.name)}</b><small>${esc(t.line)}</small></button>`).join("") +
  `<button type="button" class="mtag waste" data-act="terr" data-k="waste" style="left:${MAPPOS.waste[0]}%;top:${MAPPOS.waste[1]}%"><b>荒原</b><small>${m.waste ? m.waste + " 件没归类" : "没有没归类的"}</small></button>` +
  `<button type="button" class="mtag capital" data-act="go" data-page="brain" style="left:${MAPPOS.capital[0]}%;top:${MAPPOS.capital[1]}%"><b style="font-size:13px">收藏和资料</b><small>${m.brain_ask ? m.brain_ask + " 条要你看" : "全部资料"}</small></button>`;
const mapImg = () => `<img src="/static/map-${isNight(S.state) ? "night" : "day"}.jpg" alt="Earth Online 大陆：七个领域围着中央王城，东南角是荒原" width="1280" height="800">`;

// 赛季页（她 2026-11-02 选了样稿 B，docs/design/season-page.md）：不放副本进度（首页有），只讲七块领域这个赛季怎么样、
// 是什么造成的、彼此怎么此消彼长、下个赛季怎么调。往季回顾、成就是右上角两个按钮。
const WD = ["一", "二", "三", "四", "五", "六", "日"];
// 颜色只跟方向走：往上绿、往下暖橙、没变灰；跌破保底另用红框标（她 2026-11-02：红色的怎么也是往上的箭头）
// 地图上写现在的分数（和领域页的大数字一样），箭头只表示比开季涨还是跌（她 2026-11-02：34 和 67 对不上很困惑）
const arrow = (d) => d.delta == null || d.delta === 0 ? "" : `<span class="${d.delta < 0 ? "dn" : "up"}">${d.delta < 0 ? "↓" : "↑"}</span>`;
const delta = (d) => d.delta == null ? '<span class="muted">—</span>' : d.delta === 0 ? '<span class="muted">→0</span>' : `<span class="${d.delta < 0 ? "dn" : "up"}">${d.delta < 0 ? "↓" : "↑"}${Math.abs(d.delta)}</span>`;
// 赛季页右上角是两个按钮；往季回顾和成就页右上角是返回赛季（她 2026-11-02：只能点导航回去）
const seasonHead = (title, extra) => `<header class="row seasonhead"><h2 class="seasonname">${title}</h2>${extra || ""}
  <span class="row" style="gap:8px;margin-left:auto">${S.mapTab ? backBtn("赛季", 'data-act="maptab" data-v=""') : `${S.season && S.season.draft ? '<button type="button" class="btn gold" data-act="maptab" data-v="draft">下季草稿</button>' : ""}
  <button type="button" class="btn" data-act="maptab" data-v="reviews">复盘记录</button>
  <button type="button" class="btn" data-act="maptab" data-v="history">往季回顾</button>
  <button type="button" class="btn" data-act="maptab" data-v="ach">成就</button>`}</span></header>`;
const mapTabs = () => "";  // 旧的“总览 / 往期 / 成就”标签不用了，换成赛季页右上角两个按钮

function mapPage() {
  if (S.mapTab === "history") return pastPage();
  if (S.mapTab === "reviews") return reviewsPage();
  if (S.mapTab === "draft") return draftPage();
  if (S.mapTab === "ach") return achPage();
  const z = S.season;
  if (!z) return '<p class="loading">加载中……</p>';
  const sz = z.season, open = true;  // 依据一直显示，不用点开收起（她 2026-11-02：两个按钮不在一个地方，还会跳）
  const led = z.ledger;
  const ledger = `<div class="ledger"><div class="row" style="justify-content:space-between"><span class="muted">依据：${led.weeks.length ? "第 " + led.weeks.map((w) => w.n).join("、") + " 周" : "还没有数据"}</span></div>
    ${led.rows.map((r) => `<div class="lrow"><span>${esc(r.name)}</span><b>${r.values.map((v) => `<i class="${v.bad ? "bad" : ""}">${v.v}</i>`).join('<em>›</em>')}</b><small>${esc(r.unit)}</small><span class="muted">${esc(r.std)}</span></div>`).join("")}</div>`;
  const focus = z.domains.filter((d) => d.focus);
  const rel = (d) => [...d.crowded_by.map((x) => `<span class="rel crowd">被挤：${esc(x)}</span>`), ...d.helped_by.map((x) => `<span class="rel help">帮了它：${esc(x)}</span>`),
    ...(d.crowds.length ? [`<span class="rel crowd">挤了${d.crowds.map(esc).join("、")}</span>`] : [])].join("");
  const cal = (c) => `<div class="mcal"><div class="mcalh">${WD.map((w) => `<i>${w}</i>`).join("")}</div><div class="mcalg">${c.map((x) => `<i class="c-${x.state}" title="${x.day}"></i>`).join("")}</div></div>`;
  const card = (d) => `<button type="button" class="fcard ${d.floor ? "floor" : "down"}" data-act="terr" data-k="${d.key}">
      <span class="row" style="justify-content:space-between"><b class="fname">${tIcon(d.key)}${esc(d.name)}</b><span class="muted" style="font-size:12px">${d.hours != null ? d.hours + " 小时 " : ""}›</span></span>
      <span class="fscore"><span class="muted">${d.start ?? "—"}</span> → <b class="${d.floor ? "dn-bad" : "dn"}">${d.now ?? "—"}</b>${d.floor ? '<span class="pill p-bad">跌破保底</span>' : ""}</span>
      <span class="fbody">${d.calendar ? cal(d.calendar) : ""}<span class="facts">${d.facts.map((f) => `<span><span class="muted">${esc(f.name)}</span> ${esc(f.actual)}</span>`).join("")}</span></span>
      <span class="fwhy">${esc(d.why)}</span><span class="rels">${rel(d)}</span></button>`;
  const rest = z.domains.filter((d) => !d.focus);
  const lines = z.relations.filter((r) => MAPPOS[r.from] && MAPPOS[r.to]);
  const map = `<div class="smap">${mapImg()}<svg viewBox="0 0 100 62.5" preserveAspectRatio="none" aria-hidden="true">${lines.map((r) => `<line x1="${MAPPOS[r.from][0]}" y1="${MAPPOS[r.from][1] * 0.625}" x2="${MAPPOS[r.to][0]}" y2="${MAPPOS[r.to][1] * 0.625}" class="${r.kind}"/>`).join("")}</svg>
      ${lines.map((r) => `<span class="rlabel ${r.kind}" style="left:${(MAPPOS[r.from][0] + MAPPOS[r.to][0]) / 2}%;top:${(MAPPOS[r.from][1] + MAPPOS[r.to][1]) / 2}%" title="${esc(r.text)}">${esc(r.label)}</span>`).join("")}
      <span class="mapnote">数字是现在的领域分数（0–100），箭头是比开季涨了还是跌了</span>
      ${(S.state.badges || {}).map ? `<button type="button" class="stag waste" title="没归到领域的待办，点开选它属于哪块" data-act="terr" data-k="waste" style="left:${MAPPOS.waste[0]}%;top:${MAPPOS.waste[1]}%">荒原 <b>${S.state.badges.map}</b> 件没归类</button>` : ""}
      ${z.domains.map((d) => `<button type="button" class="stag ${d.floor ? "floor" : ""} ${MAPPOS[d.key][0] < 20 ? "eL" : MAPPOS[d.key][0] > 80 ? "eR" : ""}" title="${esc(d.name)}：开季 ${d.start ?? "—"} → 现在 ${d.now ?? "—"}${d.floor ? "，跌破保底" : ""}" data-act="terr" data-k="${d.key}" style="left:${MAPPOS[d.key][0]}%;top:${MAPPOS[d.key][1]}%">${esc(d.name)} <b>${d.now ?? "—"}</b>${arrow(d)}</button>`).join("")}</div>`;
  const nx = z.next;
  const next = sz.late && nx ? `<section class="nextbox"><b>下个赛季的建议</b><div class="nextcols">${[["保留", nx.keep], ["暂停", nx.pause], ["新开", nx.start]].map(([t, l]) => `<div><span class="muted">${t}</span>${(l || []).map((x) => `<span>${esc(x)}</span>`).join("") || '<span class="muted">—</span>'}</div>`).join("")}</div>${nx.rule ? `<span class="gold">${esc(nx.rule)}</span>` : ""}</section>`
    : `<section class="nextbox wait"><b>下个赛季的建议</b><span class="muted">保留、暂停、新开哪些目标，${esc(sz.two_thirds.slice(5))} 后出现</span></section>`;
  return seasonHead(esc(sz.name), `<span class="muted">第 ${sz.week} 周，剩 ${sz.days_left} 天</span>`) + `
    <section class="sverdict ${open ? "" : "closed"}"><div class="vline"><p>${esc(z.line)}</p>
      <span class="muted" style="font-size:12px">${z.line_by ? `小伴 ${esc(z.line_at.slice(5))} 写` : "小伴 还没写，先按数字说一句"}${z.rewriting ? "　·　小伴 正在按这几天的情况重写，过一会儿刷新" : ""}</span></div>${open ? ledger : ""}</section>
    ${focus.length ? `<div class="fcards">${focus.map(card).join("")}</div>` : ""}
    <div class="sgrid">${map}<div class="col" style="gap:10px">
      ${rest.filter((d) => d.source).map((d) => `<button type="button" class="srccard" data-act="terr" data-k="${d.key}"><span class="row" style="justify-content:space-between"><b>${tIcon(d.key)}${esc(d.name)} <span class="muted">${d.start ?? "—"}</span> → ${d.now ?? "—"}</b><span class="muted" style="font-size:12px">${d.hours != null ? d.hours + " 小时 " : ""}›</span></span><span style="font-size:13px">${esc(d.why)}</span><span class="rels">${rel(d)}</span></button>`).join("")}
      <div class="srows">${rest.filter((d) => !d.source).map((d) => `<button type="button" class="srow" data-act="terr" data-k="${d.key}"><span>${esc(d.name)}</span><span>${d.now ?? "—"}${arrow(d)}</span><span class="muted grow">${esc(d.why)}</span><span class="muted">›</span></button>`).join("")}</div>
      ${next}</div></div>`;
}

// 复盘记录：每周日 21:00 和 小伴 在聊天里复盘，这里只看结果（她 2026-11-02）
function reviewsPage() {
  const z = S.season;
  if (!z) return '<p class="loading">加载中……</p>';
  const list = z.reviews || [];
  const li = (xs) => xs.map((x) => `<li>${esc(x)}</li>`).join("");
  return seasonHead("复盘记录") + (list.length ? `<div class="col" style="gap:10px">${list.map((r) => `<section class="panel rvcard">
      <div class="row" style="justify-content:space-between"><b>${esc(r.day.slice(5))} 复盘 · ${r.layers.map(esc).join(" + ")}</b>${r.status === "open" ? '<span class="pill p-none">还在聊</span>' : ""}</div>
      ${r.summary ? `<p>${esc(r.summary)}</p>` : `<p class="muted">小伴 已经开场，你在聊天里答完就有记录</p>`}
      ${r.points.length ? `<span class="muted">要点</span><ul>${li(r.points)}</ul>` : ""}
      ${r.changes.length ? `<span class="muted">定下的调整</span><ul>${li(r.changes)}</ul>` : ""}</section>`).join("")}</div>`
    : `<section class="pastempty"><b>还没有复盘</b><span class="muted">每周日 21:00 小伴 来找你复盘，聊完的结论会出现在这里；每月最后一个周日加月度，赛季最后一个周日加赛季。</span></section>`);
}

// 下个赛季草稿：赛季复盘聊完 小伴 起草，要改跟他说；点两次“按这个开下个赛季”才建（她 2026-11-02）
const CADNAME = { once: "一次", daily: "每天", weekly: "每周", per_milestone: "每阶段一次" };
function draftPage() {
  const z = S.season;
  if (!z) return '<p class="loading">加载中……</p>';
  const d = z.draft;
  if (!d) return seasonHead("下季草稿") + '<section class="pastempty"><b>还没有草稿</b><span class="muted">赛季最后一个周日复盘聊完，小伴 会起草下个赛季放在这里。</span></section>';
  const task = (t) => `<li>${esc(t.title)}<span class="muted"> · ${t.review ? "复盘" : CADNAME[t.cadence] || ""}${t.cadence === "weekly" && t.per_week ? " " + t.per_week + " 次" : ""}${t.habit ? " · 引用 " + esc(t.habit) : ""}</span></li>`;
  return seasonHead("下季草稿", `<span class="muted">${esc(d.name)}　${esc(d.start.slice(5))} – ${esc(d.end.slice(5))}</span>`) + `
    <section class="panel rvcard"><p>${esc(d.summary)}</p>${d.rule ? `<span class="gold">硬线：${esc(d.rule)}</span>` : ""}
      <span class="muted" style="font-size:12px">小伴 ${esc(z.draft_at || "")} 写的。要改哪里直接跟他说，他改好这里就更新。</span></section>
    <div class="draftgrid">${d.goals.map((g) => `<section class="panel rvcard"><b>${esc(g.title)}</b><span class="muted" style="font-size:12px">${esc(g.domain || "")}${g.due ? " · 截止 " + esc(g.due.slice(5)) : ""}${g.from_goal ? " · 接着打" : " · 新开"}${g.agent ? " · 交给 agent" : ""}</span>
      ${g.scope ? `<span style="font-size:13px">管：${esc(g.scope)}</span>` : ""}
      ${g.milestones.map((m) => `<div class="dms"><b style="font-size:13px">${esc(m.title)}</b><span class="muted" style="font-size:12px">${m.due ? esc(m.due.slice(5)) : ""}</span><ul>${m.tasks.map(task).join("")}</ul></div>`).join("")}</section>`).join("")}</div>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn gold" data-act="seasonopen" data-confirm="会结算这一季、建好上面的副本，没接着打的老副本先暂停。再点一次确认">按这个开下个赛季</button></div>`;
}

// 往季回顾：一季一张卡（样稿 C），点开是结算页；不放当前赛季，按年份筛（她 2026-11-02）
function pastPage() {
  const z = S.season;
  if (!z) return '<p class="loading">加载中……</p>';
  const past = z.past || [], years = [...new Set(past.map((x) => x.end.slice(0, 4)))];
  const shown = past.filter((x) => !S.pastYear || x.end.startsWith(S.pastYear));
  return seasonHead("往季回顾") + (past.length ? `<div class="row" style="gap:6px">${["", ...years].map((y) => `<button type="button" class="chip ${(S.pastYear || "") === y ? "on" : ""}" data-act="pastyear" data-v="${y}">${y || "全部"}</button>`).join("")}</div>
    <div class="pastgrid">${shown.map((x) => `<div class="pastcard"><span class="pthumb">${mapImg()}</span><span class="pbody"><b>${esc(x.name)}</b><span class="muted" style="font-size:12px">${esc(x.start)} – ${esc(x.end)}</span><span style="font-size:13px">${esc(x.line || "")}</span>${(x.bosses || []).length ? `<span class="muted" style="font-size:12px">${x.bosses.map((b) => `${esc(b.title.split("：")[0])} ${b.done}/${b.total}`).join(" · ")}</span>` : ""}</span></div>`).join("")}</div>`
    : `<section class="pastempty"><b>还没有过完的赛季</b><span class="muted">${esc(z.season.name)}在 ${esc(z.season.end.slice(5))} 结束，结束后这里会有它的结算：一季一张卡，点开看整季的复盘。</span></section>`);
}


// 财务：本周和上周每天累计花了多少（她 2026-11-02 要的曲线），加本月累计和预算
const clashNote = (e) => e.clash_with && e.clash_with.length ? `<span class="clashnote">和「${e.clash_with.map(esc).join("」「")}」撞时间</span>` : "";

// ---------- 领域页的分析（她 2026-11-02：七个详情页本质上都是这七个领域的分析） ----------
// 分数：开季 → 现在、由哪几项组成、每天的走势；下面是这个领域最能说明问题的 1–2 个数的变化图
function scorePanel(sc) {
  if (!sc) return "";
  const h = sc.history || [], W = 220, H = 44;
  const spark = h.length > 1 ? (() => { const vs = h.map((x) => x.v), lo = Math.min(...vs, 0), hi = Math.max(...vs, 100);
    return `<svg viewBox="0 0 ${W} ${H}" class="spark" aria-hidden="true"><path d="${h.map((x, i) => `${i ? "L" : "M"}${(i * W / (h.length - 1)).toFixed(1)},${(H - 4 - (x.v - lo) / (hi - lo || 1) * (H - 8)).toFixed(1)}`).join(" ")}"/></svg>`; })() : "";
  return `<section class="panel col scorepanel"><div class="row" style="justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
      <h2>这个领域</h2><span class="muted" style="font-size:12px">分数 0–100，按你自己的标准算（最近 30 天）</span></div>
    <div class="row" style="gap:18px;align-items:center;flex-wrap:wrap"><span class="bigscore">${sc.now ?? "—"}</span>
      <span class="col" style="gap:2px"><span class="muted" style="font-size:12px">开季 ${sc.start ?? "—"} → 现在 ${sc.now ?? "—"}${sc.start != null && sc.now != null ? `（<span class="${sc.now < sc.start ? "dn" : "up"}">${sc.now < sc.start ? "↓" : "↑"}${Math.abs(sc.now - sc.start)}</span>）` : ""}</span>
        <span style="font-size:13px">${esc(sc.label || "")}</span></span>${spark}</div>
    <div class="sitems">${sc.items.map((i) => `<div class="sitem"><span>${esc(i.name)}<span class="muted"> · 占 ${i.weight}%</span></span>
      <span class="bar"><i style="width:${i.score ?? 0}%"></i></span><span class="muted">${i.score == null ? "还没有数据" : esc(i.actual) + " / " + esc(i.standard)}</span></div>`).join("")}</div></section>`;
}

function metricChart(c) {
  const pts = c.points || [], W = 320, H = 130, pad = 22;
  if (!pts.length || c.empty) return `<section class="panel col"><h2>${esc(c.title)}</h2><p class="muted" style="font-size:12px">还没有数据${c.note ? "（" + esc(c.note) + "）" : ""}</p></section>`;
  const top = Math.max(1, ...pts.map((p) => p.v), c.target || 0) * 1.15, step = (W - pad * 2) / pts.length;
  const y = (v) => H - pad - v / top * (H - pad * 2);
  const body = c.kind === "line"
    ? `<path d="${pts.map((p, i) => `${i ? "L" : "M"}${(pad + step * (i + .5)).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")}" class="mline"/>${pts.map((p, i) => `<circle cx="${(pad + step * (i + .5)).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="2.5" class="${c.target != null && p.v > c.target ? "over" : "dotm"}"/>`).join("")}`
    : pts.map((p, i) => `<rect x="${(pad + step * i + step * .18).toFixed(1)}" y="${y(p.v).toFixed(1)}" width="${(step * .64).toFixed(1)}" height="${(H - pad - y(p.v)).toFixed(1)}" class="${c.target != null && p.v < c.target && i < pts.length - 1 ? "under" : "barm"}"/>`).join("");
  return `<section class="panel col"><div class="row" style="justify-content:space-between;align-items:baseline"><h2>${esc(c.title)}</h2><span class="muted" style="font-size:12px">${c.target != null ? "标准 " + c.target + " " + esc(c.unit) : ""}</span></div>
    <svg viewBox="0 0 ${W} ${H}" class="mchart" role="img" aria-label="${esc(c.title)}">${c.target != null ? `<line x1="${pad}" x2="${W - pad}" y1="${y(c.target)}" y2="${y(c.target)}" class="tline"/>` : ""}${body}
      ${pts.map((p, i) => i % Math.ceil(pts.length / 7) === 0 ? `<text x="${(pad + step * (i + .5)).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="lbl">${esc(p.x)}</text>` : "").join("")}
      ${pts.map((p, i) => `<text x="${(pad + step * (i + .5)).toFixed(1)}" y="${(y(p.v) - 4).toFixed(1)}" text-anchor="middle" class="lbl">${p.v || ""}</text>`).join("")}</svg>
    ${c.note ? `<span class="muted" style="font-size:11px">${esc(c.note)}</span>` : ""}</section>`;
}

// 财务：每日开销（她 2026-11-02：默认最近 7 天，能选起止日期，给区间总额和保底平均线）
function spendChart(sp) {
  if (!sp) return "";
  const pts = sp.points, W = 320, H = 140, pad = 24;
  const top = Math.max(1, ...pts.map((p) => p.v), sp.floor_avg || 0) * 1.15, step = (W - pad * 2) / Math.max(1, pts.length);
  const y = (v) => H - pad - v / top * (H - pad * 2);
  const inp = "background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:4px 6px;font-size:12px";
  return `<section class="panel col"><div class="row" style="justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px"><h2>每日开销</h2>
      <span class="row" style="gap:6px;align-items:center"><input type="date" value="${esc(sp.start)}" data-spend="start" style="${inp}"><span class="muted">至</span><input type="date" value="${esc(sp.end)}" data-spend="end" style="${inp}"></span></div>
    <div class="row" style="gap:16px;align-items:baseline;flex-wrap:wrap"><span><span class="muted" style="font-size:12px">这段时间一共</span> <b style="font-size:20px">${sp.cur}${sp.total.toLocaleString()}</b></span>
      <span class="muted" style="font-size:12px">平均每天 ${sp.cur}${sp.avg.toLocaleString()}${sp.floor_avg ? ` · 保底每天 ${sp.cur}${sp.floor_avg.toLocaleString()}` : " · 还没定每月预算"}</span></div>
    <svg viewBox="0 0 ${W} ${H}" class="mchart" role="img" aria-label="每日开销">
      ${sp.floor_avg ? `<line x1="${pad}" x2="${W - pad}" y1="${y(sp.floor_avg)}" y2="${y(sp.floor_avg)}" class="tline"/>` : ""}
      ${pts.map((p, i) => `<rect x="${(pad + step * i + step * .15).toFixed(1)}" y="${y(p.v).toFixed(1)}" width="${(step * .7).toFixed(1)}" height="${(H - pad - y(p.v)).toFixed(1)}" class="spendbar"/>`).join("")}
      ${pts.map((p, i) => i % Math.ceil(pts.length / 7) === 0 ? `<text x="${(pad + step * (i + .5)).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="lbl">${esc(p.x.slice(5))}</text>` : "").join("")}</svg>
    <span class="muted" style="font-size:11px">${sp.floor_avg ? "每根柱子是那天花了多少；虚线是保底平均线（每月预算按天折）" : "每根柱子是那天花了多少。定了每月预算以后会画保底平均线"}</span></section>`;
}

function weightChart(ws) {
  if (!ws || ws.length < 2) return '<p class="muted" style="font-size:12px">还没有体重记录。训练前称一下告诉 小伴，他会记，攒满一周就能看到曲线。</p>';
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
  const back = backBtn(S.terrBack === "bigmap" ? "地图" : ({ brain: "资料", main: "今日", quests: "任务" })[S.terrBack] || "赛季", 'data-act="terrback"');
  if (t.key === "waste") {
    return `<header class="row" style="flex-wrap:wrap"><h2 style="font-size:22px">荒原</h2><span class="pill p-none">${t.tasks.length} 件没归类</span><span style="margin-left:auto">${back}</span></header>
      <span class="muted" style="font-size:13px">还没找到家的待办都在这。小伴 按标题先猜了一块，你点头或改掉，归好的就回到那块领域。</span>
      <section class="panel col scrollbox tall">${t.tasks.map((x) => `<div class="item"><span class="grow">${esc(x.title)}${x.due ? `<span class="muted" style="display:block;font-size:11px">${esc(x.due)}</span>` : ""}</span>
        ${x.guess ? `<span class="muted" style="font-size:11px;color:var(--accent)">小伴 猜：${esc(TERRS.find((r) => r[0] === x.guess)[1])}</span>` : ""}
        <select data-change="domain" data-id="${x.id}" aria-label="归到哪个领域"><option value="">归到……</option>${TERRS.map(([k, n]) => `<option value="${k}"${k === x.guess ? " selected" : ""}>${n}</option>`).join("")}</select>
        ${x.guess ? `<button type="button" class="btn gold" data-act="domain-ok" data-id="${x.id}" data-k="${x.guess}">确认</button>` : ""}</div>`).join("") || '<p class="muted">荒原清空啦，每件事都有家了。</p>'}</section>`;
  }
  const summary = t.light === "warn" ? `${t.rows.filter((r) => r.light === "warn").map((r) => r.name).join("、")}落后于副本标准，保底线守住了。`
    : t.light === "bad" ? `${t.rows.filter((r) => r.light === "bad").map((r) => r.name).join("、")}跌破了保底线。`
    : t.light === "ok" ? "这周都在线上。" : "这块还没有数据来源，接上一个亮一个。";
  const rows = t.rows.map((r) => `<tr><td>${esc(r.name)}${r.note ? `<span class="muted" style="display:block;font-size:11px">${esc(r.note)}</span>` : ""}</td><td>${esc(r.quest)}</td><td>${esc(r.floor)}${r.edit === "budget" ? ` <button type="button" class="pen" data-act="budgetedit" aria-label="改每月预算" title="改每月预算">✎</button>` : ""}</td><td>${esc(r.actual)}${r.log ? ` <button type="button" class="pen" data-act="lifelogopen" data-kind="${r.log}" aria-label="记一笔" title="记一笔">＋</button>` : ""}</td><td><span class="pill p-${r.light}">${r.light === "none" && r.actual && r.actual !== "—" ? "不判灯" : LIGHT[r.light]}</span></td></tr>`).join("");
  const bosses = t.bosses.map((b, i) => `<div class="bossrow"><button type="button" class="boss ${b.main ? "" : "alt"}" data-act="boss" data-id="${b.goal_id}">
      <div class="name"><b style="font-size:13px">${b.main ? "★ " : ""}${esc(b.name)}${b.main ? "（主副本）" : ""}</b><span class="muted" style="font-size:11px">${esc(b.line)}</span></div>
      <div class="bar"><i style="width:${Math.round(100 * (b.frac || 0))}%"></i></div>
      <div class="gold" style="font-size:11px">${b.next ? "下一步：" + esc(b.next) : ""} ▸</div></button>${i > 0 ? `<button type="button" class="up" data-act="goal-up" data-id="${b.goal_id}" aria-label="${esc(b.name)}往前挪" title="排到前面">↑ 提前</button>` : ""}</div>`).join("") || '<p class="muted" style="font-size:12px">这个赛季没有副本，只看健康线。</p>';
  const habits = t.habits.map((h) => h.kind === "daily"
    ? `<button type="button" class="chip ${h.done ? "on" : ""}" data-act="habit" data-id="${h.id}" data-done="${h.done ? 1 : ""}">${h.done ? "✓ " : ""}${esc(short(h.title))}</button>`
    : `<button type="button" class="chip ${h.count >= h.target ? "on" : ""}" data-act="habit" data-id="${h.id}" data-done="${h.today ? 1 : ""}">${esc(short(h.title))} ${h.count}/${h.target}</button>`).join("");
  const tasks = t.tasks.map((x) => `<div class="item"><button type="button" class="tick" data-act="task" data-id="${x.id}" data-done="" aria-label="完成：${esc(x.title)}"></button><span class="grow">${esc(x.title)}</span><span class="muted" style="font-size:11px">${x.main ? "赛季副本" : x.due ? esc(x.due) : "其他"}</span></div>`).join("") || '<p class="muted" style="font-size:12px">没有待办</p>';
  const ex = t.explore;
  const money = (v, c) => (c === "GBP" ? "£" : c === "CNY" ? "¥" : c + " ") + Math.abs(v).toFixed(2);
  const bp = t.budget || {}, yuan = (v) => "¥" + Number(v).toLocaleString();
  // 预算平时收起来，点开再改（她 2026-11-01：没必要固定占位置）
  const extra = t.key === "money" ? spendChart(t.spend) + `<section class="panel col"><h2>最近的账</h2><div class="scrollbox">${(t.transactions || []).map((x) => `<div class="item"><span class="grow">${esc(x.merchant || "（没认出商户）")}
        <span class="muted" style="display:block;font-size:11px">${esc(x.day.slice(5))} · ${esc({ monzo: "Monzo", cmb: "招行", bocom: "交行", chat: "小伴 记的" }[x.source] || x.source)}${x.category === "refund" ? " · 退款" : x.category === "transfers" ? " · 转账，不算花销" : x.category === "unparsed" ? " · 没认出来，不算钱" : x.category === "ignored" ? " · 你标了不算" : ""}</span></span>
        <b style="color:${x.amount > 0 ? "var(--ok, #6fcf97)" : "inherit"};${x.category === "ignored" ? "text-decoration:line-through;opacity:.5" : ""}">${x.amount > 0 ? "+" : x.amount < 0 ? "−" : ""}${money(x.amount, x.currency)}</b>
        ${x.category === "unparsed" ? "" : `<button type="button" class="btn" data-act="money-ignore" data-id="${x.id}" data-on="${x.category === "ignored" ? "" : 1}">${x.category === "ignored" ? "算回来" : "不算"}</button>`}</div>`).join("") || '<p class="muted" style="font-size:12px">还没有记录</p>'}</div></section>`
    : t.key === "body" ? `<section class="panel col"><h2>体重</h2>${weightChart(t.weights)}<span class="muted" style="font-size:11px">金线是 7 天平均体重，虚线是本赛季阶段目标。</span></section>`
    : t.key === "life" && ex ? `<section class="panel col"><div class="row" style="justify-content:space-between"><h2>探索地图</h2><button type="button" class="btn small" data-act="exploreopen">✎ 编辑</button></div><div class="row" style="gap:18px;flex-wrap:wrap">
        <span><b class="big2">${ex.countries}</b> <span class="muted">个国家已点亮</span></span><span><b class="big2">${ex.china}</b> <span class="muted">处国内去过</span></span>
        <span><b class="big2">${ex.goals_done}</b><span class="muted"> / ${ex.goals_total} 条人生 Goal 完成，${ex.goals_doing} 条进行中</span></span></div></section>` : "";
  return `<header class="row" style="flex-wrap:wrap;gap:12px"><span class="crest">${tIcon(t.key)}</span>
      <span style="min-width:0"><h2 style="font-size:22px">${esc(t.name)} <span class="pill p-${t.light}" style="font-size:12px;vertical-align:3px">${LIGHT[t.light]}</span></h2>
      <span class="muted" style="font-size:13px">${summary}</span></span><span style="margin-left:auto;align-self:flex-start">${back}</span></header>
    <div class="cols2">
      <div class="col">${scorePanel(t.score)}${(t.charts || []).map(metricChart).join("")}<section class="panel col"><div class="row" style="justify-content:space-between;flex-wrap:wrap"><h2>健康线 · 本周</h2><span class="muted" style="font-size:11px">自动计算，不用打卡</span></div>
        <div style="overflow-x:auto"><table class="htbl fixed"><colgroup><col style="width:25%"><col style="width:21%"><col style="width:20%"><col style="width:16%"><col style="width:18%"></colgroup><thead><tr><th>项</th><th>副本标准</th><th>保底</th><th>本周</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></section>${extra}</div>
      <div class="col"><section class="panel col"><h2>副本</h2>${bosses}</section>
        <!-- 领域页只留副本列表和资料，日常和待办首页有（她 2026-11-02） -->
        <section class="panel col"><div class="row" style="justify-content:space-between"><h2>资料</h2><button type="button" class="btn" data-act="brain-terr" data-k="${t.key}">看这块的全部资料 ▸</button></div>
          <div class="scrollbox">${(t.brain || []).map((x) => `<div class="item"><span class="grow">${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>` : esc(x.title)}
            <span class="muted" style="display:block;font-size:11px">${esc(x.kind_label)}${x.source ? " · " + esc(x.source) : ""}</span></span></div>`).join("") || '<p class="muted" style="font-size:12px">这块还没有资料</p>'}</div></section></div></div>`;
}

/* ---------- 回顾：领地地图的第二个标签（数字全由代码算，ethan/review.py） ---------- */
function reviewPage() {
  const r = S.review;
  const head = `<header class="row" style="flex-wrap:wrap"><h2 style="font-size:22px">世界</h2>${mapTabs()}</header>`;
  if (!r) return head + '<p class="loading">加载中……</p>';
  const past = r.weeks.slice(1);
  return head + `
    <section class="panel col"><div class="row" style="justify-content:space-between;flex-wrap:wrap"><h2>${esc(r.season.name)} · 赛季结算</h2><span class="muted">剩 ${r.season.days_left} 天</span></div>
      <span class="muted" style="font-size:12px">从 ${esc(r.season.start)} 算起，已过完 ${r.season.weeks} 周 · 赛季累计经验 ${r.season.points}（含本周）· 满勤 ${r.season.full_days} 天</span>
      ${r.season.weeks ? wTally(r.season.territories) : '<span class="muted" style="font-size:12px">第一周还没过完，过完就有结算。本周的回顾在“总览”里。</span>'}</section>
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
  const fine = (x) => `<span class="pill ${x ? "p-ok" : "p-bad"}">${x ? "正常" : "有问题"}</span>`;  // 体检用“有问题”，不说“没接上”（11-04）
  const SAIDN = { her_words: "你说的", her_confirmation: "你确认的", her_provided: "你给的资料", behavior: "行为记录", other_person: "别人说的",
                  system_inference: "系统推的", agent_relay: "转述", unknown: "来源没记" };
  const m = c.memory;
  const memPanel = m ? `<section class="panel col"><div class="row" style="justify-content:space-between"><h2>记忆</h2><button type="button" class="btn" data-act="go" data-page="me">去“我”页看 ▸</button></div>
      ${m.on_hold ? `<p class="hbad">记忆暂停中：${esc(m.on_hold)}。除了你本人，谁都查不到记忆，等你确认后放开。</p>` : ""}
      <div class="item"><span class="grow">关于你的记忆<span class="muted" style="display:block;font-size:11px">${Object.entries(m.by_source || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${esc(SAIDN[k] || k)} ${n}`).join("　")}</span></span><b style="font-size:18px">${m.facts}</b></div>
      <div class="item"><span class="grow">文件整理<span class="muted" style="display:block;font-size:11px">你发过的文件每晚整理 15 份：已整理 ${m.files.done} / ${m.files.files}，其中关于你的 ${m.files.about_her} 份，记下 ${m.files.facts} 条</span></span><span class="pill ${m.files.files && m.files.done >= m.files.files ? "p-ok" : "p-none"}">${!m.files.files ? "还没有文件" : m.files.done >= m.files.files ? "整理完了" : "整理中"}</span></div>
      <div class="item"><span class="grow">按意思找（语义检索）<span class="muted" style="display:block;font-size:11px">${m.semantic ? "开着：换个说法也能找到" : "还没开：服务器装好模型、量过内存后打开"}</span></span><span class="pill ${m.semantic ? "p-ok" : "p-none"}">${m.semantic ? "开着" : "没开"}</span></div>
    </section>` : "";
  const sys = c.system || {};
  const tab = S.setTab || "set";
  const seg = `<span class="modeseg" role="group" aria-label="设置页"><button type="button" class="${tab === "set" ? "on" : ""}" data-act="settab" data-v="set" aria-pressed="${tab === "set"}">设置</button><button type="button" class="${tab === "sys" ? "on" : ""}" data-act="settab" data-v="sys" aria-pressed="${tab === "sys"}">系统体检${sys.bad ? ` <i class="navdot inline">${sys.bad}</i>` : ""}</button></span>`;
  const head = `<header class="row sethead"><h2 style="font-size:22px">设置</h2>${seg}</header>`;
  if (tab === "sys") return head + sysPage(c);
  return head + `
    <div class="cols2 fitbody">
      <div class="col">
        <section class="panel col"><h2>提醒</h2>
          <div class="item"><span class="grow">小伴 主动找你<span class="muted" style="display:block;font-size:11px">早报、提醒、想你了来找你</span></span>
            <span class="pill ${c.prefs.proactive ? "p-ok" : "p-none"}">${c.prefs.proactive ? "开着" : "关着"}</span>
            <button type="button" class="btn" data-act="setpro" data-v="${c.prefs.proactive ? "" : "1"}">${c.prefs.proactive ? "关掉" : "打开"}</button></div>
          <form class="item" data-form="quiet"><span class="grow">免打扰时段<span class="muted" style="display:block;font-size:11px">这段时间他不主动发消息（${esc(c.prefs.timezone)}）</span></span>
            <input name="quiet" value="${esc(c.prefs.quiet)}" style="width:130px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 8px"><button class="btn">保存</button></form>
        </section>
        <section class="panel col fitscroll"><h2>这套系统好不好用</h2>
          <button type="button" class="item sysjump" data-act="settab" data-v="sys"><span class="grow">${sys.bad ? `<b class="sysbad">${sys.bad} 处要看</b>` : "系统体检：一切正常"}<span class="muted" style="display:block;font-size:11px">定时任务、来源、后台整理、部署、内存、红点自检都在这一页 ▸</span></span></button>
          ${(c.metrics || []).length ? `<h3 class="smh">本周</h3><div class="smet">${c.metrics.map((m) => `<div class="sm ${m.better === null ? "" : m.better ? "up" : "down"}">
            <b>${m.this_week}</b><span>${esc(m.name)}</span><small>上周 ${m.last_week}${m.better === null ? "" : m.better ? " · 变好了" : " · 变差了"}</small></div>`).join("")}</div>
          <span class="muted" style="font-size:11px">从聊天和后台记录自动算，变差了开发会话去修，不用你盯。</span>` : ""}
          <h3 class="smh">每天该有的</h3>
          ${[...c.health].sort((a, b) => a.ok - b.ok).map((h) => `<div class="item"><span class="grow">${esc(h.name)}<span class="muted" style="display:block;font-size:11px">${esc(h.detail)}</span></span>${fine(h.ok)}</div>`).join("")}
        </section>
      </div>
      <div class="col">${memPanel}<section class="panel col fitscroll"><h2>数据连接</h2>${c.connections.map((x) => `<div class="item"><span class="grow">${esc(x.name)}<span class="muted" style="display:block;font-size:11px">${esc(x.detail)}${!x.ok && x.fix ? " · 怎么接：" + esc(x.fix) : ""}</span></span>${!x.ok && x.link ? `<a class="btn gold" href="${esc(x.link)}">去授权</a>` : ""}${ok(x.ok)}</div>`).join("")}</section></div>
    </div>`;
}

/* ---------- 系统体检一屏（她 11-04：“系统页是不是也得对应着升级一下？”，数据见 ethan/syscheck.py） ---------- */
const JOBST = { ok: ["p-ok", "正常"], running: ["p-ok", "在跑"], waiting: ["p-none", "等第一次"], late: ["p-bad", "超时没跑"],
                failed: ["p-bad", "失败"], stuck: ["p-bad", "卡住了"], never: ["p-bad", "从没跑过"] };
function meter(done, total, cls = "") {
  const pct = total ? Math.min(100, Math.round(done * 100 / total)) : 0;
  return `<span class="bar sysbar ${cls}" role="img" aria-label="${done} / ${total}"><i style="width:${pct}%"></i></span>`;
}
function sysPage(c) {
  const y = c.system || {};
  if (y.error) return `<section class="panel"><p class="hbad">体检没算出来：${esc(y.error)}</p></section>`;
  const jobs = y.jobs || [], d = y.deploy || {}, mem = y.memory, bl = y.backlog, src = y.sources || [], feeds = y.feeds || [], checks = y.checks || [];
  const badJobs = jobs.filter((j) => !j.ok), stale = src.filter((s) => s.state === "stale"), badChecks = checks.filter((x) => !x.ok);
  const badFeeds = feeds.filter((f) => !f.ok);
  const issues = [
    ...badJobs.map((j) => `${j.name}${(JOBST[j.state] || [, ""])[1]}`),
    ...(d.has && !d.ok ? [`最近一次上线没成（${d.step}）`] : []), ...(!d.check_ok ? ["自动上线检查停了"] : []),
    ...stale.map((s) => `${s.name}断了`), ...(bl && (bl.talks_stuck || bl.people_stuck) ? ["后台整理卡住了"] : []),
    ...(mem && mem.level === "critical" ? ["服务器内存快用完了"] : []), ...badChecks.map((x) => `红点自检「${x.name}」对不上`),
    ...badFeeds.map((f) => f.name), ...(y.other || []).map((o) => o.what)];
  const verdict = issues.length
    ? `<p class="sysverdict bad"><b>${issues.length} 处要看：</b>${esc(issues.slice(0, 4).join("；"))}${issues.length > 4 ? " 等" : ""}。巡检每 3 小时看一次，能修的自己修，要你做的会告诉你。</p>`
    : `<p class="sysverdict">${jobs.length} 个定时任务、${src.filter((s) => s.state !== "not_connected").length} 个数据来源都正常，上次上线成功。</p>`;
  const row = (cls, name, mid, right) => `<div class="sysrow ${cls}"><span class="sname">${name}</span><span class="smid">${mid}</span>${right}</div>`;
  const jobRows = [...jobs].sort((a, b) => a.ok - b.ok).map((j) => {
    const st = JOBST[j.state] || ["p-none", j.state];
    return row(j.ok ? "" : "bad", `${esc(j.name)}<small>${esc(j.every)}</small>`,
      j.ok ? (j.state === "running" ? esc(j.detail) : `<span>上次 ${esc(j.last || "—")}</span><span>下次 ${esc(j.next || "—")}</span>`) : esc(j.detail),
      `<span class="pill ${st[0]}">${st[1]}</span>`);
  }).join("");
  const memPct = mem ? Math.round(mem.available_mb * 100 / mem.total_mb) : 0;
  const memBox = mem ? `<div class="sysmem ${mem.level}"><div class="row" style="justify-content:space-between;align-items:baseline"><span>可用内存</span><b>${mem.available_mb} MB<small> / ${mem.total_mb} MB</small></b></div>
      <span class="bar sysbar memb" role="img" aria-label="可用 ${mem.available_mb} MB"><i style="width:${memPct}%"></i><em style="left:${Math.round(300 * 100 / mem.total_mb)}%" title="低于 300MB 标黄"></em></span>
      <small class="muted">${mem.level === "ok" ? "够用" : mem.level === "low" ? "偏紧：低于 300MB，先归档用完的服务器会话" : "快用完了：低于 200MB，巡检会报"}${mem.swap_total_mb ? ` · 交换区用了 ${mem.swap_used_mb} MB` : ""}</small></div>`
    : '<p class="muted" style="font-size:12px">这台机器读不到内存（不是服务器）</p>';
  const blBox = bl ? `
      <div class="sysprog${bl.talks_stuck ? " bad" : ""}"><div class="row"><span class="grow">微信对话</span><b>${bl.wechat.done} / ${bl.wechat.total}</b></div>${meter(bl.wechat.done, bl.wechat.total)}
        <small class="muted">${bl.talks_left ? `还有 ${bl.talks_left} 份对话等整理` : "都整理完了"}${bl.last_digest ? ` · 最近一次进展 ${esc(bl.last_digest)}` : ""}${bl.talks_stuck ? " · 超过 2 小时没动" : ""}${bl.talks_given_up ? ` · ${bl.talks_given_up} 份试了 3 次没成，放弃了` : ""}</small></div>
      <div class="sysprog${bl.people_stuck ? " bad" : ""}"><div class="row"><span class="grow">人物卡</span><b>${bl.people.done} / ${bl.people.total}</b></div>${meter(bl.people.done, bl.people.total)}
        <small class="muted">${bl.people.left ? `还有 ${bl.people.left} 张要整理${bl.talks_left ? "（等对话整理完）" : ""}` : "都整理完了"}${bl.last_people ? ` · 最近一次进展 ${esc(bl.last_people)}` : ""}${bl.people_stuck ? " · 超过 2 小时没动" : ""}</small></div>` : '<p class="muted">没查成</p>';
  const depBox = d.has ? `<div class="sysdep ${d.ok ? "" : "bad"}"><div class="row" style="gap:8px;align-items:baseline"><code class="dcommit">${esc(d.commit)}</code><span class="pill ${d.ok ? "p-ok" : "p-bad"}">${d.ok ? "已上线" : "没上成"}</span><span class="muted" style="margin-left:auto;font-size:12px">${esc(d.at)}</span></div>
      <span class="dsub">${esc(d.subject || "")}</span>
      ${d.ok ? "" : `<small>卡在「${esc(d.step)}」，已退回原版本${d.failed_tests.length ? "。没过的测试：" + d.failed_tests.map((t) => `<code>${esc(t)}</code>`).join(" ") : ""}</small>`}
      ${d.migrations ? `<small class="${d.migrations_ok ? "muted" : "sysbadt"}">数据改动：${esc(d.migrations.slice(-120))}</small>` : ""}
      <small class="${d.check_ok ? "muted" : "sysbadt"}">${d.checked ? `每 2 分钟看一次 main，上次 ${esc(d.checked)}` : "每 2 分钟看一次 main"}${d.check_error ? "：" + esc(d.check_error) : ""}</small></div>`
    : '<p class="muted" style="font-size:12px">还没有部署记录</p>';
  const srcRows = [...src].sort((a, b) => (b.state === "stale") - (a.state === "stale") || (a.state === "not_connected") - (b.state === "not_connected")).map((x) => {
    const st = { ok: ["p-ok", "正常"], stale: ["p-bad", "断了"], not_connected: ["p-none", "没接上"] }[x.state] || ["p-none", x.state];
    const when = x.last ? (x.days > 0 ? `${x.days} 天前` : x.days < 0 ? "排到以后" : "今天") : "还没有";
    return row(x.state === "stale" ? "bad" : x.state === "not_connected" ? "dim" : "", `${esc(x.name)}<small>${x.every ? `平时 ${x.every} 天内` : "不定期，不报断档"}</small>`,
      `最近 ${when}${x.facts_this_month ? ` · 本月 ${x.facts_this_month} 条` : ""}`, `<span class="pill ${st[0]}">${st[1]}</span>`);
  }).join("");
  const feedRows = feeds.map((f) => row(f.ok ? "" : "bad", esc(f.name), esc(f.detail), f.ok ? "" : '<span class="pill p-bad">要看</span>')).join("");
  // 对得上的收成一排小牌子，对不上的单独一行写清哪里不对
  const checkRows = badChecks.map((x) => row("bad", esc(x.name), x.items.map(esc).join("；"), '<span class="pill p-bad">不对</span>')).join("")
    + (checks.length > badChecks.length ? `<div class="syschips">${checks.filter((x) => x.ok).map((x) => `<span class="pill p-ok">✓ ${esc(x.name)}</span>`).join("")}</div>` : "");
  const other = (y.other || []).map((o) => row("bad", esc(o.what), esc(o.detail), '<span class="pill p-bad">要看</span>')).join("");
  return `${verdict}
    <div class="sysgrid fitbody">
      <section class="panel col fitscroll sysjobs"><h2>定时任务 <small class="muted">${jobs.length - badJobs.length} / ${jobs.length} 正常</small></h2>${jobRows}${other ? `<h3 class="smh">别的</h3>${other}` : ""}</section>
      <div class="col syscol">
        <section class="panel col"><h2>后台大批整理</h2>${blBox}</section>
        <section class="panel col"><h2>最近上线</h2>${depBox}</section>
        <section class="panel col"><h2>服务器</h2>${memBox}</section>
      </div>
      <div class="col syscol">
        <section class="panel col fitscroll syssrc"><h2>数据来源 <small class="muted">${stale.length ? `${stale.length} 个断了` : "按各自频率看有没有断"}</small></h2>${srcRows}</section>
        <section class="panel col fitscroll"><h2>主动找你、派活和导出</h2>${feedRows}</section>
        <section class="panel col syschk"><h2>红点自检 <small class="muted">${checks.length ? (badChecks.length ? `${badChecks.length} 项对不上` : "都对得上") : ""}</small></h2>${checkRows || '<p class="muted" style="font-size:12px">没跑</p>'}</section>
      </div>
    </div>
    <p class="muted sysat">体检于 ${esc(y.at || "")}（伦敦时间）· 和每 3 小时的巡检看同一份</p>`;
}

/* ---------- 日记：每天一张今日卡，就是那一天的日记（2026-10-31 她定，原来的卡册） ---------- */
function dayNOf(day) {
  const st = S.state, n = st && st.profile && st.profile.day_n;
  if (!n || !st.day) return null;
  return n - Math.round((new Date(st.day + "T12:00:00") - new Date(day + "T12:00:00")) / 86400000);
}
// 进度状态单独上色（她 2026-11-01：金色用得太多，“落后”显不出来）：落后用警示色加粗，领先、达成用绿色，正常不上色
function bossPill(b) {
  // 每个副本右下角同一个样子（2026-11-01 她说一个写“正常”、两个只有小箭头很奇怪）：有指标的写领先/正常/落后，没指标的写主线走到第几步
  const st = (b.status || "").replace(/[^\u4e00-\u9fa5]/g, "");
  const txt = st || (b.steps_total ? `${b.steps_done}/${b.steps_total} 步` : "待拆步骤");
  const cls = st === "落后" ? "lag" : st === "领先" || st === "达成" ? "ok" : "";
  return `<span class="bosspill ${cls}">${esc(txt)} ›</span>`;
}

function statusHtml(text) {
  return esc(text || "").replace(/⚠️\s*落后/g, '<b class="lagc">落后</b>').replace(/🔥\s*领先/g, '<b class="okc">领先</b>')
    .replace(/✅\s*达成/g, '<b class="okc">达成</b>');
}

function cardsPage() {
  // 日记有两种看法（她 2026-11-01 定）：日记本（一页一天，从最新那天开始）和卡册（按稀有度分档、能筛）
  const c = S.cards;
  if (!c) return '<p class="loading">加载中……</p>';
  let mode = S.diaryMode;
  if (!mode) mode = S.diaryMode = "book";  // 每次进来都是日记本（她 2026-11-01），卡册要她自己切
  const seg = `<span class="modeseg" role="group" aria-label="看法"><button type="button" class="${mode === "book" ? "on" : ""}" data-act="diarymode" data-v="book" aria-pressed="${mode === "book"}">日记本</button><button type="button" class="${mode === "album" ? "on" : ""}" data-act="diarymode" data-v="album" aria-pressed="${mode === "album"}">卡册</button></span>`;
  const head = `<header class="diaryhead"><h2>日记</h2>${seg}${backBtn("今日", 'data-act="go" data-page="main"')}</header>`;
  return head + (mode === "album" ? albumView(c) : bookView(c));
}

function dayLabel(day) {
  const k = dayNOf(day), d = new Date(day + "T12:00:00");
  return { n: k && k > 0 ? `第 ${k} 天` : "开始之前", date: `${d.getMonth() + 1} 月 ${d.getDate()} 日 周${"日一二三四五六"[d.getDay()]}` };
}

function albumView(c) {
  // 卡册：2026-10-29 她要能筛、按稀有度从高到低一行一档；月份多了用下拉（2026-11-01）
  const f = S.cardF || (S.cardF = { r: "", k: "", m: "" });
  const months = [...new Set(c.cards.map((x) => x.day.slice(0, 7)))].sort().reverse();
  const chip = (field, v, label) => `<button type="button" class="chip ${f[field] === v ? "on" : ""}" data-act="cardf" data-f="${field}" data-v="${v}">${label}</button>`;
  const shown = c.cards.map((x, i) => ({ ...x, i })).filter((x) => (!f.r || x.rarity === f.r) && (!f.k || x.kind === f.k) && (!f.m || x.day.startsWith(f.m)));
  const one = (x) => `<button type="button" class="ccard ${RAR(x.rarity)}" data-act="cardopen" data-i="${x.i}">
      <img src="${imgUrl(x.image)}" alt="${esc(x.day)} 的卡：${esc(x.title)}" loading="lazy">
      <span class="cap"><b>${esc(x.title)}</b><span>${dayLabel(x.day).n} · ${esc(x.day.slice(5))}</span></span></button>`;
  const rows = ["传说", "超稀有", "稀有", "普通"].map((r) => [r, shown.filter((x) => x.rarity === r).sort((a, b) => (a.day < b.day ? 1 : -1))]).filter(([, l]) => l.length)
    .map(([r, l]) => `<section class="col" style="gap:8px"><h2 style="font-size:15px">${r} <span class="muted" style="font-weight:400;font-size:12px">${l.length} 张</span></h2><div class="cardgrid">${l.map(one).join("")}</div></section>`).join("");
  const queued = (c.wishes || []).length;
  return `<div class="albumbar"><span class="muted" style="font-size:13px">${c.cards.length} 张</span>
      <div class="chips">${chip("r", "", "全部级别")}${["传说", "超稀有", "稀有", "普通"].map((r) => chip("r", r, r)).join("")}</div>
      <div class="chips">${chip("k", "", "全部类型")}${chip("k", "日常", "天气日常")}${chip("k", "训练", "训练日")}${chip("k", "节点", "升级和满勤")}${chip("k", "点单", "我点的")}</div>
      <select class="monthpick" data-monthpick aria-label="按月份筛"><option value="">全部月份</option>${months.map((m) => `<option value="${m}" ${f.m === m ? "selected" : ""}>${m.slice(0, 4)} 年 ${Number(m.slice(5))} 月</option>`).join("")}</select>
      <button type="button" class="btn" style="margin-left:auto" data-act="cardwishopen">定制一张卡${queued ? `（${c.drawing ? "正在画" : "排队 " + queued}）` : ""}</button></div>
    ${rows || `<p class="muted">${c.cards.length ? "没有符合的卡，换个条件试试。" : "卡册还空着。每天翻开今日卡，它就会住进来。"}</p>`}`;
}

function bookView(c) {
  // 日记本：一页一天，从最新的一天（今天）开始，往前、往后翻
  const list = c.cards.map((x, i) => ({ ...x, i })).sort((a, b) => (a.day < b.day ? 1 : -1));
  if (!list.length) return '<p class="muted">还没有日记。每天翻开今日卡，那一天就会记在这里。</p>';
  const at = Math.min(Math.max(0, S.diaryAt || 0), list.length - 1), x = list[at], L = dayLabel(x.day);
  const turn = S.diaryTurn ? ` turn-${S.diaryTurn}` : "";
  S.diaryTurn = null;
  return `<div class="dbook2">
    <button type="button" class="dturn2" data-act="diaryturn" data-v="${at + 1}" ${at + 1 >= list.length ? "disabled" : ""} aria-label="往前翻一天">‹<small>前一天</small></button>
    <article class="spread${turn}" aria-label="${L.n} ${L.date} 的日记">
      <div class="leaf left">
        <button type="button" class="snap ${RAR(x.rarity)}" data-act="cardopen" data-i="${x.i}" aria-label="看大卡"><img src="${imgUrl(x.image)}" alt="${esc(x.day)} 的卡：${esc(x.title)}"><i class="tape a"></i><i class="tape b"></i></button>
        <p class="snapcap">${esc(x.title)}${x.rarity && x.rarity !== "普通" ? ` · ${esc(x.rarity)}` : ""}</p>
        ${x.line ? `<p class="margin">小伴：${esc(x.line)}</p>` : ""}
      </div>
      <div class="leaf right">
        <header class="pagehead"><b>${L.n}</b><span>${L.date}</span>${S.diaryEdit === x.day ? "" : `<span class="dtools">${x.diary ? `<button type="button" class="pen ${S.diaryTool === "hl" ? "on" : ""}" data-act="diarytool" data-v="hl" title="荧光笔：点了以后在句子上划一下" aria-pressed="${S.diaryTool === "hl"}">🖍</button><button type="button" class="pen ${S.diaryTool === "note" ? "on" : ""}" data-act="diarytool" data-v="note" title="批注：划一句写评论，小伴 只改这一句" aria-pressed="${S.diaryTool === "note"}">💬</button>` : ""}<button type="button" class="pen" data-act="diaryedit" data-day="${esc(x.day)}" aria-label="改这一天的日记" title="改这一天的日记">✎</button></span>`}</header>
        ${S.diaryEdit === x.day ? diaryEditor(x) : `<div class="ink ${S.diaryTool ? "tool-" + S.diaryTool : ""}" data-day="${esc(x.day)}">${x.diary ? diaryHtml(x.diary, x.marks, x.notes) : `<p>${x.diary_mine ? "这一天你留空了。" : x.day === (S.state && S.state.day) ? "今天的日记今晚自动写好；也可以点右上角 ✎ 自己先写。" : "这一天的日记正在补写，过一会儿再来看；也可以点右上角 ✎ 自己写。"}</p>`}</div>`}
        ${(x.notes || []).length && S.diaryEdit !== x.day ? `<div class="dnotes">${x.notes.map((n) => `<div class="dnote ${n.status}"><span>💬 ${esc(n.note)}</span><span class="muted">${n.status === "done" ? `小伴 改成了：${esc(n.new_text || "（删掉了这句）")}` : n.status === "failed" ? esc(n.new_text || "没改成") : "小伴 在改……"}</span></div>`).join("")}</div>` : ""}
        <footer class="pagefoot">${list.length - at} / ${list.length}</footer>
      </div>
    </article>
    <button type="button" class="dturn2" data-act="diaryturn" data-v="${at - 1}" ${at <= 0 ? "disabled" : ""} aria-label="往后翻一天">›<small>后一天</small></button>
  </div>`;
}

// 全局交互规范（她 2026-11-01，docs/design/workbench.md“交互规范”）：弹窗和页面每次打开都是默认状态——
// 默认标签、不在编辑、没有筛选；没保存的编辑在离开时自动存（日记、副本步骤）或丢弃（弹窗里的表单）。
function resetOverlayState() {
  if (S.overlay === null && S.noteDraft) { S.noteDraft = null; if (S.diaryTool === "note") { S.diaryTool = null; setTimeout(render, 0); } }  // 批注框关了（取消）也退出批注模式
  if (S.planDirty) {  // 她 2026-11-02：删了一个约还要点“让 AI 重排”——改了日程就自动重排，不用按钮
    S.planDirty = false;
    send("plan.replan", {}).then((r) => { if (!r.ok) return showUndo("待办没重排上：" + r.msg); showUndo("日程改了，小伴 在按新日程重排今天的待办"); load(true); });
  }
  if (S.editGoal != null) { S.editGoal = null; flushSteps(true); }  // 关副本详情弹窗时编辑自动存上并收起
  S.goalEdit = null; S.goalEditWhat = null; S.deskTab = null; S.prepTab = ""; S.prepOpen = null;
  S.exploreSheet = null; S.exploreQ = ""; S.exploreTag = "未开始";  // 默认只看还没去的（她 2026-11-02）
  S.exploreSort = ""; S.exploreEdit = null; S.renameId = null;
}

// 离开页面、翻页、切日记本 / 卡册时：日记和副本的编辑自动存上并收起（她 2026-11-01：没保存就去别的页面，回来还停在编辑状态不对）
function leaveEdits() {
  S.diaryTool = null;  // 翻页、切换、离开日记都退出荧光笔 / 批注模式（她 2026-11-02：一翻页就变成了划线）
  if (S.editGoal != null) { S.editGoal = null; flushSteps(true); }
  if (S.diaryEdit) {
    const t = document.querySelector("[data-diarydraft]"), day = S.diaryEdit;
    const text = t ? t.value : S.diaryDraft;
    const x = (S.cards && S.cards.cards || []).find((c) => c.day === day);
    S.diaryEdit = null; S.diaryDraft = null;
    if (text != null && (!x || text.trim() !== (x.diary || "").trim()))
      send("diary.save", { day, text }).then((r) => {
        if (r.ok) return showUndo("日记的改动保存好了");
        if (!S.diaryEdit) { S.diaryEdit = day; S.diaryDraft = text; }  // 没存上：草稿留着，回到日记还在编辑
        showUndo("日记没存上：" + r.msg);
      });
  }
}

// 副本编辑里改了名字还没离开输入框的，一起存上；say=true 时提示一句“改动已保存”
async function flushSteps(say) {
  const dirty = [...document.querySelectorAll("[data-steptitle]")].filter((t) => t.value.trim() && t.value !== t.defaultValue);
  let bad = null;
  for (const t of dirty) { const r = await send("task.rename", { id: t.dataset.steptitle, title: t.value.trim() }); if (!r.ok) bad = r.msg; }
  if (dirty.length && !bad) S.quests = await api("/api/quests").catch(() => S.quests);
  if (bad) showUndo("有改动没存上：" + bad);
  else if (say) showUndo("副本的改动都保存好了");
}

// 副本里加的是“任务”（主线的一步），只写内容、选是不是节点；领域、什么时候做是“待办”才有的（她 2026-11-01：别混在一起）
const stepAddForm = (gid) => `<form class="stepadd" data-form="stepadd" data-goal="${gid}"><input type="text" name="title" required placeholder="加一个任务" aria-label="新任务">
  <label class="btn nodechk"><input type="checkbox" name="node" value="1"> 节点</label><button class="btn gold">加上</button></form>`;

// 全站统一的返回键（她 2026-11-01：日记、商店、领域页的返回要一个样子）：左上角、标题前面，“‹ 回到哪”。规范见 docs/design/workbench.md 第“组件规范”节
const BACK_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"></path></svg>';
const backBtn = (label, attrs) => `<button type="button" class="backbtn" ${attrs} aria-label="返回${esc(label)}">${BACK_SVG}<span>${esc(label)}</span></button>`;

function diaryHtml(text, marks, notes) {
  // 日记是纯文字：一段一个 <p>，段落之间空一整行，去掉 markdown 记号，字和横线才能一行对一行（她 2026-11-01）
  // 记号笔标过的句子画上荧光（她 2026-11-02），点一下能取消
  const open = (notes || []).filter((n) => n.status !== "done");
  const hl = (h) => open.reduce((acc, n) => { const e = esc(n.text); return e && acc.includes(e) ? acc.split(e).join(`<span class="anno" title="${esc(n.note)}">${e}</span>`) : acc; },
    (marks || []).reduce((acc, m) => { const e = esc(m); return e && acc.includes(e) ? acc.split(e).join(`<mark class="hl" data-mark="${e}">${e}</mark>`) : acc; }, h));
  return String(text).replace(/\r/g, "").split(/\n\s*\n/).map((para) => para.split("\n")
    .map((l) => hl(esc(l.replace(/^\s*#+\s*/, "").replace(/^\s*[-*•]\s+/, "· ").replace(/\*\*(.+?)\*\*/g, "$1").replace(/\s+/g, " ")).trim())).filter(Boolean).join("<br>"))
    .filter(Boolean).map((h) => `<p>${h}</p>`).join("");
}

// 记号笔：在日记里选中一句，旁边出现“🖍 标记”；点标过的句子再点一次取消（她 2026-11-02）
function diaryMark(day, text, off) {
  api("/api/act", { action: "diary.mark", day, text, off: off ? 1 : 0 }).then((r) => {
    if (!r.ok) return showUndo(r.msg || "没标上");
    const c = ((S.cards && S.cards.cards) || []).find((x) => x.day === day);
    if (c) c.marks = r.marks;
    showUndo(r.msg); render();
  }).catch(() => showUndo("没连上服务器"));
}
// 荧光笔 / 批注：点 🖍 或 💬 进入模式，在句子上划一下（手指或鼠标），划过的就是选中的（她 2026-11-02：选中文字只能选两个字）
(() => {
  const at = (x, y) => {
    if (document.caretPositionFromPoint) { const p = document.caretPositionFromPoint(x, y); return p ? { node: p.offsetNode, off: p.offset } : null; }
    const r = document.caretRangeFromPoint && document.caretRangeFromPoint(x, y); return r ? { node: r.startContainer, off: r.startOffset } : null;
  };
  let drag = null;
  document.addEventListener("pointerdown", (ev) => {
    const ink = ev.target.closest && ev.target.closest(".ink[data-day]");
    if (!ink || !S.diaryTool) return;
    const p = at(ev.clientX, ev.clientY);
    if (!p) return;
    ev.preventDefault();
    drag = { ink, start: p };
    ink.setPointerCapture && ink.setPointerCapture(ev.pointerId);
  });
  document.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const p = at(ev.clientX, ev.clientY);
    if (!p || !drag.ink.contains(p.node)) return;
    drag.end = p;
    try { getSelection().setBaseAndExtent(drag.start.node, drag.start.off, p.node, p.off); } catch (e) {}
  });
  document.addEventListener("pointerup", () => {
    if (!drag) return;
    const { ink, start, end } = drag; drag = null;
    const day = ink.dataset.day;
    let text = "";
    if (end) {  // 用划过的范围取字（不靠浏览器自己的选中，手指划也行）
      const r = document.createRange();
      const before = start.node === end.node ? start.off <= end.off : (start.node.compareDocumentPosition(end.node) & Node.DOCUMENT_POSITION_FOLLOWING);
      try { if (before) { r.setStart(start.node, start.off); r.setEnd(end.node, end.off); } else { r.setStart(end.node, end.off); r.setEnd(start.node, start.off); } text = r.toString(); } catch (e) {}
    }
    text = text.replace(/\s+/g, " ").trim();
    getSelection().removeAllRanges();
    if (text.length < 2) return;
    if (S.diaryTool === "hl") { S.diaryTool = null; diaryMark(day, text); }  // 标一句就退出（她 2026-11-02）
    else { S.noteDraft = { day, text }; S.overlay = "diarynote"; renderOverlay(); const t = document.querySelector('#overlay textarea'); if (t) t.focus(); }
  });
  document.addEventListener("click", (ev) => {
    const m = ev.target.closest && ev.target.closest("mark.hl");
    if (!m || S.diaryTool) return;
    const day = m.closest(".ink[data-day]").dataset.day, text = m.dataset.mark;
    if (S.confirmMark !== day + text) { S.confirmMark = day + text; showUndo("再点一次取消这句的荧光"); setTimeout(() => { if (S.confirmMark === day + text) S.confirmMark = null; }, 5000); return; }
    S.confirmMark = null; diaryMark(day, m.textContent, true);
  });
})();

function diaryEditor(x) {
  // 她 2026-11-01：能自己改这一天（“说一段”她试了不好用，去掉了；要语音用键盘上的🎙）
  const draft = S.diaryDraft != null ? S.diaryDraft : (x.diary || "");
  return `<div class="dedit">
    <textarea data-diarydraft rows="14" aria-label="这一天的日记">${esc(draft)}</textarea>
    <div class="row" style="gap:6px;flex-wrap:wrap;align-items:center">
      <span style="flex:1"></span>
      <button type="button" class="btn" data-act="diarycancel">取消</button>
      <button type="button" class="btn gold" data-act="diarysave" data-day="${esc(x.day)}">保存</button></div>
  </div>`;
}

// 说一段：整段录下来传到服务器，用和 Telegram 语音同一个模型转文字（她 2026-11-01：浏览器自带的识别只认出一小截）
let diaryRec = null;
async function diaryMic() {
  if (diaryRec) { diaryRec.stop(); return; }
  if (!navigator.mediaDevices || !window.MediaRecorder) { showUndo("这个浏览器录不了音：点键盘上的🎙也能直接说"); return; }
  const keep = (document.querySelector("[data-diarydraft]") || {}).value;
  if (keep != null) S.diaryDraft = keep;
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { showUndo("没拿到麦克风权限，在浏览器设置里允许一下"); return; }
  const type = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"].find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
  const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined), chunks = [];
  rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  rec.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    diaryRec = null; S.listening = false;
    const blob = new Blob(chunks, { type: rec.mimeType || type || "audio/webm" });
    if (!blob.size) { S.diaryHeard = ""; render(); showUndo("没录到声音，再试一次"); return; }
    S.transcribing = true; render();
    try {
      const h = { "Content-Type": blob.type }; if (S.token) h.Authorization = "Bearer " + S.token;
      const up = await (await fetch("/api/voice", { method: "POST", headers: h, credentials: "same-origin", body: blob })).json();
      if (!up.ok) throw new Error(up.error || "没传上");
      for (let i = 0; i < 90; i++) {  // 最多等 3 分钟
        await new Promise((r) => setTimeout(r, 2000));
        const got = await api("/api/voice?job=" + up.job);
        if (got.done) { if (got.text) S.diaryHeard = ((S.diaryHeard ? S.diaryHeard + " " : "") + got.text).trim(); else showUndo(got.error || "没听清"); break; }
      }
    } catch (e) { showUndo("转文字没成：" + (e.message || "再试一次")); }
    S.transcribing = false; render();
  };
  rec.start(1000);
  diaryRec = rec; S.listening = true; S.recStart = Date.now(); render();
}

async function diarySend(action, payload) {
  const res = await send(action, payload);
  if (!res.ok) { showUndo(res.msg); return false; }
  S.diaryEdit = null; S.diaryDraft = null; S.diaryHeard = "";
  try { S.cards = await api("/api/cards"); } catch (e) { /* 已经存上了，下次进来再拉 */ }
  render(); showUndo(res.msg || "存好了");
  return true;
}

// 成就徽章里的图标（她 2026-11-02：别放一堆空六边形）：按成就的意思挑，线条风格和领域图标一致
const AI = {
  scale: "M5 20h14l-2-11H7zM9 9a3 3 0 0 1 6 0M12 13v3",
  dumbbell: "M3 10v4M6 8v8M18 8v8M21 10v4M6 12h12",
  target: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 4a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm0 4a1 1 0 1 0 0 2 1 1 0 0 0 0-2",
  moon: "M20 14A8 8 0 1 1 10 4a6 6 0 0 0 10 10z",
  egg: "M12 3c-4 0-6 6-6 10a6 6 0 0 0 12 0c0-4-2-10-6-10z",
  stretch: "M12 4a2 2 0 1 0 0 .01M5 9l7 2 7-2M12 11v4l-3 5M12 15l3 5",
  pen: "M4 20l4-1L19 8l-3-3L5 16zM14 6l3 3",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18",
  cal: "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4",
  heart: "M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.5A4 4 0 0 1 19 10c0 5.5-7 10-7 10z",
  book: "M4 5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2zM4 5v16",
  users: "M9 11a3 3 0 1 0 0-.01M3 20c0-3 3-5 6-5s6 2 6 5M16 5a3 3 0 0 1 0 6M18 15c2 .5 3 2 3 5",
  chat: "M4 5h16v11H9l-5 4z",
  cap: "M2 9l10-5 10 5-10 5zM6 11v5c3 2 9 2 12 0v-5",
  music: "M9 18V6l10-2v12M9 18a3 3 0 1 1-3-3M19 16a3 3 0 1 1-3-3",
  steps: "M8 3c2 0 3 2 3 5s-1 4-3 4-3-1-3-4 1-5 3-5zM6 15h4v4H6zM16 9c2 0 3 2 3 5s-1 4-3 4-3-1-3-4 1-5 3-5z",
  spark: "M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z",
  mountain: "M3 20l6-10 4 6 3-4 5 8z",
  flame: "M12 3c3 4 6 6 6 10a6 6 0 0 1-12 0c0-3 2-5 3-7 1 2 2 3 3 3 0-2-1-4 0-6z",
  leaf: "M5 19c0-9 6-14 15-14 0 9-5 15-14 15M5 19l7-7",
  flag: "M5 21V4M5 4h11l-2 4 2 4H5",
  cup: "M5 8h11v6a5 5 0 0 1-10 0zM16 10h2a2 2 0 0 1 0 4h-2M7 3v2M11 3v2",
};
const ACH_ICON = { first_weigh: "scale", train: "dumbbell", bodyfat: "target", "hidden:sleep_week": "moon", "hidden:train_week": "dumbbell",
  "hidden:protein_week": "egg", "hidden:stretch_week": "stretch", first_xhs: "pen", site_live: "globe", xhs_weeks: "cal", fans: "heart",
  book: "book", friends: "users", first_english: "chat", english_weeks: "chat", phrases: "chat", first_prof: "cap", first_dance: "music",
  outing: "steps", experiences: "spark", countries: "globe", hikes: "mountain", song: "music", streak: "flame", green: "leaf",
  "hidden:rest": "cup", "hidden:weekend": "cal" };
const achIcon = (key) => `<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${AI[ACH_ICON[key] || (String(key).startsWith("goal:") ? "flag" : "spark")]}"></path></svg>`;

function achPage() {
  // 2026-10-29 她说全平铺没差异：传说级用视觉区分、奖励单独一个标签页、解锁了要看得出来
  const a = S.ach;
  if (!a) return '<p class="loading">加载中……</p>';
  const r = a.rewards, fresh = r.plays.filter((p) => p.waiting).length;
  const tabs = `<div class="tabs" role="tablist"><button type="button" role="tab" class="${S.achTab !== "reward" ? "on" : ""}" data-act="achtab" data-v="">成就 ${a.got} / ${a.total}</button>
    <button type="button" role="tab" class="${S.achTab === "reward" ? "on" : ""}" data-act="achtab" data-v="reward">🎁 奖励 ${r.unlocked} / ${r.total}${fresh ? ` <span class="newdot">${fresh} 个待揭晓</span>` : ""}</button></div>`;
  const head = seasonHead("成就") + tabs;
  if (S.achTab === "reward") {
    return head + `<section class="panel col playbox"><span class="muted" style="font-size:13px">拿到一个铜级成就（“第一次”也算），就解开一个铜级奖励，银级、金级一样。解开了 小伴 会在聊天里亲口告诉你，这里也跟着亮。</span>
      <div class="playgrid">${r.plays.map((p) => p.waiting
        ? `<div class="play t-${p.tier} glow"><span class="medal m-${p.tier}">${p.tier}级</span><b>🎁 已解锁，等 小伴 揭晓</b><span class="muted" style="font-size:12px">下次和他聊天，他会亲口告诉你</span></div>`
        : p.unlocked
        ? `<div class="play t-${p.tier} open"><span class="medal m-${p.tier}">${p.tier}级</span><b style="font-size:16px">${esc(p.name)}</b><span style="font-size:13px;line-height:1.6">${esc(p.note)}</span>${p.at ? `<span class="muted" style="font-size:11px;margin-top:auto">${esc(p.at.slice(0, 10))} 解锁</span>` : ""}</div>`
        : `<div class="play locked"><span class="medal m-${p.tier}">${p.tier}级</span><b>？？？</b><span class="muted" style="font-size:12px">拿到一个${p.tier}级成就揭晓</span></div>`).join("")}</div></section>`;
  }
  // 成就页（她 2026-11-02 通过样稿：现在的成就 + 样稿 C 的六边形徽章，按七块领域分组）
  const level = (i) => i.tier || (i.done ? "达成" : "");
  const degree = (i) => i.steps ? ((["铜", "银", "金"].indexOf(i.tier) + 1) + (i.next != null && i.source ? (i.frac || 0) : 0)) / i.steps.length : (i.done ? 1 : 0);
  const hex = (i) => {
    if (i.riddle && !i.done) return `<div class="hexb riddle" title="${esc(i.riddle)}"><span class="hx"><b>?</b></span><span class="hn">？？？</span><span class="hs">${esc(i.riddle)}</span></div>`;
    const lv = level(i), pct = Math.round(100 * degree(i));
    const sub = i.steps ? (i.source ? `${i.value == null ? "—" : i.value}${i.value == null ? "" : i.unit}${i.next == null ? " · 全部达成" : ` / ${i.next}${i.unit}`}` : esc(i.hint || "等记录")) : (lv ? "已达成" : esc(i.hint || "还没做到"));
    return `<div class="hexb ${lv ? "got t-" + (lv === "达成" ? "金" : lv) : ""}" title="${esc(i.desc || "")}"><span class="hx" style="--p:${pct}"><b>${achIcon(i.key)}</b>${lv && lv !== "达成" ? `<i class="tier">${lv}</i>` : ""}</span>
      <span class="hn">${esc(i.name)}</span><span class="hs">${lv ? sub : `${pct ? pct + "% · " : ""}${sub}`}</span></div>`;
  };
  const groups = [["body", "健康"], ["career", "事业"], ["life", "生活体验"], ["study", "学习"], ["social", "社交"], ["academic", "学术"], ["money", "财务"], [null, "不分领域"]];
  return head + `
    <section class="legendhero" aria-label="传说级成就">${a.legend.map((l) => `<div class="legendcard ${l.done ? "got" : ""}"><span class="lcrown" aria-hidden="true">♛</span><b>${esc(l.name)}</b><span>${esc(l.desc)}</span><small>${l.done ? "已达成" : "传说 · 未解锁"}</small></div>`).join("")}</section>
    ${groups.map(([k, n]) => { const rows = a.items.filter((i) => (i.domain || null) === k).sort((x, y) => degree(y) - degree(x)); const got = rows.filter((i) => i.done || i.tier).length;
      return rows.length ? `<section class="achdom"><div class="row" style="gap:10px;align-items:baseline"><h2>${k ? tIcon(k) : ""}${n}</h2><span class="muted" style="font-size:12px">${got} / ${rows.length}</span></div><div class="hexgrid">${rows.map(hex).join("")}</div></section>` : ""; }).join("")}`;
}

/* ---------- 第二大脑：她的资料都在这里（链接、收藏、Grok 情报、学过的、人物卡、心愿） ---------- */
// 2026-10-29 她要 MECE：状态每条只有一个（要你看 / 在用 / 备查 / 整理中，回收站另算），类型按“从哪来”分三组，领域七块加没归类；
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
  const terrs = `<select data-change="bq-k" aria-label="按领域筛"><option value="">全部领域（${c.terrs[""] || 0}）</option>${b.territories.map((t) => `<option value="${t.key}"${q.k === t.key ? " selected" : ""}>${esc(t.name)}（${c.terrs[t.key] || 0}）</option>`).join("")}<option value="waste"${q.k === "waste" ? " selected" : ""}>还没归类（${c.terrs.waste || 0}）</option></select>`;
  const card = (x) => `<button type="button" class="panel bcard ${x.status === "ask" ? "ask" : ""}" data-act="bopen" data-id="${x.id}">
      <span class="row" style="justify-content:space-between;gap:8px"><span class="row" style="gap:6px">${x.status === "ask" ? '<i class="reddot"></i>' : ""}<span class="pill p-none">${esc(x.kind_label)}</span>${x.fresh ? '<span class="newtag">新</span>' : ""}</span>
        <span class="muted one" style="font-size:11px">${esc(x.source || "")}${x.happened_at ? " · " + esc(String(x.happened_at).slice(0, 10)) : ""}</span></span>
      <b class="clamp2 btitle">${esc(x.title)}</b>
      ${x.status === "ask" && x.extra && x.extra.ask ? `<span class="askline clamp2">要你定：${esc(x.extra.ask)}</span>`
        : `<span class="muted clamp2 bsum">${esc((x.extra && x.extra.ai_summary) || (x.summary !== x.title ? x.summary : "") || "")}</span>`}
      <span class="muted one bfoot" style="font-size:11px">${esc([x.territory_name || "还没归类", BSTATUS[x.status], ...(x.tags || []).slice(0, 3)].filter(Boolean).join(" · "))}</span></button>`;
  const empty = q.tab === "ask" ? "没有要你处理的，小伴 都整理好了。" : q.tab === "new" ? "小伴 都整理完了。你发给他的链接、Grok 每天凌晨整理的收藏会先到这里。" : "这里是空的。";
  // 返回键在右上角，回到从哪进来的那一页（她 2026-11-02：从财务页进来只能回“我”）
  const back = S.brainBack && S.brainBack.page === "territory"
    ? backBtn(({ body: "健康", career: "事业", money: "财务", social: "社交", study: "学习", academic: "学术", life: "生活体验" })[S.brainBack.tkey] || "领域", 'data-act="brainback"')
    : backBtn("我", 'data-act="go" data-page="me"');
  return `<header class="row" style="flex-wrap:wrap;gap:10px"><h2 style="font-size:22px">收藏和资料</h2>
      <input type="search" class="bsearch" data-input="brain-q" value="${esc(q.q || "")}" placeholder="搜标题、内容、标签……" aria-label="搜索">${terrs}<span style="margin-left:auto">${back}</span></header>
    <div class="chips">${tabs}</div>${kinds}
    <span class="muted" style="font-size:12px;line-height:1.6">这些都交给 小伴 打理：他写摘要、贴标签、分领域，常用的放<b>在用</b>（聊天、排计划、找新体验会用上），偶尔翻翻的放<b>备查</b>。他拿不准、得你拍板的放<b>要你看</b>，带红点。点开看全文。</span>
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
        <select data-change="brain-terr" data-id="${x.id}" aria-label="归到哪个领域"><option value="">还没归类</option>${b.territories.map((t) => `<option value="${t.key}"${x.territory === t.key ? " selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
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
// “待你审查”里不该她审的：误报就流转到下一步，没干成就判失败（2026-10-30 她要）
// 失败的、等审查的都能直接归档（2026-10-30 她要）；等审查的归档算放弃，不扣分
const archBtn = (id, big) => `<button type="button" class="btn ${big ? "" : "tiny"}" data-act="agent-archive" data-id="${id}" title="收进右上角“归档”">归档</button>`;
// 首页“小伴 在准备”默认只显示今天在做的和做好等她看的；其余收进“还没轮到”（2026-10-30 她要）
function prepShown(st, t) {
  return ["working", "waiting_grok", "revising", "ready"].includes(t.state) || t.star || (st.prep_today || []).some((o) => o.id === t.id);
}
function judge(attrs, big) {
  const c = big ? "btn" : "btn tiny";
  return `<span class="judge"><button type="button" class="${c}" data-act="agent-misreport" ${attrs} title="不用你审，直接流转到下一步">误报${big ? "，不用我审" : ""}</button><button type="button" class="${c}" data-act="agent-fail" ${attrs} title="没干成，记成失败">失败</button></span>`;
}
function teamHours(sec) { return sec >= 3600 ? (sec / 3600).toFixed(1) + " 小时" : Math.round(sec / 60) + " 分钟"; }
function teamCard(x, key) {
  const t = S.team, name = (a) => t.names[a] || a, hours = teamHours;
  const st = { queued: "queued", running: "running", review: "review", accepted: "done", failed: x.status === "dropped" ? "dropped" : "failed" }[key] || "";
  return `<div class="item tcard st-${st}"><i class="pulse" aria-hidden="true"></i>
      <button type="button" class="grow linkish topen" data-act="acard" data-id="${x.id}" aria-label="看这件活的详情"><span class="clamp2">${esc(x.title)}</span>
        <span class="muted one" style="display:block;font-size:11px">${esc(name(x.agent))}${x.project && !x.project.startsWith("Grok 回报") ? " · " + esc(x.project) : ""} · ${x.size} 点${x.reworks ? ` · 返工 ${x.reworks}` : ""}${x.work_seconds ? " · " + hours(x.work_seconds) : ""}</span>
        ${key === "failed" ? `<span class="muted one" style="display:block;font-size:11px">${x.status === "dropped" ? "你叫停的" : "没干成"}${x.review_note ? "：" + esc(x.review_note) : ""}</span>` : ""}</button>
      ${key === "review" ? `<div class="row tacts">${judge(`data-id="${x.id}"`)}${archBtn(x.id)}</div>`
        : key === "failed" ? `<div class="row tacts">${archBtn(x.id)}</div>`
        : ["queued", "running"].includes(key) ? `<button type="button" class="x" data-act="agent-drop" data-id="${x.id}" title="不干了" aria-label="叫停这件活">⊘</button>` : ""}
      ${key === "accepted" || x.rating ? `<span class="row rate stars5" role="group" aria-label="打分 1 到 5">${[1, 2, 3, 4, 5].map((n) => `<button type="button" class="x ${(x.rating || 0) >= n ? "on" : ""}" data-act="rate" data-id="${x.id}" data-rating="${x.rating === n ? 0 : n}" aria-label="${n} 分">★</button>`).join("")}</span>` : ""}</div>`;
}
function teamBody(t, board, card) {
  // 2026-10-29：系统外也在干的活一起放进看板（Claude 会话、准备员算 小伴 的、小伴 派给 Grok 还没回的），卡片按状态带动效
  const name = (a) => t.names[a] || a;
  // 2026-10-29 她要每张卡一眼看出在干啥：会话卡多一行它最近说的话
  // 2026-10-29 她说只有 Grok 的卡能点开：所有卡统一一个样子，都能点开看详情（准备员的打开那一件）
  const liveCard = (x) => `<div class="item tcard st-${x.state}"><i class="pulse" aria-hidden="true"></i>
      <button type="button" class="grow linkish topen" ${x.prep ? `data-act="prepgo" data-id="${x.prep}"` : `data-act="alive" data-i="${(t.live || []).indexOf(x)}"`} aria-label="看这件活的详情"><span class="clamp2">${esc(x.title)}</span>
        <span class="muted one" style="display:block;font-size:11px">${esc(name(x.agent))} · ${esc(x.sub)}${x.at ? " · " + ago(x.at) : ""}</span>
        ${x.summary ? `<span class="one tsum">${esc(x.summary)}</span>` : ""}</button>
      ${x.state === "review" && x.key ? judge(`data-key="${esc(x.key)}" data-agent="${esc(x.agent)}" data-title="${esc(x.title)}"`) : ""}</div>`;
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
  // 全局规范：不管怎么关的（关闭键、点空白、Esc），弹窗的临时状态都清掉，下次打开是默认状态（她 2026-11-01：关了再开还是编辑态）
  if (!S.overlay && S._ovShown) resetOverlayState();
  const box = $("#overlay"), old = box.querySelector(".sheet"), same = S._ovShown === S.overlay;
  const top = same && old ? old.scrollTop : 0;
  renderOverlayInner();
  // 手机上弹层占满整屏、没有空白处可点（她 2026-10-31）：保证每个弹层自己里面有“关闭”，标题行固定在顶上、滚动时也点得到；
  // 弹层外面不再另加一个（她 2026-11-01：两个关闭键重复）
  const sh = box.querySelector(".sheet");
  // 关闭键一律在右上角（她 2026-11-01）；弹窗里已经有关闭 / 取消的不再补，免得两个按钮重复
  if (S.overlay && sh && !sh.querySelector('[data-act="close"]'))
    sh.insertAdjacentHTML("afterbegin", '<button type="button" class="btn sheetclose" data-act="close">关闭</button>');
  S._ovShown = S.overlay;
  const now = box.querySelector(".sheet");
  if (now && top) now.scrollTop = top;
}
function renderOverlayInner() {
  const box = $("#overlay"), st = S.state;
  if (!S.overlay) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  if (S.overlay.startsWith("me-")) { box.innerHTML = meOverlay(); return; }  // “我”页的五个弹层
  if (S.overlay === "card") {
    const c = st.card;
    box.innerHTML = `<button type="button" class="bigcard" data-act="close" aria-label="收起今日卡">
      <span class="img ${RAR(c.rarity)}"><img src="${imgUrl(c.image)}" alt="今日卡：${esc(c.title)}"></span>
      <span class="txt"><span class="muted" style="font-size:13px">${dayNOf(c.day) > 0 ? "第 " + dayNOf(c.day) + " 天 · " : ""}${esc(c.day)}</span><span style="font-size:28px;font-weight:700">${esc(c.title)}</span>
        <span style="align-self:flex-start;font-size:13px;padding:3px 12px;border-radius:12px;border:1px solid #E2BE6A;color:#E2BE6A">${esc(c.rarity)}</span>
        <span style="font-size:14px;line-height:1.7">${esc(c.reason)}</span><span style="font-size:14px;line-height:1.7;color:#E2BE6A">小伴：${esc(c.line)}</span>
        <span style="font-size:12px;color:#ABA8CC">点任意处收起</span></span></button>`;
  } else if (S.overlay === "plan") {
    const pl = st.planner;
    box.innerHTML = `<section class="sheet" aria-label="计划"><div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap"><h2 style="font-size:18px">计划 · ${esc(st.day.slice(5))} 周${st.weekday}</h2>
        <span class="row" style="gap:8px">${pl.replanning ? '<span class="muted" style="font-size:12px">小伴 在按新日程重排……</span>' : ""}<button type="button" class="btn" data-act="close">关闭</button></span></div>
      <div class="plancols">
        <section class="col"><div class="row" style="justify-content:space-between"><h2>待办</h2><span class="muted" style="font-size:11px">★ = 今日任务，不限件数</span></div>
          ${pl.todos.map((t) => `<div class="item"><button type="button" class="star ${t.star ? "on" : ""}" data-act="star" data-id="${t.id}" data-on="${t.star ? "" : 1}" aria-label="设为或取消今日任务">★</button>
            <span class="grow"><span style="display:block">${esc(t.title)}</span><span class="muted" style="font-size:11px">${esc(t.sub)}${t.due ? " · " + esc(t.due.slice(5)) : ""}</span></span>
            <button type="button" class="x" data-act="rename" data-id="${t.id}" data-title="${esc(t.title)}" aria-label="改名字">✎</button><button type="button" class="btn" data-act="postpone" data-id="${t.id}">推到明天</button><button type="button" class="x" data-act="deltask" data-id="${t.id}" aria-label="删除">×</button></div>`).join("") || '<p class="muted">没有待办</p>'}
          ${(pl.skipped || []).length ? `<div class="col" style="gap:4px;margin-top:6px"><b style="font-size:13px">今天没排进</b>${pl.skipped.map((x) => `<span style="font-size:12px">· ${esc(x.title)}<span class="muted">：${esc(x.why)}</span></span>`).join("")}</div>` : ""}
          <button type="button" class="btn gold" data-act="addtodo">＋ 加待办</button>
        </section>
        <section class="col"><div class="row" style="justify-content:space-between"><h2>日程</h2><span class="muted" style="font-size:11px">和苹果日历对得上</span></div>
          ${pl.events.map((e) => e.id ? `<div class="item"><label style="font-size:10px">开始<input type="time" value="${esc(e.start)}" data-change="time" data-id="${e.id}"></label>
            <span class="grow"><span style="display:block">${esc(e.title)}</span><span class="muted" style="font-size:11px">${esc(e.place)}${e.leave ? " · " + esc(e.leave) + " 出发" : ""}</span>${clashNote(e)}</span>
            ${e.mine ? `<button type="button" class="btn small" data-act="evrelease" data-title="${esc(e.title)}" title="你改过这个时间；点了撤销你的改动，交回系统重排">撤销改动</button>` : ""}
            <button type="button" class="x" data-act="delevent" data-id="${e.id}" aria-label="删除这个日程">×</button></div>`
            : `<div class="item ${e.clash_with && e.clash_with.length ? "clash" : ""}"><b class="evtime">${esc(e.start)}–${esc(e.end || "")}</b>
            <span class="grow"><span style="display:block">${esc(e.title)}</span><span class="muted" style="font-size:11px">${esc(e.place ? e.place + " · " : "")}${esc(e.source)}</span>${clashNote(e)}</span>
            ${e.fid ? `<button type="button" class="x" data-act="delfuture" data-id="${e.fid}" aria-label="删除这个约">×</button>`
              : e.href ? `<button type="button" class="x" data-act="delown" data-href="${esc(e.href)}" aria-label="从苹果日历里删掉">×</button>` : ""}</div>`).join("") || '<p class="muted">今天没有日程</p>'}
          <button type="button" class="btn gold" data-act="addevent">＋ 加日程</button>
        </section></div>
      <span class="muted" style="font-size:12px">改了马上生效，小伴 也会知道；懒得点就直接跟他说“把训练挪到 17:00”。</span></section>`;
  } else if (S.overlay === "cardview") {
    const c = S.cardView;
    // 过去某天的卡：一起看那天的日记摘要，点按钮展开整篇（2026-10-30 她要）
    box.innerHTML = `<button type="button" class="bigcard" data-act="close" aria-label="收起">
      <span class="img ${RAR(c.rarity)}"><img src="${imgUrl(c.image)}" alt="今日卡：${esc(c.title)}"></span>
      <span class="txt"><span class="muted" style="font-size:13px">${dayNOf(c.day) > 0 ? "第 " + dayNOf(c.day) + " 天 · " : ""}${esc(c.day)}</span>
        <span style="font-size:28px;font-weight:700">${esc(c.title)}</span>
        <span style="align-self:flex-start;font-size:13px;padding:3px 12px;border-radius:12px;border:1px solid #E2BE6A;color:#E2BE6A">${esc(c.rarity)}</span>
        <span style="font-size:14px;line-height:1.7">${esc(c.reason)}</span><span style="font-size:14px;line-height:1.7;color:#E2BE6A">小伴：${esc(c.line)}</span>
        ${c.diary ? `<span class="cday"><b style="font-size:13px">这一天</b><span style="font-size:14px;line-height:1.7">${esc(c.diary.length > 90 ? c.diary.slice(0, 90) + "……" : c.diary)}</span>
          <span class="btn" role="button" tabindex="0" data-act="carddiary">看当天的日记</span></span>` : ""}
        <span style="font-size:12px;color:#ABA8CC">点任意处收起</span></span></button>`;
  } else if (S.overlay === "carddiary") {  // 盖在卡上看当天日记，点空白回到卡（2026-10-30 她要）
    const c = S.cardView || {};
    box.innerHTML = `<section class="sheet small col" aria-label="${esc(c.day)} 的日记" style="gap:10px">
      <span class="muted" style="font-size:13px">${esc(c.day)} · 这一天的日记 · 点空白处回到卡片</span>
      <div class="md" style="font-size:14px;line-height:1.8">${md(c.diary || "")}</div></section>`;
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
      ${S.detailK === "meals" && st.meals && st.meals.meals.length ? mealsView(st.meals) : S.detailK === "train" && st.training ? trainView(st.training) : `<div style="white-space:pre-wrap;line-height:1.7;font-size:14px">${esc(st.detail[S.detailK] || "")}</div>`}
      <button type="button" class="btn gold" style="margin-top:12px" data-act="weekopen" data-k="${S.detailK}">看一周${S.detailK === "meals" ? "（含采购清单）" : ""}</button></section>`;
  } else if (S.overlay === "week") {  // 每周日自动出的一周安排（ethan/weekly.py，她 2026-10-31 定）
    const w = S.week, which = S.weekWhich || "this", p = w && w[which];
    const pick = w ? `<span class="modeseg">${["this", "next"].map((k) => `<button type="button" class="${which === k ? "on" : ""}" data-act="weekwhich" data-k="${k}" ${w[k] ? "" : "disabled"}>${k === "this" ? "这周" : "下周"}</button>`).join("")}</span>` : "";
    const meals = (p) => `<h3 style="font-size:15px;margin:6px 0">采购清单 <span class="muted" style="font-weight:400;font-size:12px">${p.shopping.length} 样，够 7 天</span></h3>
        <div class="shoplist">${p.shopping.map((x) => `<span>${esc(x.item)} <b>${x.grams >= 1000 ? (x.grams / 1000).toFixed(1) + " 千克" : x.grams + " 克"}</b></span>`).join("")}</div>
        <h3 style="font-size:15px;margin:14px 0 6px">每天吃什么 <span class="muted" style="font-weight:400;font-size:12px">每天约 ${p.kcal} 千卡、蛋白质 ${p.protein} 克</span></h3>
        ${p.days.map((d) => `<div class="weekrow"><b>周${d.weekday}${d.prep ? '<i class="drar">备主菜</i>' : ""}</b><span>${d.items.map((m) => esc(m.meal + "：" + m.name)).join("；")}</span></div>`).join("")}`;
    const train = (p) => p.training.map((t) => `<div class="weekrow"><b>周${t.weekday} ${esc(t.start)}</b><span><b style="font-weight:600">${esc(t.focus)}</b> · ${esc(t.place)}<br><span class="muted" style="font-size:12px">${t.exercises.map(esc).join("；")}</span></span></div>`).join("")
      + '<p class="muted" style="font-size:12px">每天早上计划器会按当天日程再核一次，有冲突会挪。</p>';
    box.innerHTML = `<section class="sheet" aria-label="一周安排"><div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:8px"><h2 style="font-size:18px">${S.weekK === "train" ? "一周训练" : "一周三餐和采购清单"}</h2>${pick}<button type="button" class="btn" data-act="close">关闭</button></div>
      ${!w ? '<p class="muted">加载中……</p>' : !p ? '<p class="muted">这一周的还没排。每周日凌晨会自动排好下周的。</p>' : S.weekK === "train" ? train(p) : meals(p)}</section>`;
  } else if (S.overlay === "prep") {
    box.innerHTML = `<section class="sheet prepsheet" aria-label="小伴 在准备的事"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">小伴 在替你准备</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      ${(() => { const n = { parked: st.prep.filter((t) => t.state === "pending").length, routines: (st.prep_routines || []).length, trash: (st.prep_trash || []).length };
        const tab = (k, label) => `<button type="button" class="btn ${(S.prepTab || "") === k ? "gold" : ""}" data-act="preptab" data-tab="${k}">${label}</button>`;
        const later = st.prep.filter((t) => t.state !== "pending" && !prepShown(st, t)).length;
        return `<div class="row preptabs" style="gap:6px;flex-wrap:wrap">${tab("", `今天 ${st.prep.length - n.parked - later}`)}${tab("later", `📋 还没轮到 ${later}`)}${tab("parked", `⏸ 先放着 ${n.parked}${(S.state || {}).parked_remind ? ' <i class="navdot" aria-label="今天还没看">·</i>' : ""}`)}${tab("routines", `🔁 长期工作 ${n.routines}`)}${tab("trash", `🗑 回收站 ${n.trash}`)}</div>`; })()}
      ${S.prepTab && S.prepTab !== "later" ? "" : `${(st.prep_today || []).length && S.prepTab !== "later" ? `<div class="preptoday"><b style="font-size:13px">今天 小伴 打算先做</b>${st.prep_today.map((o, n) => { const t = st.prep.find((x) => x.id === o.id); return t ? `<span style="font-size:12px">${n + 1}. ${esc(t.title)}<span class="muted">${o.why ? "：" + esc(o.why) : ""}</span></span>` : ""; }).join("")}</div>` : ""}
      <div class="preplist">${st.prep.filter((t) => t.state !== "pending" && prepShown(st, t) === (S.prepTab !== "later")).map((t) => `<div class="prepitem ${S.prepOpen === t.id ? "open" : ""} ${t.state === "ready" ? "ready" : ""}" data-pid="${t.id}">
        <div class="row" style="gap:8px"><span class="drag" data-drag aria-label="拖动排序" title="按住拖动">⋮⋮</span><button type="button" class="x pstar ${t.star ? "on" : ""}" data-act="prepstar" data-id="${t.id}" data-on="${t.star ? "" : 1}" aria-label="${t.star ? "取消星星" : "点星星：今天必须做完"}" title="今天必须做完">${t.star ? "★" : "☆"}</button><button type="button" class="grow linkish" data-act="prepopen" data-id="${t.id}">${esc(t.title)}${t.summary ? `<span class="muted" style="display:block;font-size:11px">${esc(t.summary)}</span>` : ""}</button>
          <span class="pill ${t.state === "ready" ? "p-warn" : t.state === "approved" ? "p-ok" : "p-none"}">${esc(t.state === "ready" && !t.result ? "聊天里交了" : t.state_label)}</span></div>
        ${t.help_for ? `<span class="muted" style="font-size:12px;margin-left:30px">替你的「${esc(t.help_for)}」提前准备</span>` : ""}
        ${S.prepOpen === t.id ? `<div class="${t.result ? "prepreview" : "col"}" style="gap:8px;margin-top:8px">
          <div class="col" style="gap:8px;min-width:0">
          ${t.result ? `<div class="prepresult md">${md(t.result)}</div>` : `<span class="muted" style="font-size:12px">${t.state === "waiting_grok" ? "Grok 正在查，查回来 小伴 再整理。" : t.state === "ready" ? "小伴 在聊天里已经交了这件事，不用再等他做。你确认没问题，点“已经做完了”就收起来。" : "还在排队，轮到就做。"}</span>`}
          ${t.decision ? `<span class="gold" style="font-size:13px">要你拍板：${esc(t.decision)}</span>` : ""}</div>
          ${t.result ? `<div class="prepnote col">
            <label class="muted" for="pn${t.id}" style="font-size:12px">你的批注（Apple Pencil 直接写，会自动转成字；聊天里说过的不用再写）</label>
            <textarea id="pn${t.id}" data-prepnote="${t.id}" placeholder="哪里要改、改成什么样">${esc((S.prepDraft || {})[t.id] || "")}</textarea>
            <div class="row" style="gap:6px;flex-wrap:wrap">
              ${t.state === "ready" ? `<button type="button" class="btn gold" data-act="prepok" data-id="${t.id}">通过</button>` : ""}
              <button type="button" class="btn" data-act="prepfix" data-id="${t.id}">让他改</button>
              <button type="button" class="btn" data-act="preppend" data-id="${t.id}" title="暂时用不上，收进“先放着”，不算等你看">先放着</button>
              <button type="button" class="btn" data-act="dropprep" data-id="${t.id}">不要了</button></div></div>`
          : `<div class="row" style="gap:6px;flex-wrap:wrap">
              <button type="button" class="btn" data-act="prepdone" data-id="${t.id}" title="他在聊天里已经做完了，就点这个">已经做完了</button>
              <button type="button" class="btn" data-act="preppend" data-id="${t.id}" title="暂时用不上，先不做">先放着</button>
              <button type="button" class="btn" data-act="dropprep" data-id="${t.id}">不要了</button></div>`}</div>` : ""}</div>`).join("") || '<p class="muted">现在没有</p>'}</div>`}
      ${S.prepTab === "parked" ? (() => { const parked = st.prep.filter((t) => t.state === "pending"); return parked.length ? `<div class="col" style="gap:8px">
        ${parked.map((t) => `<div class="prepitem col" style="align-items:stretch;gap:6px"><div class="row" style="gap:8px"><button type="button" class="grow linkish" data-act="prepopen" data-id="${t.id}">${esc(t.title)}${t.summary ? `<span class="muted" style="display:block;font-size:11px">${esc(t.summary)}</span>` : ""}</button>
          <button type="button" class="btn" data-act="prepunpend" data-id="${t.id}">拿出来</button><button type="button" class="btn" data-act="dropprep" data-id="${t.id}">不要了</button></div>
          ${S.prepOpen === t.id && t.result ? `<div class="prepresult md">${md(t.result)}</div>` : ""}</div>`).join("")}</div>` : '<p class="muted">没有先放着的</p>'; })() : ""}
      ${S.prepTab === "routines" ? (st.prep_routines || []).length ? `<div class="col" style="gap:8px">
        ${st.prep_routines.map((t) => `<div class="item"><span class="grow" style="font-size:13px">${esc(t)}</span></div>`).join("")}</div>` : '<p class="muted">没有长期工作</p>' : ""}
      ${S.prepTab === "trash" ? (st.prep_trash || []).length ? `<div class="col" style="gap:8px">
        ${st.prep_trash.map((x) => `<div class="item"><span class="grow">${esc(x.title)}<span class="muted" style="display:block;font-size:11px">${esc(String(x.at).slice(5, 16).replace("T", " "))} 放进来的</span></span><button type="button" class="btn" data-act="preprestore" data-id="${x.id}">放回去</button></div>`).join("")}</div>` : '<p class="muted">回收站是空的</p>' : ""}</section>`;
  } else if (S.overlay === "desk") {  // 今日邮件和留言（ethan/desk.py，2026-10-30 她要）
    const items = (S.desk || {}).items;
    const STATE = { drafting: "小伴 在写草稿……", handed: "Dais在写草稿……", draft: "草稿好了，看看再发", sending: "等主页发出", sent: "已发送" };
    const btn = (act, x, label, cls = "") => `<button type="button" class="btn ${cls}" data-act="${act}" data-kind="${x.kind}" data-id="${x.id}" data-k="${x.kind + x.id}">${label}</button>`;
    // 一次只给她看一种语言，默认中文；“看原文 / 看翻译”切换（她 2026-10-30）。已经是中文的不出切换按钮
    const lang = (key, what) => ((S.deskLang || {})[key + what]) || "zh";
    const zhOf = (x, what) => { const key = x.kind + x.id, mine = (S.deskZh || {})[key + what];
      if (what === "body") return x.zh || (mine && mine.zh);
      const cur = deskDraftText(x);
      return mine && mine.src === cur ? mine.zh : cur === (x.draft || "") ? x.draft_zh : null; };
    const flip = (x, what) => `<button type="button" class="btn" data-act="desklang" data-what="${what}" data-k="${x.kind + x.id}">${lang(x.kind + x.id, what) === "zh" ? "看原文" : "看翻译"}</button>`;
    const accts = (S.desk || {}).accounts || [];
    const one = (x) => { const key = x.kind + x.id, open = S.deskOpen === key, sent = x.state === "sent", out = x.kind === "out", mail = x.kind === "mail" || out;
      const writer = mail ? "小伴" : "Dais", manual = !mail && x.channel === "manual";
      const bodyZh = !x.is_zh && lang(key, "body") === "zh";
      if (open && bodyZh && !zhOf(x, "body")) deskFetchZh(x, "body");
      const hasDraft = ["draft", "sending", "sent"].includes(x.state) && !!x.draft;
      const editable = x.state === "draft";
      const draftZh = hasDraft && !x.draft_is_zh && lang(key, "draft") === "zh";
      if (open && draftZh && !zhOf(x, "draft")) deskFetchZh(x, "draft");
      const left = sent && !x.draft ? `<div class="deskread">你在别处回过了。</div>`
        : !hasDraft ? `<div class="deskempty">${["drafting", "handed"].includes(x.state) ? `${writer}在写草稿……${mail ? "" : "（主页那边每 10 分钟来取一次）"}`
          : !mail && !x.can_reply ? "对方没留联系方式，回不了。看过点“不用管”就好。"
          : `还没起草。在右边写一句交代（也可以不写），点“${mail ? "按交代起草" : "交给Dais起草"}”。`}</div>`
        : draftZh ? `<div class="deskread">${zhOf(x, "draft") ? esc(zhOf(x, "draft")) : '<span class="muted">翻译中……</span>'}</div>`
        : !editable ? `<div class="deskread">${esc(x.draft || "")}</div>`
        : `<textarea class="draft" id="dd${key}" data-deskdraft="${key}" aria-label="回复草稿">${esc(deskDraftText(x))}</textarea>`;
      const leftHead = `<div class="deskhead"><span class="muted" style="font-size:12px">${sent ? `${x.draft ? (out ? "已经发出去的邮件" : "已经发出去的回复") : "已回"}${x.sent_at ? " · " + esc(String(x.sent_at).slice(11, 16)) : ""}`
          : x.state === "sending" ? "已确认，主页 10 分钟内从 hi@example.com 发出"
          : `${writer}起草的${out ? "新邮件" : "回复"}${draftZh ? "（中文版只给你看，改要在原文里改）" : manual ? "（对方只留了微信 / LinkedIn：复制去发）" : "（用对方的语言发；可以直接改）"}`}</span>
        ${hasDraft && !x.draft_is_zh ? flip(x, "draft") : ""}</div>`;
      const copy = `<button type="button" class="btn ${manual ? "primary" : ""}" data-act="deskcopy" data-k="${key}" title="复制到别处去发">复制</button>`;
      // 复制去别处发了，回来点“已回”告诉系统（她 2026-10-31）；只留微信 / LinkedIn 的留言一直显示
      const done = manual || (S.deskCopied || {})[key] ? btn("desk-replied", x, "已回", manual ? "" : "primary") : "";
      // 发了以后这条留到当天结束：还能记个待办；点标题就收起，第二天自动不再显示（她 2026-10-31）
      const leftBtns = sent || x.state === "sending" ? (x.task_id ? '<span class="muted" style="font-size:12px">已在待办里</span>' : btn("desk-todo", x, "记成待办"))
        : `${editable && !manual ? `<button type="button" class="btn primary" data-act="desksend" data-kind="${x.kind}" data-k="${key}" data-id="${x.id}">发送</button>` : ""}
          ${editable && manual ? copy + done : ""}
          ${btn("desk-ignore", x, "不用管")}${btn("desk-todo", x, "记成待办")}
          ${editable && !manual ? copy + done : ""}
          ${x.error ? `<span class="muted" style="font-size:12px">上次没发出去：${esc(x.error)}</span>` : ""}`;
      const noReply = !mail && !x.can_reply;
      // 新邮件：收件人能改，选用哪个邮箱发（她 2026-10-31：“邮件用新流程，从工作台发”）
      const outHead = out ? `<div class="deskhead" style="flex-wrap:wrap;gap:8px;justify-content:flex-start"><label class="muted deskaddr">发给 <input class="deskto" data-deskto="${key}" value="${esc((S.deskTo || {})[key] ?? x.to ?? "")}" ${editable ? "" : "disabled"}></label>
          <label class="muted deskaddr">用 <select data-deskacct="${key}" ${editable ? "" : "disabled"}>${accts.map((a) => `<option value="${esc(a.name)}" ${((S.deskAcct || {})[key] ?? x.account) === a.name ? "selected" : ""}>${esc(a.user)}</option>`).join("") || '<option value="">（还没有能发信的邮箱）</option>'}</select> 发</label></div>` : "";
      return `<div class="deskitem ${open ? "open" : ""}"><div class="row" style="gap:8px">
        <span class="kind ${out ? "mail" : x.kind}">${out ? "新邮件" : mail ? (x.via === "example.com" ? "主页来信" : "邮件") : "留言"}</span>
        <button type="button" class="linkish" data-act="deskopen" data-k="${key}">${String(x.title || "").startsWith(x.who) ? "" : esc(x.who) + "："}${esc(x.title)}<span class="muted" style="display:block;font-size:11px">${x.via_agent ? "由 agent 代留（" + esc(x.via_agent) + "）· " : ""}${esc(x.why || "")}${x.at ? " · " + esc(String(x.at).slice(5, 16).replace("T", " ")) : ""}</span></button>
        ${STATE[x.state] ? `<span class="deskstate">${STATE[x.state]}</span>` : ""}
        ${["sent", "sending"].includes(x.state) ? "" : btn("desk-ignore", x, "不用管", "small")}</div>
        ${open ? `<div class="col" style="gap:10px;margin-top:8px">
          ${out ? outHead : `<div class="deskhead"><span class="muted" style="font-size:12px">${mail ? "原邮件" : "留言原话"}${bodyZh ? "（中文翻译）" : ""}</span>
            <span class="deskright">${!mail && (x.email || x.contact) ? `<span class="muted" style="font-size:12px">联系方式：${esc([x.email, x.contact].filter(Boolean).join(" · "))}</span>` : ""}${x.is_zh ? "" : flip(x, "body")}</span></div>
          <div class="deskbody">${bodyZh ? (zhOf(x, "body") ? esc(zhOf(x, "body")) : '<span class="muted">翻译中……</span>') : esc(x.text || "")}</div>`}
          <div class="deskcols ${sent || x.state === "sending" || noReply ? "one" : ""}">
            <div class="dc-lh">${leftHead}</div>
            <div class="dc-lb">${left}</div>
            <div class="dc-lt row">${leftBtns}</div>
            ${sent || x.state === "sending" || noReply ? "" : `<div class="dc-rh deskhead"><label class="muted" style="font-size:12px" for="dn${key}">你的交代（可不写）</label></div>
            <div class="dc-rb"><textarea id="dn${key}" data-desknote="${key}" placeholder="${out ? "比如：语气再软一点 / 加一句我是交换生 / 改成问能不能退一半" : x.kind === "mail" ? "比如：客气地推掉 / 约下周三视频 / 用中文回也行" : "比如：谢谢夸奖，欢迎常来"}">${esc((S.deskNote || {})[key] ?? x.note ?? "")}</textarea></div>
            <div class="dc-rt row">${btn("desk-draft", x, x.state === "draft" || out ? "按交代重写" : mail ? "按交代起草" : "交给Dais起草", "gold")}</div>`}
          </div>
        </div>` : ""}</div>`; };
    // 邮件和主页留言分两个标签（她 2026-10-31）；发到 hi@example.com 的“主页来信”算在主页那边
    const site = (x) => x.kind === "visitor" || x.via === "example.com";
    const tabOf = { mail: (items || []).filter((x) => !site(x)), site: (items || []).filter(site) };
    const waiting = (list) => list.filter((x) => ["open", "draft"].includes(x.state)).length;
    const tab = S.deskTab || (waiting(tabOf.mail) || !waiting(tabOf.site) ? "mail" : "site");
    const tabBtn = (k, label) => `<button type="button" class="${tab === k ? "on" : ""}" data-act="desktab" data-tab="${k}">${label}${waiting(tabOf[k]) ? ` <span class="rbadge">${waiting(tabOf[k])}</span>` : ""}</button>`;
    box.innerHTML = `<section class="sheet prepsheet" aria-label="今日邮件和留言"><h2 style="font-size:18px">邮件和留言</h2>
      <div class="tabs">${tabBtn("mail", "邮件")}${tabBtn("site", "主页留言")}</div>
      <span class="muted" style="font-size:12px">${tab === "mail" ? "只放要你亲自看的邮件，别的 小伴 已经归好类、标了已读。回复由 小伴 起草，你点“发送”才发；复制去邮件 App 发的，回来点“已回”。"
        : "主页上要你回、或Dais能回的留言，以及发到 hi@example.com 的信。回复由Dais起草，你点“发送”才从 hi@example.com 发；只留了微信 / LinkedIn 的复制去发，回来点“已回”。"}</span>
      <div class="desklist">${items ? tabOf[tab].map(one).join("") || '<p class="muted">现在没有要你处理的。</p>' : '<p class="muted">加载中……</p>'}</div>
      ${((S.desk || {}).archived || []).length ? `<details class="deskarch" style="margin-top:12px"><summary class="muted" style="font-size:13px;cursor:pointer">这两天 小伴 归档了 ${S.desk.archived.length} 封（垃圾、推广、群发），点开抽查</summary>
        ${S.desk.archived.map((x) => `<div class="row" style="gap:8px;padding:5px 2px;border-top:1px solid rgba(127,127,127,.15);font-size:13px;display:flex;align-items:center"><span style="flex:1;min-width:0">${esc((x.sender || "").split("<")[0].trim() || x.sender || "")}：${esc(x.subject || "")}<span class="muted" style="display:block;font-size:11px">${esc({ junk: "垃圾", promo: "推广", newsletter: "群发" }[x.category] || x.category)} · ${esc(x.reason || "")}</span></span><button type="button" class="btn" data-act="desk-notjunk" data-id="${x.id}">不是垃圾</button></div>`).join("")}</details>` : ""}</section>`;
  } else if (S.overlay === "addtodo") {
    const goals = st.goals || [];
    box.innerHTML = `<form class="sheet small" data-form="addtodo" aria-label="加待办"><h2 style="font-size:18px">加一件待办</h2>
      <label>要做什么<input type="text" name="title" required></label>
      <label>属于哪个领域<select name="territory"><option value="">让 小伴 猜（默认）</option>${TERRS.map(([k, n]) => `<option value="${k}">${n}</option>`).join("")}<option value="waste">先放荒原</option></select></label>
      <label>什么时候做<select name="when"><option value="today">今天（进今日任务 ★）</option><option value="week">这周</option><option value="later">不急，让 AI 排</option></select></label>
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-act="close">取消</button><button class="btn primary">加上</button></div></form>`;
  } else if (S.overlay === "addevent") {
    box.innerHTML = `<form class="sheet small" data-form="event" aria-label="加日程"><h2 style="font-size:18px">加一个日程</h2>
      <label>什么事<input type="text" name="title" required></label>
      <div class="row" style="align-items:flex-end"><label style="flex:1">开始<input type="time" name="start" required></label><label style="flex:1">结束（可不填，默认 1 小时）<input type="time" name="end"></label></div>
      <label>地点（可不填）<input type="text" name="place"></label>
      <span class="muted" style="font-size:12px">今天的日程，会同步到苹果日历。</span>
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-act="close">取消</button><button class="btn primary">加上</button></div></form>`;
  } else if (S.overlay === "shop" || S.overlay === "side") {  // 商店、支线：原来任务页的两块，现在是首页的弹窗（她 2026-11-01）
    const q = S.quests, shop = S.overlay === "shop";
    box.innerHTML = `<section class="sheet prepsheet col" aria-label="${shop ? "商店" : "其他"}" style="gap:10px"><div class="row" style="justify-content:space-between;gap:8px">
      <span class="row" style="gap:10px;align-items:baseline"><h2 style="font-size:18px">${shop ? "商店" : "其他"}</h2>${shop ? `<span class="muted">积分 <b style="color:var(--text)">${S.state.profile.balance}</b></span>` : '<button type="button" class="btn small gold" data-act="addtodo">＋ 加一件</button>'}</span>
      <button type="button" class="btn" data-act="close">关闭</button></div>
      ${!q ? '<p class="loading">加载中……</p>' : shop ? shopBody(q) : sideBody(q)}</section>`;
  } else if (S.overlay.startsWith("boss:")) {
    const b = st.bosses.find((x) => String(x.goal_id) === S.overlay.split(":")[1]);
    const ch = S.quests && S.quests.chapters.find((c) => String(c.goal_id) === S.overlay.split(":")[1]);
    if (!b) { S.overlay = null; box.hidden = true; return; }
    const steps = ch ? ch.steps : S.quests ? [] : null;  // 还没拆主线的副本不在 chapters 里：别一直“加载中”（2026-11-01）
    const doneN = steps ? steps.filter((x) => x.state === "done").length : 0;
    // 名字旁边 ✎ 改名字，目标旁边 ✎ 改目标值和截止日（她 2026-11-01：原来那个“改名字、截止日、目标值”的大按钮拆开）
    const edit = S.goalEdit === b.goal_id ? S.goalEditWhat : null, ed = S.editGoal === b.goal_id;
    box.innerHTML = `<section class="sheet ${ch && ch.milestones ? "bosssheet" : "small"}" aria-label="${esc(b.name)}"><div class="row" style="justify-content:space-between;gap:8px">
        ${edit === "name" ? `<form class="row" data-form="goalset" data-id="${b.goal_id}" style="gap:6px;flex:1"><input type="text" name="title" value="${esc(b.title || b.name)}" style="flex:1" aria-label="副本名字"><button class="btn primary">保存</button><button type="button" class="btn" data-act="goaledit" data-id="${b.goal_id}" data-what="">取消</button></form>`
          : `<span class="row" style="gap:6px;align-items:center"><h2 style="font-size:18px">${b.main ? "★ " : ""}${esc(b.name)}${b.main ? "（主副本）" : ""}</h2><button type="button" class="pen" data-act="goaledit" data-id="${b.goal_id}" data-what="name" title="改名字" aria-label="改名字">✎</button></span>`}
        <button type="button" class="btn" data-act="close">关闭</button></div>
      ${edit === "target" ? `<form class="row" data-form="goalset" data-id="${b.goal_id}" style="gap:8px;align-items:flex-end;flex-wrap:wrap">
          ${b.target != null ? `<label style="flex:1">目标值${b.unit ? "（" + esc(b.unit) + "）" : ""}<input type="text" inputmode="decimal" name="target" value="${esc(String(b.target))}"></label>` : ""}
          <label style="flex:1">截止日<input type="date" name="due" value="${esc(b.due || "")}"></label>
          <button class="btn primary">保存</button><button type="button" class="btn" data-act="goaledit" data-id="${b.goal_id}" data-what="">取消</button></form>`
        : `<div class="row muted" style="font-size:13px;flex-wrap:wrap;gap:6px;align-items:center"><span>${esc(b.line)}</span>${b.due ? `<span>· 截止 ${esc(b.due)}</span>` : ""}${b.status ? `<span class="gold">· ${esc(b.status)}</span>` : ""}<button type="button" class="pen" data-act="goaledit" data-id="${b.goal_id}" data-what="target" title="改${b.target != null ? "目标值和" : ""}截止日" aria-label="改${b.target != null ? "目标值和" : ""}截止日">✎</button></div>`}
      <div class="bar" style="height:10px"><i style="width:${Math.round(100 * (b.frac || 0))}%"></i></div>
      ${b.comment ? `<span style="font-size:13px;line-height:1.6"><span class="gold">今天：</span>${esc(b.comment)}</span>` : ""}
      ${ch && ch.milestones ? milestoneView(b.goal_id, ch.milestones, ed) : steps === null ? '<p class="muted">加载中……</p>' : `<div class="row" style="justify-content:space-between"><b style="font-size:13px">主线 ${doneN} / ${steps.length} 步</b>${steps.length ? `<button type="button" class="btn small ${ed ? "gold" : ""}" data-act="editgoal" data-id="${b.goal_id}">${ed ? "完成" : "✎ 编辑"}</button>` : ""}</div>
      ${steps.length ? stepList(b.goal_id, steps, ed) : '<p class="muted">还没拆步骤。点下面“给这个副本加一步”，把阶段性的里程碑加进来，进度就按走到第几步算。</p>'}
`}
      ${ch && ch.milestones ? "" : stepAddForm(b.goal_id)}</section>`;
  } else if (S.overlay === "ethanday") {
    const d = S.ethanDays;
    box.innerHTML = `<section class="sheet prepsheet col" aria-label="小伴 的一天"><div class="row" style="justify-content:space-between"><h2>小伴 的一天</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <span class="muted" style="font-size:12px">这是他自己的日子，每天早上写好，聊天时照着过。是他的，不是你的资料，所以不放进“收藏和资料”。</span>
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
    box.innerHTML = `<section class="sheet wide col" aria-label="大地图"><div class="row" style="justify-content:space-between"><h2>大地图</h2><span class="row" style="gap:10px"><span class="legend"><span><i class="ldot s-ok"></i>达标</span><span><i class="ldot s-warn"></i>副本落后</span><span><i class="ldot s-bad"></i>跌破保底</span><span><i class="ldot s-none"></i>等数据</span></span><button type="button" class="btn phoneonly" data-act="close">关闭</button></span></div>
      <div class="mapbox">${mapImg()}${S.map ? mapTags(S.map) : ""}</div><span class="muted" style="font-size:12px">点一块领域进去看详情。</span></section>`;
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
      <span class="muted" style="font-size:12px">进度在商店的兑换记录里能看到。</span>
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
      ${x.status === "review" ? `<div class="row" style="gap:8px">${judge(`data-id="${x.id}"`, true)}${archBtn(x.id, true)}</div>`
        : ["failed", "dropped"].includes(x.status) && !x.archived_at ? `<div class="row" style="gap:8px">${archBtn(x.id, true)}</div>` : ""}
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
      ${x.state === "review" && x.key ? `<div class="row" style="gap:8px">${judge(`data-key="${esc(x.key)}" data-agent="${esc(x.agent)}" data-title="${esc(x.title)}"`, true)}</div>` : ""}
      ${x.agent === "claude" ? '<span class="muted" style="font-size:11px">Claude 会话只报项目名、你的第一句话和它最后一段回复的开头，完整对话在 Claude App 里。</span>' : ""}
    </section>`;
  } else if (S.overlay === "cardwish") {
    const c = S.cards || {};
    box.innerHTML = `<section class="sheet small col cardwish" aria-label="定制一张卡" style="gap:10px">
      <div class="row" style="justify-content:space-between"><h2 style="font-size:18px">想要什么样的卡</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <span class="muted" style="font-size:12px">场景、动作、拍照视角、穿搭都行，cosplay 也行（比如“cos 水手服战士在大本钟顶上”“鱼眼镜头跳起来接落叶”）。不点的话，小伴 每天按你当天的事自己编一个镜头。</span>
      <textarea data-cardwish rows="3" placeholder="写一句你想要的画面">${esc(S.cardWishDraft || "")}</textarea>
      <div class="row" style="gap:6px;flex-wrap:wrap"><button type="button" class="btn gold" data-act="cardwish" data-now="1">现在重画今天</button><button type="button" class="btn" data-act="cardwish" data-now="">留到明天画</button></div>
      ${(c.wishes || []).length ? `<div class="col" style="gap:4px"><span class="muted" style="font-size:12px">还没画的要求。“现在重画”的几条会合在一起画今天这张${c.drawing ? "（正在画，画完要是还有没用上的，会接着再画一次）" : ""}；“明天画”的一天用一条。</span>${c.wishes.map((w) => `<div class="item"><span class="grow" style="font-size:13px">${esc(w.text)}</span><span class="pill ${w.now ? "p-warn" : "p-none"}">${w.now ? (c.drawing ? "正在画" : "马上重画") : "明天画"}</span><button type="button" class="x" data-act="cardwishdrop" data-id="${w.id}" aria-label="删掉这条">✕</button></div>`).join("")}</div>` : ""}
    </section>`;
  } else if (S.overlay === "streakhelp") {  // 她 2026-11-01 问“满勤是什么的满勤”
    box.innerHTML = `<section class="sheet small"><div class="row" style="justify-content:space-between;gap:8px"><h2 style="font-size:18px">日常连续全勤</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <p>“全勤”是指<b>首页“日常”那一排当天该做的全部打了卡</b>，少一项那天就不算。</p>
      <p>从第一次打卡那天往后数，连着全勤几天就是几天；今天还没打完不算断。每周有 1 张补签卡，漏一天会自动补上；休整日只要做到一半就算。</p>
      <p class="muted" style="font-size:13px">连续 7 天 +50 积分，30 天 +300，还会出稀有、超稀有卡。</p></section>`;
  } else if (S.overlay === "seasonhelp") {
    box.innerHTML = `<section class="sheet small"><div class="row" style="justify-content:space-between;gap:8px"><h2 style="font-size:18px">${esc(st.season.name)}的进度</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <p>这个百分比是<b>赛季按时间走了多少</b>：${esc(st.season.start)} 开始，${esc(st.season.end)} 结束，今天走到了 ${st.season.pct}%。</p>
      <p>每个副本各到哪了，看首页右边的“副本进度”。</p>
      <p class="muted" style="font-size:13px">赛季还剩 ${st.season.days_left} 天。</p></section>`;
  } else if (S.overlay === "person") {
    box.innerHTML = personSheet();
  } else if (S.overlay === "xphelp") {
    box.innerHTML = `<section class="sheet small"><div class="row" style="justify-content:space-between;gap:8px"><h2 style="font-size:18px">经验和积分</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <p>每做完一件事，<b>经验和积分一起涨，一比一</b>，涨的数一样。</p>
      <p><b>经验</b>只涨不减，用来升级、换称号。<br><b>积分</b>是钱包，在商店拆惊喜会花掉，所以会比经验少。</p>
      <p class="muted" style="font-size:13px">怎么挣：主线一次性的 +40 / +100 / +200（看大小），反复做的每次 +10，每阶段一次的 +40，里程碑达成 +300（没达成但复盘了 +50）· 其他 +20 · 日常每项 +10，全勤再 +20，连续满勤 7 天 +50、30 天 +300 · 周常达标 +50，超额每次 +20。撤销完成会把分收回。</p></section>`;
  } else if (S.overlay === "budget") {  // 健康线“本周花销”保底旁边的 ✎（她 2026-11-01：不用收起来的框，弹窗改，选立即还是下月生效）
    const bp = S.budgetPlan || {};
    box.innerHTML = `<form class="sheet small col" data-form="budget" style="gap:10px" aria-label="改每月预算"><h2 style="font-size:18px">每月预算</h2>
      <span class="muted" style="font-size:13px">${bp.now ? `现在每月 ¥${Number(bp.now).toLocaleString()}` : "还没定"}${bp.next ? ` · ${Number(bp.next_month.slice(5))} 月起 ¥${Number(bp.next).toLocaleString()}` : ""}</span>
      <label>每月一共能花（人民币）<input type="text" inputmode="decimal" name="amount" required value="${bp.next ?? bp.now ?? ""}" placeholder="比如 8000"></label>
      <div class="row" style="gap:8px;flex-wrap:wrap"><label class="chip"><input type="radio" name="when" value="now" checked> 立即生效（这个月就按新的算）</label><label class="chip"><input type="radio" name="when" value="next"> 下个月起生效</label></div>
      <span class="muted" style="font-size:11px">英镑按汇率折成人民币一起算。健康线里每周的保底 = 月预算按这个月天数折成一周。</span>
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-act="close">取消</button><button class="btn primary">保存</button></div></form>`;
  } else if (S.overlay === "lifelog") {  // 社交那几项旁边的“＋”：直接记一笔（她 2026-11-01：原来埋得太深）
    const L = { meet: "见了朋友", new_friend: "认识了新朋友", contact: "联系了重要的人" };
    box.innerHTML = `<form class="sheet small col" data-form="lifelog" style="gap:10px" aria-label="记一笔"><h2 style="font-size:18px">记一笔</h2>
      <div class="row" style="gap:6px;flex-wrap:wrap">${Object.entries(L).map(([k, l]) => `<label class="chip"><input type="radio" name="kind" value="${k}" ${k === S.logKind ? "checked" : ""}> ${l}</label>`).join("")}</div>
      <label>是谁<input type="text" name="who" required placeholder="名字或称呼，几个人用顿号隔开"></label>
      <label>哪天<input type="date" name="day" value="${esc((S.state && S.state.day) || "")}"></label>
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-act="close">取消</button><button class="btn primary">记上</button></div></form>`;
  } else if (S.overlay === "makeup") {  // 补昨天的卡（她 2026-11-01）
    const left = ((st.habits && st.habits.daily) || []).filter((h) => !h.yesterday);
    box.innerHTML = `<section class="sheet small col" style="gap:10px" aria-label="补昨天的卡"><div class="row" style="justify-content:space-between;gap:8px"><h2 style="font-size:18px">补昨天的卡</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <div class="chips">${left.map((h) => `<button type="button" class="chip" data-act="makeupone" data-id="${h.id}">${esc(short(h.title))}</button>`).join("") || '<span class="muted">昨天的都打过了</span>'}</div></section>`;
  } else if (S.overlay === "explore") {  // 探索地图：去过、跳过这些随时改（她 2026-11-01：不能每次改完都上传一次）
    const e = S.explore;
    if (!e) { box.innerHTML = '<section class="sheet prepsheet"><p class="loading">加载中……</p></section>'; return; }
    const names = Object.keys(e.sheets), cur = S.exploreSheet && e.sheets[S.exploreSheet] ? S.exploreSheet : names[0];
    const all = e.sheets[cur] || [], sts = e.statuses[cur] || [], abroad = !e.domestic.includes(cur);
    S.exploreCur = cur;
    const q = (S.exploreQ || "").trim(), tag = sts.includes(S.exploreTag) ? S.exploreTag : "", sort = S.exploreSort || "";
    const st = (r) => r["状态"] || "未开始";  // 没标的就是未开始（她 2026-11-01：系统里不需要“没标”）
    const area = (r) => r["区域"] || r["位置"] || "";
    const label = (r) => [r["国家"], r["省份/区域"], r["省份"], r["城市"], r["项目"]].filter(Boolean).join(" · ");
    const key = { status: (r) => sts.indexOf(st(r)), name: label, area: (r) => area(r) || "~", visa: (r) => r["签证"] || "~" }[sort];
    const rows = all.map((r, i) => ({ r, i })).filter(({ r }) => (!q || [label(r), area(r), r["签证"] || ""].join(" ").includes(q)) && (!tag || st(r) === tag));
    if (key) rows.sort((x, y) => { const a = key(x.r), b = key(y.r); return typeof a === "number" ? a - b : String(a).localeCompare(String(b), "zh"); });
    const count = (v) => all.filter((r) => st(r) === v).length;
    const sel = "background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 8px";
    const visa = (r, i) => S.exploreEdit === i
      ? `<form class="row" data-form="explorevisa" data-i="${i}" style="gap:6px"><input type="text" name="v" value="${esc(r["签证"] || "")}" style="width:140px"><button class="btn small primary">存</button></form>`
      : `<span class="muted" style="font-size:12px">${esc(r["签证"] || "—")}</span><button type="button" class="pen" data-act="explorevisa" data-i="${i}" title="改签证">✎</button>`;
    box.innerHTML = `<section class="sheet prepsheet col" aria-label="探索地图" style="gap:10px"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">探索地图</h2><button type="button" class="btn" data-act="close">关闭</button></div>
      <div class="tabs" role="tablist">${names.map((k) => `<button type="button" role="tab" class="${k === cur ? "on" : ""}" data-act="exploresheet" data-v="${esc(k)}">${esc(k)}</button>`).join("")}</div>
      <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center"><input type="search" data-exploreq value="${esc(q)}" placeholder="搜地名、项目" style="flex:1;min-width:140px;${sel}">
        <select data-explore-tag aria-label="按标签筛选" style="${sel}"><option value="">全部 ${all.length}</option>${sts.map((v) => `<option value="${v}"${tag === v ? " selected" : ""}>${v} ${count(v)}</option>`).join("")}</select>
        <select data-explore-sort aria-label="排序" style="${sel}">${[["", "表格顺序"], ["status", "按状态"], ["name", "按名字"], ...(abroad ? [["area", "按区域"], ["visa", "按签证"]] : [])].map(([v, l]) => `<option value="${v}"${sort === v ? " selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="col" style="gap:4px;overflow:auto">${rows.map(({ r, i }) => `<div class="item" style="flex-wrap:wrap"><span class="grow">${esc(label(r))}${r["星标"] ? ' <span class="gold">★</span>' : ""}${abroad && area(r) ? ` <span class="muted" style="font-size:12px">${esc(area(r))}</span>` : ""}</span>
        ${abroad ? `<span class="row" style="gap:4px">${visa(r, i)}</span>` : ""}
        <select data-explore-row="${i}" data-sheet="${esc(cur)}" aria-label="状态">${sts.map((v) => `<option value="${v}"${st(r) === v ? " selected" : ""}>${v}</option>`).join("")}</select>
        <button type="button" class="x" data-act="exploredel" data-i="${i}" aria-label="删掉这一处">×</button></div>`).join("") || '<p class="muted">没有对得上的</p>'}</div>
      <form class="row stepadd" data-form="exploreadd" style="gap:6px;flex-wrap:wrap"><input type="text" name="name" required placeholder="${cur === "城市" ? "加一条路线" : cur === "中国" ? "城市" : "国家"}" style="flex:1;min-width:120px">
        ${cur === "城市" ? "" : `<input type="text" name="sub" placeholder="${cur === "中国" ? "省份" : "城市 / 区域（可不填）"}" style="width:150px">`}${abroad ? '<input type="text" name="visa" placeholder="签证（可不填）" style="width:130px">' : ""}
        <button class="btn gold">加上</button></form></section>`;
  } else if (S.overlay === "diarynote") {  // 日记批注：写给 小伴，他只改这一句（她 2026-11-02）
    const n = S.noteDraft || {};
    box.innerHTML = `<form class="sheet small col" data-form="diarynote" style="gap:10px" aria-label="批注"><h2 style="font-size:18px">批注</h2>
      <blockquote class="quote">${esc(n.text || "")}</blockquote>
      <textarea name="note" rows="3" required placeholder="哪里不对？比如：这是 小伴 在跟别人聊名字，不是我" style="background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px">${esc(n.note || "")}</textarea>
      <div class="row" style="justify-content:flex-end;gap:8px"><button type="button" class="btn" data-act="close">取消</button><button class="btn primary">交给 小伴 改这一句</button></div></form>`;
  } else if (S.overlay === "rename") {
    box.innerHTML = `<form class="sheet small col" data-form="rename" style="gap:10px" aria-label="改名字"><h2 style="font-size:18px">改名字</h2>
      <input type="text" name="title" required value="${esc(S.renameTitle || "")}">
      <div class="row" style="justify-content:flex-end"><button type="button" class="btn" data-act="close">取消</button><button class="btn primary">保存</button></div></form>`;
  } else if (S.overlay === "shopask") {
    const items = S.shopAsk || [];
    box.innerHTML = `<section class="sheet small col" aria-label="积分够换惊喜了" style="gap:12px"><h2 style="font-size:18px">🎁 积分够换惊喜了</h2>
      ${items.map((x) => `<div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:8px"><b>${esc(x.name.replace(/^能拆/, "能换"))}</b>
        <span class="row" style="gap:8px"><button type="button" class="btn" data-act="shopsave">再攒一攒</button><button type="button" class="btn primary" data-act="shopnow" data-id="${x.reward}">立即兑换</button></span></div>`).join("")}
      <span class="muted" style="font-size:12px">立即兑换会再让你确认一次；选再攒一攒，下次积分掉下去又攒够时再问你。</span></section>`;
  } else if (S.overlay === "shortof") {
    box.innerHTML = `<section class="sheet small quip"><p>${esc(S.quip)}</p></section>`;
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

async function loadDesk() {  // 收件台的数据单独拉（打开时、按了按钮后；草稿在写的时候 5 秒后再看一次）
  try { S.desk = await api("/api/desk"); } catch (e) { return; }
  if (S.overlay === "desk") renderOverlay();
  if (S.overlay === "desk" && (S.desk.items || []).some((x) => x.state === "drafting")) setTimeout(() => { if (S.overlay === "desk") loadDesk(); }, 5000);
}
function deskItem(k) { return ((S.desk || {}).items || []).find((x) => x.kind + x.id === k); }
function deskDraftText(x) { return (S.deskDraft || {})[x.kind + x.id] ?? x.draft ?? ""; }
function deskFetchZh(x, what) {  // 默认看翻译：没译好的，打开时去译一次
  const k = x.kind + x.id + what, src = what === "draft" ? deskDraftText(x) : "";
  S.deskAsking = S.deskAsking || {};
  if (S.deskAsking[k] === src + "|") return;
  S.deskAsking[k] = src + "|";
  api("/api/act", { action: "desk.translate", kind: x.kind, id: x.id, what, text: what === "draft" ? src : "" }, 60000)
    .then((r) => { if (r.ok) S.deskZh = { ...(S.deskZh || {}), [k]: { src, zh: r.zh } };
      else { showUndo(r.msg || "没翻成"); S.deskLang = { ...(S.deskLang || {}), [k]: "orig" }; }
      if (S.overlay === "desk") renderOverlay(); })
    .catch(() => { S.deskLang = { ...(S.deskLang || {}), [k]: "orig" }; if (S.overlay === "desk") renderOverlay(); });
}

// 返回服务器的结果（{ok, msg, ...}）：调用的地方要清草稿、收起，先看 ok
async function act(action, payload, el, quiet) {
  if (S.busy) return { ok: false, msg: "上一个操作还没回来" };  // 上一个操作还没回来就不接新的，免得加成两条
  if (/^(event|future)\./.test(action)) S.planDirty = true;  // 改了日程：关掉计划时 小伴 自动按新日程重排待办
  S.busy = true;
  document.body.classList.add("busy");
  let res;
  try { res = await send(action, payload); }
  finally { S.busy = false; document.body.classList.remove("busy"); }
  if (!res.ok) { if (!res.login) showUndo(res.msg); return res; }
  if (res.gain > 0 && el) pop(el, "+" + res.gain);
  if (!quiet) showUndo(res.msg || "好了", res.undo);
  await load(true);
  return res;
}

// 工作台开着、在前台：每分钟报一次，算进工作时间（她 2026-10-31 定）
// 只算真的在用：最近 3 分钟内点过、划过、打过字（她 2026-11-01：屏幕一直开着停在工作台，也被算成在工作）
let lastTouch = Date.now();
["pointerdown", "keydown", "wheel", "touchstart", "scroll"].forEach((t) => document.addEventListener(t, () => { lastTouch = Date.now(); }, { passive: true, capture: true }));
setInterval(() => { if (document.visibilityState === "visible" && S.state && Date.now() - lastTouch < 180000) api("/api/heartbeat", {}).catch(() => {}); }, 60000);

document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const a = el.dataset.act, id = el.dataset.id;
  if (a === "go") { leaveEdits();  // 离开页面：正在编辑的一律自动存上、收起编辑（她 2026-11-01：所有能编辑的地方都一样）
    S.page = el.dataset.page; S.brainBack = null; S.quests = null; S.team = null; S.map = null; S.overlay = null;
    if (S.page === "brain") { S.bq = null; S.brain = null; }  // 2026-10-29：从导航进大脑看全部，不沿用上次从某个领域带过来的筛选
    if (S.page === "map") { S.mapTab = ""; S.seasonWhy = true; }  // 进赛季页先看本季，依据默认展开
    // 从导航进来都回到默认筛选（2026-10-29 她说卡册的筛选没复原）
    if (S.page === "cards") { S.cardF = null; S.diaryAt = 0; S.cards = null; S.diaryMode = "book"; S.diaryEdit = null; }  // 每次进日记都从最新那天开始（她 2026-11-01）
    if (S.page === "ach") S.achTab = "";
    if (S.state) render();  // 先切过去（这页的数据没到就显示“加载中”），数据到了再画
    load(); }
  else if (a === "terr") { S.terrBack = S.overlay === "bigmap" ? "bigmap" : S.page === "territory" ? S.terrBack : S.page; S.overlay = null; S.page = "territory"; S.tkey = el.dataset.k; S.terr = null; render(); load(); }
  else if (a === "rate") act("agent.rate", { id, rating: Number(el.dataset.rating) });
  else if (a === "agent-drop") { if (S.dropAsk === id) { S.dropAsk = null; act("agent.drop", { id }); } else { S.dropAsk = id; showUndo("再点一次 ⊘ 就放弃这件活（不算失败）"); } }
  else if (a === "domain-ok") act("task.domain", { id, key: el.dataset.k });
  else if (a === "maptab") { S.mapTab = el.dataset.v; S.review = null; render(); load(); }
  else if (a === "setpro") act("settings.set", { proactive: !!el.dataset.v });
  else if (a === "settab") { S.setTab = el.dataset.v; render(); }
  else if (a === "cardf") { S.cardF = { ...(S.cardF || {}), [el.dataset.f]: el.dataset.v }; render(); }
  else if (a === "weekopen") { S.weekK = el.dataset.k; S.weekWhich = "this"; S.week = null; S.overlay = "week"; renderOverlay();
    api("/api/week").then((w) => { S.week = w; renderOverlay(); }).catch(() => showUndo("没连上服务器")); }
  else if (a === "weekwhich") { S.weekWhich = el.dataset.k; renderOverlay(); }
  else if (a === "diarymode") { leaveEdits(); S.diaryMode = el.dataset.v; S.diaryAt = 0; render(); }
  else if (a === "diaryturn") { leaveEdits(); const to = Number(el.dataset.v); S.diaryTurn = to > (S.diaryAt || 0) ? "back" : "fwd"; S.diaryAt = to; render(); }
  else if (a === "diaryedit") { S.diaryEdit = el.dataset.day; S.diaryDraft = null; S.diaryHeard = ""; render(); const t = document.querySelector("[data-diarydraft]"); if (t) t.focus(); }
  else if (a === "diarycancel") { if (diaryRec) { diaryRec.onstop = null; diaryRec.stop(); diaryRec = null; S.listening = false; } S.diaryEdit = null; S.diaryDraft = null; S.diaryHeard = ""; render(); }
  else if (a === "diarymic") diaryMic();
  else if (a === "diaryheardx") { S.diaryHeard = ""; S.diaryDraft = (document.querySelector("[data-diarydraft]") || {}).value; render(); }
  else if (a === "diaryappend") { const t = document.querySelector("[data-diarydraft]"); S.diaryDraft = ((t ? t.value : "").trim() + "\n\n" + S.diaryHeard).trim(); S.diaryHeard = ""; render(); }
  else if (a === "diarysave") { const t = document.querySelector("[data-diarydraft]"); diarySend("diary.save", { day: el.dataset.day, text: t ? t.value : "" }); }
  else if (a === "diaryweave") {
    const t = document.querySelector("[data-diarydraft]"), day = el.dataset.day, note = S.diaryHeard;
    const x = (S.cards && S.cards.cards || []).find((c) => c.day === day);
    showUndo("在写进去……");
    (async () => {  // 她改过还没保存的先存上，再按她说的融进去
      if (t && t.value.trim() && (!x || t.value.trim() !== (x.diary || "").trim())) {
        S.diaryDraft = t.value;
        const r = await send("diary.save", { day, text: t.value });
        if (!r.ok) return showUndo("改的没存上，先没往里加：" + r.msg);
      }
      diarySend("diary.weave", { day, note });
    })();
  }
  else if (a === "budgetedit") { S.budgetPlan = (S.terr && S.terr.budget) || null; S.overlay = "budget"; renderOverlay();
    if (!S.budgetPlan) api("/api/territory?k=money").then((t) => { S.budgetPlan = t.budget; if (S.overlay === "budget") renderOverlay(); }).catch(() => {}); }
  else if (a === "lifelogopen") { S.logKind = el.dataset.kind; S.overlay = "lifelog"; renderOverlay(); const i = document.querySelector('#overlay input[name="who"]'); if (i) i.focus(); }
  else if (a === "goaledit") { S.goalEdit = el.dataset.what ? Number(id) : null; S.goalEditWhat = el.dataset.what; renderOverlay(); }
  else if (a === "makeup") { S.overlay = "makeup"; renderOverlay(); }
  else if (a === "makeupone") act("habit.check", { id, yesterday: 1 }, el).then(() => { if (S.overlay === "makeup") renderOverlay(); });
  else if (a === "editgoal") { const g = Number(id); const was = S.editGoal; S.editGoal = was === g ? null : g; flushSteps(was !== null && was !== undefined).then(render); }
  else if (a === "stepnode") act("task.milestone", { id, on: el.dataset.on ? 1 : 0 }, el, true).then(async () => { S.quests = await api("/api/quests"); render(); });
  else if (a === "stepdel") act("task.delete", { id }, el).then(async () => { S.quests = await api("/api/quests"); render(); });
  else if (a === "cardopen") { S.cardView = S.cards.cards[Number(el.dataset.i)]; S.cardDiary = false; S.overlay = "cardview"; renderOverlay(); }
  else if (a === "carddiary") { S.back = "cardview"; S.overlay = "carddiary"; renderOverlay(); }
  else if (a === "prepopen") { S.prepOpen = S.prepOpen === Number(id) ? null : Number(id); renderOverlay(); }
  else if (a === "prepok") act("prep.approve", { id });
  else if (a === "prepfix") { const box = document.querySelector(`[data-prepnote="${id}"]`); act("prep.revise", { id, note: box ? box.value : "" }).then((r) => { if (r.ok && S.prepDraft) delete S.prepDraft[id]; }); }
  else if (a === "bq") {
    S.bq = { ...(S.bq || { tab: "all" }), [el.dataset.f]: el.dataset.v }; S.purge = null;
        load();
  }
  else if (a === "brain-terr") { S.brainBack = { page: "territory", tkey: S.tkey, terrBack: S.terrBack }; S.page = "brain"; S.bq = { tab: "all", k: el.dataset.k }; S.brain = null; render(); load(); }
  else if (a === "brainback") { const b = S.brainBack || {}; S.brainBack = null; S.page = "territory"; S.tkey = b.tkey; S.terrBack = b.terrBack; S.terr = null; render(); load(); }
  else if (a === "brain-set") act("brain.set", { id, status: el.dataset.status, was: el.dataset.was });
  else if (a === "brain-purge") { if (S.purge === Number(id)) { S.purge = null; act("brain.purge", { id }); } else { S.purge = Number(id); render(); } }
  else if (a === "goal-up") act("goal.up", { id });
  else if (a === "replan") act("plan.replan", {});
  else if (a === "money-ignore") act("money.ignore", { id, on: el.dataset.on || "" });
  else if (a === "rename") { S.renameId = id; S.renameTitle = el.dataset.title || ""; S.back = S.overlay; S.overlay = "rename"; renderOverlay();
    const i = document.querySelector('#overlay input[name="title"]'); if (i) { i.focus(); i.select(); } }  // 不用浏览器自带的白框（她 2026-11-01）
  else if (a === "celebrate-done") { const keys = (S.celebrating || []).map((x) => x.key); S.celebrating = null; S.overlay = (S.shopAsk || []).length ? "shopask" : null; S.askAt = Date.now(); if (S.state) S.state.celebrate = []; renderOverlay(); api("/api/act", { action: "ach.seen", keys }).catch(() => {}); }
  else if (a === "shopsave" || a === "shopnow") {
    if (Date.now() - (S.askAt || 0) < 1200) return;  // 弹出来一秒多以内的点击不算，免得连着勾任务时误点（10-31 误兑换 6 次）
    const keys = (S.shopAsk || []).map((x) => x.key), rid = el.dataset.id;
    S.shopAsk = []; api("/api/act", { action: "ach.seen", keys }).catch(() => {});
    if (a === "shopsave") { S.overlay = null; renderOverlay(); showUndo("好，接着攒"); }
    else api("/api/quests").then((q) => { S.quests = q; S.overlay = "redeem:" + rid; renderOverlay(); });
  }
  else if (a === "bopen") { S.brainOpen = Number(id); S.purge = null; S.overlay = "brain"; renderOverlay(); }
  else if (a === "tarchive") { S.overlay = "tarchive"; renderOverlay(); }
  else if (a === "seasonopen") {  // 点两次才开季（她 2026-11-02）
    if (S.confirmTask !== "season") { S.confirmTask = "season"; showUndo(el.dataset.confirm); setTimeout(() => { if (S.confirmTask === "season") S.confirmTask = null; }, 6000); return; }
    S.confirmTask = null; S.mapTab = ""; act("season.open", {}, el);
  }
  else if (a === "evrelease") act("event.release", { title: el.dataset.title }, el);
  else if (a === "delfuture") act("future.delete", { id }, el);
  else if (a === "delown") {  // 她自己日历上的约：点两次才删（会从苹果日历里真删掉）
    const href = el.dataset.href;
    if (S.confirmOwn !== href) { S.confirmOwn = href; showUndo("再点一次 × 从你的苹果日历里删掉"); setTimeout(() => { if (S.confirmOwn === href) S.confirmOwn = null; }, 6000); return; }
    S.confirmOwn = null; act("calendar.delete", { href }, el);
  }
  else if (a === "exploredel") {  // 删一处：点两次才删（她能撤销）
    const i = Number(el.dataset.i), sheet = S.exploreCur, key = sheet + ":" + i;
    if (S.confirmExplore !== key) { S.confirmExplore = key; showUndo("再点一次 × 删掉这一处"); setTimeout(() => { if (S.confirmExplore === key) S.confirmExplore = null; }, 6000); return; }
    S.confirmExplore = null;
    send("explore.delete", { sheet, i }).then(async (r) => {
      if (!r.ok) return showUndo("没删成：" + r.msg);
      showUndo(r.msg || "删了"); S.explore = await api("/api/explore").catch(() => S.explore); renderOverlay(); });
  }
  else if (a === "diarytool") { S.diaryTool = S.diaryTool === el.dataset.v ? null : el.dataset.v; showUndo(S.diaryTool === "hl" ? "在要标的句子上划一下" : S.diaryTool === "note" ? "在写错的句子上划一下，再写批注" : "退出了"); render(); }
  else if (a === "pastyear") { S.pastYear = el.dataset.v; render(); }
  else if (a === "shop" || a === "side") { S.overlay = a; renderOverlay(); load(true); }
  else if (a === "face") { store("eo_theme", themeKey(S.state) + ":" + (isNight(S.state) ? "day" : "night")); render(); }
  else if (a === "card") {
    if (S.state.card && S.state.card.image) openCard();
    else if (!S.state.card) act("card.make", {}).then(waitForCard);
  }
  else if (a === "desk") { resetOverlayState(); S.overlay = "desk"; S.back = null; S.desk = null; renderOverlay(); loadDesk(); }
  else if (a === "deskopen") { S.deskOpen = S.deskOpen === el.dataset.k ? null : el.dataset.k; renderOverlay(); }
  else if (["desk-draft", "desk-todo", "desk-ignore", "desk-replied"].includes(a)) {
    const k = el.dataset.k, box = document.querySelector(`[data-desknote="${k}"]`), x = deskItem(k);
    if (box) S.deskNote = { ...(S.deskNote || {}), [k]: box.value };
    act(a.replace("-", "."), { kind: el.dataset.kind, id, note: box ? box.value : "" }).then((r) => {
      if (r.ok) {  // 成了才清交代、收起；没成交代和草稿都留着
        if (S.deskNote) delete S.deskNote[k];
        // 已发送的那条点“记成待办”是她自己要跟进，照样留着；别的点了就收起
        if (a === "desk-ignore" || a === "desk-replied" || (a === "desk-todo" && !(x && ["sent", "sending"].includes(x.state)))) S.deskOpen = null;
        if (a === "desk-draft" && S.deskDraft) delete S.deskDraft[k];
      }
      loadDesk();
    }); }
  else if (a === "desksend") { const k = el.dataset.k, x = deskItem(k);
    el.disabled = true; el.textContent = "发送中……";
    const acct = document.querySelector(`[data-deskacct="${k}"]`), to = document.querySelector(`[data-deskto="${k}"]`);
    act("desk.send", { kind: el.dataset.kind, id, text: x ? deskDraftText(x) : "", account: acct ? acct.value : undefined, to: to ? to.value : undefined }).then((r) => { if (r.ok && S.deskDraft) delete S.deskDraft[k]; loadDesk(); }); }
  else if (a === "desktab") { S.deskTab = el.dataset.tab; S.deskOpen = null; renderOverlay(); }
  else if (a === "desklang") {  // 看原文 / 看翻译：只给她看，发出去的还是原文
    const k = el.dataset.k + el.dataset.what;
    S.deskLang = { ...(S.deskLang || {}), [k]: ((S.deskLang || {})[k] || "zh") === "zh" ? "orig" : "zh" }; renderOverlay(); }
  else if (a === "deskcopy") { const x = deskItem(el.dataset.k);
    if (x && navigator.clipboard) navigator.clipboard.writeText(deskDraftText(x)).then(() => showUndo("复制好了，发完回来点“已回”"));
    S.deskCopied = { ...(S.deskCopied || {}), [el.dataset.k]: true }; renderOverlay(); }
  else if (["plan", "prep"].includes(a)) { resetOverlayState(); S.overlay = a; S.back = null; renderOverlay(); }
  else if (a === "detail") { S.overlay = "detail"; S.detailK = el.dataset.k; S.back = null; renderOverlay(); if (typeof animateMoves === "function") animateMoves(); }
  else if (a === "addtodo" || a === "addevent") {
    S.back = S.overlay && (S.overlay === "plan" || S.overlay === "side" || S.overlay.startsWith("boss:")) ? S.overlay : null;
    S.goal = el.dataset.goal || ""; S.overlay = a; renderOverlay();
  }
  else if (a === "boss") {
    resetOverlayState(); S.overlay = "boss:" + id; renderOverlay();
    if (!S.quests) api("/api/quests").then((q) => { S.quests = q; if (S.overlay === "boss:" + id) renderOverlay(); }).catch(() => {});
  }
  else if (a === "close") { S.overlay = ["addtodo", "addevent", "rename"].includes(S.overlay) ? S.back : null; S.back = null; resetOverlayState(); render(); }
  else if (a === "showdone") { S.showDone = !S.showDone; render(); }
  else if (a === "exploreopen") { S.overlay = "explore"; S.explore = null; S.exploreTag = "未开始"; renderOverlay(); api("/api/explore").then((e) => { S.explore = e; if (S.overlay === "explore") renderOverlay(); }); }
  else if (a === "exploresheet") { S.exploreSheet = el.dataset.v; S.exploreQ = ""; S.exploreEdit = null; renderOverlay(); }
  else if (a === "explorevisa") { S.exploreEdit = Number(el.dataset.i); renderOverlay(); const i = document.querySelector('[data-form="explorevisa"] input'); if (i) i.focus(); }
  else if (a === "habit") act(el.dataset.done ? "habit.uncheck" : "habit.check", { id }, el);
  else if (a === "task") {
    if (el.dataset.confirm && S.confirmTask !== id) { S.confirmTask = id; showUndo(el.dataset.confirm); setTimeout(() => { if (S.confirmTask === id) S.confirmTask = null; }, 6000); return; }
    S.confirmTask = null; act(el.dataset.done ? "task.undo" : "task.done", { id }, el);
  }
  else if (a === "unhang") act("stage.set", { id, depends_on: null }, el, true).then(async () => { S.quests = await api("/api/quests"); render(); });
  else if (a === "star") act("task.star", { id, on: !!el.dataset.on });
  else if (a === "postpone") act("task.postpone", { id });
  else if (a === "deltask") act("task.delete", { id });
  else if (a === "delevent") act("event.delete", { id });
  else if (a === "dropprep") act("prep.drop", { id });
  else if (a === "agent-archive") {
    if (String(S.overlay || "").startsWith("acard:")) { S.overlay = null; renderOverlay(); }
    act("agent.archive", { id }).then(() => { S.team = null; load(); });
  }
  else if (a === "agent-misreport" || a === "agent-fail") {
    const d = el.dataset, p = d.key ? { key: d.key, agent: d.agent, title: d.title } : { id };
    if (String(S.overlay || "").startsWith("acard:") || String(S.overlay || "").startsWith("alive:")) { S.overlay = null; renderOverlay(); }
    act(a === "agent-fail" ? "agent.fail" : "agent.misreport", p).then(() => { S.team = null; load(); });
  }
  else if (a === "cardwish") {
    const box = document.querySelector("[data-cardwish]"), text = box ? box.value.trim() : "";
    if (!text) { showUndo("先写一句想要的画面"); }
    else act("card.wish", { text, now: el.dataset.now ? 1 : 0 }).then((r) => { if (!r.ok) return; S.cardWishDraft = ""; S.overlay = null; renderOverlay(); S.cards = null; load(); });
  }
  else if (a === "cardwishdrop") act("card.wishdrop", { id }).then(async () => { S.cards = await api("/api/cards"); renderOverlay(); render(); });
  else if (a === "cardwishopen") { S.overlay = "cardwish"; renderOverlay(); }
  else if (a === "preppend") act("prep.pending", { id });
  else if (a === "prepunpend") act("prep.unpend", { id });
  else if (a === "preptab") {
    S.prepTab = el.dataset.tab || ""; S.prepOpen = null;
    if (S.prepTab === "parked" && S.state.parked_remind) { S.state.parked_remind = 0; act("prep.parkedseen", {}, null, true); }  // 看过一次，小红点就消失（2026-10-30 她要）
    renderOverlay();
  }
  else if (a === "preprestore") act("prep.restore", { id });
  else if (a === "prepstar") { const t = S.state.prep.find((x) => x.id === Number(id)); if (t) t.star = !!el.dataset.on; renderOverlay(); act("prep.star", { id: Number(id), on: el.dataset.on ? 1 : 0 }, el, true); }
  else if (a === "prepgo") { S.prepOpen = Number(id); S.prepTab = ""; S.overlay = "prep"; renderOverlay(); }  // 团队页点准备员的活，直接打开那一件
  else if (a === "prepdone") act("prep.done", { id });
  else if (a === "desk-notjunk") act("desk.notjunk", { id }).then(() => loadDesk());
  else if (a === "rest") act("rest.take", {});
  else if (a === "restcancel") act("rest.cancel", {});
  else if (a === "redeem") { S.overlay = "redeem:" + id; renderOverlay(); }
  else if (a === "delivered") act("reward.delivered", { id: Number(id) });
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
  else if (a === "streakhelp" || a === "seasonhelp") { S.overlay = a; renderOverlay(); }
  else if (a === "shortof") showUndo(quip(Number(el.dataset.gap)));  // 不弹窗，提示几秒自己消失（她 2026-11-02）
  else if (a === "redeemok") { S.overlay = "redeemed"; renderOverlay(); act("reward.redeem", { id }, el, true); }
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
  if (f.dataset.form === "diarynote") {
    const n = S.noteDraft || {}; S.overlay = null; S.diaryTool = null; renderOverlay(); showUndo("小伴 在改这一句……");
    send("diary.note", { day: n.day, text: n.text, note: d.note }, 120000).then((r) => {
      if (!r.ok) {  // 没记上：批注框带着她写的字重新打开
        S.noteDraft = { ...n, note: d.note }; S.diaryTool = "note"; S.overlay = "diarynote"; renderOverlay();
        return showUndo("批注没记上：" + r.msg);
      }
      showUndo(r.msg || "记上了");
      const c = ((S.cards && S.cards.cards) || []).find((x) => x.day === n.day);
      if (c) { c.notes = r.notes; if (r.diary) c.diary = r.diary; }
      render();
    });
    return;
  }
  if (f.dataset.form === "exploreadd") {  // 工作台是探索地图唯一的数据源（她 2026-11-02）：在这里加地方
    send("explore.add", { sheet: S.exploreCur, name: d.name, sub: d.sub || "", visa: d.visa || "" }).then(async (r) => {
      if (!r.ok) return showUndo("没加上：" + r.msg);  // 没加上：表单不重画，填的字还在
      showUndo(r.msg || "加上了"); S.explore = await api("/api/explore").catch(() => S.explore); renderOverlay(); });
    return;
  }
  if (f.dataset.form === "explorevisa") {  // 签证内容她可以改（2026-11-01）
    const sheet = S.exploreCur, i = Number(f.dataset.i), v = (d.v || "").trim();
    send("explore.set", { sheet, i, field: "签证", value: v }).then((r) => {
      if (!r.ok) return showUndo("没改成：" + r.msg);  // 没改成：编辑框不收，写的字还在
      S.exploreEdit = null;
      const row = S.explore.sheets[sheet][i]; if (v) row["签证"] = v; else delete row["签证"];
      showUndo(r.msg); renderOverlay();
    });
    return;
  }
  if (f.dataset.form === "rename") {
    const rid = S.renameId, back = S.back, title = d.title.trim();
    S.overlay = back; S.back = null; renderOverlay();
    act("task.rename", { id: rid, title }).then((r) => {  // 没改成：改名框带着她写的名字重新打开
      if (r.ok || r.login) return;
      S.renameId = rid; S.renameTitle = title; S.back = back; S.overlay = "rename"; renderOverlay();
    });
    return;
  }
  // 下面这些表单：服务器回了 ok 才关表单、重画（没成就留着她填的字，act 里已经提示了原因）
  const closeOk = (fn) => (r) => { if (r.ok) fn(); };
  if (f.dataset.form === "budget") { act("money.budget", { amount: d.amount, when: d.when }).then(closeOk(() => { S.overlay = null; renderOverlay(); })); return; }
  if (f.dataset.form === "stageadd") { act("stage.add", { milestone_id: f.dataset.ms, title: d.title, cadence: d.cadence, per_week: d.per_week }).then(async (r) => { if (!r.ok) return; S.quests = await api("/api/quests").catch(() => S.quests); render(); }); return; }
  if (f.dataset.form === "msadd") { act("milestone.add", { goal_id: f.dataset.goal, title: d.title, due: d.due }).then(async (r) => { if (!r.ok) return; S.quests = await api("/api/quests").catch(() => S.quests); render(); }); return; }
  if (f.dataset.form === "stepadd") { act("step.add", { goal_id: f.dataset.goal, title: d.title, milestone: d.node ? 1 : 0 }).then(async (r) => { if (!r.ok) return; S.quests = await api("/api/quests").catch(() => S.quests); render(); }); return; }
  if (f.dataset.form === "lifelog") { act("life.log", { kind: d.kind, who: d.who, day: d.day }).then(closeOk(() => { S.overlay = null; renderOverlay(); })); return; }
  if (f.dataset.form === "goalset") { act("goal.set", { id: f.dataset.id, title: d.title, due: d.due, target: d.target }).then(closeOk(() => { S.goalEdit = null; render(); renderOverlay(); })); return; }
  if (f.dataset.form === "event" && d.title && d.start) {
    act("event.add", { title: d.title, start: d.start, end: d.end || "", place: d.place }).then(closeOk(() => { S.overlay = S.back; S.back = null; render(); }));
  }
  if (f.dataset.form === "addtodo" && d.title) {
    act("task.add", { title: d.title, goal_id: d.goal_id || null, when: d.when, territory: d.territory || "" }).then(closeOk(() => { S.overlay = S.back; S.back = null; render(); }));
  }
});

// 点弹窗外面的空白处也能关（2026-10-29 她要所有弹窗都这样）；加待办这类从别的弹窗开出来的，退回上一个
$("#overlay").addEventListener("click", (ev) => {
  if (ev.target !== ev.currentTarget || !S.overlay) return;
  if (S.overlay === "celebrate") { const b = document.querySelector('#overlay [data-act="celebrate-done"]'); if (b) b.click(); return; }
  S.overlay = ["addtodo", "addevent", "carddiary"].includes(S.overlay) ? S.back : null; S.back = null; render();
});
document.addEventListener("change", (ev) => {
  if (ev.target.matches && ev.target.matches("[data-monthpick]")) { S.cardF = { ...(S.cardF || {}), m: ev.target.value }; render(); }
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && S.overlay) { S.overlay = null; S.back = null; render(); } });
// 日记本：左右方向键、手指左右滑翻页
document.addEventListener("keydown", (ev) => {
  if (S.page !== "cards" || S.overlay || S.diaryMode !== "book" || !["ArrowLeft", "ArrowRight"].includes(ev.key)) return;
  const btn = document.querySelectorAll(".dturn2")[ev.key === "ArrowLeft" ? 0 : 1];
  if (btn && !btn.disabled) btn.click();
});
let _sx = null;
document.addEventListener("touchstart", (ev) => {  // 荧光笔 / 批注模式里在字上划，是划线不是翻页
  _sx = ev.target.closest(".spread") && !(S.diaryTool && ev.target.closest(".ink")) ? ev.touches[0].clientX : null;
}, { passive: true });
document.addEventListener("touchend", (ev) => {
  if (_sx === null) return;
  const dx = ev.changedTouches[0].clientX - _sx; _sx = null;
  if (Math.abs(dx) < 50) return;
  const btn = document.querySelectorAll(".dturn2")[dx > 0 ? 0 : 1];
  if (btn && !btn.disabled) btn.click();
}, { passive: true });

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

// 批注写到一半，页面每分钟刷新也不丢（2026-10-30 她在 iPad 上用手写笔批注 小伴 准备的东西）
document.addEventListener("input", (e) => {
  if (e.target && e.target.dataset && "cardwish" in e.target.dataset) S.cardWishDraft = e.target.value;
  const id = e.target && e.target.dataset && e.target.dataset.prepnote;
  if (id) { S.prepDraft = S.prepDraft || {}; S.prepDraft[id] = e.target.value; }
});

// 收件台里打的字先记着，重画（比如草稿写好了自动刷新）时不丢
document.addEventListener("input", (ev) => {
  const el = ev.target;
  if (el.dataset.desknote) S.deskNote = { ...(S.deskNote || {}), [el.dataset.desknote]: el.value };
  if (el.dataset.deskdraft) S.deskDraft = { ...(S.deskDraft || {}), [el.dataset.deskdraft]: el.value };
  if (el.dataset.deskto) S.deskTo = { ...(S.deskTo || {}), [el.dataset.deskto]: el.value };
  if (el.dataset.deskacct) S.deskAcct = { ...(S.deskAcct || {}), [el.dataset.deskacct]: el.value };
});

// ---------- “我”页（ethan/mepage.py；她 2026-10-31 定稿，docs/design/memory-alignment.md 第 9 节） ----------
// 第一屏：结论、升级建议、十个方面（meBoard）；右上角五个弹层：待你定、问一问、全部记忆、谁在用和纠错、删除和对外
const pct = (r) => (r === null || r === undefined ? "—" : Math.round(r * 100) + "%");
// “我”页第一屏（ontology-plan 2.2，她 11-03 选样稿 A：左边结论和升级建议，右边 10 个方面的格子，点一下切成星图）
const ME_STATE = { thick: "厚", thin: "薄", stale: "旧", empty: "空" };
const ME_STAR_XY = [[60, 50], [160, 34], [262, 56], [100, 124], [196, 112], [290, 136], [56, 196], [150, 214], [246, 188], [300, 240]];
const ME_STAR_LINKS = [[0, 1], [1, 2], [0, 3], [3, 4], [4, 1], [4, 5], [2, 5], [3, 6], [6, 7], [7, 4], [7, 8], [8, 5], [8, 9]];
function meSentence(dims) {  // 一句结论：规则拼出来，不靠模型
  const by = (k) => dims.filter((d) => d.state === k);
  const thick = by("thick").sort((a, b) => b.used - a.used || b.hers - a.hers).slice(0, 2).map((d) => d.name);
  const asked = dims.filter((d) => ["empty", "thin"].includes(d.state) && d.used + d.missed > 0).sort((a, b) => (b.used + b.missed) - (a.used + a.missed));
  const empty = by("empty").map((d) => d.name);
  const parts = [];
  parts.push(thick.length ? `${thick.join("和")}记得最厚` : "还没有哪个方面记得够厚");
  if (asked.length) parts.push(`${asked.slice(0, 2).map((d) => d.name).join("、")}很薄，这周却被问到 ${asked.slice(0, 2).reduce((n, d) => n + d.used + d.missed, 0)} 次`);
  else if (empty.length) parts.push(`${empty.slice(0, 2).join("、")}${empty.length > 2 ? `等 ${empty.length} 个方面` : ""}还空着`);
  return parts.join("；") + "。";
}
function meBoard(mt, rate) {
  if (!mt) return "";
  const dims = mt.dims || [];
  const star = S.meStar;
  const tile = (d) => { const tot = Math.max(d.n, 1), conf = Math.min(d.confirmed, d.hers), other = d.n - d.hers;
    const seg = (n, c) => n ? `<i class="${c}" style="width:${(n / tot * 100).toFixed(1)}%"></i>` : "";
    return `<button type="button" class="metile ${d.state}" data-act="me-dimopen" data-k="${esc(d.dim)}">
      <span class="t">${esc(d.name)}</span><span class="st">${ME_STATE[d.state]}</span>
      <span class="mebarx">${seg(conf, "c")}${seg(d.hers - conf, "h")}${seg(other, "s")}</span>
      <span class="u">${d.n} 条　被查 ${d.used + d.missed} 次${d.missed ? `，<b class="miss">没查到 ${d.missed}</b>` : ""}</span></button>`; };
  const max = Math.max(1, ...dims.map((d) => d.hers));
  const xy = (i) => ME_STAR_XY[i % ME_STAR_XY.length];
  const stars = `<svg class="mestars" viewBox="0 0 350 290" role="img" aria-label="十个方面的星图">
      ${ME_STAR_LINKS.filter(([a, b]) => dims[a] && dims[b]).map(([a, b]) => `<line class="link ${dims[a].state === "empty" || dims[b].state === "empty" ? "faint" : ""}" x1="${xy(a)[0]}" y1="${xy(a)[1]}" x2="${xy(b)[0]}" y2="${xy(b)[1]}"/>`).join("")}
      ${dims.map((d, i) => { const [x, y] = ME_STAR_XY[i % ME_STAR_XY.length]; const r = d.state === "empty" ? 7 : 6 + 12 * Math.sqrt(d.hers / max);
        return `<g class="mestar ${d.state}" data-act="${d.n ? "me-stardim" : "me-dimopen"}" data-k="${esc(d.dim)}" tabindex="0" role="button" aria-label="${esc(d.name)}：${ME_STATE[d.state]}，你说过 ${d.hers} 条">
          ${d.state === "empty" ? `<circle cx="${x}" cy="${y}" r="${r}" class="ring"/>` : `<circle cx="${x}" cy="${y}" r="${r + 7}" class="halo"/><circle cx="${x}" cy="${y}" r="${r}" class="core"/>`}
          <text x="${x}" y="${y + r + 15}" text-anchor="middle">${esc(d.name)}</text></g>`; }).join("")}</svg>`;
  const drill = star && S.meStarDim ? dims.find((d) => d.dim === S.meStarDim) : null;
  const sub = (d) => { const ps = d.props || [], pmax = Math.max(1, ...ps.map((p) => p.n)), cx = 175, cy = 140, R = ps.length > 1 ? 100 : 0;
    return `<div class="row" style="gap:8px;align-items:center;margin-bottom:4px"><button type="button" class="chip" data-act="me-stardim" data-k="">回到十个方面</button>
        <button type="button" class="chip" data-act="me-dimopen" data-k="${esc(d.dim)}">看${esc(d.name)}的全部 ${d.n} 条</button></div>
      <svg class="mestars" viewBox="0 0 350 290" role="img" aria-label="${esc(d.name)}里的细项">
      <circle cx="${cx}" cy="${cy}" r="24" class="hub ${d.state}"/><text x="${cx}" y="${cy + 4}" text-anchor="middle" class="hubt">${esc(d.name)}</text>
      ${ps.map((p, i) => { const a = -Math.PI / 2 + i * 2 * Math.PI / ps.length, x = cx + R * Math.cos(a), y = cy + R * Math.sin(a) + (R ? 0 : 80), r = 5 + 11 * Math.sqrt(p.n / pmax);
        return `<line class="link" x1="${cx}" y1="${cy}" x2="${x}" y2="${y}"/><g class="mestar ${p.hers ? d.state : "stale"}" data-act="me-propopen" data-dim="${esc(d.dim)}" data-k="${esc(p.key)}" data-name="${esc(p.name)}" tabindex="0" role="button" aria-label="${esc(p.name)} ${p.n} 条">
          <circle cx="${x}" cy="${y}" r="${r + 6}" class="halo"/><circle cx="${x}" cy="${y}" r="${r}" class="core"/><text x="${x}" y="${y + r + 14}" text-anchor="middle">${esc(p.name)} ${p.n}</text></g>`; }).join("")}</svg>`; };
  const sug = (mt.suggestions || []).map((x) => `<div class="mesug"><span class="who ${x.who === "你" ? "me" : "sys"}">${x.who === "你" ? "你来" : "系统来"}</span>
      <div><p>${esc(x.what)}</p><span class="muted">${esc(x.weak)}　做完看：${esc(x.watch)}</span></div></div>`).join("") || '<p class="muted" style="font-size:13px">这周没有要升级的地方。</p>';
  // 最近升级（ontology-plan 4.1）：小字一行一条，最多 3 条，点开看前两周 → 后两周的数字
  const ufmt = (k, v) => (v === null || v === undefined ? "—" : k === "share" ? pct(v) : v);
  const upg = (mt.recent_upgrades || []).slice(0, 3).map((u) => `<details class="meupg"><summary><span class="muted">${esc(u.at.slice(5))} · ${esc(u.kind)}</span> ${esc(u.what)}</summary>
      <table>${(u.numbers || []).map((n) => `<tr><td>${esc(n.name)}</td><td>${ufmt(n.key, n.before)}</td><td>→</td><td>${u.after ? ufmt(n.key, n.after) : "满两周再看"}</td></tr>`).join("")}</table></details>`).join("");
  const upgs = upg ? `<div class="meupgs"><span class="muted">最近升级　前两周 → 后两周</span>${upg}</div>` : "";
  const said = mt.said || {}, total = (said.hers || 0) + (said.system || 0) + (said.behavior || 0) + (said.others || 0);
  const rt = mt.routing || {};
  const route = (rt.by_question || rt.fallback) ? `<p class="muted mix">这周按题答 ${rt.by_question} 次，兜底检索 ${rt.fallback} 次${rt.share !== null && rt.share !== undefined ? `（按题占 ${pct(rt.share)}）` : ""}</p>` : "";
  return `<section class="meboard">
    <div class="meverdict"><div class="lead"><p class="line">${esc(meSentence(dims))}</p>
      <p class="muted mix">${rate !== undefined && rate !== null ? `这周对得上 ${pct(rate)}　` : ""}${total ? `关于你的 ${total} 条里，` + [["你说的、你给的", said.hers], ["行为记录", said.behavior], ["系统推的", said.system], ["别人说的", said.others]].filter((x) => x[1]).map((x) => `${x[0]} ${x[1]} 条`).join("，") : "还没有关于你的记忆"}</p>${route}</div>
      ${sug}${upgs}</div>
    <div class="memap"><div class="row" style="justify-content:space-between;align-items:center;margin-bottom:8px">${star ? '<span class="mekey">十个方面的星图</span>' : '<span class="mekey"><i class="c"></i>你确认过的<i class="h"></i>你说的<i class="s"></i>别的来源</span>'}
      <span class="chips"><button type="button" class="chip ${star ? "" : "on"}" data-act="me-star" data-k="">格子</button><button type="button" class="chip ${star ? "on" : ""}" data-act="me-star" data-k="1">星图</button></span></div>
      ${star ? (drill ? sub(drill) : stars) : `<div class="metiles">${dims.map(tile).join("")}</div>`}
      <p class="muted melegend">${star ? (drill ? "每颗小星是这个方面里的一项，点一颗看那一项的记忆。" : "星越大，你说过的越多；绿色是厚的，金色是薄的，灰色是旧的，虚线圈还空着。点一颗星往里看。") : "厚：你说过的够用　薄：条数少或只有一种来源　旧：一个月没更新　空：还没说过。点一个方面看它的全部记忆"}</p></div></section>`;
}
function mePage() {
  const m = S.me;
  if (!m) return '<p class="loading">加载中……</p>';
  // 第一屏只放结论、升级建议和十个方面（她 11-04：“旧版的还没下”）；对得上的比例只在结论下面一行，使用者和纠错单挪进“谁在用和纠错”
  return `<header class="row mehead"><h2 style="font-size:22px">我</h2><span class="metools">
      <button type="button" class="btn" data-act="me-open" data-k="me-pending">待你定${m.pending ? ` <i class="navdot inline">${m.pending}</i>` : ""}</button>
      ${((S.state || {}).badges || {}).brain ? `<button type="button" class="btn gold" data-act="me-brain">收藏和资料 <i class="navdot inline">${S.state.badges.brain}</i></button>` : ""}
      <button type="button" class="btn" data-act="me-open" data-k="me-ask">🔍 问一问</button>
      <button type="button" class="btn" data-act="me-open" data-k="me-all">全部记忆</button>
      <button type="button" class="btn" data-act="me-open" data-k="me-users">谁在用和纠错</button>
      <button type="button" class="btn" data-act="me-open" data-k="me-manage">删除和对外</button></span></header>
    ${meBoard(m.metrics, m.rate)}`;
}
// 第二层“谁在用和纠错”：使用者小卡（点开看详细，下面的纠错单跟着它变）+ 纠错单；原来在第一屏下面，11-04 挪进来
function meUsersSheet() {
  const m = S.me, close = '<button type="button" class="btn" data-act="close">关闭</button>';
  const head = `<div class="row" style="justify-content:space-between"><h2 style="font-size:18px">谁在用和纠错</h2>${close}</div>`;
  if (!m) return `<section class="sheet mesheet wide" aria-label="谁在用和纠错">${head}<p class="muted">加载中……</p></section>`;
  const sel = S.meUser;
  const card = (u) => `<button type="button" class="meuser ${sel === u.user ? "on" : ""} ${u.connected ? "" : "off"}" data-act="me-user" data-k="${esc(u.user)}" ${u.connected ? "" : "disabled"}>
      <span class="n"><span>${esc(u.name)}</span><span>${u.connected ? (sel === u.user ? "▾" : "▸") : ""}</span></span>
      ${u.members && u.members.length > 1 ? `<span class="s" style="display:block">${esc(u.parts)}</span>` : ""}
      ${u.connected ? `<b>${pct(u.accuracy)}</b> <span class="s">这周用了 ${u.queries} 次</span><span class="bar"><i style="width:${Math.round((u.accuracy ?? 0) * 100)}%"></i></span>` : '<span class="s">登记了，还没接上</span>'}</button>`;
  const su = m.users.find((u) => u.user === sel);
  const detail = su ? `<div class="medetail"><table>
      <tr><td>这周查了几次记忆</td><td>${su.queries}</td></tr><tr><td>用上了记忆</td><td>${su.with_memory}</td></tr><tr><td>没查到</td><td>${su.misses}</td></tr>
      <tr><td>被你纠正</td><td class="${su.corrections ? "warn" : ""}">${su.corrections}</td></tr>
      <tr><td>错在哪</td><td>${Object.entries(su.layers || {}).map(([k, v]) => esc(k) + " " + v).join("、") || "—"}</td></tr>
      <tr><td>上周用对的比例</td><td>${pct(su.last_week)}</td></tr></table>
      <div><div class="muted" style="font-size:12px;margin-bottom:4px">这周用得最多的记忆</div>
      ${(su.top || []).map((t) => `<button type="button" class="linkish" style="display:block;font-size:13px;line-height:1.9;text-align:left" data-act="me-jump" data-q="${esc(t.text)}">${esc(t.text)} <span class="muted" style="font-size:12px">· ${t.n} 次</span></button>`).join("") || '<p class="muted" style="font-size:13px">这周还没用到</p>'}</div></div>` : "";
  const tickets = m.tickets.filter((t) => !sel || t.user === sel);
  const tk = (t) => { const open = S.meTicket === t.id;
    return `<div class="meticket ${open ? "open" : ""}"><button type="button" class="linkish" data-act="me-ticket" data-id="${t.id}">
      <span class="muted" style="font-size:12px;display:block">${esc(t.at.slice(5))} · ${esc(t.entry)} · ${esc(t.user_name)} · ${esc(t.layer)}</span>
      ${t.wrong ? `<s>${esc(t.wrong)}</s> → ` : ""}${esc(t.right)}</button>
      <div><span class="stamp ${t.used_after ? "good" : "wait"}">${t.used_after ? `之后用对 ${t.used_after} 次` : "之后还没被问到"}</span>${t.recurred.length ? `<span class="stamp bad">${esc(t.recurred[0].slice(5))} 复发 ${t.recurred.length} 次</span>` : ""}${t.kind === "changed" ? '<span class="stamp wait">变了，不算错</span>' : ""}</div>
      ${open ? `<div class="metdetail">${t.quote ? `<div><span class="muted">你当时说</span>“${esc(t.quote)}”</div>` : ""}
        <div><span class="muted">错在哪</span>${esc(t.layer)}</div>
        <div class="row" style="gap:8px;flex-wrap:wrap"><button type="button" class="btn gold" data-act="me-reask" data-q="${esc(t.quote || t.right)}">再问一次，看现在对不对</button>
        <button type="button" class="btn" data-act="me-undo" data-id="${t.id}">撤销这次纠正</button></div></div>` : ""}</div>`; };
  const week = m.confirmed + m.wrong ? `这周你确认了 ${m.confirmed} 次、纠正了 ${m.wrong} 次${m.corrections > m.wrong ? `（另有 ${m.corrections - m.wrong} 次是 小伴 他自己的事）` : ""}${m.recurrences ? `，复发 ${m.recurrences} 次` : ""}${m.eval && m.eval.total ? `；检验题通过 ${pct(m.eval.rate)}` : ""}。` : "这周你还没有确认或纠正过。";
  return `<section class="sheet mesheet wide" aria-label="谁在用和纠错">${head}
    <p class="muted" style="font-size:12px;margin:2px 0 14px">${week}</p>
    <h3 style="font-size:15px;margin:0 0 8px">谁在用你的记忆 <span class="muted" style="font-weight:400;font-size:12px">点一张卡看详细</span></h3>
    <div class="meusers">${m.users.map(card).join("")}</div>${detail}
    <h3 style="font-size:15px;margin:18px 0 8px">${su ? esc(su.name) + " 被你纠正过的" : "你纠正过的"} <span class="muted" style="font-weight:400;font-size:12px">${su ? "再点一次卡片回到全部" : "改完之后有没有用对"}</span></h3>
    ${tickets.map(tk).join("") || `<p class="muted">${su ? "它还没被你纠正过。" : "还没有纠错单。聊天里跟 小伴 说“你记错了”，或者点上面的“问一问”抽查一下。"}</p>`}</section>`;
}

function meOverlay() {
  const close = '<button type="button" class="btn" data-act="close">关闭</button>';
  if (S.overlay === "me-users") return meUsersSheet();
  if (S.overlay === "me-pending") {
    const items = S.mePending;
    const one = (x) => { const k = "p" + x.id, both = (S.meBoth || {})[x.id];
      const opt = (o, i) => `<div class="meopt"><div>${esc(o.text)}${o.mine ? '<span class="mine">你说过的</span>' : ""}<div class="src">${esc(o.src)}</div>
          ${both ? `<div class="medates">从 <input type="date" data-mef="${k}since${i}"> 到 <input type="date" data-mef="${k}until${i}"></div>` : ""}</div>
        </div>`;
      const yes = x.kind === "expire" ? ["还算数", "不算了"] : ["对", "不对"];
      return `<div class="mepair"><div class="q"><span class="kind ${x.type === "once" ? "k-once" : "k-long"}">${x.type === "once" ? "那一次" : "长期"}</span>${esc(x.question)}${x.users.length ? " · " + esc(x.users.join("、")) + " 用过" : ""}</div>
        ${x.options.map(opt).join("")}
        ${x.type !== "once" && !both ? `<div class="medates">${x.kind === "conflict" ? "按哪条为准，这条" : "这条"}从 <input type="date" data-mef="${k}since"> 起，到 <input type="date" data-mef="${k}until">（不填就是一直）</div>` : ""}
        ${x.kind === "conflict" && !both ? `<div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap">${x.options.map((o, i) => `<button type="button" class="btn gold" data-act="me-pick" data-id="${x.id}" data-i="${i}">以“${esc(o.text.length > 12 ? o.text.slice(0, 12) + "…" : o.text)}”为准</button>`).join("")}</div>` : ""}
        ${x.kind !== "conflict" && x.options.length ? `<div class="row" style="gap:8px;margin-top:8px"><button type="button" class="btn gold" data-act="me-yes" data-id="${x.id}">${yes[0]}</button><button type="button" class="btn" data-act="me-no" data-id="${x.id}">${yes[1]}</button></div>` : ""}
        ${x.kind === "conflict" ? (both ? `<button type="button" class="btn gold" data-act="me-both" data-id="${x.id}" data-n="${x.options.length}">两条都留着，按上面的时间</button>`
          : `<button type="button" class="linkish meboth" data-act="me-bothopen" data-id="${x.id}">都对，只是时间不同</button>`) : ""}
        <div class="meown"><input data-mef="${k}text" placeholder="${x.type === "once" ? "都不对？写那天实际发生了什么" : x.options.length ? "都不对？自己写，比如：现在每周 3 次" : "写一句，比如：现在每周 3 次"}" value="${esc((S.meF || {})[k + "text"] || "")}">
          <button type="button" class="btn" data-act="me-own" data-id="${x.id}" data-type="${x.type}">用我写的</button></div></div>`; };
    const outside = (x) => { const k = "p" + x.id, o = x.options[0] || {};  // 对外使用者问到“问我”的类别（ontology-plan 3.3）
      return `<div class="mepair medisc"><div class="q"><span class="kind k-out">对外</span>${esc(x.question)}</div>
        <div class="meopt"><div>${o.text ? `拟答：“${esc(o.text)}”` : '<span class="muted">还没有现成的说法，写一句能说的</span>'}<div class="src">${esc(o.src || "")}</div></div></div>
        <div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap">${o.text ? `<button type="button" class="btn gold" data-act="me-yes" data-id="${x.id}">可以说${o.recipient ? "（这次）" : ""}</button>
          ${o.category ? `<button type="button" class="btn" data-act="me-always" data-id="${x.id}">以后“${esc(o.category_name)}”都可以</button>` : ""}` : ""}
          <button type="button" class="btn" data-act="me-no" data-id="${x.id}">不说</button></div>
        <div class="meown"><input data-mef="${k}text" placeholder="改一下再说，比如：她在伦敦" value="${esc((S.meF || {})[k + "text"] || "")}">
          <button type="button" class="btn" data-act="me-own" data-id="${x.id}" data-type="long">用我写的</button></div></div>`; };
    const decide = (items || []).filter((x) => !x.gap), asks = (items || []).filter((x) => x.gap);
    const ask = (x) => { const k = "p" + x.id;
      return `<div class="mepair"><div class="q">${esc(x.question)}</div>
        <div class="meown"><input data-mef="${k}text" placeholder="想到就写一句，不想答就跳过" value="${esc((S.meF || {})[k + "text"] || "")}">
          <button type="button" class="btn" data-act="me-own" data-id="${x.id}" data-type="long">记下</button><button type="button" class="btn" data-act="me-skip" data-id="${x.id}">不想答</button></div></div>`; };
    return `<section class="sheet mesheet" aria-label="待你定"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">待你定${items ? " " + decide.length + " 件" : ""}</h2>${close}</div>
      <p class="muted" style="font-size:12px">长期的按“现在一般是怎样”定；那一次的按“那天实际发生了什么”定。定了就锁定，所有使用者都按这个来；小伴 在聊天里问你、你回了，这里也会少一件。</p>
      ${items ? decide.map((x) => x.kind === "disclose" ? outside(x) : one(x)).join("") || '<p class="muted">没有要你定的。</p>' : '<p class="muted">加载中……</p>'}
      ${asks.length ? `<details class="measks"><summary class="muted" style="font-size:13px;cursor:pointer">小伴 想多了解你的 ${asks.length} 个问题（不用在这里答，他会在聊天里顺口问；想先写也行）</summary>${asks.map(ask).join("")}</details>` : ""}</section>`;
  }
  if (S.overlay === "me-ask") {
    const r = S.meAsk, marks = S.meMarks || {};
    const recent = (S.me || {}).recent_questions || [];
    const ans = (a) => `<div class="means ${marks[a.user] === "bad" ? "wrong" : marks[a.user] === "ok" ? "right" : ""}"><div class="who"><span>${esc(a.user === "twin" ? "Dais（访客问时会这么答）" : a.name)}</span><span>用了 ${a.keys.length} 条记忆</span></div>
        <p>${esc(a.answer)}</p>${a.sources.length ? `<div class="src">依据：${a.sources.map(esc).join("；")}</div>` : '<div class="src">没查到相关的记忆</div>'}
        <div class="row" style="gap:8px;margin-top:8px"><button type="button" class="btn ${marks[a.user] === "ok" ? "gold" : ""}" data-act="me-mark" data-k="${a.user}" data-v="ok">对</button>
        <button type="button" class="btn ${marks[a.user] === "bad" ? "warnbtn" : ""}" data-act="me-mark" data-k="${a.user}" data-v="bad">不对</button></div></div>`;
    let next = "";
    if (r && r.answers) {
      const vals = r.answers.map((a) => marks[a.user]);
      const ok = r.answers.filter((a) => marks[a.user] === "ok"), bad = r.answers.filter((a) => marks[a.user] === "bad");
      const F = S.meF || {};
      const kindSeg = `<div class="seg">${[["long", "长期：我现在是这样"], ["once", "那一次：只是那天发生的事"]].map(([v, l]) => `<button type="button" class="${(S.meType || "long") === v ? "on" : ""}" data-act="me-type" data-k="${v}">${l}</button>`).join("")}</div>`;
      const whySeg = `<div class="seg">${[["wrong", "它说的从来不对"], ["changed", "以前是这样，现在变了"]].map(([v, l]) => `<button type="button" class="${(S.meKind || "wrong") === v ? "on" : ""}" data-act="me-kind" data-k="${v}">${l}</button>`).join("")}</div>`;
      if (vals.every((v) => v) && !ok.length) {
        next = `<div class="mepair" style="border-color:var(--gold)"><div class="q">${bad.length > 1 ? "两个你都点了“不对”" : "你点了“不对”"}：正确的是？</div>
          <div class="meown" style="border:0;padding:0"><input data-mef="asktext" value="${esc(F.asktext || "")}" placeholder="写一句正确的"><button type="button" class="btn gold" data-act="me-verdict">记下</button></div>
          ${kindSeg}${(S.meType || "long") === "long" ? whySeg + `<div class="medates">从 <input type="date" data-mef="asksince"> 起，到 <input type="date" data-mef="askuntil">（不填就是一直）</div>` : ""}</div>
          <div class="meeffect">记下以后：${bad.map((a) => esc(a.user === "twin" ? "Dais" : a.name)).join("和")}都改成这句，各开一张纠错单${(S.meKind || "wrong") === "changed" ? "；旧的说法留作历史，不算错" : "；旧的说法作废"}。</div>`;
      } else if (ok.length === 1 && vals.every((v) => v)) {
        next = `<div class="meeffect">记下以后：正确说法是“${esc(ok[0].answer)}”${bad.length ? "，" + bad.map((a) => esc(a.user === "twin" ? "Dais" : a.name)).join("和") + "改成这句，各开一张纠错单" : ""}。</div>
          <div class="row" style="margin-top:8px"><button type="button" class="btn gold" data-act="me-verdict">记下</button></div>`;
      } else if (ok.length > 1) {
        const c = S.meCompare;
        next = !c ? '<p class="muted" style="margin-top:10px">在看两个回答是不是一回事……</p>'
          : !c.conflict ? `<div class="meeffect">两个说的是一回事，记成你确认过。</div><div class="row" style="margin-top:8px"><button type="button" class="btn gold" data-act="me-verdict">记下</button></div>`
          : `<div class="mepair" style="border-color:var(--gold)"><div class="q">这两个说法不一样，你都点了“对”（${esc(c.why)}）</div>
              ${S.meSplit ? r.answers.map((a, i) => `<div class="meown" style="border:0;padding:4px 0"><input data-mef="split${i}" value="${esc(F["split" + i] ?? a.answer)}"></div>
                <div class="medates">从 <input type="date" data-mef="splitsince${i}"> 到 <input type="date" data-mef="splituntil${i}"></div>`).join("") + `<div class="row" style="margin-top:8px"><button type="button" class="btn gold" data-act="me-split">两条都留着</button></div>`
              : `<div class="row" style="gap:8px;flex-wrap:wrap"><button type="button" class="btn" data-act="me-splitopen">都对，是不同时候或不同情况</button>
                <button type="button" class="btn" data-act="me-remark">其实只有一个对</button><button type="button" class="btn" data-act="me-half">各对一半，我来写</button></div>`}</div>`;
      }
    }
    return `<section class="sheet mesheet wide" aria-label="问一问"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">问一问</h2>${close}</div>
      <p class="muted" style="font-size:12px">只是问问看，不会发给任何人。每张回答只看它说得对不对。</p>
      <div class="meown" style="border:0;padding:0"><input data-mef="q" value="${esc((S.meF || {}).q || "")}" placeholder="比如：我每周练几次？我吃香菜吗？" aria-label="问一件关于你的事">
        <button type="button" class="btn gold" data-act="me-ask" ${S.meAsking ? "disabled" : ""}>${S.meAsking ? "在问……" : "问"}</button></div>
      ${recent.length ? `<div class="chips" style="margin-top:8px"><span class="muted" style="font-size:12px">最近问过：</span>${recent.map((q) => `<button type="button" class="chip" data-act="me-reask" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>` : ""}
      ${r && r.answers ? `<div class="meanswers">${r.answers.map(ans).join("")}</div>${next}` : ""}</section>`;
  }
  if (S.overlay === "me-manage") {  // 删除管理和对外表达（ontology-plan 2.2 第二层）
    const g = S.meManage, F = S.meF || {};
    const del = (x) => `<div class="memem"><div><b style="font-size:14px">${esc(x.at)}</b> 删了 ${x.facts || 0} 条${x.dims.length ? `（${esc(x.dims.join("、"))}）` : ""}${x.sources ? `，来源 ${x.sources} 个` : ""}${x.suspended ? `，另有 ${x.suspended} 条先停用` : ""}
        <div class="src">内容已经清掉，放不回来${x.reimport ? "；已允许以后重新记" : x.sources ? "；你再发同样的东西，小伴 不会再记" : ""}</div></div>
        ${x.sources && !x.reimport ? `<button type="button" class="btn" data-act="me-reimport" data-id="${x.id}">允许以后重新记</button>` : ""}</div>`;
    const sus = (x) => `<div class="memem"><div>${esc(x.text)}<div class="src">${esc(x.dim)} · 它的一部分依据被你删了，还剩别的依据撑着</div></div>
        <span class="mevote"><button type="button" class="btn" data-act="me-keep" data-id="${x.id}">留着</button>${S.meForget === "s" + x.id ? `<button type="button" class="btn primary" data-act="me-dropsusp" data-id="${x.id}">确定删掉？</button>` : `<button type="button" class="btn" data-act="me-forgetsus" data-id="${x.id}">也删掉</button>`}</span></div>`;
    const dis = (x) => `<div class="memem"><div>${x.deny ? '<span class="pchg">挡住</span>' : ""}“${esc(x.text)}”<div class="src">给 ${esc(x.to)} · ${x.deny ? "这句不说" : x.once ? `这次对 ${esc(x.to_whom)} 说` : `长期${x.renew_at ? "，" + esc(x.renew_at) + " 问你要不要续" : ""}`}${x.until ? " · 到 " + esc(x.until) : ""} · ${esc(x.state)}</div></div>
        ${x.state === "有效" ? `<button type="button" class="btn" data-act="me-revoke" data-id="${x.id}">撤回</button>` : ""}</div>`;
    const MODES = [["allow", "可以说"], ["ask", "问我"], ["deny", "不说"]];
    const prow = (t) => (r) => `<div class="pol ${r.locked ? "locked" : ""}"><div class="pn">${esc(r.name)}${r.changed ? '<i class="pchg" title="你改过">改过</i>' : ""}<span>${esc(r.hint)}</span></div>
        ${r.locked ? '<span class="plock">永远不说</span>' : `<span class="pseg" role="group" aria-label="${esc(t.name)}·${esc(r.name)}">${MODES.map(([v, l]) => `<button type="button" class="m-${v} ${r.mode === v ? "on" : ""}" aria-pressed="${r.mode === v}" data-act="me-policy" data-tier="${t.tier}" data-k="${r.key}" data-v="${v}">${l}</button>`).join("")}</span>`}</div>`;
    const ptable = (t) => `<div class="poltable"><div class="ph"><b>${esc(t.name)}</b><span>${t.tier === "public" ? "Dais、主页访客、不认识的 agent" : "以后接进来、你信任的 agent"}</span></div>${t.rows.map(prow(t)).join("")}</div>`;
    const body = !g ? '<p class="muted">加载中……</p>' : `
      <h3 style="font-size:15px;margin:6px 0 4px">对外开关</h3><p class="muted" style="font-size:12px;margin:0 0 4px">${esc(g.policy_note || "")}“问我”的被问到时先回“我问问她”，进待你定；别人说的、系统猜的，开着也不说。</p>
      <div class="polgrid">${(g.policy || []).map(ptable).join("")}</div>
      <h3 style="font-size:15px;margin:16px 0 4px">删过的</h3><p class="muted" style="font-size:12px;margin:0 0 6px">在“全部记忆”里点“删掉”的都在这里。为了真删干净，只留日期、方面和条数。</p>
      <div class="memlist">${g.deletions.map(del).join("") || '<p class="muted">还没删过</p>'}</div>
      ${g.suspended.length ? `<h3 style="font-size:15px;margin:14px 0 4px">先停用的</h3><div class="memlist">${g.suspended.map(sus).join("")}</div>` : ""}
      <h3 style="font-size:15px;margin:14px 0 4px">逐条放行和挡住的</h3><p class="muted" style="font-size:12px;margin:0 0 6px">开关是按类的；某类“不说”但这一句可以，在这里放行，对外的只能原样说这一句，不会顺着往下推。</p>
      <div class="memlist">${g.disclosures.map(dis).join("") || '<p class="muted">还没放行过</p>'}</div>
      <div class="meown" style="border:0;padding:8px 0 0;flex-wrap:wrap;gap:6px"><input data-mef="distext" value="${esc(F.distext || "")}" placeholder="允许对外说的一句，比如：我在 某商学院 做交换生">
        <div class="seg">${[["", "长期"], ["1", "只这一次"]].map(([v, l]) => `<button type="button" class="${(S.meDisOnce || "") === v ? "on" : ""}" data-act="me-disonce" data-k="${v}">${l}</button>`).join("")}</div>
        ${S.meDisOnce ? `<input data-mef="diswho" value="${esc(F.diswho || "")}" placeholder="对谁说">` : ""}
        <button type="button" class="btn gold" style="flex:none" data-act="me-disclose">放行这一句</button></div>`;
    return `<section class="sheet mesheet wide" aria-label="删除和对外"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">删除和对外</h2>${close}</div>${body}</section>`;
  }
  if (S.overlay === "me-all") {
    const d = S.meAll, F = S.meF || {};
    const item = (x) => { const fixing = S.meFix === x.id;
      return `<div class="memem"><div>${esc(x.text)} ${x.locked ? '<span class="gold" title="你定过的，系统不会自己改">🔒</span>' : ""}
          <div class="src">${esc(x.said || (x.mine ? "你说的" : "系统推出来的"))}${x.kind === "self_report" ? "（自述）" : x.kind === "hypothesis" ? "（猜测）" : ""}${x.context ? " · " + esc(x.context) : ""} · ${esc(x.from)}${x.to ? " 到 " + esc(x.to) : x.until ? " 到 " + esc(x.until) : ""} · 用过 ${x.uses} 次</div>
          ${fixing ? `<div class="meown" style="border:0;padding:6px 0 0"><input data-mef="fixtext" placeholder="正确的是？" value="${esc(F.fixtext || "")}"><button type="button" class="btn gold" data-act="me-fixsave" data-id="${x.id}">改</button></div>
            <div class="seg">${[["wrong", "当时就记错了"], ["changed", "以前是这样，现在变了"]].map(([v, l]) => `<button type="button" class="${(S.meKind || "wrong") === v ? "on" : ""}" data-act="me-kind" data-k="${v}">${l}</button>`).join("")}</div>` : ""}</div>
        ${S.meHistory ? "" : x.kind === "hypothesis" || ["转述", "系统推的", "系统推出来的"].includes(x.said)
          ? `<span class="mevote">${[["like", "像我"], ["unlike", "不像我"], ["unsure", "说不清"]].map(([v, l]) => `<button type="button" class="btn" data-act="me-vf" data-v="${v}" data-id="${x.id}">${l}</button>`).join("")}</span>`
          : `<span class="mevote">${x.locked ? "" : `<button type="button" class="btn" data-act="me-vf" data-v="yes" data-id="${x.id}">对</button>`}<button type="button" class="btn" data-act="me-fix" data-id="${x.id}">不对</button><button type="button" class="btn" data-act="me-fix" data-kind="changed" data-id="${x.id}">变了</button>${S.meForget === x.id ? `<button type="button" class="btn primary" data-act="me-vf" data-v="forget" data-id="${x.id}" title="删掉以后 小伴 不再记得，同一来源也不会再记回来">确定删掉？</button>` : `<button type="button" class="btn" data-act="me-forget" data-id="${x.id}">删掉</button>`}</span>`}</div>`; };
    const tab = S.meAllTab || "facts", more = d && d.more ? `<button type="button" class="btn" style="align-self:center" data-act="me-more">再看 100 条</button>` : "";
    const brainN = ((S.state || {}).badges || {}).brain || 0;
    const tabs = `<div class="chips">${[["facts", "关于你"], ["days", "发生过的事"], ["people", "人"]].map(([k, l]) => `<button type="button" class="chip ${tab === k ? "on" : ""}" data-act="me-alltab" data-k="${k}">${l}</button>`).join("")}
      <button type="button" class="chip" data-act="me-brain">收藏和资料${brainN ? ` <span class="rbadge">${brainN}</span>` : ""}</button></div>`;
    const dayRow = (x) => `<div class="memem" style="display:block"><b style="font-size:14px">${esc(x.day)}</b>${x.journal ? `<div style="font-size:13px;line-height:1.7;margin-top:2px">${esc(x.journal)}</div>` : ""}
        ${x.events.map((e) => `<div class="src">· ${esc(e.title)}${e.summary && e.summary !== e.title ? "：" + esc(e.summary) : ""}</div>`).join("")}</div>`;
    const personRow = (x) => `<div class="memem" style="display:block"><b style="font-size:14px">${esc(x.name)}</b>${x.circle ? ` <span class="muted" style="font-size:12px">${esc(x.circle)}</span>` : ""}
        ${x.how_met || x.notes ? `<div style="font-size:13px;line-height:1.7">${esc([x.how_met, x.notes].filter(Boolean).join("；"))}</div>` : ""}
        <div class="src">${x.last_contact ? "最近一次联系 " + esc(x.last_contact) : "还没记过联系"}</div></div>`;
    const body = !d ? '<p class="muted">加载中……</p>'
      : tab === "days" ? `<div class="memlist">${d.items.map(dayRow).join("") || '<p class="muted">没有</p>'}${more}</div>`
      : tab === "people" ? `<div class="memlist">${d.items.map(personRow).join("") || '<p class="muted">还没记下谁。聊天里提到的人，小伴 会记进来。</p>'}${more}</div>`
      : `<div class="chips" style="margin-top:8px"><button type="button" class="chip ${!S.meDim ? "on" : ""}" data-act="me-dim" data-k="">全部 ${d.total}</button>${d.dims.map((x) => `<button type="button" class="chip ${S.meDim === x.key ? "on" : ""}" data-act="me-dim" data-k="${x.key}">${esc(x.name)} ${x.n}</button>`).join("")}
        ${S.meProp ? `<button type="button" class="chip on" data-act="me-noprop" title="去掉这个筛选">只看这一项：${esc(S.mePropName || "")} ✕</button>` : ""}
        <button type="button" class="chip ${S.meHistory ? "on" : ""}" data-act="me-history">${S.meHistory ? "回到现在的" : "看过时的"}</button></div>
        <div class="memlist">${d.items.map(item).join("") || '<p class="muted">没有</p>'}${more}</div>`;
    const hint = { facts: "系统现在记得的你（从聊天、文件里整理出来的一条条）。🔒 是你定过的，系统不会自己改。你发过的文件、链接、收藏原件在“收藏和资料”里。", days: "每天的记忆日记和那天记下的事，新的在前。", people: "你生活里的人，按最近联系排。" }[tab];
    return `<section class="sheet mesheet wide" aria-label="全部记忆"><div class="row" style="justify-content:space-between"><h2 style="font-size:18px">全部记忆</h2>${close}</div>
      ${tabs}<p class="muted" style="font-size:12px">${hint}</p>
      <div class="meown" style="border:0;padding:0"><input data-mef="allq" value="${esc(F.allq || "")}" placeholder="搜一搜，比如：香菜、训练、Amy"><button type="button" class="btn" data-act="me-allsearch">搜</button></div>
      ${body}</section>`;
  }
  return "";
}

async function meLoad(what) {
  try {
    if (what === "pending") S.mePending = (await api("/api/me/pending")).items;
    if (what === "manage") S.meManage = await api("/api/me/manage");
    if (what === "all") S.meAll = await api("/api/me/all?" + new URLSearchParams({ q: (S.meF || {}).allq || "", dim: S.meDim || "", prop: S.meProp || "", history: S.meHistory ? "1" : "",
      tab: S.meAllTab || "facts", limit: S.meLimit || 100 }));
    if (what === "page") S.me = await api("/api/me");
  } catch (e) { showUndo("没连上服务器"); }
  renderOverlay(); if (S.page === "me") render();
}
async function meAct(action, payload, after) {
  const res = await send(action, payload, action === "me.ask" ? 120000 : 30000);
  if (!res.ok) { if (!res.login) showUndo(res.msg); return null; }
  if (res.msg && action !== "me.ask" && action !== "me.compare") showUndo(res.msg, res.undo);
  if (after) await after(res);
  return res;
}
const meF = (k) => ((S.meF || {})[k] || "").trim();

document.addEventListener("input", (ev) => {
  const k = ev.target.dataset && ev.target.dataset.mef;
  if (k) S.meF = { ...(S.meF || {}), [k]: ev.target.value };
});
document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act^='me-']");
  if (!el) return;
  const a = el.dataset.act, id = el.dataset.id ? Number(el.dataset.id) : null;
  const refresh = async () => { await meLoad("page"); await load(true); };
  if (a === "me-user") { S.meUser = S.meUser === el.dataset.k ? null : el.dataset.k; S.meTicket = null; renderOverlay(); }
  else if (a === "me-ticket") { S.meTicket = S.meTicket === id ? null : id; renderOverlay(); }
  else if (a === "me-undo") meAct("me.undo", { id }, refresh);
  else if (a === "me-open") { S.overlay = el.dataset.k; S.meAsk = null; S.meMarks = {}; S.meCompare = null; S.meSplit = false; S.meFix = null;
    S.meF = {}; S.meDim = null; S.meProp = null; S.meHistory = false; S.meLimit = 100; S.meAllTab = "facts"; S.meAll = null; S.meUser = null; S.meTicket = null;  // 每次打开都从头开始，不留上次的搜索（她 11-04，ui-spec 默认状态）
    renderOverlay(); if (el.dataset.k === "me-pending") meLoad("pending"); if (el.dataset.k === "me-all") meLoad("all"); if (el.dataset.k === "me-manage") { S.meManage = null; meLoad("manage"); } }
  else if (a === "me-reask") { S.overlay = "me-ask"; S.meF = { ...(S.meF || {}), q: el.dataset.q }; S.meAsk = null; S.meMarks = {}; S.meCompare = null; renderOverlay(); }
  else if (a === "me-policy") {
    const t = el.dataset.tier, k = el.dataset.k, v = el.dataset.v, g = S.meManage;
    const row = g && (g.policy || []).find((x) => x.tier === t);
    const r = row && row.rows.find((x) => x.key === k);
    if (!r || r.mode === v) return;
    const old = r.mode; r.mode = v; renderOverlay();  // 先改界面，服务器没成再退回
    meAct("me.policy", { tier: t, category: k, mode: v }).then((res) => { if (!res) r.mode = old; else r.changed = r.mode !== r.default || r.changed; renderOverlay(); });
  }
  else if (a === "me-always") meAct("me.resolve", { id, how: "always" }, async () => { await meLoad("pending"); await refresh(); });
  else if (["me-pick", "me-yes", "me-no", "me-own", "me-both"].includes(a)) {
    const k = "p" + id, body = { id };
    if (a === "me-pick") Object.assign(body, { how: "pick", pick: Number(el.dataset.i), since: meF(k + "since") || null, until: meF(k + "until") || null });
    if (a === "me-yes") Object.assign(body, { how: "yes", since: meF(k + "since") || null, until: meF(k + "until") || null });  // 改了下面的时间，点“对”一起存（她 10-31：改完时间没地方提交）
    if (a === "me-no") body.how = "no";
    if (a === "me-own") { if (!meF(k + "text")) return showUndo("先写一句正确的"); Object.assign(body, { how: "own", text: meF(k + "text"), type: el.dataset.type, since: meF(k + "since"), until: meF(k + "until") }); }
    if (a === "me-both") Object.assign(body, { how: "both", ranges: [...Array(Number(el.dataset.n)).keys()].map((i) => ({ since: meF(k + "since" + i) || null, until: meF(k + "until" + i) || null })) });
    meAct("me.resolve", body, async () => { S.meBoth = {}; await meLoad("pending"); await refresh(); });
  }
  else if (a === "me-bothopen") { S.meBoth = { ...(S.meBoth || {}), [id]: true }; renderOverlay(); }
  else if (a === "me-ask") { const q = meF("q"); if (!q) return showUndo("问一句");
    S.meAsking = true; S.meAsk = null; S.meMarks = {}; S.meCompare = null; S.meSplit = false; renderOverlay();
    meAct("me.ask", { question: q }, (res) => { S.meAsk = res; }).finally(() => { S.meAsking = false; renderOverlay(); }); }
  else if (a === "me-mark") { S.meMarks = { ...(S.meMarks || {}), [el.dataset.k]: el.dataset.v }; S.meCompare = null; S.meSplit = false;
    const r = S.meAsk, ok = r.answers.filter((x) => S.meMarks[x.user] === "ok");
    renderOverlay();
    if (ok.length > 1) meAct("me.compare", { a: ok[0].answer, b: ok[1].answer }, (res) => { S.meCompare = res; renderOverlay(); }); }
  else if (a === "me-type") { S.meType = el.dataset.k; renderOverlay(); }
  else if (a === "me-kind") { S.meKind = el.dataset.k; renderOverlay(); }
  else if (a === "me-splitopen") { S.meSplit = true; renderOverlay(); }
  else if (a === "me-remark") { S.meMarks = {}; S.meCompare = null; showUndo("再点一次哪个是对的"); renderOverlay(); }
  else if (a === "me-half") { S.meMarks = Object.fromEntries(S.meAsk.answers.map((x) => [x.user, "bad"])); S.meCompare = null; renderOverlay(); }
  else if (a === "me-verdict") { const r = S.meAsk, marks = S.meMarks;
    const allBad = r.answers.every((x) => marks[x.user] === "bad");
    if (allBad && !meF("asktext")) return showUndo("先写一句正确的");
    meAct("me.verdict", { id: r.id, marks, text: meF("asktext"), type: S.meType || "long", kind: S.meKind || "wrong", since: meF("asksince"), until: meF("askuntil") },
      async () => { S.overlay = null; S.meF = { ...(S.meF || {}), asktext: "" }; renderOverlay(); await refresh(); }); }
  else if (a === "me-split") { const r = S.meAsk;
    meAct("me.verdict", { id: r.id, marks: S.meMarks, split: r.answers.map((x, i) => ({ user: x.user, text: meF("split" + i) || x.answer, since: meF("splitsince" + i) || null, until: meF("splituntil" + i) || null })) },
      async () => { S.overlay = null; renderOverlay(); await refresh(); }); }
  else if (a === "me-dim") { S.meDim = el.dataset.k || null; S.meProp = null; meLoad("all"); }
  else if (a === "me-star") { S.meStar = !!el.dataset.k; render(); }
  else if (a === "me-dimopen") { S.overlay = "me-all"; S.meAllTab = "facts"; S.meDim = el.dataset.k; S.meProp = null; S.meHistory = false; S.meLimit = 100; S.meF = {}; S.meAll = null; renderOverlay(); meLoad("all"); }
  else if (a === "me-history") { S.meHistory = !S.meHistory; meLoad("all"); }
  else if (a === "me-allsearch") { S.meLimit = 100; meLoad("all"); }
  else if (a === "me-alltab") { S.meAllTab = el.dataset.k; S.meAll = null; S.meLimit = 100; renderOverlay(); meLoad("all"); }
  else if (a === "me-jump") { S.overlay = "me-all"; S.meAllTab = "facts"; S.meDim = null; S.meHistory = false; S.meLimit = 100; S.meF = { ...(S.meF || {}), allq: el.dataset.q }; S.meAll = null; renderOverlay(); meLoad("all"); }
  else if (a === "me-more") { S.meLimit = (S.meLimit || 100) + 100; meLoad("all"); }
  else if (a === "me-brain") { S.overlay = null; S.page = "brain"; S.bq = null; S.brain = null; render(); load(); }
  else if (a === "me-fix") { S.meFix = S.meFix === id && (S.meKind || "wrong") === (el.dataset.kind || "wrong") ? null : id; S.meKind = el.dataset.kind || "wrong"; renderOverlay(); }
  else if (a === "me-vf") {
    S.meForget = null;
    meAct("me.verdict_fact", { id, v: el.dataset.v }, async () => { await meLoad("all"); await refresh(); }); }
  else if (a === "me-forget") { S.meForget = id; renderOverlay(); }
  else if (a === "me-skip") meAct("me.skip", { id }, async () => { await meLoad("pending"); await refresh(); });
  else if (a === "me-forgetsus") { S.meForget = "s" + id; renderOverlay(); }
  else if (["me-reimport", "me-keep", "me-dropsusp", "me-revoke"].includes(a)) { S.meForget = null; meAct(a.replace("-", "."), { id }, async () => { await meLoad("manage"); await refresh(); }); }
  else if (a === "me-disonce") { S.meDisOnce = el.dataset.k || ""; renderOverlay(); }
  else if (a === "me-disclose") { if (!meF("distext")) return showUndo("先写一句允许对外说的");
    meAct("me.disclose", { text: meF("distext"), once: S.meDisOnce ? 1 : 0, to_whom: meF("diswho") }, async () => { S.meF = { ...(S.meF || {}), distext: "", diswho: "" }; await meLoad("manage"); }); }
  else if (a === "me-stardim") { S.meStarDim = el.dataset.k || null; render(); }
  else if (a === "me-noprop") { S.meProp = null; meLoad("all"); }
  else if (a === "me-propopen") { S.mePropName = el.dataset.name || ""; S.overlay = "me-all"; S.meAllTab = "facts"; S.meDim = el.dataset.dim; S.meProp = el.dataset.k; S.meHistory = false; S.meLimit = 100; S.meF = {}; S.meAll = null; renderOverlay(); meLoad("all"); }
  else if (a === "me-fixsave") { if (!meF("fixtext")) return showUndo("先写一句正确的");
    meAct("me.fix", { id, text: meF("fixtext"), kind: S.meKind || "wrong" }, async () => { S.meFix = null; S.meF = { ...(S.meF || {}), fixtext: "" }; await meLoad("all"); await refresh(); }); }
});

// 日记编辑中：随打随存进草稿，页面定时刷新时不丢（2026-11-01）
document.addEventListener("input", (ev) => { if (ev.target.matches && ev.target.matches("[data-diarydraft]")) S.diaryDraft = ev.target.value; });

// 主线步骤改名：编辑时改完离开输入框就存（她 2026-11-01）
document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (!t.matches || !t.matches("[data-steptitle]")) return;
  const title = t.value.trim();
  if (title) act("task.rename", { id: t.dataset.steptitle, title }, t).then(async (r) => {
    if (!r.ok) return;  // 没改成：框里她写的字留着（act 已经提示了原因），不拿旧名字重画
    t.defaultValue = t.value; S.quests = await api("/api/quests").catch(() => S.quests); render(); });
});

// 里程碑和阶段任务的编辑：改完离开输入框 / 选完就存（改动自动保存，全局交互规范）
document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (!t.dataset) return;
  const reload = async () => { S.quests = await api("/api/quests"); render(); };
  if (t.dataset.ms && t.dataset.msk) act("milestone.set", { id: t.dataset.ms, [t.dataset.msk]: t.value }, null, true).then(reload);
  else if (t.dataset.stagecad) act("stage.set", { id: t.dataset.stagecad, cadence: t.value }, null, true).then(reload);
  else if (t.dataset.stageweek) { const sel = document.querySelector(`[data-stagecad="${t.dataset.stageweek}"]`); act("stage.set", { id: t.dataset.stageweek, cadence: sel ? sel.value : "weekly", per_week: t.value }, null, true).then(reload); }
  else if (t.dataset.stagehabit !== undefined) act("stage.set", { id: t.dataset.stagehabit, habit_id: t.value || null }, null, true).then(reload);
  else if (t.dataset.addcad !== undefined) { const w = t.form.querySelector('[name="per_week"]'); if (w) w.hidden = t.value !== "weekly"; }
});

// 副本里的任务按住左边的 ⋮⋮ 拖动排序（她 2026-11-01：不要上下箭头）；鼠标和手指都行
(() => {
  let drag = null;
  document.addEventListener("pointerdown", (ev) => {
    const grip = ev.target.closest && ev.target.closest("[data-grip]");
    if (!grip) return;
    const row = grip.closest(".step.edit");
    if (!row) return;
    ev.preventDefault();
    grip.setPointerCapture && grip.setPointerCapture(ev.pointerId);
    drag = { row, list: row.parentElement, before: [...row.parentElement.children].map((x) => x.dataset.stepid).join(",") };
    row.classList.add("dragging");
  });
  document.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    ev.preventDefault();
    const rows = [...drag.list.querySelectorAll(".step.edit")].filter((x) => x !== drag.row);
    // 阶段任务（新结构）：拖到另一件的正中间 = 挂在它后面、等它做完（她 2026-11-01：不用下拉选，拖过去挂上，可以挂 N 级）
    const target = drag.list.dataset.ms ? rows.find((x) => { const r = x.getBoundingClientRect(); return ev.clientY > r.top + r.height * 0.3 && ev.clientY < r.bottom - r.height * 0.3; }) : null;
    rows.forEach((x) => x.classList.toggle("droptarget", x === target));
    drag.hang = target ? target.dataset.stepid : null;
    if (target) return;
    const after = rows.find((x) => { const r = x.getBoundingClientRect(); return ev.clientY < r.top + r.height / 2; });
    if (after) drag.list.insertBefore(drag.row, after); else drag.list.appendChild(drag.row);
  }, { passive: false });
  const done = () => {
    if (!drag) return;
    const { row, list, before, hang } = drag;
    drag = null;
    row.classList.remove("dragging");
    list.querySelectorAll(".droptarget").forEach((x) => x.classList.remove("droptarget"));
    if (hang) { act("stage.set", { id: row.dataset.stepid, depends_on: hang }, null, true).then(async () => { S.quests = await api("/api/quests"); render(); }); return; }
    const ids = [...list.querySelectorAll(".step.edit")].map((x) => x.dataset.stepid);
    if (ids.join(",") !== before) act("step.order", { goal_id: list.dataset.goal, ids }, null, true).then(async () => { S.quests = await api("/api/quests"); render(); });
  };
  document.addEventListener("pointerup", done);
  document.addEventListener("pointercancel", done);
})();

// 设置页上传探索地图（她 2026-11-01 更新了一版）
document.addEventListener("change", async (ev) => {
  const inp = ev.target;
  if (!inp.matches || !inp.matches("[data-explore]") || !inp.files[0]) return;
  showUndo("在上传……");
  try {
    const h = { "Content-Type": "application/octet-stream" }; if (S.token) h.Authorization = "Bearer " + S.token;
    const r = await (await fetch("/api/explore", { method: "POST", headers: h, credentials: "same-origin", body: inp.files[0] })).json();
    showUndo(r.ok ? "探索地图换好了：" + Object.entries(r.sheets).map(([k, v]) => k + " " + v).join("、") : (r.error || "没传上"));
  } catch (e) { showUndo("没传上，再试一次"); }
  inp.value = "";
});

// 探索地图里改一行的状态、搜索
document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (!t.matches) return;
  if (t.matches("[data-spend]") && S.terr && S.terr.spend) {
    const sp = S.terr.spend, start = t.dataset.spend === "start" ? t.value : sp.start, end = t.dataset.spend === "end" ? t.value : sp.end;
    api("/api/spend?" + new URLSearchParams({ start, end })).then((r) => { if (r && r.points) { S.terr.spend = r; render(); } });
    return;
  }
  if (t.matches("[data-explore-tag]")) { S.exploreTag = t.value; return renderOverlay(); }
  if (t.matches("[data-explore-sort]")) { S.exploreSort = t.value; return renderOverlay(); }
  if (!t.matches("[data-explore-row]")) return;
  const i = Number(t.dataset.exploreRow), sheet = t.dataset.sheet;
  send("explore.set", { sheet, i, status: t.value }).then((r) => {
    if (!r.ok) { showUndo("没改成：" + r.msg); return renderOverlay(); }  // 下拉框退回原来的状态
    S.explore.sheets[sheet][i]["状态"] = t.value;
    showUndo(r.msg); renderOverlay();
  });
});
document.addEventListener("input", (ev) => {
  if (ev.target.matches && ev.target.matches("[data-exploreq]")) { S.exploreQ = ev.target.value; const pos = ev.target.selectionStart; renderOverlay();
    const n = document.querySelector("[data-exploreq]"); if (n) { n.focus(); n.setSelectionRange(pos, pos); } }
});


/* ---------- 关系（ethan/relations.py，她 2026-11-04 定名“关系”）----------
   顶上三张清单：该联系了、生日和重要日子、待回复；下面按重要程度分组的人物卡。
   每张卡左边一条“联系线”：上次联系到今天走了提醒间隔的多少，走满了就该联系了。 */
const REL_GROUPS = [[3, "很重要"], [2, "常联系"], [1, "认识的人"], [0, "还没分"]];
const REL_STARS = (n) => "★".repeat(n || 0) + "☆".repeat(3 - (n || 0));
function relPerson(id) { return ((S.rel || {}).people || []).find((x) => x.id === id); }
function relThread(x) {
  if (x.days == null || !x.every) return `<span class="relthread none" aria-hidden="true"><i style="height:0"></i></span>`;
  const pct = Math.min(100, Math.round(100 * x.days / x.every));
  return `<span class="relthread ${x.overdue ? "over" : pct >= 75 ? "near" : ""}" title="${x.days} 天没联系 / 每 ${x.every} 天提醒一次" aria-hidden="true"><i style="height:${pct}%"></i></span>`;
}
function relAgo(x) {
  if (x.days == null) return "还没有联系记录";
  return x.days === 0 ? "今天聊过" : `${x.days} 天没联系` + (x.every ? `<span class="muted"> / 每 ${x.every} 天</span>` : "");
}
function relationsPage() {
  const r = S.rel;
  if (!r) return '<p class="loading">加载中……</p>';
  const d = r.due, q = (S.relQ || "").trim(), circle = S.relCircle || "";
  const lane = (title, hint, rows, empty) => `<section class="panel rellane" aria-label="${title}">
      <div class="row" style="justify-content:space-between;align-items:baseline"><h2>${title}</h2><span class="muted" style="font-size:12px">${rows.length ? rows.length + " 个" : ""}</span></div>
      <p class="muted relhint">${hint}</p>
      ${rows.length ? rows.slice(0, 6).join("") : `<p class="muted relempty">${empty}</p>`}
      ${rows.length > 6 ? `<p class="muted" style="font-size:12px;margin:4px 0 0">还有 ${rows.length - 6} 个，在下面的卡里</p>` : ""}</section>`;
  const due = (id, name, sub, tag) => `<button type="button" class="reldue" data-act="person-open" data-id="${id}"><b>${esc(name)}</b><span class="muted">${sub}</span>${tag ? `<em>${tag}</em>` : ""}</button>`;
  const lanes = `<div class="grid3 rellanes">
    ${lane("该联系了", "超过提醒间隔的，附一句能聊的", d.contact.map((x) => due(x.id, x.name, esc(x.topic || "好久没聊了"), x.days + " 天")), "都联系过了")}
    ${lane("生日和重要日子", "未来 7 天", d.days.map((x) => due(x.id, x.name, esc(x.what), x.in === 0 ? "今天" : x.in === 1 ? "明天" : x.date.slice(5).replace("-", "月") + "日")), "这周没有")}
    ${lane("待回复", "对方问了你还没回，7 天内的", d.reply.map((x) => due(x.id, x.name, esc(x.text), x.ago === 0 ? "今天" : x.ago + " 天前")), "没有欠着的")}</div>`;
  let people = r.people;
  if (circle) people = people.filter((x) => x.circle === circle);
  if (q) people = people.filter((x) => [x.name, x.relation, x.circle, x.recent, x.notes, (x.topics || []).join(" ")].some((v) => (v || "").includes(q)));
  const tile = (x) => `<button type="button" class="reltile ${x.overdue ? "over" : ""}" data-act="person-open" data-id="${x.id}" aria-label="${esc(x.name)} 的人物卡">
      ${relThread(x)}<span class="relbody"><span class="relname">${esc(x.name)}</span>
      <span class="relrel">${esc(x.relation || x.circle || (x.organized ? "" : "还没整理"))}</span>
      <span class="relmeta">${relAgo(x)}</span></span></button>`;
  const groups = REL_GROUPS.map(([lv, label]) => {
    const g = people.filter((x) => (x.importance || 0) === lv);
    if (!g.length) return "";
    const all = (S.relMore || {})[lv], shown = all || lv >= 2 ? g : g.slice(0, 24);
    return `<section class="relgroup" aria-label="${label}"><div class="row relghead"><span class="relstars" aria-hidden="true">${lv ? REL_STARS(lv) : ""}</span><h2>${label}</h2><span class="muted" style="font-size:12px">${g.length} 人</span></div>
      <div class="reltiles">${shown.map(tile).join("")}</div>
      ${shown.length < g.length ? `<button type="button" class="btn" data-act="rel-more" data-lv="${lv}">再看 ${g.length - shown.length} 个</button>` : ""}</section>`;
  }).join("");
  const circles = [...new Set(r.people.map((x) => x.circle).filter(Boolean))];
  return `<header class="row relhead"><div><h1 class="reltitle">关系</h1>
      <p class="muted" style="margin:2px 0 0;font-size:12px">${r.people.length} 张人物卡 · 从微信聊天整理，你改过的地方系统不再动</p></div>
      <input class="relq" type="search" data-relq placeholder="找人、找话题" value="${esc(S.relQ || "")}" aria-label="找人"></header>
    ${lanes}
    ${circles.length ? `<div class="chips" role="group" aria-label="按关系圈看"><button type="button" class="chip ${circle ? "" : "on"}" data-act="rel-circle" data-c="">全部</button>${circles.map((c) => `<button type="button" class="chip ${circle === c ? "on" : ""}" data-act="rel-circle" data-c="${esc(c)}">${esc(c)}</button>`).join("")}</div>` : ""}
    ${groups || `<section class="panel"><p class="muted" style="margin:0">${r.people.length ? "没找到。" : "还没有人物卡。等你 Mac 上的微信导出下次交过来（每周日 20:43，或者现在手动跑一次 --send），这里就会按聊天多少排好。"}</p></section>`}`;
}
function personSheet() {
  const x = relPerson(S.personId);
  if (!x) return `<section class="sheet small"><p class="muted">这张卡不在了。</p></section>`;
  const st = x.stats || {}, lk = (f) => (x.locked || []).includes(f) ? '<span class="rellock" title="你改过，自动整理不再动这一项">你改过</span>' : "";
  const field = (f, label, val, opts = {}) => `<label class="relfield"><span>${label}${lk(f)}</span>${opts.area
      ? `<textarea data-pf="${f}" data-id="${x.id}" rows="2" placeholder="${esc(opts.ph || "")}">${esc(val)}</textarea>`
      : `<input type="text" data-pf="${f}" data-id="${x.id}" value="${esc(val)}" placeholder="${esc(opts.ph || "")}" ${opts.max ? `maxlength="${opts.max}"` : ""}>`}</label>`;
  const pct = (v) => v == null ? "—" : Math.round(100 * v) + "%";
  const loops = (x.open_loops || []).map((o) => `<div class="relloop"><span class="relloopk">${o.kind === "they_asked" ? "对方问了" : "你答应了"}</span>
      <span style="flex:1;min-width:0">${esc(o.text)}<span class="muted" style="display:block;font-size:11px">${esc(o.date)} · “${esc(o.quote || "")}”</span></span>
      <button type="button" class="btn tiny" data-act="person-loopdone" data-id="${x.id}" data-text="${esc(o.text)}">了结</button></div>`).join("");
  const dates = (x.dates || []).map((o) => `<span class="chip">${esc(o.date)} ${esc(o.what)}</span>`).join("");
  const del = S.personDel === x.id
    ? `<span class="row" style="gap:6px"><span class="muted" style="font-size:12px">删了以后不再从微信建回来</span><button type="button" class="btn" data-act="person-delno">算了</button><button type="button" class="btn warnbtn" data-act="person-delyes" data-id="${x.id}">确定删除</button></span>`
    : `<button type="button" class="btn" data-act="person-del" data-id="${x.id}">删卡</button>`;
  return `<section class="sheet relsheet" aria-label="${esc(x.name)} 的人物卡">
    <div class="row relsheethead">${relThread(x)}<div style="flex:1;min-width:0"><h2 class="relbig">${esc(x.name)}</h2>
      <p class="muted" style="margin:4px 0 0;font-size:13px">${esc(x.relation || "关系还没整理出来")}${x.frequency ? " · " + esc(x.frequency) : ""}</p></div>
      <button type="button" class="btn" data-act="close">关闭</button></div>
    <div class="relcontrols">
      <div class="relfield"><span>重要程度${lk("importance")}</span><span class="row relimp" role="group" aria-label="重要程度">${[1, 2, 3].map((n) => `<button type="button" class="chip ${x.importance === n ? "on" : ""}" data-act="person-imp" data-id="${x.id}" data-v="${n}">${REL_STARS(n)}</button>`).join("")}</span></div>
      <label class="relfield"><span>关系圈${lk("circle")}</span><select data-pf="circle" data-id="${x.id}"><option value="">没分</option>${(S.rel.circles || []).map((c) => `<option ${x.circle === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
      <label class="relfield"><span>多少天提醒一次${lk("contact_every_days")}</span><input type="number" min="1" max="3650" data-pf="contact_every_days" data-id="${x.id}" value="${x.every_set ? x.every : ""}" placeholder="默认 ${x.importance ? S.rel.defaults[x.importance] + " 天" : "按重要程度"}"></label>
    </div>
    <div class="relstats">
      <div><b>${x.last_contact ? esc(x.last_contact.slice(5).replace("-", "月")) + "日" : "—"}</b><span>最近联系${x.days != null ? `（${x.days} 天前）` : ""}</span></div>
      <div><b>${st.n90 ?? "—"}</b><span>近 90 天条数</span></div><div><b>${st.n365 ?? "—"}</b><span>近一年</span></div>
      <div><b>${pct(st.her_share)}</b><span>你说的占比</span></div><div><b>${pct(st.her_starts)}</b><span>你先开口</span></div>
    </div>
    ${(st.groups || []).length ? `<p class="muted" style="margin:0;font-size:12px">共同群：${st.groups.map(esc).join("、")}</p>` : ""}
    <div class="reldraft"><div class="row" style="justify-content:space-between"><label class="muted" style="font-size:12px" for="pd${x.id}">可以发的一句${lk("draft")}<span>（草稿，系统不会替你发，复制去微信里自己发）</span></label>${x.draft ? `<button type="button" class="btn tiny" data-act="person-copy" data-id="${x.id}">复制</button>` : ""}</div>
      <textarea id="pd${x.id}" data-pf="draft" data-id="${x.id}" rows="2" placeholder="${x.organized ? "这次没想到合适的，你可以自己写一句" : "整理后 小伴 会给一句"}">${esc(x.draft)}</textarea></div>
    ${loops ? `<div><h3 class="relh3">没了结的事</h3>${loops}</div>` : ""}
    <div class="relfields">
      ${field("recent", "对方近况", x.recent, { area: true, ph: "比如：刚换工作去了上海" })}
      ${field("topics", "常聊的话题（用逗号隔开）", (x.topics || []).join("，"))}
      ${field("how_met", "怎么认识的", x.how_met)}
      ${field("relation", "关系", x.relation, { max: 60 })}
      ${field("birthday", "生日（MM-DD）", x.birthday, { ph: "03-14", max: 5 })}
      ${field("notes", "备注", x.notes, { area: true })}
      ${field("name", "名字", x.name, { max: 30 })}
    </div>
    ${dates ? `<div><h3 class="relh3">重要日子</h3><div class="chips">${dates}</div></div>` : ""}
    <div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:8px"><span class="muted" style="font-size:11px">${x.organized ? "小伴 从你们的微信聊天整理的" : "还没整理（每晚整理几个人，聊得多的先）"}</span>${del}</div>
  </section>`;
}
document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const a = el.dataset.act, id = Number(el.dataset.id);
  if (a === "person-open") { S.personId = id; S.personDel = null; S.overlay = "person"; renderOverlay(); }
  else if (a === "person-imp") act("person.set", { id, field: "importance", value: Number(el.dataset.v) }, el);
  else if (a === "person-loopdone") act("person.loopdone", { id, text: el.dataset.text }, el);
  else if (a === "person-del") { S.personDel = id; renderOverlay(); }
  else if (a === "person-delno") { S.personDel = null; renderOverlay(); }
  else if (a === "person-delyes") { S.overlay = null; S.personDel = null; renderOverlay(); act("person.delete", { id }, el); }
  else if (a === "person-copy") { const x = relPerson(id); if (x && navigator.clipboard) navigator.clipboard.writeText(x.draft).then(() => showUndo("复制好了，去微信里发")); }
  else if (a === "rel-more") { S.relMore = { ...(S.relMore || {}), [el.dataset.lv]: true }; render(); }
  else if (a === "rel-circle") { S.relCircle = el.dataset.c; render(); }
});
document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (!t.matches || !t.matches("[data-pf]")) return;
  act("person.set", { id: Number(t.dataset.id), field: t.dataset.pf, value: t.value }, t);
});
document.addEventListener("input", (ev) => {
  if (!ev.target.matches || !ev.target.matches("[data-relq]")) return;
  S.relQ = ev.target.value; const pos = ev.target.selectionStart; render();
  const n = document.querySelector("[data-relq]"); if (n) { n.focus(); n.setSelectionRange(pos, pos); }
});
