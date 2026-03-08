"""
A股智能分析 v4 - 单文件版
运行: python app.py
依赖: pip install flask yfinance openai
"""
import json, threading, urllib.request, os, ast as _ast
try:
    import yfinance as yf
except ImportError:
    yf = None
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, Response

try:
    import requests as _req
    def _http_get(url, timeout=8):
        r = _req.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=timeout)
        return r.json()
except ImportError:
    def _http_get(url, timeout=8):
        rq = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(rq, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

BASE_DIR   = Path(__file__).parent.resolve()
DATA_DIR   = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
ARCHIVE_DIR  = DATA_DIR / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)
STATUS_FILE  = DATA_DIR / ".status.json"

CONFIG_FILE    = DATA_DIR / "config.json"
KEY_FILE       = DATA_DIR / "deepseek_key.txt"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
DEFAULT_WATCHLIST = [
    {"code": "002236", "name": "大华技术"},
    {"code": "002415", "name": "海康威视"},
    {"code": "601360", "name": "360"},
    {"code": "603000", "name": "人民网"},
]

app = Flask(__name__)
_running = False

# ── helpers ──
def jload(p):
    try: return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else None
    except Exception: return None

def jsave(p, d):
    Path(p).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def load_key():
    p = Path(KEY_FILE); return p.read_text(encoding="utf-8").strip() if p.exists() else ""
def save_key(k):  Path(KEY_FILE).write_text(k.strip(), encoding="utf-8")
def load_cfg():
    d = jload(CONFIG_FILE)
    if not d or not isinstance(d.get("watchlist"), list) or len(d.get("watchlist", [])) == 0:
        d = {"watchlist": DEFAULT_WATCHLIST}
        jsave(CONFIG_FILE, d)
    return d
def save_cfg(d):
    d.pop("deepseek_api_key", None)
    jsave(CONFIG_FILE, d)
def load_port(): return jload(PORTFOLIO_FILE) or {}
def save_port(d): jsave(PORTFOLIO_FILE, d)

def load_latest():
    # 直接按文件名时间戳排序，取最新的 done 状态文件
    files = sorted(ARCHIVE_DIR.glob("analysis_*.json"), reverse=True)
    for f in files:
        d = jload(f)
        if d and d.get("status") == "done": return d
    return {"status": "no_data"}

def save_archive(result):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 原子写入：先写临时文件再重命名，防止写入一半崩溃产生损坏文件
    target = ARCHIVE_DIR / f"analysis_{ts}.json"
    tmp    = ARCHIVE_DIR / f".tmp_{ts}.json"
    try:
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(target)
    except Exception:
        if tmp.exists(): tmp.unlink()
        raise
    # 清理旧文件：先按名称排序（文件名即时序）
    files = sorted(ARCHIVE_DIR.glob("analysis_*.json"))
    cutoff = datetime.now() - timedelta(days=14)
    to_delete = []
    for f in files:
        try:
            ts_str = f.stem.replace("analysis_", "")
            if datetime.strptime(ts_str, "%Y%m%d_%H%M%S") < cutoff:
                to_delete.append(f)
        except Exception: pass
    for f in to_delete:
        f.unlink(); files.remove(f)
    # 超过50个则删最旧的
    while len(files) > 50:
        files.pop(0).unlink()

def code_to_ticker(code):
    return code + (".SS" if code.startswith("6") else ".SZ")

def fetch_stock(code, name=""):
    try:
        ticker = yf.Ticker(code_to_ticker(code))
        df = ticker.history(period="5y")
        if df.empty: return {"code":code,"name":name or code,"error":"无数据"}
        close = df["Close"]; volume = df["Volume"]
        cur = round(float(close.iloc[-1]),2)
        prev = round(float(close.iloc[-2]),2) if len(close)>1 else cur
        chg = round((cur-prev)/prev*100,2)
        vr  = round(float(volume.iloc[-1]/volume.mean()),2)
        def ma(n):
            v = close.rolling(n).mean().iloc[-1]
            return round(float(v),2) if str(v)!="nan" else None
        def ds(lst, n=60):
            if len(lst)<=n: return lst
            s=max(1,len(lst)//n); return lst[::s]
        sparks = {
            "5d":  [round(float(v),2) for v in close.tail(5)],
            "30d": ds([round(float(v),2) for v in close.tail(30)],30),
            "90d": ds([round(float(v),2) for v in close.tail(90)],60),
            "180d":ds([round(float(v),2) for v in close.tail(180)],60),
            "365d":ds([round(float(v),2) for v in close.tail(365)],60),
            "5y":  ds([round(float(v),2) for v in close],60),
        }
        try:
            info = ticker.info
        except Exception:
            info = {}
        pe = info.get("trailingPE"); pb = info.get("priceToBook")
        mc = round(info.get("marketCap",0)/1e8,1) if info.get("marketCap") else None
        return {"code":code,"name":name or code,"close":cur,"change_pct":chg,
                "ma5":ma(5),"ma30":ma(30),"ma90":ma(90),"ma180":ma(180),"ma365":ma(365),"ma1250":ma(1250),
                "vol_ratio":vr,"pe":round(pe,1) if pe else None,"pb":round(pb,2) if pb else None,
                "mkt_cap":mc,"sparks":sparks,"error":None}
    except Exception as e:
        return {"code":code,"name":name or code,"error":str(e)}

def fetch_market():
    try:
        r={}
        for name,ticker in {"上证指数":"000001.SS","深证成指":"399001.SZ","创业板":"399006.SZ"}.items():
            df=yf.Ticker(ticker).history(period="2d")
            if not df.empty and len(df)>=2:
                c=float(df["Close"].iloc[-1]); p=float(df["Close"].iloc[-2])
                r[name]={"close":round(c,2),"change_pct":round((c-p)/p*100,2)}
            else: r[name]={"close":None,"change_pct":None}
        return r
    except Exception as e: return {"error":str(e)}

def fetch_news():
    results=[]
    try:
        d=_http_get("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=12&page=1&r=0.1")
        items=d.get("result",{}).get("data",[])
        results=[{"title":i.get("title",""),"url":i.get("url","")} for i in items if i.get("title")][:10]
    except Exception: pass
    if not results:
        try:
            nr=yf.Ticker("000001.SS").news or []
            results=[{"title":n.get("content",{}).get("title","") or n.get("title",""),
                      "url":n.get("link","")} for n in nr[:8] if n]
        except Exception: pass
    return results or [{"title":"暂无新闻","url":""}]

def fetch_market_hot():
    """爬取实时市场热点数据：涨幅榜、板块资金流、连续上涨股"""
    result = {}

    # 1. 东方财富 A股涨幅榜 top15
    try:
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               "?cb=&pn=1&pz=15&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
               "&fltt=2&invt=2&wbp2u=&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
               "&fields=f12,f14,f3,f2,f10,f5&_=1")
        d = _http_get(url, timeout=6)
        items = d.get("data",{}).get("diff",[]) or []
        top_gainers = []
        for s in items[:15]:
            code = str(s.get("f12",""))
            name = s.get("f14","")
            chg  = s.get("f3",0)
            price= s.get("f2",0)
            vol_ratio = s.get("f10",0)  # 量比
            if code and name and chg != "-":
                top_gainers.append(f"{name}({code}) +{chg}% 量比{vol_ratio}")
        result["top_gainers"] = top_gainers
    except Exception as e:
        result["top_gainers"] = []

    # 2. 东方财富 板块资金流向 top8
    try:
        url2 = ("https://push2.eastmoney.com/api/qt/clist/get"
                "?cb=&pn=1&pz=8&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                "&fltt=2&invt=2&fid=f62&fs=m:90+t:2+f:!50&fields=f12,f14,f62,f184,f3&_=1")
        d2 = _http_get(url2, timeout=6)
        items2 = d2.get("data",{}).get("diff",[]) or []
        sector_flow = []
        for s in items2[:8]:
            name = s.get("f14","")
            flow = s.get("f62",0)  # 主力净流入（万元）
            chg  = s.get("f3",0)
            if name and flow != "-":
                flow_yi = round(float(flow)/1e8, 2) if flow else 0
                sector_flow.append(f"{name} 净流入{flow_yi}亿 涨{chg}%")
        result["sector_flow"] = sector_flow
    except:
        result["sector_flow"] = []

    # 3. 东方财富 连板股（情绪指标）
    try:
        url3 = ("https://push2.eastmoney.com/api/qt/clist/get"
                "?cb=&pn=1&pz=10&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                "&fltt=2&invt=2&fid=f26&fs=m:0+f:!2&fields=f12,f14,f3,f26&_=1")
        d3 = _http_get(url3, timeout=6)
        items3 = d3.get("data",{}).get("diff",[]) or []
        limit_up = []
        for s in items3[:8]:
            name = s.get("f14","")
            days = s.get("f26",1)
            chg  = s.get("f3",0)
            code = s.get("f12","")
            if name and days and str(days) != "-":
                limit_up.append(f"{name}({code}) {days}连板")
        result["limit_up"] = limit_up
    except:
        result["limit_up"] = []

    return result

POLICY = """
A股政策背景（2025-2026）：
AI/算力/大模型、国产半导体设备、人形机器人、低空经济/无人机、
创新药/医疗器械、高端装备/工业母机。
注意：
政策方向仅作为长期产业背景，不代表当前资金流入。
不得仅凭政策方向推荐股票。
股票推荐必须遵循以下优先级：
1. 实时资金
   - 板块资金净流入
   - 涨幅榜集中度
   - 成交额放大
2. 市场情绪
   - 连板高度
   - 龙头股是否换手健康
   - 是否处于情绪高潮或退潮
3. 政策共振（加分项）
   - 当资金流入板块与政策方向一致时提高评分
若资金、情绪、政策三者同时共振，优先级最高。
若仅有政策而无资金流入，不得推荐。
"""

FRAMEWORK = """
A股分析评分模型（权重之和=100%）：
  资金流向   40% — 板块主力净流入金额、涨幅榜集中度、成交额是否放大
  板块热度   30% — 连板数量/高度、龙头是否还在涨停、题材是否仍有扩散
  量价结构   20% — 缩量回调=健康、放量下跌=出货、缩量横盘=蓄势
  均线趋势   10% — 仅作辅助确认趋势方向，不作买卖时机信号

均线使用规范（权重仅10%，勿过度依赖）：
  ✅ 正确用法：确认中长期趋势方向（MA30以上才有意义）
  ❌ 错误用法：用MA5/MA10做短线买卖点、用"跌破MA30"作为唯一止损依据
  短线（<2周）：均线几乎无效，应看题材热度和板块涨停数
  中线（1~3月）：MA30/MA60可辅助判断趋势，但需结合资金和行业
  长线（>3月）：MA120/MA250配合基本面才有价值

止损信号优先级（从高到低）：
  1. 板块热度退潮：龙头炸板、板块涨停数连续减少（比均线早一周）
  2. 放量下跌：成交额放大的下跌 = 主力出货信号
  3. 题材逻辑破坏：催化剂消失、利好兑现后无后续
  4. 均线（最后参考）：中线持仓破MA30且资金同步流出才考虑
"""

def run_ai(api_key, watchlist_data, market, news, portfolio, hot_data=None):
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    mkt_str = "\n".join(f"  {n}: {v.get('close')} ({'+' if (v.get('change_pct') or 0)>=0 else ''}{v.get('change_pct')}%)"
                        for n,v in market.items() if isinstance(v,dict) and "close" in v) if "error" not in market else "获取失败"
    stocks_str = json.dumps([{k:v for k,v in s.items() if k!="sparks"} for s in watchlist_data], ensure_ascii=False, indent=2)
    port_str = ""
    if portfolio:
        rows=[]
        for code,pos in portfolio.items():
            wl=next((s for s in watchlist_data if s["code"]==code),None)
            cur=wl["close"] if wl and wl.get("close") else None
            avg=pos.get("avg_price",0); qty=pos.get("quantity",0)
            pnl=round((cur-avg)/avg*100,2) if cur and avg else None
            rows.append(f"  {code} {pos.get('name','')} {qty}股 均价{avg} 现价{cur} 盈亏{pnl}%")
        port_str = "## 持仓\n" + "\n".join(rows)
    news_str = "\n".join(f"  - {n.get('title','')}" for n in news if n.get("title"))

    # 实时市场热点数据
    hot_str = ""
    if hot_data:
        if hot_data.get("top_gainers"):
            hot_str += "\n## 今日涨幅榜（实时）\n" + "\n".join(f"  {s}" for s in hot_data["top_gainers"])
        if hot_data.get("sector_flow"):
            hot_str += "\n## 板块主力资金净流入（实时）\n" + "\n".join(f"  {s}" for s in hot_data["sector_flow"])
        if hot_data.get("limit_up"):
            hot_str += "\n## 连板股/情绪指标（实时）\n" + "\n".join(f"  {s}" for s in hot_data["limit_up"])

    wl_codes = "、".join(s.get("code","") for s in watchlist_data if s.get("code"))
    prompt = f"""你是资深A股分析师。今天{datetime.now().strftime('%Y年%m月%d日')}。

## 大盘
{mkt_str}

{port_str}

## 自选股实时数据（需逐一分析）
{stocks_str}

## 今日财经新闻
{news_str}
{hot_str}

{FRAMEWORK}
{POLICY}

---
【自选股分析要求】对每只自选股按以下框架分析，均线权重仅10%，不得作为主要依据：

评分维度（必须逐项给出判断）：
  资金(40%)：该股所在板块今日是否在资金净流入榜？同板块是否有涨停？成交额vs近5日均量？
  热度(30%)：题材是否仍在发酵？有无催化剂（公告/政策/订单）？还是已是热点尾声？
  量价(20%)：近期是缩量回调（健康）还是放量下跌（出货）？突破时是否有量能配合？
  趋势(10%)：中期方向向上还是向下？（仅用MA30/MA60确认，不做买卖点）

买入信号组合（需同时满足）：
  ✅ 板块资金流入 + 缩量回调至支撑位 + 题材逻辑未破坏
止损信号组合（满足其一即需提示）：
  ⚠️ 板块涨停数明显减少 / 放量下跌 / 龙头炸板 / 题材催化剂消失

【严格禁止】recommendations中的股票代码不得出现：{wl_codes}，必须推荐自选股以外的新标的。
以上涨幅榜、资金流向、连板数据均为今日实时数据，推荐优先从资金正在流入的板块中选。

返回JSON（不要任何markdown包裹，直接返回JSON）:
{{
  "market_summary": "100字大盘综述，重点说明资金流向和市场情绪",
  "market_sentiment": "偏多|震荡|偏空",
  "watchlist_analysis": [
    {{
      "code": "股票代码",
      "score_breakdown": "资金:高/中/低 热度:高/中/低 量价:健康/中性/警示 趋势:上/横/下",
      "sector_heat": "板块今日资金净流入XX亿|板块中性|板块资金流出",
      "volume_signal": "缩量回调蓄势|放量上涨突破|放量下跌出货|量能平淡",
      "suggestion": "买入|关注|持有|观望|减仓",
      "entry": "入场条件：如板块资金重新流入+缩量回踩支撑位可介入；或等放量突破XX元确认",
      "exit": "离场信号：如板块涨停数减少至X个以下、或出现放量阴线即减仓",
      "reason": "60字，依据：资金面(权重40%)+板块热度(30%)+量价结构(20%)+趋势(10%)"
    }}
  ],
  "hot_sectors": [{{"name": "板块名", "em_keyword": "东方财富搜索关键词"}}],
  "recommendations": [
    {{
      "code": "6位代码",
      "name": "股票名",
      "sector": "所属板块",
      "term": "短线|中长线",
      "score": "资金XX分+热度XX分+量价XX分+趋势XX分=总分XX/100",
      "catalyst": "核心催化剂：如连板情绪/资金净流入/政策落地",
      "term_reason": "20字持有逻辑",
      "entry": "入场条件",
      "stop_signal": "止损信号（优先用板块热度/量价，慎用均线）",
      "reason": "80字：资金面+热度+量价结构+催化剂",
      "risk": "30字主要风险",
      "suggestion": "买入|关注",
      "eastmoney_code": "sz000000或sh600000"
    }}
  ],
  "risk_warning": "50字整体风险提示"
}}
recommendations 6-8只：2-3只短线（板块涨停仍在扩散、有量能配合、注明板块退潮止损信号）、3-4只中长线（资金持续流入板块、有政策共振、量价结构健康）。只返回JSON。"""
    resp = client.chat.completions.create(model="deepseek-chat", max_tokens=3000, temperature=0.3,
                                          messages=[{"role":"user","content":prompt}])
    text = resp.choices[0].message.content.strip()
    # strip markdown fences if present
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"): p = p[4:].strip()
            if p.startswith("{"): text = p; break
    return json.loads(text.strip())

# ── Routes ──
@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html; charset=utf-8")

@app.route("/api/config", methods=["GET"])
def get_config():
    c = load_cfg()
    return jsonify({"watchlist": c["watchlist"], "has_key": bool(load_key())})

@app.route("/api/config", methods=["POST"])
def set_config():
    c = load_cfg(); d = request.json or {}
    if d.get("deepseek_api_key"): save_key(d["deepseek_api_key"])
    if "watchlist" in d: c["watchlist"] = d["watchlist"]
    save_cfg(c); return jsonify({"ok": True})

@app.route("/api/portfolio", methods=["GET"])
def get_port(): return jsonify(load_port())

@app.route("/api/portfolio", methods=["POST"])
def set_port(): save_port(request.json or {}); return jsonify({"ok": True})

@app.route("/api/analysis", methods=["GET"])
def get_analysis():
    # STATUS_FILE 只用于 running/error 状态追踪
    if STATUS_FILE.exists():
        try:
            d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            if d.get("status") == "running":
                return jsonify({"status": "running"})
            if d.get("status") == "error":
                return jsonify(d)
            # status=done 时 STATUS_FILE 已无用，走 archive
        except: pass
    return jsonify(load_latest())

@app.route("/api/run", methods=["POST"])
def run():
    global _running
    # 先写入 running 状态文件，再启动线程，避免竞态条件
    STATUS_FILE.write_text(json.dumps({"status":"running","started_at":datetime.now().isoformat()},ensure_ascii=False), encoding="utf-8")
    _running = True
    def _go():
        global _running
        try:
            key = load_key()
            if not key: raise ValueError("未配置 API Key，请点设置填写")
            cfg = load_cfg(); port = load_port()
            wl = [fetch_stock(s["code"],s["name"]) for s in cfg["watchlist"]]
            mkt = fetch_market(); news = fetch_news()
            hot_data = fetch_market_hot()
            ai = run_ai(key, wl, mkt, news, port, hot_data)
            result = {"status":"done","updated_at":datetime.now().strftime("%Y-%m-%d %H:%M"),
                      "market":mkt,"watchlist":wl,"news":news,"ai":ai}
            save_archive(result)
            # 分析完成后清除 STATUS_FILE，让 get_analysis 走 archive
            if STATUS_FILE.exists(): STATUS_FILE.unlink()
        except Exception as e:
            err = {"status":"error","message":str(e)}
            STATUS_FILE.write_text(json.dumps(err,ensure_ascii=False), encoding="utf-8")
        finally:
            _running = False
    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"status":"started"})

@app.route("/api/reset", methods=["POST"])
def reset():
    global _running
    _running = False
    if STATUS_FILE.exists(): STATUS_FILE.unlink()
    return jsonify({"ok": True})

@app.route("/api/diagnose", methods=["POST"])
def diagnose():
    from openai import OpenAI
    key = load_key()
    if not key: return jsonify({"error":"未配置 API Key"}), 400
    body = request.json or {}
    portfolio = body.get("portfolio", {})
    if not portfolio: return jsonify({"error":"持仓为空"}), 400

    # 获取持仓股票的实时价格
    latest = load_latest()
    wl_data = latest.get("watchlist", []) if latest.get("status") == "done" else []
    market  = latest.get("market", {})
    ai_ctx  = latest.get("ai", {})

    # 构建持仓描述 - 持仓股若不在自选股中则实时抓取
    rows = []
    for code, pos in portfolio.items():
        live = next((s for s in wl_data if s["code"] == code), None)
        if not live:
            try: live = fetch_stock(code, pos.get("name", code))
            except Exception: live = None
        cur  = live["close"] if live and live.get("close") else None
        avg  = float(pos.get("avg_price") or 0)
        qty  = int(pos.get("quantity") or 0)
        pnl  = round((cur - avg) / avg * 100, 2) if cur and avg else None
        ma5  = live.get("ma5") if live else None
        ma30 = live.get("ma30") if live else None
        rows.append({
            "code": code, "name": pos.get("name", code),
            "qty": qty, "avg_price": avg, "current_price": cur,
            "pnl_pct": pnl,
            "ma5": ma5, "ma30": ma30,
            "ma90":  live.get("ma90")  if live else None,
            "ma180": live.get("ma180") if live else None,
            "change_pct": live.get("change_pct") if live else None,
            "vol_ratio": live.get("vol_ratio") if live else None,
        })

    mkt_str = ai_ctx.get("market_summary", "")
    sent    = ai_ctx.get("market_sentiment", "未知")
    today   = datetime.now().strftime("%Y年%m月%d日")

    # 补充实时市场热点数据
    hot_data = fetch_market_hot()
    hot_str = ""
    if hot_data.get("sector_flow"):
        hot_str += "\n当前板块资金流向：" + "；".join(hot_data["sector_flow"][:5])
    if hot_data.get("top_gainers"):
        hot_str += "\n今日涨幅榜：" + "；".join(hot_data["top_gainers"][:5])

    prompt = f"""今天{today}。大盘情绪：{sent}。{mkt_str}{hot_str}

你是专业A股持仓诊断顾问。以下是用户的持仓数据：
{json.dumps(rows, ensure_ascii=False, indent=2)}

字段说明：
- pnl_pct: 浮动盈亏%（负=亏损）
- vol_ratio: 量比（>1.5今日活跃，<0.5今日萎缩）
- change_pct: 今日涨跌幅
- ma30/ma90: 中期趋势参考（权重低，勿过度依赖）

持仓诊断评分模型（权重之和=100%）：
  资金/板块热度  40% — 所在板块今日是否有资金流入？涨幅榜是否有同板块标的？
  量价结构      30% — 今日是缩量整理(健康)还是放量下跌(出货)?近期成交额趋势?
  盈亏与风控    20% — 浮亏程度、持仓逻辑是否仍然成立
  均线趋势      10% — 仅作中期方向辅助，不作买卖时机依据

止损触发信号（按优先级，满足其一即需提示止损/减仓）：
  1. 放量下跌：今日或近日出现成交额放大的大阴线（主力出货信号，最优先）
  2. 板块退潮：所在板块涨停数持续减少、龙头炸板、题材催化剂消失
  3. 持仓逻辑破坏：买入理由（政策/订单/业绩）已被证伪
  4. 亏损超限：浮亏>10%且以上信号同时出现（均线仅作参考，不单独触发止损）

止盈触发信号：
  - 浮盈>15%：板块开始出现分歧（龙头不再新高）时分批止盈
  - 浮盈>25%：无论板块状态，建议至少减半仓

【重要】禁止给出的建议：
  ❌ 仅凭"跌破MA30"建议止损（均线滞后，不单独作为依据）
  ❌ 亏损时给出"继续持有等反弹"（需说明反弹的具体条件）
  ❌ 盈利时给出"继续持有"而不说明离场信号

请返回JSON（不要markdown包裹）：
{{
  "results": {{
    "股票代码": {{
      "suggestion": "持有|加仓|减仓|止损|止盈|观望",
      "score": "资金热度XX+量价XX+风控XX+趋势XX=总XX/100",
      "analysis": "60字：板块热度+量价结构+持仓逻辑是否成立（均线仅作补充）",
      "action": "具体操作：如板块资金持续流入可继续持有；若出现放量阴线立即减仓50%",
      "exit_signal": "明确离场触发条件：如板块涨停数跌至X个以下、或单日放量下跌X%即止损"
    }}
  }}
}}
每只股票都必须给出诊断，只返回JSON。"""

    client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat", max_tokens=1500, temperature=0.3,
            messages=[{"role":"user","content":prompt}]
        )
        text = resp.choices[0].message.content.strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                p = part.strip()
                if p.startswith("json"): p = p[4:].strip()
                if p.startswith("{"): text = p; break
        data = json.loads(text.strip())
        return jsonify({"results": data.get("results", {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/chat", methods=["POST"])
def chat():
    from openai import OpenAI
    key = load_key()
    if not key: return jsonify({"error":"未配置 API Key"}), 400
    body = request.json or {}
    msgs = body.get("messages", [])
    if not msgs: return jsonify({"error":"消息为空"}), 400
    latest = load_latest()
    ctx = ""
    if latest.get("status") == "done":
        ai = latest.get("ai",{})
        ctx = f"大盘情绪：{ai.get('market_sentiment','未知')}。{ai.get('market_summary','')}"
        wl = latest.get("watchlist",[])
        if wl: ctx += " 自选股：" + "，".join(f"{s['name']}{s.get('close','')}元{s.get('change_pct','')}%" for s in wl[:4] if s.get("close"))
    today = datetime.now().strftime("%Y年%m月%d日")
    core_rules = """
分析准则：
1. A股重资金和情绪。评判优先级：板块主力资金/连板热度 > 量价结构(缩量健康/放量出货) > 均线趋势。
2. 绝对禁止仅凭"跌破均线"建议止损，止损应以板块热度退潮、龙头炸板或放量下跌为核心信号。
3. 均线(MA30/MA60)仅用于辅助判断中期趋势方向，不是短线买卖点。
"""
    sys_msg = {"role":"system","content":f"你是资深A股投资顾问。今天{today}。{ctx}\n{core_rules}\n请严格遵循上述准则，用简洁专业中文回答。涉及操作建议须结合资金流向提示风险。"}
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
    try:
        resp = client.chat.completions.create(model="deepseek-chat", max_tokens=800, temperature=0.5,
                                              messages=[sys_msg]+msgs[-20:])
        return jsonify({"reply": resp.choices[0].message.content.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>A股·智能分析</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0e1a;--sf:#111827;--bd:#1e2d45;--ac:#00d4aa;--ac2:#3b82f6;--up:#00c97a;--dn:#ff4d6a;--tx:#e2e8f0;--mu:#64748b;--gd:#f5c842;--pu:#a78bfa}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Noto Sans SC',sans-serif;font-size:14px;min-height:100vh}
header{display:flex;align-items:center;justify-content:space-between;padding:0 20px;height:54px;background:var(--sf);border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:100}
.logo{font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:600;color:var(--ac);letter-spacing:2px}
.logo span{color:var(--tx)}
.hdr-r{display:flex;align-items:center;gap:10px}
.utime{color:var(--mu);font-size:11px;font-family:'JetBrains Mono',monospace}
.btn{padding:7px 14px;border-radius:6px;border:none;cursor:pointer;font-family:'Noto Sans SC',sans-serif;font-size:13px;transition:all .2s}
.btn-p{background:var(--ac);color:#0a0e1a;font-weight:700}
.btn-p:hover{background:#00ffcc}
.btn-p:disabled{opacity:.5;cursor:not-allowed}
.btn-g{background:transparent;color:var(--mu);border:1px solid var(--bd)}
.btn-g:hover{color:var(--tx);border-color:var(--ac)}
.btn-sm{padding:4px 10px;font-size:12px;border-radius:5px}
.layout{display:grid;grid-template-columns:255px 1fr;min-height:calc(100vh - 54px)}
aside{background:var(--sf);border-right:1px solid var(--bd);padding:14px;display:flex;flex-direction:column;overflow-y:auto;max-height:calc(100vh - 54px)}
.stitle{font-size:11px;font-weight:600;letter-spacing:1.5px;color:var(--mu);text-transform:uppercase;margin:14px 0 8px}
.stitle:first-child{margin-top:0}
.wl-item{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border-radius:7px;background:rgba(255,255,255,.03);border:1px solid var(--bd);margin-bottom:5px;cursor:pointer;transition:all .2s}
.wl-item:hover{border-color:var(--ac);background:rgba(0,212,170,.05)}
.wl-code{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--mu)}
.wl-name{font-size:13px;font-weight:500;margin-top:1px}
.wl-r{text-align:right}
.wl-price{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600}
.wl-chg{font-size:11px;font-family:'JetBrains Mono',monospace}
.up{color:var(--up)}.dn{color:var(--dn)}
.add-area{display:flex;flex-direction:column;gap:5px;margin-top:6px}
.add-row{display:flex;gap:5px}
.si{background:rgba(255,255,255,.05);border:1px solid var(--bd);border-radius:5px;color:var(--tx);padding:6px 9px;font-size:12px;outline:none;transition:border .2s}
.si:focus{border-color:var(--ac)}
.sector-tag{display:inline-block;padding:3px 8px;border-radius:4px;font-size:12px;background:rgba(0,212,170,.08);border:1px solid rgba(0,212,170,.2);color:var(--ac);margin:0 3px 4px 0;transition:all .15s}
.sector-tag:hover{background:rgba(0,212,170,.2);border-color:var(--ac)}
.sector-tag-link{text-decoration:none}
.ext-links{display:flex;flex-direction:column;gap:4px;margin-top:4px}
.ext-link{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:7px;background:rgba(255,255,255,.03);border:1px solid var(--bd);color:var(--mu);text-decoration:none;font-size:12px;transition:all .2s}
.ext-link:hover{border-color:var(--ac);color:var(--tx);background:rgba(0,212,170,.05)}
.ext-link-icon{font-size:15px;width:20px;text-align:center;flex-shrink:0}
.ext-link-info{display:flex;flex-direction:column}
.ext-link-name{font-size:12px;font-weight:500;color:var(--tx)}
.ext-link-desc{font-size:10px;color:var(--mu);margin-top:1px}
main{padding:16px 20px;overflow-y:auto;max-height:calc(100vh - 54px)}
.idx-bar{display:flex;gap:16px;flex-wrap:wrap;padding:12px 18px;border-radius:10px;background:var(--sf);border:1px solid var(--bd);margin-bottom:16px;align-items:center}
.idx-item{text-align:center;min-width:90px}
.idx-n{font-size:11px;color:var(--mu);margin-bottom:3px}
.idx-v{font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700}
.idx-c{font-family:'JetBrains Mono',monospace;font-size:12px;margin-top:2px}
.vdiv{width:1px;background:var(--bd);align-self:stretch}
.card{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:16px}
.ai-hdr{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.ai-badge{background:linear-gradient(135deg,var(--ac),var(--ac2));color:#0a0e1a;font-weight:700;font-size:11px;padding:3px 8px;border-radius:4px;letter-spacing:1px}
.sent-tag{padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}
.s-bull{background:rgba(0,201,122,.15);color:var(--up)}
.s-bear{background:rgba(255,77,106,.15);color:var(--dn)}
.s-neut{background:rgba(245,200,66,.15);color:var(--gd)}
.risk-box{color:var(--gd);font-size:12px;margin-top:10px;padding:8px 12px;background:rgba(245,200,66,.06);border-radius:6px;border-left:3px solid var(--gd)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.stock-card{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:14px;transition:border .2s}
.stock-card:hover{border-color:var(--ac)}
.sc-hdr{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.sc-name{font-size:15px;font-weight:600}
.sc-code{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--mu);margin-top:2px}
.sc-price{text-align:right}
.pv{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700}
.pc{font-family:'JetBrains Mono',monospace;font-size:12px;margin-top:2px}
.ma-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;margin:10px 0}
.ma-item{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.ma-lbl{font-size:11px;color:var(--mu)}
.ma-val{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600}
.ma-ab{color:var(--up)}.ma-bl{color:var(--dn)}.ma-na{color:var(--mu)}
.ptabs{display:flex;gap:3px;margin-bottom:5px;flex-wrap:wrap}
.pbtn{padding:2px 7px;border-radius:4px;border:1px solid var(--bd);background:transparent;color:var(--mu);font-size:11px;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all .15s}
.pbtn:hover{border-color:var(--ac);color:var(--tx)}
.pbtn.active{background:rgba(0,212,170,.15);border-color:var(--ac);color:var(--ac)}
.spark-cv{width:100%;height:46px;display:block}
.sc-ana{margin-top:10px;padding-top:10px;border-top:1px solid var(--bd)}
.sb{display:inline-block;padding:3px 10px;border-radius:4px;font-size:12px;font-weight:700;margin-right:6px}
.sb-buy{background:rgba(0,201,122,.15);color:var(--up);border:1px solid rgba(0,201,122,.3)}
.sb-hold{background:rgba(59,130,246,.15);color:var(--ac2);border:1px solid rgba(59,130,246,.3)}
.sb-watch{background:rgba(245,200,66,.15);color:var(--gd);border:1px solid rgba(245,200,66,.3)}
.sb-red{background:rgba(255,77,106,.15);color:var(--dn);border:1px solid rgba(255,77,106,.3)}
.arow{margin-bottom:4px;font-size:12px;color:var(--mu);line-height:1.6}
.arow b{color:var(--tx)}
.port-card{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:14px}
.port-card:hover{border-color:var(--ac)}
.pc-hdr{display:flex;justify-content:space-between;margin-bottom:10px}
.pc-nm{font-size:14px;font-weight:600}
.pc-cd{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--mu);margin-top:2px}
.pc-pv{text-align:right}
.pc-v{font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700}
.pc-cp{font-size:12px;font-family:'JetBrains Mono',monospace}
.pc-flds{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:8px 0}
.pc-fld label{font-size:10px;color:var(--mu);display:block;margin-bottom:2px}
.pc-fld input{width:100%;background:rgba(255,255,255,.06);border:1px solid var(--bd);border-radius:4px;color:var(--tx);padding:4px 7px;font-size:12px;font-family:'JetBrains Mono',monospace;outline:none}
.pc-fld input:focus{border-color:var(--ac)}
.pc-stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;padding:8px 0;border-top:1px solid var(--bd);margin-top:4px}
.pc-stat{text-align:center}
.pcs-lbl{font-size:10px;color:var(--mu)}
.pcs-val{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600;margin-top:2px}
.pc-sug{font-size:11px;color:var(--gd);margin-top:8px;padding:6px 8px;background:rgba(245,200,66,.06);border-radius:4px;border-left:2px solid var(--gd);line-height:1.5}
.reco-card{background:linear-gradient(135deg,rgba(0,212,170,.04),rgba(59,130,246,.04));border:1px solid rgba(0,212,170,.2);border-radius:12px;padding:14px}
.reco-card:hover{border-color:var(--ac)}
.reco-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px}
.reco-nm{font-size:14px;font-weight:600}
.reco-cd{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--ac);margin-top:1px}
.reco-pol{font-size:11px;color:var(--pu);margin-bottom:6px;padding:2px 7px;background:rgba(167,139,250,.1);border-radius:3px;display:inline-block}
.reco-why{font-size:12px;line-height:1.6;margin-bottom:5px}
.reco-rsk{font-size:11px;color:var(--mu);margin-bottom:8px}
.reco-term-short{font-size:11px;padding:2px 8px;border-radius:3px;background:rgba(245,200,66,.15);color:var(--gd);border:1px solid rgba(245,200,66,.3);font-weight:600}
.reco-term-long{font-size:11px;padding:2px 8px;border-radius:3px;background:rgba(167,139,250,.12);color:var(--pu);border:1px solid rgba(167,139,250,.3);font-weight:600}
.reco-term-why{font-size:11px;color:var(--mu);margin-bottom:5px}
.reco-stop{font-size:12px;color:var(--dn);margin-bottom:5px;padding:4px 8px;background:rgba(255,77,106,.06);border-radius:4px;border-left:2px solid var(--dn)}
.reco-ft{display:flex;justify-content:space-between;align-items:center;padding-top:8px;border-top:1px solid var(--bd)}
.reco-lnk{font-size:11px;color:var(--ac2);text-decoration:none}
.reco-lnk:hover{text-decoration:underline}
.btn-flw{padding:3px 10px;border-radius:4px;border:1px solid var(--ac);background:transparent;color:var(--ac);font-size:11px;cursor:pointer;transition:all .2s}
.btn-diagnose{padding:3px 11px;border-radius:4px;border:1px solid var(--pu);background:rgba(167,139,250,.1);color:var(--pu);font-size:11px;cursor:pointer;font-family:'Noto Sans SC',sans-serif;transition:all .2s;font-weight:600}
.btn-diagnose:hover{background:var(--pu);color:#0a0e1a}
.btn-diagnose:disabled{opacity:.4;cursor:not-allowed}
.pc-diag{margin-top:8px;padding:7px 10px;background:rgba(167,139,250,.07);border-radius:6px;border-left:2px solid var(--pu);font-size:11px;line-height:1.6;color:var(--tx)}
.pc-diag .diag-suggest{font-weight:700;color:var(--pu);margin-right:6px}
.btn-flw:hover,.btn-flw.followed{background:var(--ac);color:#0a0e1a;font-weight:600}
.news-item{padding:7px 0;border-bottom:1px solid var(--bd);font-size:12px;line-height:1.5}
.news-item:last-child{border:none}
.news-item a{color:var(--mu);text-decoration:none}
.news-item a:hover{color:var(--tx)}
.ndot{color:var(--ac);margin-right:6px}
.sec-lbl{font-size:11px;font-weight:600;letter-spacing:1.5px;color:var(--mu);text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.empty-state{text-align:center;padding:60px 20px;color:var(--mu)}
.empty-icon{font-size:48px;margin-bottom:12px}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.2);border-top-color:var(--ac);border-radius:50%;animation:spin .7s linear infinite;margin-right:6px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.disc{font-size:11px;color:var(--mu);text-align:center;padding:14px;border-top:1px solid var(--bd)}
.modal-ov{position:fixed;inset:0;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;z-index:200;opacity:0;pointer-events:none;transition:opacity .2s}
.modal-ov.show{opacity:1;pointer-events:all}
.modal{background:var(--sf);border:1px solid var(--bd);border-radius:14px;padding:24px;width:380px;max-width:90vw}
.modal h3{font-size:15px;margin-bottom:16px}
.fl{font-size:12px;color:var(--mu);margin-bottom:5px}
.fi{width:100%;background:rgba(255,255,255,.05);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:8px 11px;font-size:13px;outline:none;margin-bottom:12px;transition:border .2s}
.fi:focus{border-color:var(--ac)}
.fhint{font-size:11px;color:var(--mu);margin-top:-8px;margin-bottom:12px}
.chat-fab{position:fixed;bottom:28px;right:28px;width:52px;height:52px;border-radius:50%;background:var(--ac);border:none;cursor:pointer;font-size:22px;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 20px rgba(0,212,170,.4);transition:all .2s;z-index:150}
.chat-fab:hover{transform:scale(1.1);background:#00ffcc}
.chat-panel{position:fixed;bottom:92px;right:28px;width:380px;max-width:calc(100vw - 40px);height:520px;background:var(--sf);border:1px solid var(--bd);border-radius:16px;display:flex;flex-direction:column;z-index:150;box-shadow:0 8px 40px rgba(0,0,0,.5);transform:scale(0.95) translateY(10px);opacity:0;pointer-events:none;transition:all .2s}
.chat-panel.open{transform:scale(1) translateY(0);opacity:1;pointer-events:all}
.chat-hd{padding:14px 16px;border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.chat-hd-t{font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px}
.chat-dot{width:8px;height:8px;border-radius:50%;background:var(--ac);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.chat-x{background:none;border:none;color:var(--mu);cursor:pointer;font-size:18px;padding:2px 6px;border-radius:4px}
.chat-x:hover{color:var(--tx)}
.chat-msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.chat-msgs::-webkit-scrollbar{width:4px}
.chat-msgs::-webkit-scrollbar-thumb{background:var(--bd);border-radius:2px}
.msg{max-width:85%;padding:9px 12px;border-radius:10px;font-size:13px;line-height:1.6;word-break:break-word}
.msg-u{background:var(--ac);color:#0a0e1a;align-self:flex-end;border-bottom-right-radius:3px}
.msg-a{background:rgba(255,255,255,.06);color:var(--tx);align-self:flex-start;border-bottom-left-radius:3px;border:1px solid var(--bd)}
.msg-a b{color:var(--ac)}
.msg-err{background:rgba(255,77,106,.1);color:var(--dn);border:1px solid rgba(255,77,106,.2)}
.msg-t{font-size:10px;color:var(--mu);margin-top:3px}
.typing{align-self:flex-start;padding:10px 14px;background:rgba(255,255,255,.06);border-radius:10px;border:1px solid var(--bd)}
.td{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--mu);margin:0 2px;animation:bounce .9s infinite}
.td:nth-child(2){animation-delay:.2s}.td:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,80%,100%{transform:translateY(0)}40%{transform:translateY(-6px)}}
.chat-inp-a{padding:10px 12px;border-top:1px solid var(--bd);display:flex;gap:8px;flex-shrink:0}
.chat-inp{flex:1;background:rgba(255,255,255,.05);border:1px solid var(--bd);border-radius:8px;color:var(--tx);padding:8px 11px;font-size:13px;font-family:'Noto Sans SC',sans-serif;outline:none;resize:none;height:38px;overflow-y:auto;transition:border .2s}
.chat-inp:focus{border-color:var(--ac)}
.chat-snd{background:var(--ac);border:none;border-radius:8px;color:#0a0e1a;width:38px;height:38px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .2s}
.chat-snd:hover{background:#00ffcc}
.chat-snd:disabled{opacity:.4;cursor:not-allowed}
.chat-qs{padding:0 12px 8px;display:flex;gap:5px;flex-wrap:wrap;flex-shrink:0}
.chat-q{padding:3px 9px;border-radius:20px;border:1px solid var(--bd);background:transparent;color:var(--mu);font-size:11px;cursor:pointer;font-family:'Noto Sans SC',sans-serif;transition:all .15s}
.chat-q:hover{border-color:var(--ac);color:var(--ac)}
@media(max-width:900px){.grid2{grid-template-columns:1fr}.layout{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="logo">A<span>股</span>·智能分析</div>
  <div class="hdr-r">
    <span class="utime" id="utime" style="display:none"></span>
    <button class="btn btn-g btn-sm" onclick="openSettings()">⚙ 设置</button>
    <button class="btn btn-g btn-sm" onclick="doReset()" title="卡住时点此重置">↺ 重置</button>
    <button class="btn btn-p" id="reAnalyzeBtn" onclick="runAnalysis()">▶ 开始分析</button>
  </div>
</header>
<div class="layout">
<aside>
  <div class="stitle">自选股</div>
  <div id="wlEl"></div>
  <div class="add-area">
    <div class="add-row">
      <input id="addCode" class="si" placeholder="6位代码" maxlength="6" style="width:82px;flex:none">
      <input id="addName" class="si" placeholder="股票名称" style="flex:1;font-family:'Noto Sans SC',sans-serif">
    </div>
    <button class="btn btn-p btn-sm" style="width:100%;margin-top:2px;font-weight:700;letter-spacing:1px" onclick="addStock()">＋ 添加自选</button>
  </div>
  <div class="stitle">热门板块</div>
  <div id="hotSectors" style="color:var(--mu);font-size:12px">-- 待分析 --</div>

  <div class="stitle" style="margin-top:18px">行情网站</div>
  <div class="ext-links">
    <a class="ext-link" href="https://www.eastmoney.com" target="_blank">
      <span class="ext-link-icon">📈</span>
      <div class="ext-link-info"><span class="ext-link-name">东方财富</span><span class="ext-link-desc">行情 / 研报 / 资金流向</span></div>
    </a>
    <a class="ext-link" href="https://finance.sina.com.cn/stock/" target="_blank">
      <span class="ext-link-icon">📰</span>
      <div class="ext-link-info"><span class="ext-link-name">新浪财经</span><span class="ext-link-desc">新闻 / 公告 / 个股行情</span></div>
    </a>
    <a class="ext-link" href="https://xueqiu.com" target="_blank">
      <span class="ext-link-icon">🐦</span>
      <div class="ext-link-info"><span class="ext-link-name">雪球</span><span class="ext-link-desc">社区讨论 / 组合 / 深度分析</span></div>
    </a>
    <a class="ext-link" href="https://www.ithome.com/tag/aigc/" target="_blank">
      <span class="ext-link-icon">🤖</span>
      <div class="ext-link-info"><span class="ext-link-name">IT之家 AI</span><span class="ext-link-desc">AI / 科技行业最新动态</span></div>
    </a>
    <a class="ext-link" href="https://www.cls.cn" target="_blank">
      <span class="ext-link-icon">⚡</span>
      <div class="ext-link-info"><span class="ext-link-name">财联社</span><span class="ext-link-desc">实时快讯 / 电报</span></div>
    </a>
    <a class="ext-link" href="https://data.eastmoney.com/zjlx/detail.html" target="_blank">
      <span class="ext-link-icon">💰</span>
      <div class="ext-link-info"><span class="ext-link-name">资金流向</span><span class="ext-link-desc">主力 / 北向 / 板块流入</span></div>
    </a>
    <a class="ext-link" href="https://q.stock.sohu.com/cn/lhb.shtml" target="_blank">
      <span class="ext-link-icon">🐉</span>
      <div class="ext-link-info"><span class="ext-link-name">龙虎榜</span><span class="ext-link-desc">游资席位 / 机构动向</span></div>
    </a>
    <a class="ext-link" href="https://www.cninfo.com.cn" target="_blank">
      <span class="ext-link-icon">📋</span>
      <div class="ext-link-info"><span class="ext-link-name">巨潮资讯</span><span class="ext-link-desc">官方公告 / 财报 / 招募书</span></div>
    </a>
  </div>
</aside>
<main>
  <div id="dataBanner" style="display:none;border-radius:8px;padding:9px 14px;margin-bottom:12px;align-items:center;gap:8px;background:rgba(0,212,170,.07);border:1px solid rgba(0,212,170,.2)">
    <span id="dataBannerIcon" style="font-size:14px">📊</span>
    <span id="dataBannerText" style="font-size:13px;color:var(--tx)">暂无数据</span>
  </div>
  <div id="idxBar" class="idx-bar" style="display:none"></div>
  <div id="mc">
    <div class="empty-state">
      <div class="empty-icon">📊</div>
      <div style="font-size:16px;color:var(--tx);margin-bottom:8px">欢迎使用 A股智能分析</div>
      <div>在设置中填写 DeepSeek API Key，然后点击「开始分析」</div>
    </div>
  </div>
</main>
</div>

<div class="modal-ov" id="settingsModal">
  <div class="modal">
    <h3>⚙ 设置</h3>
    <div class="fl">DeepSeek API Key</div>
    <input class="fi" id="apiKey" type="password" placeholder="sk-xxxx">
    <div class="fhint">在 platform.deepseek.com 注册获取</div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn btn-g" onclick="closeModal('settingsModal')">取消</button>
      <button class="btn btn-p" onclick="saveSettings()">保存</button>
    </div>
  </div>
</div>

<div class="modal-ov" id="portModal">
  <div class="modal">
    <h3>+ 添加持仓</h3>
    <div class="fl">股票代码</div><input class="fi" id="pCode" placeholder="如 002236" maxlength="6">
    <div class="fl">股票名称</div><input class="fi" id="pName" placeholder="如 大华技术" style="font-family:'Noto Sans SC',sans-serif">
    <div class="fl">持股数量</div><input class="fi" id="pQty" type="number" placeholder="如 1000">
    <div class="fl">买入均价</div><input class="fi" id="pAvg" type="number" step="0.01" placeholder="如 18.50">
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn btn-g" onclick="closeModal('portModal')">取消</button>
      <button class="btn btn-p" onclick="savePortEntry()">添加</button>
    </div>
  </div>
</div>

<button class="chat-fab" onclick="toggleChat()" title="AI问答">💬</button>
<div class="chat-panel" id="chatPanel">
  <div class="chat-hd">
    <div class="chat-hd-t"><span class="chat-dot"></span>AI 投资顾问</div>
    <button class="chat-x" onclick="toggleChat()">✕</button>
  </div>
  <div class="chat-msgs" id="chatMsgs">
    <div class="msg msg-a">你好！我是AI投资顾问，已加载当前市场数据。可以问我选股、操作建议、政策解读等问题。<div class="msg-t"></div></div>
  </div>
  <div class="chat-qs">
    <button class="chat-q" onclick="quickAsk(this)">今天大盘怎么样？</button>
    <button class="chat-q" onclick="quickAsk(this)">我的持仓建议？</button>
    <button class="chat-q" onclick="quickAsk(this)">政策利好哪些股？</button>
    <button class="chat-q" onclick="quickAsk(this)">现在适合加仓吗？</button>
  </div>
  <div class="chat-inp-a">
    <textarea class="chat-inp" id="chatInp" placeholder="问任何A股问题…" onkeydown="chatKey(event)"></textarea>
    <button class="chat-snd" id="chatSnd" onclick="sendChat()">➤</button>
  </div>
</div>

<script>
var S = {wl:[], port:{}, analysis:null, polling:null, sparks:{}, diagnose:{}};

async function api(url, opts) {
  var r = await fetch(url, opts || {});
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}

async function init() {
  // Show loading
  document.getElementById('mc').innerHTML =
    '<div class="empty-state"><div class="empty-icon"><span class="spinner" style="width:32px;height:32px;border-width:3px"></span></div>'
    +'<div style="margin-top:14px;color:var(--mu)">正在连接服务器…</div></div>';
  try {
    var cfg = await api('/api/config');
    if(!cfg) throw new Error('/api/config 返回空');
    S.wl = Array.isArray(cfg.watchlist) ? cfg.watchlist : [];
    try {
      var port = await api('/api/portfolio');
      S.port = (port && typeof port === 'object') ? port : {};
    } catch(e2) { S.port = {}; }
    renderWL();
    // Show welcome
    // Show banner immediately
    if(cfg.has_key) {
      setBannerState('📊', '点击右侧按钮获取最新行情和 AI 分析　<span style="color:var(--mu)">自选股 '+S.wl.length+' 只已加载</span>',
        '↻ 开始分析', false);
    } else {
      setBannerState('⚠️', '<b style="color:var(--dn)">请先点右上角「设置」填写 DeepSeek API Key</b>',
        '↻ 开始分析', true, 'rgba(255,77,106,.06)', 'rgba(255,77,106,.2)');
    }
    document.getElementById('mc').innerHTML =
      '<div class="empty-state"><div class="empty-icon">📊</div>'
      +'<div style="font-size:16px;color:var(--tx);margin-bottom:8px">欢迎使用 A股智能分析</div>'
      +'<div style="color:var(--mu)">自选股 '+S.wl.length+' 只已加载' + (cfg.has_key ? '' : ' · ⚠️ 请先在<b>设置</b>中填写 DeepSeek API Key') + '</div></div>';
    var data = await api('/api/analysis');
    if(data && data.status === 'done') renderAnalysis(data);
    else if(data && data.status === 'running') { startPolling(); setBannerState('⏳','<b>上次分析仍在进行中…</b>','分析中…',true,'rgba(245,200,66,.07)','rgba(245,200,66,.25)'); }
    else { setBannerState('📭','<span style="color:var(--mu)">暂无分析数据，点右侧按钮开始</span>','↻ 开始分析',false); }
  } catch(e) {
    document.getElementById('mc').innerHTML =
      '<div class="empty-state"><div class="empty-icon">❌</div>'
      +'<div style="color:var(--dn);font-size:15px;margin:8px 0;font-weight:600">连接失败</div>'
      +'<div style="color:var(--tx);margin-bottom:8px">错误: ' + e.message + '</div>'
      +'<div style="color:var(--mu);font-size:12px;line-height:1.8">'
      +'请检查：<br>1. 终端里 <b style="color:var(--ac)">python app.py</b> 是否正在运行<br>'
      +'2. 终端是否有红色报错<br>3. 是否访问的是 <b style="color:var(--ac)">http://localhost:5000</b></div>'
      +'<div style="margin-top:14px"><button class="btn btn-p" onclick="location.reload()">🔄 重新连接</button></div></div>';
    console.error('init failed:', e);
  }
}

// ── 自选股 ──
function renderWL() {
  var el = document.getElementById('wlEl');
  if(!S.wl.length) { el.innerHTML='<div style="color:var(--mu);font-size:12px;padding:6px 0">还没有自选股</div>'; return; }
  var h = '';
  S.wl.forEach(function(s,i) {
    var d = S.analysis && S.analysis.watchlist && S.analysis.watchlist.find(function(w){return w.code===s.code;});
    var chg = d ? d.change_pct : null;
    var cls = chg > 0 ? 'up' : chg < 0 ? 'dn' : '';
    var sign = chg > 0 ? '+' : '';
    h += '<div class="wl-item" onclick="jumpTo(\'stock-'+s.code+'\')">'
       +'<div><div class="wl-code">'+s.code+'</div><div class="wl-name">'+s.name+'</div></div>'
       +'<div class="wl-r">'
       +(d ? '<div class="wl-price '+cls+'">'+d.close+'</div><div class="wl-chg '+cls+'">'+sign+chg+'%</div>' : '<div class="wl-price" style="color:var(--mu)">--</div>')
       +'<div style="margin-top:3px"><span style="color:var(--mu);font-size:10px;cursor:pointer" onclick="event.stopPropagation();removeStock('+i+')">✕</span></div>'
       +'</div></div>';
  });
  el.innerHTML = h;
}

async function addStock() {
  var code = document.getElementById('addCode').value.trim();
  var name = document.getElementById('addName').value.trim() || code;
  if(!/^\d{6}$/.test(code)) { alert('请输入6位股票代码'); return; }
  if(S.wl.find(function(s){return s.code===code;})) { alert('已在自选股中'); return; }
  S.wl.push({code:code, name:name});
  document.getElementById('addCode').value='';
  document.getElementById('addName').value='';
  await saveWL(); renderWL();
}

async function removeStock(i) { S.wl.splice(i,1); await saveWL(); renderWL(); }

async function saveWL() {
  await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({watchlist:S.wl})});
}

// ── 持仓 ──
function renderPort() {
  var el = document.getElementById('portSection');
  if(!el) return;
  var codes = Object.keys(S.port);
  if(!codes.length) { el.innerHTML='<p style="color:var(--mu);font-size:12px;margin-bottom:12px">还没有持仓</p>'; return; }
  var h = '<div class="grid4">';
  codes.forEach(function(code) {
    var pos = S.port[code];
    var d   = S.analysis && S.analysis.watchlist && S.analysis.watchlist.find(function(w){return w.code===code;});
    var ai  = S.analysis && S.analysis.ai && S.analysis.ai.watchlist_analysis && S.analysis.ai.watchlist_analysis.find(function(w){return w.code===code;});
    var cur = d ? d.close : null;
    var avg = parseFloat(pos.avg_price)||0;
    var qty = parseInt(pos.quantity)||0;
    var cost = avg*qty;
    var val  = cur ? cur*qty : null;
    var pnlAmt = val ? (val-cost).toFixed(0) : null;
    var pnlPct = (cur&&avg) ? ((cur-avg)/avg*100).toFixed(2) : null;
    var cls = pnlPct>0?'up':pnlPct<0?'dn':'';
    var sign = pnlPct>0?'+':'';
    h += '<div class="port-card">'
       +'<div class="pc-hdr"><div><div class="pc-nm">'+(pos.name||code)+'</div><div class="pc-cd">'+code+'</div></div>'
       +'<div class="pc-pv">'+(cur?'<div class="pc-v '+cls+'">'+cur+'</div><div class="pc-cp '+cls+'">'+sign+pnlPct+'%</div>':'<div class="pc-v" style="color:var(--mu)">--</div>')+'</div></div>'
       +'<div class="pc-flds"><div class="pc-fld"><label>持股数量</label><input type="number" value="'+qty+'" onchange="updPort(\''+code+'\',\'quantity\',this.value)"></div>'
       +'<div class="pc-fld"><label>买入均价</label><input type="number" step="0.01" value="'+(avg||'')+'" onchange="updPort(\''+code+'\',\'avg_price\',this.value)"></div></div>'
       +'<div class="pc-stats">'
       +'<div class="pc-stat"><div class="pcs-lbl">成本</div><div class="pcs-val">'+(cost?cost.toFixed(0):'--')+'</div></div>'
       +'<div class="pc-stat"><div class="pcs-lbl">现值</div><div class="pcs-val">'+(val?val.toFixed(0):'--')+'</div></div>'
       +'<div class="pc-stat"><div class="pcs-lbl">盈亏</div><div class="pcs-val '+cls+'">'+(pnlAmt!==null?sign+pnlAmt:'--')+'</div></div></div>'
       +(ai&&ai.portfolio_suggestion?'<div class="pc-sug">💡 '+ai.portfolio_suggestion+'</div>':'')
       +(S.diagnose[code]?'<div class="pc-diag"><span class="diag-suggest">'+(S.diagnose[code].suggestion||'')+'</span>'+(S.diagnose[code].score?'<span style="font-size:10px;color:var(--mu);margin-left:6px">'+S.diagnose[code].score+'</span>':'')+'<br>'+(S.diagnose[code].analysis||'')+(S.diagnose[code].action?'<br><b style="color:var(--ac)">操作：</b>'+S.diagnose[code].action:'')+(S.diagnose[code].exit_signal?'<br><b style="color:var(--dn)">离场信号：</b>'+S.diagnose[code].exit_signal:'')+'</div>':'')
       +'<div style="text-align:right;margin-top:6px"><button class="btn btn-g btn-sm" style="font-size:10px" onclick="delPort(\''+code+'\')">移除</button></div>'
       +'</div>';
  });
  h += '</div>';
  el.innerHTML = h;
}

async function updPort(code,field,val) {
  if(!S.port[code]) return;
  S.port[code][field] = parseFloat(val)||0;
  await api('/api/portfolio',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(S.port)});
  renderPort();
}
async function delPort(code) {
  delete S.port[code];
  await api('/api/portfolio',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(S.port)});
  renderPort();
}
function openAddPort() {
  ['pCode','pName','pQty','pAvg'].forEach(function(id){document.getElementById(id).value='';});
  document.getElementById('portModal').classList.add('show');
}
async function savePortEntry() {
  var code = document.getElementById('pCode').value.trim();
  var name = document.getElementById('pName').value.trim();
  var qty  = parseFloat(document.getElementById('pQty').value)||0;
  var avg  = parseFloat(document.getElementById('pAvg').value)||0;
  if(!/^\d{6}$/.test(code)){alert('请输入6位代码');return;}
  S.port[code]={name:name||code,quantity:qty,avg_price:avg};
  await api('/api/portfolio',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(S.port)});
  closeModal('portModal'); renderPort();
}

// ── AI诊股 ──
async function runDiagnose() {
  var codes = Object.keys(S.port);
  if(!codes.length) { alert('还没有持仓，请先添加持仓股票'); return; }
  var btn = document.getElementById('diagnoseBtn');
  if(btn) { btn.disabled=true; btn.textContent='🔬 诊股中…'; }
  try {
    var resp = await api('/api/diagnose', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({portfolio: S.port})
    });
    if(resp.error) { alert('诊股失败: '+resp.error); return; }
    S.diagnose = resp.results || {};
    renderPort();
  } catch(e) {
    alert('诊股失败: '+e.message);
  } finally {
    if(btn) { btn.disabled=false; btn.textContent='🔬 AI诊股'; }
  }
}

// ── 分析 ──
function setBannerState(icon, html, btnText, btnDisabled, bgColor, borderColor) {
  var b = document.getElementById('dataBanner');
  if(!b) return;
  b.style.display = 'flex';
  b.style.background = bgColor || 'rgba(0,212,170,.07)';
  b.style.borderColor = borderColor || 'rgba(0,212,170,.2)';
  document.getElementById('dataBannerIcon').textContent = icon;
  document.getElementById('dataBannerText').innerHTML = html;
  // Update header button
  var btn = document.getElementById('reAnalyzeBtn');
  if(btn) {
    btn.textContent = btnText || '▶ 开始分析';
    btn.disabled = !!btnDisabled;
  }
}

async function runAnalysis() {
  console.log('[分析] 开始，清除旧状态...');
  if(S.polling){ clearInterval(S.polling); S.polling=null; }

  setBannerState('⏳', '<b>分析中…</b>　正在抓取行情，请等待 30~90 秒', '分析中…', true,
    'rgba(245,200,66,.07)', 'rgba(245,200,66,.25)');
  document.getElementById('mc').innerHTML =
    '<div class="empty-state"><div class="empty-icon"><span class="spinner" style="width:36px;height:36px;border-width:3px"></span></div>'
    +'<div style="margin-top:16px;font-size:15px">正在抓取行情数据和 AI 分析中…</div>'
    +'<div style="color:var(--mu);margin-top:6px;font-size:12px">通常需要 30~90 秒，请耐心等待</div></div>';
  try {
    console.log('[分析] 调用 /api/run ...');
    var resp = await api('/api/run', {method:'POST'});
    console.log('[分析] /api/run 返回:', JSON.stringify(resp));
    if(!resp) { showErr('服务器无响应'); return; }
    if(resp.status==='error') { showErr('启动失败: '+resp.message); return; }
    console.log('[分析] 开始轮询...');
    startPolling();
  } catch(e) {
    console.error('[分析] 错误:', e);
    showErr('连接失败: '+e.message);
  }
}

function showErr(msg) {
  resetBtn();
  document.getElementById('mc').innerHTML =
    '<div class="empty-state"><div class="empty-icon">❌</div>'
    +'<div style="color:var(--dn);font-size:14px;margin:8px 0">'+msg+'</div>'
    +'<div style="margin-top:12px"><button class="btn btn-p" onclick="runAnalysis()">↻ 重新分析</button></div></div>';
  setBannerState('❌', '<b style="color:var(--dn)">分析失败</b>　'+msg, '↻ 重试', false, 'rgba(255,77,106,.06)', 'rgba(255,77,106,.2)');
}

function startPolling() {
  if(S.polling){ clearInterval(S.polling); S.polling=null; }
  var attempts = 0;
  S.polling = setInterval(async function(){
    attempts++;
    try {
      var data = await api('/api/analysis');
      console.log('[轮询] 第'+attempts+'次, status='+data.status);
      if(data.status==='done'){
        clearInterval(S.polling); S.polling=null;
        console.log('[轮询] 完成，渲染数据');
        data._fresh = true;
        renderAnalysis(data); resetBtn();
        if(Object.keys(S.port).length>0) setTimeout(runDiagnose, 500);
      } else if(data.status==='error'){
        clearInterval(S.polling); S.polling=null;
        console.error('[轮询] 分析失败:', data.message);
        showErr('分析失败: '+(data.message||'未知错误')); resetBtn();
      } else if(attempts > 60){
        clearInterval(S.polling); S.polling=null;
        showErr('分析超时（>3分钟），请检查终端报错后重试');
      }
    } catch(e) {
      console.warn('[轮询] 请求失败:', e.message);
    }
  }, 3000);
}

function resetBtn(){ var btn=document.getElementById('reAnalyzeBtn'); if(btn){btn.disabled=false;btn.innerHTML='▶ 开始分析';} }

async function doReset(){
  await api('/api/reset',{method:'POST'});
  if(S.polling){clearInterval(S.polling);S.polling=null;}
  resetBtn();
}

// ── 渲染主内容 ──
function renderAnalysis(data) {
  S.analysis = data;
  var ai = data.ai || {};
  // Show data banner
  var banner = document.getElementById('dataBanner');
  var bannerText = document.getElementById('dataBannerText');
  var bannerIcon = document.getElementById('dataBannerIcon');
  var ts = data.updated_at || '';
  if(data._fresh) {
    setBannerState('✅', '<b style="color:var(--up)">分析完成</b>　数据时间：<span style="font-family:monospace;color:var(--ac)">'+ts+'</span>',
      '↻ 重新分析', false, 'rgba(0,201,122,.07)', 'rgba(0,201,122,.25)');
  } else {
    setBannerState('🕐', '<b style="color:var(--mu)">历史数据</b>　数据时间：<span style="font-family:monospace;color:var(--ac)">'+ts+'</span>',
      '↻ 重新分析', false, 'rgba(0,212,170,.07)', 'rgba(0,212,170,.2)');
  }

  // 大盘
  var m = data.market || {};
  if(typeof m==='object' && !m.error) {
    var ib = document.getElementById('idxBar');
    ib.style.display = 'flex';
    var ih = '';
    Object.keys(m).forEach(function(n,i) {
      var v = m[n];
      var chg = (v.change_pct !== null && v.change_pct !== undefined) ? v.change_pct : null;
      var cls = (chg||0)>=0?'up':'dn';
      if(i>0) ih += '<div class="vdiv"></div>';
      ih += '<div class="idx-item"><div class="idx-n">'+n+'</div>'
          +'<div class="idx-v '+cls+'">'+(v.close||'--')+'</div>'
          +'<div class="idx-c '+cls+'">'+(chg!==null?(chg>=0?'+':'')+chg+'%':'--')+'</div></div>';
    });
    ib.innerHTML = ih;
  }

  // 热门板块
  var hot = ai.hot_sectors || [];
  document.getElementById('hotSectors').innerHTML = hot.length
    ? hot.map(function(s){
        var name = (typeof s === 'object') ? s.name : s;
        var kw   = (typeof s === 'object' && s.em_keyword) ? s.em_keyword : name;
        var url  = 'https://so.eastmoney.com/web/s?keyword='+encodeURIComponent(kw);
        return '<a href="'+url+'" target="_blank" class="sector-tag-link"><span class="sector-tag">'+name+' ↗</span></a>';
      }).join('')
    : '-- --';

  var h = '';

  // AI摘要
  var sc = ai.market_sentiment==='偏多'?'s-bull':ai.market_sentiment==='偏空'?'s-bear':'s-neut';
  h += '<div class="card"><div class="ai-hdr"><span class="ai-badge">AI · DeepSeek</span>'
     + '<span class="sent-tag '+sc+'">'+(ai.market_sentiment||'--')+'</span></div>'
     + '<div style="line-height:1.8">'+(ai.market_summary||'--')+'</div>'
     + (ai.risk_warning?'<div class="risk-box">⚠ '+ai.risk_warning+'</div>':'')
     + '</div>';

  // 持仓
  h += '<div class="sec-lbl">我的持仓 <button class="btn btn-g btn-sm" style="font-size:11px" onclick="openAddPort()">+ 添加</button> <button class="btn-diagnose" id="diagnoseBtn" onclick="runDiagnose()">🔬 AI诊股</button></div>'
     + '<div id="portSection"></div>';

  // 自选股
  h += '<div class="sec-lbl" style="margin-top:4px">自选股分析</div><div class="grid3">';
  (data.watchlist||[]).forEach(function(s) {
    if(s.sparks) S.sparks[s.code] = s.sparks;
    var ana = (ai.watchlist_analysis||[]).find(function(a){return a.code===s.code;})||{};
    h += buildCard(s, ana);
  });
  h += '</div>';

  // AI推荐 - 分短线/中长线
  var recos = ai.recommendations || [];
  var shortTerm  = recos.filter(function(r){ return r.term === '短线'; });
  var longTerm   = recos.filter(function(r){ return r.term !== '短线'; });

  function buildReco(r) {
    var em = r.eastmoney_code || ((r.code.charAt(0)==='6'?'sh':'sz')+r.code);
    var followed = S.wl.some(function(w){return w.code===r.code;});
    var isShort = r.term === '短线';
    var cardBg = isShort
      ? 'background:linear-gradient(135deg,rgba(245,200,66,.05),rgba(255,77,106,.04));border:1px solid rgba(245,200,66,.25)'
      : 'background:linear-gradient(135deg,rgba(167,139,250,.05),rgba(59,130,246,.04));border:1px solid rgba(167,139,250,.25)';
    return '<div class="reco-card" style="'+cardBg+'">'
      +'<div class="reco-top">'
      +'<div><div class="reco-nm">'+r.name+'</div><div class="reco-cd">'+r.code+'</div></div>'
      +'<span class="sb sb-buy">'+(r.suggestion||'关注')+'</span></div>'
      +'<div class="reco-pol">🏛 '+(r.policy_direction||r.sector||'')+'</div>'
      +(r.term_reason?'<div style="font-size:11px;color:'+(isShort?'var(--gd)':'var(--pu)')+'margin-bottom:5px">🕐 '+r.term_reason+'</div>':'')
      +'<div class="reco-why">'+(r.reason||'')+'</div>'
      +(r.stop_loss?'<div class="reco-stop">🛑 止损参考：'+r.stop_loss+'</div>':'')
      +'<div class="reco-rsk">⚠ '+(r.risk||'')+'</div>'
      +'<div class="reco-ft"><a class="reco-lnk" href="https://quote.eastmoney.com/'+em+'.html" target="_blank">📈 东方财富</a>'
      +'<button class="btn-flw'+(followed?' followed':'')+'" id="fl_'+r.code+'" onclick="followStock(\''+r.code+'\',\''+r.name+'\',this)">'
      +(followed?'✓ 已关注':'+ 关注')+'</button></div></div>';
  }

  if(shortTerm.length) {
    h += '<div class="sec-lbl" style="margin-top:4px">'
       + '<span style="background:rgba(245,200,66,.15);color:var(--gd);border:1px solid rgba(245,200,66,.3);padding:2px 10px;border-radius:4px;font-size:12px">⚡ 短线推荐</span>'
       + '<span style="margin-left:8px;font-weight:400;color:var(--mu);font-size:11px">1~2周内题材/资金驱动，严守止损</span></div>'
       + '<div class="grid3">';
    shortTerm.forEach(function(r){ h += buildReco(r); });
    h += '</div>';
  }

  if(longTerm.length) {
    h += '<div class="sec-lbl" style="margin-top:8px">'
       + '<span style="background:rgba(167,139,250,.12);color:var(--pu);border:1px solid rgba(167,139,250,.3);padding:2px 10px;border-radius:4px;font-size:12px">📈 中长线推荐</span>'
       + '<span style="margin-left:8px;font-weight:400;color:var(--mu);font-size:11px">政策+基本面共振，可承受回调</span></div>'
       + '<div class="grid3">';
    longTerm.forEach(function(r){ h += buildReco(r); });
    h += '</div>';
  }

  // 新闻
  h += '<div class="card"><div class="sec-lbl" style="margin-bottom:10px">今日财经要闻</div>';
  (data.news||[]).forEach(function(n) {
    var title = typeof n==='string'?n:(n.title||'');
    var url   = typeof n==='string'?'':(n.url||'');
    h += '<div class="news-item"><span class="ndot">▸</span>'
       +(url?'<a href="'+url+'" target="_blank">'+title+'</a>':'<span style="color:var(--mu)">'+title+'</span>')
       +'</div>';
  });
  h += '</div><div class="disc">⚠ 本报告由AI自动生成，仅供参考，不构成投资建议。</div>';

  document.getElementById('mc').innerHTML = h;
  renderPort();

  // 画图表
  (data.watchlist||[]).forEach(function(s) {
    var sparks = S.sparks[s.code];
    if(sparks) drawSpark('cv_'+s.code, sparks['365d']||sparks['30d']||[], (s.change_pct||0)>=0);
  });

  renderWL();
}

function buildCard(s, ana) {
  var cur = s.close||0;
  var isUp = (s.change_pct||0)>=0;
  var sid = 'cv_'+s.code;
  var sugMap = {'买入':'sb-buy','关注':'sb-hold','持有':'sb-hold','观望':'sb-watch','减仓':'sb-red'};
  var sugCls = sugMap[ana.suggestion]||'sb-watch';
  var maData = [['MA5日',s.ma5],['MA30日',s.ma30],['MA90日',s.ma90],['MA180日',s.ma180],['MA365日',s.ma365],['MA5年',s.ma1250]];
  var maH = '';
  maData.forEach(function(row){
    var c = row[1]?(cur>row[1]?'ma-ab':cur<row[1]?'ma-bl':'ma-na'):'ma-na';
    maH += '<div class="ma-item"><span class="ma-lbl">'+row[0]+'</span><span class="ma-val '+c+'">'+(row[1]||'--')+'</span></div>';
  });
  var tabH = '';
  ['5d','30d','90d','180d','365d','5y'].forEach(function(p,i){
    tabH += '<button class="pbtn'+(i===4?' active':'')+'" onclick="swPeriod(\''+sid+'\',\''+s.code+'\',\''+p+'\',this)">'+p+'</button>';
  });

  var anaH = '';
  if(ana.suggestion){
    // trend badge
    var trendCol = ana.trend==='强势'?'var(--up)':ana.trend==='弱势'?'var(--dn)':'var(--gd)';
    var trendBg  = ana.trend==='强势'?'rgba(0,201,122,.12)':ana.trend==='弱势'?'rgba(255,77,106,.12)':'rgba(245,200,66,.12)';
    // sector heat badge
    var heatCol  = (ana.sector_heat||'').indexOf('流入')>-1 ? 'var(--up)' : (ana.sector_heat||'').indexOf('流出')>-1 ? 'var(--dn)' : 'var(--mu)';
    var heatBg   = (ana.sector_heat||'').indexOf('流入')>-1 ? 'rgba(0,201,122,.1)' : (ana.sector_heat||'').indexOf('流出')>-1 ? 'rgba(255,77,106,.1)' : 'rgba(100,116,139,.1)';
    // volume signal badge
    var volCol = (ana.volume_signal||'').indexOf('放量上涨')>-1 ? 'var(--up)' : (ana.volume_signal||'').indexOf('出货')>-1||( ana.volume_signal||'').indexOf('放量下跌')>-1 ? 'var(--dn)' : 'var(--mu)';

    anaH = '<div class="sc-ana">'
      + '<div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:6px">'
      + '<span class="sb '+sugCls+'">'+ana.suggestion+'</span>'
      + (ana.score_breakdown?'<span style="font-size:10px;color:var(--mu)">'+ana.score_breakdown+'</span>':'')
      + '</div>'
      + (ana.sector_heat?'<div style="font-size:11px;padding:3px 7px;border-radius:3px;background:'+heatBg+';color:'+heatCol+';margin-bottom:5px">📊 '+ana.sector_heat+'</div>':'')
      + (ana.volume_signal?'<div style="font-size:11px;padding:3px 7px;border-radius:3px;background:rgba(100,116,139,.08);color:'+volCol+';margin-bottom:5px">📶 '+ana.volume_signal+'</div>':'')
      + '<div class="arow" style="color:var(--tx);margin-bottom:4px">'+(ana.reason||'')+'</div>'
      + (ana.entry?'<div style="font-size:11px;color:var(--ac);background:rgba(0,212,170,.06);border-radius:4px;padding:4px 8px;margin-bottom:4px;border-left:2px solid var(--ac)">🎯 '+ana.entry+'</div>':'')
      + (ana.exit?'<div style="font-size:11px;color:var(--dn);background:rgba(255,77,106,.06);border-radius:4px;padding:4px 8px;border-left:2px solid var(--dn)">⚠️ '+ana.exit+'</div>':'')
      + '</div>';
  }
  return '<div class="stock-card" id="stock-'+s.code+'">'
    +'<div class="sc-hdr"><div><div class="sc-name">'+s.name+'</div><div class="sc-code">'+s.code+'</div></div>'
    +'<div class="sc-price"><div class="pv '+(isUp?'up':'dn')+'">'+(s.close||'--')+'</div>'
    +'<div class="pc '+(isUp?'up':'dn')+'">'+(isUp?'+':'')+(s.change_pct||0)+'%</div></div></div>'
    +'<div class="ma-grid">'+maH+'</div>'
    +'<div><div class="ptabs">'+tabH+'</div><canvas class="spark-cv" id="'+sid+'"></canvas></div>'
    +anaH+'</div>';
}

// ── 图表 ──
function swPeriod(cvId, code, period, btn) {
  btn.closest('.ptabs').querySelectorAll('.pbtn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  var sp = S.sparks[code]; if(!sp) return;
  var d = sp[period]||[];
  drawSpark(cvId, d, d.length<2||d[d.length-1]>=d[0]);
}

function drawSpark(id, data, isUp) {
  var cv = document.getElementById(id);
  if(!cv||!data||data.length<2) return;
  var ctx = cv.getContext('2d');
  var W = cv.parentElement.offsetWidth||280, H=46, pad=2;
  cv.width=W; cv.height=H; ctx.clearRect(0,0,W,H);
  var mn=Math.min.apply(null,data), mx=Math.max.apply(null,data), rng=mx-mn||1;
  var pts=data.map(function(v,i){return{x:pad+(i/(data.length-1))*(W-pad*2),y:H-pad-((v-mn)/rng)*(H-pad*2)};});
  var col=isUp?'0,201,122':'255,77,106';
  var g=ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,'rgba('+col+',.3)'); g.addColorStop(1,'rgba('+col+',0)');
  ctx.beginPath(); ctx.moveTo(pts[0].x,pts[0].y);
  pts.slice(1).forEach(function(p){ctx.lineTo(p.x,p.y);});
  ctx.lineTo(pts[pts.length-1].x,H); ctx.lineTo(pts[0].x,H); ctx.closePath();
  ctx.fillStyle=g; ctx.fill();
  ctx.beginPath(); ctx.moveTo(pts[0].x,pts[0].y);
  pts.slice(1).forEach(function(p){ctx.lineTo(p.x,p.y);});
  ctx.strokeStyle=isUp?'#00c97a':'#ff4d6a'; ctx.lineWidth=1.5; ctx.lineJoin='round'; ctx.stroke();
}

// ── 关注 ──
async function followStock(code,name,btn){
  if(S.wl.some(function(s){return s.code===code;})){btn.textContent='✓ 已关注';btn.classList.add('followed');return;}
  S.wl.push({code:code,name:name}); await saveWL();
  btn.textContent='✓ 已关注'; btn.classList.add('followed'); renderWL();
}

// ── 设置 ──
async function openSettings(){
  var cfg=await api('/api/config');
  document.getElementById('apiKey').value=cfg.has_key?'••••••••':'';
  document.getElementById('settingsModal').classList.add('show');
}
async function saveSettings(){
  var k=document.getElementById('apiKey').value.trim();
  if(k&&k!=='••••••••') await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({deepseek_api_key:k})});
  closeModal('settingsModal');
}
function closeModal(id){document.getElementById(id).classList.remove('show');}
document.querySelectorAll('.modal-ov').forEach(function(el){el.addEventListener('click',function(e){if(e.target===el)closeModal(el.id);});});

// ── 聊天 ──
var chatOpen=false, chatMsgs=[];
function toggleChat(){
  chatOpen=!chatOpen;
  document.getElementById('chatPanel').classList.toggle('open',chatOpen);
  if(chatOpen) document.getElementById('chatInp').focus();
}
function chatKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}}
function quickAsk(btn){document.getElementById('chatInp').value=btn.textContent;sendChat();}
async function sendChat(){
  var inp=document.getElementById('chatInp');
  var text=inp.value.trim(); if(!text) return;
  inp.value=''; appendMsg('u',text);
  chatMsgs.push({role:'user',content:text});
  if(chatMsgs.length>20) chatMsgs=chatMsgs.slice(-20);
  var snd=document.getElementById('chatSnd'); snd.disabled=true;
  var tid='t'+Date.now();
  var ml=document.getElementById('chatMsgs');
  ml.innerHTML+='<div class="typing" id="'+tid+'"><span class="td"></span><span class="td"></span><span class="td"></span></div>';
  ml.scrollTop=ml.scrollHeight;
  try{
    var resp=await api('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:chatMsgs})});
    var t=document.getElementById(tid); if(t) t.remove();
    if(resp.error){appendMsg('err','错误: '+resp.error);}
    else{chatMsgs.push({role:'assistant',content:resp.reply});appendMsg('a',resp.reply);}
  }catch(e){
    var t=document.getElementById(tid); if(t) t.remove();
    appendMsg('err','连接失败: '+e.message);
  }
  snd.disabled=false; document.getElementById('chatInp').focus();
}
function appendMsg(type,text){
  var el=document.getElementById('chatMsgs');
  var now=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  var html=text.replace(/\*\*(.*?)\*\*/g,'<b>$1</b>').replace(/\n/g,'<br>');
  var cls=type==='u'?'msg-u':type==='err'?'msg-a msg-err':'msg-a';
  el.innerHTML+='<div class="msg '+cls+'">'+html+'<div class="msg-t">'+now+'</div></div>';
  el.scrollTop=el.scrollHeight;
}

function jumpTo(id){var el=document.getElementById(id);if(el)el.scrollIntoView({behavior:'smooth',block:'center'});}

init();
</script>
</body>
</html>"""

if __name__ == "__main__":
    print("=" * 50)
    print("🚀 A股智能分析 v4 启动")
    print(f"📁 目录: {BASE_DIR}")
    print(f"🔑 Key: {'✅ 已配置' if load_key() else '❌ 未配置（请点设置填写）'}")
    print(f"📊 打开浏览器访问: http://localhost:5000")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=5000)