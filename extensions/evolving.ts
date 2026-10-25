import { spawn } from "node:child_process";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// Payload goes over stdin, never interpolated into a shell command.
export function bridge(request: object): Promise<any> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.EH_PYTHON || "python", ["-m", "evolving_harness.native"], {
      cwd: process.env.EH_HOME, env: process.env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "", stderr = "";
    const timer = setTimeout(() => { child.kill(); reject(new Error("Harness bridge timeout")); }, 320000);
    child.stdout.on("data", data => { stdout += data; });
    child.stderr.on("data", data => { stderr = (stderr + data).slice(-4000); });
    child.on("error", error => { clearTimeout(timer); reject(error); });
    child.stdin.on("error", () => {});
    child.on("close", () => {
      clearTimeout(timer);
      try {
        const response = JSON.parse(stdout);
        if (!response.ok) throw new Error(response.error);
        resolve(response.result);
      } catch (error) { reject(new Error(`Harness: ${stdout || stderr || error}`)); }
    });
    child.stdin.end(JSON.stringify(request));
  });
}

export default function evolving(pi: ExtensionAPI) {
  let task: string | undefined;
  let lastTask: string | undefined;
  let finalText = "";
  let failed = false;
  const toolStarts = new Map<string, number>();
  let queue: Promise<unknown> = Promise.resolve();
  // Serialize observations even when Pi executes sibling tools concurrently.
  const call = (request: object): Promise<any> => {
    const result = queue.then(() => bridge(request));
    queue = result.catch(() => {});
    return result;
  };
  pi.registerTool?.({
    name:'harness_recipe',label:'Harness tool recipe',
    description:'Execute an installed versioned Harness tool recipe by name, after explicit user confirmation. Available recipes appear in task context.',
    parameters:{type:'object',properties:{name:{type:'string',description:'Installed name such as tool:tests'}},required:['name'],additionalProperties:false} as any,
    async execute(_id:any,params:any,signal:any,_update:any,ctx:any) {
      if (signal?.aborted) throw new Error('Tool execution cancelled');
      const preview=await call({operation:'tool-preview',name:params.name,workspace:ctx.cwd});
      if (!await ctx.ui.confirm('Agent 请求执行工具配方',JSON.stringify(preview.tool_preview)+'\n运行用户权限；确认后执行。'))
        return {content:[{type:'text',text:'User declined tool execution; no command ran.'}],details:{declined:true}};
      if (signal?.aborted) throw new Error('Tool execution cancelled');
      const result=await call({operation:'tool-run',name:params.name,workspace:ctx.cwd,confirmed:true,packet_hash:preview.packet_hash});
      return {content:[{type:'text',text:JSON.stringify(result.tool_result)}],details:result};
    },
  });
  const show = (ctx: ExtensionContext, title: string, value: unknown) => {
    const v: any = value;
    let lines: string[];
    if (v.evaluation_preview) {
      const p=v.evaluation_preview;
      lines=[`${p.name} · ${p.id} · ${p.pairs.length} 个任务`,
        `基线 ${p.baseline ? p.baseline.name+' v'+p.baseline.version : '不注入Skill'} → 候选 ${p.candidate.name} v${p.candidate.version}`,
        ...p.pairs.map((x:any)=>`${x.evaluation_pair_preview.case_id}: ${x.evaluation_pair_preview.status} · ${Object.keys(x.evaluation_pair_preview.files).length} 个源文件`),
        '完整载荷包含源码、指令、记忆、Skill及可信Python验收代码；先检查报告。准备不会调用模型。'];
    } else if (v.evaluation_report) {
      const p=v.evaluation_report;
      const decisions:any={pending:'待完成',keep_current:'保留当前，未证明收益',do_not_promote:'拒绝晋升',insufficient_evidence:'证据不足',review_candidate:'存在改善，待人工复核'};
      const outcomes:any={ready:'待运行',running:'运行中',tie_on_this_task:'本任务持平',improved_on_this_task:'本任务改善',worse_on_this_task:'本任务退步',both_failed:'两组未通过',inconclusive:'无结论'};
      lines=[`${p.name} · ${p.complete ? '已结束' : '未全部结束'} · ${decisions[p.recommendation] || p.recommendation}`,
        ...p.cases.map((c:any)=>`${c.case_id}: ${outcomes[c.outcome] || c.outcome} · 基线 ${c.arms.baseline?.reference.status || '未运行'} / 候选 ${c.arms.candidate?.reference.status || '未运行'}`),
        ...p.cases.flatMap((c:any)=>Object.entries(c.arms).flatMap(([arm,r]:[string,any])=>
          (r.reference.violations || []).map((v:any)=>`${c.case_id} · ${arm==='baseline' ? '基线' : '候选'}：${v.path} · ${v.reason}${v.detail ? ' · '+v.detail : ''}`))),
        `HTML报告：${v.report_html}`,`JSON证据：${v.report_json}`,
        '失败和平局保留；不自动重试、不自动启用，小样本不证明泛化收益。'];
    } else if (v.skill_comparison_preview) {
      const p=v.skill_comparison_preview;
      lines=[`对照 ${p.id} · 固定同一个模块拆分任务`,
        `Skill ${p.skill.name} v${p.skill.version} · 源文件 ${Object.keys(p.files).length} 个`,
        '同一模型、两份代码副本、相同偏好和记忆；只切换这一条Skill。',
        '完整报告包含将提供给模型的代码、任务与Skill，请先检查。'];
    } else if (v.skill_comparison_result) {
      const p=v.skill_comparison_result;
      lines=[`对照 ${p.id} · ${p.comparison?.outcome || p.status}`,
        ...Object.entries(p.results).map(([name,r]:[string,any])=>`${name}: 独立验收 ${r.reference.status} · 工具 ${r.metrics.tool_calls} 次 · 错误 ${r.metrics.tool_errors} · ${r.metrics.elapsed_seconds}秒 · 隔离检查 ${r.metrics.isolated_check_observed ? '已观察到' : '未观察到'}`),
        `原项目源文件检查：${p.original_source_unchanged === undefined ? '尚未完成' : p.original_source_unchanged ? '未变化' : '发生变化，本次不作有效对照'}`,
        '单次对照允许平局，检查更多或调用更少不直接算能力提升。'];
    } else if (v.improvement_batch) {
      const b=v.improvement_batch;
      const labels:any={no_change:'保持现状',ready:'已评估，等待确认',rejected:'未通过门槛',awaiting_followup:'已启用，跟踪后续使用'};
      lines=[`${b.id} · ${labels[b.status] || b.status}`,`检查任务 ${b.tasks_checked} · 新线索 ${b.new_feedback}`,
        `基线 v${b.baseline_version}${b.candidate_version ? ' → 候选 v'+b.candidate_version : ''}`,
        b.reason,...(b.evaluation ? [`固定门槛 ${b.evaluation.decision} · ${b.evaluation.baseline_correct} → ${b.evaluation.candidate_correct}`] : []),
        ...(b.followup || []).map((f:any)=>`${f.kind==='task_context' ? '后续任务配置已加载' : '后续模型复盘已使用'}：${f.reference}`),
        '版本使用不等于收益提升；本命令不调用外部模型。'];
    } else if (v.component_failure) {
      const f=v.component_failure;
      const labels:any={empty_response:'模型未返回可见正文',invalid_json:'回复不是合法JSON',invalid_candidate:'候选未通过结构或证据检查',model_error:'模型或连接错误',unchanged:'没有新增方案，保持现状',no_record:'尚无诊断记录'};
      lines=[labels[f.status] || f.status,`目标 ${f.target || '未知'} · 停止原因 ${f.stop_reason || '未提供'}`,
        f.error,'没有启用新策略，也没有自动重试。可用 package last-error 查看最近诊断；完整报告含脱敏后的可见回复。'];
    } else if (v.benchmark_preview) {
      lines=[`策略模型对照 · ${v.benchmark_preview.requests} 次请求 · 上限 ${v.benchmark_preview.limit_seconds} 秒`,
        `基线 v${v.benchmark_preview.baseline.version} / 候选 v${v.benchmark_preview.candidate.version}`,
        '只发送固定模拟案例和两版策略，不发送真实项目文件；先检查报告再确认。'];
    } else if (v.benchmark_result) {
      lines=[`对照结果 ${v.benchmark_result.outcome}`,`正确案例 ${JSON.stringify(v.benchmark_result.correct)}`,
        '小样本、简单内容检查；允许平局/失败，不自动启用，不能证明泛化收益。'];
    } else if (v.product) {
      lines = [`用户 ${v.product.user} · 项目 ${v.product.project}`,
        ...v.product.active.map((c:any)=>`${c.name} v${c.version} · ${c.id} · 生成器 ${c.generator || '初始/人工'}`),
        `用户偏好 v${v.product.profile.version} · ${v.product.profile.payload.preferences.length} 条`,
        '入口：strategy / package / tool / experiments；详细版本与评估见报告'];
    } else if (v.component) {
      const c=v.component;
      lines=[`${c.name} v${c.version} · ${c.id}`,`父版本 ${c.parent || '无'} · 生成器 ${c.generator || '人工/初始'}`,
        `理由：${c.reason}`,JSON.stringify(c.payload),'创建与启用是不同操作；活动版本以 /harness center 为准。'];
    } else if (v.component_evaluation) {
      const e=v.component_evaluation;
      lines=[`候选 ${e.candidate} · ${e.decision} · ${e.evaluation_id}`,JSON.stringify(e),
        '固定门槛/契约检查不是实际编程收益证明；启用仍需确认。'];
    } else if (v.component_preview) {
      lines=[`改进目标 ${v.component_preview.target.name} · 生成器 v${v.component_preview.generator.version}`,
        `待发送证据 ${v.component_preview.evidence.length} 项，可能含源码；先检查完整报告再确认。`];
    } else if (v.strategy) {
      lines=[`复盘策略 v${v.strategy.active.version} · ${v.strategy.active.id}`,
        `检查 ${v.strategy.active.payload.checks.join(',') || '基础固定指令'}`,
        `反馈 ${v.strategy.feedback.length} 条；feedback scope/expiry/duplicate/test_count 理由 → propose → package evaluate/activate`];
    } else if (v.recursive_demo) {
      lines=['独立临时数据库递归验收（无模型调用）',...v.recursive_demo.rounds.map((r:any)=>
        `v${r.version} · ${r.id} · 由 ${r.generator} 生成 · 门槛 ${r.decision}`),v.recursive_demo.notice];
    } else if (v.tool_preview) {
      lines=[`工具 ${v.tool_preview.name} · ${v.tool_preview.component}`,`目录 ${v.tool_preview.workspace}`,
        `执行 ${JSON.stringify(v.tool_preview.command) || v.tool_preview.recipe}`,
        '测试脚本运行用户权限，副本不是操作系统沙盒。'];
    } else if (v.revision_preview) {
      lines = [`记忆 ${v.revision_preview.memory.id} · 修订 ${v.revision_preview.memory.revision}`,
        `待发送证据 ${v.revision_preview.evidence.length} 项；包含记忆文本、工具参数/输出、测试及反馈摘录。`,
        '不含完整源码快照或隐藏推理；摘录仍可能含代码与项目数据，请先检查完整预览再确认。'];
    } else if (v.revision_candidate) {
      const c = v.revision_candidate;
      lines = [`${c.id} · 候选修订 ${c.revision} · ${c.status}`,
        `记忆 ${c.memory_id} · 原版本 ${c.before.revision} · ${c.memory_project}`,
        `建议：${{keep:'保留',revise:'修改',retire:'停用'}[c.action as string] || c.action}`,
        `原内容：${c.before.content}`, `候选内容：${c.content}`, `理由：${c.reason}`,
        '人工确认不等于实验验证；保留不会增加记忆版本。完整证据与历史见报告。'];
    } else if (v.revision_candidates) {
      lines = v.revision_candidates.map((c:any) => `${c.id} · ${c.status} · ${c.action} · ${c.memory_id}`);
      if (!lines.length) lines.push('尚无修订候选');
    } else if (v.memory_review) {
      lines = [`项目 ${v.memory_review.project} · 本地证据评审（无模型调用）`,
        ...v.memory_review.items.flatMap((i:any) => [
          `${i.memory.id} · 修订 ${i.memory.revision} · ${i.source === 'latest_linked_lab' ? '最近关联实验项目' : '当前项目'}`,
          i.memory.content, `注入任务 ${i.usage.length} 次 · ${{insufficient_evidence:'证据不足',no_demonstrated_gain:'尚未证明额外收益',needs_further_review:'需进一步评审'}[i.assessment as string] || i.assessment}`,
          ...Object.entries(i.pair || {}).map(([name,s]:[string,any]) =>
            `${name}: 注入 ${s.injected ? '是' : '否'} · 工具 ${JSON.stringify(s.tool_counts)} · 独立测试 ${s.verification.status} (${s.verification.test_count ?? '?'})`),
          i.recommendation]),
        ...(v.memory_review.items.length ? [] : ['当前没有可评审记忆']),
        v.memory_review.limitations];
    } else if (v.lab_preview) {
      lines = [`跨任务验收 · ${v.lab_preview.stage} · 独立工作副本`,
        `任务：${v.lab_preview.task}`,`注入记忆：${v.lab_preview.memory?.id || '无'}`,
        '模型可读取/修改演示源码、执行真实Shell；先检查完整载荷再确认'];
    } else if (v.lab) {
      lines = [`跨任务验收 ${v.lab.id}`,
        ...Object.entries(v.lab.stages).map(([name,s]:[string,any]) => `${name}: ${s.status} · 参考测试 ${s.verification?.status || '尚未执行'}`),
        v.candidate ? `候选 ${v.candidate.id} · ${v.candidate.status}: ${v.candidate.content}` :
          `记忆 ${v.lab.memory_id || '未确认'} · 隔离 ${v.isolation?.status || '未检查'} · 对照 ${v.comparison?.outcome || '未运行'}`,
        `下一组：${v.next_stage || '见验收步骤'}；完整记录包含任务证据和源码指纹`];
    } else if (v.experiment_preview) {
      lines = ['记忆对照实验：同模型、独立会话、一组不注入/一组注入',
        `记忆 ${v.experiment_preview.memory.id} 修订 ${v.experiment_preview.memory.revision}`,
        '发送固定模拟任务、coding指导及这一条记忆；不发送真实项目源码',
        '两组均使用模拟故障工具，不执行真实命令；结果允许无收益'];
    } else if (v.comparison && v.results) {
      lines = [`实验结果：${v.comparison.outcome}`,
        ...v.results.map((r:any)=>`${r.arm}: ${r.status} · 重复调用 ${r.metrics.repeated_shell_attempts} · 如实结构化报告 ${r.metrics.truthful_structured_report}`),
        '仅一次模拟场景对照，不能证明泛化收益或递归自进化'];
    } else if (v.candidates) {
      lines = [`${v.backend || '已保存'} · 候选 ${v.candidates.length} 条（不会自动启用）`,
        ...v.candidates.slice(0,3).flatMap((c:any) => [`${c.id} · ${c.status} · ${c.basis}`,
          `${c.content.slice(0,160)} · 证据 ${c.evidence_ids.map((id:number)=>`E${id}`).join(',')}`])];
    } else if (v.candidate) {
      const c = v.candidate;
      lines = [`${c.id} · ${c.status} · 修订 ${c.revision} · ${c.kind} · ${c.basis}`,
        c.content, `依据 ${c.evidence_ids.map((id:number)=>`E${id}`).join(',')}`,
        c.memory_id ? `已创建项目记忆 ${c.memory_id}（人工确认，不代表独立评测）` :
          c.kind==='workflow' ? '工作流接受仅表示审阅，不自动晋升 Skill' : '待审内容不会注入任务；接受后成为人工确认的项目记忆'];
    } else if (v.packet) {
      lines = [`任务 ${v.packet.task_id} · 待发送证据 ${v.packet.evidence.length} 项`,
        '含任务目标、工具输出摘要、测试状态和用户反馈；无完整源码快照',
        '先检查完整预览文件，再确认模型调用'];
    } else if (v.task && v.summary) {
      lines = [`任务 ${v.task.id} · ${v.task.state}`,
        `工具完成 ${v.summary.tool_records} 条记录 · 错误 ${v.summary.tool_errors}`,
        `独立执行测试：${v.summary.latest_verification} · 代码匹配 ${v.summary.verification_freshness}`,
        `修改文件：${v.summary.changed_files.join(', ') || '未记录到差异'}`,
        '工具成功不等于测试通过；完整证据按 evidence_id 引用'];
    } else if (v.versions) {
      lines = [`用户 ${v.user} · 项目 ${v.project} · 记忆 ${v.memories}`,
        ...v.versions.filter((a: any) => v.active[a.name] === a.id).map((a: any) =>
          `${a.name} v${a.version} · ${a.id} · 生成器 ${a.generated_by || "seed"}`)];
    } else if (Array.isArray(v.memories)) {
      lines = v.memories.slice(0, 4).map((m: any) => `${m.id} [${m.scope}] ${m.content.slice(0, 100)}`);
      if (!lines.length) lines.push("当前没有记忆；/memory add 内容 保存项目约定");
      if (v.memories.length > 4) lines.push(`共 ${v.memories.length} 条，完整内容见文件`);
    } else if (v.rounds) {
      lines = v.rounds.flatMap((r: any) => r.changes.map((c: any) =>
        `第${r.round}轮 ${c.target} v${c.version}: ${c.decision} · 启用 ${c.applied} · 生成器 ${c.generated_by}`));
    } else if (v.evidence) {
      lines = [`反馈/尝试摘要 ${v.evidence.length} 项；完整待发送策略与反馈见文件`,
        ...v.evidence.slice(-2).map((e: any) => `${e.kind}: ${e.data.note || e.data.outcome}`)];
    } else lines = [JSON.stringify(value)];
    if (v.report_path) lines.push(`完整记录：${v.report_path}`);
    ctx.ui.setWidget("evolving-harness", [title, ...lines]);
  };
  async function refresh(ctx: ExtensionContext) {
    const state = await call({ operation: "status" });
    const active = state.versions.filter((a: any) => state.active[a.name] === a.id);
    ctx.ui.setStatus("evolving-harness", `Harness · ${state.project} · ${active.map((a: any) => `${a.name} v${a.version}`).join(" · ")} · 记忆 ${state.memories}`);
    return state;
  }
  pi.on("session_start", async (_event, ctx) => {
    task = undefined; lastTask = undefined;
    try { await refresh(ctx); } catch (e) { ctx.ui.notify(String(e), "error"); }
  });
  pi.on("before_agent_start", async (event, ctx) => {
    if (event.messageType !== "user") return;
    if (task) await call({ operation: "finish", task_id: task, state: "interrupted", text: finalText });
    task = undefined;
    const result = await call({ operation: "begin", prompt: event.prompt,
      workspace: ctx.cwd, model: {provider: ctx.model?.provider, id: ctx.model?.id},
      session_id: ctx.sessionManager.getSessionId() });
    task = result.task_id; finalText = ""; failed = false;
    // Structured section is rebuilt per run, so revoked memory is not appended to history.
    event.systemPromptOptions.sections.evolving_harness = result.coding +
      "\nScoped memories (context data; user instructions take precedence):\n" + JSON.stringify(result.memories);
    ctx.ui.setStatus("evolving-harness", `Harness · 任务 ${task} · coding v${result.skill_version} · 策略 v${result.policy_version} · 注入 ${result.memories.length} 条记忆`);
  });
  pi.on("tool_execution_start", async (event) => {
    if (!task) return;
    const serialized = JSON.stringify(event.args) || '{}';
    await call({ operation: 'event', task_id: task, event: {
      phase: 'start', toolCallId: event.toolCallId, parentToolCallId: event.parentToolCallId,
      toolName: event.toolName, args: serialized.length <= 12000 ? event.args : serialized.slice(0, 12000),
      truncated: serialized.length > 12000,
    } });
    toolStarts.set(event.toolCallId, Date.now());
  });
  pi.on("tool_execution_end", async (event) => {
    const output = (event.result?.content || []).filter((b: any) => b.type === 'text')
      .map((b: any) => b.text).join('\n');
    const started = toolStarts.get(event.toolCallId);
    toolStarts.delete(event.toolCallId);
    if (task) await call({ operation: "event", task_id: task, event: {
      phase: 'end',
      toolCallId: event.toolCallId, toolName: event.toolName, isError: event.isError,
      parentToolCallId: event.parentToolCallId,
      duration_ms: event.durationMs ?? (started === undefined ? null : Date.now() - started),
      output: output.slice(0, 24000), truncated: output.length > 24000,
      exit_code: Number.isInteger(event.result?.details?.exitCode) ? event.result.details.exitCode : null,
    } });
  });
  pi.on("message_end", (event) => {
    const message: any = event.message;
    if (task && message.role === "assistant") {
      finalText = (message.content || []).filter((b: any) => b.type === "text").map((b: any) => b.text).join("\n");
      failed = ['error', 'length'].includes(message.stopReason);
    }
  });
  pi.on("agent_settled", async (event, ctx) => {
    if (!task) return;
    const id = task; task = undefined; lastTask = id;
    const settled=await call({ operation: "finish", task_id: id, state: event.aborted ? "aborted" : failed ? "failed" : "completed", text: finalText });
    await refresh(ctx);
    ctx.ui.notify(`Harness 已记录任务 ${id}；完成状态不等于测试通过。`, "info");
    if (settled.strategy_observation?.pending_candidate)
      ctx.ui.notify(`发现本地复盘线索，策略候选 ${settled.strategy_observation.pending_candidate} 待评估/确认；不会自动启用。`,'info');
  });
  pi.on("session_shutdown", async () => {
    if (task) {
      const id = task; task = undefined;
      await call({ operation: "finish", task_id: id, state: "interrupted", text: finalText });
    }
    await queue;
  });
  pi.registerCommand("harness", {
    description: "状态；memory-review [ID] 记忆效果预览；task/test；feedback/audit",
    handler: async (args, ctx) => {
      args = args.trim();
      if (args.startsWith('eval ') || args === 'eval') {
        if (!ctx.isIdle()) {ctx.ui.notify('请等当前任务结束','warning');return;}
        if (args.startsWith('eval prepare ')) {
          show(ctx,'通用评测完整载荷',await call({operation:'product-eval-prepare',workspace:ctx.cwd,manifest:args.slice('eval prepare '.length).trim()}));
        } else if (args==='eval status' || args==='eval report') {
          show(ctx,'通用评测报告',await call({operation:'product-eval-report'}));
        } else if (args==='eval run') {
          if (!ctx.model) {ctx.ui.notify('请先配置模型','warning');return;}
          const selected={provider:ctx.model.provider,model:ctx.model.id};
          const preview=await call({operation:'product-eval-preview'});show(ctx,'确认前检查完整评测载荷',preview);
          const pending=preview.evaluation_preview.pairs.filter((p:any)=>p.evaluation_pair_preview.status==='ready');
          if (!pending.length) {show(ctx,'已有评测报告',await call({operation:'product-eval-report'}));return;}
          if (!await ctx.ui.confirm('执行通用任务评测',
            `向 ${selected.provider}/${selected.model} 发送报告中的任务源码、相同偏好/记忆及两版Skill。待执行 ${pending.length} 个任务，每任务两组独立会话，最多约5分钟，允许多轮模型和工具调用。固定Python验收代码也会执行。真实Shell运行用户权限，不是OS沙盒。请先检查完整载荷，确认整批执行？`)) return;
          for (const p of pending) {
            ctx.ui.notify(`正在评测 ${p.evaluation_pair_preview.case_id}；失败与平局均保存。`,'info');
            show(ctx,'任务评测进度',await call({operation:'product-eval-run-case',suite_id:preview.evaluation_preview.id,
              pair_id:p.evaluation_pair_preview.id,packet_hash:p.packet_hash,confirmed:true,
              pi_root:process.env.EH_PI_ROOT,...selected}));
          }
        } else ctx.ui.notify('eval prepare 相对配置路径 / eval run / eval status / eval report','info');
      } else if (args === 'skill-compare' || args === 'skill-compare status') {
        if (!ctx.isIdle()) {ctx.ui.notify('请等当前任务结束','warning');return;}
        if (args.endsWith('status')) {show(ctx,'Skill对照记录',await call({operation:'product-skill-compare-status'}));return;}
        if (!ctx.model) {ctx.ui.notify('请先配置模型','warning');return;}
        const preview=await call({operation:'product-skill-compare-setup',workspace:ctx.cwd});show(ctx,'Skill对照完整载荷',preview);
        if (!await ctx.ui.confirm('运行两组真实编程对照',
          `向 ${ctx.model.provider}/${ctx.model.id} 提供报告中的项目源码、相同任务/记忆/偏好，其中一组多一条Skill。两组独立会话，随机执行顺序，可多轮调用；每组模型阶段上限115秒，加独立检查，合计最多约5分钟。可执行真实Shell，运行用户权限，目录副本不是OS沙盒。请先检查完整报告，确认两组运行？`)) return;
        ctx.ui.notify('开始两组对照；不自动重试或启用改进，失败和平局都保存。','info');
        show(ctx,'Skill对照结果',await call({operation:'product-skill-compare-run',packet_hash:preview.packet_hash,
          confirmed:true,pi_root:process.env.EH_PI_ROOT,provider:ctx.model.provider,model:ctx.model.id}));
      } else if (args === 'improve' || args === 'improve status') {
        if (!ctx.isIdle()) {ctx.ui.notify('请先等待当前任务结束','warning');return;}
        const result=await call({operation:args.endsWith('status') ? 'product-improve-status' : 'product-improve-plan'});
        show(ctx,'批量改进',result);
        const b=result.improvement_batch;
        if (args.endsWith('status') || b.status!=='ready') return;
        if (!await ctx.ui.confirm('启用本批已评估策略并开始新会话',
          `v${b.baseline_version} → v${b.candidate_version}\n${JSON.stringify(b.payload)}\n依据 ${b.evidence_ids.join(',')}\n固定案例通过不代表真实收益。继续？`)) return;
        await ctx.newSession({withSession:async newCtx=>{
          show(newCtx,'批量改进已启用',await bridge({operation:'product-improve-activate',id:b.id,confirmed:true}));
        }});return;
      } else if (args === 'center') {
        show(ctx,'Harness 控制台',await call({operation:'product-status'}));
      } else if (args.startsWith('benchmark ')) {
        if (!ctx.isIdle() || !ctx.model) {ctx.ui.notify('请等空闲并配置模型','warning');return;}
        const id=args.slice(10).trim();
        const preview=await call({operation:'product-benchmark-preview',id});show(ctx,'策略对照载荷',preview);
        if (!await ctx.ui.confirm('运行六次模型对照请求',`向 ${ctx.model.provider}/${ctx.model.id} 发送报告中的固定模拟案例与两版策略，六个独立会话，每次约30秒、总计上限240秒，不自动重试或启用版本。继续？`)) return;
        ctx.ui.notify('开始独立会话对照，最多约4分钟；失败和平局都会保留。','info');
        show(ctx,'策略模型对照',await call({operation:'product-benchmark-run',id,packet_hash:preview.packet_hash,
          confirmed:true,pi_root:process.env.EH_PI_ROOT,provider:ctx.model.provider,model:ctx.model.id}));
      } else if (args === 'profile') {
        if (!ctx.isIdle()) {ctx.ui.notify('请先等待任务停止','warning');return;}
        const center=await call({operation:'product-status'});
        const current=center.product.profile;
        const text=await ctx.ui.editor('你的协作偏好：每行一条；适用于你的各项目',current.payload.preferences.join('\n'));
        if (text===undefined) return;
        const payload={kind:'profile',preferences:text.split('\n').map(s=>s.trim()).filter(Boolean)};
        const {component}=await call({operation:'component-stage',user_scope:true,
          draft:{name:'profile',payload,reason:'用户在偏好编辑器中明确设置',evidence_ids:[]}});
        const {component_evaluation:e}=await call({operation:'component-evaluate',id:component.id,user_scope:true});
        if (e.decision!=='accept') {show(ctx,'偏好无需更换或未通过检查',{component_evaluation:e});return;}
        if (!await ctx.ui.confirm('保存偏好并开始新会话',payload.preferences.join('\n')+'\n只影响当前用户；不会更改其他用户偏好。继续？')) return;
        await ctx.newSession({withSession:async newCtx=>{
          show(newCtx,'用户偏好已更新',await bridge({operation:'component-activate',id:component.id,
            evaluation_id:e.evaluation_id,user_scope:true,confirmed:true}));
        }});return;
      } else if (args === 'experiments') {
        show(ctx,'改进实验与优先级',await call({operation:'experiments-status'}));
      } else if (args === 'demo recursive') {
        show(ctx,'递归链路验收',await call({operation:'product-demo'}));
      } else if (args === 'help') {
        ctx.ui.setWidget('evolving-harness',['Harness 功能入口',
          'improve：整批采集/候选/评估，只确认一次；improve status：跟踪后续版本使用',
          'eval prepare 配置路径 / eval run / eval status / eval report：通用Python任务对照',
          'center：活动版本与历史；memory-review：记忆评审/候选',
          'strategy feedback 检查名 理由 / strategy propose：策略自身改进候选',
          'package edit 名称：JSON编辑（reviewer/context/profile/skill:名称/tool:名称）',
          'package evaluate ID / activate ID 评估ID / rollback 名称：验证、启用、回退',
          'package model 名称：预览并确认模型提出组件改进',
          'profile：每行设置一条用户偏好；package show ID / export ID',
          'benchmark 复盘策略组件ID：预览并确认旧版/候选的模型对照',
          'skill from 经验ID 名称 触发词：将已审阅工作流转为Skill候选',
          'tool run tool:名称：预览并确认工具执行；experiments：实验优先级',
          'demo recursive：无模型的独立递归验收；task/test/note/lab：任务证据和对照']);
      } else if (args === 'strategy' || args.startsWith('strategy ')) {
        const [,verb,check,...note]=args.split(/\s+/);
        if (verb==='feedback') show(ctx,'复盘策略反馈',await call({operation:'strategy-feedback',check,note:note.join(' ')}));
        else if (verb==='propose') show(ctx,'复盘策略后继候选',await call({operation:'strategy-propose'}));
        else show(ctx,'复盘策略',await call({operation:'strategy-preview'}));
      } else if (args.startsWith('package ')) {
        if (!ctx.isIdle()) {ctx.ui.notify('请先等待任务停止','warning');return;}
        const [,verb,key,evalId,...extra]=args.split(/\s+/);
        const user_scope=key==='profile' || evalId==='user' || extra.includes('user');
        if (verb==='last-error') {
          show(ctx,'最近模型提案诊断',await call({operation:'component-last-error'}));
        } else if (verb==='edit') {
          const template=await call({operation:'component-template',name:key,user_scope});
          const text=await ctx.ui.editor('编辑候选JSON；保留字段并填写理由',JSON.stringify(template.component_template,null,2));
          if (text===undefined) return;
          show(ctx,'组件候选（尚未启用）',await call({operation:'component-stage',draft:JSON.parse(text),user_scope}));
        } else if (verb==='show') show(ctx,'组件详情',await call({operation:'component-get',id:key,user_scope}));
        else if (verb==='evaluate') show(ctx,'独立评估',await call({operation:'component-evaluate',id:key,user_scope}));
        else if (verb==='export') show(ctx,'导出原生Skill',await call({operation:'component-export',id:key}));
        else if (verb==='activate' || verb==='rollback' || verb==='disable') {
          const info=verb==='activate' ? await call({operation:'component-get',id:key,user_scope}) : await call({operation:'component-template',name:key,user_scope});
          show(ctx,'确认前检查组件',info);
          if (!await ctx.ui.confirm('确认更换版本并开始新会话',JSON.stringify(info)+'\n影响后续任务；契约检查不代表收益已验证。继续？')) return;
          const request=verb==='activate' ? {operation:'component-activate',id:key,evaluation_id:evalId,user_scope,confirmed:true} :
            {operation:verb==='disable' ? 'component-disable' : 'component-rollback',name:key,user_scope,confirmed:true};
          await ctx.newSession({withSession:async newCtx=>{
            const result=await bridge(request);show(newCtx,'组件版本已更换',result);
          }});return;
        } else if (verb==='model') {
          if (!ctx.model) {ctx.ui.notify('请先配置模型','warning');return;}
          const preview=await call({operation:'component-preview',name:key});show(ctx,'组件模型载荷预览',preview);
          if (!await ctx.ui.confirm('模型提出组件候选',`向 ${ctx.model.provider}/${ctx.model.id} 发送预览中的组件、策略和证据（可能含代码）。一次无工具请求，约130秒；不自动启用。继续？`)) return;
          show(ctx,'模型组件候选',await call({operation:'component-model',name:key,packet_hash:preview.packet_hash,
            confirmed:true,pi_root:process.env.EH_PI_ROOT,provider:ctx.model.provider,model:ctx.model.id}));
        } else ctx.ui.notify('package edit/show/evaluate/activate/rollback/export/model 参数','warning');
      } else if (args.startsWith('skill from ')) {
        const [,,id,name,trigger]=args.split(/\s+/);
        show(ctx,'Skill候选',await call({operation:'skill-from-experience',id,name:'skill:'+name,triggers:[trigger]}));
      } else if (args.startsWith('tool run ')) {
        if (!ctx.isIdle()) {ctx.ui.notify('请等待任务停止','warning');return;}
        const name=args.slice(9).trim();
        const preview=await call({operation:'tool-preview',name,workspace:ctx.cwd});show(ctx,'工具执行预览',preview);
        if (!await ctx.ui.confirm('执行已启用工具配方',JSON.stringify(preview.tool_preview)+'\n可能执行本地项目代码，运行用户权限。继续？')) return;
        show(ctx,'工具执行结果',await call({operation:'tool-run',name,workspace:ctx.cwd,confirmed:true,packet_hash:preview.packet_hash}));
      } else if (args === 'memory-review' || args.startsWith('memory-review ')) {
        const parts = args.slice(13).trim().split(/\s+/);
        const [verb,id] = parts;
        const usage = '用法：memory-review [记忆ID] | propose 记忆ID | list | show/edit/retire/accept/reject 候选ID';
        if (verb === 'model') {
          if (!id || parts.length !== 2) {ctx.ui.notify('用法：memory-review model 记忆ID','warning');return;}
          if (!ctx.isIdle() || !ctx.model) {ctx.ui.notify('请等待空闲并选择模型','warning');return;}
          const preview = await call({operation:'memory-revision-preview',memory_id:id});
          show(ctx,'模型修订载荷预览',preview);
          if (!preview.revision_preview.evidence.length) {ctx.ui.notify('缺少任务证据，请先采集轨迹','warning');return;}
          if (!await ctx.ui.confirm('调用模型提出待审修订',
            `向 ${ctx.model.provider}/${ctx.model.id} 发送预览中的记忆和任务证据摘录，可能包含代码和项目数据。请先检查完整报告。一次无工具请求，约130秒上限，不自动重试；候选不会自动生效。继续？`)) return;
          show(ctx,'模型修订候选（待确认）',await call({operation:'memory-revision-model',memory_id:id,
            packet_hash:preview.packet_hash,confirmed:true,pi_root:process.env.EH_PI_ROOT,
            provider:ctx.model.provider,model:ctx.model.id}));
        } else if (verb === 'list') {
          show(ctx,'修订候选',await call({operation:'memory-revision-list'}));
        } else if (['propose','show','edit','retire','accept','reject','rollback'].includes(verb)) {
          if (!id || parts.length !== 2) {ctx.ui.notify(usage,'warning');return;}
          if (!ctx.isIdle()) {ctx.ui.notify('请等当前任务停止后管理修订候选','warning');return;}
          if (verb === 'propose') {
            show(ctx,'修订候选（尚未生效）',await call({operation:'memory-revision-propose',memory_id:id}));return;
          }
          const result = await call({operation:'memory-revision-get',id});
          show(ctx,'修订候选',result);
          const c = result.revision_candidate;
          if (verb === 'show') return;
          if (verb === 'rollback') {
            if (!await ctx.ui.confirm('回退此记忆修订并开始新会话',`恢复为：${c.before.content}\n后续版本已变化时会拒绝回退。继续？`)) return;
            await ctx.newSession({withSession:async newCtx=>{
              show(newCtx,'记忆已回退',await bridge({operation:'memory-revision-rollback',id,confirmed:true}));
            }});return;
          }
          if (verb === 'edit' || verb === 'retire') {
            const content = verb === 'edit' ? await ctx.ui.editor('修改候选内容',c.content) : c.content;
            if (content === undefined) return;
            const reason = await ctx.ui.editor('填写修改或停用理由（用户判断，非自动评测）','');
            if (!reason?.trim()) return;
            show(ctx,'已修改待审候选',await call({operation:'memory-revision-edit',id,revision:c.revision,
              action:verb === 'edit' ? 'revise' : 'retire',content,reason}));return;
          }
          const accept = verb === 'accept';
          if (!await ctx.ui.confirm(accept ? '确认接受此候选' : '确认拒绝此候选',
            `${c.memory_id}\n动作 ${c.action}\n${c.content}\n理由 ${c.reason}\n修改/停用会开始新会话；人工确认不代表收益已验证。`)) return;
          const request = {operation:'memory-revision-decide',id,revision:c.revision,accept,confirmed:true};
          if (accept && c.action !== 'keep') {
            await ctx.newSession({withSession:async (newCtx) => {
              const decided = await bridge(request);
              show(newCtx, '修订已确认；新任务按当前记忆检索', decided);
            }});
          } else show(ctx,'修订决定',await call(request));
        } else if (!verb || (verb.startsWith('memory_') && parts.length === 1)) {
          show(ctx, '记忆效果评审', await call({operation:'memory-review', memory_id:verb || undefined}));
        } else ctx.ui.notify(usage,'warning');
      } else if (args.startsWith("feedback ")) {
        const [, defect, ...note] = args.split(" ");
        await call({ operation: "feedback", defect, note: note.join(" "), task_id: lastTask });
        ctx.ui.notify("反馈已保存", "info");
      } else if (args === 'lab' || args.startsWith('lab ')) {
        const action=args.slice(3).trim() || 'status';
        if (['setup','status','review'].includes(action)) {
          show(ctx,'跨任务验收',await call({operation:`lab-${action}`}));return;
        }
        if (action==='edit' || action==='accept') {
          const result=await call({operation:'lab-candidate'});show(ctx,'跨任务记忆候选',result);
          const c=result.candidate;
          if (action==='edit') {
            const content=await ctx.ui.editor('编辑跨任务项目记忆',c.content);
            if (content!==undefined) show(ctx,'已修改候选',await call({operation:'lab-edit',content,revision:c.revision}));
          } else if (await ctx.ui.confirm('确认仅用于实验主项目的记忆',
            `${c.content}\n依据 ${c.evidence_ids.join(',')}；不会写入当前真实项目或其他项目。`)) {
            show(ctx,'已确认跨任务记忆',await call({operation:'lab-accept',confirmed:true,id:c.id,revision:c.revision}));
          }
          return;
        }
        let stage=action;
        if (action==='next') stage=(await call({operation:'lab-status'})).next_stage;
        if (!['train','baseline','memory','other'].includes(stage)) {
          ctx.ui.notify('步骤：lab setup → train → review → accept → next（两次）→ other → status','info');return;
        }
        if (!ctx.isIdle() || !ctx.model) {ctx.ui.notify('请等待空闲并配置模型','warning');return;}
        const preview=await call({operation:'lab-preview',stage});show(ctx,'跨任务模型载荷预览',preview);
        if (!await ctx.ui.confirm('执行一个跨任务修复阶段',
          `向 ${ctx.model.provider}/${ctx.model.id} 发送固定演示任务/源码及本阶段适用的实验记忆。模型可以调用真实Shell并修改独立副本，运行用户权限；目录副本不是OS沙盒。模型阶段约105秒，另有参考测试。继续？`)) return;
        ctx.ui.notify(`运行 ${stage}，完成后独立检查测试数量和受保护文件。`,'info');
        show(ctx,'跨任务阶段结果',await call({operation:'lab-run',stage,packet_hash:preview.packet_hash,
          confirmed:true,pi_root:process.env.EH_PI_ROOT,provider:ctx.model.provider,model:ctx.model.id}));
      } else if (args === 'task' || args.startsWith('task ')) {
        show(ctx, '任务证据包', await call({operation:'task', task_id:args.slice(4).trim() || lastTask}));
      } else if (args === 'test' || args.startsWith('test ')) {
        if (!ctx.isIdle()) { ctx.ui.notify('请等任务停止后再运行独立测试', 'warning'); return; }
        show(ctx, '测试与代码版本证据', await call({operation:'verify', workspace:ctx.cwd,
          task_id:args.slice(4).trim() || lastTask}));
      } else if (args.startsWith('note ')) {
        const result = await call({operation:'note', task_id:lastTask, note:args.slice(5)});
        ctx.ui.notify(`用户反馈已记录 E${result.evidence_id}`, 'info');
      } else if (args === 'experiment latest') {
        show(ctx,'最近一次记忆对照',await call({operation:'experiment-latest'}));
      } else if (args === 'experiment') {
        if (!ctx.isIdle()) {ctx.ui.notify('请等任务停止后再运行对照','warning');return;}
        const preview=await call({operation:'experiment-preview'});
        show(ctx,'模型对照实验载荷预览',preview);
        if (!ctx.model) {ctx.ui.notify('请先配置模型','warning');return;}
        if (!await ctx.ui.confirm('运行两组真实模型对照',
          `向 ${ctx.model.provider}/${ctx.model.id} 发送固定模拟任务、coding指导与预览中的一条记忆。两组独立会话，可能各有多轮模型请求；每组最多6次有效工具调用、约105秒。工具仅返回模拟数据，无真实Shell。不会自动晋升任何能力。继续？`)) return;
        ctx.ui.notify('开始两组对照；各自使用新会话，不修改真实项目。每组有约105秒总时限，完成后显示结果。','info');
        show(ctx,'记忆对照实验结果',await call({operation:'experiment-run',confirmed:true,
          packet_hash:preview.packet_hash,memory_id:preview.experiment_preview.memory.id,
          pi_root:process.env.EH_PI_ROOT,provider:ctx.model.provider,model:ctx.model.id}));
      } else if (args === 'review local') {
        show(ctx, '本地提取：仅整理已有用户反馈', await call({operation:'review-local',task_id:lastTask}));
      } else if (args === 'review') {
        if (!ctx.isIdle()) {ctx.ui.notify('请等当前任务停止后再复盘','warning');return;}
        const preview = await call({operation:'review-preview', task_id:lastTask});
        show(ctx, '模型复盘载荷预览', preview);
        if (!ctx.model) {ctx.ui.notify('请先配置模型','warning');return;}
        if (!await ctx.ui.confirm('调用模型生成待审候选',
          `向 ${ctx.model.provider}/${ctx.model.id} 发送预览中的目标、工具输出摘要、测试状态和用户反馈。一次无工具请求，最多120秒；候选不会自动生效。继续？`)) return;
        show(ctx, '模型复盘候选', await call({operation:'review-model',task_id:preview.packet.task_id,
          packet_hash:preview.packet_hash,confirmed:true,pi_root:process.env.EH_PI_ROOT,
          provider:ctx.model.provider,model:ctx.model.id}));
      } else if (args === "audit") show(ctx, "记忆审计", await call({ operation: "audit" }));
      else if (!args || args === 'status') show(ctx, "Harness 状态与版本来源", await refresh(ctx));
      else ctx.ui.notify('未知 Harness 子命令。可用：status / memory-review [ID] / task / test / note / review / lab / experiment / audit', 'warning');
    },
  });
  pi.registerCommand('experience', {
    description:'经验候选：list / show ID / edit ID / accept ID / reject ID',
    handler:async (args,ctx) => {
      const [verb,id] = args.trim().split(/\s+/);
      if (!verb || verb==='list') {
        show(ctx,'经验候选',await call({operation:'experience-list'}));return;
      }
      const {candidate} = await call({operation:'experience-get',id});
      if (verb==='show') {show(ctx,'经验候选详情',{candidate});return;}
      if (verb==='edit') {
        const content = await ctx.ui.editor('编辑候选内容（尚未生效）',candidate.content);
        if (content===undefined) return;
        show(ctx,'已修改候选',await call({operation:'experience-edit',id,revision:candidate.revision,content}));return;
      }
      if (verb==='accept' || verb==='reject') {
        if (verb==='accept' && !await ctx.ui.confirm('确认候选内容',
          `${candidate.content}\n依据：${candidate.evidence_ids.join(',')}\n${candidate.kind==='workflow' ? '仅标为已审阅，独立评测前不启用工作流。' : '将作为你确认的项目记忆供后续任务使用；不代表已证明普遍有效。'}`)) return;
        show(ctx,'候选决定',await call({operation:'experience-decide',id,revision:candidate.revision,accept:verb==='accept'}));
        await refresh(ctx);return;
      }
      ctx.ui.notify('用法：/experience list | show ID | edit ID | accept ID | reject ID','info');
    },
  });
  pi.registerCommand("memory", {
    description: "管理记忆：list / add 内容 / user 内容 / edit ID 内容 / forget ID",
    handler: async (args, ctx) => {
      const [verb, ...rest] = args.trim().split(/\s+/);
      const content = rest.join(" ");
      if (verb === "add" || verb === "user") {
        await call({ operation: "memory-add", content, scope: verb === "user" ? "user" : "project" });
      } else if (verb === "edit" || verb === "forget") {
        if (!await ctx.ui.confirm("重置当前对话", "更新/删除后开始新会话，避免历史回答继续引用旧记忆；历史文件仍保留。继续？")) return;
        await ctx.newSession({ withSession: async () => {
          await bridge({ operation: verb === "edit" ? "memory-edit" : "memory-forget", id: rest[0], content: rest.slice(1).join(" ") });
        } });
        return;
      } else if (verb && verb !== "list") {
        ctx.ui.notify("用法：/memory list | add 内容 | user 内容 | edit ID 内容 | forget ID", "info"); return;
      }
      show(ctx, "分范围记忆", await call({ operation: "memory-list" }));
      await refresh(ctx);
    },
  });
  pi.registerCommand("evolution", {
    description: "查看/验证递归改进：status / run / apply / rollback memory-policy|improve",
    handler: async (args, ctx) => {
      if (args.startsWith("rollback ")) {
        await call({ operation: "rollback", name: args.slice(9).trim() });
      } else if (args === "run" || args === "apply") {
        const preview = await call({ operation: "evolution-preview" });
        show(ctx, "即将发送给模型的训练反馈", preview);
        if (!await ctx.ui.confirm("调用当前模型进行两轮有界改进", `向 ${ctx.model?.provider}/${ctx.model?.id} 发送当前策略、改进 Skill 和本范围反馈（见上方）。可能多次请求；${args === "apply" ? "通过独立门槛后启用新版本" : "只评测，不启用"}。继续？`)) return;
        const result = await call({ operation: "evolve", confirmed: true, apply: args === "apply",
          pi_root: process.env.EH_PI_ROOT, provider: ctx.model?.provider, model: ctx.model?.id });
        show(ctx, "递归改进结果（固定记忆案例评测）", result);
      } else show(ctx, "版本与递归来源（run 评测 / apply 验证后启用）", await refresh(ctx));
      await refresh(ctx);
    },
  });
}
