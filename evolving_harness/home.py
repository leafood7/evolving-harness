"""Local usage guide; experimental capability claims are deliberately bounded."""
import html
import json
from pathlib import Path
import shutil

from .completion import export as task_card
from .domain import digest


def export(store, owner, project, workspace):
    recent = store.db.execute('SELECT id,prompt,state FROM tasks WHERE owner=? AND project=? ORDER BY rowid DESC LIMIT 1',
                              (owner, project)).fetchone()
    card = task_card(store,owner,project,recent['id']) if recent else None
    data = {'user':owner,'project':project,'workspace':str(Path(workspace).resolve()) if workspace else None,
        'recent_task':dict(recent) if recent else None,
        'daily':[{'name':'开发任务','command':'直接输入需求','status':'可日常使用；模型调用计费'},
                 {'name':'结果与证据','command':'/harness card','status':'本地查看，不调用模型'},
                 {'name':'提交真实反馈','command':'/harness rate 满意 或 /harness rate 需调整 说明','status':'本地记录，确认后保存'},
                 {'name':'查看反馈建议','command':'/harness ratings','status':'待审核不等于已启用'},
                 {'name':'项目记忆','command':'/memory list','status':'按用户与项目分范围管理'}],
        'experimental':[{'name':'Skill/复盘策略改进','status':'候选和版本机制已实现；真实收益未证明稳定提升'},
                        {'name':'对照评测','status':'支持隔离副本、独立验收；小样本、平局、中断不能证明进化有效'},
                        {'name':'递归与工具改进','status':'仅支持有边界的机制；不是任意自修改代码'}],
        'search_tools':{'ripgrep':bool(shutil.which('rg')),'fd':bool(shutil.which('fd'))},
        'latest_card':card['card_html'] if card else None,
        'notice':'模型任务完成不等于验证通过；缺失证据显示未验证。查看本页不调用模型、不自动安装工具或启用候选。'}
    root=Path(store.db.execute('PRAGMA database_list').fetchone()[2]).parent/'native-reports'/digest({'user':owner,'project':project})[:16]
    root.mkdir(parents=True,exist_ok=True)
    key=digest(data)[:16]; page=root/('home-'+key+'.html'); record=root/('home-'+key+'.json')
    record.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    esc=lambda x:html.escape(str(x))
    body='<!doctype html><html lang="zh"><meta charset="utf-8"><title>Harness 使用入口</title><style>body{font:17px system-ui;max-width:1000px;margin:36px auto;padding:16px}td,th{padding:10px;border:1px solid #ccc;text-align:left}table{border-collapse:collapse;width:100%}code{white-space:pre-wrap}</style>'
    body+='<h1>Harness 使用入口</h1><p>用户 '+esc(owner)+' · 项目 '+esc(project)+'</p><p>工作目录：'+esc(data['workspace'] or '未配置')+'</p>'
    if card:body+='<p><a href="'+esc(Path(card['card_html']).as_uri())+'">查看最近任务卡片与证据</a></p>'
    else:body+='<p>当前范围暂无任务。直接输入一个具体需求，才会开始模型调用。</p>'
    for title,key_name in [('日常使用','daily'),('实验功能与限制','experimental')]:
        body+='<h2>'+title+'</h2><table>'
        for row in data[key_name]:body+='<tr><th>'+esc(row['name'])+'</th><td>'+esc(row.get('command',''))+'</td><td>'+esc(row['status'])+'</td></tr>'
        body+='</table>'
    body+='<h2>遇到问题</h2><p>终端显示不全：输入 <code>/harness card full</code> 获取完整卡片路径。代码改动后旧验收变为过期，需重新验证；不会把旧结果当作当前代码通过。</p>'
    body+='<p>缺少fd/rg表示对应搜索工具未安装；不是启动失败。当前仍可使用读取、列目录和已授权Shell。此页不会自动下载工具。</p>'
    body+='<p>版本回退属于高级操作，先 <code>/harness package show 组件ID</code> 检查，再用 <code>/harness package rollback 名称</code> 并确认；会切换会话。反馈建议本身没有自动启用权限。</p>'
    body+='<p>'+esc(data['notice'])+'</p></html>'
    page.write_text(body,encoding='utf-8')
    return {'project_home':data,'guide_html':str(page),'guide_json':str(record)}
