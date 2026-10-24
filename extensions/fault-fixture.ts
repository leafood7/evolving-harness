// Synthetic tools only: no filesystem reads, shell execution, network, or credentials.
import { Type } from '@earendil-works/pi-ai';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';

export const SOURCE = 'def paginate(items, page, size):\n    return items[page * size:(page + 1) * size]\n# Contract: pages are one-based; page and size must be positive.';
export default function faultFixture(pi: ExtensionAPI) {
  let calls = 0;
  const result = (test: boolean) => {
    calls++;
    const exhausted = calls > 6;
    return {
      content:[{type:'text' as const,text:exhausted ? 'FIXTURE_CALL_BUDGET_EXHAUSTED' : test ?
        'FIXTURE_SHELL_UNAVAILABLE: simulated command interpreter is unavailable. No tests executed. This fixture cannot be repaired or installed.' : SOURCE}],
      details:{synthetic:true,code:exhausted ? 'BUDGET_EXHAUSTED':test ? 'SHELL_UNAVAILABLE':'SOURCE',testsExecuted:false},
      isError:test || exhausted,
      ...(exhausted ? {terminate:true} : {}),
    };
  };
  pi.registerTool({name:'fixture_read',label:'Read synthetic project',
    description:'Read the fixed synthetic Python pagination implementation. Does not access the filesystem.',
    parameters:Type.Object({}),async execute(){return result(false);}});
  pi.registerTool({name:'fixture_test',label:'Test synthetic project',
    description:'Attempt to run tests for the synthetic project. This is a simulation, never a real shell.',
    parameters:Type.Object({}),async execute(){return result(true);}});
  pi.registerCommand('fixture-check', {description:'Offline fixture smoke only',handler:async(_args,ctx)=>{
    const read=result(false), test=result(true);
    if (read.details.synthetic!==true || !test.isError || test.details.testsExecuted!==false)
      throw new Error('Fixture contract failed');
    ctx.ui.notify('FIXTURE_CHECK_OK: synthetic source and unavailable shell; no real test executed','info');
  }});
}
