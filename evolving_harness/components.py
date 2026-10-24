"""Versioned, bounded Harness components. Payloads cannot modify approval or eval code."""
import json
import re
from pathlib import Path

from .domain import canonical, digest, now, uid

CHECKS = ('scope', 'expiry', 'duplicate', 'test_count')
SEEDS = {
    'reviewer': {'kind':'reviewer','checks':[], 'priority':list(CHECKS)},
    'context': {'kind':'context','max_memories':10,'max_chars':10000,'deduplicate':False},
    'profile': {'kind':'profile','preferences':[]},
}


def validate(name, payload):
    if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9:_-]{0,63}',name):
        raise ValueError('Invalid component name')
    if not isinstance(payload,dict): raise ValueError('Component must be JSON object')
    kind=payload.get('kind')
    fields={'reviewer':{'kind','checks','priority'}, 'context':{'kind','max_memories','max_chars','deduplicate'},
            'profile':{'kind','preferences'}, 'skill':{'kind','description','triggers','steps'},
            'tool':{'kind','description','recipe','timeout'}}
    if kind not in fields or set(payload)!=fields[kind]: raise ValueError('Invalid component schema')
    expected = kind if kind in SEEDS else kind+':'
    if (kind in SEEDS and name!=expected) or (kind not in SEEDS and not name.startswith(expected)):
        raise ValueError('Component name/kind mismatch')
    def strings(value,count,length):
        return isinstance(value,list) and len(value)<=count and all(isinstance(s,str) and 0<len(s.strip())<=length for s in value)
    if kind=='reviewer':
        if (not strings(payload['checks'],4,30) or len(set(payload['checks']))!=len(payload['checks'])
            or not set(payload['checks'])<=set(CHECKS) or not strings(payload['priority'],4,30)
            or sorted(payload['priority'])!=sorted(CHECKS)):
            raise ValueError('Reviewer only supports fixed check vocabulary')
    elif kind=='context':
        if (type(payload['max_memories']) is not int or not 1<=payload['max_memories']<=20
            or type(payload['max_chars']) is not int or not 256<=payload['max_chars']<=20000
            or type(payload['deduplicate']) is not bool): raise ValueError('Invalid context budget')
    elif kind=='profile':
        if not strings(payload['preferences'],20,400): raise ValueError('Invalid preferences')
    elif kind=='skill':
        if (not isinstance(payload['description'],str) or not 1<=len(payload['description'])<=500
            or not strings(payload['triggers'],10,60) or not payload['triggers']
            or not strings(payload['steps'],12,600) or not payload['steps']): raise ValueError('Invalid skill')
    elif kind=='tool':
        if (not isinstance(payload['description'],str) or not 1<=len(payload['description'])<=500
            or payload['recipe'] not in {'unittest','checks_runner','workspace_summary'}
            or type(payload['timeout']) is not int or not 1<=payload['timeout']<=30):
            raise ValueError('Tool recipe must use a built-in bounded adapter')


class Components:
    def __init__(self,store,owner,project):
        self.store,self.owner,self.project=store,owner,project
        store.db.executescript('''
        CREATE TABLE IF NOT EXISTS components(
          id TEXT PRIMARY KEY,owner TEXT NOT NULL,project TEXT NOT NULL,name TEXT NOT NULL,
          version INTEGER NOT NULL,parent TEXT,generator TEXT,payload TEXT NOT NULL,hash TEXT NOT NULL,
          evidence TEXT NOT NULL,backend TEXT NOT NULL,reason TEXT NOT NULL,created TEXT NOT NULL,
          UNIQUE(owner,project,name,version));
        CREATE TABLE IF NOT EXISTS component_active(owner TEXT,project TEXT,name TEXT,id TEXT,
          PRIMARY KEY(owner,project,name));
        CREATE TABLE IF NOT EXISTS component_evaluations(id TEXT PRIMARY KEY,owner TEXT,project TEXT,
          candidate TEXT,baseline TEXT,candidate_hash TEXT,report TEXT,created TEXT);
        ''')

    def get(self,key):
        r=self.store.db.execute('SELECT * FROM components WHERE id=? AND owner=? AND project=?',
            (key,self.owner,self.project)).fetchone()
        if r is None: raise ValueError('Component not found in current user/project')
        return dict(r)|{'payload':json.loads(r['payload']),'evidence':json.loads(r['evidence'])}

    def active(self,name):
        row=self.store.db.execute('SELECT id FROM component_active WHERE owner=? AND project=? AND name=?',
            (self.owner,self.project,name)).fetchone()
        return self.get(row[0]) if row else None

    def seed(self,name):
        existing=self.active(name)
        if existing: return existing
        if name not in SEEDS: raise ValueError('Component not installed')
        return self.propose(name,SEEDS[name],[],backend='seed',reason='Initial bounded configuration',seed=True)

    def history(self):
        rows=self.store.db.execute('SELECT id FROM components WHERE owner=? AND project=? ORDER BY rowid',
                                  (self.owner,self.project)).fetchall()
        return [self.get(r[0]) for r in rows]

    def propose(self,name,payload,evidence,*,backend='user-editor',reason='',seed=False,generator=None):
        validate(name,payload)
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000: raise ValueError('Reason required')
        if not isinstance(evidence,list) or len(evidence)>100: raise ValueError('Invalid evidence list')
        for seq in evidence:
            if type(seq) is not int or not self.store.db.execute('SELECT 1 FROM events WHERE seq=? AND owner=? AND project=?',
                (seq,self.owner,self.project)).fetchone(): raise ValueError('Evidence outside component scope')
        if generator and self.get(generator)['payload']['kind']!='reviewer':
            raise ValueError('Only a scoped reviewer can generate component improvements')
        with self.store.transaction():
            parent=self.active(name)
            if seed and parent: return parent
            version=self.store.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM components WHERE owner=? AND project=? AND name=?',
                (self.owner,self.project,name)).fetchone()[0]
            key=uid('component')
            self.store.db.execute('INSERT INTO components VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key,self.owner,self.project,name,version,parent['id'] if parent else None,generator,
                 canonical(payload),digest(payload),canonical(evidence),backend,reason,now()))
            if seed: self.store.db.execute('INSERT INTO component_active VALUES(?,?,?,?)',(self.owner,self.project,name,key))
        return self.get(key)

    def evaluate(self,key):
        from .component_eval import evaluate
        c=self.get(key)
        baseline=self.get(c['parent']) if c['parent'] else None
        report=evaluate(baseline,c)
        key=uid('gate')
        with self.store.db:
            self.store.db.execute('INSERT INTO component_evaluations VALUES(?,?,?,?,?,?,?,?)',
                (key,self.owner,self.project,c['id'],c['parent'],c['hash'],canonical(report),now()))
        return {'evaluation_id':key,'candidate':c['id'],**report}

    def promote(self,key,evaluation):
        c=self.get(key)
        with self.store.transaction():
            row=self.store.db.execute('SELECT * FROM component_evaluations WHERE id=? AND owner=? AND project=?',
                (evaluation,self.owner,self.project)).fetchone()
            if (not row or row['candidate']!=key or row['candidate_hash']!=c['hash']
                or row['baseline']!=c['parent'] or json.loads(row['report']).get('decision')!='accept'):
                raise ValueError('Matching independent evaluation required')
            if c['payload']['kind']=='skill':
                from .task_eval import assert_promotion_allowed
                assert_promotion_allowed(self.store,self.owner,self.project,key)
            active=self.active(c['name'])
            if (active['id'] if active else None)!=c['parent']: raise ValueError('Active baseline changed; create a new candidate')
            self.store.db.execute('INSERT INTO component_active VALUES(?,?,?,?) ON CONFLICT(owner,project,name) DO UPDATE SET id=excluded.id',
                (self.owner,self.project,c['name'],key))
        self.store.event(self.owner,self.project,'component_promoted',{'id':key,'evaluation':evaluation})
        return c

    def rollback(self,name):
        with self.store.transaction():
            c=self.active(name)
            if not c or not c['parent']: raise ValueError('No previous active version')
            parent=self.get(c['parent'])
            self.store.db.execute('UPDATE component_active SET id=? WHERE owner=? AND project=? AND name=?',
                (parent['id'],self.owner,self.project,name))
        self.store.event(self.owner,self.project,'component_rollback',{'from':c['id'],'to':parent['id']})
        return parent

    def export_skill(self,key):
        c=self.get(key)
        if c['payload']['kind']!='skill' or (self.active(c['name']) or {}).get('id')!=key:
            raise ValueError('Export requires an active skill')
        db=Path(self.store.db.execute('PRAGMA database_list').fetchone()[2])
        folder=db.parent/'managed-skills'/digest([self.owner,self.project])[:16]/c['id']
        folder.mkdir(parents=True,exist_ok=True)
        payload=c['payload'];name=c['name'].split(':',1)[1]
        text='---\nname: '+name+'\ndescription: '+json.dumps(payload['description'],ensure_ascii=False)+'\n---\n\n'
        text+='\n'.join(f'{i+1}. {step}' for i,step in enumerate(payload['steps']))+'\n'
        path=folder/'SKILL.md';path.write_text(text,encoding='utf-8')
        return {'path':str(path),'component':key,'notice':'Managed workflows also enter task context by trigger; export may be loaded with Pi --skill.'}

    def disable(self,name):
        with self.store.transaction():
            current=self.active(name)
            if not current or current['payload']['kind'] not in {'skill','tool'}:
                raise ValueError('Only installed optional skills/tools may be disabled')
            self.store.db.execute('DELETE FROM component_active WHERE owner=? AND project=? AND name=?',
                (self.owner,self.project,name))
        self.store.event(self.owner,self.project,'component_disabled',{'id':current['id'],'name':name})
        return {'disabled':name,'id':current['id']}
