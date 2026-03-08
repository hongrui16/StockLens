"""
StockLens - A股 + 美股智能分析
运行: python app.py
依赖: pip install flask yfinance openai requests
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
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── A股目录 ──
CN_DATA_DIR      = DATA_DIR / "china"
CN_DATA_DIR.mkdir(parents=True, exist_ok=True)
CN_ARCHIVE_DIR   = CN_DATA_DIR / "archive"
CN_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
CN_STATUS_FILE   = CN_DATA_DIR / ".status.json"
CN_CONFIG_FILE   = CN_DATA_DIR / "config.json"
CN_KEY_FILE      = CN_DATA_DIR / "deepseek_key.txt"
CN_PORTFOLIO_FILE= CN_DATA_DIR / "portfolio.json"

# ── 政策主线目录（在 china/ 下） ──
POLICY_DATA_DIR   = CN_DATA_DIR / "policy"
POLICY_DATA_DIR.mkdir(parents=True, exist_ok=True)
POLICY_STATUS_FILE= POLICY_DATA_DIR / ".status.json"
POLICY_LATEST_FILE= POLICY_DATA_DIR / "latest.json"
_policy_running = False

# ── 美股目录 ──
US_DATA_DIR      = DATA_DIR / "us"
US_DATA_DIR.mkdir(parents=True, exist_ok=True)
US_ARCHIVE_DIR   = US_DATA_DIR / "archive"
US_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
US_STATUS_FILE   = US_DATA_DIR / ".status.json"
US_CONFIG_FILE   = US_DATA_DIR / "config.json"
US_KEY_FILE      = US_DATA_DIR / "openai_key.txt"
US_PORTFOLIO_FILE= US_DATA_DIR / "portfolio.json"

# ── 一次性数据迁移（旧 data/ → 新 data/china/ 和 data/us/） ──
def _migrate_old_data():
    import shutil
    moves = [
        # A股
        (DATA_DIR / "config.json",        CN_CONFIG_FILE),
        (DATA_DIR / "portfolio.json",     CN_PORTFOLIO_FILE),
        (DATA_DIR / "deepseek_key.txt",   CN_KEY_FILE),
        # 美股
        (DATA_DIR / "openai_key.txt",     US_KEY_FILE),
        # archive
        (DATA_DIR / "archive",            CN_ARCHIVE_DIR),
        (DATA_DIR / "policy" / "archive", POLICY_DATA_DIR),
    ]
    for src, dst in moves:
        if src.exists() and not dst.exists():
            try:
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            except Exception as e:
                print(f"[迁移] {src} → {dst} 失败: {e}")
    # 旧 us/ 下的文件
    old_us = DATA_DIR / "us"
    if old_us.exists():
        for fname in ["config.json", "portfolio.json", "openai_key.txt"]:
            src = old_us / fname
            dst = US_DATA_DIR / fname
            if src.exists() and not dst.exists():
                try: shutil.copy2(src, dst)
                except Exception: pass
        old_us_arc = old_us / "archive"
        if old_us_arc.exists() and not US_ARCHIVE_DIR.exists():
            try: shutil.copytree(old_us_arc, US_ARCHIVE_DIR)
            except Exception: pass
_migrate_old_data()
DEFAULT_WATCHLIST = [
    {"code": "002236", "name": "大华技术"},
    {"code": "002415", "name": "海康威视"},
    {"code": "601360", "name": "360"},
    {"code": "603000", "name": "人民网"},
]

app = Flask(__name__)
_running = False

@app.route('/figure/<path:filename>')
def static_files(filename):
    from flask import send_from_directory
    return send_from_directory(Path(__file__).parent / 'figure', filename)

# ── 美股常量 ──
DEFAULT_US_WATCHLIST = [
    {"ticker": "NVDA",  "name": "NVIDIA"},
    {"ticker": "AAPL",  "name": "Apple"},
    {"ticker": "TSLA",  "name": "Tesla"},
    {"ticker": "MSFT",  "name": "Microsoft"},
]
_us_running = False

# ── 政策主线常量 ──

# 六大政策主线板块（东方财富板块代码）
POLICY_SECTORS = {
    "AI算力/大模型": {"em_code": "BK0734", "policy": "国家AI战略、算力基础设施投入、大模型产业化", "horizon": "2-5年"},
    "半导体/芯片":   {"em_code": "BK0248", "policy": "国产替代、光刻机突破、芯片自主可控",       "horizon": "3-5年"},
    "人形机器人":    {"em_code": "BK0793", "policy": "制造业升级、智能制造、工业母机",             "horizon": "2-4年"},
    "低空经济":      {"em_code": "BK1037", "policy": "低空经济写入政府工作报告、无人机商业化",     "horizon": "2-3年"},
    "新能源/储能":   {"em_code": "BK0435", "policy": "碳中和目标2060、新型电力系统、储能配套",     "horizon": "3-5年"},
    "国防军工":      {"em_code": "BK0173", "policy": "国防现代化、装备升级换代、军民融合",         "horizon": "3-5年"},
    "创新药/医疗器械":{"em_code": "BK0465", "policy": "健康中国2030、创新药优先审评、医疗器械国产替代", "horizon": "2-4年"},
}

# ── helpers ──
def jload(p):
    try: return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else None
    except Exception: return None

def jsave(p, d):
    Path(p).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def load_key():
    p = Path(CN_KEY_FILE); return p.read_text(encoding="utf-8").strip() if p.exists() else ""
def save_key(k): Path(CN_KEY_FILE).write_text(k.strip(), encoding="utf-8")
def load_openai_key():
    p = Path(US_KEY_FILE); return p.read_text(encoding="utf-8").strip() if p.exists() else ""
def save_openai_key(k): Path(US_KEY_FILE).write_text(k.strip(), encoding="utf-8")
def load_cfg():
    d = jload(CN_CONFIG_FILE)
    if not d or not isinstance(d.get("watchlist"), list) or len(d.get("watchlist", [])) == 0:
        d = {"watchlist": DEFAULT_WATCHLIST}
        jsave(CN_CONFIG_FILE, d)
    return d
def save_cfg(d):
    d.pop("deepseek_api_key", None)
    jsave(CN_CONFIG_FILE, d)
def load_port(): return jload(CN_PORTFOLIO_FILE) or {}
def save_port(d): jsave(CN_PORTFOLIO_FILE, d)

def load_latest():
    files = sorted(CN_ARCHIVE_DIR.glob("analysis_*.json"), reverse=True)
    for f in files:
        d = jload(f)
        if d and d.get("status") == "done": return d
    return {"status": "no_data"}

# ── 美股存储 ──
def load_us_cfg():
    d = jload(US_CONFIG_FILE)
    if not d or not isinstance(d.get("watchlist"), list) or len(d.get("watchlist", [])) == 0:
        d = {"watchlist": DEFAULT_US_WATCHLIST}
        jsave(US_CONFIG_FILE, d)
    return d
def save_us_cfg(d): jsave(US_CONFIG_FILE, d)
def load_us_port(): return jload(US_PORTFOLIO_FILE) or {}
def save_us_port(d): jsave(US_PORTFOLIO_FILE, d)

def load_us_latest():
    files = sorted(US_ARCHIVE_DIR.glob("analysis_*.json"), reverse=True)
    for f in files:
        d = jload(f)
        if d and d.get("status") == "done": return d
    return {"status": "no_data"}

def _save_archive_to(archive_dir, result):
    """原子写入 archive，保留最近14天且最多50个文件"""
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = archive_dir / f"analysis_{ts}.json"
    tmp    = archive_dir / f".tmp_{ts}.json"
    try:
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(target)
    except Exception:
        if tmp.exists(): tmp.unlink()
        raise
    # 清理：超过14天 或 超过50个文件 的旧文件
    files = sorted(archive_dir.glob("analysis_*.json"))
    cutoff = datetime.now() - timedelta(days=14)
    to_delete = []
    for f in files:
        try:
            if datetime.strptime(f.stem.replace("analysis_", ""), "%Y%m%d_%H%M%S") < cutoff:
                to_delete.append(f)
        except Exception:
            pass
    for f in to_delete:
        f.unlink()
        files.remove(f)
    while len(files) > 50:
        files.pop(0).unlink()

def save_cn_latest(result):     _save_archive_to(CN_ARCHIVE_DIR, result)
def save_us_latest(result):     _save_archive_to(US_ARCHIVE_DIR, result)
def save_policy_latest(result):
    """政策主线只保留最新一份，直接覆盖"""
    tmp = POLICY_DATA_DIR / ".tmp_latest.json"
    try:
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(POLICY_LATEST_FILE)
    except Exception:
        if tmp.exists(): tmp.unlink()
        raise

def load_policy_latest():
    d = jload(POLICY_LATEST_FILE)
    return d if d and d.get("status") == "done" else {"status": "no_data"}

# ── 政策主线数据抓取 ──
def fetch_policy_sector_data():
    """抓取六大政策板块：板块资金流 + 板块内主力净流入 Top5 个股 + yfinance 90日走势"""
    results = []
    for sector_name, info in POLICY_SECTORS.items():
        sector = {
            "name": sector_name,
            "policy": info["policy"],
            "horizon": info["horizon"],
            "em_code": info["em_code"],
            "stocks": [],
            "sector_today_flow": None,
            "sector_today_chg": None,
        }
        # 1. 板块今日资金流向
        try:
            url_s = ("https://push2.eastmoney.com/api/qt/clist/get"
                     "?cb=&pn=1&pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                     "&fltt=2&invt=2&fid=f62&fs=m:90+t:2+f:!50"
                     "&fields=f12,f14,f62,f3&_=1")
            ds = _http_get(url_s, timeout=6)
            for item in (ds.get("data", {}).get("diff", []) or []):
                if str(item.get("f12", "")) == info["em_code"].replace("BK", ""):
                    flow = item.get("f62", 0)
                    sector["sector_today_flow"] = round(float(flow)/1e8, 2) if flow and str(flow) != "-" else 0
                    sector["sector_today_chg"]  = item.get("f3", 0)
                    break
        except Exception: pass

        # 2. 板块内 主力净流入 Top5 个股
        try:
            url_c = ("https://push2.eastmoney.com/api/qt/clist/get"
                     f"?cb=&pn=1&pz=5&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                     f"&fltt=2&invt=2&fid=f62&fs=b:{info['em_code']}"
                     f"&fields=f12,f14,f2,f3,f62,f10,f8&_=1")
            dc = _http_get(url_c, timeout=8)
            items = dc.get("data", {}).get("diff", []) or []
            for s in items[:5]:
                code  = str(s.get("f12", ""))
                name  = s.get("f14", "")
                price = s.get("f2",  "-")
                chg   = s.get("f3",  "-")
                flow  = s.get("f62", 0)
                vr    = s.get("f10", 0)
                tr    = s.get("f8",  0)
                if not code or not name or str(chg) == "-": continue
                flow_yi = round(float(flow)/1e8, 2) if flow and str(flow) != "-" else 0
                stock = {
                    "code": code, "name": name, "price": price,
                    "change_pct": chg, "net_inflow_yi": flow_yi,
                    "vol_ratio": vr, "turnover": tr,
                    "ma30": None, "ma60": None,
                    "high_90d": None, "pct_from_high": None,
                    "trend_90d": None,
                }
                # 3. yfinance 90日数据（均线 + 距高点距离 + 趋势）
                try:
                    import yfinance as _yf
                    ticker_code = code + (".SS" if code.startswith("6") else ".SZ")
                    df = _yf.Ticker(ticker_code).history(period="6mo")
                    if not df.empty and len(df) >= 30:
                        closes = df["Close"]
                        cur = float(closes.iloc[-1])
                        ma30 = round(float(closes.rolling(30).mean().iloc[-1]), 2)
                        ma60 = round(float(closes.rolling(60).mean().iloc[-1]), 2) if len(closes) >= 60 else None
                        high90 = round(float(closes.tail(90).max()), 2)
                        pct_from_high = round((cur - high90) / high90 * 100, 1)
                        # 90日趋势：前45日均价 vs 后45日均价
                        mid = len(closes) // 2
                        trend = "上升" if float(closes.iloc[-1]) > float(closes.iloc[mid]) else "下降"
                        stock.update({
                            "ma30": ma30, "ma60": ma60,
                            "high_90d": high90, "pct_from_high": pct_from_high,
                            "trend_90d": trend,
                        })
                except Exception: pass
                sector["stocks"].append(stock)
        except Exception: pass
        results.append(sector)
    return results

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

def fetch_candidate_pool():
    """实盘选股候选池：东方财富 今日主力净流入 Top 30
    包含真实的代码、名称、涨幅、净流入、量比、换手率
    AI 推荐必须且只能从此池中选股，杜绝幻觉编造"""
    try:
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               "?cb=&pn=1&pz=30&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
               "&fltt=2&invt=2&fid=f62"
               "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
               "&fields=f12,f14,f2,f3,f62,f10,f8&_=1")
        d = _http_get(url, timeout=8)
        items = d.get("data", {}).get("diff", []) or []
        pool = []
        for s in items:
            code  = str(s.get("f12", ""))
            name  = s.get("f14", "")
            price = s.get("f2", "-")
            chg   = s.get("f3", "-")
            flow  = s.get("f62", 0)   # 主力净流入（元）
            vr    = s.get("f10", 0)   # 量比
            tr    = s.get("f8",  0)   # 换手率%
            # 过滤无效数据
            if not code or not name: continue
            if str(flow) == "-" or str(chg) == "-": continue
            flow_yi = round(float(flow) / 1e8, 2) if flow else 0
            pool.append({
                "code":          code,
                "name":          name,
                "price":         price,
                "change_pct":    chg,
                "net_inflow_yi": flow_yi,  # 主力净流入（亿元）
                "vol_ratio":     vr,       # 量比：>1.5 今日活跃
                "turnover":      tr,       # 换手率%：>10% 过热警惕
            })
        return pool
    except Exception:
        return []


def fetch_us_stock(ticker, name=""):
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="5y")
        if df.empty: return {"ticker": ticker, "name": name or ticker, "error": "No data"}
        close = df["Close"]; volume = df["Volume"]
        cur  = round(float(close.iloc[-1]), 2)
        prev = round(float(close.iloc[-2]), 2) if len(close) > 1 else cur
        chg  = round((cur - prev) / prev * 100, 2)
        vr   = round(float(volume.iloc[-1] / volume.mean()), 2)
        def ma(n):
            v = close.rolling(n).mean().iloc[-1]
            return round(float(v), 2) if str(v) != "nan" else None
        def ds(lst, n=60):
            if len(lst) <= n: return lst
            s = max(1, len(lst) // n); return lst[::s]
        sparks = {
            "5d":   [round(float(v), 2) for v in close.tail(5)],
            "30d":  ds([round(float(v), 2) for v in close.tail(30)], 30),
            "90d":  ds([round(float(v), 2) for v in close.tail(90)], 60),
            "180d": ds([round(float(v), 2) for v in close.tail(180)], 60),
            "365d": ds([round(float(v), 2) for v in close.tail(365)], 60),
            "5y":   ds([round(float(v), 2) for v in close], 60),
        }
        try: info = t.info
        except Exception: info = {}
        pe     = info.get("trailingPE")
        pb     = info.get("priceToBook")
        mc     = round(info.get("marketCap", 0) / 1e9, 1) if info.get("marketCap") else None
        sector = info.get("sector", "")
        return {
            "ticker": ticker, "name": name or info.get("shortName", ticker),
            "close": cur, "change_pct": chg,
            "ma5": ma(5), "ma20": ma(20), "ma50": ma(50), "ma200": ma(200),
            "vol_ratio": vr,
            "pe": round(pe, 1) if pe else None,
            "pb": round(pb, 2) if pb else None,
            "mkt_cap_b": mc, "sector": sector,
            "sparks": sparks, "error": None
        }
    except Exception as e:
        return {"ticker": ticker, "name": name or ticker, "error": str(e)}

def fetch_us_market():
    try:
        r = {}
        for name, tkr in {"S&P 500": "^GSPC", "Nasdaq": "^IXIC", "Dow Jones": "^DJI", "VIX": "^VIX"}.items():
            df = yf.Ticker(tkr).history(period="2d")
            if not df.empty and len(df) >= 2:
                c = float(df["Close"].iloc[-1]); p = float(df["Close"].iloc[-2])
                r[name] = {"close": round(c, 2), "change_pct": round((c - p) / p * 100, 2)}
            else:
                r[name] = {"close": None, "change_pct": None}
        return r
    except Exception as e:
        return {"error": str(e)}

def fetch_us_news():
    results = []
    try:
        nr = yf.Ticker("^GSPC").news or []
        for n in nr[:8]:
            if not n: continue
            # Support both old (flat) and new (nested content{}) yfinance API
            content_obj = n.get("content", {}) or {}
            title = content_obj.get("title", "") or n.get("title", "") or ""
            # New API: content.canonicalUrl.url  Old API: link
            url = (content_obj.get("canonicalUrl", {}) or {}).get("url", "") \
                  or content_obj.get("url", "") \
                  or n.get("link", "") or n.get("url", "") or ""
            if title:
                results.append({"title": title, "url": url})
    except Exception: pass
    return results or [{"title": "No news available", "url": ""}]

def fetch_us_hot():
    result = {}
    # 板块 ETF 涨跌
    sector_etfs = [
        ("科技 XLK", "XLK"), ("半导体 SOXX", "SOXX"), ("AI/纳指 QQQ", "QQQ"),
        ("金融 XLF", "XLF"), ("能源 XLE", "XLE"), ("医疗 XLV", "XLV"),
        ("消费 XLY", "XLY"), ("工业 XLI", "XLI"),
    ]
    try:
        flows = []
        for name, etf in sector_etfs:
            df = yf.Ticker(etf).history(period="2d")
            if not df.empty and len(df) >= 2:
                c = float(df["Close"].iloc[-1]); p = float(df["Close"].iloc[-2])
                chg = round((c - p) / p * 100, 2)
                flows.append((name, chg))
        flows.sort(key=lambda x: x[1], reverse=True)
        result["sector_flows"] = [f"{n} {'+' if c >= 0 else ''}{c}%" for n, c in flows]
    except Exception:
        result["sector_flows"] = []
    # VIX 恐慌指数
    try:
        vix_df = yf.Ticker("^VIX").history(period="2d")
        if not vix_df.empty:
            vix = round(float(vix_df["Close"].iloc[-1]), 2)
            label = "极度贪婪" if vix < 12 else "贪婪" if vix < 16 else "中性" if vix < 20 else "恐慌" if vix < 30 else "极度恐慌"
            result["fear_greed"] = f"VIX={vix} ({label})"
    except Exception:
        result["fear_greed"] = ""
    # 今日涨幅榜（Yahoo Finance screener）
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=10&region=US&lang=en-US"
        d = _http_get(url, timeout=6)
        quotes = d.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        result["top_gainers"] = [
            f"{q.get('symbol','')} {q.get('shortName','')[:15]} +{round(q.get('regularMarketChangePercent', 0), 2)}%"
            for q in quotes[:8] if q.get("symbol")
        ]
    except Exception:
        result["top_gainers"] = []
    return result

def fetch_us_candidate_pool():
    """美股候选池：从 Yahoo Finance 抓取今日放量/涨幅 Top 股票，防止 AI 幻觉"""
    candidates = []
    try:
        # 1. 今日涨幅榜 Top20
        url_gain = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=20&region=US&lang=en-US"
        d = _http_get(url_gain, timeout=8)
        quotes = d.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        for q in quotes:
            sym = q.get("symbol", "")
            if not sym or "=" in sym or len(sym) > 6:
                continue
            chg = round(q.get("regularMarketChangePercent", 0), 2)
            vol_avg = q.get("averageDailyVolume3Month") or 1
            vol_today = q.get("regularMarketVolume") or 0
            vol_ratio = round(vol_today / vol_avg, 2) if vol_avg else 0
            candidates.append({
                "ticker": sym,
                "name": (q.get("shortName") or q.get("longName") or sym)[:20],
                "change_pct": chg,
                "price": round(q.get("regularMarketPrice", 0), 2),
                "volume_ratio": vol_ratio,
                "market_cap_b": round((q.get("marketCap") or 0) / 1e9, 1),
                "source": "day_gainers",
            })
    except Exception:
        pass
    try:
        # 2. 今日成交量最大 Top20（动能来源）
        url_vol = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=20&region=US&lang=en-US"
        d2 = _http_get(url_vol, timeout=8)
        quotes2 = d2.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        existing = {c["ticker"] for c in candidates}
        for q in quotes2:
            sym = q.get("symbol", "")
            if not sym or "=" in sym or len(sym) > 6 or sym in existing:
                continue
            chg = round(q.get("regularMarketChangePercent", 0), 2)
            vol_avg = q.get("averageDailyVolume3Month") or 1
            vol_today = q.get("regularMarketVolume") or 0
            vol_ratio = round(vol_today / vol_avg, 2) if vol_avg else 0
            candidates.append({
                "ticker": sym,
                "name": (q.get("shortName") or q.get("longName") or sym)[:20],
                "change_pct": chg,
                "price": round(q.get("regularMarketPrice", 0), 2),
                "volume_ratio": vol_ratio,
                "market_cap_b": round((q.get("marketCap") or 0) / 1e9, 1),
                "source": "most_actives",
            })
    except Exception:
        pass
    # 过滤：去掉今日已涨超15%的（追高风险过高）
    candidates = [c for c in candidates if c["change_pct"] <= 15]

    # ── Fallback：Yahoo Screener 失败时，用 yfinance 抓主流股今日行情 ──
    if len(candidates) < 5:
        FALLBACK_TICKERS = [
            # AI/科技
            ("NVDA", "NVIDIA"), ("MSFT", "Microsoft"), ("META", "Meta"), ("GOOGL", "Alphabet"),
            ("AMZN", "Amazon"), ("AAPL", "Apple"), ("TSM", "TSMC"), ("AMD", "AMD"),
            ("AVGO", "Broadcom"), ("CRM", "Salesforce"),
            # 能源/工业
            ("XOM", "ExxonMobil"), ("CVX", "Chevron"), ("GE", "GE Aerospace"),
            # 金融
            ("JPM", "JPMorgan"), ("BAC", "BofA"), ("GS", "Goldman Sachs"),
            # 医疗
            ("UNH", "UnitedHealth"), ("LLY", "Eli Lilly"), ("ABBV", "AbbVie"),
            # 消费/其他
            ("TSLA", "Tesla"), ("NFLX", "Netflix"), ("UBER", "Uber"), ("SHOP", "Shopify"),
        ]
        try:
            tickers_str = " ".join(t for t, _ in FALLBACK_TICKERS)
            import yfinance as _yf
            fb = _yf.download(tickers_str, period="2d", auto_adjust=True, progress=False)
            close = fb["Close"]
            for ticker, name in FALLBACK_TICKERS:
                try:
                    if ticker not in close.columns: continue
                    prices = close[ticker].dropna()
                    if len(prices) < 2: continue
                    prev, cur = float(prices.iloc[-2]), float(prices.iloc[-1])
                    chg = round((cur - prev) / prev * 100, 2)
                    if chg > 15: continue
                    candidates.append({
                        "ticker": ticker, "name": name,
                        "change_pct": chg, "price": round(cur, 2),
                        "volume_ratio": 1.0, "market_cap_b": 0,
                        "source": "fallback",
                    })
                except Exception: continue
        except Exception: pass

    return candidates[:40]

POLICY = """
A股政策主线（2025-2026中长期）：
七大政策板块（按当前资金热度排序）：
  1. AI算力/大模型 — 国产大模型落地、算力基础设施、CPO/光模块/液冷服务器
  2. 半导体/芯片 — 国产替代加速、存储芯片/设备/材料全链条
  3. 人形机器人 — 特斯拉/华为产业链、电机/减速器/传感器核心零部件
  4. 低空经济 — eVTOL/无人机/空管，各省市加速落地
  5. 国防军工 — 信息化装备、航空发动机、导弹产业链
  6. 新能源/储能 — 大储/工商储政策加持，出海逻辑持续
  7. 创新药/医疗器械 — 国产创新药出海+集采后周期修复

注意：
政策方向仅作为长期产业背景，不代表当前资金流入。
不得仅凭政策方向推荐股票。
股票推荐必须遵循以下优先级：
1. 实时资金（权重最高）
   - 板块资金净流入排名
   - 涨幅榜集中度（同板块多只上涨=题材扩散）
   - 成交额放大倍数
2. 市场情绪
   - 连板高度（情绪温度计）
   - 龙头是否健康换手（不炸板=主力未撤）
   - 是否处于情绪高潮期（追高风险）还是刚启动期
3. 政策共振（加分项，不单独构成推荐理由）
   - 当资金流入板块与以上七大政策方向一致时，评分+10
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

def run_ai(api_key, watchlist_data, market, news, portfolio, hot_data=None, candidates=None):
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

    # 市场热点数据
    hot_str = ""
    if hot_data:
        if hot_data.get("top_gainers"):
            hot_str += "\n## 今日涨幅榜（实时）\n" + "\n".join(f"  {s}" for s in hot_data["top_gainers"])
        if hot_data.get("sector_flow"):
            hot_str += "\n## 板块主力资金净流入（实时）\n" + "\n".join(f"  {s}" for s in hot_data["sector_flow"])
        if hot_data.get("limit_up"):
            hot_str += "\n## 今日连板股（情绪温度计）\n" + "\n".join(f"  {s}" for s in hot_data["limit_up"])

    # 候选池：AI 推荐的唯一来源
    cand_str = "[]"
    cand_note = "【警告】候选池为空，本次不得推荐任何股票，recommendations 返回空列表 []。"
    if candidates:
        cand_str = json.dumps(candidates, ensure_ascii=False)
        cand_note = (
            f"候选池共 {len(candidates)} 只股票，字段含义：\n"
            "  code=代码 name=名称 price=现价 change_pct=涨跌幅%\n"
            "  net_inflow_yi=今日主力净流入(亿元) vol_ratio=量比 turnover=换手率%\n"
            "量比参考：>2 今日资金高度活跃；换手率参考：>15% 过热追高有风险，建议候选回踩。"
        )

    wl_codes = "、".join(s.get("code","") for s in watchlist_data if s.get("code"))

    prompt = f"""你是资深A股分析师。今天{datetime.now().strftime('%Y年%m月%d日')}。

## 大盘指数
{mkt_str}

{port_str}

## 自选股实时数据（需逐一分析）
字段说明：close=现价 change_pct=涨跌幅% vol_ratio=量比（今日成交/近期均量）
ma5/ma10/ma20/ma30/ma60=各周期均线（权重仅10%，辅助趋势，不作买卖信号）
{stocks_str}

## 今日财经新闻
{news_str}
{hot_str}

{FRAMEWORK}
{POLICY}

---
【自选股分析】对每只自选股，只根据你实际拿到的数据判断，不要推断你没有的信息：

可判断的维度：
  资金(40%)：该股是否出现在今日板块资金流入榜？所在板块板块名是否在 sector_flow 前列？
  热度(30%)：从新闻标题和涨幅榜判断题材是否仍在发酵；连板股所在板块与本股是否相关？
  量价(20%)：vol_ratio>1.5 且价格上涨 = 有量能支撑；vol_ratio<0.8 且价格下跌 = 缩量回调健康；
             vol_ratio>2 且价格下跌 = 放量出货警示
  趋势(10%)：仅用 ma30/ma60 判断中期方向，不作买卖点

【严禁推断无数据字段】：你没有换手率、封单、炸板、历史成交额序列数据，不得在分析中使用这些词语。

---
## 今日实盘选股候选池（真实主力净流入数据，AI 推荐的唯一来源）
{cand_note}
{cand_str}

【实盘推荐铁律——关乎资金安全，必须绝对遵守】
1. recommendations 中的每一只股票，【必须且只能】从上方候选池中挑选，代码和名称必须与候选池完全一致。
2. 禁止出现候选池以外的任何股票代码或名称，禁止凭记忆或推断填写代码。
3. 禁止推荐以下自选股代码：{wl_codes}
4. 如果候选池为空，或池中所有股票今日涨幅均超过 9%（追高风险过大），返回 recommendations: []，宁可不推，绝不乱推。
5. 推荐理由必须引用候选池中的真实数据，格式示例：「净流入X亿，量比X，涨幅X%」。
6. 换手率 >15% 的标的，必须在 risk 字段注明「今日换手率过高，建议等回踩再介入」。

返回JSON（不要任何markdown包裹，直接返回JSON）:
{{
  "market_summary": "100字大盘综述，重点说明资金流向和市场情绪",
  "market_sentiment": "偏多|震荡|偏空",
  "watchlist_analysis": [
    {{
      "code": "股票代码（直接写6位代码如000001）",
      "score_breakdown": "资金:高/中/低 热度:高/中/低 量价:健康/中性/警示 趋势:上/横/下",
      "sector_heat": "该股板块是否出现在今日资金流入榜，及流入金额（若未出现则写：板块未进入流入榜）",
      "volume_signal": "必须写出量比数值，如：放量上涨(量比1.8)|缩量回调(量比0.6)|放量下跌警示(量比2.3)",
      "suggestion": "买入|关注|持有|观望|减仓",
      "entry": "必须包含该股代码，每只股写法不同。如：XXXX等板块资金回流时介入；若XXXX量比回落至1.5以下可分批",
      "exit": "必须包含该股代码，每只股写法不同。如：XXXX若出现放量阴线（量比>2）立即减仓；或板块资金连续流出2日止损",
      "reason": "60字：必须包含该股代码、vol_ratio数值、板块资金流入情况三项，缺一不可"
    }}
  ],
  "hot_sectors": [{{"name": "板块名", "em_keyword": "东方财富搜索关键词"}}],
  "recommendations": [
    {{
      "code": "必须是候选池中的真实6位代码",
      "name": "必须与候选池中一致的名称",
      "sector": "推测所属板块（基于候选池数据）",
      "term": "短线|中长线",
      "score": "资金XX分+热度XX分+量价XX分+趋势XX分=总分XX/100",
      "catalyst": "必须引用真实数据，格式：净流入X亿，量比X，涨幅X%",
      "entry": "必须包含该股代码，每只股不同。如：XXXX净流入X亿且量比回落至合理区间时分批；或等当日高点回踩3%再介入",
      "stop_signal": "必须包含该股代码，每只股不同，禁止5只写相同文字。如：XXXX若放量跌破今日低点（量比>2）→止损；或所在板块连续2日主力净流出→减仓",
      "reason": "80字：基于候选池真实数据的分析",
      "risk": "风险提示（换手率>15%必须注明追高风险）",
      "suggestion": "买入|关注",
      "eastmoney_code": "sz000000或sh600000格式"
    }}
  ],
  "risk_warning": "50字整体风险提示"
}}
从候选池中选 5-8 只（短线3-4只+中长线2-4只）：优先净流入大、量比适中(1.5-3)、涨幅未超9%的标的。换手率>15%须标注追高风险但仍可推荐。只返回JSON。"""
    resp = client.chat.completions.create(model="deepseek-chat", max_tokens=5000, temperature=0.3,
                                          messages=[{"role":"user","content":prompt}])
    text = resp.choices[0].message.content.strip()
    # strip markdown fences if present
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"): p = p[4:].strip()
            if p.startswith("{"): text = p; break
    text = text.strip()
    if not text.endswith("}"):
        last = text.rfind("\n  }")
        if last > 0: text = text[:last+4] + "\n}"
    return json.loads(text)

# ── 政策主线 AI（中长线，逻辑完全独立于短线） ──
def run_policy_ai(api_key, sectors_data, market):
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    today = datetime.now().strftime("%Y年%m月%d日")
    mkt_str = "\n".join(
        f"  {n}: {v.get('close')} ({'+' if (v.get('change_pct') or 0)>=0 else ''}{v.get('change_pct')}%)"
        for n,v in market.items() if isinstance(v,dict) and "close" in v
    ) if "error" not in market else "获取失败"

    def slim(sec):
        return {
            "name": sec["name"], "policy": sec.get("policy",""),
            "horizon": sec.get("horizon",""),
            "sector_today_flow": sec.get("sector_today_flow"),
            "sector_today_chg": sec.get("sector_today_chg"),
            "stocks": [{
                "code": s.get("code"), "name": s.get("name"),
                "change_pct": s.get("change_pct"),
                "net_inflow_yi": s.get("net_inflow_yi"),
                "vol_ratio": s.get("vol_ratio"),
                "pct_from_high": s.get("pct_from_high"),
                "trend_90d": s.get("trend_90d"),
                "ma30": s.get("ma30"), "ma60": s.get("ma60"),
            } for s in sec.get("stocks", [])]
        }

    FRAMEWORK = """阶段判断：
【酝酿期】flow偶正，个股分化 → 建仓≤30%
【启动期】flow持续正，龙头起 → 买二线，仓位50-60%
【加速期】普涨pct_from_high≈0 → 不追，设止盈
【调整期】pct_from_high -20%~-40%，flow转负但政策未变 → 最佳建仓窗口
【衰退期】flow长期负，trend下降 → 清仓
字段：sector_today_flow=今日板块净流入亿元，pct_from_high=距90日高点回撤，trend_90d=趋势方向"""

    OUTPUT_TMPL = """返回JSON（不要markdown）：
{
  %s
  "sectors": [{"name":"板块名","stage":"酝酿期|启动期|加速期|调整期|衰退期","stage_reason":"30字内","policy_strength":"强|中|弱","policy_note":"20字催化剂","action":"建仓|加仓|持有|等待回调|不建议介入|减仓","best_entry_window":"30字入场时机"}],
  "not_recommended": ["板块名：原因15字内"]
}
只返回JSON。"""

    def _call(batch, include_macro):
        macro_field = '"macro_view": "60字宏观摘要",' if include_macro else ""
        prompt = f"""你是A股政策主线研究员。今天{today}。大盘：{mkt_str}
{FRAMEWORK}
板块数据：
{json.dumps([slim(s) for s in batch], ensure_ascii=False, indent=2)}
{OUTPUT_TMPL % macro_field}"""
        resp = client.chat.completions.create(
            model="deepseek-chat", max_tokens=2000, temperature=0.3,
            messages=[{"role": "user", "content": prompt}]
        )
        return _parse_policy_json(resp.choices[0].message.content)

    r1 = _call(sectors_data[:4], include_macro=True)
    r2 = _call(sectors_data[4:], include_macro=False)

    risk_resp = client.chat.completions.create(
        model="deepseek-chat", max_tokens=150, temperature=0.3,
        messages=[{"role": "user", "content":
            f"今天{today}，大盘：{mkt_str}。用40字说明当前A股宏观风险，只返回纯文本。"}]
    )
    return {
        "macro_view": r1.get("macro_view", ""),
        "sectors": r1.get("sectors", []) + r2.get("sectors", []),
        "market_risk": risk_resp.choices[0].message.content.strip(),
        "not_recommended": list(set(r1.get("not_recommended", []) + r2.get("not_recommended", []))),
    }

def _parse_policy_json(text):
    text = text.strip()
    if "```" in text:
        for part in text.split("```"):
            p = part.strip()
            if p.startswith("json"): p = p[4:].strip()
            if p.startswith("{"): text = p; break
    try:
        return json.loads(text)
    except Exception:
        for marker in ["\n    }\n  ]", "\n    }"]:
            last = text.rfind(marker)
            if last > 0:
                for suffix in ["\n  ]\n}", "\n  ],\n  \"not_recommended\": []\n}"]:
                    try:
                        return json.loads(text[:last+len(marker)] + suffix)
                    except Exception:
                        pass
        return {"sectors": [], "not_recommended": []}


# 美股三种持仓类型，策略完全不同
US_STOCK_TYPES = """
US Stock Types & Strategies:
  Growth (NVDA/MSFT/META): driven by earnings beats + multiple expansion. Earnings = biggest risk.
  Momentum: recent outperformers vs SPY. Driven by relative strength + volume breakouts. Strict stops.
  Value/Dividend: driven by dividend yield + asset quality. Focus on payout safety and cycle positioning.
  Thematic (AI/Nuclear/GLP-1): driven by sector narrative + ETF inflow + catalyst. High volatility.
"""

US_MACRO = """
US Macro Context (2025-2026):
Key themes: AI compute (NVDA/AMD/AVGO), software AI adoption (MSFT/CRM/SNOW),
GLP-1 pharma (LLY/NVO), nuclear power (CEG/VST), cybersecurity (CRWD/PANW).
Macro risks: Fed rate path (high rates compress growth multiples), USD strength (hurts international revenue),
VIX > 25 = caution; VIX > 30 = systemic risk, reduce all positions.
Recommendations MUST be backed by BOTH recent sector ETF inflow AND an earnings/catalyst driver.
Do NOT recommend purely on sector theme without confirming ETF momentum.
"""

def run_us_ai(api_key, watchlist_data, market, news, portfolio, hot_data=None, candidates=None):
    from openai import OpenAI
    client = OpenAI(api_key=api_key)  # OpenAI GPT-4o-mini
    mkt_str = "\n".join(
        f"  {n}: {v.get('close')} ({'+' if (v.get('change_pct') or 0) >= 0 else ''}{v.get('change_pct')}%)"
        for n, v in market.items() if isinstance(v, dict) and "close" in v
    ) if "error" not in market else "获取失败"
    # VIX level for context
    vix_val = market.get("VIX", {}).get("close", 0) or 0
    vix_note = f"VIX={vix_val} — " + ("Extreme Fear: systemic risk, reduce all positions" if vix_val > 30 else "Fear: risk-off, reduce high-beta" if vix_val > 25 else "Neutral" if vix_val > 18 else "Greed: watch for overvaluation")
    stocks_str = json.dumps([{k: v for k, v in s.items() if k != "sparks"} for s in watchlist_data], ensure_ascii=False, indent=2)
    hot_str = ""
    if hot_data:
        if hot_data.get("fear_greed"):
            hot_str += f"\n## 恐慌贪婪\n  {hot_data['fear_greed']}"
        if hot_data.get("sector_flows"):
            hot_str += "\n## 板块ETF今日涨跌\n" + "\n".join(f"  {s}" for s in hot_data["sector_flows"])
        if hot_data.get("top_gainers"):
            hot_str += "\n## 今日涨幅榜\n" + "\n".join(f"  {s}" for s in hot_data["top_gainers"])
    wl_tickers = "、".join(s.get("ticker", "").upper() for s in watchlist_data if s.get("ticker"))
    wl_count = len([s for s in watchlist_data if s.get("ticker")])
    # 持仓股也排除出推荐列表
    port_tickers = [t.upper() for t in (portfolio or {}).keys()]
    exclude_tickers = "、".join(sorted(set(wl_tickers.split('、') + port_tickers) - {''}))
    today = datetime.now().strftime("%Y-%m-%d")

    # 候选池
    cand_str = ""
    if candidates:
        cand_lines = []
        for c in candidates:
            sign = "+" if c["change_pct"] >= 0 else ""
            flag = " ⚠️高追" if c["change_pct"] > 8 else ""
            cand_lines.append(
                f"  {c['ticker']} {c['name']} | 涨跌{sign}{c['change_pct']}% | 量比{c['volume_ratio']} | 市值{c['market_cap_b']}B | {c['source']}{flag}"
            )
        cand_str = "## US Candidate Pool (ONLY recommend from this list)\n" + "\n".join(cand_lines)
    else:
        cand_str = "## US Candidate Pool\n  （今日数据获取失败，如无候选池数据则不得推荐个股）"

    prompt = f"""You are a senior US equity analyst. Today is {today}.
{vix_note}

## Market Data
{mkt_str}
{hot_str}

{cand_str}

## Watchlist Stocks (analyze each one)
{stocks_str}

{US_STOCK_TYPES}
{US_MACRO}

---
## WATCHLIST ANALYSIS RULES
Analyze each stock using this 5-step framework. All output text fields must be written in Chinese (中文).

**Step 1 — EARNINGS RISK (highest priority)**
- Earnings within 4 weeks? State approximate date.
- Position with >20% gain + earnings within 2 weeks → trim 30-50% before the event.
- Recent earnings BEAT (stock held gains, guidance raised) → confirm hold or add on pullbacks.
- Recent earnings MISS → reduce immediately, never average down on earnings misses.

**Step 2 — SECTOR ETF MOMENTUM (40% weight)**
- Which ETF covers this stock (XLK/SOXX/XLV/XLE/XLF/XLY/XLI etc.)?
- Is that ETF outperforming or underperforming SPY today?
- Risk-On or Risk-Off environment based on VIX and sector rotation?

**Step 3 — RELATIVE STRENGTH (20% weight)**
- Stronger or weaker than SPY over the past month? By how much?
- Volume on up days vs down days: heavy up volume = accumulation; heavy down volume = distribution.
- Distance from 52-week high: gauge of momentum strength.

**Step 4 — VOLUME/PRICE STRUCTURE (20% weight)**
- Today's vol_ratio: >1.5 = active, <0.8 = quiet.
- Up day + high volume = accumulation signal; down day + high volume = distribution warning.

**Step 5 — TECHNICAL (10% weight, confirmation only)**
- Use MA50/MA200 for trend direction only, never as buy/sell triggers.
- Never use moving averages as the sole stop-loss reason.

**Entry condition rules:**
- If VIX > 25 OR sector ETF in multi-day decline: entry = "暂不入场，等待ETF企稳后再评估" — this IS the correct answer, do not force a buy condition.
- If market is normal: give a stock-specific entry condition. Each stock must have a DIFFERENT entry condition.

【STRICT RULE 1】Do NOT include these tickers in recommendations (watchlist + existing holdings): {exclude_tickers}
【STRICT RULE 2 — CANDIDATE POOL】
- If the Candidate Pool above has 10+ stocks: ONLY recommend tickers from the pool. Copy ticker and name EXACTLY.
- If the pool has 1-9 stocks: Recommend from the pool first, then supplement with well-known sector ETF leaders (S&P 500 components) to reach 5 total. Mark supplemental picks with "market_leader" in the source field.
- If the pool is empty or unavailable: Recommend 5-7 well-known S&P 500 stocks based on today's market context (sector rotation, VIX level, earnings cycle). Do NOT fabricate tickers.
- NEVER invent tickers that don't exist.
- Prefer: volume_ratio 1.5-4 (active but not overheated), change_pct under 8%
- Flag tickers with change_pct > 8% as high chase risk in the risk field
【STRICT RULE 3 — WATCHLIST ANALYSIS REQUIRED】
- The watchlist_analysis array MUST contain EXACTLY {wl_count} items, one per watchlist ticker.
- The tickers to analyze are: {wl_tickers}
- Each item's "ticker" field MUST be one of the above tickers, copied EXACTLY (uppercase).
- Do NOT add extra tickers or omit any. Do NOT use tickers from the Candidate Pool here.
- entry 和 exit 字段必须针对每只股个性化，严禁不同股票写相同内容。
【STRICT RULE 4 — RECOMMENDATIONS COUNT】
- recommendations 数组必须包含 5~7 只股票，严禁返回少于5只。
- 分配：短线动能 1~2 只 + 财报催化 2~3 只 + 中长线主线 2~3 只。
- 候选池不足时必须用 S&P 500 成分股补足至5只，不得以候选池不足为由减少推荐数量。
- 每只推荐的 stop_signal 必须个性化，严禁5只用相同文字。

Return JSON only, no markdown. ALL text fields must be written in Chinese (中文), except ticker symbols and ETF codes.
{{
  "market_summary": "100字中文：聚焦VIX水平、板块轮动、美联储影响的市场摘要",
  "market_sentiment": "Risk-On|Neutral|Risk-Off",
  "watchlist_analysis": [
    {{
      "ticker": "直接写股票代码如NVDA",
      "stock_type": "Growth|Momentum|Value|Thematic",
      "earnings_alert": "只能写以下之一：无近期财报风险 / 财报已过结果beat或miss / 财报约在[具体月份]。无把握一律写无近期财报风险，严禁三只股写相同月份",
      "sector_etf": "必须写该股正确ETF（NVDA/AMD/AVGO→SOXX；AAPL/MSFT/GOOGL→XLK；TSLA/AMZN/HD→XLY；LLY/UNH→XLV；XOM/CVX→XLE）今日涨跌幅，如SOXX -1.2% 半导体板块偏弱",
      "relative_strength": "强于SPY|与SPY持平|弱于SPY，近一月具体幅度如-6%",
      "volume_signal": "必须写出vol_ratio数值，如：下跌放量派发(vol_ratio=1.8)|缩量整理健康(vol_ratio=0.6)",
      "suggestion": "Buy|Watch|Hold|Reduce|Sell",
      "entry": "必须含该股ticker+对应ETF名称，每只股不同写法，如：NVDA等SOXX企稳后分批买入；TSLA等XLY反弹确认后介入",
      "exit": "必须含该股ticker+对应ETF名称，每只股不同写法，如：NVDA若SOXX连跌3日放量→减仓50%；TSLA若XLY破近期低点→止损",
      "reason": "70字：必须包含该股ticker、对应ETF今日涨跌幅、vol_ratio数值、相对SPY强弱幅度，四项缺一不可"
    }}
  ],
  "hot_sectors": [{{"name": "sector name", "etf": "ETF ticker like XLK"}}],
  "recommendations": [
    {{
      "ticker": "ticker",
      "name": "公司名称",
      "sector": "所属板块（如Technology/Healthcare/Energy）",
      "stock_type": "Growth|Momentum|Value|Thematic",
      "term": "短线|中长线",
      "score": "ETF动能XX+业绩催化XX+相对强度XX+宏观XX=总分XX/100",
      "catalyst": "具体催化剂（中文）：如近期ETF资金流入+分析师升评；或即将财报+历史连续超预期",
      "earnings_risk": "只能写：无近期财报风险 / 财报约在[月]（无把握一律写无近期财报风险，严禁多只股写相同月份）/ 财报已过结果beat或miss",
      "entry": "必须含该股ticker+对应ETF，每只股不同。如：NVDA等SOXX反弹确认后分批；TSLA等XLY企稳后介入",
      "stop_signal": "必须含该股ticker+对应ETF名称，每只股写法不同。如：NVDA若SOXX连跌3日放量→减仓50%；TSLA若XLY破近期低点且VIX>25→止损",
      "reason": "80字中文：基于ETF动能+财报状态+相对强度的综合判断",
      "risk": "30字中文：该股最主要的风险点",
      "suggestion": "买入|关注"
    }}
  ],
  "risk_warning": "50字中文整体风险提示"
}}
推荐数量：必须返回5~7只，禁止返回空数组，禁止少于5只。
推荐结构：
- 短线动能 1~2 只：今日ETF领涨板块+放量突破，给出ETF反转时的具体止损条件
- 财报催化 2~3 只：即将财报且有超预期历史，说明财报日期和建议穿越仓位比例
- 中长线主线 2~3 只：AI/半导体/医疗/能源，ETF持续流入+分析师上调
候选池不足时，从当前市场板块轮动相关的S&P 500成分股中补充，确保总数≥5只。
每只股的 stop_signal 必须不同，结合该股财报时间/所属ETF/当前涨跌幅个性化。
所有字段必须用中文。只返回JSON。"""
    resp = client.chat.completions.create(model="gpt-4o-mini", max_tokens=5000, temperature=0.3,
                                          messages=[{"role": "user", "content": prompt}])
    text = resp.choices[0].message.content.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"): p = p[4:].strip()
            if p.startswith("{"): text = p; break
    text = text.strip()
    if not text.endswith("}"):
        last = text.rfind("\n  }")
        if last > 0: text = text[:last+4] + "\n}"
    result = json.loads(text)

    # ── Post-process: fix wrong/missing tickers in watchlist_analysis ──
    wl_ticker_list = [s.get("ticker","").upper() for s in watchlist_data if s.get("ticker")]
    wla = result.get("watchlist_analysis", [])
    # Map returned items by ticker
    wla_map = {}
    for item in wla:
        t = (item.get("ticker") or "").upper()
        if t: wla_map[t] = item
    # Find unmatched watchlist tickers and orphan wla items
    unmatched_wl = [t for t in wl_ticker_list if t not in wla_map]
    orphan_items  = [item for item in wla if (item.get("ticker","").upper() not in wl_ticker_list)]
    # Positional fallback: assign orphans to unmatched tickers
    for i, wl_t in enumerate(unmatched_wl):
        if i < len(orphan_items):
            orphan_items[i]["ticker"] = wl_t
            wla_map[wl_t] = orphan_items[i]
    # Rebuild in watchlist order with correct tickers
    fixed_wla = []
    for t in wl_ticker_list:
        if t in wla_map:
            item = dict(wla_map[t]); item["ticker"] = t
            fixed_wla.append(item)
    result["watchlist_analysis"] = fixed_wla
    return result

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
    # CN_STATUS_FILE 只用于 running/error 状态追踪
    if CN_STATUS_FILE.exists():
        try:
            d = json.loads(CN_STATUS_FILE.read_text(encoding="utf-8"))
            if d.get("status") == "running":
                return jsonify({"status": "running"})
            if d.get("status") == "error":
                return jsonify(d)
            # status=done 时 CN_STATUS_FILE 已无用，走 archive
        except: pass
    return jsonify(load_latest())

@app.route("/api/run", methods=["POST"])
def run():
    global _running
    # 先写入 running 状态文件，再启动线程，避免竞态条件
    CN_STATUS_FILE.write_text(json.dumps({"status":"running","started_at":datetime.now().isoformat()},ensure_ascii=False), encoding="utf-8")
    _running = True
    def _go():
        global _running
        try:
            key = load_key()
            if not key: raise ValueError("未配置 API Key，请点设置填写")
            cfg = load_cfg(); port = load_port()
            wl = [fetch_stock(s["code"],s["name"]) for s in cfg["watchlist"]]
            mkt = fetch_market(); news = fetch_news()
            hot_data   = fetch_market_hot()
            candidates = fetch_candidate_pool()
            ai = run_ai(key, wl, mkt, news, port, hot_data, candidates)
            result = {"status":"done","updated_at":datetime.now().strftime("%Y-%m-%d %H:%M"),
                      "market":mkt,"watchlist":wl,"news":news,"ai":ai}
            save_cn_latest(result)
            # 分析完成后清除 CN_STATUS_FILE，让 get_analysis 走 archive
            if CN_STATUS_FILE.exists(): CN_STATUS_FILE.unlink()
        except Exception as e:
            err = {"status":"error","message":str(e)}
            CN_STATUS_FILE.write_text(json.dumps(err,ensure_ascii=False), encoding="utf-8")
        finally:
            _running = False
    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"status":"started"})

@app.route("/api/reset", methods=["POST"])
def reset():
    global _running
    _running = False
    if CN_STATUS_FILE.exists(): CN_STATUS_FILE.unlink()
    return jsonify({"ok": True})

@app.route("/api/diagnose", methods=["GET"])
def diagnose_get():
    """返回上次保存的诊股结果"""
    latest = load_latest()
    return jsonify({"results": latest.get("diagnose", {})})

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
        results = data.get("results", {})
        # 把诊股结果存入最新 archive 文件
        files = sorted(CN_ARCHIVE_DIR.glob("analysis_*.json"), reverse=True)
        for f in files:
            d = jload(f)
            if d and d.get("status") == "done":
                d["diagnose"] = results
                f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                break
        return jsonify({"results": results})
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
    sys_msg = {"role":"system","content":f"你是A股投资顾问。今天{today}。{ctx}\n用简洁专业中文回答，涉及操作建议须提示风险。"}
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
    try:
        resp = client.chat.completions.create(model="deepseek-chat", max_tokens=800, temperature=0.5,
                                              messages=[sys_msg]+msgs[-20:])
        return jsonify({"reply": resp.choices[0].message.content.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── 政策主线路由 ──
@app.route("/api/policy/analysis", methods=["GET"])
def policy_get_analysis():
    if POLICY_STATUS_FILE.exists():
        try:
            d = json.loads(POLICY_STATUS_FILE.read_text(encoding="utf-8"))
            if d.get("status") == "running": return jsonify({"status": "running"})
            if d.get("status") == "error":   return jsonify(d)
        except Exception: pass
    return jsonify(load_policy_latest())

@app.route("/api/policy/run", methods=["POST"])
def policy_run():
    global _policy_running
    POLICY_STATUS_FILE.write_text(
        json.dumps({"status": "running", "started_at": datetime.now().isoformat()}, ensure_ascii=False),
        encoding="utf-8"
    )
    _policy_running = True
    def _go():
        global _policy_running
        try:
            key = load_key()
            if not key: raise ValueError("未配置 DeepSeek API Key")
            mkt     = fetch_market()
            sectors = fetch_policy_sector_data()
            ai      = run_policy_ai(key, sectors, mkt)
            result  = {
                "status": "done",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "sectors": sectors, "market": mkt, "ai": ai
            }
            save_policy_latest(result)
            if POLICY_STATUS_FILE.exists(): POLICY_STATUS_FILE.unlink()
        except Exception as e:
            POLICY_STATUS_FILE.write_text(
                json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False),
                encoding="utf-8"
            )
        finally:
            _policy_running = False
    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/policy/reset", methods=["POST"])
def policy_reset():
    global _policy_running
    _policy_running = False
    if POLICY_STATUS_FILE.exists(): POLICY_STATUS_FILE.unlink()
    return jsonify({"ok": True})

# ── 美股路由 ──
@app.route("/api/us/config", methods=["GET"])
def us_get_config():
    c = load_us_cfg()
    return jsonify({"watchlist": c["watchlist"], "has_key": bool(load_key()), "has_openai_key": bool(load_openai_key())})

@app.route("/api/us/config", methods=["POST"])
def us_set_config():
    c = load_us_cfg(); d = request.json or {}
    if d.get("deepseek_api_key"): save_key(d["deepseek_api_key"])
    if d.get("openai_api_key"): save_openai_key(d["openai_api_key"])
    if "watchlist" in d: c["watchlist"] = d["watchlist"]
    save_us_cfg(c); return jsonify({"ok": True})

@app.route("/api/us/portfolio", methods=["GET"])
def us_get_port(): return jsonify(load_us_port())

@app.route("/api/us/portfolio", methods=["POST"])
def us_set_port(): save_us_port(request.json or {}); return jsonify({"ok": True})

@app.route("/api/us/analysis", methods=["GET"])
def us_get_analysis():
    if US_STATUS_FILE.exists():
        try:
            d = json.loads(US_STATUS_FILE.read_text(encoding="utf-8"))
            if d.get("status") == "running": return jsonify({"status": "running"})
            if d.get("status") == "error":   return jsonify(d)
        except Exception: pass
    return jsonify(load_us_latest())

@app.route("/api/us/run", methods=["POST"])
def us_run():
    global _us_running
    US_STATUS_FILE.write_text(json.dumps({"status": "running", "started_at": datetime.now().isoformat()}, ensure_ascii=False), encoding="utf-8")
    _us_running = True
    def _go():
        global _us_running
        try:
            key = load_openai_key()
            if not key: raise ValueError("未配置 OpenAI API Key，请在设置中填写")
            cfg  = load_us_cfg(); port = load_us_port()
            wl   = [fetch_us_stock(s["ticker"], s.get("name", "")) for s in cfg["watchlist"]]
            mkt  = fetch_us_market(); news = fetch_us_news(); hot = fetch_us_hot()
            us_candidates = fetch_us_candidate_pool()
            ai   = run_us_ai(key, wl, mkt, news, port, hot, us_candidates)
            result = {"status": "done", "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                      "market": mkt, "watchlist": wl, "news": news, "ai": ai}
            save_us_latest(result)
            if US_STATUS_FILE.exists(): US_STATUS_FILE.unlink()
        except Exception as e:
            US_STATUS_FILE.write_text(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False), encoding="utf-8")
        finally:
            _us_running = False
    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/us/reset", methods=["POST"])
def us_reset():
    global _us_running
    _us_running = False
    if US_STATUS_FILE.exists(): US_STATUS_FILE.unlink()
    return jsonify({"ok": True})

@app.route("/api/us/diagnose", methods=["GET"])
def us_diagnose_get():
    """返回上次保存的美股诊股结果"""
    latest = load_us_latest()
    return jsonify({"results": latest.get("diagnose", {})})

@app.route("/api/us/diagnose", methods=["POST"])
def us_diagnose():
    from openai import OpenAI
    key = load_openai_key()
    if not key: return jsonify({"error": "未配置 OpenAI API Key"}), 400
    body = request.json or {}
    portfolio = body.get("portfolio", {})
    if not portfolio: return jsonify({"error": "持仓为空"}), 400
    latest  = load_us_latest()
    wl_data = latest.get("watchlist", []) if latest.get("status") == "done" else []
    ai_ctx  = latest.get("ai", {})
    rows = []
    for ticker, pos in portfolio.items():
        live = next((s for s in wl_data if s["ticker"] == ticker), None)
        if not live:
            try: live = fetch_us_stock(ticker, pos.get("name", ticker))
            except Exception: live = None
        cur = live["close"] if live and live.get("close") else None
        avg = float(pos.get("avg_price") or 0)
        qty = int(pos.get("quantity") or 0)
        pnl = round((cur - avg) / avg * 100, 2) if cur and avg else None
        rows.append({
            "ticker": ticker, "name": pos.get("name", ticker),
            "qty": qty, "avg_price": avg, "current_price": cur, "pnl_pct": pnl,
            "ma50":  live.get("ma50")  if live else None,
            "ma200": live.get("ma200") if live else None,
            "vol_ratio":  live.get("vol_ratio")  if live else None,
            "change_pct": live.get("change_pct") if live else None,
            "sector": live.get("sector") if live else None,
        })
    vix_val = 0
    try:
        import yfinance as _yf
        _vdf = _yf.Ticker("^VIX").history(period="1d")
        if not _vdf.empty: vix_val = round(float(_vdf["Close"].iloc[-1]), 1)
    except Exception: pass
    vix_note = f"VIX={vix_val} — " + ("Extreme Fear: systemic risk, cut all positions now" if vix_val > 30 else "Fear: reduce high-beta 20-30%" if vix_val > 25 else "Neutral" if vix_val > 18 else "Greed: watch for complacency")

    sent    = ai_ctx.get("market_sentiment", "Neutral")
    mkt_sum = ai_ctx.get("market_summary", "")
    today   = datetime.now().strftime("%Y-%m-%d")
    hot     = fetch_us_hot()
    hot_lines = []
    if hot.get("fear_greed"):   hot_lines.append(f"Fear/Greed: {hot['fear_greed']}")
    if hot.get("sector_flows"): hot_lines.append("Sector ETFs today: " + " | ".join(hot["sector_flows"][:6]))
    hot_str = "\n".join(hot_lines)

    prompt = f"""You are a senior US equity portfolio advisor. Today is {today}.
{vix_note}
{hot_str}
Last AI market sentiment: {sent}. {mkt_sum}

User's holdings:
{json.dumps(rows, ensure_ascii=False, indent=2)}

Field guide:
- pnl_pct: unrealized P&L % (negative = loss)
- ma50/ma200: trend direction reference only, NOT buy/sell signals
- vol_ratio: today's volume / 3-month average (>1.5 = active)
- sector: stock sector

## PORTFOLIO DIAGNOSIS FRAMEWORK
All output text fields must be written in Chinese (中文).

**Step 1 — Classify stock type first:**
- Growth (NVDA/MSFT): earnings beats + multiple expansion. Earnings = biggest risk.
- Momentum: relative strength + price/volume breakout. Track sector ETF.
- Value/Dividend: cash flow + yield sustainability.
- Thematic (AI/GLP-1 etc.): sector narrative + ETF inflow + catalyst.

**Step 2 — Earnings calendar (MOST IMPORTANT):**
- Earnings within 2 weeks AND gain > 20% → trim 30-50% BEFORE the event, lock profits.
- Earnings within 2 weeks AND flat/loss → evaluate cutting before the binary event.
- Recent earnings BEAT (stock held gains) → confirm hold, add on pullbacks.
- Recent earnings MISS → reduce immediately. NEVER average down on earnings misses.

**Step 3 — Sector ETF health:**
Using sector_flows data above, determine if this stock's ETF is seeing inflow or outflow:
- ETF inflow + stock outperforming ETF → hold or add.
- ETF inflow + stock underperforming ETF → rotation risk, consider trimming.
- ETF outflow → sector headwind. Reduce unless strong independent catalyst exists.

**Step 4 — VIX position sizing:**
- VIX < 18: normal sizing.
- VIX 18-25: reduce high-beta/growth 20-30%.
- VIX 25-30: cut all positions in half.
- VIX > 30: minimum positions only, capital preservation mode.

**Step 5 — P&L discipline:**
- Gain > 25%: MUST give specific trim plan (e.g., sell 30%, let rest run with trailing stop).
- Gain > 50%: strongly recommend selling half, lock in realized gains.
- Loss > 10%: explicitly assess if thesis is still intact. If broken, cut.
- Loss > 15%: stop-loss unless imminent catalyst justifies holding.
- NEVER write "hold and wait for recovery" without specifying exact conditions for recovery confirmation.

**Stop-loss priority order:**
1. Earnings miss (EPS below estimates + guidance cut) → exit within 1-2 days, no exceptions.
2. Sector ETF heavy outflow 3+ consecutive days → reduce 50%.
3. VIX > 30 → cut all risk assets to minimum.
4. MA50 breakdown + sector underperforming (last resort, never sole trigger).

Return JSON only, no markdown:
{{
  "results": {{
    "TICKER": {{
      "stock_type": "成长股|动能股|价值股|主题股",
      "suggestion": "持有|加仓|减仓|止损|止盈|观察",
      "earnings_action": "只能写：无近期财报风险 / 财报已过结果超预期持有/不及预期减仓 / 财报约在[月]（无把握一律写无近期财报风险，禁止多只股写相同月份）",
      "sector_health": "所在ETF（如XLK）今日涨跌X%，资金流入/流出，对持仓的具体影响",
      "analysis": "70字中文：股票类型+财报状态+ETF健康度+盈亏状态综合判断",
      "action": "具体操作（中文，针对该股个性化）：如财报前减仓30%至XX股；或ETF企稳后可加仓XX股",
      "exit_signal": "止损条件（中文，必须针对该股个性化，每只股不同，禁止复制粘贴）：结合该股财报日期/所属ETF名称/当前浮盈浮亏具体说明"
    }}
  }}
}}
Diagnose every holding. Return JSON only."""
    client = OpenAI(api_key=key)  # OpenAI GPT-4o-mini
    try:
        resp = client.chat.completions.create(model="gpt-4o-mini", max_tokens=1500, temperature=0.3,
                                              messages=[{"role": "user", "content": prompt}])
        text = resp.choices[0].message.content.strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                p = part.strip()
                if p.startswith("json"): p = p[4:].strip()
                if p.startswith("{"): text = p; break
        data = json.loads(text.strip())
        results = data.get("results", {})
        # 把诊股结果存入最新 archive 文件
        files = sorted(US_ARCHIVE_DIR.glob("analysis_*.json"), reverse=True)
        for f in files:
            d = jload(f)
            if d and d.get("status") == "done":
                d["diagnose"] = results
                f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                break
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/us/chat", methods=["POST"])
def us_chat():
    from openai import OpenAI
    key = load_openai_key()
    if not key: return jsonify({"error": "未配置 OpenAI API Key"}), 400
    body = request.json or {}
    msgs = body.get("messages", [])
    if not msgs: return jsonify({"error": "消息为空"}), 400
    latest = load_us_latest()
    today = datetime.now().strftime("%Y年%m月%d日")

    ctx_parts = [f"今天是{today}。"]

    # 实时抓 VIX + 板块ETF（聊天时也需要最新数据）
    try:
        hot = fetch_us_hot()
        if hot.get("fear_greed"):
            ctx_parts.append(f"当前{hot['fear_greed']}。")
        if hot.get("sector_flows"):
            etf_str = " | ".join(hot["sector_flows"][:6])
            ctx_parts.append(f"板块ETF今日：{etf_str}。")
    except Exception:
        pass

    if latest.get("status") == "done":
        ai = latest.get("ai", {})
        sent = ai.get("market_sentiment", "")
        mkt_sum = ai.get("market_summary", "")
        if sent:    ctx_parts.append(f"最近一次分析市场情绪：{sent}。")
        if mkt_sum: ctx_parts.append(f"市场摘要：{mkt_sum}")
        # 自选股今日涨跌
        wl = latest.get("watchlist", [])
        if wl:
            wl_brief = []
            for s in wl[:8]:
                chg = s.get("change_pct", 0) or 0
                wl_brief.append(f"{s['ticker']}({'+'if chg>=0 else ''}{chg}%)")
            ctx_parts.append(f"自选股今日：{' | '.join(wl_brief)}。")
        # 持仓
        port = load_us_port()
        if port:
            port_items = []
            for ticker, pos in list(port.items())[:6]:
                live = next((s for s in wl if s.get("ticker")==ticker), None)
                cur = live.get("close") if live else None
                avg = float(pos.get("avg_price") or 0)
                pnl = round((cur-avg)/avg*100, 1) if cur and avg else None
                pnl_str = f"{'+'if (pnl or 0)>=0 else ''}{pnl}%" if pnl is not None else ""
                port_items.append(f"{ticker}{pnl_str}")
            ctx_parts.append(f"用户持仓：{', '.join(port_items)}。")
        # 风险提示
        rw = ai.get("risk_warning", "")
        if rw: ctx_parts.append(f"风险提示：{rw}")

    ctx = " ".join(ctx_parts)
    sys_msg = {"role": "system", "content": (
        f"你是专业美股投资顾问，精通财报周期、板块ETF轮动、VIX仓位管理。\n"
        f"当前市场数据：{ctx}\n\n"
        "回答规范：\n"
        "1. 简洁专业，优先引用上方实时数据（VIX/ETF涨跌）支撑观点\n"
        "2. 涉及买卖操作必须给出具体止损条件\n"
        "3. 不给绝对化建议，始终提示风险\n"
        "4. 中文为主，专业术语可保留英文（如VIX、ETF代码）"
    )}
    client = OpenAI(api_key=key)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=800, temperature=0.4,
            messages=[sys_msg] + msgs[-20:])
        return jsonify({"reply": resp.choices[0].message.content.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>StockLens · 智能股票分析</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0e1a;--sf:#111827;--bd:#1e2d45;--ac:#00d4aa;--ac2:#3b82f6;--up:#00c97a;--dn:#ff4d6a;--tx:#e2e8f0;--mu:#64748b;--gd:#f5c842;--pu:#a78bfa}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Noto Sans SC',sans-serif;font-size:14px;min-height:100vh}
.market-tabs{display:flex;gap:2px;background:rgba(255,255,255,.06);border-radius:8px;padding:3px}
.mktab{padding:5px 18px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:600;font-family:'Noto Sans SC',sans-serif;background:transparent;color:var(--mu);transition:all .2s}
.mktab.active{background:var(--ac);color:#0a0e1a}
.mktab.us-tab.active{background:#3b82f6;color:#fff}
.a-only,.us-only{display:none}
.a-only.show,.us-only.show{display:contents}
.policy-panel{background:var(--sf);border:1px solid var(--bd);border-radius:10px;margin-top:16px;overflow:hidden}
.policy-hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;cursor:pointer;border-bottom:1px solid var(--bd)}
.policy-hdr-l{display:flex;align-items:center;gap:10px}
.policy-badge{background:linear-gradient(135deg,rgba(167,139,250,.2),rgba(245,200,66,.15));border:1px solid rgba(167,139,250,.4);border-radius:6px;padding:3px 10px;font-size:12px;font-weight:700;color:var(--pu)}
.policy-body{padding:14px 16px;display:none}
.policy-body.open{display:block}
.stage-tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.stage-brew{background:rgba(100,116,139,.2);color:var(--mu)}
.stage-start{background:rgba(0,212,170,.15);color:var(--ac)}
.stage-accel{background:rgba(245,200,66,.2);color:var(--gd)}
.stage-adj{background:rgba(59,130,246,.15);color:#3b82f6}
.stage-fade{background:rgba(255,77,106,.15);color:var(--dn)}
.sector-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.sector-card{background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:12px}
.sector-card:hover{border-color:var(--pu)}
.sector-nm{font-size:13px;font-weight:700;color:var(--tx);margin-bottom:6px}
.sector-flow{font-size:12px;margin-bottom:4px}
.sector-action{font-size:11px;color:var(--mu);margin-top:6px;font-style:italic}
.policy-entry{font-size:12px;color:var(--ac);margin-bottom:4px;line-height:1.6}
.policy-hold{font-size:12px;color:var(--mu);margin-bottom:4px;line-height:1.6}
.policy-stop{font-size:11px;color:var(--dn);margin-bottom:4px}
.policy-target{font-size:12px;color:var(--gd);font-weight:600}
@media(max-width:900px){.sector-grid{grid-template-columns:repeat(2,1fr)}}
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
.spark-cv{width:100%;height:92px;display:block}
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
  <div class="logo">Stock<span>Lens</span></div>
  <div class="market-tabs">
    <button class="mktab active" id="tabA" onclick="switchMarket('a')">🇨🇳 A股</button>
    <button class="mktab us-tab" id="tabUS" onclick="switchMarket('us')">🇺🇸 美股</button>
  </div>
  <div class="hdr-r">
    <span class="utime" id="utime" style="display:none"></span>
    <button class="btn btn-g btn-sm" onclick="openSettings()">⚙ 设置</button>
    <button class="btn btn-g btn-sm" id="resetBtn2" onclick="doReset()" title="卡住时点此重置">↺ 重置</button>
    <button class="btn btn-p" id="reAnalyzeBtn" onclick="runAnalysis()">▶ 开始分析</button>
  </div>
</header>
<div id="marketA" class="layout">
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

  <!-- 政策主线面板（夹在推荐和新闻之间） -->
  <div class="policy-panel">
    <div class="policy-hdr" onclick="togglePolicy()">
      <div class="policy-hdr-l">
        <span class="policy-badge">🏛 政策主线</span>
        <span style="font-size:13px;color:var(--tx);font-weight:600">中长线战略配置</span>
        <span style="font-size:11px;color:var(--mu)">AI算力 · 半导体 · 机器人 · 低空 · 新能源 · 军工 · 创新药</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span id="policyTime" style="font-size:11px;color:var(--mu)"></span>
        <button class="btn btn-sm" id="policyRunBtn"
          style="background:rgba(167,139,250,.15);border:1px solid rgba(167,139,250,.4);color:var(--pu);font-size:12px"
          onclick="event.stopPropagation();runPolicyAnalysis()">▶ 分析主线</button>
        <span id="policyArrow" style="color:var(--mu);font-size:14px">▼</span>
      </div>
    </div>
    <div class="policy-body" id="policyBody">
      <div id="policyContent">
        <div style="color:var(--mu);font-size:13px;padding:20px 0;text-align:center">
          点击「分析主线」获取六大政策板块的中长线分析<br>
          <span style="font-size:11px;opacity:.7">分析耗时约60-90秒（需抓取个股yfinance数据）</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 今日财经要闻（政策面板下方） -->
  <div id="newsEl"></div>
</main>
</div><!-- end #marketA -->

<!-- 美股市场 -->
<div id="marketUS" class="layout" style="display:none">
<aside>
  <div class="stitle">自选股 (美股)</div>
  <div id="usWlEl"></div>
  <div class="add-area">
    <div class="add-row">
      <input id="usAddTicker" class="si" placeholder="Ticker (如AAPL)" style="width:100px;flex:none;font-family:'JetBrains Mono',monospace">
      <input id="usAddName" class="si" placeholder="名称(可选)" style="flex:1;font-family:'Noto Sans SC',sans-serif">
    </div>
    <button class="btn btn-p btn-sm" style="width:100%;margin-top:2px;font-weight:700;background:#3b82f6;border-color:#3b82f6" onclick="usAddStock()">＋ 添加自选</button>
  </div>
  <div class="stitle" style="margin-top:14px">热门板块ETF</div>
  <div id="usHotSectors" style="color:var(--mu);font-size:12px">-- 待分析 --</div>
  <div class="stitle" style="margin-top:18px">美股网站</div>
  <div class="ext-links">
    <a class="ext-link" href="https://finance.yahoo.com" target="_blank">
      <span class="ext-link-icon">📈</span>
      <div class="ext-link-info"><span class="ext-link-name">Yahoo Finance</span><span class="ext-link-desc">行情 / 财报 / 新闻</span></div>
    </a>
    <a class="ext-link" href="https://finviz.com" target="_blank">
      <span class="ext-link-icon">🔭</span>
      <div class="ext-link-info"><span class="ext-link-name">Finviz</span><span class="ext-link-desc">选股筛选 / 热力图 / 内幕</span></div>
    </a>
    <a class="ext-link" href="https://unusualwhales.com" target="_blank">
      <span class="ext-link-icon">🐋</span>
      <div class="ext-link-info"><span class="ext-link-name">Unusual Whales</span><span class="ext-link-desc">期权流 / 机构大单</span></div>
    </a>
    <a class="ext-link" href="https://www.wsj.com/market-data" target="_blank">
      <span class="ext-link-icon">📰</span>
      <div class="ext-link-info"><span class="ext-link-name">WSJ Markets</span><span class="ext-link-desc">市场数据 / 深度分析</span></div>
    </a>
    <a class="ext-link" href="https://www.marketwatch.com" target="_blank">
      <span class="ext-link-icon">⚡</span>
      <div class="ext-link-info"><span class="ext-link-name">MarketWatch</span><span class="ext-link-desc">实时行情 / 快讯</span></div>
    </a>
    <a class="ext-link" href="https://seekingalpha.com" target="_blank">
      <span class="ext-link-icon">🔍</span>
      <div class="ext-link-info"><span class="ext-link-name">Seeking Alpha</span><span class="ext-link-desc">深度研究 / 分析师观点</span></div>
    </a>
    <a class="ext-link" href="https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html" target="_blank">
      <span class="ext-link-icon">🏦</span>
      <div class="ext-link-info"><span class="ext-link-name">FedWatch</span><span class="ext-link-desc">美联储利率预期</span></div>
    </a>
    <a class="ext-link" href="https://www.earningswhispers.com" target="_blank">
      <span class="ext-link-icon">📅</span>
      <div class="ext-link-info"><span class="ext-link-name">Earnings Whispers</span><span class="ext-link-desc">财报日历 / 预期</span></div>
    </a>
  </div>
</aside>
<main>
  <div id="usDataBanner" style="display:none;border-radius:8px;padding:9px 14px;margin-bottom:12px;align-items:center;gap:8px;background:rgba(59,130,246,.07);border:1px solid rgba(59,130,246,.2)">
    <span id="usDataBannerIcon" style="font-size:14px">📊</span>
    <span id="usDataBannerText" style="font-size:13px;color:var(--tx)">暂无数据</span>
  </div>
  <div id="usIdxBar" class="idx-bar" style="display:none"></div>
  <div id="usMc">
    <div class="empty-state">
      <div class="empty-icon">🇺🇸</div>
      <div style="font-size:16px;color:var(--tx);margin-bottom:8px">美股智能分析</div>
      <div>切换到美股市场后点击「开始分析」</div>
    </div>
  </div>
  <!-- 美股今日要闻（独立区块，排在推荐后面） -->
  <div id="usNewsEl"></div>
</main>
</div><!-- end #marketUS -->

<div class="modal-ov" id="settingsModal">
  <div class="modal">
    <h3>⚙ 设置</h3>
    <div class="fl">🇨🇳 DeepSeek API Key <span style="font-size:11px;color:var(--mu);font-weight:400">（A股分析）</span></div>
    <input class="fi" id="apiKey" type="password" placeholder="sk-xxxx">
    <div class="fhint">在 <a href="https://platform.deepseek.com" target="_blank" style="color:var(--ac)">platform.deepseek.com</a> 注册获取</div>
    <div class="fl" style="margin-top:14px">🇺🇸 OpenAI API Key <span style="font-size:11px;color:var(--mu);font-weight:400">（美股分析，GPT-4o-mini）</span></div>
    <input class="fi" id="openaiKey" type="password" placeholder="sk-xxxx">
    <div class="fhint">在 <a href="https://platform.openai.com" target="_blank" style="color:#3b82f6">platform.openai.com</a> 注册获取</div>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">
      <button class="btn btn-g" onclick="closeModal('settingsModal')">取消</button>
      <button class="btn btn-p" onclick="saveSettings()">保存</button>
    </div>
  </div>
</div>

<div class="modal-ov" id="portModal">
  <div class="modal">
    <h3 id="portModalTitle">+ 添加持仓</h3>
    <div class="fl" id="portCodeLabel">股票代码</div>
    <input class="fi" id="pCode" placeholder="如 002236">
    <div class="fl">股票名称</div>
    <input class="fi" id="pName" placeholder="如 大华技术" style="font-family:'Noto Sans SC',sans-serif">
    <div class="fl">持股数量</div><input class="fi" id="pQty" type="number" placeholder="如 1000">
    <div class="fl" id="portPriceLabel">买入均价</div><input class="fi" id="pAvg" type="number" step="0.01" placeholder="如 18.50">
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn btn-g" onclick="closeModal('portModal')">取消</button>
      <button class="btn btn-p" id="portSaveBtn" onclick="savePortEntry()">添加</button>
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
var S  = {wl:[], port:{}, analysis:null, polling:null, sparks:{}, diagnose:{}};
var US = {wl:[], port:{}, analysis:null, polling:null, sparks:{}, diagnose:{}, policy:null, market:'us'};
var currentMarket = 'a';

function switchMarket(m) {
  currentMarket = m;
  document.getElementById('marketA').style.display  = m === 'a'  ? '' : 'none';
  document.getElementById('marketUS').style.display = m === 'us' ? '' : 'none';
  document.getElementById('tabA').classList.toggle('active', m === 'a');
  document.getElementById('tabUS').classList.toggle('active', m === 'us');
  var btn = document.getElementById('reAnalyzeBtn');
  if(m === 'a') {
    btn.textContent = '▶ 开始分析';
    btn.onclick = runAnalysis;
    btn.style.background = '';
  } else {
    btn.textContent = '▶ 分析美股';
    btn.onclick = usRunAnalysis;
    btn.style.background = '#3b82f6';
  }
  document.getElementById('utime').style.display = 'none';
  // 切换市场时重置聊天上下文，并更新UI
  chatMsgs = [];
  var ml = document.getElementById('chatMsgs');
  if(ml) {
    var greeting = m === 'a'
      ? '已切换到 <b>A股模式</b>（DeepSeek）。可问我选股、持仓诊断、政策解读等。'
      : 'Switched to <b>US Market mode</b> (GPT-4o-mini). Ask me about US stocks, earnings, ETF flows, etc.';
    ml.innerHTML = '<div class="msg msg-a">'+greeting+'<div class="msg-t"></div></div>';
  }
  updateChatUI();
}

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
    if(data && data.status === 'done') {
      renderAnalysis(data);
      if(data.diagnose && Object.keys(data.diagnose).length > 0) {
        S.diagnose = data.diagnose;
        renderPort();
      }
    }
    else if(data && data.status === 'running') { startPolling(); setBannerState('⏳','<b>上次分析仍在进行中…</b>','分析中…',true,'rgba(245,200,66,.07)','rgba(245,200,66,.25)'); }
    else { setBannerState('📭','<span style="color:var(--mu)">暂无分析数据，点右侧按钮开始</span>','↻ 开始分析',false); }
    // 后台预加载政策主线历史数据
    api('/api/policy/analysis').then(function(d) {
      if (d && d.status === 'done') { S.policy = d; renderPolicyAnalysis(d); }
    }).catch(function(){});
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
  document.getElementById('portModalTitle').textContent = '+ 添加持仓（A股）';
  document.getElementById('portCodeLabel').textContent = '股票代码（6位数字）';
  document.getElementById('portPriceLabel').textContent = '买入均价（元）';
  document.getElementById('pCode').placeholder = '如 002236';
  document.getElementById('pName').placeholder = '如 大华技术';
  document.getElementById('portSaveBtn').onclick = savePortEntry;
  document.getElementById('portModal').classList.add('show');
}
async function savePortEntry() {
  var code = document.getElementById('pCode').value.trim();
  var name = document.getElementById('pName').value.trim();
  var qty  = parseFloat(document.getElementById('pQty').value)||0;
  var avg  = parseFloat(document.getElementById('pAvg').value)||0;
  if(!code){alert('请输入代码/Ticker');return;}
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
    setBannerState('✅', '<b style="color:var(--up)">行情更新</b>　<span style="font-size:11px;color:var(--mu)">自选分析</span>　<span style="font-family:monospace;color:var(--ac)">'+ts+'</span>',
      '↻ 重新分析', false, 'rgba(0,201,122,.07)', 'rgba(0,201,122,.25)');
  } else {
    setBannerState('🕐', '<b style="color:var(--mu)">历史数据</b>　<span style="font-size:11px;color:var(--mu)">自选分析</span>　<span style="font-family:monospace;color:var(--ac)">'+ts+'</span>',
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

  // 新闻 — 单独渲染到 #newsEl（政策面板下方）
  var newsH = '<div class="card"><div class="sec-lbl" style="margin-bottom:10px">今日财经要闻</div>';
  (data.news||[]).forEach(function(n) {
    var title = typeof n==='string'?n:(n.title||'');
    var url   = typeof n==='string'?'':(n.url||'');
    newsH += '<div class="news-item"><span class="ndot">▸</span>'
       +(url?'<a href="'+url+'" target="_blank">'+title+'</a>':'<span style="color:var(--mu)">'+title+'</span>')
       +'</div>';
  });
  newsH += '</div><div class="disc">⚠ 本报告由AI自动生成，仅供参考，不构成投资建议。</div>';

  document.getElementById('mc').innerHTML = h;
  document.getElementById('newsEl').innerHTML = newsH;
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
  var W = cv.parentElement.offsetWidth||280, H=92, pad=2, labelH=14;
  cv.width=W; cv.height=H; ctx.clearRect(0,0,W,H);
  var mn=Math.min.apply(null,data), mx=Math.max.apply(null,data), rng=mx-mn||1;
  // Reserve top labelH px for max label, bottom labelH px for min label
  var chartTop=labelH, chartBot=H-labelH, chartH=chartBot-chartTop;
  var pts=data.map(function(v,i){return{x:pad+(i/(data.length-1))*(W-pad*2),y:chartTop+(1-(v-mn)/rng)*chartH};});
  var col=isUp?'0,201,122':'255,77,106';
  var g=ctx.createLinearGradient(0,chartTop,0,chartBot);
  g.addColorStop(0,'rgba('+col+',.3)'); g.addColorStop(1,'rgba('+col+',0)');
  ctx.beginPath(); ctx.moveTo(pts[0].x,pts[0].y);
  pts.slice(1).forEach(function(p){ctx.lineTo(p.x,p.y);});
  ctx.lineTo(pts[pts.length-1].x,chartBot); ctx.lineTo(pts[0].x,chartBot); ctx.closePath();
  ctx.fillStyle=g; ctx.fill();
  ctx.beginPath(); ctx.moveTo(pts[0].x,pts[0].y);
  pts.slice(1).forEach(function(p){ctx.lineTo(p.x,p.y);});
  ctx.strokeStyle=isUp?'#00c97a':'#ff4d6a'; ctx.lineWidth=1.5; ctx.lineJoin='round'; ctx.stroke();
  // Draw min/max labels
  var fmt=function(v){return v>=1000?v.toFixed(0):v>=100?v.toFixed(1):v.toFixed(2);};
  ctx.font='10px JetBrains Mono,monospace';
  ctx.fillStyle='rgba('+col+',.85)';
  // Max label at top-right
  var maxLabel=fmt(mx);
  ctx.textAlign='right'; ctx.fillText(maxLabel, W-pad, labelH-2);
  // Min label at bottom-right
  var minLabel=fmt(mn);
  ctx.fillText(minLabel, W-pad, H-2);
  // Subtle dashed lines for max and min
  ctx.setLineDash([2,3]); ctx.lineWidth=0.5; ctx.strokeStyle='rgba('+col+',.2)';
  ctx.beginPath(); ctx.moveTo(0,chartTop); ctx.lineTo(W,chartTop); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0,chartBot); ctx.lineTo(W,chartBot); ctx.stroke();
  ctx.setLineDash([]);
}

// ── 关注 ──
async function followStock(code,name,btn){
  if(S.wl.some(function(s){return s.code===code;})){btn.textContent='✓ 已关注';btn.classList.add('followed');return;}
  S.wl.push({code:code,name:name}); await saveWL();
  btn.textContent='✓ 已关注'; btn.classList.add('followed'); renderWL();
}

// ── 设置 ──
async function openSettings(){
  var cfg = await api('/api/config');
  var usCfg = await api('/api/us/config');
  document.getElementById('apiKey').value = cfg.has_key ? '••••••••' : '';
  document.getElementById('openaiKey').value = usCfg.has_openai_key ? '••••••••' : '';
  document.getElementById('settingsModal').classList.add('show');
}
async function saveSettings(){
  var k  = document.getElementById('apiKey').value.trim();
  var ok = document.getElementById('openaiKey').value.trim();
  var body = {};
  if(k  && k  !== '••••••••') body.deepseek_api_key = k;
  if(ok && ok !== '••••••••') body.openai_api_key = ok;
  if(Object.keys(body).length) {
    await api('/api/config',    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    await api('/api/us/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  }
  closeModal('settingsModal');
}
function closeModal(id){document.getElementById(id).classList.remove('show');}
document.querySelectorAll('.modal-ov').forEach(function(el){el.addEventListener('click',function(e){if(e.target===el)closeModal(el.id);});});

// ── 聊天 ──
var chatOpen=false, chatMsgs=[];
function round2(v){ return Math.round(v*100)/100; }
var CHAT_CFG = {
  a:  { api: '/api/chat',    title: 'A股 AI 顾问', color: 'var(--ac)',  fab: 'var(--ac)',
        qs: ['今天大盘怎么样？','我的持仓建议？','政策利好哪些股？','现在适合加仓吗？'] },
  us: { api: '/api/us/chat', title: '美股 AI 顾问', color: '#3b82f6', fab: '#3b82f6',
        qs: ['US market outlook today?','Any earnings this week?','VIX太高该怎么仓位管理？','推荐当前动能股？'] }
};
function updateChatUI() {
  var cfg = CHAT_CFG[currentMarket];
  // 标题
  var titleEl = document.querySelector('.chat-hd-t');
  if(titleEl) titleEl.innerHTML = '<span class="chat-dot" style="background:'+cfg.color+'"></span>' + cfg.title;
  // fab 颜色
  var fab = document.querySelector('.chat-fab');
  if(fab) fab.style.background = cfg.fab;
  // 快捷问题
  var qs = document.querySelector('.chat-qs');
  if(qs) qs.innerHTML = cfg.qs.map(function(q){
    return '<button class="chat-q" onclick="quickAsk(this)">'+q+'</button>';
  }).join('');
  // placeholder
  var inp = document.getElementById('chatInp');
  if(inp) inp.placeholder = currentMarket==='a' ? '问任何A股问题…' : 'Ask any US stock question…';
}
function toggleChat(){
  chatOpen=!chatOpen;
  document.getElementById('chatPanel').classList.toggle('open',chatOpen);
  if(chatOpen){ updateChatUI(); document.getElementById('chatInp').focus(); }
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
  var endpoint = CHAT_CFG[currentMarket].api;
  try{
    var resp=await api(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:chatMsgs})});
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

// ── 美股 JS ──
async function usInit() {
  try {
    var cfg = await api('/api/us/config');
    US.wl = Array.isArray(cfg.watchlist) ? cfg.watchlist : [];
    var port = await api('/api/us/portfolio');
    US.port = (port && typeof port === 'object') ? port : {};
    usRenderWL();
    usRenderPort();
    if(!cfg.has_openai_key) {
      usSetBanner('⚠️', '<b style="color:var(--dn)">请在「设置」中填写 OpenAI API Key（美股分析用 GPT-4o-mini）</b>');
      return;
    }
    var data = await api('/api/us/analysis');
    if(data && data.status === 'done') {
      usRenderAnalysis(data);
      var usts = data.updated_at||'';
      usSetBanner('🕐', '<b style="color:var(--mu)">历史数据</b>　<span style="font-size:11px;color:var(--mu)">自选分析</span>　<span style="font-family:monospace;color:var(--ac)">'+usts+'</span>');
      if(data.diagnose && Object.keys(data.diagnose).length > 0) {
        US.diagnose = data.diagnose;
        usRenderPort();
      }
    } else if(data && data.status === 'running') {
      usStartPolling(); usSetBanner('⏳','<b>分析进行中…</b>');
    } else {
      usSetBanner('📭', '暂无美股数据，点右侧「分析美股」开始');
    }
  } catch(e) { console.warn('US init error', e); }
}

function usSetBanner(icon, html) {
  var b = document.getElementById('usDataBanner');
  b.style.display = 'flex';
  document.getElementById('usDataBannerIcon').textContent = icon;
  document.getElementById('usDataBannerText').innerHTML = html;
}

async function usRunAnalysis() {
  if(US.polling) clearInterval(US.polling);
  usSetBanner('⏳', '<b>正在抓取美股行情 + AI分析…</b>');
  var btn = document.getElementById('reAnalyzeBtn');
  btn.disabled = true; btn.textContent = '分析中…';
  try {
    await api('/api/us/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
    usStartPolling();
  } catch(e) {
    usSetBanner('❌', '启动失败：' + e.message);
    btn.disabled = false; btn.textContent = '▶ 分析美股';
  }
}

function usStartPolling() {
  var attempts = 0;
  US.polling = setInterval(async function() {
    attempts++;
    try {
      var data = await api('/api/us/analysis');
      if(data.status === 'done') {
        clearInterval(US.polling);
        usRenderAnalysis(data);
        var btn = document.getElementById('reAnalyzeBtn');
        btn.disabled = false; btn.textContent = '▶ 分析美股';
        var usts2 = data.updated_at||'';
        usSetBanner('✅', '<b style="color:var(--up)">行情更新</b>　<span style="font-size:11px;color:var(--mu)">自选分析</span>　<span style="font-family:monospace;color:var(--ac)">'+usts2+'</span>');
        setTimeout(usRunDiagnose, 500);
      } else if(data.status === 'error') {
        clearInterval(US.polling);
        usSetBanner('❌', '分析出错：' + (data.message||''));
        var btn = document.getElementById('reAnalyzeBtn');
        btn.disabled = false; btn.textContent = '▶ 分析美股';
      }
    } catch(e) {}
    if(attempts > 60) { clearInterval(US.polling); usSetBanner('❌', '分析超时，请重置后重试'); }
  }, 3000);
}

function usRenderWL() {
  var el = document.getElementById('usWlEl'); if(!el) return;
  if(!US.wl.length) { el.innerHTML='<div style="color:var(--mu);font-size:12px;padding:8px 0">暂无自选股</div>'; return; }
  el.innerHTML = US.wl.map(function(s, i) {
    var d = US.analysis && US.analysis.watchlist && US.analysis.watchlist.find(function(w){return w.ticker===s.ticker;});
    var chg = d ? d.change_pct : null;
    var cls = chg > 0 ? 'up' : chg < 0 ? 'dn' : '';
    var sign = chg > 0 ? '+' : '';
    return '<div class="wl-item">'
      +'<div><div class="wl-code">'+s.ticker+'</div><div class="wl-name">'+s.name+'</div></div>'
      +'<div class="wl-r">'
      +(d&&d.close ? '<div class="wl-price '+cls+'">$'+d.close+'</div><div class="wl-chg '+cls+'">'+sign+chg+'%</div>' : '<div class="wl-price" style="color:var(--mu)">--</div>')
      +'<div style="margin-top:3px"><span style="color:var(--mu);font-size:10px;cursor:pointer" onclick="event.stopPropagation();usRemoveStock('+i+')">✕</span></div>'
      +'</div></div>';
  }).join('');
}

async function usAddStock() {
  var ticker = (document.getElementById('usAddTicker').value||'').trim().toUpperCase();
  var name   = (document.getElementById('usAddName').value||'').trim();
  if(!ticker) return;
  US.wl.push({ticker: ticker, name: name||ticker});
  await api('/api/us/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({watchlist: US.wl})});
  document.getElementById('usAddTicker').value=''; document.getElementById('usAddName').value='';
  usRenderWL();
}

async function usRemoveStock(i) {
  US.wl.splice(i, 1);
  await api('/api/us/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({watchlist: US.wl})});
  usRenderWL();
}

function usRenderAnalysis(data) {
  US.analysis = data;
  var ai = data.ai || {};
  var h = '';
  // DEBUG — open browser console to inspect
  console.log('[US] ai keys:', Object.keys(ai));
  console.log('[US] watchlist_analysis count:', (ai.watchlist_analysis||[]).length);
  if((ai.watchlist_analysis||[]).length) console.log('[US] first wla item:', JSON.stringify(ai.watchlist_analysis[0]));
  console.log('[US] watchlist count:', (data.watchlist||[]).length);
  if((data.watchlist||[]).length) console.log('[US] first stock ticker:', data.watchlist[0].ticker);
  console.log('[US] news count:', (data.news||[]).length);
  if((data.news||[]).length) console.log('[US] first news:', JSON.stringify(data.news[0]));

  // 1. 大盘指数 bar
  var mkt = data.market || {};
  var idxBar = document.getElementById('usIdxBar');
  if(Object.keys(mkt).length) {
    idxBar.style.display = 'flex';
    idxBar.innerHTML = Object.entries(mkt).map(function(kv) {
      var n=kv[0], v=kv[1];
      var isUp=(v.change_pct||0)>=0;
      var cls = n==='VIX' ? (v.close>20?'dn':'up') : (isUp?'up':'dn');
      return '<div class="idx-item"><div class="idx-n">'+n+'</div>'
        +'<div class="idx-v '+cls+'">'+(v.close||'--')+'</div>'
        +'<div class="idx-c '+cls+'">'+(isUp&&n!=='VIX'?'+':'')+(v.change_pct||0)+'%</div></div>';
    }).join('');
  }

  // 2. AI市场摘要（对应A股的AI·DeepSeek card）
  var sent = ai.market_sentiment || '';
  var sentColor = sent==='Risk-On'?'var(--up)':sent==='Risk-Off'?'var(--dn)':'var(--gd)';
  var sentCls   = sent==='Risk-On'?'s-bull':sent==='Risk-Off'?'s-bear':'s-neut';
  h += '<div class="card"><div class="ai-hdr">'
    +'<span class="ai-badge">AI</span>'
    +(sent?'<span class="sent-tag '+sentCls+'">'+sent+'</span>':'')
    +'</div>'
    +'<div style="line-height:1.8">'+(ai.market_summary||'--')+'</div>'
    +(ai.risk_warning?'<div class="risk-box">⚠ '+ai.risk_warning+'</div>':'')
    +'</div>';

  // 3. 持仓（对应A股位置）
  h += '<div class="sec-lbl">我的持仓 '
    +'<button class="btn btn-g btn-sm" style="font-size:11px" onclick="usOpenAddPort()">+ 添加</button> '
    +'<button class="btn-diagnose" id="usDiagnoseBtn" onclick="usRunDiagnose()">🔬 AI诊股</button></div>'
    +'<div class="grid4" id="usPortCards"></div>';

  // 4. 自选股分析
  var wla = ai.watchlist_analysis || [];
  var wlStocks = data.watchlist || [];
  if(wlStocks.length) {
    h += '<div class="sec-lbl" style="margin-top:4px">自选股分析</div><div class="grid3">';
    wlStocks.forEach(function(s) {
      var ana = wla.find(function(a){ return (a.ticker||'').toUpperCase()===(s.ticker||'').toUpperCase(); }) || {};
      h += usBuildCard(s, ana);
      US.sparks[s.ticker] = s.sparks;
    });
    h += '</div>';
    setTimeout(function(){
      wlStocks.forEach(function(s){
        if(s.sparks) drawSpark('uscv_'+s.ticker, s.sparks['365d']||s.sparks['30d']||[], (s.change_pct||0)>=0);
      });
    }, 100);
  }

  // 5. 热门板块ETF → 侧栏
  var hs = ai.hot_sectors || [];
  if(hs.length) {
    var hsEl = document.getElementById('usHotSectors');
    if(hsEl) hsEl.innerHTML = hs.map(function(s){
      return '<a href="https://finance.yahoo.com/quote/'+s.etf+'" target="_blank" class="sector-tag" style="border-color:rgba(59,130,246,.3);color:#3b82f6">'+s.name+'<span style="opacity:.6;margin-left:3px">'+s.etf+'</span></a>';
    }).join('');
  }

  // 6. AI荐股
  var recos = ai.recommendations || [];
  var shortTerm = recos.filter(function(r){return r.term==='短线';});
  var longTerm  = recos.filter(function(r){return r.term!=='短线' && r.term;});
  if(!shortTerm.length && !longTerm.length && recos.length) longTerm = recos;

  if(!recos.length) {
    h += '<div style="color:var(--mu);font-size:13px;padding:12px 16px;background:var(--sf);border:1px solid var(--bd);border-radius:8px;margin-bottom:16px">'
      +'本次分析暂无推荐（候选池为空或所有候选标的风险过高）<br>'
      +'<span style="font-size:11px">点击「分析美股」重新分析可获取最新推荐</span></div>';
  }
  if(shortTerm.length) {
    h += '<div class="sec-lbl" style="margin-top:4px">'
      +'<span style="background:rgba(245,200,66,.15);color:var(--gd);border:1px solid rgba(245,200,66,.3);padding:2px 10px;border-radius:4px;font-size:12px">⚡ 短线动能</span>'
      +'<span style="margin-left:8px;font-weight:400;color:var(--mu);font-size:11px">板块ETF强势+量价配合，注明离场信号</span></div>'
      +'<div class="grid3">';
    shortTerm.forEach(function(r){ h += usBuildReco(r); });
    h += '</div>';
  }
  if(longTerm.length) {
    h += '<div class="sec-lbl" style="margin-top:8px">'
      +'<span style="background:rgba(59,130,246,.12);color:#3b82f6;border:1px solid rgba(59,130,246,.3);padding:2px 10px;border-radius:4px;font-size:12px">📈 中长线主线</span>'
      +'<span style="margin-left:8px;font-weight:400;color:var(--mu);font-size:11px">AI/医疗/能源核心主线，ETF资金+业绩催化共振</span></div>'
      +'<div class="grid3">';
    longTerm.forEach(function(r){ h += usBuildReco(r); });
    h += '</div>';
  }

  document.getElementById('usMc').innerHTML = h;
  usRenderPort();
  usRenderWL();

  // 7. 新闻 — 单独写入 #usNewsEl（#usMc 外面，排在推荐后面）
  var newsH = '<div class="card" style="margin-top:16px"><div class="sec-lbl" style="margin-bottom:10px">今日美股要闻</div>';
  (data.news||[]).forEach(function(n){
    var title = typeof n==='string' ? n : (n.title||'');
    var url   = typeof n==='string' ? '' : (n.url||n.link||'');
    newsH += '<div class="news-item"><span class="ndot">▸</span>'
      +(url ? '<a href="'+url+'" target="_blank">'+title+'</a>' : '<span style="color:var(--mu)">'+title+'</span>')
      +'</div>';
  });
  newsH += '</div><div class="disc">⚠ 本报告由AI自动生成，仅供参考，不构成投资建议。</div>';
  document.getElementById('usNewsEl').innerHTML = newsH;
}

function usBuildCard(s, ana) {
  var cur = s.close||0;
  var isUp = (s.change_pct||0)>=0;
  var sid = 'uscv_'+s.ticker;
  var sugMap = {'Buy':'sb-buy','Watch':'sb-hold','Hold':'sb-hold','Reduce':'sb-red','Sell':'sb-red'};
  var sugCls = sugMap[ana.suggestion]||'sb-watch';
  var maData = [['MA5',s.ma5],['MA20',s.ma20],['MA50',s.ma50],['MA200',s.ma200]];
  var maH = '';
  maData.forEach(function(row){
    var c = row[1]?(cur>row[1]?'ma-ab':cur<row[1]?'ma-bl':'ma-na'):'ma-na';
    maH += '<div class="ma-item"><span class="ma-lbl">'+row[0]+'</span><span class="ma-val '+c+'">'+(row[1]||'--')+'</span></div>';
  });
  var tabH = '';
  ['5d','30d','90d','180d','365d','5y'].forEach(function(p,i){
    tabH += '<button class="pbtn'+(i===4?' active':'')+'" onclick="usSwPeriod(\''+sid+'\',\''+s.ticker+'\',\''+p+'\',this)">'+p+'</button>';
  });
  var heatCol = (ana.sector_etf||'').indexOf('-')>0&&(ana.sector_etf||'').split('%')[0].includes('-') ? 'var(--dn)' : 'var(--up)';
  var volCol  = (ana.volume_signal||'').indexOf('出货')>-1||(ana.volume_signal||'').indexOf('放量下跌')>-1 ? 'var(--dn)' : (ana.volume_signal||'').indexOf('放量突破')>-1 ? 'var(--up)' : 'var(--mu)';
  var anaH = '';
  var hasAna = ana && (ana.suggestion || ana.reason || ana.earnings_alert || ana.sector_etf || ana.relative_strength);
  if(hasAna){
    anaH = '<div class="sc-ana">'
      +'<div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:6px">'
      +(ana.suggestion?'<span class="sb '+sugCls+'">'+ana.suggestion+'</span>':'')
      +(ana.stock_type?'<span style="font-size:10px;color:var(--mu);border:1px solid var(--bd);padding:1px 6px;border-radius:3px">'+ana.stock_type+'</span>':'')
      +'</div>'
      +(ana.earnings_alert?'<div style="font-size:11px;padding:3px 7px;border-radius:3px;background:rgba(245,200,66,.08);color:var(--gd);margin-bottom:5px">📅 '+ana.earnings_alert+'</div>':'')
      +(ana.sector_etf?'<div style="font-size:11px;padding:3px 7px;border-radius:3px;background:rgba(59,130,246,.08);color:#3b82f6;margin-bottom:5px">📊 '+ana.sector_etf+'</div>':'')
      +(ana.relative_strength?'<div style="font-size:11px;padding:3px 7px;border-radius:3px;background:rgba(100,116,139,.08);color:var(--mu);margin-bottom:5px">📶 '+ana.relative_strength+'</div>':'')
      +(ana.volume_signal?'<div style="font-size:11px;padding:3px 7px;border-radius:3px;background:rgba(100,116,139,.08);color:'+volCol+';margin-bottom:5px">🔊 '+ana.volume_signal+'</div>':'')
      +'<div class="arow" style="color:var(--tx);margin-bottom:4px">'+(ana.reason||'')+'</div>'
      +(ana.entry?'<div style="font-size:11px;color:var(--ac);background:rgba(0,212,170,.06);border-radius:4px;padding:4px 8px;margin-bottom:4px;border-left:2px solid var(--ac)">🎯 '+ana.entry+'</div>':'')
      +(ana.exit?'<div style="font-size:11px;color:var(--dn);background:rgba(255,77,106,.06);border-radius:4px;padding:4px 8px;border-left:2px solid var(--dn)">⚠️ '+ana.exit+'</div>':'')
      +'</div>';
  } else {
    anaH = '<div style="color:var(--mu);font-size:12px;padding:10px 0;text-align:center;opacity:.5">点击「分析美股」获取AI分析</div>';
  }
  return '<div class="stock-card">'
    +'<div class="sc-hdr"><div><div class="sc-name">'+s.ticker+'</div><div class="sc-code">'+s.name+'</div></div>'
    +'<div class="sc-price"><div class="pv '+(isUp?'up':'dn')+'">$'+(s.close||'--')+'</div>'
    +'<div class="pc '+(isUp?'up':'dn')+'">'+(isUp?'+':'')+(s.change_pct||0)+'%</div></div></div>'
    +'<div class="ma-grid">'+maH+'</div>'
    +'<div><div class="ptabs">'+tabH+'</div><canvas class="spark-cv" id="'+sid+'"></canvas></div>'
    +anaH+'</div>';
}

function usSwPeriod(cvId, ticker, period, btn) {
  btn.closest('.ptabs').querySelectorAll('.pbtn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  var sp = US.sparks[ticker]; if(!sp) return;
  var d = sp[period]||[];
  drawSpark(cvId, d, d.length<2||d[d.length-1]>=d[0]);
}

function usBuildReco(r) {
  var isShort = r.term==='短线';
  var cardBg = isShort
    ? 'background:linear-gradient(135deg,rgba(245,200,66,.05),rgba(255,77,106,.04));border:1px solid rgba(245,200,66,.25)'
    : 'background:linear-gradient(135deg,rgba(59,130,246,.05),rgba(0,212,170,.04));border:1px solid rgba(59,130,246,.25)';
  var tagColor = isShort ? 'var(--gd)' : '#3b82f6';
  var alreadyWatched = US.wl && US.wl.some(function(s){return s.ticker===r.ticker;});
  var watchBtn = alreadyWatched
    ? '<button class="btn btn-sm" style="background:rgba(0,212,170,.15);color:var(--ac);border:1px solid var(--ac);cursor:default;font-size:11px;padding:4px 10px;border-radius:5px">✓ 已关注</button>'
    : '<button class="btn btn-sm" onclick="usFollowReco(\''+r.ticker+'\',\''+r.name.replace(/'/g,'')+'\',this)" style="background:rgba(59,130,246,.15);color:#3b82f6;border:1px solid rgba(59,130,246,.4);font-size:11px;padding:4px 10px;border-radius:5px">+ 加入自选</button>';
  return '<div class="reco-card" style="'+cardBg+'">'
    +'<div class="reco-top"><div><div class="reco-nm">'+r.name+'</div><div class="reco-cd">'+r.ticker+'</div></div>'
    +'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:5px">'
    +'<span class="sb '+(r.suggestion==='买入'||r.suggestion==='Buy'?'sb-buy':'sb-hold')+'">'+r.suggestion+'</span>'
    +watchBtn
    +'</div></div>'
    +'<div class="reco-pol" style="color:'+tagColor+'">📊 '+r.sector+'</div>'
    +(r.score?'<div style="font-size:10px;color:var(--mu);margin-bottom:4px">'+r.score+'</div>':'')
    +(r.catalyst?'<div style="font-size:11px;color:var(--ac);margin-bottom:5px">⚡ '+r.catalyst+'</div>':'')
    +'<div class="reco-why">'+(r.reason||'')+'</div>'
    +(r.stop_signal?'<div class="reco-stop" style="color:var(--dn)">⚠️ 离场：'+r.stop_signal+'</div>':'')
    +'<div class="reco-rsk">⚠ '+(r.risk||'')+'</div>'
    +'<div class="reco-ft"><a class="reco-lnk" href="https://finance.yahoo.com/quote/'+r.ticker+'" target="_blank">📈 Yahoo</a>'
    +'<a class="reco-lnk" href="https://finviz.com/quote.ashx?t='+r.ticker+'" target="_blank" style="margin-left:6px">🔭 Finviz</a>'
    +'</div></div>';
}

// 美股推荐 → 加入自选
async function usFollowReco(ticker, name, btn) {
  if(US.wl.some(function(s){return s.ticker===ticker;})){
    btn.textContent='✓ 已关注'; btn.style.color='var(--ac)'; return;
  }
  US.wl.push({ticker: ticker, name: name});
  await api('/api/us/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({watchlist: US.wl})});
  btn.textContent='✓ 已关注';
  btn.style.background='rgba(0,212,170,.15)'; btn.style.color='var(--ac)';
  btn.style.borderColor='var(--ac)'; btn.disabled=true;
  usRenderWL();
}

// 美股持仓
function usRenderPort() {
  var el = document.getElementById('usPortCards'); if(!el) return;
  var port = US.port;
  if(!Object.keys(port).length){
    el.innerHTML='<div style="color:var(--mu);font-size:12px;padding:8px">暂无持仓，点「+ 添加」录入持仓</div>';
    return;
  }
  var latest = US.analysis;
  var wl = (latest&&latest.watchlist)||[];
  el.innerHTML = Object.entries(port).map(function(kv){
    var ticker=kv[0], pos=kv[1];
    var live = wl.find(function(s){return s.ticker===ticker;});
    var cur = live?live.close:null;
    var avg = parseFloat(pos.avg_price||0);
    var qty = parseInt(pos.quantity||0);
    var cost = avg&&qty ? round2(avg*qty) : null;
    var val  = cur&&qty ? round2(cur*qty) : null;
    var pnl  = cur&&avg ? round2((cur-avg)/avg*100) : null;
    var pnlAmt = val&&cost ? round2(val-cost) : null;
    var isUp = (pnl||0) >= 0;
    var cls  = isUp ? 'up' : 'dn';
    var sign = isUp ? '+' : '';
    var diag = US.diagnose[ticker]||null;
    return '<div class="port-card">'
      +'<div class="pc-hdr">'
        +'<div><div class="pc-nm">'+ticker+'</div><div class="pc-cd">'+pos.name+'</div></div>'
        +'<div class="pc-pv">'
          +(cur ? '<div class="pc-v '+cls+'">$'+cur+'</div><div class="pc-cp '+cls+'">'+sign+(pnl||0)+'%</div>' : '<div class="pc-v" style="color:var(--mu)">--</div>')
        +'</div>'
      +'</div>'
      +'<div class="pc-flds">'
        +'<div class="pc-fld"><label>持股数量</label><input type="number" value="'+qty+'" onchange="usUpdPort(\''+ticker+'\',\'quantity\',this.value)"></div>'
        +'<div class="pc-fld"><label>买入均价($)</label><input type="number" step="0.01" value="'+(avg||'')+'" onchange="usUpdPort(\''+ticker+'\',\'avg_price\',this.value)"></div>'
      +'</div>'
      +'<div class="pc-stats">'
        +'<div class="pc-stat"><div class="pcs-lbl">成本</div><div class="pcs-val">'+(cost?'$'+cost:'--')+'</div></div>'
        +'<div class="pc-stat"><div class="pcs-lbl">现值</div><div class="pcs-val">'+(val?'$'+val:'--')+'</div></div>'
        +'<div class="pc-stat"><div class="pcs-lbl">盈亏</div><div class="pcs-val '+cls+'">'+(pnlAmt!==null ? sign+'$'+pnlAmt : '--')+'</div></div>'
      +'</div>'
      +(diag ? '<div class="pc-sug">'
          +(function(){
            var sug = diag.suggestion||'';
            var sugCls = (sug==='加仓'||sug==='Add') ? 'up'
              : (sug==='止损'||sug==='Stop-Loss'||sug==='减仓'||sug==='Trim') ? 'dn'
              : (sug==='止盈'||sug==='Take-Profit') ? 'up'
              : '';
            var sugMap = {'Hold':'持有','Add':'加仓','Trim':'减仓','Stop-Loss':'止损','Take-Profit':'止盈','Watch':'观察'};
            var sugDisplay = sugMap[sug] || sug;
            return '<b class="'+sugCls+'">'+sugDisplay+'</b>';
          })()
          +(diag.analysis ? '<br><span style="color:var(--tx)">'+diag.analysis+'</span>' : '')
          +(diag.earnings_action ? '<br>📅 <span style="color:var(--gd)">'+diag.earnings_action+'</span>' : '')
          +(diag.sector_health ? '<br>📊 <span style="color:var(--mu);font-size:11px">'+diag.sector_health+'</span>' : '')
          +(diag.action ? '<br><b style="color:var(--ac)">操作：</b>'+diag.action : '')
          +(diag.exit_signal ? '<br><b style="color:var(--dn)">离场：</b>'+diag.exit_signal : '')
        +'</div>' : '')
      +'<div style="text-align:right;margin-top:6px">'
        +'<button class="btn btn-g btn-sm" style="font-size:10px" onclick="usDelPort(\''+ticker+'\')">移除</button>'
      +'</div>'
      +'</div>';
  }).join('');
}

async function usUpdPort(ticker, field, val) {
  if(!US.port[ticker]) return;
  US.port[ticker][field] = parseFloat(val)||0;
  await api('/api/us/portfolio',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(US.port)});
  usRenderPort();
}

function usOpenAddPort() {
  ['pCode','pName','pQty','pAvg'].forEach(function(id){document.getElementById(id).value='';});
  document.getElementById('portModalTitle').textContent = '+ 添加持仓（美股）';
  document.getElementById('portCodeLabel').textContent = 'Ticker（如 AAPL）';
  document.getElementById('portPriceLabel').textContent = '买入均价（$）';
  document.getElementById('pCode').placeholder = '如 NVDA';
  document.getElementById('pName').placeholder = '如 NVIDIA';
  document.getElementById('portSaveBtn').onclick = usSavePortEntry;
  document.getElementById('portModal').classList.add('show');
}

async function usSavePortEntry() {
  var ticker = document.getElementById('pCode').value.trim().toUpperCase();
  var name   = document.getElementById('pName').value.trim();
  var qty    = parseFloat(document.getElementById('pQty').value)||0;
  var avg    = parseFloat(document.getElementById('pAvg').value)||0;
  if(!ticker){alert('请输入 Ticker');return;}
  US.port[ticker]={name:name||ticker,quantity:qty,avg_price:avg};
  await api('/api/us/portfolio',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(US.port)});
  closeModal('portModal'); usRenderPort();
}

async function usDelPort(ticker) {
  delete US.port[ticker];
  await api('/api/us/portfolio', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(US.port)});
  usRenderPort();
}

async function usRunDiagnose() {
  if(!Object.keys(US.port).length) return;
  var btn = document.getElementById('usDiagnoseBtn'); if(btn) { btn.disabled=true; btn.textContent='诊断中…'; }
  try {
    var resp = await api('/api/us/diagnose', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({portfolio: US.port})});
    US.diagnose = resp.results || {};
    usRenderPort();
  } catch(e) { console.warn('US diagnose error', e); }
  finally { if(btn) { btn.disabled=false; btn.textContent='🔬 AI诊股'; } }
}

// ── 政策主线 JS ──
var policyOpen = false;
var policyPolling = null;

function togglePolicy() {
  policyOpen = !policyOpen;
  document.getElementById('policyBody').classList.toggle('open', policyOpen);
  document.getElementById('policyArrow').textContent = policyOpen ? '▲' : '▼';
  if (policyOpen && document.getElementById('policyContent').querySelector('.empty-state, div[style*="点击"]')) {
    if (S.policy) {
      renderPolicyAnalysis(S.policy);  // 直接用已缓存的历史数据
    } else {
      api('/api/policy/analysis').then(function(d) {
        if (d && d.status === 'done') { S.policy = d; renderPolicyAnalysis(d); }
        else if (d && d.status === 'running') { policyStartPolling(); }
      }).catch(function(){});
    }
  }
}

async function runPolicyAnalysis() {
  var btn = document.getElementById('policyRunBtn');
  btn.disabled = true; btn.textContent = '分析中…';
  if (!policyOpen) {
    policyOpen = true;
    document.getElementById('policyBody').classList.add('open');
    document.getElementById('policyArrow').textContent = '▲';
  }
  document.getElementById('policyContent').innerHTML =
    '<div style="color:var(--mu);font-size:13px;padding:30px 0;text-align:center">' +
    '⏳ 正在抓取六大政策板块数据 + yfinance 90日走势…<br>' +
    '<span style="font-size:11px;opacity:.7">约需60-90秒</span></div>';
  try {
    await api('/api/policy/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
    policyStartPolling();
  } catch(e) {
    document.getElementById('policyContent').innerHTML = '<div style="color:var(--dn);padding:20px">启动失败：' + e.message + '</div>';
    btn.disabled = false; btn.textContent = '▶ 分析主线';
  }
}

function policyStartPolling() {
  var attempts = 0;
  policyPolling = setInterval(async function() {
    attempts++;
    try {
      var d = await api('/api/policy/analysis');
      if (d.status === 'done') {
        clearInterval(policyPolling);
        S.policy = d; renderPolicyAnalysis(d);
        var btn = document.getElementById('policyRunBtn');
        btn.disabled = false; btn.textContent = '▶ 重新分析';
      } else if (d.status === 'error') {
        clearInterval(policyPolling);
        document.getElementById('policyContent').innerHTML =
          '<div style="color:var(--dn);padding:20px">分析出错：' + (d.message||'') + '</div>';
        var btn = document.getElementById('policyRunBtn');
        btn.disabled = false; btn.textContent = '▶ 分析主线';
      }
    } catch(e) {}
    if (attempts > 60) { clearInterval(policyPolling); }
  }, 3000);
}

function renderPolicyAnalysis(data) {
  var ai = data.ai || {};
  var sectors = data.sectors || [];
  var aiSectors = ai.sectors || [];
  var pt = document.getElementById('policyTime');
  pt.innerHTML = data.updated_at
    ? '政策更新：<span style="font-family:monospace;color:var(--pu)">' + data.updated_at + '</span>'
    : '<span style="color:var(--mu)">尚未分析</span>';

  var stageClass = {
    '酝酿期': 'stage-brew', '启动期': 'stage-start',
    '加速期': 'stage-accel', '调整期': 'stage-adj', '衰退期': 'stage-fade'
  };
  var actionColor = {
    '建仓': 'var(--ac)', '加仓': 'var(--up)', '持有': 'var(--tx)',
    '等待回调': '#3b82f6', '不建议介入': 'var(--mu)', '减仓': 'var(--dn)'
  };

  var h = '';

  // 宏观观点
  if (ai.macro_view) {
    h += '<div style="background:rgba(167,139,250,.07);border:1px solid rgba(167,139,250,.2);border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:13px;color:var(--tx);line-height:1.7">'
      + '🌐 ' + ai.macro_view + '</div>';
  }

  // 六大板块状态格子
  h += '<div class="sector-grid">';
  aiSectors.forEach(function(sec) {
    var raw = sectors.find(function(s){ return s.name === sec.name; }) || {};
    var flowColor = (raw.sector_today_flow||0) >= 0 ? 'var(--up)' : 'var(--dn)';
    var flowSign  = (raw.sector_today_flow||0) >= 0 ? '+' : '';
    var acColor   = actionColor[sec.action] || 'var(--mu)';
    h += '<div class="sector-card">'
      + '<div class="sector-nm">' + sec.name + '</div>'
      + '<span class="stage-tag ' + (stageClass[sec.stage]||'stage-brew') + '">' + (sec.stage||'') + '</span>'
      + (raw.sector_today_flow != null
          ? '<div class="sector-flow" style="margin-top:6px">今日资金 <span style="color:' + flowColor + ';font-weight:600">'
            + flowSign + raw.sector_today_flow + '亿</span>'
            + (raw.sector_today_chg != null ? ' · <span style="color:' + flowColor + '">' + (raw.sector_today_chg>=0?'+':'') + raw.sector_today_chg + '%</span>' : '')
            + '</div>' : '')
      + '<div class="sector-action">建议：<span style="color:' + acColor + ';font-weight:700">' + (sec.action||'') + '</span></div>'
      + '<div style="font-size:11px;color:var(--mu);margin-top:4px;line-height:1.5">' + (sec.stage_reason||'').slice(0,40) + '</div>'
      + '</div>';
  });
  h += '</div>';

  // 不建议板块
  var notRec = ai.not_recommended || [];
  if (notRec.length) {
    h += '<div style="margin-top:14px;padding:10px 14px;background:rgba(255,77,106,.05);border:1px solid rgba(255,77,106,.2);border-radius:8px">'
      + '<div style="font-size:12px;font-weight:700;color:var(--dn);margin-bottom:6px">⛔ 当前不建议介入</div>'
      + notRec.map(function(s){ return '<div style="font-size:11px;color:var(--mu);padding:2px 0">' + s + '</div>'; }).join('')
      + '</div>';
  }

  // 市场风险
  if (ai.market_risk) {
    h += '<div style="margin-top:12px;font-size:12px;color:var(--gd)">⚠ 宏观风险：' + ai.market_risk + '</div>';
  }

  h += '<div style="font-size:11px;color:var(--mu);margin-top:14px;padding-top:10px;border-top:1px solid var(--bd)">'
    + '⚠ 政策主线分析为中长线参考，持仓逻辑以政策方向为主，短期波动无需过度干预。不构成投资建议。</div>';

  document.getElementById('policyContent').innerHTML = h;
}

init();
usInit();
</script>
<div style="text-align:center;padding:40px 20px 32px;border-top:1px solid var(--bd);margin-top:40px">
  <div style="font-size:13px;color:var(--mu);margin-bottom:14px">如果这个工具帮助了你，欢迎打赏支持 🙏</div>
  <img src="/figure/wexin_payment_QR.jpg" alt="微信打赏" style="width:160px;height:160px;border-radius:10px;border:1px solid var(--bd)">
  <div style="font-size:11px;color:var(--mu);margin-top:10px">微信扫码打赏</div>
</div>
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