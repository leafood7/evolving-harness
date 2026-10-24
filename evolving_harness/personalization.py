"""Scoped user preferences, workflow selection and bounded context assembly."""
from .components import Components
from .domain import digest


def budget_memories(memories,policy):
    selected=[];seen=set();used=0
    for m in memories:
        key=' '.join(m['content'].casefold().split())
        if policy['deduplicate'] and key in seen: continue
        length=len(m['content'])
        if used+length>policy['max_chars']: continue
        selected.append(m);seen.add(key);used+=length
        if len(selected)>=policy['max_memories']: break
    return selected


def assemble(store,owner,project,prompt,memories):
    components=Components(store,owner,project)
    context=components.seed('context')
    reviewer=components.seed('reviewer')
    profile=Components(store,owner,'@user').seed('profile')
    selected=budget_memories(memories,context['payload'])
    skills=[]
    for c in components.history():
        if c['payload']['kind']!='skill' or (components.active(c['name']) or {}).get('id')!=c['id']: continue
        if any(t.casefold() in prompt.casefold() for t in c['payload']['triggers']): skills.append(c)
    # Workflows are instructions, not automatically executed scripts. Keep a separate total budget.
    skills=skills[:3];skill_text=[];used=0;chosen=[]
    for c in skills:
        text=c['name']+' v'+str(c['version'])+':\n'+'\n'.join(c['payload']['steps'])
        if used+len(text)<=6000: skill_text.append(text);used+=len(text);chosen.append(c['id'])
    text='User preferences (user instructions take precedence):\n'+'\n'.join(profile['payload']['preferences'])
    text+='\nManaged workflows (run relevant verification; do not claim unexecuted tests):\n'+'\n'.join(skill_text)
    tools=[c for c in components.history() if c['payload']['kind']=='tool' and
           (components.active(c['name']) or {}).get('id')==c['id']]
    text+='\nInstalled tool recipes (harness_recipe tool asks confirmation before execution):\n'+\
          '\n'.join(c['name']+': '+c['payload']['description'] for c in tools[:20])
    return {'memories':selected,'instructions':text,'components':{'context':context['id'],'reviewer':reviewer['id'],
            'profile':profile['id'],'skills':chosen,'tools':[c['id'] for c in tools[:20]]},
            'context_metrics':{'considered_memories':len(memories),'injected_memories':len(selected),
                               'memory_chars':sum(len(m['content']) for m in selected),'workflow_chars':used,
                               'payload_hash':digest(text)}}
