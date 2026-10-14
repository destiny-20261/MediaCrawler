"""Put beside MediaCrawler main.py. Start with: python lead_bridge.py"""
import inspect, json, os, re, runpy, secrets, signal, subprocess, sys, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('LEAD_DATA_DIR',str(ROOT/'lead_data'))).resolve()
TOKEN=os.environ.get('CRAWLER_API_TOKEN','')
LOCK=threading.Lock()
ACTIVE=None

def write_json(f,d):
    t=f.with_suffix('.tmp');t.write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8');t.replace(f)

def worker(f):
    j=json.loads(Path(f).read_text(encoding='utf-8'))
    os.chdir(ROOT);sys.path.insert(0,str(ROOT))
    import config
    cfg={'PLATFORM':'xhs','LOGIN_TYPE':'cookie','CRAWLER_TYPE':'search','KEYWORDS':j['keyword'],
         'COOKIES':os.environ.get('XHS_COOKIES',''),'CRAWLER_MAX_NOTES_COUNT':j['limit'],
         'START_PAGE':1,'SAVE_DATA_OPTION':'jsonl','SAVE_DATA_PATH':j['output'],
         'HEADLESS':True,'CDP_HEADLESS':True,'ENABLE_CDP_MODE':False,'MAX_CONCURRENCY_NUM':1,
         'ENABLE_GET_MEDIA':False,'ENABLE_GET_WORDCLOUD':False}
    for k,v in cfg.items():setattr(config,k,v)
    import cmd_arg
    original=cmd_arg.parse_cmd
    async def parse(*a,**kw):
        result=original(*a,**kw)
        if inspect.isawaitable(result):result=await result
        for k,v in cfg.items():setattr(config,k,v)
        result.platform,result.lt,result.type='xhs','cookie','search'
        result.keywords,result.init_db=j['keyword'],None
        return result
    cmd_arg.parse_cmd=parse
    sys.argv=['main.py','--platform','xhs','--lt','cookie','--type','search']
    runpy.run_path(str(ROOT/'main.py'),run_name='__main__')

def read_notes(j):
    notes={}
    for f in sorted(Path(j['output']).rglob('*')):
        if f.suffix not in ('.jsonl','.json') or f.stat().st_size>30000000:continue
        try:
            text=f.read_text(encoding='utf-8');rows=[]
            if f.suffix=='.json':
                rows=json.loads(text);rows=rows if isinstance(rows,list) else [rows]
            else:
                for line in text.splitlines():
                    try:rows.append(json.loads(line))
                    except ValueError:pass
            for r in rows:
                if not isinstance(r,dict) or r.get('comment_id'):continue
                nid=str(r.get('note_id',''))
                if not re.fullmatch(r'[a-zA-Z0-9_-]{8,64}',nid):continue
                url=str(r.get('note_url') or r.get('url') or '')
                u=urlparse(url)
                if u.scheme!='https' or u.hostname not in ('xiaohongshu.com','www.xiaohongshu.com') or u.username:url='https://www.xiaohongshu.com/explore/'+nid
                def num(k):
                    try:return int(r[k])
                    except (ValueError,KeyError,TypeError):return r.get(k)
                notes[nid]={'id':nid,'title':str(r.get('title') or r.get('desc') or '未命名笔记')[:300],
                    'url':url,'keyword':j['keyword'],'author':str(r.get('nickname') or ''),
                    'likes':num('liked_count'),'comments':num('comment_count'),'saves':num('collected_count'),
                    'shares':num('share_count'),'description':str(r.get('desc') or '')[:5000],
                    'source':'MediaCrawler','collectedAt':j['startedAt'],'score':None,'signal':'待人工分析'}
        except (OSError,ValueError):continue
    return list(notes.values())[:j['limit']]

def public(j):
    notes=read_notes(j)
    return {k:j.get(k) for k in ('id','keyword','limit','status','message','startedAt','finishedAt')}|{'notes':notes,'count':len(notes)}

def execute(j):
    global ACTIVE
    folder=DATA/j['id']
    try:
        with (folder/'private.log').open('wb') as log:
            p=subprocess.Popen([sys.executable,str(ROOT/'lead_bridge.py'),'--worker',str(folder/'job.json')],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:code=p.wait(timeout=1200)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid,signal.SIGTERM)
                try:p.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
                code=-1
        count=len(read_notes(j))
        if code==0:
            j['status']='completed';j['message']=f'采集完成，共 {count} 条笔记。' if count else '任务结束，但没有采集到笔记，请核对关键词或登录状态。'
        else:
            j['status']='failed';j['message']='采集超时，任务已停止。' if code==-1 else '采集失败，已取得的结果仍可导出。请查看 MediaCrawler 服务日志。'
            log=(folder/'private.log').read_text(encoding='utf-8',errors='replace')[-20000:]
            # Never return raw crawler logs, which may contain session credentials.
            if re.search(r'login.*fail|not logged|check_login|pong.*False',log,re.I):j['message']='登录状态无效或需要验证，请更新 MediaCrawler 服务中的 XHS_COOKIES。'
            # Redact credential-shaped values before sending diagnostics to Railway.
            cookie=os.environ.get('XHS_COOKIES','')
            if cookie:log=log.replace(cookie,'[REDACTED]')
            log=re.sub(r'(?i)(web_session|a1|cookie|authorization|token)([\s\"\x27:=]+)[^\s,;\"\x27}]+',r'\1\2[REDACTED]',log)
            print(f"Task {j['id']} failed (exit {code}).\n"+log[-5000:],flush=True)
        j['finishedAt']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    except Exception as e:
        j['status'],j['message']='failed','采集进程启动失败，请检查服务配置。'
        print('Task startup failed: '+type(e).__name__,flush=True)
    finally:
        try:write_json(folder/'job.json',j)
        finally:
            with LOCK:ACTIVE=None

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def send(self,status,d):
        b=json.dumps(d,ensure_ascii=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def auth(self):
        if not TOKEN or not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+TOKEN):
            self.send(401,{'ok':False,'error':'采集服务连接密钥不匹配。'});return False
        return True
    def do_GET(self):
        route=urlparse(self.path).path
        if route=='/health':return self.send(200,{'ok':True,'service':'mediacrawler-bridge'})
        if not self.auth():return
        if route.startswith('/tasks/'):
            tid=route[7:]
            if not re.fullmatch('[a-f0-9]{32}',tid):return self.send(400,{'ok':False,'error':'任务编号无效。'})
            try:
                with LOCK:j=dict(ACTIVE) if ACTIVE and ACTIVE['id']==tid else json.loads((DATA/tid/'job.json').read_text(encoding='utf-8'))
                return self.send(200,{'ok':True,'task':public(j)})
            except (OSError,ValueError):return self.send(404,{'ok':False,'error':'任务不存在，可能未配置持久化存储。'})
        self.send(404,{'ok':False,'error':'not found'})
    def do_POST(self):
        global ACTIVE
        if not self.auth():return
        if urlparse(self.path).path!='/tasks':return self.send(404,{'ok':False,'error':'not found'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=10000:raise ValueError()
            self.connection.settimeout(15);b=json.loads(self.rfile.read(n))
            if not isinstance(b,dict):raise ValueError()
            keyword,limit=b.get('keyword',''),b.get('limit',50)
            if not isinstance(keyword,str) or not 1<=len(keyword.strip())<=100 or re.search('[\x00-\x1f]',keyword):raise ValueError()
            if type(limit) is not int or not 1<=limit<=200:raise ValueError()
        except (ValueError,TypeError,OSError):return self.send(400,{'ok':False,'error':'关键词或采集数量格式不正确。'})
        if not os.environ.get('XHS_COOKIES','').strip():return self.send(503,{'ok':False,'error':'MediaCrawler 服务未配置 XHS_COOKIES。'})
        if not (ROOT/'main.py').is_file():return self.send(503,{'ok':False,'error':'请把 lead_bridge.py 放在 main.py 旁边。'})
        with LOCK:
            if ACTIVE:return self.send(409,{'ok':False,'error':'已有任务正在采集，请等待完成。'})
            tid=secrets.token_hex(16);folder=DATA/tid;folder.mkdir(parents=True)
            j={'id':tid,'keyword':keyword.strip(),'limit':limit,'output':str(folder/'output'),'status':'running','message':'正在启动浏览器并采集，结果会陆续显示。','startedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
            write_json(folder/'job.json',j);ACTIVE=j
        threading.Thread(target=execute,args=(j,),daemon=True).start();self.send(202,{'ok':True,'task':public(j)})

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker':worker(sys.argv[2])
    else:
        if not TOKEN:raise SystemExit('Set CRAWLER_API_TOKEN in Railway Variables first.')
        DATA.mkdir(parents=True,exist_ok=True)
        for f in DATA.glob('*/job.json'):
            try:
                j=json.loads(f.read_text(encoding='utf-8'))
                if j.get('status')=='running':j.update(status='interrupted',message='服务重启，之前的任务已中断；已保存的结果仍可查看。');write_json(f,j)
            except (OSError,ValueError):pass
        print('MediaCrawler task API listening',flush=True)
        ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
