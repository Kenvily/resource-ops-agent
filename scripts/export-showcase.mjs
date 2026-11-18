#!/usr/bin/env node
/** Render actual, de-identified database views and a project deck to PDF.
 * Requires explicit real-data approval. No Prometheus, LLM, tracking or resource writes.
 */
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFile, writeFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import os from "node:os";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const deckOnly = process.argv.includes("--deck-only");
const evidenceIndex = process.argv.indexOf("--value-evidence");
const valueEvidence = evidenceIndex < 0 ? null : JSON.parse(
  await readFile(process.argv[evidenceIndex + 1], "utf8"),
);
if (!process.argv.includes("--authorized-real-data")) {
  console.log(
    "Usage: node scripts/export-showcase.mjs --authorized-real-data [--deck-only] [--value-evidence FILE]\nRequires a built frontend, Playwright Chromium, local Live PostgreSQL and explicit data approval.\nOutputs: assets/screenshots/*.pdf and assets/project-overview.pdf. --deck-only preserves screenshot PDFs.",
  );
  process.exit(process.argv.includes("--help") ? 0 : 1);
}
const require = createRequire(path.join(root, "frontend/package.json"));
const { chromium } = require("@playwright/test");
const output = path.join(root, "assets");
const temporary = await mkdtemp(path.join(os.tmpdir(), "roc-showcase-"));
const escaped = (s) =>
  String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll('"', "&quot;");
const localDate = (value) =>
  new Date(value * 1000).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
  });
let browser, server;
let stage = "read-only extraction";
try {
  const data = await new Promise((resolve, reject) => {
    const child = spawn(
      path.join(root, ".venv/bin/python"),
      ["scripts/showcase-data.py", "--authorized-real-data"],
      { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
    );
    const chunks = [];
    let bytes = 0;
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(Error("Read-only export timed out"));
    }, 60000);
    child.stdout.on("data", (chunk) => {
      bytes += chunk.length;
      if (bytes > 16000000) {
        child.kill("SIGTERM");
        reject(Error("Export exceeded bound"));
      } else chunks.push(chunk);
    });
    child.stderr.on("data", () => {}); // Never print credentials, labels or raw errors.
    child.on("error", reject);
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code) reject(Error("Read-only data export failed"));
      else {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString()));
        } catch {
          reject(Error("Invalid export data"));
        }
      }
    });
  });
  if (
    !data.capture.read_only ||
    !data.capture.redacted ||
    data.capture.source !== "live_postgresql"
  )
    throw Error("Real-data export guards were not verified");
  const dist = path.join(root, "frontend/dist");
  server = createServer(async (req, res) => {
    try {
      const requested = decodeURIComponent(
        new URL(req.url, "http://localhost").pathname,
      );
      const file = path.resolve(
        dist,
        "." + (requested === "/" ? "/index.html" : requested),
      );
      if (!file.startsWith(dist + path.sep)) {
        res.writeHead(403).end();
        return;
      }
      const bytes = await readFile(file);
      const mime =
        {
          ".html": "text/html; charset=utf-8",
          ".js": "text/javascript",
          ".css": "text/css",
          ".svg": "image/svg+xml",
          ".png": "image/png",
        }[path.extname(file)] ?? "application/octet-stream";
      res.writeHead(200, { "Content-Type": mime });
      res.end(bytes);
    } catch {
      res.writeHead(404).end();
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    deviceScaleFactor: 1,
  });
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    if (!url.pathname.startsWith("/api/")) return route.continue();
    const send = (json) => route.fulfill({ json });
    const p = url.pathname;
    if (p === "/api/v1/overview") return send(data.overview);
    if (p === "/api/health") return send({ mode: "live", service: "ok" });
    if (p === "/api/v1/resource-statistics") return send(data.statistics);
    if (p === "/api/v1/operations/monitoring") return send(data.monitoring);
    if (p === "/api/v1/resource-statistics/trends") return send(data.trend);
    if (p === "/api/v1/insights/assess") return send(data.assessment);
    if (p === "/api/v1/analytics/summary") return send(data.analytics);
    if (p === "/api/v1/analytics/events")
      return send({ enabled: false, accepted: 0 });
    if (p === "/api/v1/operations/workspace/capabilities")
      return send({ available: false, write_enabled: false });
    if (p === "/api/v1/investigations") return send(data.tasks);
    if (p.startsWith("/api/v1/investigations/") && data.investigation)
      return send(data.investigation);
    return route.fulfill({
      status: 503,
      json: { detail: "本次只读导出未包含此视图，未执行额外查询" },
    });
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", () => errors.push("UI error"));
  const shots = [];
  const freezeLabel = `历史真实快照 · ${localDate(data.capture.source_as_of)} · 已脱敏`;
  const addCaptureLabel = async () => {
    await page.evaluate((label) => {
      const walker = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
      );
      for (let node = walker.nextNode(); node; node = walker.nextNode())
        node.textContent = node.textContent
          .replaceAll("亦庄", "集群 A")
          .replaceAll("兆维", "集群 B");
      document.getElementById("capture-label")?.remove();
      const tag = document.createElement("div");
      tag.id = "capture-label";
      tag.textContent = label;
      tag.style.cssText =
        'position:fixed;right:18px;bottom:12px;z-index:99999;background:#fff2d8;color:#815018;padding:9px 14px;border:1px solid #edcf94;border-radius:8px;font:12px "Noto Sans CJK SC",sans-serif;box-shadow:0 2px 8px #0001';
      document.body.append(tag);
    }, freezeLabel);
    await page.addStyleTag({
      content:
        '*{animation:none!important;transition:none!important}body{font-family:Inter,"Noto Sans CJK SC","Microsoft YaHei",sans-serif!important}',
    });
    await page.evaluate(() => document.fonts.ready);
  };
  const capture = async (slug, title, hash, action) => {
    stage = slug;
    await page.goto(origin + "/#" + hash);
    await page.locator("#page-title").waitFor();
    if (action) await action();
    await page.waitForLoadState("networkidle");
    await addCaptureLabel();
    const png = await page.screenshot({ fullPage: true });
    const width = png.readUInt32BE(16),
      height = png.readUInt32BE(20);
    const image = "data:image/png;base64," + png.toString("base64");
    const print = await browser.newPage({ viewport: { width, height } });
    await print.setContent(
      `<html><head><title>${escaped(title)} · 真实历史数据脱敏展示</title><style>@page{margin:0}html,body{margin:0;padding:0}img{display:block;width:${width}px;height:${height}px}</style></head><body><img src="${image}"/></body></html>`,
    );
    await print.evaluate(() =>
      Promise.all([...document.images].map((i) => i.decode())),
    );
    if (!deckOnly) await print.pdf({
      path: path.join(output, "screenshots", slug + ".pdf"),
      width: width + "px",
      height: height + "px",
      printBackground: true,
      margin: { top: 0, bottom: 0, left: 0, right: 0 },
    });
    await print.close();
    const focusImage =
      slug === "05-assistant" && data.investigation
        ? "data:image/png;base64," +
          (await page.locator(".agent-result").screenshot()).toString("base64")
        : image;
    shots.push({ slug, title, image, focusImage, width, height });
    if (!deckOnly) console.log("Exported:", slug + ".pdf");
  };
  await mkdir(path.join(output, "screenshots"), { recursive: true });
  await capture("01-workbench", "工作台", "workbench", () =>
    page.getByRole("region", { name: "资源概况" }).waitFor(),
  );
  await capture("02-servers", "服务器资源", "assets?view=servers", () =>
    page.getByRole("textbox", { name: "搜索服务器" }).waitFor(),
  );
  await capture("03-gpus", "GPU资产", "assets?view=gpus", () =>
    page.getByText("物理显存", { exact: false }).first().waitFor(),
  );
  await capture(
    "04-history",
    "历史分析",
    "analysis?view=history&range=1d",
    async () => {
      await page.getByLabel("历史趋势指标").selectOption("gpu_util");
      await page
        .getByRole("img", {
          name: "历史趋势折线图；缺口不连线，精确采样值可通过下方滑块和数据表查看",
        })
        .waitFor();
    },
  );
  await capture(
    "05-assistant",
    "资源助手与调查记录",
    data.investigation
      ? `analysis?view=assistant&task=${data.investigation.id}`
      : "analysis?view=assistant",
    async () => {
      if (data.investigation) {
        await page.getByRole("region", { name: "调查结论" }).waitFor();
        if (data.investigation.result?.answer_markdown?.includes("**"))
          await page.locator(".agent-prose > p strong").first().waitFor();
      }
    },
  );
  await capture("06-planning", "资源规划", "planning", async () => {
    await page.getByRole("button", { name: "开始评估", exact: true }).click();
    await page.locator(".planning-report").waitFor();
  });
  await capture("07-usage", "访问与Agent任务统计", "settings?view=usage", () =>
    page.getByRole("img", { name: "页面访问与活跃浏览器趋势" }).waitFor(),
  );
  if (errors.length)
    throw Error("UI capture failed; PDFs should be reviewed before use");
  const icon =
    "data:image/svg+xml;base64," +
    (await readFile(path.join(root, "frontend/public/app-icon.svg"))).toString(
      "base64",
    );
  const shot = (slug, cls = "") =>
    `<figure class="preview ${cls}"><img src="${shots.find((s) => s.slug === slug).focusImage}"/></figure>`;
  const tag = (s) => `<span class="tag">${s}</span>`;
  const card = (head, text) =>
    `<article class="card"><h3>${head}</h3><p>${text}</p></article>`;
  let valueSlide = `<p class="eyebrow">08 / 运行价值</p><h2>统一取数，调查过程可以追溯</h2><div class="grid three">${card("资源查询", "跨集群读取当前资源与历史，页面查询与后台采集分离。")}${card("任务去重", "执行中的重复请求被拒绝，已完成请求回放保存结果。")}${card("终态一致", "取消保留已完成证据，迟到完成不能覆盖已取消状态。")}</div>`;
  if (valueEvidence) {
    const s = valueEvidence.service, t = valueEvidence.task_consistency;
    if (s.mode !== "live" || s.transaction_read_only !== "on" || s.external_calls !== 0 ||
        s.benchmarks.some((b) => b.errors !== 0) ||
        t.kind !== "synthetic_agent_task_consistency" || t.worker_executions !== 1 ||
        t.additional_worker_executions_on_replay !== 0 || !t.different_request_same_id_rejected ||
        !t.cancelled_state_preserved_after_late_finish)
      throw Error("Quantitative evidence failed validation");
    const measure = (name) => {
      const row = s.benchmarks.find((b) => b.case === name && b.concurrency === 4);
      if (!row || row.runs < 20 || !Number.isFinite(row.p95_ms)) throw Error("Missing benchmark evidence");
      return row.p95_ms.toFixed(2);
    };
    const gpu = Object.values(s.current_observed_device_counts);
    if (!gpu.every(Number.isInteger)) throw Error("Current device inventory incomplete");
    const requests = s.benchmarks.reduce((n, b) => n + b.runs, 0);
    valueSlide = `<p class="eyebrow">08 / 实测结果</p><h2>真实数据查询与任务重试验证</h2><div class="grid three">${card(`${gpu.reduce((a,b) => a+b, 0)} 张 GPU`, `${gpu.length} 个真实集群的当前观测设备`) }${card(`${s.workloads.containers} 个容器`, "CPU与系统内存使用均有观测")}${card(`${requests} 次查询 · 0 错误`, "数据库只读，外部请求 0 次")}</div><div class="grid two" style="margin-top:24px">${card("真实库 · 查询响应", `4 并发，资源汇总 p95 <strong>${measure("statistics")} ms</strong>；<br/>单卡近三个月历史 p95 <strong>${measure("device_3mo")} ms</strong>。`)}${card("隔离测试库 · 任务一致性", `${t.simultaneous_same_id_requests} 个同ID并发请求 → <strong>${t.worker_executions} 次执行</strong>；<br/>${t.completed_replays} 次结果回放 → <strong>0 次额外执行</strong>。`)}</div><p class="note">${localDate(s.observed_at).split(" ")[0]} · 本机查询每组20次，不含HTTP/LLM；任务测试使用合成工作器。</p>`;
  }
  const slides = [
    `<div class="split"><div><img class="logo" src="${icon}"/><p class="eyebrow">RESOURCE OPERATIONS CENTER</p><h1>资源观测<br/>证据调查<br/><em>决策辅助</em></h1><p class="lead">面向 Kubernetes GPU 集群的<br/>资源运营平台与只读调查 Agent</p><div class="tags">${tag("FastAPI")}${tag("PostgreSQL")}${tag("React")}${tag("DeepSeek")}${tag("脱敏真实历史数据")}</div></div>${shot("01-workbench")}</div>`,
    `<p class="eyebrow">01 / 场景</p><h2>把资源状态与调查过程连接起来</h2><div class="grid three">${card("统一口径", "区分虚拟显存额度、物理显存和计算利用率，保留原始单位与缺测。")}${card("解释变化", "跨时间比较并逐层查证；区分业务增长、容量变化及数据质量问题。")}${card("可复核交付", "保留任务、证据、引用与受限状态；支持继续调查、规划和人工核对。")}</div><div class="flow">观测问题 <b>→</b> 收集证据 <b>→</b> 核验解释 <b>→</b> 人工决策</div>`,
    `<p class="eyebrow">02 / 产品</p><h2>按使用目标组织页面</h2><div class="split"><div class="grid two">${card("工作台 / 资产", "概况与下钻，服务器和GPU侧重不同。")}${card("业务模型", "显式归属与关联设备。")}${card("分析中心", "历史、资源助手、诊断与记录。")}${card("规划 / 运营", "额度候选、风险、报告与问题。")}</div>${shot("02-servers")}</div>`,
    `<p class="eyebrow">03 / 架构</p><h2>采集、存储、查询与调查分层</h2><div class="architecture"><div>Prometheus<br/><small>原始监控来源</small></div><b>→</b><div>后台采集<br/><small>回填 / 聚合 / 质量</small></div><b>→</b><div>PostgreSQL<br/><small>快照与历史</small></div></div><div class="architecture second"><div>页面 / Agent</div><b>→</b><div>统一领域 Service</div><b>→</b><div>数据库只读查询</div></div><div class="grid three">${card("前端", "React、TypeScript、Vite，本地CSS/SVG。")}${card("后端", "FastAPI、httpx、Pydantic、Psycopg。")}${card("轻量演进", "单实例，无Redis、队列或多Agent编排依赖。")}</div>`,
    `<p class="eyebrow">04 / 数据</p><h2>先保证指标含义和历史可靠性</h2><div class="split"><div><div class="flow compact">分钟 <b>→</b> 5分钟 <b>→</b> 小时</div>${card("聚合先于清理", "保留sum、极值、有效数、预期数及覆盖，均值按有效采样加权。")}${card("身份与范围", "GPU按集群/节点/UUID唯一；业务按实际标签关联，冲突与缺测不补零。")}${card("统计与采样", "小时点不冒充整小时均值，不据此判断持续低效或遗漏尖峰。")}</div>${shot("04-history")}</div>`,
    `<p class="eyebrow">05 / AGENT</p><h2>共享执行器，按证据推进调查</h2><div class="architecture"><div>目标与范围</div><b>→</b><div>白名单工具</div><b>→</b><div>Service取证</div><b>→</b><div>核验与回答</div></div><div class="grid three">${card("动态工具组合", "结合快照、历史、覆盖、同步状态、业务贡献及反例。")}${card("确定性核验", "校验数值、引用、单位和范围，不将模型自由文本作为测量事实。")}${card("受控执行", "并发、轮次、工具、时间及响应预算；幂等、取消和失败降级。")}</div>`,
    `<p class="eyebrow">06 / 调查交付</p><h2>自然语言回答，保留可核对依据</h2><div class="split">${shot("05-assistant")}<div>${card("任务记忆", "目标、对象、证据和待办持久化；继续时重新取证、继承或收窄范围，最多4轮。")}${card("流式Markdown", "正文按段返回，事实占位符由后端展开；执行过程与依据按需展开。")}${card("保留未知", "覆盖不足或冲突时不确定归因；失败、取消和旧记录有明确状态。")}</div></div>`,
    `<p class="eyebrow">07 / 资源规划</p><h2>让模型部署需求有清晰的额度初筛</h2><div class="split"><div><p class="lead">默认输入 GPU 数量与每卡显存。<br/>高级约束收起，候选结果便于比较。</p>${card("确定性评估", "根据未分配虚拟显存、节点约束和历史证据形成候选与风险。")}${card("人工决策", "历史最低额度不是未来预约；不承诺调度、部署或资源变更。")}</div>${shot("06-planning")}</div>`,
    valueSlide,
    `<p class="eyebrow">09 / 运营与下一步</p><h2>可观测使用过程，按真实需求演进</h2><div class="split"><div>${card("产品统计", "匿名PV/UV/DAU、功能触发及任务完整完成率；受限、取消和LLM调用未知单列。")}${card("生产边界", "企业SSO/RBAC、TLS/网络隔离、恢复演练与真实业务验收仍需完成。")}${card("下一步", "完善覆盖与可信信号，收集独立业务任务，再评估缓存、分区和队列。")}</div>${shot("07-usage")}</div>`,
  ];
  const html = `<html lang="zh-CN"><head><meta charset="utf-8"/><title>Resource Operations Center · 项目介绍</title><style>
    @page{size:13.333333in 7.5in;margin:0}*{box-sizing:border-box}body{margin:0;font-family:Inter,"Noto Sans CJK SC","Microsoft YaHei",sans-serif;color:#182b46;background:#fff}
    .slide{width:1280px;height:720px;padding:54px 64px 58px;position:relative;overflow:hidden;break-after:page;background:linear-gradient(120deg,#fff 62%,#f2f7fd)}.slide:last-child{break-after:auto}
    .slide:first-child h1{font-size:60px;margin:16px 0}.slide:first-child .lead{font-size:21px;line-height:1.75;margin:18px 0}.slide:first-child .logo{width:56px;height:56px}.split .card{padding:18px;margin-bottom:12px}.split .card p{font-size:17px;line-height:1.7}.slide:last-child .card p{font-size:16px;line-height:1.6}.slide:last-child .note{font-size:14px;line-height:1.6;margin-top:16px}
    .slide:before{content:"";position:absolute;left:0;top:0;bottom:0;width:8px;background:#075fa6}.eyebrow{font-size:13px;font-weight:700;letter-spacing:2px;color:#075fa6;margin:0 0 18px}h1{font-size:66px;line-height:1.15;letter-spacing:-2px;margin:20px 0}h1 em{font-style:normal;color:#075fa6}h2{font-size:36px;line-height:1.3;margin:0 0 32px;letter-spacing:-1px}h3{font-size:21px;margin:0 0 12px}.lead{font-size:23px;line-height:1.8;margin:24px 0}.note{font-size:17px;line-height:1.8;color:#52627a;margin-top:24px}.logo{width:64px;height:64px}.split{display:grid;grid-template-columns:1fr 1fr;gap:44px;align-items:center}.split .split{gap:20px}.grid{display:grid;gap:20px}.three{grid-template-columns:repeat(3,1fr)}.two{grid-template-columns:repeat(2,1fr)}.card{padding:22px;background:#f1f6fc;border:1px solid #dfe8f3;border-radius:14px;margin-bottom:16px}.card p{font-size:18px;line-height:1.75;margin:0}.grid .card{margin:0}.preview{margin:0;border:1px solid #dae3ef;border-radius:14px;box-shadow:0 12px 36px #18345113;height:480px;overflow:hidden;background:#f5f7fb}.preview img{width:100%;height:100%;object-fit:cover;object-position:top}.flow{display:flex;gap:24px;justify-content:center;align-items:center;font-size:26px;padding:32px;margin-top:40px;border-top:1px solid #dfe8f3}.flow b,.architecture b{color:#d58a00}.compact{font-size:22px;margin:0;padding:16px 0 24px}.architecture{display:flex;gap:18px;align-items:center;margin:18px 0 28px}.architecture>div{flex:1;background:#075fa6;color:white;padding:22px 12px;text-align:center;border-radius:12px;font-size:21px}.architecture small{font-size:15px;line-height:2;color:#daecfb}.architecture.second>div{background:#eaf3fc;color:#075fa6}.tags{display:flex;gap:10px;flex-wrap:wrap}.tag{background:#eaf3fc;color:#075fa6;padding:7px 12px;border-radius:6px;font-size:14px}.callout{padding:24px;border-left:5px solid #ffb52e;background:#fff6e7;font-size:23px;line-height:1.8}.footer{position:absolute;left:64px;right:64px;bottom:22px;display:flex;justify-content:flex-end;font-size:12px;color:#62738a}
  </style></head><body>${slides.map((s, i) => `<section class="slide">${s}<footer class="footer"><span>${String(i + 1).padStart(2, "0")} / ${slides.length}</span></footer></section>`).join("")}</body></html>`;
  stage = "presentation";
  const deck = await browser.newPage({
    viewport: { width: 1280, height: 720 },
  });
  await deck.setContent(html, { waitUntil: "load" });
  await deck.evaluate(() => document.fonts.ready);
  const overflowing = await deck
    .locator(".slide")
    .evaluateAll((sections) =>
      sections
        .map((s, i) => ({
          i: i + 1,
          overflow:
            s.scrollHeight > s.clientHeight || s.scrollWidth > s.clientWidth,
        }))
        .filter((s) => s.overflow),
    );
  for (const index of [0, 6, 8, 9])
    await deck
      .locator(".slide")
      .nth(index)
      .screenshot({
        path: path.join(os.tmpdir(), `roc-showcase-slide-${index + 1}.png`),
      });
  if (overflowing.length)
    throw Error("Slides overflow: " + overflowing.map((s) => s.i).join(","));
  await deck.pdf({
    path: path.join(output, "project-overview.pdf"),
    preferCSSPageSize: true,
    printBackground: true,
    margin: { top: 0, bottom: 0, left: 0, right: 0 },
  });
  // Keep only PDFs in the repository; diagnostic previews are local temporary files.
  await deck
    .locator(".slide")
    .first()
    .screenshot({ path: path.join(temporary, "deck-preview.png") });
  console.log("Exported: project-overview.pdf (" + slides.length + " slides)");
  console.log(
    "Capture source: authorized, de-identified historical PostgreSQL snapshot; no upstream calls.",
  );
} catch (error) {
  console.error("Stage:", stage);
  if (/^Slides overflow: [0-9,]+$/.test(error.message))
    console.error(error.message);
  console.error(
    "PDF export failed; check the local database, built frontend and Playwright. No private payload was logged.",
  );
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  if (server) await new Promise((resolve) => server.close(resolve));
  await rm(temporary, { recursive: true, force: true });
}
