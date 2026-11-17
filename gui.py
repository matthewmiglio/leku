#!/usr/bin/env python
"""gui — a local web UI over leku.py: search, watch list, download, browse.

    python gui.py           # -> http://localhost:8596

State (watch list, chosen folder, per-title download status) lives in
gui_data.json next to this file. Downloads run one-at-a-time through a single
background worker (parallel pulls trip the provider's captcha gate); yt-dlp's
live percent is parsed back for the progress bars.
"""
import json, os, pathlib, queue, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import leku

HERE = pathlib.Path(__file__).parent
STATE = HERE / "gui_data.json"
PORT = 8596
IMG = "https://image.tmdb.org/t/p/w342"   # same CDN/size the CLI search uses

_lock = threading.Lock()
JOBS = {}          # jid -> {id, key, label, status, pct, msg}
PAY = {}           # jid -> {item, season, episode, folder}
Q = queue.Queue()
_seq = [0]


# --- persistent state -------------------------------------------------------

def load_state():
    try:
        s = json.loads(STATE.read_text("utf-8"))
    except (OSError, ValueError):
        s = {}
    s.setdefault("watchlist", [])
    s.setdefault("folder", str(HERE / "downloads"))
    s.setdefault("status", {})     # key -> {state, msg, file}
    return s


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2), "utf-8")


def set_status(key, **fields):
    with _lock:
        s = load_state()
        s["status"][key] = {**s["status"].get(key, {}), **fields}
        save_state(s)


def unit_key(item, season, episode):
    if item["type"] == "tv":
        return f"tv:{item['id']}:{season}:{episode}"
    return f"movie:{item['id']}"


def st_verified(folder, st):
    """Return the status dict only if a 'downloaded' file still exists."""
    if st and st.get("state") == "downloaded":
        f = st.get("file")
        if f and not (pathlib.Path(folder) / f).exists():
            return None
    return st or None


# --- search -----------------------------------------------------------------

def search(query, mtype):
    import urllib.parse
    q = urllib.parse.urlencode({"api_key": leku.TMDB_KEY, "query": query})
    data = leku._get(f"{leku.TMDB}/search/{mtype}?{q}")
    out = []
    for r in data.get("results", []):
        t = r.get("media_type", mtype if mtype != "multi" else "?")
        if t == "person":
            continue
        out.append({
            "id": r["id"], "type": t,
            "title": r.get("title") or r.get("name") or "?",
            "year": (r.get("release_date") or r.get("first_air_date") or "")[:4],
            "poster": (IMG + r["poster_path"]) if r.get("poster_path") else "",
        })
    return out


# --- download worker --------------------------------------------------------

def _mp4s(folder):
    try:
        return {p.name for p in pathlib.Path(folder).glob("*.mp4")}
    except OSError:
        return set()


def _process(jid):
    job, pay = JOBS[jid], PAY[jid]
    item, s, e, folder = pay["item"], pay["season"], pay["episode"], pay["folder"]
    key = job["key"]

    def setj(**k):
        with _lock:
            job.update(k)

    def prog(p):
        with _lock:
            job.update(status="running", pct=p)

    try:
        setj(status="running", pct=0, msg="")
        before = _mp4s(folder)
        if item["type"] == "tv":
            rc = leku.download(str(item["id"]), s, e, out=folder, on_progress=prog)
        else:
            rc = leku.download(str(item["id"]), None, None, out=folder,
                               on_progress=prog)
        new = _mp4s(folder) - before
        meta = {k: item.get(k) for k in ("id", "type", "title", "year", "poster")}
        fname = None
        for name in new:
            fname = name
            try:
                (pathlib.Path(folder) / (name + ".leku.json")).write_text(
                    json.dumps(meta), "utf-8")
            except OSError:
                pass
        if rc == 0:
            set_status(key, state="downloaded", file=fname, msg="")
            setj(status="done", pct=100)
        else:
            setj(status="failed", msg="yt-dlp returned nonzero (see terminal)")
    except leku.GatedError as ex:
        setj(status="failed", msg=f"captcha-gated ({ex.reason}); open {ex.embed} "
                                  "in a browser, solve it, then retry")
    except RuntimeError as ex:
        setj(status="failed", msg=str(ex))
        if "status 404" in str(ex) or "doesn't have this title" in str(ex):
            # genuinely missing from the library: remember it across restarts
            set_status(key, state="failed_missing", msg=str(ex))
    except Exception as ex:
        traceback.print_exc()
        setj(status="failed", msg=str(ex))     # transient: not persisted


def _worker():
    while True:
        jid = Q.get()
        try:
            _process(jid)
        except Exception:
            traceback.print_exc()
        finally:
            Q.task_done()


def enqueue(item, units, folder):
    ids = []
    for u in units:
        s, e = u.get("season") or None, u.get("episode") or None
        _seq[0] += 1
        jid = _seq[0]
        key = unit_key(item, s, e)
        label = item["title"] + (f" S{s}E{e}" if item["type"] == "tv" else "")
        with _lock:
            JOBS[jid] = {"id": jid, "key": key, "label": label,
                         "status": "queued", "pct": 0, "msg": ""}
            PAY[jid] = {"item": item, "season": s, "episode": e, "folder": folder}
        Q.put(jid)
        ids.append(jid)
    return ids


# --- http -------------------------------------------------------------------

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or "{}")

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        s = load_state()
        folder = s["folder"]
        if p == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if p == "/api/search":
            try:
                return self._send(200, search(qs.get("q", [""])[0],
                                               qs.get("type", ["multi"])[0]))
            except Exception as ex:
                return self._send(502, {"error": str(ex)})
        if p == "/api/watchlist":
            out = []
            for x in s["watchlist"]:
                y = dict(x)
                if x["type"] == "tv":
                    y["dlcount"] = sum(
                        1 for k, v in s["status"].items()
                        if k.startswith(f"tv:{x['id']}:")
                        and st_verified(folder, v)
                        and v.get("state") == "downloaded")
                else:
                    y["status"] = st_verified(folder,
                                              s["status"].get(f"movie:{x['id']}"))
                out.append(y)
            return self._send(200, out)
        if p == "/api/episodes":
            tid = qs.get("id", [""])[0]
            try:
                seasons = []
                for sn, n in leku._tv_season_map(tid):
                    eps = []
                    for e in range(1, n + 1):
                        st = st_verified(folder, s["status"].get(f"tv:{tid}:{sn}:{e}"))
                        eps.append({"e": e,
                                    "state": (st or {}).get("state"),
                                    "msg": (st or {}).get("msg")})
                    seasons.append({"season": sn, "episodes": eps})
                return self._send(200, {"seasons": seasons})
            except Exception as ex:
                return self._send(502, {"error": str(ex)})
        if p == "/api/settings":
            return self._send(200, {"folder": folder})
        if p == "/api/jobs":
            with _lock:
                return self._send(200, list(JOBS.values()))
        if p == "/api/downloaded":
            files = []
            try:
                for f in pathlib.Path(folder).iterdir():
                    if not f.is_file() or f.suffix != ".mp4":
                        continue
                    rec = {"name": f.name,
                           "mb": round(f.stat().st_size / 1e6, 1),
                           "poster": "", "id": None, "type": None}
                    side = f.with_name(f.name + ".leku.json")
                    if side.exists():
                        try:
                            rec.update(json.loads(side.read_text("utf-8")))
                            rec["name"] = f.name
                        except (OSError, ValueError):
                            pass
                    files.append(rec)
                files.sort(key=lambda x: x["name"])
            except OSError:
                pass
            return self._send(200, {"folder": folder, "files": files})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        d = self._body()
        s = load_state()
        if p == "/api/watchlist":
            if not any(w["id"] == d["id"] and w["type"] == d["type"]
                       for w in s["watchlist"]):
                s["watchlist"].append({k: d.get(k) for k in
                                       ("id", "type", "title", "year", "poster")})
                save_state(s)
            return self._send(200, {"ok": True})
        if p == "/api/watchlist/remove":
            s["watchlist"] = [w for w in s["watchlist"]
                              if not (w["id"] == d["id"] and w["type"] == d["type"])]
            save_state(s)
            return self._send(200, {"ok": True})
        if p == "/api/settings":
            s["folder"] = d["folder"].strip() or s["folder"]
            save_state(s)
            os.makedirs(s["folder"], exist_ok=True)
            return self._send(200, {"folder": s["folder"]})
        if p == "/api/enqueue":
            ids = enqueue(d["item"], d.get("units") or [{}], s["folder"])
            return self._send(200, {"ids": ids})
        return self._send(404, {"error": "not found"})


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>leku</title>
<style>
:root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--fg:#e7e9ee;--mut:#8b93a3;
--acc:#5b9cff;--ok:#4ccb7a;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.4 system-ui,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;
gap:18px;align-items:center}header b{font-size:18px}nav{display:flex;gap:4px}
nav button{background:none;border:0;color:var(--mut);padding:8px 14px;cursor:pointer;
border-radius:8px;font:inherit}nav button.on{background:var(--card);color:var(--fg)}
main{padding:20px;max-width:1100px;margin:0 auto}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center}
input,select{background:var(--card);border:1px solid var(--line);color:var(--fg);
padding:9px 11px;border-radius:8px;font:inherit}input{flex:1;min-width:180px}
button.b{background:var(--acc);border:0;color:#fff;padding:9px 16px;border-radius:8px;
cursor:pointer;font:inherit}button.g{background:var(--card);border:1px solid var(--line);
color:var(--fg)}button.red{background:var(--bad);border:0;color:#fff}
button:disabled{opacity:.7;cursor:default}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
overflow:hidden;display:flex;flex-direction:column}
.card img{width:100%;aspect-ratio:2/3;object-fit:cover;background:#000;display:block}
.card .p{padding:10px;display:flex;flex-direction:column;flex:1}
.card .t{font-weight:600;font-size:14px}
.card .m{color:var(--mut);font-size:12px;margin:2px 0 8px}
.card .act{margin-top:auto}.card .act button{width:100%;margin-top:6px}
.tag{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.bar{position:relative;height:36px;border-radius:8px;overflow:hidden;
background:var(--line);margin-top:6px}
.bar>i{position:absolute;inset:0;background:var(--acc);transition:width .3s}
.bar>span{position:absolute;inset:0;display:flex;align-items:center;
justify-content:center;font-size:13px;font-weight:600}
.file{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:8px 12px;margin-bottom:6px;display:flex;justify-content:space-between}
.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}
.empty{color:var(--mut);padding:30px 0}
.ep{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
.ep .lbl{width:70px;font-variant-numeric:tabular-nums}
.ep .grow{flex:1}.ep button{padding:6px 12px}
.seas{margin:18px 0}.seas h3{margin:0 0 6px;display:flex;justify-content:space-between;align-items:center}
.msg{font-size:12px;margin-top:4px}
</style></head><body>
<header><b>leku</b><nav id=nav></nav></header>
<main id=main></main>
<script>
const $=s=>document.querySelector(s),el=h=>{const d=document.createElement('div');d.innerHTML=h;return d.firstElementChild}
const TABS=['Search','Watch list','Files on disk','Settings']
let tab='Search', showEps=null, poll=null
const api=(u,m,b)=>fetch(u,m?{method:m,headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{}).then(r=>r.json())
const esc=s=>(s||'').replace(/"/g,'&quot;')

function nav(){$('#nav').innerHTML='';TABS.forEach(t=>{const b=el(`<button class="${t==tab?'on':''}">${t}</button>`);b.onclick=()=>{tab=t;showEps=null;render()};$('#nav').append(b)})}
function stopPoll(){if(poll){clearInterval(poll);poll=null}}
function startPoll(fn){stopPoll();poll=setInterval(fn,1500)}

async function render(){nav();stopPoll();const m=$('#main');m.innerHTML='loading…'
 if(tab=='Search')return renderSearch(m)
 if(tab=='Watch list')return showEps?renderEps(m):renderWatch(m)
 if(tab=='Files on disk')return renderFiles(m)
 if(tab=='Settings')return renderSettings(m)}

function posterTag(x){return x.poster?`<img src="${x.poster}">`:`<img alt="">`}

// ---- Search
function renderSearch(m){
 m.innerHTML=`<div class=row><input id=q placeholder="Search movies & TV…" autofocus>
 <select id=ty><option value=multi>All</option><option value=movie>Movies</option><option value=tv>TV</option></select>
 <button class=b id=go>Search</button></div><div class=grid id=res></div>`
 const run=async()=>{const q=$('#q').value.trim();if(!q)return
  $('#res').innerHTML='searching…'
  const r=await api('/api/search?q='+encodeURIComponent(q)+'&type='+$('#ty').value)
  if(r.error){$('#res').innerHTML='<div class=empty>'+r.error+'</div>';return}
  if(!r.length){$('#res').innerHTML='<div class=empty>No results.</div>';return}
  $('#res').innerHTML=''
  r.forEach(x=>{const c=el(`<div class=card>${posterTag(x)}<div class=p>
   <div class=t>${x.title}</div><div class=m><span class=tag>${x.type}</span> ${x.year||''}</div>
   <div class=act><button class="b add">+ Watch list</button></div></div></div>`)
   c.querySelector('.add').onclick=async e=>{e.target.textContent='Added ✓';e.target.disabled=true;await api('/api/watchlist','POST',x)}
   $('#res').append(c)})}
 $('#go').onclick=run;$('#q').onkeydown=e=>{if(e.key=='Enter')run()}}

// ---- Watch list (movies: inline download + progress; tv: view episodes)
function jobsByKey(jobs){const m={};jobs.forEach(j=>{if(!m[j.key]||j.id>m[j.key].id)m[j.key]=j});return m}
function barHtml(j){const p=j.status=='queued'?0:j.pct||0
 return `<div class=bar><i style="width:${p}%"></i><span>${j.status=='queued'?'queued…':p.toFixed(0)+'%'}</span></div>`}

function movieAction(x,job){
 if(job&&(job.status=='queued'||job.status=='running'))return barHtml(job)
 const st=x.status&&x.status.state
 if(st=='downloaded')return `<button class="b" style="background:var(--ok)" disabled>Downloaded ✓</button>`
 if(st=='failed_missing')return `<div class=msg><span class=bad>failed — ${x.status.msg||'not in library'}</span></div><button class="b red dl">Retry</button>`
 if(job&&job.status=='failed')return `<div class=msg><span class=bad>failed — ${job.msg||''}</span></div><button class="b red dl">Retry</button>`
 return `<button class="b dl">Download</button>`}

async function renderWatch(m){
 const [w,jobs]=await Promise.all([api('/api/watchlist'),api('/api/jobs')])
 if(!w.length){m.innerHTML='<div class=empty>Watch list is empty. Add things from Search.</div>';return}
 const jk=jobsByKey(jobs)
 m.innerHTML='<div class=grid id=g></div>'
 w.forEach(x=>{
  let act
  if(x.type=='tv'){act=`<button class="b eps">View episodes</button>`
   +(x.dlcount?`<div class="msg ok">${x.dlcount} episode(s) downloaded</div>`:'')}
  else act=movieAction(x,jk['movie:'+x.id])
  const c=el(`<div class=card>${posterTag(x)}<div class=p>
   <div class=t>${x.title}</div><div class=m><span class=tag>${x.type}</span> ${x.year||''}</div>
   <div class=act>${act}<button class="g rm">Remove</button></div></div></div>`)
  const dl=c.querySelector('.dl')
  if(dl)dl.onclick=async()=>{await api('/api/enqueue','POST',{item:x,units:[{}]});renderWatch(m)}
  const eps=c.querySelector('.eps')
  if(eps)eps.onclick=()=>{showEps=x;renderEps(m)}
  c.querySelector('.rm').onclick=async()=>{await api('/api/watchlist/remove','POST',x);renderWatch(m)}
  $('#g').append(c)})
 // live refresh while anything is active
 if(jobs.some(j=>j.status=='queued'||j.status=='running'))
  startPoll(()=>{if(tab=='Watch list'&&!showEps)renderWatch(m)})}

// ---- Episodes sub-page
async function renderEps(m){const x=showEps
 m.innerHTML=`<div class=row><button class="g" id=back>← Back</button>
  <b style="font-size:17px">${x.title}</b><span class=mut>${x.year||''}</span>
  <button class="b" id=all style="margin-left:auto">Download all missing</button></div>
  <div id=body>loading episodes…</div>`
 $('#back').onclick=()=>{showEps=null;renderWatch(m)}
 const [r,jobs]=await Promise.all([api('/api/episodes?id='+x.id),api('/api/jobs')])
 if(r.error){$('#body').innerHTML='<div class=empty>'+r.error+'</div>';return}
 const jk=jobsByKey(jobs)
 const go=(units)=>api('/api/enqueue','POST',{item:x,units}).then(()=>renderEps(m))
 const missing=[]
 r.seasons.forEach(se=>se.episodes.forEach(ep=>{if(ep.state!='downloaded')missing.push({season:String(se.season),episode:String(ep.e)})}))
 $('#all').onclick=()=>go(missing)
 if(!missing.length)$('#all').textContent='All downloaded ✓',$('#all').disabled=true
 let h=''
 r.seasons.forEach(se=>{
  const sm=se.episodes.filter(ep=>ep.state!='downloaded').map(ep=>({season:String(se.season),episode:String(ep.e)}))
  h+=`<div class=seas><h3><span>Season ${se.season} <span class=mut>(${se.episodes.length} ep)</span></span>
   <button class="g sall" data-s="${se.season}" ${sm.length?'':'disabled'}>Download season</button></h3>`
  se.episodes.forEach(ep=>{
   const key='tv:'+x.id+':'+se.season+':'+ep.e, job=jk[key]
   let a
   if(job&&(job.status=='queued'||job.status=='running'))a=barHtml(job)
   else if(ep.state=='downloaded')a=`<span class=ok>Downloaded ✓</span>`
   else if(ep.state=='failed_missing')a=`<span class=bad>missing</span> <button class="b red one" data-s="${se.season}" data-e="${ep.e}">Retry</button>`
   else if(job&&job.status=='failed')a=`<span class=bad>failed</span> <button class="b one" data-s="${se.season}" data-e="${ep.e}">Retry</button>`
   else a=`<button class="b one" data-s="${se.season}" data-e="${ep.e}">Download</button>`
   h+=`<div class=ep><span class=lbl>S${String(se.season).padStart(2,'0')}E${String(ep.e).padStart(2,'0')}</span><span class="grow bararea">${a}</span></div>`})
  h+='</div>'})
 $('#body').innerHTML=h
 $('#body').querySelectorAll('.one').forEach(b=>b.onclick=()=>go([{season:b.dataset.s,episode:b.dataset.e}]))
 $('#body').querySelectorAll('.sall').forEach(b=>b.onclick=()=>{
  const u=[];r.seasons.find(s=>String(s.season)==b.dataset.s).episodes.forEach(ep=>{if(ep.state!='downloaded')u.push({season:b.dataset.s,episode:String(ep.e)})});go(u)})
 if(jobs.some(j=>j.status=='queued'||j.status=='running'))
  startPoll(()=>{if(showEps)renderEps(m)})}

// ---- Files on disk
async function renderFiles(m){const r=await api('/api/downloaded')
 let h=`<div class="row mut">Folder: ${r.folder}</div>`
 if(!r.files.length){m.innerHTML=h+'<div class=empty>Nothing downloaded here yet.</div>';return}
 h+='<div class=grid>'+r.files.map(f=>`<div class=card>${f.poster?`<img src="${f.poster}">`:`<img alt="">`}
  <div class=p><div class=t>${f.name.replace(/\.mp4$/,'')}</div>
  <div class=m>${f.type?`<span class=tag>${f.type}</span> `:''}${f.mb} MB</div></div></div>`).join('')+'</div>'
 m.innerHTML=h}

// ---- Settings
async function renderSettings(m){const r=await api('/api/settings')
 m.innerHTML=`<div class=row><input id=f value="${esc(r.folder)}">
  <button class=b id=sv>Save</button></div>
  <div class=mut>Paste the full path downloads should go to (e.g. D:\\Movies). Created if missing.</div>`
 $('#sv').onclick=async()=>{const r=await api('/api/settings','POST',{folder:$('#f').value});$('#f').value=r.folder;$('#sv').textContent='Saved ✓';setTimeout(()=>$('#sv').textContent='Save',1500)}}

render()
</script></body></html>"""


if __name__ == "__main__":
    threading.Thread(target=_worker, daemon=True).start()
    folder = load_state()["folder"]
    os.makedirs(folder, exist_ok=True)
    print(f"leku gui -> http://localhost:{PORT}  (downloads: {folder})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
