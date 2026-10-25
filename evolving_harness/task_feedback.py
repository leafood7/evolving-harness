"""Explicit task ratings and local suggestions; review never activates a rule."""
import json
import re

from .domain import canonical, digest, now, uid
from .evidence import Evidence
from .runtime import redact


class TaskFeedback:
    def __init__(self, store, owner, project):
        self.store, self.owner, self.project = store, owner, project
        store.db.executescript('''
        CREATE TABLE IF NOT EXISTS task_ratings (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
          task_id TEXT NOT NULL REFERENCES tasks(id), rating TEXT NOT NULL,
          target TEXT NOT NULL, note TEXT NOT NULL, request_id TEXT NOT NULL,
          request_hash TEXT NOT NULL, evidence_id INTEGER NOT NULL, source TEXT NOT NULL,
          created TEXT NOT NULL, UNIQUE(owner,project,request_id)
        );
        CREATE TABLE IF NOT EXISTS task_suggestions (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
          task_id TEXT NOT NULL REFERENCES tasks(id), feedback_id TEXT NOT NULL REFERENCES task_ratings(id),
          target TEXT NOT NULL, reason TEXT NOT NULL, proposal TEXT NOT NULL,
          evidence TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
          created TEXT NOT NULL, UNIQUE(feedback_id)
        );
        ''')

    def list(self, task):
        self.store.get_task(self.owner, self.project, task)
        ratings = [dict(r) for r in self.store.db.execute(
            'SELECT * FROM task_ratings WHERE owner=? AND project=? AND task_id=? ORDER BY rowid',
            (self.owner, self.project, task))]
        suggestions = [dict(r) | {'evidence_ids': json.loads(r['evidence'])} for r in self.store.db.execute(
            'SELECT * FROM task_suggestions WHERE owner=? AND project=? AND task_id=? ORDER BY rowid',
            (self.owner, self.project, task))]
        return {'ratings': ratings, 'suggestions': suggestions, 'automatic_application': False}

    def submit(self, task, rating, target, note, request_id, *, source='explicit-task-rating'):
        bundle = Evidence(self.store, self.owner, self.project).bundle(task)
        if bundle['task']['state'] == 'running': raise ValueError('Finish the task before rating it')
        if not isinstance(rating,str) or not isinstance(target,str) or rating not in {'satisfied', 'adjust', 'issue'} or target not in {'task', 'memory', 'workflow'}:
            raise ValueError('Invalid rating or suggestion target')
        if not isinstance(note, str) or len(note) > 2000 or (rating != 'satisfied' and not note.strip()):
            raise ValueError('Explain the requested adjustment or problem in 1..2000 characters')
        if rating == 'satisfied' and target != 'task':
            raise ValueError('Satisfaction records an outcome, not a memory or workflow change')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request_id):
            raise ValueError('A bounded request ID is required')
        note = redact(note.strip()); fingerprint = digest({'task':task,'rating':rating,'target':target,'note':note,'source':source})
        verifications = [e for e in bundle['events'] if e['kind'] == 'verification']
        latest = verifications[-1] if verifications else None
        refs = [latest['seq']] if latest else []
        reason = ('用户表示满意，仅记录本次结果；不推断长期偏好或进化收益。' if rating == 'satisfied' else
                  '用户报告了调整需求或问题；报告本身不证明历史执行失败。')
        if latest and rating != 'satisfied':
            reason += '最近独立验收状态为 '+str(latest['data'].get('status','unverified'))+'；测试通过不代表需求已满足。'
        elif rating != 'satisfied': reason += '当前缺少独立验收，需先补充复现或验证。'
        if source != 'explicit-task-rating': reason = '离线合成反馈演示，不代表用户真实意见。' + reason
        proposals = {'task':'先核对并处理本任务的反馈：',
                     'memory':'待确认的项目记忆草案，仅来自本次反馈，未作为长期偏好或项目规则启用：',
                     'workflow':'工作流改进假设，需先补充复现、独立对照和回归验证，再考虑生成Skill版本：'}
        with self.store.transaction():
            previous = self.store.db.execute('SELECT * FROM task_ratings WHERE owner=? AND project=? AND request_id=?',
                                            (self.owner,self.project,request_id)).fetchone()
            if previous:
                if previous['request_hash'] != fingerprint: raise ValueError('Request ID was reused with different feedback')
            else:
                feedback = uid('rating'); suggestion = uid('suggestion'); created = now()
                event = {'rating_id':feedback,'rating':rating,'target':target,'note':note,
                         'source':source,'is_user_report':source=='explicit-task-rating','not_evolution_evidence':True}
                cursor = self.store.db.execute('INSERT INTO events(owner,project,task_id,kind,data,created) VALUES(?,?,?,?,?,?)',
                    (self.owner,self.project,task,'user_feedback',canonical(event),created))
                refs.append(cursor.lastrowid)
                self.store.db.execute('INSERT INTO task_ratings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (feedback,self.owner,self.project,task,rating,target,note,request_id,fingerprint,cursor.lastrowid,source,created))
                status = 'no_change' if rating == 'satisfied' else 'pending'
                proposal = '保留当前设置，不自动修改记忆或Skill。' if rating == 'satisfied' else proposals[target]+note
                self.store.db.execute('INSERT INTO task_suggestions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (suggestion,self.owner,self.project,task,feedback,'none' if rating=='satisfied' else target,
                     reason,proposal,canonical(refs),status,1,created))
        return self.list(task)

    def decide(self, key, revision, action):
        if type(revision) is not int or action not in {'review', 'reject'}:
            raise ValueError('Choose review or reject with the current revision')
        with self.store.transaction():
            row = self.store.db.execute('SELECT * FROM task_suggestions WHERE id=? AND owner=? AND project=?',
                                       (key,self.owner,self.project)).fetchone()
            if not row: raise ValueError('Suggestion not found in this scope')
            if row['status'] != 'pending' or row['revision'] != revision:
                raise ValueError('Suggestion is no longer pending or revision changed')
            status = 'reviewed_not_applied' if action == 'review' else 'rejected'
            self.store.db.execute('UPDATE task_suggestions SET status=?,revision=revision+1 WHERE id=?', (status,key))
            self.store.db.execute('INSERT INTO events(owner,project,task_id,kind,data,created) VALUES(?,?,?,?,?,?)',
                (self.owner,self.project,row['task_id'],'task_suggestion_review',canonical({'id':key,'action':action,
                 'status':status,'previous_revision':revision,'applied':False}),now()))
        return self.list(row['task_id'])
