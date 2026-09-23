"""Real Docker socket, isolated application network and verified TLS reverse tunnel.

Build ambergate:ci and ambergate-agent:ci first. Only disposable QA resources are
created; applications and agent publish no ports. Cleans up on all exits.
"""
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

IMAGE = os.environ.get('AMBERGATE_TEST_IMAGE', 'ambergate:ci')
AGENT = os.environ.get('AMBERGATE_AGENT_TEST_IMAGE', 'ambergate-agent:ci')
PREFIX = 'ag-tunnel-' + uuid.uuid4().hex[:8]
owned, networks = [], []


def docker(*args, check=True):
    result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=90)
    if check and result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def inspect(name): return json.loads(docker('inspect', name))[0]


def wait(predicate, seconds=45):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            value=predicate()
            if value: return value
        except (OSError, urllib.error.URLError): pass
        time.sleep(.5)
    raise AssertionError('Timed out waiting for Docker agent')


def run(name, *args):
    docker('run','-d','--name',name,*args); owned.append(name); return name


BACKEND = '''
import hashlib,sys
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  data=(sys.argv[1]+':'+self.path).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_POST(self):
  data=hashlib.sha256(self.rfile.read(int(self.headers['Content-Length']))).hexdigest().encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*args): pass
ThreadingHTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''


def main():
    central = None
    with tempfile.TemporaryDirectory(prefix=PREFIX) as tmp:
        root=Path(tmp)
        try:
            edge_net, private = PREFIX+'-edge', PREFIX+'-private'
            for network in (edge_net,private):
                docker('network','create',network); networks.append(network)
            apps=[]
            for i in range(2):
                apps.append(run(PREFIX+f'-app-{i}','--network',private,'--label',
                    'ambergate.route=host=agent.test;path=/api;port=8000;strip=true',
                    '--entrypoint','python3',IMAGE,'-u','-c',BACKEND,f'app-{i}'))
            manual_app=run(PREFIX+'-manual','--network',private,'--entrypoint','python3',IMAGE,'-u','-c',BACKEND,'manual')
            central=run(PREFIX+'-central','--network',edge_net,'--network-alias','agent-central',
                '-p','127.0.0.1::8083','-p','127.0.0.1::80',
                '-e','AMBERGATE_ADMIN_PASSWORD=disposable-agent-docker-password',IMAGE)
            info=inspect(central)
            admin=int(info['NetworkSettings']['Ports']['8083/tcp'][0]['HostPort'])
            public=int(info['NetworkSettings']['Ports']['80/tcp'][0]['HostPort'])
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            csrf=''
            def api(path, body=None):
                req=urllib.request.Request(f'http://127.0.0.1:{admin}/api/'+path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json','X-CSRF-Token':csrf})
                with opener.open(req,timeout=10) as r:return json.load(r)
            def login(): return api('login',dict(password='disposable-agent-docker-password'))
            csrf=wait(login)['csrf']
            data=api('agents'); created=api('agents',dict(action='create',name='Docker TLS test',timeout=15,revision=data['revision']))
            token=created['token']; aid=created['agent_id']
            data=api('docker/labels'); api('docker/labels',dict(action='settings',mode='auto',revision=data['revision']))
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',str(root/'key.pem'),
                '-out',str(root/'cert.pem'),'-subj','/CN=agent-edge','-addext','subjectAltName=DNS:agent-edge,IP:127.0.0.1'],check=True,capture_output=True)
            (root/'nginx.conf').write_text('''events {}\nhttp { access_log off; server { listen 443 ssl;
ssl_certificate /tls/cert.pem; ssl_certificate_key /tls/key.pem;
location / { proxy_pass http://agent-central:8083; proxy_http_version 1.1;
proxy_set_header Host $host; proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade"; proxy_read_timeout 90s; proxy_buffering off; }
} }''')
            tls=run(PREFIX+'-tls','--network',edge_net,'--network-alias','agent-edge','-p','127.0.0.1::443',
                '-v',f'{root}:/tls:ro','--entrypoint','nginx',IMAGE,'-c','/tls/nginx.conf','-g','daemon off;')
            # Start agent initially on the app network, then attach outbound connectivity.
            agent=run(PREFIX+'-agent','--network',private,'--read-only','--tmpfs','/tmp:size=16m,mode=1777',
                '-v','/var/run/docker.sock:/var/run/docker.sock:ro','-v',f'{root}/cert.pem:/ca.pem:ro',
                '-e','AMBERGATE_SERVER_URL=https://agent-edge','-e','AMBERGATE_AGENT_TOKEN='+token,
                '-e','AMBERGATE_AGENT_CA_FILE=/ca.pem','-e','AMBERGATE_AGENT_INTERVAL=2',AGENT)
            docker('network','connect',edge_net,agent)
            wait(lambda:any(a['status']=='online' for a in api('agents')['agents']))
            def traffic(path='/api/check', body=None):
                req=urllib.request.Request(f'http://127.0.0.1:{public}'+path,data=body,headers={'Host':'agent.test'})
                with opener.open(req,timeout=10) as r:return r.read()
            wait(lambda:traffic().startswith(b'app-'))
            responses={traffic() for _ in range(12)}
            assert responses=={b'app-0:/check',b'app-1:/check'},responses
            upload=os.urandom(400000)
            assert traffic('/api/upload',upload).decode()==hashlib.sha256(upload).hexdigest()
            for app in apps:
                info=inspect(app)
                assert not any(info['NetworkSettings']['Ports'].values())
                assert set(info['NetworkSettings']['Networks']).isdisjoint(inspect(central)['NetworkSettings']['Networks'])
            assert not any(inspect(agent)['NetworkSettings']['Ports'].values())
            wait(lambda:inspect(agent)['State']['Health']['Status']=='healthy')
            # A self-signed edge must fail without the mounted CA; verification is not disabled.
            check = '''import os,ssl\nfrom ambergate.tunnel import connect\ntry: connect('https://agent-edge','/api/agents/connect',os.environ['AMBERGATE_AGENT_TOKEN'])\nexcept ssl.SSLCertVerificationError: print('TLS verification enforced')\nelse: raise SystemExit('Untrusted certificate accepted')'''
            assert docker('exec',agent,'python3','-c',check)=='TLS verification enforced'
            # Inventory excludes daemon secrets / unrelated labels and records both private apps.
            detail=api('agents/'+aid)['agents'][0]
            assert {a['name'] for a in detail['inventory']} >= set(apps)
            assert detail['routes']==2
            assert detail['manual_routing']
            # The UI can select an unlabelled container and an unexposed application port.
            manual_targets=api('agents/targets',dict(agent_id=aid,targets=[dict(container_id=inspect(manual_app)['Id'],port=8000)]))['targets']
            draft=api('config'); host=next(h for h in draft['config']['hosts'] if h['domain']=='agent.test')
            manual_route={k:v for k,v in host['routes'][0].items() if k!='docker'}
            manual_route.update(id='manual-agent-route',name='Selected container',path='/manual',targets=manual_targets)
            host['routes'].append(manual_route)
            saved=api('config',dict(config=draft['config'],revision=draft['revision']));api('apply',dict(revision=saved['revision']))
            assert traffic('/manual/check')==b'manual:/check'
            docker('rm','-f',manual_app)
            run(manual_app,'--network',private,'--entrypoint','python3',IMAGE,'-u','-c',BACKEND,'recreated')
            wait(lambda:traffic('/manual/check')==b'recreated:/check')
            assert not any(inspect(manual_app)['NetworkSettings']['Ports'].values())
            docker('restart',central)
            info=inspect(central)
            admin=int(info['NetworkSettings']['Ports']['8083/tcp'][0]['HostPort'])
            public=int(info['NetworkSettings']['Ports']['80/tcp'][0]['HostPort'])
            csrf=wait(login)['csrf']
            wait(lambda:traffic().startswith(b'app-'))
            wait(lambda:traffic('/manual/check')==b'recreated:/check')
            # Host-mode one-command installation: the agent joins no app network,
            # yet reaches private ports on two different local bridges via IP.
            other=PREFIX+'-other';docker('network','create',other);networks.append(other)
            docker('network','connect',other,apps[1])
            docker('network','disconnect',private,apps[1])
            docker('rm','-f',agent)
            tls_port=inspect(tls)['NetworkSettings']['Ports']['443/tcp'][0]['HostPort']
            run(agent,'--network','host','--init','--read-only','--tmpfs','/tmp:size=16m,mode=1777',
                '-v','/var/run/docker.sock:/var/run/docker.sock:ro','-v',f'{root}/cert.pem:/ca.pem:ro',
                '-e','AMBERGATE_DOCKER_CONTAINER='+agent,
                '-e','AMBERGATE_SERVER_URL=https://127.0.0.1:'+tls_port,'-e','AMBERGATE_AGENT_TOKEN='+token,
                '-e','AMBERGATE_AGENT_CA_FILE=/ca.pem','-e','AMBERGATE_AGENT_INTERVAL=2',AGENT)
            def host_inventory():
                detail=api('agents/'+aid)['agents'][0]
                rows={c['name']:c for c in detail['inventory']}
                return all(rows.get(app,{}).get('endpoints') and rows[app]['endpoints'][0]['kind']=='ip' for app in apps)
            wait(host_inventory)
            assert {traffic() for _ in range(12)}=={b'app-0:/check',b'app-1:/check'}
            assert traffic('/manual/check')==b'recreated:/check'
            assert inspect(agent)['HostConfig']['NetworkMode']=='host'
            assert not inspect(agent)['HostConfig'].get('PortBindings')
            assert set(inspect(apps[0])['NetworkSettings']['Networks']).isdisjoint(inspect(apps[1])['NetworkSettings']['Networks'])
            wait(lambda:inspect(agent)['State']['Health']['Status']=='healthy')
            print('PASS: host-mode agent reaches two separate bridge networks over verified TLS without published app ports')
            docker('stop',agent)
            wait(lambda:api('agents')['agents'][0]['status']=='offline')
            def empty_route():
                try: traffic()
                except urllib.error.HTTPError as exc:return exc.code==503
                return False
            wait(empty_route)
            docker('start',agent)
            wait(lambda:traffic().startswith(b'app-'))
            created=api('agents',dict(action='rotate',id=aid,revision=api('agents')['revision']))
            assert created['token']!=token
            wait(lambda:not api('agents')['agents'][0]['tunnel'])
            print('PASS: Docker discovery, private ports, TLS, labels and manual routes, container recreation, balancing, uploads, restart, expiry and revocation')
        except BaseException:
            # No environment dumps or credentials. Agent logs contain status only.
            if central: print(docker('logs','--tail','40',central,check=False))
            for name in owned:
                if name.endswith('-agent'): print(docker('logs','--tail','20',name,check=False))
            raise
        finally:
            for name in reversed(owned):docker('rm','-f','-v',name,check=False)
            for network in reversed(networks):docker('network','rm',network,check=False)


if __name__=='__main__':main()
