const { chromium } = require(
  process.env.FORGE_PLAYWRIGHT_MODULE || "playwright",
);
const fs = require("fs"),
  assert = require("assert");
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.FORGE_BROWSER_PATH,
    args: ["--no-sandbox"],
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  let fail = false;
  const now = Date.now() / 1000;
  const repo = {
    repo: "example/academy",
    short: "academy",
    counts: {
      ready: 2,
      wip: 1,
      review: 1,
      audit: 1,
      held: 0,
      conflicts: 0,
      sentback: 0,
      limbo: 0,
    },
    queue_status: "ok",
    observed_at: now,
    schedule: {
      ordered: [1, 2],
      eligible: [2],
      active: [1],
      blocked: {
        1: ["active_claim"],
        2: ["dependency_open:example/academy#1"],
      },
    },
    work: { review: [3], audit: [4] },
    build: { up: true, busy: true, provider: "codex" },
    review: { up: true, busy: false },
    audit: { up: false, busy: false },
  };
  const fleet = {
    ts: "2026-09-07 22:10:00",
    repos: [
      repo,
      {
        ...repo,
        repo: "example/quiet",
        short: "quiet",
        counts: {},
        schedule: {},
        work: {},
        build: { up: false },
        review: { up: false },
        queue_status: "ok",
      },
      {
        ...repo,
        repo: "example/stale",
        short: "stale",
        queue_status: "stale",
        observed_at: now - 2000,
      },
    ],
    capacity: {
      observed_at: now - 200,
      reason: "fleet_session_limit",
      requested_session: "academy-build",
      admitted: false,
      fleet_count: 3,
      limits: { max_sessions: 6, max_sessions_per_role: 3 },
    },
    slots: {status: "ok", observed_at: now, occupied: 5, roles: {build: 2, review: 3, audit: 0}},
    health: { account_until: 0, sessions: {"academy-build": {until: now+3600, reason: "no_progress"}} },
    routing: {status: "ok", providers: {claude: {reason: "blocked_quota", blocked_until: now+3600}}},
    log: ["test <script>bad()</script>"],
    notification_events: [
      {
        created_at: now,
        message: "Merged PR #8",
        channels: { ntfy: { status: "accepted_by_relay" } },
      },
    ],
    notifications: [],
  };
  fleet.issue_flow = {
    status: "ok",
    issues: [
      {
        repo: repo.repo,
        number: 1,
        title: "Build the issue",
        state: "build",
        step: 1,
        pr_numbers: [],
        blockers: [],
      },
      {
        repo: repo.repo,
        number: 2,
        title: "Ready issue",
        state: "ready",
        step: 0,
        pr_numbers: [],
        blockers: [],
      },
      {
        repo: repo.repo,
        number: 5,
        title: "Review issue",
        state: "review",
        step: 2,
        pr_numbers: [3],
        blockers: [],
      },
      {
        repo: repo.repo,
        number: 6,
        title: "Audit issue",
        state: "audit",
        step: 3,
        pr_numbers: [4],
        blockers: [],
      },
    ],
    counts: { in_flight: 3, ready: 1 },
    unlinked_prs: [],
  };
  const history = {
    status: "ok",
    observed_at: now,
    coverage_since: now - 30 * 86400,
    repos: [
      {
        repo: repo.repo,
        counts: {
          pending: 5,
          build: 1,
          review: 1,
          audit: 1,
          held: 0,
          completed: 12,
          closed: 2,
        },
      },
    ],
    daily: Array.from({ length: 30 }, (_, i) => ({
      date: "2026-08-" + String(i + 1).padStart(2, "0"),
      opened: i % 4,
      completed: i % 3,
      merged: i % 2,
    })),
    events: [
      {
        repo: repo.repo,
        number: 3,
        kind: "pr",
        previous_stage: "review",
        stage: "audit",
        observed_at: now,
        summary: "Review → Security audit",
      },
    ],
  };
  const detail = {
    repo: repo.repo,
    observed_at: now,
    items: [
      {
        number: 3,
        title: 'Fix <img src=x onerror="window.pwned=1">',
        kind: "pr",
        stage: "audit",
        summary: "Changes ready for security audit",
        labels: ["ready-for-audit"],
        evidence: [
          {
            kind: "audit",
            author: "auditor",
            created_at: now,
            summary: "Criticality: NORMAL",
            body: "<script>window.pwned=1</script>\nEvidence: app.py:42",
            url: "https://github.com/example/academy/pull/3#issuecomment-1",
          },
        ],
      },
      {
        number: 2,
        title: "Pending item",
        kind: "issue",
        stage: "pending",
        labels: [],
      },
      {
        number: 9,
        title: "Completed item",
        kind: "issue",
        stage: "completed",
        labels: [],
      },
    ],
    events: history.events,
    errors: [],
  };
  await page.route("http://forge.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/")
      return route.fulfill({
        contentType: "text/html",
        body: fs.readFileSync(
          require("path").join(__dirname, "../legacy/dashboard.html"),
          "utf8",
        ),
      });
    if (url.pathname === "/api/status" && fail)
      return route.fulfill({ status: 503, body: "offline" });
    const data =
      url.pathname === "/api/status"
        ? fleet
        : url.pathname === "/api/history"
          ? history
          : url.pathname === "/api/repository"
            ? detail
            : "worker output";
    return route.fulfill({
      contentType: typeof data === "string" ? "text/plain" : "application/json",
      body: typeof data === "string" ? data : JSON.stringify(data),
    });
  });
  await page.goto("http://forge.test/");
  await page.waitForSelector(".repo-row");
  await page.waitForSelector("[data-day]");
  assert.equal(await page.locator(".stage").count(), 0);
  assert.equal(await page.locator("#pipeline").count(), 0);
  assert.equal(await page.locator(".issue-card").count(), 4);
  assert.match(await page.locator(".stat-slots").innerText(), /Slots occupied/);
  assert.match(await page.locator(".stat-slots .number").innerText(), /5\s*\/\s*6/);
  assert.match(await page.locator(".slot-states").innerText(), /2 working/);
  assert.match(await page.locator(".slot-states").innerText(), /1 unknown/);
  assert.match(await page.locator(".slot-roles").first().innerText(), /Review 3\/3/);
  assert.match(await page.locator(".provider-outages").innerText(), /Claude unavailable.*retry in 60m/);
  assert.equal(await page.locator(".working-stack .issue-card").count(), 1);
  assert.match(await page.locator(".working-stack .issue-card").innerText(), /Codex/);
  assert.equal(await page.locator(".pending-stack .issue-card").count(), 3);
  assert.equal(await page.locator(".mini-card.moving").count(), 1);
  assert.equal(await page.locator(".role-card.working").count(), 0);
  assert(!(await page.locator("#repos").innerText()).includes("Snapshot"));
  assert.equal(await page.locator('#interventions, #timeline, #logs, #activity').count(), 0);
  assert.equal(await page.locator('#unified-activity').count(), 1);
  await page.evaluate(() => {
    const at=new Date().toISOString();
    fleet.host={observed_at:Date.now()/1000,services:{'forge-supervisor.service':'inactive'}};
    fleet.activity={events:[
      {event:'dispatch',session:'academy-build',at,metadata:{reason:'initial_task'}},
      {event:'dispatch',session:'academy-build',at,metadata:{reason:'initial_task'}},
      ...Array.from({length:8},(_,i)=>({event:'admission_deferred',session:'quiet-build',at:new Date(Date.now()+i).toISOString(),metadata:{capacity:{reason:'role_session_limit'}}})),
      ...Array.from({length:6},(_,i)=>({event:'session_stopped',session:'worker-'+i,at:new Date(Date.now()-i*1000).toISOString(),metadata:{reason:'handoff'}}))
    ]};
    fleet.notification_events.push({message:'Delivery failed <script>bad()</script>',created_at:Date.now()/1000,channels:{ntfy:{status:'retry',last_attempt_at:Date.now()/1000}}});
    render();
  });
  assert.equal(await page.locator('#unified-latest .unified-row').count(), 5);
  assert((await page.locator('#unified-latest').innerText()).includes('Supervisor is inactive'));
  assert(!(await page.locator('#unified-latest').innerText()).includes('quiet-build'));
  await page.locator('#unified-expanded > summary').click();
  await page.locator('#unified-search').fill('academy-build');
  assert.equal(await page.locator('#unified-older .unified-row').count(), 1);
  await page.locator('#unified-search').fill('quiet-build');
  assert.equal(await page.locator('#unified-older .unified-row').count(), 8);
  await page.evaluate(()=>render());
  assert(await page.locator('#unified-expanded').evaluate(el=>el.open));
  assert.equal(await page.locator('#unified-search').inputValue(),'quiet-build');
  await page.locator('#unified-search').fill('');
  await page.locator('#unified-source').selectOption('pipeline');
  assert.equal(await page.locator('#unified-older [data-log-source="pipeline"]').count(),1);
  await page.locator('#unified-source').selectOption('supervisor');
  assert((await page.locator('#unified-older').innerText()).includes('Time unknown'));
  await page.evaluate(()=>{fleet.host.services['forge-supervisor.service']='active';render();});
  await page.locator('#unified-source').selectOption('operator');
  assert((await page.locator('#unified-older').innerText()).includes('No longer present'));
  await page.locator('#unified-source').selectOption('all');
  await page.locator('#unified-expanded > summary').click();
  await page.evaluate(()=>{
    const r=fleet.repos[0];
    r.schedule.blocked[21]=['triage_pending'];
    r.schedule.triage_pending=[21];
    r.triage={status:'applied',at:Date.now()/1000,changes:[{number:21,changes:['priority p1']}]};
    render();
  });
  assert(!(await page.locator('#attention').innerText()).includes('Triage pending'));
  await page.evaluate(()=>{fleet.repos[0].triage={status:'failed',at:Date.now()/1000,error:'Provider unavailable'};render();});
  assert((await page.locator('#attention').innerText()).includes('Triage failed · pending #21: Provider unavailable'));
  assert((await page.locator('#attention').innerText()).includes('other eligible issues can continue'));
  assert.equal(await page.locator('#attention-title').innerText(),'Needs attention');
  await page.evaluate(()=>{delete fleet.repos[0].triage;delete fleet.repos[0].schedule.blocked[21];render();});
  await page.evaluate(() => {
    fleet.repos[1].review = {up:true,busy:false};
    fleet.health.sessions['quiet-review'] = {until:Date.now()/1000+60,reason:'provider_start_failed'};
    fleet.session_blockers = {'quiet-review':{reason:'provider_start_failed',why:'Provider startup was not confirmed <script>bad()</script>',next:'Configure the native executable and inspect startup diagnostics.'}};
    render();
  });
  const workerAlert=page.locator('.worker-attention-card[data-worker-fault="quiet-review"]');
  assert.equal(await workerAlert.count(),1);
  assert(await workerAlert.evaluate(el=>el.classList.contains('compact-issue') && !el.classList.contains('moving')));
  assert.equal(await workerAlert.locator('[data-card-issue], [data-issue]').count(),0);
  assert((await workerAlert.locator('.compact-identity').innerText()).includes('WORKER'));
  assert(!(await workerAlert.innerText()).includes('Configure the native executable'));
  await workerAlert.locator('summary').click();
  await workerAlert.locator('summary').focus();
  await page.evaluate(()=>renderAttention());
  assert(await workerAlert.locator('details').evaluate(el=>el.open));
  assert.equal(await page.evaluate(()=>document.activeElement.tagName),'SUMMARY');
  assert((await workerAlert.innerText()).includes('Configure the native executable'));
  assert((await workerAlert.innerText()).includes('no exact issue assignment'));
  assert.equal(await page.evaluate(()=>window.pwned),undefined);
  await page.evaluate(()=>{
    delete fleet.health.sessions['quiet-review'];
    fleet.session_blockers['quiet-review']={kind:'cleanup_retained',reason:'worktree_not_clean',why:'Uncommitted files prevent worker cleanup.',next:'Preserve generated files and verify the published handoff.',impact:'This idle worker occupies a slot.'};
    render();
  });
  assert.equal(await workerAlert.count(),1);
  assert((await workerAlert.locator('.issue-card-title').innerText()).includes('Uncommitted files'));
  await workerAlert.locator('summary').click();
  assert((await workerAlert.innerText()).includes('Preserve generated files'));
  await page.evaluate(()=>{
    fleet.repos[1].review={up:false,busy:false};
    delete fleet.health.sessions['quiet-review'];
    fleet.session_blockers={};
    render();
  });
  assert.equal(await page.locator('.worker-attention-card').count(),0);
  await page.emulateMedia({ reducedMotion: "reduce" });
  assert.equal(
    await page
      .locator(".mini-card.moving")
      .evaluate((el) => getComputedStyle(el, "::after").animationName),
    "none",
  );
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.evaluate(() => {
    for (let number = 20; number < 24; number++)
      fleet.issue_flow.issues.push({
        repo: "example/academy",
        number,
        title: "Waiting " + number,
        state: "ready",
        step: 0,
        pr_numbers: [],
        blockers: [],
      });
    renderNow();
  });
  assert.equal(
    await page
      .locator('[data-station="build"] .pending-stack .issue-card:visible')
      .count(),
    0,
  );
  await page.locator(".more-pending summary").first().click();
  await page.waitForTimeout(50);
  await page.evaluate(() => renderNow());
  assert(
    await page
      .locator(".more-pending")
      .first()
      .evaluate((el) => el.open),
  );
  await page.evaluate(() => {
    fleet.issue_flow.unlinked_prs.push({
      repo: "example/academy",
      number: 99,
      stage: "build",
    });
    renderNow();
    renderRepos();
  });
  assert.equal(await page.locator(".issue-card.moving").count(), 0);
  assert.equal(await page.locator(".repo-row .mini-card.moving").count(), 0);
  assert.equal(await page.locator(".assignment-gap").count(), 1);
  await page.evaluate(() => {
    fleet.issue_flow.unlinked_prs = [];
    fleet.issue_flow.issues = fleet.issue_flow.issues.filter(
      (i) => i.number < 20,
    );
    renderNow();
    renderRepos();
  });
  await page.screenshot({ path: "/tmp/forge-desktop.png", fullPage: true });
  await page.locator('[data-day="5"]').click();
  assert(
    (await page.locator("#chart-tip").textContent()).includes("2026-08-06"),
  );
  await page.locator('.repo-row[data-repo="example/academy"]').click();
  await page.waitForSelector(".evidence");
  assert.equal(await page.locator(".issue").count(), 3);
  const drawerIssue = page.locator('[data-drawer-issue="5"]');
  assert.equal(await drawerIssue.count(), 1);
  assert((await drawerIssue.locator('.drawer-stage').innerText()).includes('Review 2/3'));
  assert.equal(await drawerIssue.locator('.supporting-pr').count(), 1);
  assert.equal(await drawerIssue.locator('.evidence').count(), 1);
  assert.equal(await page.locator('.drawer-unlinked').count(), 0);
  await page.locator('#item-search').fill('#3');
  assert.equal(await page.locator('.drawer-issue').count(), 1);
  assert.equal(await page.locator('[data-drawer-issue="5"]').count(), 1);
  await page.locator('#item-search').fill('#13');
  assert.equal(await page.locator('.drawer-issue').count(), 0);
  await page.locator('#item-search').fill('#5');
  assert.equal(await page.locator('.supporting-pr').count(), 1);
  await page.locator('#item-search').fill('');
  await page.evaluate(() => {
    fleet.repos[0].review.busy = true;
    renderDetail();
  });
  assert.equal(await page.locator('.drawer-issue.moving[data-station="review"]').count(), 1);
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await page.locator('.drawer-issue.moving').evaluate(el => getComputedStyle(el,'::after').animationName), 'none');
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.evaluate(() => {
    fleet.repos[0].review.busy = false;
    renderDetail();
  });
  assert.equal(await page.locator('.drawer-issue.moving').count(), 0);

  await page.locator(".evidence summary").click();
  await page.locator(".evidence summary").focus();
  await page.evaluate(() => renderDetail());
  assert.equal(
    await page.evaluate(() => document.activeElement.tagName),
    "SUMMARY",
  );
  assert.equal(await page.evaluate(() => window.pwned), undefined);
  await page.locator('#worker-tabs [data-role="build"]').click();
  await page.waitForFunction(
    () =>
      document.querySelector("#pane-output").textContent === "worker output",
  );
  assert(await page.locator(".evidence").evaluate((x) => x.open));
  await page.screenshot({ path: "/tmp/forge-detail.png", fullPage: true });
  await page.locator("#item-stage").selectOption("completed");
  assert.equal(await page.locator(".issue").count(), 1);
  assert(
    (await page.locator(".issue").textContent()).includes("Completed item"),
  );
  await page.keyboard.press("Escape");
  await page.locator("#search").fill("quiet");
  assert.equal(await page.locator(".repo-row").count(), 1);
  await page.locator("#search").fill("");
  fail = true;
  await page.evaluate(() => load());
  await page.waitForSelector("#error:not(.hidden)");
  assert.equal(await page.locator(".repo-row").count(), 3);
  fail = false;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => load());
  await page.waitForSelector("#error.hidden", { state: "attached" });
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  await page.screenshot({ path: "/tmp/forge-mobile.png", fullPage: true });
  await page.locator('.repo-row[data-repo="example/academy"]').click();
  await page.waitForSelector(".issue");
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  await page.screenshot({
    path: "/tmp/forge-mobile-detail.png",
    fullPage: true,
  });
  assert.deepEqual(errors, []);
  console.log(
    "PASS: desktop/mobile, graphs, filters, evidence persistence, escaping, worker output, refresh failure",
  );
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
