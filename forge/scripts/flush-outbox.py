#!/usr/bin/env python3
"""INFINITY_FORGE outbox flush — ~/forge/outbox/*.md 를 MEMEX MCP(localhost)로 실배달.
성공분만 sent/로 이동, 실패는 남기고 flush.log에 기록. LLM 0, fire-and-forget (plan.md 11절)."""
import json, re, os, sys, glob, shutil, datetime, urllib.request, urllib.error
import yaml

HOME = os.path.expanduser('~')
OUTBOX = os.path.join(HOME, 'forge', 'outbox')
SENT = os.path.join(OUTBOX, 'sent')
LOG = os.path.join(HOME, 'forge', 'flush.log')
URL = 'http://127.0.0.1:8080/mcp'

def log(msg):
    with open(LOG, 'a') as f:
        f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")

def mcp_post(payload, auth, sid=None, timeout=15):
    h = {'Content-Type': 'application/json',
         'Accept': 'application/json, text/event-stream',
         'Authorization': auth}
    if sid: h['mcp-session-id'] = sid
    req = urllib.request.Request(URL, json.dumps(payload).encode(), h)
    r = urllib.request.urlopen(req, timeout=timeout)
    body = r.read().decode()
    m = re.search(r'data: (\{.*\})', body)
    return r.headers.get('mcp-session-id'), (json.loads(m.group(1)) if m else (json.loads(body) if body.strip() else {}))

def parse_entry(text):
    """skill 형식: '## [aspect] 제목' 헤더 + project:: + tags:: 필드"""
    aspect = None; project = 'INFINITY_FORGE'; tags = None
    m = re.search(r'^##\s*\[(\w+)\]', text, re.M)
    if m: aspect = m.group(1)
    m = re.search(r'^project::\s*(.+)$', text, re.M)
    if m: project = m.group(1).strip()
    m = re.search(r'^tags::\s*(.+)$', text, re.M)
    if m: tags = [t.strip() for t in re.split(r'[,\s]+', m.group(1)) if t.strip()]
    return aspect, project, tags

def main():
    files = sorted(glob.glob(os.path.join(OUTBOX, '*.md')))
    if not files:
        return
    cfg = yaml.safe_load(open(os.path.join(HOME, '.hermes', 'config.yaml')))
    auth = cfg['mcp_servers']['memex']['headers']['Authorization']
    try:
        sid, _ = mcp_post({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2024-11-05','capabilities':{},
            'clientInfo':{'name':'forge-flush','version':'1.0'}}}, auth)
        mcp_post({'jsonrpc':'2.0','method':'notifications/initialized'}, auth, sid)
    except Exception as e:
        log(f"INIT_FAIL {e}")  # MEMEX 다운 = 다음 주기 재시도, 밤 작업 안 막음
        sys.exit(0)
    ok = fail = 0
    for path in files:
        name = os.path.basename(path)
        try:
            text = open(path, encoding='utf-8').read()
            aspect, project, tags = parse_entry(text)
            args = {'content': text, 'project': project}
            if aspect: args['aspect'] = aspect
            if tags: args['tags'] = tags
            _, resp = mcp_post({'jsonrpc':'2.0','id':2,'method':'tools/call',
                'params':{'name':'save_memex','arguments':args}}, auth, sid, timeout=120)
            if resp.get('result', {}).get('isError'):
                raise RuntimeError(str(resp)[:200])
            shutil.move(path, os.path.join(SENT, name))
            log(f"SENT {name} (aspect={aspect} project={project})")
            ok += 1
        except Exception as e:
            log(f"FAIL {name} {e}")
            fail += 1
    log(f"done: ok={ok} fail={fail}")

if __name__ == '__main__':
    main()
