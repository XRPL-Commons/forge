const renderPage=require('./browser_page.cjs');
const { chromium } = require(
  process.env.FORGE_PLAYWRIGHT_MODULE || "playwright",
);
const fs = require("fs"),
  assert = require("assert");
(async () => {
  const browser = await chromium.launch({chromiumSandbox:true,
    headless: true,
    executablePath: process.env.FORGE_BROWSER_PATH,
    args: [],
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
    build: { up: true, busy: true, provider: "codex", generation_id:"$1|100|%1|1001" },
    review: { up: true, busy: false, generation_id:"$2|100|%2|1002" },
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
  fleet.workflow = {status:"ok",read_at:now,repositories:[{repo:repo.repo,observed_at:now}],
    issues:[{repo:repo.repo,number:1,stage:"build",disposition:"working",prs:[]},
            {repo:repo.repo,number:5,stage:"review",disposition:"pending",prs:[{number:3,head:"a",base:"b"}]}],
    assignments:[{id:"build-assignment",repo:repo.repo,role:"build",session:"academy-build",issue_numbers:[1],state:"working",provider:"codex",generation:"$1|100|%1|1001"},
                 {id:"review-assignment",repo:repo.repo,role:"review",session:"academy-review",issue_numbers:[5],pr:{number:3,head:"a",base:"b"},state:"submitted",provider:"claude",generation:"$2|100|%2|1002"}],
    slots:[{session:"academy-build",repo:repo.repo,role:"build",state:"working",assignment_id:"build-assignment",generation:"$1|100|%1|1001",reason:"execution_running"},
           {session:"academy-review",repo:repo.repo,role:"review",state:"reserved",assignment_id:"review-assignment",generation:"$2|100|%2|1002",reason:"waiting_for_provider"},
           {session:"stale-build",repo:"example/stale",role:"build",state:"working",assignment_id:null,generation:"$1|100|%1|1001",reason:"started_before_assignment_tracking"}]};
  fleet.controls_token = 'synthetic-control-token';
  fleet.slot_actions = [];
  const slotRequests = [];
  let slotReply = {status:200, body:{ok:true,reason:'queued',id:'slot-request-1'}};
  let servedDashboardVersion="a".repeat(64), documentRequests=0;
  let workflowPages = [];
  await page.route("http://forge.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/") {
      documentRequests++;
      return route.fulfill({
        contentType: "text/html",
        body: renderPage('dashboard.html',url.pathname).replace("__FORGE_DASHBOARD_VERSION__",servedDashboardVersion),
      });
    }
    if (url.pathname === "/api/slots/action") {
      slotRequests.push({method:route.request().method(),body:route.request().postDataJSON(),token:route.request().headers()['x-forge-action']});
      return route.fulfill({status:slotReply.status,contentType:'application/json',body:JSON.stringify(slotReply.body)});
    }
    if (url.pathname === "/api/status" && fail)
      return route.fulfill({ status: 503, body: "offline" });
    const data =
      url.pathname === "/api/status"
        ? fleet
        : url.pathname === "/api/workflow"
          ? (workflowPages.shift() || {...fleet.workflow,events:[],next_after:Number(url.searchParams.get("after")),has_more:false})
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
  assert.equal(await page.locator("[data-card-issue]").count(), 4);
  assert.equal(await page.locator('.occupied-slot').count(),5);
  const assertSlotCounts = async () => {
    const counts = await page.evaluate(() => roles.map(role => ({role,
      summary:Number(document.querySelector(`[data-slot-role="${role}"]`).dataset.slotCount),
      cards:document.querySelectorAll(`[data-station="${role}"] .occupied-slot`).length})));
    for (const row of counts) assert.equal(row.summary,row.cards,`${row.role} occupied summary matches cards`);
  };
  await assertSlotCounts();
  assert.match(await page.locator('[data-slot-role="review"]').innerText(), /Review \d+\/3 occupied/);
  assert.equal(await page.locator('[data-slot-session^="Unidentified"] [data-slot-action]:enabled').count(),0);
  assert(await page.locator('[data-slot-session="academy-review"]').isVisible());
  assert.match(await page.locator('[data-slot-session="academy-review"]').innerText(), /#5/);
  assert.equal(await page.locator('[data-slot-session="academy-review"] .moving').count(),0);
  await page.evaluate(()=>{fleet.workflow.slots.find(s=>s.session==='academy-review').state='retiring';fleet.workflow.slots.find(s=>s.session==='academy-review').reason='codex_execution_completed';renderNow();renderStats();});
  await assertSlotCounts();
  assert.match(await page.locator('[data-slot-session="academy-review"]').innerText(), /Retiring/);
  assert.match(await page.locator('[data-slot-session="academy-review"] .slot-state').getAttribute('title'), /codex execution completed/i);
  await page.evaluate(()=>{fleet.workflow.slots.find(s=>s.session==='academy-review').state='reserved';fleet.workflow.slots.find(s=>s.session==='academy-review').reason='waiting_for_provider';renderNow();});

  assert.match(await page.locator('[data-slot-session="stale-build"] .slot-note').getAttribute('title'), /issue unassigned/);
  await page.evaluate(()=>{
    window.slotFixture=JSON.parse(JSON.stringify(fleet));
    fleet.repos[0].review={up:true,busy:false,state:'blocked',generation_id:'$2|100|%2|1002'};
    fleet.workflow.slots.find(s=>s.session==='academy-review').state='blocked';
    fleet.workflow.issues.find(i=>i.number===5).disposition='stalled';
    fleet.issue_flow.issues.find(i=>i.number===5).attention_state='STALLED';
    fleet.slots.sessions=[{session:'disabled-audit',repo:'example/disabled',role:'audit',generation:'$7|100|%7|1007'}];
    fleet.workflow.slots.push({session:'quiet-audit',repo:'example/quiet',role:'audit',state:'reserved',generation:null,assignment_id:null});
    renderNow();
  });
  assert(await page.locator('[data-slot-session="academy-review"]').isVisible());
  assert.match(await page.locator('[data-slot-session="academy-review"]').innerText(), /Stalled/);
  assert.match(await page.locator('[data-slot-session="academy-review"]').innerText(), /#5/);
  assert.equal(await page.locator('[data-slot-session="academy-review"] .moving').count(),0);
  assert(await page.locator('[data-slot-session="disabled-audit"]').isVisible());
  assert.equal(await page.locator('[data-slot-session="disabled-audit"] [data-slot-action="stop"]').isEnabled(),true);
  assert.equal(await page.locator('[data-slot-session="quiet-audit"] [data-slot-action="release"]').isDisabled(),true);
  await page.evaluate(()=>{fleet=window.slotFixture;delete window.slotFixture;renderNow();});
  // Opening or canceling a native confirmation never writes to the controller.
  await page.locator('[data-slot-session="academy-build"] [data-slot-action="stop"]').click();
  assert.equal(slotRequests.length,0);
  assert(await page.locator('#slot-dialog-cancel').evaluate(el=>el===document.activeElement));
  await page.locator('#slot-dialog-cancel').click();
  assert.equal(slotRequests.length,0);
  assert.equal(await page.locator('#slot-action-dialog').isVisible(),false);
  await page.locator('[data-slot-session="academy-build"] [data-slot-action="stop"]').click();
  await page.evaluate(()=>{fleet.repos[0].build.generation_id='$99|100|%99|1099';renderNow();});
  await page.locator('#slot-dialog-confirm').click();
  assert.equal(slotRequests.length,0);
  assert.match(await page.locator('#slot-action-status').innerText(),/worker changed/i);
  await page.evaluate(()=>{fleet.repos[0].build.generation_id='$1|100|%1|1001';renderNow();});
  await page.locator('[data-slot-session="academy-build"] [data-slot-action="stop"]').click();
  assert(await page.locator('#slot-action-dialog').isVisible());
  await page.locator('#slot-dialog-confirm').click();
  await page.waitForFunction(()=>pendingSlotActions.get('academy-build')?.id==='slot-request-1');
  await page.waitForFunction(()=>!loading);
  assert.deepEqual(slotRequests[0],{method:'POST',body:{action:'stop',session:'academy-build',generation:'$1|100|%1|1001'},token:'synthetic-control-token'});
  assert(await page.locator('[data-slot-session="academy-build"] [data-slot-action="stop"]').isDisabled());
  assert(await page.locator('[data-slot-session="academy-build"]').isVisible()); // Queueing never pretends release succeeded.
  fleet.slot_actions=[{id:'slot-request-1',session:'academy-build',generation:'$1|100|%1|1001',status:'queued',reason:'queued'}];
  await page.evaluate(()=>{pendingSlotActions.clear();seenSlotActions.clear();return load();});
  assert(await page.locator('[data-slot-session="academy-build"] [data-slot-action="stop"]').isDisabled());
  fleet.slot_actions=[{id:'slot-request-1',session:'academy-build',generation:'$1|100|%1|1001',status:'succeeded',action:'stop',paused:true,updated_at:100,reason:'operator_stopped',message:'Worker stopped; edits preserved.'}];
  await page.evaluate(()=>load());
  assert.match(await page.locator('#slot-action-status').innerText(),/edits preserved/);
  assert(await page.locator('[data-slot-session="academy-build"] [data-slot-action="resume"]').isVisible());
  await page.evaluate(()=>{fleet.repos[0].build.generation_id='$9|200|%9|1009';renderNow();});
  assert.equal(await page.locator('[data-slot-session="academy-build"] [data-slot-action="resume"]').count(),0);
  await page.evaluate(()=>{fleet.repos[0].build.generation_id='$1|100|%1|1001';renderNow();});
  slotReply={status:200,body:{ok:true,reason:'queued',id:'resume-request-1'}};
  await page.locator('[data-slot-session="academy-build"] [data-slot-action="resume"]').click();
  assert(await page.locator('#slot-action-dialog').isVisible());
  await page.locator('#slot-dialog-confirm').click();
  await page.waitForFunction(()=>pendingSlotActions.get('academy-build')?.id==='resume-request-1');
  await page.waitForFunction(()=>!loading);
  assert.deepEqual(slotRequests[1],{method:'POST',body:{action:'resume',session:'academy-build',generation:'$1|100|%1|1001'},token:'synthetic-control-token'});
  fleet.slot_actions.unshift({id:'resume-request-1',session:'academy-build',generation:'$1|100|%1|1001',status:'succeeded',action:'resume',paused:false,updated_at:101,reason:'operator_resumed'});
  await page.evaluate(()=>load());
  assert.equal(await page.locator('[data-slot-session="academy-build"] [data-slot-action="resume"]').count(),0);

  slotReply={status:409,body:{ok:false,reason:'generation_changed',message:'Generation changed <script>window.pwned=1</script>'}};
  await page.locator('[data-slot-session="academy-review"] [data-slot-action="release"]').click();
  assert(await page.locator('#slot-action-dialog').isVisible());
  await page.locator('#slot-dialog-confirm').click();
  await page.waitForFunction(()=>slotActionMessages.has('academy-review'));
  assert.deepEqual(slotRequests[2].body,{action:'release',session:'academy-review',generation:'$2|100|%2|1002'});
  assert.match(await page.locator('[data-slot-session="academy-review"] .slot-action-message').innerText(),/Generation changed <script>/);
  assert.equal(await page.evaluate(()=>window.pwned),undefined);
  assert(await page.locator('[data-slot-session="academy-review"]').isVisible());
  await page.evaluate(()=>{
    pendingSlotActions.set('academy-review',{id:'delayed-refusal',generation:'$2|100|%2|1002'});
    fleet.slot_actions=[{id:'delayed-refusal',session:'academy-review',generation:'$2|100|%2|1002',status:'refused',reason:'release_proof_required',message:'Worker retained',proof_reasons:['worktree_not_clean','processes_not_quiescent']}];
    renderNow();
  });
  assert.match(await page.locator('[data-slot-session="academy-review"] .slot-action-message').innerText(),/worktree not clean.*processes not quiescent/i);
  await page.evaluate(()=>{delete fleet.controls_token;renderNow();});
  assert(await page.locator('[data-slot-session="academy-build"] [data-slot-action="stop"]').isDisabled());
  fleet.slot_actions=[];
  await page.evaluate(()=>{pendingSlotActions.clear();slotActionMessages.clear();slotActionSummary='';return load();});

  assert.match(await page.locator(".stat-slots").innerText(), /Slots occupied/);
  assert.match(await page.locator(".stat-slots .number").innerText(), /5\s*\/\s*6/);
  assert.match(await page.locator(".slot-states").innerText(), /2 working/);
  assert.match(await page.locator(".slot-states").innerText(), /1 unknown/);
  assert.match(await page.locator(".slot-states").innerText(), /2 idle/);
  assert.match(await page.locator(".slot-context").innerText(), /academy #5.*review.*waiting for provider/i);
  assert.match(await page.locator(".slot-context").innerText(), /Started before assignment tracking/);
  await page.evaluate(()=>{
    fleet.workflow.assignments.push({id:'reserved-only',repo:'example/quiet',session:'quiet-build',role:'build',issue_numbers:[20],state:'reserved',generation:null});
    fleet.workflow.slots.push({session:'quiet-build',repo:'example/quiet',role:'build',state:'reserved',assignment_id:'reserved-only',reason:'capacity_admission_pending',generation:null});
    renderStats();renderNow();
  });
  await assertSlotCounts();
  assert.match(await page.locator('.stat-slots .number').innerText(), /6\s*\/\s*6/);
  assert.match(await page.locator('.slot-context').innerText(), /quiet #20.*build.*capacity admission pending/i);
  await page.evaluate(()=>{fleet.workflow.assignments=fleet.workflow.assignments.filter(a=>a.id!=='reserved-only');fleet.workflow.slots=fleet.workflow.slots.filter(s=>s.session!=='quiet-build');renderStats();});

  const readyGate=await page.evaluate(()=>{
    const original=JSON.parse(JSON.stringify(fleet)), baseline=pipelineIssues().length, r=fleet.repos[0];
    fleet.issue_flow.backlog=Array.from({length:124},(_,n)=>({repo:r.repo,number:1000+n,title:`Unlabeled backlog ${n}`,state:"backlog"}));
    fleet.workflow.issues.push(...fleet.issue_flow.backlog.map(i=>({repo:r.repo.toLowerCase(),number:i.number,stage:"queued",disposition:"pending",prs:[]})),
      ...[2000,2001,2002].map(number=>({repo:r.repo.toLowerCase(),number,stage:"queued",disposition:"pending",prs:[]})));
    r.queue_status="ok";r.observed_at=Date.now()/1000;r.work={...r.work,ready:[...(r.work?.ready || []),2001]};
    r.schedule={eligible:[],blocked:{2001:["depends_on:2000"]}};
    fleet.issue_flow.issues.push({repo:r.repo,number:2002,title:"Source ready label",state:"ready"});
    render();
    const fresh={count:pipelineIssues().length,backlog:currentIssueRecords().find(i=>i.number===1000),
      unknown:currentIssueRecords().find(i=>i.number===2000).state,
      blockedReady:currentIssueRecords().find(i=>i.number===2001).state,
      backlogCards:document.querySelectorAll('[data-card-issue*="#10"]').length};
    r.observed_at=Date.now()/1000-301;
    const stale=currentIssueRecords().find(i=>i.number===2001).state;
    fleet=original;render();return {baseline,fresh,stale};
  });
  assert.equal(readyGate.fresh.count,readyGate.baseline+2);
  assert.equal(readyGate.fresh.backlog.state,'backlog');
  assert.equal(readyGate.fresh.backlog.title,'Unlabeled backlog 0');
  assert.equal(readyGate.fresh.unknown,'unknown');
  assert.equal(readyGate.fresh.blockedReady,'ready');
  assert.equal(readyGate.fresh.backlogCards,0);
  assert.equal(readyGate.stale,'unknown');
  // Old loaded scripts still map queued to ready; the status API now supplies only gated inventory.
  const oldClient=await page.evaluate(status=>{
    const projected=status.workflow.issues.map(i=>({...i,state:({queued:"ready",backlog:"ready"}[i.stage] || i.stage)}));
    return {waiting:projected.filter(i=>i.state==="ready").length,backlog:projected.filter(i=>i.number>=1000).length};
  },{workflow:{projection:"pipeline",inventory_issue_count:126,issues:[{number:1,stage:"queued"},{number:2,stage:"queued"},{number:3,stage:"build"}]}});
  assert.deepEqual(oldClient,{waiting:2,backlog:0});


  assert.match(await page.locator(".slot-roles").first().innerText(), /Review 3\/3/);
  assert.match(await page.locator(".provider-outages").innerText(), /Claude unavailable.*retry in 60m/);
  assert.equal(await page.locator(".working-stack [data-card-issue]").count(), 2);
  assert.match(await page.locator(".working-stack .issue-card.moving").innerText(), /Codex/);
  assert.equal(await page.locator(".pending-stack .issue-card").count(), 2);
  assert.equal(await page.locator(".mini-card.moving").count(), 1);
  assert.equal(await page.locator(".role-card.working").count(), 0);
  await page.evaluate(()=>{fleet.repos[0].repo='Example/Academy';for(const issue of fleet.issue_flow.issues) if(issue.repo==='example/academy') issue.repo='Example/Academy';renderNow();});
  assert.equal(await page.locator('.working-stack [data-card-issue]').count(),2);
  assert.equal(await page.locator('[data-slot-session="academy-build"] [data-card-issue] button').getAttribute('data-repo'),'Example/Academy');
  assert.equal(await page.locator('.assignment-gap').count(),0);
  await page.evaluate(()=>{fleet.repos[0].repo='example/academy';for(const issue of fleet.issue_flow.issues) if(issue.repo==='Example/Academy') issue.repo='example/academy';renderNow();});

  await page.evaluate(()=>{window.savedIssue=fleet.issue_flow.issues.shift();renderNow();});
  assert.match(await page.locator('[data-slot-session="academy-build"] [data-card-issue]').innerText(),/Issue #1 · details not collected/);
  assert.equal(await page.locator('.assignment-gap').count(),0);
  await page.evaluate(()=>{fleet.issue_flow.issues.unshift(window.savedIssue);delete window.savedIssue;renderNow();});

  await page.evaluate(()=>{fleet.workflow.status='unavailable';render();});
  assert.equal(await page.locator('.issue-card.moving').count(),0);
  assert(await page.locator('#workflow-state-note').isVisible());
  assert.match(await page.locator('#workflow-state-note').innerText(),/Workflow state unavailable/);
  assert.equal(await page.locator('#stats .stat').nth(1).locator('.number').innerText(),'—');
  await page.evaluate(()=>{fleet.workflow.status='ok';render();});
  assert.equal(await page.locator('.issue-card.moving').count(),1);

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
  workflowPages.push({...fleet.workflow,events:[{id:10,repo:repo.repo,operation:"transition",at:now,payload:{action:"working",after:{role:"build",issue_numbers:[1]}}}],next_before:10,has_more:true});
  await page.evaluate(()=>load()); // Recent transitions load on the normal automatic refresh path.
  await page.waitForFunction(()=>document.querySelector('#workflow-history-status').textContent.includes('1 durable'));
  await page.locator('#unified-source').selectOption('workflow');
  assert.match(await page.locator('#unified-older').innerText(), /academy.*working.*#1/i);
  assert.equal(await page.locator('#workflow-history-more').innerText(),'Load older workflow events');
  await page.locator('#unified-source').selectOption('all');

  const slotsBeforeParked=await page.locator('.stat-slots .number').innerText();
  await page.evaluate(()=>{
    fleet.parked_work={status:"ok",issues:[{repo:fleet.repos[0].repo,number:612,summary:"Typecheck: 451 diagnostics <script>bad()</script>",branch:"issue-612-patched-dependencies",head:"a".repeat(40),branch_url:`https://github.com/${fleet.repos[0].repo}/tree/issue-612-patched-dependencies`,checkpoint_url:`https://github.com/${fleet.repos[0].repo}/commit/${"a".repeat(40)}`,next:"Fix failing checks on the preserved branch, then resume."}]};render();
  });
  const parkedCard=page.locator('#attention [data-card-issue$="#612"]');
  assert.equal(await parkedCard.count(),1);
  assert.equal(await page.locator('.stat-slots .number').innerText(),slotsBeforeParked);
  assert.equal(await parkedCard.locator('.attention-state').innerText(),'STALLED');
  assert.equal(await parkedCard.evaluate(e=>e.classList.contains('moving')),false);
  assert.equal(await parkedCard.locator('script').count(),0);
  await parkedCard.locator('summary').click();
  assert.match(await parkedCard.innerText(),/Fix failing checks on the preserved branch/);
  assert.equal(await parkedCard.locator('a[href*="/commit/"]').count(),2);
  assert.equal(await page.locator('[data-worker-fault*="612"]').count(),0);
  await page.evaluate(()=>{delete fleet.parked_work;render();});

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
  assert(!(await workerAlert.innerText()).includes('no exact issue assignment'));
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
    fleet.session_blockers['quiet-review']={kind:'cleanup_retained',reason:'detached_merged_pr_not_unique',
      why:'The reviewer finished, but Forge could not identify one merged PR for its checkout.',
      next:'Match the checkout to its published PR before releasing the idle session.',
      context:{repo:'example/quiet',issue:127,pr:131,basis:'Previously queued'}};
    render();
  });
  assert.match(await workerAlert.locator('.compact-identity').innerText(), /quiet · #127/);
  assert.equal(await workerAlert.locator('.issue-card-title').innerText(),'Review cleanup · merged PR not identified');
  assert.equal(await workerAlert.locator('.issue-card-title').getAttribute('data-issue'),'127');
  assert.equal(await workerAlert.locator('a[href="https://github.com/example/quiet/pull/131"]').count(),1);
  assert.match(await workerAlert.innerText(), /Previously queued/);
  await workerAlert.locator('summary').click();
  assert(!(await workerAlert.innerText()).includes('The reviewer finished'));
  assert(!(await workerAlert.innerText()).includes('no exact issue assignment'));
  await page.evaluate(()=>{fleet.session_blockers['quiet-review'].context.repo='other/repo';renderAttention();});
  assert.equal(await workerAlert.locator('[data-issue]').count(),0);
  assert.equal(await workerAlert.locator('.worker-context').count(),0);

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
  assert.equal(await page.locator(".issue-card.moving").count(), 1); // Unrelated PR cannot erase exact assignment.
  await page.evaluate(()=>{fleet.repos[0].build.generation_id="$9|200|%9|1009";renderNow();renderRepos();});
  assert.equal(await page.locator(".issue-card.moving").count(), 0);
  assert.equal(await page.locator(".repo-row .mini-card.moving").count(), 0);
  assert.match(await page.locator('[data-slot-session="academy-build"] .slot-note').getAttribute('title'), /Unassigned slot/);
  await page.evaluate(() => {
    fleet.repos[0].build.generation_id="$1|100|%1|1001";
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
    fleet.workflow.issues.find(i=>i.number===5).disposition="working";
    fleet.workflow.assignments.find(a=>a.id==="review-assignment").state="working";
    fleet.workflow.slots.find(s=>s.session==="academy-review").state="working";
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
  await page.evaluate(()=>{
    closeRepo();window.beforeQuarantineSlots=occupiedSlots().length;
    fleet.execution_quarantines={status:'ok',items:[
      {id:'exact-checkpoint',repo:'example/academy',session:'academy-audit',role:'audit',assignment_id:'prior-assignment',issue_numbers:[6],phase:'confirmed',released:true,head:'a'.repeat(40),branch:'issue-6-audit',final_message:'Checks failed <img src=x onerror="window.pwned=1">',last_lifecycle_failure:{reason:'worktree_not_clean'},reason:'workspace_reconciliation_required'},
      {id:'legacy-checkpoint',repo:'example/academy',session:'academy-build',role:'build',assignment_id:null,issue_numbers:[],previous_issue_context:{number:612},phase:'prepared',released:false,head:'b'.repeat(40),branch:'legacy-work',final_message:'Final result needs reconciliation',reason:'workspace_reconciliation_required'}]};
    render();
  });
  assert.equal(await page.locator('.execution-quarantine-card').count(),2);
  const held=page.locator('[data-quarantine="exact-checkpoint"]');
  await held.locator('summary').click();
  assert.match(await held.innerText(),/#6/);assert.match(await held.innerText(),/Slot freed.*workspace still blocked/);
  assert.match(await held.innerText(),/worktree not clean/i);
  assert.match(await held.innerText(),/Checks failed <img/);
  assert.equal(await page.evaluate(()=>window.pwned),undefined);
  assert.equal(await page.evaluate(()=>occupiedSlots().length===window.beforeQuarantineSlots),true);
  assert.equal(await page.locator('.execution-quarantine-card [data-slot-action]').count(),0);
  const legacyHeld=page.locator('[data-quarantine="legacy-checkpoint"]');
  await legacyHeld.locator('summary').click();
  assert.match(await legacyHeld.innerText(),/Unassigned workspace/);assert.match(await legacyHeld.innerText(),/Previously claimed #612/);
  assert.match(await legacyHeld.innerText(),/Slot release unconfirmed/);
  assert.equal(await legacyHeld.locator('[data-issue]').count(),0);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.evaluate(()=>{fleet.execution_quarantines.items[0].final_message='GitHub reports conflicts and an old base.';render();});
  assert.match(await held.innerText(),/reported merge conflicts/);
  await page.evaluate(()=>{fleet.execution_quarantines.items[0].final_message='Reviewer policy forbids workflow changes.';render();});
  assert.match(await held.innerText(),/conflicting scope and policy/);
  await page.evaluate(()=>{fleet.execution_quarantines={status:'ok',items:[]};delete window.beforeQuarantineSlots;render();});
  await page.evaluate(()=>{
    const issue=fleet.workflow.issues.find(i=>i.number===1);
    window.retryIssueBefore=JSON.parse(JSON.stringify(issue));
    Object.assign(issue,{stage:"failed",disposition:"stalled",reason:"rejected_cycle_limit",retry:{rejected_cycles:3,max_rejected_cycles:3}});
    render();
  });
  const failedIssue=page.locator('#attention [data-card-issue="example/academy#1"]');
  assert.equal(await failedIssue.count(),1);
  assert.match(await failedIssue.innerText(),/FAILED/);
  assert.match(await failedIssue.innerText(),/3 rejected cycles/);
  assert.equal(await failedIssue.locator('[data-slot-action]').count(),0);
  assert.match(await page.locator('#attention-title').innerText(),/failed/);
  await page.evaluate(()=>{
    Object.assign(fleet.workflow.issues.find(i=>i.number===1),window.retryIssueBefore);
    delete fleet.workflow.issues.find(i=>i.number===1).retry;
    delete window.retryIssueBefore;
    const issue=fleet.workflow.issues.find(i=>i.number===1);
    window.priorDisposition=issue.disposition;issue.disposition="pending";
    render();
  });
  assert.match(await page.locator('[data-slot-session="academy-build"] .slot-state').innerText(),/Verifying assignment/);
  await page.evaluate(()=>{fleet.workflow.issues.find(i=>i.number===1).disposition=window.priorDisposition;delete window.priorDisposition;render();});
  const updateStart=documentRequests;
  servedDashboardVersion="b".repeat(64);fleet.dashboard_version=servedDashboardVersion;
  await Promise.all([page.waitForURL(url=>url.searchParams.get('ui_version')===servedDashboardVersion),page.evaluate(()=>{void load();})]);
  await page.waitForSelector('.occupied-slot');
  assert.equal(documentRequests,updateStart+1);
  await page.evaluate(()=>load());
  assert.equal(documentRequests,updateStart+1); // Matching new bundle never loops.
  servedDashboardVersion="a".repeat(64); // Simulate a proxy repeatedly returning an old bundle.
  await page.goto('http://forge.test/?stale=1');
  await page.waitForSelector('.occupied-slot');
  assert.match(await page.locator('#error').innerText(),/refreshed page did not match/);
  await page.evaluate(()=>load());
  assert.equal(documentRequests,updateStart+2); // One attempted reload per target version.
  assert.deepEqual(errors, []);
  console.log(
    "PASS: desktop/mobile, graphs, filters, evidence persistence, escaping, worker output, refresh failure",
  );
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
