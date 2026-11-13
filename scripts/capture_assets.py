"""Temporary local UI for saving browser screenshots to the documentation folder."""
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re

ROOT=Path('docs/screenshots');ROOT.mkdir(parents=True,exist_ok=True)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page='''<!doctype html><meta charset=utf-8><title>Сохранение кадров</title><label>Название кадра <input id=name value=landing></label><p><label>Изображение <textarea id=data></textarea></label></p><button id=save>Сохранить кадр</button><p id=result></p><script>save.onclick=async()=>{let r=await fetch('/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('name').value,data:document.getElementById('data').value})});document.getElementById('result').textContent=await r.text()}</script>'''
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(page.encode())
    def do_POST(self):
        try:
            if self.headers.get('Origin')!='http://127.0.0.1:8020':raise ValueError('Origin')
            length=int(self.headers.get('Content-Length','0'))
            if not 1<=length<=3000000:raise ValueError('Size')
            value=json.loads(self.rfile.read(length));name=value['name']
            if not re.fullmatch('[a-z0-9_-]{1,40}',name):raise ValueError('Name')
            data=b64decode(value['data'].split(',',1)[1],validate=True)
            if not data.startswith(b'\xff\xd8\xff'):raise ValueError('JPEG required')
            file=ROOT/(name+'.jpg');file.write_bytes(data)
            self.send_response(200);self.end_headers();self.wfile.write(('Сохранено: '+str(file)).encode())
        except (ValueError,KeyError):self.send_response(400);self.end_headers()
HTTPServer(('127.0.0.1',8020),Handler).serve_forever()
