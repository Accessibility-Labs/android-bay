"""Build an offline, searchable acquisition index without executing recovered data."""
import html
import json
from pathlib import Path
from urllib.parse import quote


def write_catalog(root):
    root = Path(root).resolve()
    manifest = root / "manifest.jsonl"
    temporary = root / "catalog.html.tmp"
    escape = lambda value: html.escape(str(value), quote=True)
    with temporary.open("w", encoding="utf-8", errors="xmlcharrefreplace") as output:
        output.write('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Android Bay — recovered files</title>
<style>body{font:15px system-ui;margin:40px auto;max-width:1300px;padding:0 24px;color:#1d3346;background:#f7fafb}h1{margin-bottom:8px}p{line-height:1.5}input{font:inherit;padding:12px;width:min(650px,90%);border:1px solid #aabcc3;border-radius:8px}table{border-collapse:collapse;width:100%;background:white;margin-top:20px}td,th{padding:12px;text-align:left;border-bottom:1px solid #dce5e9;overflow-wrap:anywhere}th{background:#e8f3f2}a{color:#076c65}small{color:#566977}tr[hidden]{display:none}td:nth-child(2){white-space:nowrap}code{font-size:12px}button{font:inherit;padding:8px 12px;margin:4px;border:1px solid #abbfc5;border-radius:6px;background:white;cursor:pointer}</style>
<h1>Recovered files</h1><p>Search the <strong>original phone paths</strong> below. Each link opens the corresponding saved PC file. Windows-compatible filenames may differ. This index lists completed file copies, including earlier revisions; partial transfers are excluded.</p><p><a href="report.html">Coverage report</a> · <a href="manifest.jsonl">Full path and SHA-256 manifest</a></p><label for="search">Find by original path, filename or category</label><p><input id="search" type="search" placeholder="Examples: DCIM, Download, contacts, .jpg" autocomplete="off"></p><p id="count" role="status"></p><button id="previous">Previous 200</button><button id="next">Next 200</button><table><thead><tr><th>Original phone path / saved file</th><th>Bytes</th><th>Method</th></tr></thead><tbody id="files">''')
        count = 0
        if manifest.is_file():
            with manifest.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                        if record.get("status") != "copied":
                            continue
                        relative = Path(record["localPath"])
                        target = (root / relative).resolve()
                        if relative.is_absolute() or ".." in relative.parts or root not in target.parents:
                            continue
                        url = quote(relative.as_posix(), safe="/")
                        output.write('<tr hidden><td><a href="' + escape(url) + '">' + escape(record.get("source", relative)) + '</a><br><small>' + escape(relative) + '</small></td><td>' + escape(record.get("size", "")) + '</td><td>' + escape(record.get("category", "")) + '</td></tr>')
                        count += 1
                    except (ValueError, KeyError, TypeError, OSError):
                        continue
        output.write('''</tbody></table><p>Open and inspect important documents/photos before retiring the phone. Host checksums verify stored bytes; they do not establish whole-phone completeness. App archives require suitable tools and may contain live/inconsistent databases.</p>
<script>"use strict";const rows=Array.from(document.querySelectorAll("#files tr"));let page=0,matches=rows;function render(){for(const row of rows)row.hidden=true;const start=page*200;for(const row of matches.slice(start,start+200))row.hidden=false;document.getElementById("count").textContent=matches.length?`${start+1}–${Math.min(start+200,matches.length)} of ${matches.length} matching copies (${rows.length} total)`:"No matching copies";document.getElementById("previous").disabled=page===0;document.getElementById("next").disabled=start+200>=matches.length;}document.getElementById("search").addEventListener("input",function(){page=0;const term=this.value.toLowerCase();matches=rows.filter(row=>row.textContent.toLowerCase().includes(term));render();});document.getElementById("previous").onclick=()=>{if(page>0)page--;render();};document.getElementById("next").onclick=()=>{if((page+1)*200<matches.length)page++;render();};render();</script></html>''')
    temporary.replace(root / "catalog.html")
    return count
