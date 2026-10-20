"""
NEPSE company viewer: look up any symbol, see Merolagani's company summary and
price history, and download it as CSV/JSON (CSV matches your signal tool's format:
Symbol,Date,High,Low,Close,Volume,EPS,PE,PBV,Dividend).

Setup:   pip install django requests beautifulsoup4
Run:     python nepse_clone.py runserver
Open:    http://127.0.0.1:8000/

For personal research use. Be polite: results are cached and bulk mode is throttled.
Check Merolagani's Terms of Use (merolagani.com/Disclaimer.aspx) before heavy use.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.urls import path

settings.configure(DEBUG=True, SECRET_KEY="local-dev-only", ROOT_URLCONF=__name__, ALLOWED_HOSTS=["*"])

BASE = "https://merolagani.com"
# Price history comes from the JSON endpoint Merolagani's own chart uses. If it ever changes,
# open the Price History / chart in your browser, DevTools > Network, copy the request URL and
# set ML_CHART_URL with {symbol}, {start}, {end} placeholders (unix seconds).
CHART_URL = os.environ.get(
    "ML_CHART_URL",
    BASE + "/handlers/TechnicalChartHandler.ashx?type=get_advanced_chart&symbol={symbol}"
    "&resolution=1D&rangeStartDate={start}&rangeEndDate={end}&isAdjust=1&currencyCode=NPR",
)
COLS = ["Symbol", "Date", "High", "Low", "Close", "Volume", "EPS", "PE", "PBV", "Dividend"]

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (personal research tool)"
_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def clean_symbol(raw):
    s = (raw or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{1,15}", s):
        raise ValueError("Enter a valid symbol, for example GLH.")
    return s


def num(text):
    m = re.search(r"-?\d[\d,]*\.?\d*", text or "")
    return m.group(0).replace(",", "") if m else ""


def get_summary(symbol):
    def fetch():
        r = session.get(BASE + "/CompanyDetail.aspx", params={"symbol": symbol}, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        data = {}
        for tr in soup.select("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) != 2:
                continue
            key = cells[0].get_text(" ", strip=True)
            if key.startswith("% Dividend"):
                rows = [[c.get_text(strip=True) for c in x.find_all("td")] for x in cells[1].find_all("tr")]
                rows = [x for x in rows if len(x) >= 3]
                data.setdefault("Dividend", rows[0][2] if rows else "")
            elif key and key not in data:
                data[key] = cells[1].get_text(" ", strip=True)
        if "Market Price" not in data:
            raise ValueError(f"No company found for symbol {symbol}.")
        return data

    return cached(("summary", symbol), 300, fetch)


def get_history(symbol, days):
    def fetch():
        end = int(time.time())
        r = session.get(CHART_URL.format(symbol=symbol, start=end - days * 86400, end=end), timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("s") != "ok" or not j.get("t"):
            raise ValueError(f"No price history returned for {symbol}.")
        rows = []
        for t, h, l, c, v in zip(j["t"], j["h"], j["l"], j["c"], j["v"]):
            day = (dt.datetime.utcfromtimestamp(t) + dt.timedelta(minutes=345)).date().isoformat()  # Nepal time
            rows.append({"Date": day, "High": h, "Low": l, "Close": c, "Volume": v})
        return rows

    return cached(("history", symbol, days), 300, fetch)


def build_rows(symbol, days):
    s = get_summary(symbol)
    fund = {
        "EPS": num(s.get("EPS")),
        "PE": num(s.get("P/E Ratio")),
        "PBV": num(s.get("PBV")),
        "Dividend": s.get("Dividend", ""),
    }
    return s, [{"Symbol": symbol, **r, **fund} for r in get_history(symbol, days)]


def days_param(request):
    try:
        return max(7, min(int(request.GET.get("days", 365)), 6000))
    except ValueError:
        return 365


def csv_response(rows, filename, extra=None):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS)
    w.writeheader()
    w.writerows(rows)
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    for k, v in (extra or {}).items():
        resp[k] = v
    return resp


def fail(e, status=400):
    return JsonResponse({"error": str(e)}, status=status)


def api_company(request):
    try:
        symbol = clean_symbol(request.GET.get("symbol"))
        summary, rows = build_rows(symbol, days_param(request))
        return JsonResponse({"symbol": symbol, "summary": summary, "history": rows})
    except ValueError as e:
        return fail(e)
    except Exception as e:  # network / parsing
        return fail(f"Could not fetch data from Merolagani: {e}", 502)


def download(request, symbol, fmt):
    try:
        symbol = clean_symbol(symbol)
        summary, rows = build_rows(symbol, days_param(request))
    except ValueError as e:
        return fail(e)
    except Exception as e:
        return fail(f"Could not fetch data from Merolagani: {e}", 502)
    if fmt == "json":
        resp = JsonResponse({"symbol": symbol, "summary": summary, "history": rows}, json_dumps_params={"indent": 2})
        resp["Content-Disposition"] = f'attachment; filename="{symbol}.json"'
        return resp
    return csv_response(rows, f"{symbol}_history.csv")


def bulk(request):
    """/bulk.csv?symbols=GLH,NHPC&min=200&max=300&days=120 -> one CSV, filtered by latest Close."""
    lo = float(request.GET.get("min", 0) or 0)
    hi = float(request.GET.get("max", 1e9) or 1e9)
    days = days_param(request)
    out, skipped = [], []
    for raw in re.split(r"[,\s]+", request.GET.get("symbols", "")):
        if not raw:
            continue
        try:
            _, rows = build_rows(clean_symbol(raw), days)
            if rows and lo <= float(rows[-1]["Close"]) <= hi:
                out.extend(rows)
            else:
                skipped.append(raw.upper() + "(range)")
        except Exception:
            skipped.append(raw.upper() + "(error)")
        time.sleep(1)  # be polite to the source site
    return csv_response(out, "nepse_bulk.csv", {"X-Skipped": ",".join(skipped)})


def index(request):
    return HttpResponse(PAGE)


urlpatterns = [
    path("", index),
    path("api/company", api_company),
    path("download/<str:symbol>.<str:fmt>", download),
    path("bulk.csv", bulk),
]

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEPSE company viewer</title>
<style>
:root{--bg:#f3f5f2;--ink:#17231d;--mute:#5d6b63;--line:#d5dbd6;--panel:#fff;--up:#1b7a4a;--down:#b3372e;--accent:#16425b}
@media (prefers-color-scheme:dark){:root{--bg:#121816;--ink:#e6ece8;--mute:#93a39a;--line:#2a3631;--panel:#19211e;--up:#4cc489;--down:#f0766c;--accent:#7cc0e0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.05rem;margin:28px 0 10px}
p.sub{margin:0 0 20px;color:var(--mute)}
form{display:flex;gap:8px;flex-wrap:wrap}
input,select,button,textarea{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
input[name=symbol]{width:140px;text-transform:uppercase}
button{cursor:pointer;background:var(--accent);color:#fff;border-color:var(--accent)}
button.alt,a.btn{background:var(--panel);color:var(--accent);border:1px solid var(--accent)}
a.btn{display:inline-block;padding:8px 12px;border-radius:6px;text-decoration:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#err{color:var(--down);margin:14px 0}
.head{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:12px;margin-top:24px}
.price{font-size:2rem;font-weight:650;font-variant-numeric:tabular-nums}
.chg.up{color:var(--up)}.chg.down{color:var(--down)}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);font-variant-numeric:tabular-nums}
th,td{padding:7px 12px;border-bottom:1px solid var(--line);text-align:left}
td.n,th.n{text-align:right}
.scroll{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:6px}
.scroll table{border:0}.scroll th{position:sticky;top:0;background:var(--panel)}
details{margin-top:28px}summary{cursor:pointer;font-weight:600}
textarea{width:100%;height:70px;margin:10px 0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.row input{width:90px}
</style></head><body><main>
<h1>NEPSE company viewer</h1>
<p class="sub">Enter a symbol to load its Merolagani summary and price history, then download it.</p>
<form id="f">
  <input name="symbol" placeholder="GLH" value="GLH" aria-label="Symbol" required>
  <select name="days" aria-label="History length">
    <option value="90">Last 90 days</option><option value="180">Last 180 days</option>
    <option value="365" selected>Last 1 year</option><option value="1095">Last 3 years</option>
    <option value="6000">Everything</option></select>
  <button>Load data</button>
</form>
<div id="err" role="alert"></div>
<div id="out" hidden>
  <div class="head">
    <div><div id="name"></div><span class="price" id="price"></span> <span class="chg" id="chg"></span></div>
    <div class="row"><a class="btn" id="dlcsv">Download CSV</a><a class="btn" id="dljson">Download JSON</a></div>
  </div>
  <h2>Company summary</h2><table id="sum"></table>
  <h2>Price history (<span id="cnt"></span> trading days, newest first)</h2>
  <div class="scroll"><table id="hist"></table></div>
</div>
<details><summary>Download several stocks at once, filtered by price</summary>
  <textarea id="syms" placeholder="GLH, NHPC, UPPER ... (comma or space separated)"></textarea>
  <div class="row">
    <label>Close between <input id="mn" value="200" inputmode="numeric"> and <input id="mx" value="300" inputmode="numeric"></label>
    <label>days <input id="bd" value="120" inputmode="numeric"></label>
    <button class="alt" id="bulk" type="button">Download combined CSV</button>
  </div>
  <p class="sub">Keeps only symbols whose latest Close is in the range. Takes about one second per symbol.</p>
</details>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('f').addEventListener('submit',async e=>{
  e.preventDefault(); $('err').textContent=''; $('out').hidden=true;
  const sym=e.target.symbol.value.trim().toUpperCase(), days=e.target.days.value;
  $('err').textContent='Loading '+sym+'...';
  try{
    const r=await fetch('/api/company?symbol='+encodeURIComponent(sym)+'&days='+days);
    const d=await r.json(); if(!r.ok) throw new Error(d.error);
    $('err').textContent='';
    const s=d.summary;
    $('name').textContent=(s['Company Name']||sym)+' ('+sym+')';
    $('price').textContent=s['Market Price'];
    const ch=parseFloat(s['% Change']); $('chg').textContent=s['% Change'];
    $('chg').className='chg '+(ch>=0?'up':'down');
    $('sum').innerHTML=Object.entries(s).map(([k,v])=>'<tr><th>'+esc(k)+'</th><td>'+esc(v)+'</td></tr>').join('');
    const h=d.history.slice().reverse();
    $('cnt').textContent=h.length;
    $('hist').innerHTML='<tr><th>Date</th><th class="n">High</th><th class="n">Low</th><th class="n">Close</th><th class="n">Volume</th></tr>'+
      h.map(x=>'<tr><td>'+x.Date+'</td><td class="n">'+x.High+'</td><td class="n">'+x.Low+'</td><td class="n">'+x.Close+'</td><td class="n">'+x.Volume+'</td></tr>').join('');
    $('dlcsv').href='/download/'+sym+'.csv?days='+days; $('dljson').href='/download/'+sym+'.json?days='+days;
    $('out').hidden=false;
  }catch(err){$('err').textContent=err.message}
});
$('bulk').onclick=()=>{
  const q=new URLSearchParams({symbols:$('syms').value,min:$('mn').value,max:$('mx').value,days:$('bd').value});
  location.href='/bulk.csv?'+q;
};
</script></main></body></html>
"""

if __name__ == "__main__":
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
