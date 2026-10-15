#!/usr/bin/env python3
"""PCBridge: transfer files and text between computers on the same LAN.

Run on each machine:   python3 pcbridge.py [--name NAME] [--token SECRET]
Then open:             http://localhost:8765

CLI shortcuts (with the server running on another machine):
    python3 pcbridge.py send FILE... [--to HOST]
    python3 pcbridge.py text "some text" [--to HOST]
"""
import argparse
import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTTP_PORT = 8765
DISCOVERY_PORT = 8766
CHUNK = 1024 * 1024
INBOX_DIR = Path.home() / "PCBridge"

peers = {}  # "ip:port" -> {"name", "last_seen"}
inbox = []  # newest first: {"type", "from", "time", "text"|"file", "size"}
lock = threading.Lock()
config = {"name": socket.gethostname(), "token": ""}


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return ips


# ---------- Discovery (UDP broadcast) ----------

def announce_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = json.dumps({"app": "pcbridge", "name": config["name"], "port": HTTP_PORT}).encode()
    while True:
        try:
            sock.sendto(msg, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass
        time.sleep(3)


def listen_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", DISCOVERY_PORT))
    while True:
        data, (ip, _) = sock.recvfrom(4096)
        try:
            info = json.loads(data)
        except ValueError:
            continue
        if info.get("app") != "pcbridge" or ip in local_ips() or ip.startswith("127."):
            continue
        with lock:
            peers[f"{ip}:{info['port']}"] = {"name": info["name"], "last_seen": time.time()}


def active_peers():
    now = time.time()
    with lock:
        return [{"addr": a, "name": p["name"]} for a, p in peers.items() if now - p["last_seen"] < 10]


# ---------- Sending ----------

def unique_path(name):
    name = os.path.basename(name) or "file"
    path = INBOX_DIR / name
    stem, suffix, i = path.stem, path.suffix, 1
    while path.exists():
        path = INBOX_DIR / f"{stem} ({i}){suffix}"
        i += 1
    return path


def forward(addr, path, headers, body_iter, length):
    """POST a (possibly streamed) body to a peer."""
    host, port = addr.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=30)
    headers = {**headers, "Content-Length": str(length), "X-Token": config["token"],
               "X-From": config["name"]}
    conn.putrequest("POST", path)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    for chunk in body_iter:
        conn.send(chunk)
    resp = conn.getresponse()
    out = resp.read()
    conn.close()
    if resp.status != 200:
        raise RuntimeError(f"peer responded {resp.status}: {out.decode(errors='replace')}")


def send_file(addr, filepath):
    size = os.path.getsize(filepath)

    def chunks():
        with open(filepath, "rb") as f:
            while chunk := f.read(CHUNK):
                yield chunk

    name = urllib.parse.quote(os.path.basename(filepath))
    forward(addr, "/receive/file", {"X-Filename": name}, chunks(), size)


def send_text(addr, text):
    data = text.encode()
    forward(addr, "/receive/text", {"Content-Type": "text/plain; charset=utf-8"}, [data], len(data))


# ---------- HTTP server ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, status=200, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away (e.g. browser cancelled a request)

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def body_chunks(self):
        remaining = int(self.headers.get("Content-Length", 0))
        while remaining > 0:
            chunk = self.rfile.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        # UI and inbox are only available from this machine itself
        if not self.is_local():
            return self.reply(403, {"error": "local only"})
        if url.path == "/favicon.ico":
            return self.reply(204)
        if url.path == "/":
            return self.reply(200, UI_HTML, "text/html; charset=utf-8")
        if url.path == "/api/state":
            with lock:
                items = list(inbox)
            return self.reply(200, {"name": config["name"], "peers": active_peers(),
                                    "inbox": items, "dir": str(INBOX_DIR)})
        if url.path.startswith("/files/"):
            name = os.path.basename(urllib.parse.unquote(url.path[7:]))
            path = INBOX_DIR / name
            if not path.is_file():
                return self.reply(404, {"error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(CHUNK):
                    self.wfile.write(chunk)
            return
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            # --- incoming from a peer ---
            if url.path.startswith("/receive/"):
                if config["token"] and self.headers.get("X-Token") != config["token"]:
                    return self.reply(401, {"error": "bad token"})
                sender = self.headers.get("X-From", self.client_address[0])
                if url.path == "/receive/text":
                    text = b"".join(self.body_chunks()).decode("utf-8", errors="replace")
                    entry = {"type": "text", "text": text}
                else:
                    path = unique_path(urllib.parse.unquote(self.headers.get("X-Filename", "file")))
                    with open(path, "wb") as f:
                        for chunk in self.body_chunks():
                            f.write(chunk)
                    entry = {"type": "file", "file": path.name, "size": path.stat().st_size}
                    print(f"Received {path}")
                entry.update({"from": sender, "time": time.time()})
                with lock:
                    inbox.insert(0, entry)
                    del inbox[200:]
                return self.reply(200, {"ok": True})

            # --- outgoing, requested by the local UI ---
            if not self.is_local():
                return self.reply(403, {"error": "local only"})
            addr = qs.get("to", [""])[0]
            if not addr:
                return self.reply(400, {"error": "missing peer"})
            if url.path == "/api/send/text":
                send_text(addr, b"".join(self.body_chunks()).decode("utf-8"))
                return self.reply(200, {"ok": True})
            if url.path == "/api/send/file":
                length = int(self.headers.get("Content-Length", 0))
                forward(addr, "/receive/file", {"X-Filename": self.headers.get("X-Filename", "file")},
                        self.body_chunks(), length)
                return self.reply(200, {"ok": True})
            self.reply(404, {"error": "not found"})
        except Exception as e:  # report any transfer failure to the UI
            self.reply(502, {"error": str(e)})


UI_HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PCBridge</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1d2330;--mut:#6b7280;--acc:#2563eb;--bd:#e3e6eb}
@media (prefers-color-scheme:dark){:root{--bg:#14161b;--card:#1d2027;--fg:#e8eaee;--mut:#9aa1ad;--acc:#5b8cff;--bd:#2c3039}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:760px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}.mut{color:var(--mut);font-size:13px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-top:16px}
h2{font-size:15px;margin:0 0 10px}
select,textarea,button{font:inherit;color:inherit}
select,textarea{width:100%;background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:8px}
textarea{min-height:90px;resize:vertical}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer}
button.ghost{background:transparent;color:var(--acc);border:1px solid var(--bd);padding:4px 10px;font-size:13px}
#drop{border:2px dashed var(--bd);border-radius:10px;padding:28px;text-align:center;color:var(--mut);cursor:pointer}
#drop.over{border-color:var(--acc);color:var(--acc)}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-top:8px}
.item{border-top:1px solid var(--bd);padding:10px 0}.item:first-child{border-top:0}
.item pre{white-space:pre-wrap;word-break:break-word;margin:6px 0;font:13px/1.4 ui-monospace,monospace}
#status{min-height:20px;font-size:13px;margin-top:8px}
progress{width:100%}
</style></head><body><main>
<h1>PCBridge</h1><div class="mut" id="me"></div>
<div class="card"><h2>Destino</h2>
<select id="peer"></select><div class="mut" id="nopeer"></div></div>
<div class="card"><h2>Texto</h2>
<textarea id="text" placeholder="Pegá texto acá… (Ctrl/Cmd+Enter para enviar)"></textarea>
<div class="row"><span></span><button id="sendText">Enviar texto</button></div></div>
<div class="card"><h2>Archivos</h2>
<div id="drop">Arrastrá archivos acá o hacé click para elegir</div>
<input type="file" id="file" multiple hidden>
<progress id="prog" value="0" max="1" hidden></progress>
<div id="status"></div></div>
<div class="card"><h2>Recibido</h2><div id="inbox" class="mut">Nada todavía.</div></div>
</main><script>
const $=id=>document.getElementById(id);
let lastInbox="";
const fmt=n=>n<1024?n+" B":n<1048576?(n/1024).toFixed(1)+" KB":n<1073741824?(n/1048576).toFixed(1)+" MB":(n/1073741824).toFixed(2)+" GB";
const esc=s=>s.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function status(msg,err){$("status").textContent=msg;$("status").style.color=err?"#e5484d":""}
async function refresh(){
  const s=await (await fetch("/api/state")).json();
  $("me").textContent=`Este equipo: ${s.name} · archivos recibidos en ${s.dir}`;
  const sel=$("peer"),cur=sel.value;
  sel.innerHTML=s.peers.map(p=>`<option value="${p.addr}">${esc(p.name)} (${p.addr})</option>`).join("");
  if(cur&&s.peers.some(p=>p.addr===cur))sel.value=cur;
  $("nopeer").textContent=s.peers.length?"":"Buscando otros equipos en la red… (¿está corriendo PCBridge en la otra PC?)";
  const key=JSON.stringify(s.inbox);if(key===lastInbox)return;lastInbox=key;
  $("inbox").innerHTML=s.inbox.length?s.inbox.map((e,i)=>`<div class="item">
    <div class="row" style="margin:0"><span class="mut">${esc(e.from)} · ${new Date(e.time*1000).toLocaleTimeString()}</span>
    ${e.type==="text"?`<button class="ghost" data-copy="${i}">Copiar</button>`:`<a href="/files/${encodeURIComponent(e.file)}"><button class="ghost">Descargar</button></a>`}</div>
    ${e.type==="text"?`<pre>${esc(e.text)}</pre>`:`<div>📄 ${esc(e.file)} <span class="mut">(${fmt(e.size)})</span></div>`}</div>`).join(""):"Nada todavía.";
  document.querySelectorAll("[data-copy]").forEach(b=>b.onclick=async()=>{
    await navigator.clipboard.writeText(s.inbox[b.dataset.copy].text);b.textContent="¡Copiado!";setTimeout(()=>b.textContent="Copiar",1200)});
}
async function sendText(){
  const t=$("text").value;if(!t||!$("peer").value)return;
  const r=await fetch("/api/send/text?to="+$("peer").value,{method:"POST",body:t});
  if(r.ok){$("text").value="";status("Texto enviado ✓")}else status("Error: "+(await r.json()).error,1);
}
function sendFile(f){return new Promise((res,rej)=>{
  const x=new XMLHttpRequest();x.open("POST","/api/send/file?to="+$("peer").value);
  x.setRequestHeader("X-Filename",encodeURIComponent(f.name));
  x.upload.onprogress=e=>{$("prog").value=e.loaded/e.total};
  x.onload=()=>x.status===200?res():rej(JSON.parse(x.responseText).error);
  x.onerror=()=>rej("error de red");x.send(f);
})}
async function sendFiles(files){
  if(!$("peer").value)return status("No hay destino",1);
  $("prog").hidden=false;
  for(const f of files){
    status(`Enviando ${f.name} (${fmt(f.size)})…`);
    try{await sendFile(f);status(`${f.name} enviado ✓`)}catch(e){status(`Error con ${f.name}: ${e}`,1);break}
  }
  $("prog").hidden=true;$("prog").value=0;
}
$("sendText").onclick=sendText;
$("text").onkeydown=e=>{if(e.key==="Enter"&&(e.ctrlKey||e.metaKey))sendText()};
const drop=$("drop");
drop.onclick=()=>$("file").click();
$("file").onchange=e=>{sendFiles([...e.target.files]);e.target.value=""};
drop.ondragover=e=>{e.preventDefault();drop.classList.add("over")};
drop.ondragleave=()=>drop.classList.remove("over");
drop.ondrop=e=>{e.preventDefault();drop.classList.remove("over");sendFiles([...e.dataTransfer.files])};
refresh();setInterval(refresh,2000);
</script></body></html>"""


# ---------- CLI ----------

def resolve_target(to):
    if to:
        return to if ":" in to else f"{to}:{HTTP_PORT}"
    threading.Thread(target=listen_loop, daemon=True).start()
    print("Buscando equipos…")
    for _ in range(8):
        time.sleep(0.5)
        if found := active_peers():
            print(f"→ {found[0]['name']} ({found[0]['addr']})")
            return found[0]["addr"]
    sys.exit("No se encontró ningún equipo. Usá --to IP")


def main():
    ap = argparse.ArgumentParser(description="PCBridge: compartir archivos y texto en la red local")
    ap.add_argument("--name", help="nombre de este equipo")
    ap.add_argument("--token", default=os.environ.get("PCBRIDGE_TOKEN", ""),
                    help="clave compartida (igual en ambos equipos)")
    ap.add_argument("--to", help="IP del destino (CLI)")
    ap.add_argument("cmd", nargs="?", choices=["serve", "send", "text"], default="serve")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    config["token"] = a.token
    if a.name:
        config["name"] = a.name

    if a.cmd == "send":
        addr = resolve_target(a.to)
        for f in a.args:
            send_file(addr, f)
            print(f"✓ {f}")
        return
    if a.cmd == "text":
        addr = resolve_target(a.to)
        send_text(addr, " ".join(a.args) if a.args else sys.stdin.read())
        print("✓ texto enviado")
        return

    INBOX_DIR.mkdir(exist_ok=True)
    threading.Thread(target=announce_loop, daemon=True).start()
    threading.Thread(target=listen_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"PCBridge '{config['name']}' listo → abrí http://localhost:{HTTP_PORT}")
    print(f"Archivos recibidos en {INBOX_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
