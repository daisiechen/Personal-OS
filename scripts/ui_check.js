// 界面自动检查（docs/design/system-rules.md 第 6 节）：每次改界面后在开发会话里跑。
// 用法：python3 scripts/ui_demo.py <目录> 8810 &  然后  node scripts/ui_check.js <目录> [截图目录]
// 查：每个页面和常用弹窗都能打开、没有脚本报错；每个导航红点点进去有东西；弹窗只有右上角一个关闭、关了再开是默认状态；
// 返回键在右上角；iPad 和手机两种尺寸各截一张图（大改时发给她，平时自查）。
const { chromium } = require("playwright");
const fs = require("fs");
const dir = process.argv[2], shots = process.argv[3] || "";
const base = "http://127.0.0.1:" + (process.env.UI_PORT || "8810");
const PAGES = ["main", "map", "me", "relations", "team", "settings"];
const TABS = [["map", "reviews"], ["map", "history"], ["map", "ach"]];
const OVERLAYS = ['[data-act="prep"]', '[data-act="desk"]'];

(async () => {
  const tok = fs.readFileSync(dir + "/token.txt", "utf8").trim();
  const b = await chromium.launch();
  const problems = [];
  for (const [name, vp] of [["ipad", { width: 1180, height: 820 }], ["phone", { width: 390, height: 844 }]]) {
    const ctx = await b.newContext({ viewport: vp });
    await ctx.addCookies([{ name: "eo_session", value: tok, url: base }]);
    const p = await ctx.newPage();
    p.on("pageerror", (e) => problems.push(`[${name}] 脚本报错：${e.message}`));
    await p.goto(base + "/");
    await p.waitForTimeout(1500);
    const visit = async (label) => {
      await p.waitForFunction(() => !document.querySelector("#page .loading"), null, { timeout: 15000 }).catch(() => problems.push(`[${name}] ${label} 一直在加载`));
      const len = await p.evaluate(() => document.querySelector("#page").innerText.trim().length);
      if (len < 10) problems.push(`[${name}] ${label} 是空的`);
      const back = await p.$(".backbtn");
      if (back) {
        const bx = await back.boundingBox(), w = vp.width;
        if (bx && bx.x + bx.width < w * 0.6) problems.push(`[${name}] ${label} 的返回键不在右上角`);
      }
      if (shots) await p.screenshot({ path: `${shots}/${name}-${label}.png` });
    };
    for (const pg of PAGES) {
      await p.click(`#nav [data-page="${pg}"]`);
      await visit(pg);
      const dot = await p.$(`#nav [data-page="${pg}"] .navdot`);
      if (dot) {
        const n = Number(await dot.innerText());
        if (!(n > 0)) problems.push(`[${name}] ${pg} 的红点不是正数：${await dot.innerText()}`);
      }
    }
    for (const [pg, tab] of TABS) {
      await p.click(`#nav [data-page="${pg}"]`);
      await p.waitForTimeout(600);
      const btn = await p.$(`[data-act="maptab"][data-v="${tab}"]`);
      if (!btn) { problems.push(`[${name}] 赛季页没有“${tab}”按钮`); continue; }
      await btn.click();
      await visit(`${pg}-${tab}`);
    }
    await p.click('#nav [data-page="settings"]');  // 设置页的“系统体检”一屏（2026-11-04）
    await p.waitForTimeout(600);
    const sysTab = await p.$('[data-act="settab"][data-v="sys"]');
    if (!sysTab) problems.push(`[${name}] 设置页没有“系统体检”`);
    else {
      await sysTab.click();
      await visit("settings-sys");
      if (!(await p.$(".sysgrid .sysrow"))) problems.push(`[${name}] 系统体检里没有定时任务`);
      await p.click('[data-act="settab"][data-v="set"]');
    }
    await p.click('#nav [data-page="relations"]');  // 关系页：点一张人物卡能打开、有关闭键
    await p.waitForTimeout(800);
    const tile = await p.$(".reltile");
    if (!tile) problems.push(`[${name}] 关系页没有人物卡`);
    else {
      await tile.click();
      await p.waitForTimeout(500);
      if (!(await p.$('#overlay .relsheet [data-act="close"]'))) problems.push(`[${name}] 人物卡没有关闭键`);
      if (shots) await p.screenshot({ path: `${shots}/${name}-person.png` });
      await p.keyboard.press("Escape");
      await p.waitForTimeout(300);
    }
    await p.click('#nav [data-page="me"]');  // “我”页“删除和对外”：对外开关两张表，住址那行锁死没有按钮
    await p.waitForTimeout(800);
    const mg = await p.$('[data-act="me-open"][data-k="me-manage"]');
    if (!mg) problems.push(`[${name}] “我”页没有“删除和对外”`);
    else {
      await mg.click();
      await p.waitForSelector("#overlay .poltable", { timeout: 8000 }).catch(() => problems.push(`[${name}] 对外开关没出来`));
      const tables = await p.$$("#overlay .poltable");
      if (tables.length !== 2) problems.push(`[${name}] 对外开关不是两张表：${tables.length}`);
      const lockedBtns = await p.$$("#overlay .pol.locked button");
      if (lockedBtns.length) problems.push(`[${name}] 住址联系方式那行还能点`);
      const wide = await p.evaluate(() => { const o = document.querySelector("#overlay .sheet"); return o && o.scrollWidth > o.clientWidth + 2; });
      if (wide) problems.push(`[${name}] 对外开关横向溢出`);
      if (shots) { await p.evaluate(() => { const t = document.querySelector("#overlay .polgrid"); t && t.scrollIntoView({ block: "start" }); });
        await p.waitForTimeout(200); await p.screenshot({ path: `${shots}/${name}-outside.png` }); }
      await p.keyboard.press("Escape");
      await p.waitForTimeout(300);
    }
    await p.click('#nav [data-page="main"]');
    await p.waitForTimeout(800);
    // “先放着”的红点有去处（她 11-05）：首页按钮上有，点进去“先放着”那栏上也有同样的数；点开看过就消失
    const homeDot = await p.$('[data-act="prep"] .parkdot');
    if (homeDot) {
      const n = (await homeDot.innerText()).replace(/\D/g, "");
      await p.click('[data-act="prep"]');
      await p.waitForTimeout(600);
      const tabDot = await p.$('#overlay [data-act="preptab"][data-tab="parked"] .navdot');
      if (!tabDot) problems.push(`[${name}] 首页有“先放着”红点，点进去那栏上没有`);
      else if ((await tabDot.innerText()).trim() !== n) problems.push(`[${name}] “先放着”红点首页 ${n}，里面 ${await tabDot.innerText()}`);
      if (shots) await p.screenshot({ path: `${shots}/${name}-prep-parked-dot.png` });
      if (tabDot) {
        await tabDot.click();
        await p.waitForTimeout(800);
        if (shots) await p.screenshot({ path: `${shots}/${name}-prep-parked.png` });
        if (await p.$('#overlay [data-act="preptab"][data-tab="parked"] .navdot')) problems.push(`[${name}] 看过“先放着”红点还在`);
      }
      await p.keyboard.press("Escape");
      await p.waitForTimeout(400);
    }
    for (const sel of OVERLAYS) {
      const el = await p.$(sel);
      if (!el) { problems.push(`[${name}] 首页没有 ${sel}`); continue; }
      await el.click();
      await p.waitForTimeout(800);
      const closes = await p.$$('#overlay [data-act="close"]');
      if (closes.length === 0) problems.push(`[${name}] 弹窗 ${sel} 没有关闭键`);
      const first = await p.evaluate(() => document.querySelector("#overlay") && document.querySelector("#overlay").innerHTML.length);
      await p.keyboard.press("Escape");
      await p.waitForTimeout(400);
      const open = await p.evaluate(() => typeof S !== "undefined" && !!S.overlay);
      if (open) problems.push(`[${name}] 弹窗 ${sel} 按 Esc 关不掉`);
      await el.click().catch(() => {});
      await p.waitForTimeout(800);
      const again = await p.evaluate(() => document.querySelector("#overlay") && document.querySelector("#overlay").innerHTML.length);
      if (first && again && Math.abs(first - again) > first * 0.2) problems.push(`[${name}] 弹窗 ${sel} 关了再开不是原来的样子`);
      await p.keyboard.press("Escape");
      await p.waitForTimeout(300);
    }
    // 保存失败（Codex 评审 11-05）：服务器回 ok:false 时不能提示成功、不能清掉她填的字
    await p.click('#nav [data-page="main"]');
    await p.waitForTimeout(600);
    const add = await p.$('[data-act="addtodo"]');
    if (!add) problems.push(`[${name}] 首页没有“加一件”`);
    else {
      await add.click();
      await p.waitForSelector('#overlay form[data-form="addtodo"] input[name="title"]', { timeout: 5000 }).catch(() => {});
      const title = await p.$('#overlay form[data-form="addtodo"] input[name="title"]');
      if (!title) problems.push(`[${name}] 加待办的表单没出来`);
      else {
        await p.route("**/api/act", (r) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: false, msg: "测试：存不上" }) }));
        await title.fill("失败检查用的待办");
        await p.click('#overlay form[data-form="addtodo"] button:not([type="button"])');
        await p.waitForTimeout(600);
        const still = await p.evaluate(() => { const i = document.querySelector('#overlay form[data-form="addtodo"] input[name="title"]'); return i ? i.value : null; });
        if (still !== "失败检查用的待办") problems.push(`[${name}] 保存失败后填的字没了：${still}`);
        const bar = await p.evaluate(() => document.querySelector("#undo").innerText);
        if (!bar.includes("测试：存不上")) problems.push(`[${name}] 保存失败没提示原因：${bar}`);
        await p.route("**/api/act", (r) => r.abort());  // 连不上服务器：也一样
        await p.click('#overlay form[data-form="addtodo"] button:not([type="button"])', { timeout: 2000 }).catch(() => {});
        await p.waitForTimeout(600);
        const still2 = await p.evaluate(() => { const i = document.querySelector('#overlay form[data-form="addtodo"] input[name="title"]'); return i ? i.value : null; });
        if (still2 !== "失败检查用的待办") problems.push(`[${name}] 连不上服务器时填的字没了：${still2}`);
        if (!(await p.evaluate(() => document.querySelector("#undo").innerText)).includes("没连上")) problems.push(`[${name}] 连不上服务器没提示`);
        await p.unroute("**/api/act");
        await p.keyboard.press("Escape");
        await p.waitForTimeout(300);
      }
    }
    await ctx.close();
  }
  await b.close();
  console.log(problems.length ? problems.join("\n") : "界面检查通过");
  process.exit(problems.length ? 1 : 0);
})();
