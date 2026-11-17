import json
import logging
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .db import dumps
from .demo import seed
from .errors import AppError

STATIC=Path(__file__).parent/"static"
log=logging.getLogger(__name__)


def make_server(service):
    class Handler(BaseHTTPRequestHandler):
        server_version="QualityGate/1.0"

        def log_message(self,fmt,*args):
            log.info("%s %s",self.address_string(),fmt%args)

        def send(self,value,status=200,content_type="application/json; charset=utf-8",attachment=None):
            raw=value if isinstance(value,bytes) else dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",content_type)
            self.send_header("Content-Length",str(len(raw)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("Referrer-Policy","no-referrer")
            if attachment:
                self.send_header("Content-Disposition",f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(raw)

        def guard(self):
            port=self.server.server_address[1]
            hosts={f"127.0.0.1:{port}",f"localhost:{port}"}
            if self.headers.get("Host","").lower() not in hosts:
                raise AppError("仅允许本机访问",403)
            origin=self.headers.get("Origin")
            if origin and origin not in {f"http://{host}" for host in hosts}:
                raise AppError("拒绝跨来源请求",403)
            if self.headers.get("Sec-Fetch-Site")=="cross-site":
                raise AppError("拒绝跨站请求",403)

        def body(self,raw=False):
            if self.headers.get("Transfer-Encoding"):
                raise AppError("不支持分块请求",400)
            try:
                length=int(self.headers.get("Content-Length","0"))
            except ValueError:
                raise AppError("无效的 Content-Length") from None
            if not 0 < length <= service.config.max_bytes:
                raise AppError("请求为空或超过4 MiB",413)
            if not raw and self.headers.get("Content-Type","").split(";",1)[0]!="application/json":
                raise AppError("请使用 application/json",415)
            content=self.rfile.read(length)
            if len(content)!=length:
                raise AppError("请求体不完整")
            if raw:
                return content
            try:
                obj=json.loads(content,parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限数值")))
            except (ValueError,UnicodeDecodeError):
                raise AppError("JSON 格式无效") from None
            if not isinstance(obj,dict):
                raise AppError("请求体必须为 JSON 对象")
            return obj

        def do_GET(self): self.dispatch("GET")
        def do_POST(self): self.dispatch("POST")

        def dispatch(self,method):
            try:
                self.connection.settimeout(15)
                self.guard()
                url=urlsplit(self.path)
                path=url.path
                query=parse_qs(url.query)
                def q(key,default=""): return query.get(key,[default])[0]
                def integer(key,default,low,high):
                    try: value=int(q(key,str(default)))
                    except ValueError: raise AppError(f"{key} 必须为整数") from None
                    if not low<=value<=high: raise AppError(f"{key} 超出范围")
                    return value
                if method=="GET" and path=="/api/health":
                    self.send({"status":"ok","version":"1.0.0","mode":"local","worker_alive":bool(getattr(self.server,"worker",None) and self.server.worker.thread.is_alive())}); return
                if path=="/api/datasets":
                    if method=="GET": self.send(service.datasets())
                    else: self.send(service.upload(self.body(raw=True),q("filename","upload.csv")),201)
                    return
                match=re.fullmatch(r"/api/datasets/([a-f0-9]{32})",path)
                if method=="GET" and match:
                    self.send(service.dataset(match[1])); return
                if path=="/api/rulesets":
                    self.send(service.rulesets() if method=="GET" else service.save_ruleset(self.body()),200 if method=="GET" else 201); return
                match=re.fullmatch(r"/api/rulesets/([a-f0-9]{32})",path)
                if match:
                    self.send(service.ruleset(match[1]) if method=="GET" else service.save_ruleset(self.body(),match[1])); return
                if path=="/api/jobs":
                    if method=="GET": self.send(service.jobs(q("state") or None,integer("offset",0,0,1000000),integer("limit",50,1,100)))
                    else: self.send(service.enqueue(self.body(),self.headers.get("Idempotency-Key")),202)
                    return
                match=re.fullmatch(r"/api/jobs/([a-f0-9]{32})(?:/(issues|cancel|retry|export))?",path)
                if match:
                    jid,action=match[1],match[2]
                    if method=="GET" and not action: self.send(service.job(jid)); return
                    if method=="GET" and action=="issues":
                        self.send(service.issues(jid,q("severity") or None,q("rule_id") or None,q("search")[:200],integer("offset",0,0,1000000),integer("limit",50,1,100))); return
                    if method=="POST" and action in ("cancel","retry"):
                        self.body()
                        self.send(service.cancel(jid) if action=="cancel" else service.retry(jid),200 if action=="cancel" else 202); return
                    if method=="GET" and action=="export":
                        job=service.job(jid)
                        if job["state"]!="succeeded": raise AppError("任务尚未完成",409)
                        if q("format","csv")=="json":
                            self.send({"job":job,"issues":service.issues(jid,limit=service.config.max_issues)["items"]},attachment=f"qualitygate-{jid[:8]}.json")
                        elif q("format","csv")=="csv":
                            self.send(service.export_csv(jid),content_type="text/csv; charset=utf-8",attachment=f"qualitygate-{jid[:8]}.csv")
                        else: raise AppError("导出格式仅支持 csv 或 json")
                        return
                if method=="POST" and path=="/api/demo":
                    self.body(); self.send(seed(service)); return
                assets={"/":"index.html","/app.js":"app.js","/style.css":"style.css"}
                if method=="GET" and path in assets:
                    file=STATIC/assets[path]
                    mime={".html":"text/html",".css":"text/css",".js":"text/javascript"}[file.suffix]
                    self.send(file.read_bytes(),content_type=mime+"; charset=utf-8"); return
                raise AppError("接口不存在",404)
            except AppError as exc:
                self.send({"error":str(exc)},exc.status)
            except (BrokenPipeError,ConnectionResetError,TimeoutError):
                self.close_connection=True
            except Exception:
                log.exception("Request failed")
                self.send({"error":"服务内部错误，请查看终端日志"},500)
    server=ThreadingHTTPServer((service.config.host,service.config.port),Handler)
    server.daemon_threads=True
    return server
