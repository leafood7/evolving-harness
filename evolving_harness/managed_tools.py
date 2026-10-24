"""Tool recipes expand via versioned manifests; executable adapters remain trusted code."""
import os
from pathlib import Path
import subprocess
import sys

from .components import Components
from .domain import digest
from .evidence import snapshot
from .runtime import redact


def preview(store,owner,project,name,workspace):
    c=Components(store,owner,project).active(name)
    if not c or c['payload']['kind']!='tool': raise ValueError('No active tool recipe')
    root=Path(workspace).resolve()
    if not root.is_dir(): raise ValueError('Workspace missing')
    recipe=c['payload']['recipe']
    if recipe=='checks_runner':
        path=root/'checks'/'run_tests.py'
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('Test runner must be a real file within workspace')
    command={'unittest':[sys.executable,'-m','unittest','-v'],
             'checks_runner':[sys.executable,'checks/run_tests.py'], 'workspace_summary':None}[recipe]
    data={'component':c['id'],'name':name,'workspace':str(root),'command':command,
          'recipe':recipe,'timeout':c['payload']['timeout']}
    return {'tool_preview':data,'packet_hash':digest(data)}


def run(store,owner,project,prepared):
    p=prepared['tool_preview']
    if preview(store,owner,project,p['name'],p['workspace'])['packet_hash']!=prepared['packet_hash']:
        raise ValueError('Tool changed since preview')
    if p['recipe']=='workspace_summary':
        data=snapshot(p['workspace'])
        result={'status':'completed','files':list(data.get('files',{})),'coverage':data.get('coverage')}
    else:
        if os.environ.get('EH_ALLOW_WRITE')!='1': raise ValueError('Restart native with --allow-write for test execution')
        try:
            child=subprocess.run(p['command'],cwd=p['workspace'],capture_output=True,text=True,timeout=p['timeout'])
            output=redact(child.stdout+child.stderr)
            import re
            count=re.search(r'Ran (\d+) tests?',output)
            result={'status':'passed' if child.returncode==0 and count and int(count[1])>0 else 'failed',
                    'test_count':int(count[1]) if count else None,'exit_code':child.returncode,'output':output}
        except subprocess.TimeoutExpired:
            result={'status':'timeout','test_count':None}
    store.event(owner,project,'managed_tool_run',{'component':p['component'],'workspace':p['workspace'],'result':result})
    return result
