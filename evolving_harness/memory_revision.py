"""Human-reviewed memory revisions with immutable event history and stale guards."""
from .domain import canonical, digest, now, uid
from .memory import Memory
from .memory_review import review


class Revisions:
    def __init__(self, store, owner, project):
        self.store, self.owner, self.project = store, owner, project

    def list(self):
        latest = {}
        for e in self.store.events(self.owner, self.project, 'memory_revision_candidate'):
            latest[e['data']['id']] = e['data']
        return list(latest.values())

    def get(self, key):
        return next((c for c in self.list() if c['id'] == key), None) or self.missing()

    def missing(self):
        raise ValueError('Revision candidate not found in this user/project')

    def save(self, candidate):
        self.store.db.execute('INSERT INTO events(owner,project,kind,data,created) VALUES(?,?,?,?,?)',
            (self.owner, self.project, 'memory_revision_candidate', canonical(candidate), now()))

    def propose(self, memory_id):
        item = review(self.store, self.owner, self.project, memory_id)['memory_review']['items'][0]
        m = item['memory']
        scope = m['project'] or self.project
        original = Memory(self.store).get(self.owner, scope, memory_id)
        fingerprint = digest(original)
        for c in self.list():
            if c['status'] == 'pending' and c['before_hash'] == fingerprint and c['memory_id'] == memory_id:
                return c
        candidate = {'id': uid('revision'), 'memory_id': memory_id, 'memory_project': scope,
            'before': original, 'before_hash': fingerprint, 'action': 'keep',
            'content': m['content'], 'reason': item['recommendation'], 'assessment': item['assessment'],
            'evidence': item, 'generator': 'local-evidence-summary-v1',
            'status': 'pending', 'revision': 1, 'created': now()}
        with self.store.db:
            self.save(candidate)
        return candidate

    def edit(self, key, revision, action, content=None, reason=None):
        if action not in {'keep','revise','retire'}:
            raise ValueError('Action must be keep, revise or retire')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('An explicit user reason is required')
        if action == 'revise' and (not isinstance(content, str) or not content.strip()):
            raise ValueError('Revised content cannot be empty')
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            c = self.get(key)
            if c['status'] != 'pending' or c['revision'] != revision:
                raise ValueError('Candidate changed or already decided; show it again')
            c = c | {'action':action, 'content':content.strip() if action == 'revise' else c['before']['content'],
                     'reason':reason.strip(), 'revision':revision+1, 'editor':'explicit-user'}
            self.save(c)
        return c

    def decide(self, key, revision, accept):
        if type(accept) is not bool:
            raise ValueError('Explicit accept/reject decision required')
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            c = self.get(key)
            if c['status'] != 'pending' or c['revision'] != revision:
                raise ValueError('Candidate changed or already decided; show it again')
            if accept:
                m = Memory(self.store).get(self.owner, c['memory_project'], c['memory_id'])
                if digest(m) != c['before_hash']:
                    raise ValueError('Memory changed since proposal; create a fresh candidate')
                if c['action'] != 'keep':
                    self.store.db.execute('UPDATE memories SET content=?,status=?,verified=?,revision=revision+1,source=?,updated=? WHERE id=? AND owner=? AND revision=?',
                        (c['content'], 'inactive' if c['action'] == 'retire' else m['status'],
                         1 if c['action'] == 'revise' else m['verified'], 'user-confirmed-revision:'+c['id'],
                         now(), m['id'], self.owner, m['revision']))
                c['result_memory_revision'] = m['revision'] + (c['action'] != 'keep')
            c = c | {'status':'accepted' if accept else 'rejected','decided':now()}
            self.save(c)
        return c

    def rollback(self,key):
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            c=self.get(key)
            if c['status']!='accepted' or c['action']=='keep': raise ValueError('Only accepted modifications can be rolled back')
            m=Memory(self.store).get(self.owner,c['memory_project'],c['memory_id'])
            if m['revision']!=c['result_memory_revision'] or m['source']!='user-confirmed-revision:'+c['id']:
                raise ValueError('Memory changed since acceptance; refusing to overwrite later changes')
            old=c['before']
            self.store.db.execute('UPDATE memories SET content=?,status=?,verified=?,expires=?,source=?,revision=revision+1,updated=? WHERE id=? AND owner=?',
                (old['content'],old['status'],old['verified'],old['expires'],'rollback:'+c['id'],now(),m['id'],self.owner))
            c=c|{'status':'rolled_back','rollback_before':m,'rollback_revision':m['revision']+1,'rolled_back':now()}
            self.save(c)
        return c
