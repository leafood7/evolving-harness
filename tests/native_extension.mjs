// Offline integration: real Python bridge + extension lifecycle, no model calls.
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, cpSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import evolving from '../extensions/evolving.ts';
import { bridge } from '../extensions/evolving.ts';

const temporary = mkdtempSync(join(tmpdir(), 'harness-extension-'));
Object.assign(process.env, { EH_HOME: resolve('.'), EH_DB: join(temporary, 'state.db'),
  EH_USER: 'fixture', EH_PROJECT: 'fixture', PYTHONUTF8: '1' });
const handlers = {}, commands = {}, notifications = [], widgets = {}, tools = {};
let resets = 0, confirms = 0, status = '';
const ctx = {
  cwd: temporary,
  isIdle: () => true,
  sessionManager: { getSessionId: () => 'offline-fixture' },
  model: { provider: 'fixture', id: 'never-call' },
  ui: {
    setStatus: (_, value) => { status = value; },
    setWidget: (key, value) => { widgets[key] = value; },
    notify: value => notifications.push(value),
    confirm: async () => { confirms++; return true; },
  },
  newSession: async options => {
    resets++;
    const next = { ...ctx, ui: { ...ctx.ui } };
    const oldUi = ctx.ui;
    ctx.ui = new Proxy({}, {get() {throw new Error('stale context after replacement');}});
    try { await options?.withSession?.(next); }
    finally { ctx.ui = oldUi; }
    // Detect access to the original ctx in the continuation after newSession resolves.
    ctx.ui = new Proxy({}, {get() {throw new Error('stale context after replacement');}});
    return { cancelled: false };
  },
};
const liveUi = ctx.ui;
evolving({ on: (name, handler) => { handlers[name] = handler; },
  registerTool: tool => {tools[tool.name]=tool;},
  registerCommand: (name, command) => { commands[name] = command.handler; } });
try {
  await handlers.session_start({}, ctx);
  assert.match(status, /coding v1/);
  await commands.memory('add use unittest', ctx);
  const first = { messageType: 'user', prompt: 'fix tests', systemPromptOptions: { sections: {} } };
  await handlers.before_agent_start(first, ctx);
  assert.match(first.systemPromptOptions.sections.evolving_harness, /use unittest/);
  await handlers.tool_execution_start({ toolCallId: 'one', toolName: 'bash', args:{command:'fixture only'} }, ctx);
  await handlers.tool_execution_end({ toolCallId: 'one', toolName: 'bash', isError: false,
    result:{content:[{type:'text',text:'fixture result'}]} }, ctx);
  handlers.message_end({ message: { role: 'assistant', content: [{type:'thinking',thinking:'omit'}, {type:'text',text:'done'}], stopReason:'stop' } }, ctx);
  await handlers.agent_settled({ aborted: false }, ctx);
  await commands.harness('feedback duplicate repeated instruction', ctx);
  await commands.harness('note user correction', ctx);
  await commands.harness('review local', ctx);
  const candidate = (await bridge({operation:'experience-list'})).candidates[0];
  assert.equal(candidate.status,'pending');
  await commands.experience(`show ${candidate.id}`,ctx);
  await commands.experience(`reject ${candidate.id}`,ctx);
  assert.equal((await bridge({operation:'experience-get',id:candidate.id})).candidate.status,'rejected');
  await commands.harness('task', ctx);
  const evidence = await bridge({operation:'task'});
  assert.equal(evidence.summary.latest_verification, 'unverified');
  assert.equal(evidence.summary.tool_records, 1);
  assert(evidence.events.some(e => e.kind === 'native_tool' && e.data.args.command === 'fixture only'));
  assert(evidence.events.some(e => e.kind === 'user_feedback'));
  await commands.harness('', ctx);
  await commands.harness('memory-review', ctx);
  assert.match(widgets['evolving-harness'].join('\n'), /记忆效果评审/);
  assert.match(widgets['evolving-harness'].join('\n'), /证据不足/);
  await commands.harness('nonexistent-command', ctx);
  assert.match(notifications.at(-1), /未知 Harness 子命令/);
  const memories = await bridge({operation:'status'});
  assert.equal(memories.memories, 1);
  // Read memory ID from the UI and exercise actual reset + deletion.
  await commands.memory('list', ctx);
  const id = (await bridge({operation:'memory-list'})).memories[0].id;
  await commands.memory(`forget ${id}`, ctx);
  ctx.ui = liveUi; // A subsequent command receives the fresh session context.
  assert.equal(resets, 1);
  assert.equal(confirms, 1);
  const second = { messageType:'user', prompt:'next task', systemPromptOptions:{sections:{}} };
  await handlers.before_agent_start(second, ctx);
  assert.doesNotMatch(second.systemPromptOptions.sections.evolving_harness, /use unittest/);
  await handlers.agent_settled({ aborted: true }, ctx);
  await handlers.session_shutdown({}, ctx);
  assert.equal(notifications.filter(n => n.includes('已记录任务')).length, 2);
  await commands.memory('add revision fixture',ctx);
  const mid = (await bridge({operation:'memory-list'})).memories[0].id;
  await commands.harness(`memory-review propose ${mid}`,ctx);
  let revision = (await bridge({operation:'memory-revision-list'})).revision_candidates[0];
  assert.equal(revision.action,'keep');
  await commands.harness(`memory-review accept ${revision.id}`,ctx);
  assert.equal((await bridge({operation:'memory-list'})).memories[0].revision,1);
  await commands.harness(`memory-review propose ${mid}`,ctx);
  revision = (await bridge({operation:'memory-revision-list'})).revision_candidates.at(-1);
  const edits = ['corrected command','explicit fixture user correction'];
  ctx.ui.editor = async () => edits.shift();
  await commands.harness(`memory-review edit ${revision.id}`,ctx);
  await commands.harness(`memory-review accept ${revision.id}`,ctx);
  assert.equal(resets,2);
  ctx.ui = liveUi;
  assert.equal((await bridge({operation:'memory-list'})).memories[0].revision,2);
  await commands.harness('memory-review accept',ctx);
  assert.match(notifications.at(-1),/用法/);
  await bridge({operation:'begin',prompt:'fixture evidence',workspace:temporary});
  ctx.ui.confirm = async () => false;
  await commands.harness(`memory-review model ${mid}`,ctx);
  assert.match(widgets['evolving-harness'].join('\n'), /模型修订载荷预览/);
  assert.match(widgets['evolving-harness'].join('\n'), /先检查完整预览/);
  await commands.harness('center',ctx);
  assert.match(widgets['evolving-harness'].join('\n'),/Harness 控制台/);
  await commands.harness('package last-error',ctx);
  assert.match(widgets['evolving-harness'].join('\n'),/尚无诊断记录/);
  await commands.harness('strategy feedback scope explicit fixture scope problem',ctx);
  await commands.harness('strategy propose',ctx);
  const firstStrategy=(await bridge({operation:'product-status'})).product.history.at(-1);
  const {component_evaluation:gate}=await bridge({operation:'component-evaluate',id:firstStrategy.id});
  ctx.ui.confirm=async()=>true;
  await commands.harness(`package activate ${firstStrategy.id} ${gate.evaluation_id}`,ctx);
  ctx.ui=liveUi;
  assert.match(widgets['evolving-harness'].join('\n'),/组件版本已更换/);
  await commands.harness('strategy feedback expiry explicit fixture stale problem',ctx);
  await commands.harness('strategy propose',ctx);
  const nextStrategy=(await bridge({operation:'product-status'})).product.history.at(-1);
  assert.equal(nextStrategy.generator,firstStrategy.id);
  ctx.ui.confirm=async()=>false;
  await commands.harness(`benchmark ${nextStrategy.id}`,ctx);
  assert.match(widgets['evolving-harness'].join('\n'),/策略对照载荷/);
  ctx.ui.confirm=async()=>true;
  await commands.harness('demo recursive',ctx);
  assert.match(widgets['evolving-harness'].join('\n'),/独立临时数据库递归验收/);
  ctx.ui.editor=async()=> '用中文说明\n优先小步修改';
  await commands.harness('profile',ctx);
  ctx.ui=liveUi;
  const withProfile={messageType:'user',prompt:'fix code',systemPromptOptions:{sections:{}}};
  await handlers.before_agent_start(withProfile,ctx);
  assert.match(withProfile.systemPromptOptions.sections.evolving_harness,/优先小步修改/);
  await handlers.agent_settled({aborted:false},ctx);
  const {component:toolComponent}=await bridge({operation:'component-stage',draft:{name:'tool:inspect',
    payload:{kind:'tool',description:'Inspect workspace','recipe':'workspace_summary','timeout':3},
    reason:'fixture adapter',evidence_ids:[]}});
  const {component_evaluation:toolGate}=await bridge({operation:'component-evaluate',id:toolComponent.id});
  await bridge({operation:'component-activate',id:toolComponent.id,evaluation_id:toolGate.evaluation_id,confirmed:true});
  ctx.ui.confirm=async()=>false;
  const declined=await tools.harness_recipe.execute('fixture',{name:'tool:inspect'},null,null,ctx);
  assert.equal(declined.details.declined,true);
  ctx.ui.confirm=async()=>true;
  const ran=await tools.harness_recipe.execute('fixture',{name:'tool:inspect'},null,null,ctx);
  assert.equal(ran.details.tool_result.status,'completed');
  await commands.harness('improve',ctx);
  ctx.ui=liveUi;
  assert.match(widgets['evolving-harness'].join('\n'),/批量改进已启用/);
  await commands.harness('improve status',ctx);
  assert.match(widgets['evolving-harness'].join('\n'),/跟踪后续使用/);
  const {component:evalSkill}=await bridge({operation:'component-stage',draft:{name:'skill:eval-fixture',
    payload:{kind:'skill',description:'Offline evaluation UI fixture',triggers:['test'],steps:['Run actual project tests.']},
    reason:'Exercise preview and decline without model calls',evidence_ids:[]}});
  cpSync(resolve('examples/evaluation'),join(temporary,'eval'),{recursive:true});
  const manifestPath=join(temporary,'eval','suite.json');
  const evalConfig=JSON.parse(readFileSync(manifestPath,'utf8'));evalConfig.candidate=evalSkill.id;
  writeFileSync(manifestPath,JSON.stringify(evalConfig));
  await commands.harness('eval prepare eval/suite.json',ctx);
  assert.match(widgets['evolving-harness'].join('\n'),/通用评测完整载荷/);
  ctx.ui.confirm=async()=>false;
  await commands.harness('eval run',ctx);
  const preparedEval=await bridge({operation:'product-eval-preview'});
  assert.equal(preparedEval.evaluation_preview.pairs.length,2);
  assert.ok(preparedEval.evaluation_preview.pairs.every(p=>p.evaluation_pair_preview.status==='ready'));
  await commands.harness('eval report',ctx);
  assert.match(widgets['evolving-harness'].join('\n'),/HTML报告/);
  console.log('PASS: native extension + Python bridge, context, tools, feedback, reset, deletion, abort, eval prepare/decline/report; zero model calls');
} finally { rmSync(temporary, { recursive:true, force:true }); }
