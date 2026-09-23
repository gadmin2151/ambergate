"""Real Certbot + Pebble HTTP-01 issuance and renewal; no public CA or domains.

Run on a Docker host after building ambergate:ci. All resources are disposable.
"""
import http.client
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request
import uuid

IMAGE = os.environ.get('AMBERGATE_TEST_IMAGE', 'ambergate:ci')
PEBBLE = 'ghcr.io/letsencrypt/pebble@sha256:ddf230642b1a584f519f32e347de1b05a6e4c1f6c35c1863b33effeab5f78199'
PREFIX = 'ambergate-acme-' + uuid.uuid4().hex[:8]
DOMAIN = 'acme.ambergate.test'
PASSWORD = 'acme-disposable-test-password'
OWNED = []


def docker(*args):
    p = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=180)
    if p.returncode: raise RuntimeError(p.stderr[-3000:])
    return p.stdout.strip()


def port(name, private):
    return int(json.loads(docker('inspect', name))[0]['NetworkSettings']['Ports'][f'{private}/tcp'][0]['HostPort'])


def eventually(fn, seconds=160):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            result = fn()
            if result: return result
        except (OSError, ValueError): pass
        time.sleep(1)
    raise AssertionError('Timed out waiting for ACME/HTTPS')


def main():
    network = False
    try:
        with tempfile.TemporaryDirectory(prefix=PREFIX) as tmp:
            root = Path(tmp)
            docker('run', '--rm', '-v', f'{root}:/work', '--entrypoint', 'openssl', IMAGE,
                   'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                   '-keyout', '/work/key.pem', '-out', '/work/cert.pem', '-days', '1', '-subj', '/CN=pebble',
                   '-addext', 'subjectAltName=DNS:pebble,IP:127.0.0.1')
            (root/'pebble.json').write_text(json.dumps({'pebble':{
                'listenAddress':'0.0.0.0:14000','managementListenAddress':'0.0.0.0:15000',
                'certificate':'/work/cert.pem','privateKey':'/work/key.pem','httpPort':80,'tlsPort':443,
                'externalAccountBindingRequired':False,
                'profiles':{'default':{'description':'One-day test certificate','validityPeriod':86400}}}}))
            docker('network', 'create', PREFIX);network=True
            ca = docker('run','-d','--name',PREFIX+'-ca','--network',PREFIX,'--network-alias','pebble',
                        '-p','127.0.0.1::15000','-e','PEBBLE_VA_NOSLEEP=1','-e','PEBBLE_WFE_NONCEREJECT=0',
                        '-v',f'{root}:/work:ro',PEBBLE,'-config','/work/pebble.json');OWNED.append(ca)
            gateway_args=['run','-d','--name',PREFIX+'-gw','--network',PREFIX,'--network-alias',DOMAIN,
                '-p','127.0.0.1::8083','-p','127.0.0.1::443','-v',f'{root}/cert.pem:/test-ca.pem:ro',
                '-e','REQUESTS_CA_BUNDLE=/test-ca.pem','-e','AMBERGATE_ACME_SERVER=https://pebble:14000/dir',
                '-e','AMBERGATE_ADMIN_PASSWORD='+PASSWORD]
            # Optional development source mount; CI tests the unmodified image.
            if os.environ.get('AMBERGATE_TEST_SOURCE'):
                gateway_args += ['-v', os.environ['AMBERGATE_TEST_SOURCE']+':/app/ambergate:ro']
            gateway=docker(*gateway_args, IMAGE);OWNED.append(gateway)
            base='http://127.0.0.1:'+str(port(gateway,8083))
            client=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            csrf=''
            def api(path,body=None):
                req=urllib.request.Request(base+'/api/'+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json','X-CSRF-Token':csrf})
                with client.open(req,timeout=5) as r:return json.load(r)
            eventually(lambda: client.open(base+'/healthz',timeout=2).status==200)
            csrf=api('login',{'password':PASSWORD})['csrf']
            snap=api('config');c=snap['config']
            c['hosts']=[{'id':'acme-test','domain':DOMAIN,'enabled':True,'routes':[],
                'tls':{'enabled':True,'email':'ci@example.com','renew_before_days':5,'redirect_http':True,'terms_accepted':True}}]
            snap=api('config',{'config':c,'revision':snap['revision']});api('apply',{'revision':snap['revision']})
            def ready():
                status=api('dashboard')['certificates']['acme-test']
                if status['status']=='error':raise AssertionError(status['error'])
                return status['status']=='ready'
            eventually(ready)
            print('PASS: real HTTP-01 issuance', flush=True)
            first=json.loads(docker('exec',gateway,'python3','-c',
                'from pathlib import Path;from ambergate.tls import certificate_info;import json;print(json.dumps(certificate_info(Path("/data/tls"),"'+DOMAIN+'")))'))
            # Verify application HTTPS against the test CA, including SNI/name verification.
            ca_context=ssl.create_default_context(cafile=str(root/'cert.pem'))
            with urllib.request.urlopen(f'https://127.0.0.1:{port(ca,15000)}/roots/0',context=ca_context,timeout=5) as r:
                context=ssl.create_default_context(cadata=r.read().decode())
            with socket.create_connection(('127.0.0.1',port(gateway,443))) as raw:
                with context.wrap_socket(raw,server_hostname=DOMAIN) as secure:
                    secure.sendall(f'GET / HTTP/1.1\r\nHost: {DOMAIN}\r\nConnection: close\r\n\r\n'.encode())
                    response=http.client.HTTPResponse(secure);response.begin();assert response.status==404
            # A one-day certificate is inside the configured five-day window.
            # Clear only the test cooldown, then restart to exercise renewal recovery.
            docker('stop',gateway)
            docker('run','--rm','--volumes-from',gateway,'--entrypoint','python3',IMAGE,'-c',
                'from pathlib import Path;import json;p=Path("/data/tls/status.json");s=json.loads(p.read_text());'
                '[v.update(next_attempt=0) for v in s.values()];p.write_text(json.dumps(s))')
            docker('start',gateway)
            base='http://127.0.0.1:'+str(port(gateway,8083))
            eventually(lambda: client.open(base+'/healthz',timeout=2).status==200)
            csrf=api('login',{'password':PASSWORD})['csrf']
            def renewed():
                info=json.loads(docker('exec',gateway,'python3','-c',
                    'from ambergate.tls import certificate_info;import json;print(json.dumps(certificate_info("/data/tls","'+DOMAIN+'")))'))
                return info['version']!=first['version'] and ready()
            eventually(renewed)
            print('PASS: real Certbot HTTP-01 issuance, verified HTTPS, renewal inside the five-day window after restart')
    finally:
        for name in reversed(OWNED):
            subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if network:subprocess.run(['docker','network','rm',PREFIX],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':main()
