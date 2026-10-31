"""Shared WebUI page styling."""

BASE_CSS = """
:root{--bg:#d3d1c5;--surface:#f0ede3;--surface2:#e4e1d6;--text:#343a2e;--muted:#62695a;--border:#b8bbaa;--accent:#b96e35;--accent2:#65734f;--moss:#65734f;--green:#53725e;--amber:#a96a43;--red:#a85e53}
body.dark{--bg:#19161d;--surface:#211d25;--surface2:#28242e;--text:#f2f1f4;--muted:#938f9b;--border:#352f3d;--accent:#b080ff;--accent2:#7138cc;--moss:#a3b58a;--green:#80ffb5;--amber:#ffcf99;--red:#ff8080}
body{font-family:"Avenir Next",Avenir,"Trebuchet MS",system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0 auto;padding:24px;max-width:980px;line-height:1.5}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:-1;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:56px 56px;opacity:.14}body:not(.dark)::before{display:none}
h1{font-family:Georgia,"Times New Roman",serif;font-size:1.8em;color:var(--text);margin:0 0 0.5em;letter-spacing:-.025em}
h2{font-size:1.2em;color:var(--text);margin:1.2em 0 0.4em}
h3{font-size:1em;margin:1em 0 0.3em}
a{color:var(--accent)}
button{background:var(--moss);color:#fff;border:0;border-radius:6px;padding:7px 14px;font-size:1em;cursor:pointer}
button:hover{filter:brightness(1.15)}
.settings-shell button:not(.set-tab){background:var(--moss)}
input,select,textarea{border:1px solid var(--border);border-radius:6px;padding:7px 9px;font-size:1em;background:var(--surface2);color:var(--text)}
label{margin-right:0.6em}
form{margin:0.8em 0}
pre{background:var(--surface2);border-radius:6px;padding:12px;overflow-x:auto;white-space:pre-wrap}
code{background:var(--surface2);border-radius:4px;padding:1px 5px}
table{border-collapse:collapse;margin:0.8em 0}
th,td{border:1px solid var(--border);padding:6px 10px;text-align:left}
th{background:var(--surface2)}
#chat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:16px}
#messages{min-height:120px}
#messages p{background:var(--surface2);border-radius:10px;padding:10px 12px;margin:0.5em 0}
#messages table{background:var(--surface)}
#form{display:flex;gap:8px}
#msg{flex:1}
#pills span{display:inline-block;background:var(--surface2);border-radius:12px;padding:2px 10px;margin:2px;font-size:0.9em}
#avatar{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px;margin-bottom:16px}
#nodes>div{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px;margin:0.8em 0}
#factory{max-width:100%}
#vrm-view{max-width:100%}
#layout{display:flex;gap:20px;align-items:flex-start}
#sidecol{width:280px;flex-shrink:0}
#sidecol #avatar{width:auto;margin-bottom:20px}
#sidebar{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
#sidebar h2{font-size:1em;margin:1em 0 0.3em}
#sidebar h2:first-child{margin-top:0}
#sidebar a{display:block;margin:0.2em 0}
#chat{flex:1;margin-top:0;display:flex;flex-direction:column;min-height:60vh}
#messages{flex:1;overflow-y:auto}
#form{margin-top:12px}
#approval>div{background:rgba(245,166,35,.12);border:1px solid var(--amber);border-radius:10px;padding:12px;margin:0.8em 0}
#approval>div button{margin-right:8px}
body.chat-page{max-width:1200px}
body.chat-page h1{display:inline-block;margin-right:12px;vertical-align:middle}
#theme-toggle{padding:6px 12px;font-size:1.1em;vertical-align:middle}
.settings-shell{max-width:980px;margin:0 auto;padding:8px 0 40px}
.settings-top{display:flex;align-items:center;gap:14px;padding:8px 0 24px;border-bottom:1px solid var(--border)}
.settings-top h1{flex:1;margin:0;font-size:1.65em}
.settings-top a{font-size:.9em;text-decoration:none}
.settings-intro{padding:18px 0 10px;color:var(--muted);font-size:.92em}
.settings-intro code,.set-key{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}
.set-group{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:8px 20px 14px;margin:16px 0;box-shadow:0 12px 30px rgba(0,0,0,.12)}
.set-group h2{color:var(--accent);font-size:1em;letter-spacing:.16em;text-transform:uppercase;margin:12px 0 4px}
.set-row{display:flex;gap:22px;align-items:center;padding:14px 0;border-top:1px solid var(--border)}
.set-row:first-of-type{border-top:0}
.set-label{flex:1;min-width:0}.set-label label{font-weight:600}.set-key{display:block;font-size:.72em;color:var(--muted);margin-top:3px}.set-desc{font-size:.8em;color:var(--muted);line-height:1.4;margin-top:4px}
.set-badge{font-size:.68em;color:var(--green);border:1px solid var(--green);border-radius:5px;padding:1px 5px;margin-left:5px}
.set-input{flex:0 0 330px}.set-input input[type=text],.set-input input[type=password],.set-input input[type=number],.set-input select{width:100%;padding:9px 11px;border:1px solid var(--border);border-radius:9px;background:var(--surface2);color:var(--text);outline:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.set-input input:focus,.set-input select:focus{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-glow)}
.range-control{display:grid;grid-template-columns:1fr 92px;gap:10px;align-items:center}.range-control input[type=range]{width:100%;accent-color:var(--accent)}.range-control input[type=number]{width:100%}.range-meta{display:flex;justify-content:space-between;margin-top:4px;color:var(--muted);font-size:.7em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.switch{position:relative;display:inline-block;width:46px;height:25px}.switch input{opacity:0;width:0;height:0}.switch span{position:absolute;inset:0;border-radius:13px;background:var(--surface2);border:1px solid var(--border);cursor:pointer}.switch span:before{content:"";position:absolute;width:17px;height:17px;left:3px;top:3px;border-radius:50%;background:var(--muted);transition:.15s}.switch input:checked+span{background:var(--moss);border-color:var(--moss)}.switch input:checked+span:before{transform:translateX(21px);background:#fff}
#settings-footer{display:flex;align-items:center;gap:14px;padding:16px 0 28px}#settings-note{flex:1;color:var(--muted);font-size:.8em}#settings-save{padding:11px 24px;border:0;border-radius:10px;background:var(--moss);color:#fff;font-weight:700;letter-spacing:.12em;text-transform:uppercase;cursor:pointer}
.set-tabs{display:flex;gap:6px;overflow-x:auto;padding:12px 0 4px;border-bottom:1px solid var(--border)}.set-tab{flex:0 0 auto;padding:8px 12px;border:1px solid var(--border);border-radius:9px 9px 0 0;background:var(--surface2);color:var(--text);font:inherit;font-size:.82em;cursor:pointer}.set-tab:hover{border-color:var(--moss)}.set-tab.active{background:var(--surface);border-color:var(--moss);color:var(--moss);font-weight:700}.set-group[hidden]{display:none!important}
@media(max-width:700px){.set-row{display:block}.set-input{width:100%;margin-top:10px}.settings-top{align-items:flex-start}.settings-shell{padding:0 4px 30px}}
@media (max-width:700px){#layout{display:block}#sidecol{width:auto;margin-bottom:20px}}
"""
