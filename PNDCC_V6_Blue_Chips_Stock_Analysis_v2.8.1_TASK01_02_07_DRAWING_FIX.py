#!/usr/bin/env python3
"""PNDCC Blue Chips Stock Analysis v2.8.0 — consolidated chart and returns improvement candidate.

Does not import or start the PNDCC master. Uses Canadian-listed provider symbols only
(.TO TSX, .V TSXV, .NE Cboe Canada, .CN Canadian Securities Exchange). It deliberately
never falls back to a US-listed ticker without an exchange suffix. Provider coverage is
verified at runtime; unsupported instruments remain unresolved rather than being guessed.
"""
import json, math, time, re, urllib.parse, urllib.request, urllib.error, http.cookiejar, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST, PORT = "127.0.0.1", 8800
CACHE_SECONDS = 300
LOCK = threading.Lock()
_CACHE = {"at": 0, "data": None}
_SESSION_LOCK = threading.Lock()
_SESSION = {"opener": None, "crumb": None, "at": 0}

# Original supplied list, excluding XNDU, APTX and BRK as requested.
# Industry labels are retained from the user's sheet. Symbols are starting search keys,
# not a claim that each original symbol is itself a TSX/TSXV symbol.
STOCKS = [
("AAPL","Apple","Computers, Phones & Household Electronics"),
("AMD","AMD","Semiconductors & Semiconductor Equipment"),
("META","Meta","Software & IT Services"),
("MSFT","Microsoft","Software & IT Services"),
("NVDA","NVIDIA","Semiconductors & Semiconductor Equipment"),
("TSLA","Tesla","Automobiles & Auto Parts"),
("IBM","IBM","Software & IT Services"),
("GOOG","Alphabet","Software & IT Services"),
("AMZN","Amazon","Diversified Retail"),
("INTC","Intel","Semiconductors & Semiconductor Equipment"),
("PFE","Pfizer","Pharmaceuticals"),
("LLY","Eli Lilly","Pharmaceuticals"),
("MU","Micron","Semiconductors & Semiconductor Equipment"),
("ASML","ASML","Semiconductors & Semiconductor Equipment"),
("AVGO","Broadcom","Semiconductors & Semiconductor Equipment"),
("SMCI","Super Micro","Computers, Phones & Household Electronics"),
("QCOM","Qualcomm","Semiconductors & Semiconductor Equipment"),
("AMAT","Applied Materials","Semiconductors & Semiconductor Equipment"),
("TXN","Texas Instruments","Semiconductors & Semiconductor Equipment"),
("LRCX","Lam Research","Semiconductors & Semiconductor Equipment"),
("ANET","Arista Networks","Communications & Networking"),
("MRV","Marvell","Semiconductors & Semiconductor Equipment"),
("VRT","Vertiv","Machinery, Equipment & Components"),
("LMT","Lockheed Martin","Aerospace & Defense"),
("KLAC","KLA","Semiconductors & Semiconductor Equipment"),
("PNG","Kraken Robotics","Aerospace & Defense"),
("MDA","MDA Space","Aerospace & Defense"),
("JNJ","J&J","Pharmaceuticals"),
("BA","Boeing","Aerospace & Defense"),
("CRWD","CrowdStrike","Software & IT Services"),
("WDC","Western Digital","Computers, Phones & Household Electronics"),
("SPCX","SpaceX CDR","Telecommunications Services"),
("ABBV","AbbVie","Pharmaceuticals"),
("GE","GE Aerospace","Aerospace & Defense"),
("BB","BlackBerry","Software & IT Services"),
("NVS","Novartis","Pharmaceuticals"),
("MRK","Merck","Pharmaceuticals"),
("SNDK","SanDisk","Computers, Phones & Household Electronics"),
("NOVO","Novo Nordisk","Pharmaceuticals"),
]

SUFFIXES = [".TO", ".V", ".NE", ".CN"]

# Price/technical data always uses the verified Canadian listing. For US issuers,
# fundamentals are fetched separately from the issuer's primary US ticker so the
# financial statements and EPS are not confused with the Canadian trading currency.
# These are fundamentals-only source symbols; they never replace Canadian price data.
US_FUNDAMENTALS_SYMBOLS = {
    "AAPL", "AMD", "META", "MSFT", "NVDA", "TSLA", "IBM", "GOOG", "AMZN",
    "INTC", "PFE", "LLY", "MU", "AVGO", "SMCI", "QCOM", "AMAT", "TXN",
    "LRCX", "ANET", "MRV", "VRT", "LMT", "KLAC", "JNJ", "BA", "CRWD",
    "WDC", "ABBV", "GE", "BB", "SNDK"
}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 PNDCC-BlueChips/1.0"

def _get(url, timeout=10, opener=None, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*", **(headers or {})})
    op = opener or urllib.request.build_opener()
    with op.open(req, timeout=timeout) as r:
        raw = r.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Provider response exceeded size limit")
    return json.loads(raw.decode("utf-8"))

def _yahoo_session(force=False):
    with _SESSION_LOCK:
        if not force and _SESSION["opener"] and time.time() - _SESSION["at"] < 1800:
            return _SESSION["opener"], _SESSION["crumb"]
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            req = urllib.request.Request("https://fc.yahoo.com/", headers={"User-Agent": UA})
            try:
                opener.open(req, timeout=8).read(1024)
            except Exception:
                pass
            crumb_req = urllib.request.Request("https://query1.finance.yahoo.com/v1/test/getcrumb", headers={"User-Agent": UA})
            with opener.open(crumb_req, timeout=8) as resp:
                crumb = resp.read(256).decode("utf-8", "replace").strip()
            if not crumb or "{" in crumb or len(crumb) > 100:
                crumb = None
        except Exception:
            crumb = None
        _SESSION.update(opener=opener, crumb=crumb, at=time.time())
        return opener, crumb

def _chart(ticker):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker, safe=".-^=") + "?range=5y&interval=1d&events=div%2Csplits"
    data = _get(url, timeout=12)
    root = data.get("chart") or {}
    if root.get("error") or not root.get("result"):
        raise ValueError((root.get("error") or {}).get("description") or "No price history returned")
    item = root["result"][0]
    stamps = item.get("timestamp") or []
    quote = ((item.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((item.get("indicators") or {}).get("adjclose") or [{}])
    adj_values = (adj[0].get("adjclose") or []) if adj else []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    bars = []
    for i, (stamp, close) in enumerate(zip(stamps, closes)):
        try:
            if close is None or not math.isfinite(float(close)) or float(close) <= 0: continue
            dt = datetime.fromtimestamp(int(stamp)).date().isoformat()
            adjusted = adj_values[i] if i < len(adj_values) else None
            # Provider occasionally emits null/non-finite adjusted-close entries. Keep the
            # date and price series aligned by falling back to the same day's valid close.
            try:
                adjusted_value = float(adjusted) if adjusted is not None else float(close)
                if not math.isfinite(adjusted_value) or adjusted_value <= 0:
                    adjusted_value = float(close)
            except (TypeError, ValueError, OverflowError):
                adjusted_value = float(close)
            bars.append({"date":dt,"close":float(close),"adjclose":adjusted_value,"volume":int(volumes[i]) if i < len(volumes) and volumes[i] is not None else None})
        except Exception: continue
    if len(bars) < 20: raise ValueError("Fewer than 20 daily price observations")
    meta = item.get("meta") or {}
    exchange = str(meta.get("fullExchangeName") or meta.get("exchangeName") or "")
    currency = str(meta.get("currency") or "")
    # Exchange suffix is necessary but not sufficient: verify provider metadata as well.
    suffix = ticker.rsplit(".", 1)[-1] if "." in ticker else ""
    expected = {"TO":("Toronto Stock Exchange", ("toronto",)), "V":("TSX Venture Exchange", ("venture", "tsx")), "NE":("Cboe Canada", ("cboe", "neo")), "CN":("Canadian Securities Exchange", ("canadian securities", "cse"))}
    if suffix not in expected:
        raise ValueError("Ticker is not a Canadian exchange-suffixed symbol")
    if not exchange:
        raise ValueError("Provider did not return exchange metadata; listing remains unverified")
    normalized_exchange=exchange.casefold()
    if not any(token in normalized_exchange for token in expected[suffix][1]):
        raise ValueError("Exchange metadata mismatch for "+ticker+": "+exchange)
    if currency.upper()!="CAD":
        raise ValueError("Expected CAD trading currency for Canadian listing; provider returned "+(currency or "unknown"))
    return {"bars":bars,"meta":{"symbol":meta.get("symbol",ticker),"exchange":exchange,"currency":currency,"exchange_suffix":suffix,"regularMarketPrice":meta.get("regularMarketPrice"),"fiftyTwoWeekHigh":meta.get("fiftyTwoWeekHigh"),"fiftyTwoWeekLow":meta.get("fiftyTwoWeekLow")}}

def _resolve(stock):
    base = stock[0]
    errors=[]
    for suffix in SUFFIXES:
        candidate=base+suffix
        try:
            payload=_chart(candidate)
            meta=payload["meta"]
            # Guard against provider aliasing to a different symbol.
            provider_symbol=str(meta.get("symbol") or "").upper()
            if provider_symbol and provider_symbol != candidate.upper():
                errors.append(candidate+": provider symbol mismatch ("+provider_symbol+")")
                continue
            return candidate,payload,None
        except Exception as e:
            errors.append(candidate+": "+str(e)[:100])
    return None,None,"No verified Canadian listing found. Checked "+", ".join(errors)

def _moving(values,n):
    if len(values)<n:return None
    return sum(values[-n:])/n

def _stats(bars):
    # Keep every close paired with its own date. The previous implementation filtered
    # `closes` independently but indexed it using positions from `bars`; one invalid
    # adjusted close could therefore shift all historical return anchors.
    valid_bars = []
    for bar in bars:
        try:
            date_value = datetime.strptime(str(bar.get("date", "")), "%Y-%m-%d").date()
            close_value = float(bar.get("close"))
            adjusted_value = float(bar.get("adjclose"))
            if not (math.isfinite(close_value) and close_value > 0 and math.isfinite(adjusted_value) and adjusted_value > 0):
                continue
            valid_bars.append({**bar, "date": date_value.isoformat(), "close": close_value, "adjclose": adjusted_value})
        except (TypeError, ValueError, OverflowError):
            continue
    bars = valid_bars
    if len(bars) < 2:
        raise ValueError("Fewer than two valid date-aligned price observations")
    closes=[float(b["adjclose"]) for b in bars]
    raw_closes=[float(b["close"]) for b in bars]
    returns=[closes[i]/closes[i-1]-1 for i in range(1,len(closes)) if closes[i-1]>0]
    log_returns=[math.log(closes[i]/closes[i-1]) for i in range(1,len(closes)) if closes[i-1]>0]
    n=len(returns)
    avg=sum(returns)/n if n else 0.0
    sample_var=sum((x-avg)**2 for x in returns)/(n-1) if n>1 else 0.0
    daily_vol=math.sqrt(sample_var) if sample_var>0 else 0.0
    vol=daily_vol*math.sqrt(252)*100 if n>1 else None
    downside=[min(0.0,x) for x in returns]
    downside_dev=math.sqrt(sum(x*x for x in downside)/n)*math.sqrt(252) if n else None
    risk_free_annual=0.04
    sharpe=((avg*252-risk_free_annual)/(daily_vol*math.sqrt(252))) if daily_vol>0 else None
    sortino=((avg*252-risk_free_annual)/downside_dev) if downside_dev and downside_dev>0 else None
    peak=closes[0] if closes else 0.0; maxdd=0.0; drawdowns=[]
    for v in closes:
        peak=max(peak,v)
        dd=(v/peak-1)*100 if peak>0 else 0.0
        drawdowns.append(dd); maxdd=min(maxdd,dd)
    def change(days):
        if len(closes)<=days:return None
        return (closes[-1]/closes[-days-1]-1)*100
    return_anchors = {}
    def change_years(years):
        if len(closes)<2:
            return_anchors[years] = {"date": None, "gap_days": None}
            return None
        latest_date=datetime.strptime(bars[-1]["date"],"%Y-%m-%d").date()
        try:
            target=latest_date.replace(year=latest_date.year-years)
        except ValueError:
            target=latest_date.replace(year=latest_date.year-years,day=28)
        candidates=[]
        # `bars` and `closes` are now built from the same validated records, so idx
        # always points to the adjusted close belonging to the selected trading date.
        for idx,bar in enumerate(bars):
            d=datetime.strptime(bar["date"],"%Y-%m-%d").date()
            if idx < len(closes)-1:
                candidates.append((abs((d-target).days),idx,d))
        if not candidates:
            return_anchors[years] = {"date": None, "gap_days": None}
            return None
        distance,idx,anchor_date=min(candidates,key=lambda item:item[0])
        return_anchors[years] = {"date": anchor_date.isoformat(), "gap_days": distance}
        # Task 01: use a flexible but bounded anchor window for 1Y only.
        # Provider gaps and unusual holiday/session coverage can leave the closest
        # valid observation more than two weeks from the calendar anniversary.
        # Keep the 2Y/3Y/5Y windows strict; never extrapolate or invent prices.
        anchor_tolerance_days = 21 if years == 1 else 7
        if distance > anchor_tolerance_days:
            return None
        return (closes[-1]/closes[idx]-1)*100
    def sma(values,n): return sum(values[-n:])/n if len(values)>=n else None
    def ema_series(values,period):
        if not values:return []
        alpha=2/(period+1); out=[values[0]]
        for value in values[1:]: out.append(alpha*value+(1-alpha)*out[-1])
        return out
    def rsi(values,period=14):
        if len(values)<=period:return None
        changes=[values[i]-values[i-1] for i in range(1,len(values))]
        gains=[max(x,0) for x in changes]; losses=[max(-x,0) for x in changes]
        ag=sum(gains[:period])/period; al=sum(losses[:period])/period
        for i in range(period,len(changes)):
            ag=((period-1)*ag+gains[i])/period; al=((period-1)*al+losses[i])/period
        if al==0:return 100.0 if ag>0 else 50.0
        return 100-(100/(1+ag/al))
    def pct_rank(values, fraction):
        if len(values)<2:return None
        ordered=sorted(values); idx=min(len(ordered)-1,max(0,int(round((len(ordered)-1)*fraction))))
        return ordered[idx]*100
    e12=ema_series(closes,12); e26=ema_series(closes,26)
    macd_line=[a-b for a,b in zip(e12,e26)] if len(e12)==len(e26) else []
    signal_line=ema_series(macd_line,9)
    macd=macd_line[-1] if macd_line else None
    macd_signal=signal_line[-1] if signal_line else None
    macd_hist=(macd-macd_signal) if macd is not None and macd_signal is not None else None
    sma20=sma(closes,20); sma50=sma(closes,50); latest=closes[-1] if closes else None
    rsi14=rsi(closes,14)
    if latest is not None and sma20 is not None and sma50 is not None:
        if latest>sma20 and sma20>sma50: trend="Bullish uptrend"
        elif latest<sma20 and sma20<sma50: trend="Bearish downtrend"
        else: trend="Mixed / transition"
    else: trend="Insufficient history"
    signals=[]
    if rsi14 is not None: signals.append("RSI overbought" if rsi14>=70 else "RSI oversold" if rsi14<=30 else "RSI neutral")
    if macd_hist is not None: signals.append("MACD bullish momentum" if macd_hist>0 else "MACD bearish momentum" if macd_hist<0 else "MACD neutral")
    if latest is not None and sma20 is not None: signals.append("Price above SMA20" if latest>sma20 else "Price below SMA20")
    # Simple, transparent historical-distribution scenario; not a guaranteed prediction.
    mean_log=sum(log_returns)/len(log_returns) if log_returns else 0.0
    log_var=sum((x-mean_log)**2 for x in log_returns)/(len(log_returns)-1) if len(log_returns)>1 else 0.0
    monthly_drift=math.exp(mean_log*21)-1
    monthly_sigma=math.sqrt(max(0,log_var)*21)
    bear=(math.exp(mean_log*21-1.28*monthly_sigma)-1)*100 if log_returns else None
    base=(math.exp(mean_log*21)-1)*100 if log_returns else None
    bull=(math.exp(mean_log*21+1.28*monthly_sigma)-1)*100 if log_returns else None
    tail5=pct_rank(returns,0.05); var95=-tail5 if tail5 is not None else None
    losses=sorted(x for x in returns if x<= (tail5/100 if tail5 is not None else -999))
    cvar95=-(sum(losses)/len(losses)*100) if losses else None
    skew=kurt=None
    if n>=3 and daily_vol>0:
        m3=sum((x-avg)**3 for x in returns)/n
        m4=sum((x-avg)**4 for x in returns)/n
        skew=m3/(math.sqrt(sum((x-avg)**2 for x in returns)/n)**3)
        kurt=m4/((sum((x-avg)**2 for x in returns)/n)**2)-3
    volumes=[b.get("volume") for b in bars if isinstance(b.get("volume"),(int,float))]
    vol20=(sum(volumes[-20:])/min(20,len(volumes))) if volumes else None
    def rolling_vol(period):
        if len(returns)<period:return None
        window=returns[-period:]; mu=sum(window)/len(window)
        return math.sqrt(sum((x-mu)**2 for x in window)/(len(window)-1))*math.sqrt(252)*100 if len(window)>1 else None
    annualized_arithmetic_return=(avg*252*100) if n else None
    cagr_approx=((closes[-1]/closes[0])**(252/max(1,len(closes)-1))-1)*100 if len(closes)>1 else None
    return {"price":bars[-1]["close"],"adjusted_price":latest,"last_date":bars[-1]["date"],
      "change_1d_pct":change(1),"change_1m_pct":change(21),"change_3m_pct":change(63),"change_6m_pct":change(126),
      "change_1y_pct":change_years(1),"change_2y_pct":change_years(2),"change_3y_pct":change_years(3),"change_5y_pct":change_years(5),
      "return_1y_anchor_date":return_anchors.get(1,{}).get("date"),"return_1y_anchor_gap_days":return_anchors.get(1,{}).get("gap_days"),
      "return_2y_anchor_date":return_anchors.get(2,{}).get("date"),"return_2y_anchor_gap_days":return_anchors.get(2,{}).get("gap_days"),
      "return_3y_anchor_date":return_anchors.get(3,{}).get("date"),"return_3y_anchor_gap_days":return_anchors.get(3,{}).get("gap_days"),
      "return_5y_anchor_date":return_anchors.get(5,{}).get("date"),"return_5y_anchor_gap_days":return_anchors.get(5,{}).get("gap_days"),
      "sma20":sma20,"sma50":sma50,"ema12":e12[-1] if e12 else None,"ema26":e26[-1] if e26 else None,
      "rsi14":rsi14,"macd":macd,"macd_signal":macd_signal,"macd_histogram":macd_hist,
      "trend_status":trend,"technical_signals":signals,"annualized_volatility_pct":vol,
      "sharpe_ratio":sharpe,"sortino_ratio":sortino,"risk_free_rate_assumption_pct":risk_free_annual*100,
      "annualized_arithmetic_return_pct":annualized_arithmetic_return,"annualized_cagr_approx_pct":cagr_approx,
      "rolling_volatility_20d_pct":rolling_vol(20),"rolling_volatility_60d_pct":rolling_vol(60),
      "downside_deviation_pct":downside_dev*100 if downside_dev is not None else None,
      "historical_var_95_daily_pct":var95,"historical_cvar_95_daily_pct":cvar95,
      "daily_return_skewness":skew,"daily_return_excess_kurtosis":kurt,
      "max_drawdown_pct":maxdd,"current_drawdown_pct":drawdowns[-1] if drawdowns else None,
      "scenario_1m_bear_pct":bear,"scenario_1m_base_pct":base,"scenario_1m_bull_pct":bull,
      "scenario_horizon_trading_days":21,"scenario_method":"Historical daily log-return drift ±1.28 standard deviations over 21 trading days; illustrative, not a forecast guarantee",
      "observations":len(bars),"history_start_date":bars[0]["date"],"history_years":max(0.0,(datetime.strptime(bars[-1]["date"],"%Y-%m-%d").date()-datetime.strptime(bars[0]["date"],"%Y-%m-%d").date()).days/365.2425),"volume_latest":bars[-1].get("volume"),"average_volume_20d":vol20,
      "fifty_two_week_high":(max(b["close"] for b in bars[-252:]) if len(bars)>=252 else None),"fifty_two_week_low":(min(b["close"] for b in bars[-252:]) if len(bars)>=252 else None)}


def _news(company_name, symbol):
    """Fetch best-effort current Yahoo Finance news; tolerate malformed/provider variants."""
    query=urllib.parse.quote(str(company_name or symbol)[:120])
    url="https://query1.finance.yahoo.com/v1/finance/search?q="+query+"&quotesCount=0&newsCount=8"
    try:
        data=_get(url,timeout=8)
        # Some provider/proxy responses may be JSON encoded as a string rather than an object.
        if isinstance(data, str):
            try:
                data=json.loads(data)
            except (TypeError, ValueError):
                return {"status":"unavailable","items":[],"reason":"News provider returned text instead of a usable JSON object.","source":"Yahoo Finance news search"}
        if not isinstance(data, dict):
            return {"status":"unavailable","items":[],"reason":"News provider returned an unexpected response format.","source":"Yahoo Finance news search"}
        raw_items=data.get("news") or []
        if not isinstance(raw_items, list):
            raw_items=[]
        items=[]
        for item in raw_items[:8]:
            # Ignore malformed entries instead of failing the whole news panel.
            if isinstance(item, str):
                try:
                    item=json.loads(item)
                except (TypeError, ValueError):
                    continue
            if not isinstance(item, dict):
                continue
            title=item.get("title"); link=item.get("link")
            if not isinstance(title, str) or not title.strip() or not isinstance(link, str) or not link.strip():
                continue
            publisher=item.get("publisher") or item.get("source") or "Source not specified"
            stamp=item.get("providerPublishTime")
            try:
                published=datetime.fromtimestamp(int(stamp)).astimezone().isoformat(timespec="minutes") if stamp else None
            except (TypeError, ValueError, OverflowError, OSError):
                published=None
            related=item.get("relatedTickers") or []
            if not isinstance(related, list):
                related=[]
            items.append({"title":title[:300],"link":link,"publisher":str(publisher)[:120],"published_at":published,
                          "related_tickers":[x.get("symbol") for x in related if isinstance(x,dict) and isinstance(x.get("symbol"),str)]})
        # Keep company-specific stories separate from broader market/industry context.
        aliases={"AAPL":["apple"],"AMD":["advanced micro devices","amd"],"META":["meta platforms","meta","facebook"],"MSFT":["microsoft"],"NVDA":["nvidia"],"TSLA":["tesla"],"IBM":["ibm","international business machines"],"GOOG":["alphabet","google"],"AMZN":["amazon"],"INTC":["intel"],"PFE":["pfizer"],"LLY":["eli lilly","lilly"],"MU":["micron"],"ASML":["asml"],"AVGO":["broadcom"],"SMCI":["super micro","smci"],"QCOM":["qualcomm"],"AMAT":["applied materials"],"TXN":["texas instruments"],"LRCX":["lam research"],"ANET":["arista networks","arista"],"MRV":["marvell"],"VRT":["vertiv"],"LMT":["lockheed martin"],"KLAC":["kla corporation","kla"],"PNG":["kraken robotics"],"MDA":["mda space"],"JNJ":["johnson & johnson","johnson and johnson"],"BA":["boeing"],"CRWD":["crowdstrike"],"WDC":["western digital"],"SPCX":["spacex"],"ABBV":["abbvie"],"GE":["ge aerospace","general electric"],"BB":["blackberry"],"NVS":["novartis"],"MRK":["merck"],"SNDK":["sandisk"],"NOVO":["novo nordisk"]}
        aliases_for_symbol=aliases.get(str(symbol).upper(),[str(company_name or symbol).split()[0].lower()])
        company_items=[]; context_items=[]
        for item in items:
            title_l=item["title"].casefold()
            ticker_match=bool(re.search(r"(?<![A-Z0-9])"+re.escape(str(symbol).upper())+r"(?![A-Z0-9])",item["title"],re.I))
            related_match=str(symbol).upper() in [x.upper() for x in item.get("related_tickers",[])]
            name_match=any(alias.casefold() in title_l for alias in aliases_for_symbol if len(alias)>=3)
            (company_items if ticker_match or related_match or name_match else context_items).append(item)
        return {"status":"available" if company_items or context_items else "empty","items":company_items,"context_items":context_items,"retrieved_at":datetime.now().astimezone().isoformat(timespec="seconds"),"source":"Yahoo Finance news search","note":"Company-specific headlines are separated from broader market context. Headlines do not prove causation for price movements."}
    except Exception as e:
        return {"status":"unavailable","items":[],"reason":"News provider unavailable: "+str(e)[:150],"source":"Yahoo Finance news search"}

def _fundamentals(ticker, source_ticker=None):
    source_ticker = source_ticker or ticker
    # Yahoo quoteSummary requires a valid session/crumb in many regions. Return explicit
    # availability/error status rather than fabricating or substituting fundamentals.
    modules="price,summaryDetail,defaultKeyStatistics,financialData,incomeStatementHistory,balanceSheetHistory,cashflowStatementHistory,calendarEvents"
    opener,crumb=_yahoo_session()
    if not crumb:
        return {"status":"unavailable","reason":"Yahoo fundamentals session/crumb unavailable; price history may still work."}
    url="https://query1.finance.yahoo.com/v10/finance/quoteSummary/"+urllib.parse.quote(source_ticker,safe=".-")+"?modules="+urllib.parse.quote(modules)+"&crumb="+urllib.parse.quote(crumb,safe="")
    try:
        data=_get(url,timeout=12,opener=opener)
        root=data.get("quoteSummary") or {}
        if root.get("error") or not root.get("result"):
            return {"status":"unavailable","reason":str((root.get("error") or {}).get("description") or "No fundamentals returned")[:180]}
        x=root["result"][0]
        def raw(obj,key):
            v=(obj or {}).get(key)
            if isinstance(v,dict): return v.get("raw")
            return v
        price=x.get("price") or {}; detail=x.get("summaryDetail") or {}; keys=x.get("defaultKeyStatistics") or {}; fin=x.get("financialData") or {}; cal=x.get("calendarEvents") or {}
        inc=((x.get("incomeStatementHistory") or {}).get("incomeStatementHistory") or [])
        bal=((x.get("balanceSheetHistory") or {}).get("balanceSheetStatements") or [])
        cf=((x.get("cashflowStatementHistory") or {}).get("cashflowStatements") or [])
        def statement_value(rows,key):
            if not rows:return None
            return raw(rows[0],key)
        rev=statement_value(inc,"totalRevenue"); prevrev=statement_value(inc[1:],"totalRevenue") if len(inc)>1 else None
        earnings=statement_value(inc,"netIncome"); prevearn=statement_value(inc[1:],"netIncome") if len(inc)>1 else None
        cash=statement_value(cf,"totalCashFromOperatingActivities"); capex=statement_value(cf,"capitalExpenditures")
        debt=raw(fin,"totalDebt"); cash_balance=raw(fin,"totalCash")
        fcf=raw(fin,"freeCashflow")
        if fcf is None and cash is not None and capex is not None: fcf=cash+capex # Yahoo usually reports capex as negative.
        def growth(cur,prev): return ((cur/prev)-1)*100 if isinstance(cur,(int,float)) and isinstance(prev,(int,float)) and prev else None
        earnings_block=cal.get("earnings") or {}; earnings_dates=earnings_block.get("earningsDate") or []; next_earnings=None
        now_epoch=time.time()
        for entry in earnings_dates:
            stamp=raw(entry,"raw") if isinstance(entry,dict) else entry
            try:
                stamp=int(stamp) if stamp else 0
                if stamp>now_epoch and (next_earnings is None or stamp<next_earnings): next_earnings=stamp
            except (TypeError,ValueError): pass
        next_earnings_date=datetime.fromtimestamp(next_earnings).astimezone().date().isoformat() if next_earnings else None
        out={"status":"available","company_name":raw(price,"longName") or raw(price,"shortName"),"currency":raw(price,"currency"),"market_cap":raw(price,"marketCap"),"trailing_pe":raw(detail,"trailingPE"),"forward_pe":raw(detail,"forwardPE"),"peg_ratio":raw(keys,"pegRatio") if raw(keys,"pegRatio") is not None else raw(keys,"trailingPegRatio"),"next_earnings_date":next_earnings_date,"price_to_book":raw(keys,"priceToBook"),"eps_ttm":raw(keys,"trailingEps"),"eps_forward":raw(keys,"forwardEps"),"dividend_yield_pct":(raw(detail,"dividendYield")*100 if isinstance(raw(detail,"dividendYield"),(int,float)) else None),"payout_ratio_pct":(raw(detail,"payoutRatio")*100 if isinstance(raw(detail,"payoutRatio"),(int,float)) else None),"revenue_latest":rev,"revenue_growth_pct":growth(rev,prevrev),"net_income_latest":earnings,"net_income_growth_pct":growth(earnings,prevearn),"profit_margin_pct":(raw(fin,"profitMargins")*100 if isinstance(raw(fin,"profitMargins"),(int,float)) else None),"operating_margin_pct":(raw(fin,"operatingMargins")*100 if isinstance(raw(fin,"operatingMargins"),(int,float)) else None),"free_cash_flow":fcf,"total_debt":debt,"total_cash":cash_balance,"debt_to_equity":raw(fin,"debtToEquity"),"current_ratio":raw(fin,"currentRatio"),"quick_ratio":raw(fin,"quickRatio"),"gross_margin_pct":(raw(fin,"grossMargins")*100 if isinstance(raw(fin,"grossMargins"),(int,float)) else None),"return_on_assets_pct":(raw(fin,"returnOnAssets")*100 if isinstance(raw(fin,"returnOnAssets"),(int,float)) else None),"return_on_equity_pct":(raw(fin,"returnOnEquity")*100 if isinstance(raw(fin,"returnOnEquity"),(int,float)) else None),"source_symbol":source_ticker,"source":"Yahoo Finance quoteSummary; issuer fundamentals source is separate from Canadian listing price data; reporting periods/definitions may differ by issuer"}
        return out
    except Exception as e:
        # Refresh session once for common crumb/session failures.
        if "401" in str(e) or "403" in str(e):
            try:
                opener,crumb=_yahoo_session(force=True)
                if crumb:
                    url="https://query1.finance.yahoo.com/v10/finance/quoteSummary/"+urllib.parse.quote(source_ticker,safe=".-")+"?modules="+urllib.parse.quote(modules)+"&crumb="+urllib.parse.quote(crumb,safe="")
                    data=_get(url,timeout=10,opener=opener)
                    root=(data.get("quoteSummary") or {}).get("result") or []
                    if root: return {"status":"unavailable","reason":"Fundamentals endpoint responded after session refresh; parser retry required."}
            except Exception: pass
        return {"status":"unavailable","reason":"Fundamentals request failed: "+str(e)[:150]}

def _load_all(force=False):
    with LOCK:
        if not force and _CACHE["data"] and time.time()-_CACHE["at"]<CACHE_SECONDS:
            return _CACHE["data"]
    assets=[]
    # Deliberately serial requests to reduce provider throttling and keep startup responsive.
    for symbol,name,industry in STOCKS:
        a={"symbol":symbol,"name":name,"industry":industry,"status":"unresolved","provider_symbol":None,"listing_exchange":None,"currency":None,"price_data":None,"fundamentals":{"status":"unavailable","reason":"Not requested until a Canadian listing is verified."},"errors":[]}
        resolved,history,error=_resolve((symbol,name,industry))
        if resolved:
            a["provider_symbol"]=resolved
            a["listing_exchange"]=history["meta"].get("exchange")
            a["currency"]=history["meta"].get("currency")
            a["status"]="available"
            a["price_data"]={**_stats(history["bars"]),"bars":history["bars"]}
            fundamentals_source = symbol if symbol in US_FUNDAMENTALS_SYMBOLS else resolved
            a["fundamentals"]=_fundamentals(resolved, fundamentals_source)
        else:
            a["errors"].append(error)
        assets.append(a)
    available=sum(1 for a in assets if a["status"]=="available")
    fundamentals=sum(1 for a in assets if a["fundamentals"].get("status")=="available")
    data={"assets":assets,"summary":{"requested":len(assets),"price_available":available,"price_unavailable":len(assets)-available,"fundamentals_available":fundamentals,"retrieved_at":datetime.now().astimezone().isoformat(timespec="seconds"),"cache_seconds":CACHE_SECONDS},"notes":["Price histories are requested for up to five years; 1Y/2Y/3Y/5Y returns use date-aligned adjusted closes and calendar-year lookbacks. Return anchor dates/gaps are included in the API response for diagnostics.","Only verified Canadian exchange-suffixed symbols are used; there is no silent US ticker fallback.","Yahoo Finance data may be incomplete, delayed, unavailable or subject to provider changes.","Fundamental metrics can use different reporting periods and definitions; inspect source/period before comparing.","This analytical tool is educational, not investment advice."]}
    with LOCK: _CACHE.update(at=time.time(),data=data)
    return data

HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PNDCC · Blue Chips Stock Analysis</title><style>
:root{color-scheme:light dark;--panel:color-mix(in srgb,Canvas 94%,#718096 6%);--line:color-mix(in srgb,CanvasText 18%,transparent);--muted:color-mix(in srgb,CanvasText 68%,transparent);--accent:#2563eb}*{box-sizing:border-box}body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:20px;max-width:1500px;margin-inline:auto;background:Canvas;color:CanvasText}header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:20px}h1{font-size:28px;margin:4px 0}h2{font-size:19px;margin:0 0 12px}h3{font-size:16px;margin:0 0 8px}.muted{color:var(--muted)}button,select,input{font:inherit;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:Canvas;color:CanvasText}button{cursor:pointer}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:16px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.kpi{padding:14px;border:1px solid var(--line);border-radius:10px}.kpi strong{font-size:24px;display:block}.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.tablewrap{overflow:auto;max-height:65vh}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}thead{position:sticky;top:0;background:Canvas;z-index:1}tr[data-id]{cursor:pointer}tr[data-id]:hover{background:color-mix(in srgb,var(--accent) 9%,transparent)}.positive{color:#16a34a}.negative{color:#dc2626}.overbought{color:#b45309;font-weight:700}.oversold{color:#2563eb;font-weight:700}.badge{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:2px 8px;font-size:12px}.details{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.metric{border:1px solid var(--line);border-radius:10px;padding:10px;min-width:0}.metric .label{font-size:12px;color:var(--muted)}.metric .value{font-size:17px;font-weight:650;overflow-wrap:anywhere}.metric .comment{font-size:11px;font-weight:700;letter-spacing:.035em;margin-left:6px;text-transform:uppercase}.chart-wrap{margin:8px 0}.chart-help{font-size:12px;color:var(--muted);margin:3px 0 8px}.metric .value.positive{color:#16a34a}.metric .value.negative{color:#dc2626}.chart{width:100%;height:auto;display:block;min-height:170px}.twocol{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}.detail-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;flex-wrap:wrap}.range-card{border:1px solid var(--line);border-radius:12px;padding:12px;min-width:260px;max-width:390px;flex:1}.range-line{position:relative;height:14px;border-radius:99px;background:linear-gradient(90deg,#dc2626,#d97706,#16a34a);margin:16px 5px 8px}.range-marker{position:absolute;top:50%;width:14px;height:14px;border:3px solid Canvas;background:CanvasText;border-radius:50%;transform:translate(-50%,-50%)}.range-labels{display:flex;justify-content:space-between;gap:10px;font-size:12px;color:var(--muted)}.forecast-controls{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0}.forecast-controls label{display:inline-flex;align-items:center;gap:6px}.chart-layout{display:grid;grid-template-columns:minmax(165px,220px) minmax(0,1fr);gap:12px;align-items:stretch}.chart-side-indicators{display:grid;grid-template-columns:1fr;gap:8px;align-content:start}.chart-side-indicators .metric .value{font-size:19px}.chart-interactive{position:relative;width:100%;min-width:0}.chart-interactive>.chart{position:relative;z-index:1}.chart-draw-overlay{position:absolute;inset:0;width:100%;height:100%;z-index:2;overflow:visible;touch-action:none}.chart-draw-overlay[data-mode="select"]{pointer-events:auto}.chart-draw-overlay[data-mode="none"]{pointer-events:none}.drawing-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px;border:1px solid var(--line);border-radius:10px;margin:8px 0 12px}.drawing-toolbar label{display:inline-flex;gap:5px;align-items:center;font-size:12px}.drawing-toolbar select,.drawing-toolbar input{padding:5px}.draw-shape{cursor:move;vector-effect:non-scaling-stroke}.draw-shape.selected{filter:drop-shadow(0 0 2px #2563eb)}.forecast-card h4{font-size:17px;font-weight:800;text-align:center;margin:0 0 8px}.forecast-card .forecast-summary{text-align:center;font-weight:750;font-size:14px;color:var(--accent);margin:4px 0 10px}.forecast-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}.forecast-card{border:1px solid var(--line);border-radius:12px;padding:12px;min-width:0}.forecast-card h4{margin:0 0 6px;font-size:14px}.warn{border-left:3px solid #d97706;padding:8px 10px;background:color-mix(in srgb,#d97706 9%,transparent);margin:8px 0}.small{font-size:12px}.hidden{display:none}.analysis-section{margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}.signal-list{display:flex;gap:6px;flex-wrap:wrap}.scenario-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.news-item{padding:10px 0;border-bottom:1px solid var(--line)}.news-item a{color:var(--accent);font-weight:650;text-decoration:none}.news-item a:hover{text-decoration:underline}@media(max-width:700px){.chart-layout{grid-template-columns:1fr}.chart-side-indicators{grid-template-columns:repeat(3,minmax(0,1fr))}.chart-side-indicators .metric .value{font-size:15px}}@media(max-width:600px){.scenario-grid{grid-template-columns:1fr}.chart-side-indicators{grid-template-columns:1fr}}@media(max-width:850px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.twocol{grid-template-columns:1fr}.details{grid-template-columns:repeat(2,minmax(0,1fr))}body{padding:12px}}@media(max-width:480px){.details{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><header><div><div class="muted">PNDCC · CAPITAL MARKETS · ISOLATED CANDIDATE v2.8.0</div><h1>Blue Chips Stock Analysis</h1><div class="muted">Canadian-listed instrument verification · Technicals · Fundamentals · Data-integrity screen</div></div><div><button id="refresh">Refresh data</button></div></header>
<div class="warn"><strong>Listing verification is strict:</strong> the app tests Canadian exchange-suffixed symbols only (.TO, .V, .NE, .CN). It will not silently substitute a US listing. Buying through a Canadian brokerage does not itself establish the exchange where an instrument trades.</div>
<section class="panel"><h2>Coverage overview</h2><div class="grid"><div class="kpi"><span class="muted">Requested instruments</span><strong id="requested">39</strong></div><div class="kpi"><span class="muted">Canadian price histories verified</span><strong id="priceok">—</strong></div><div class="kpi"><span class="muted">Price data unresolved</span><strong id="pricebad">—</strong></div><div class="kpi"><span class="muted">Fundamentals available</span><strong id="fundok">—</strong></div></div><p id="stamp" class="muted small">Loading coverage report…</p></section>
<section class="panel"><h2>Blue Chips universe</h2><div class="tools"><input id="search" placeholder="Search symbol, company or industry…" aria-label="Search stocks"><select id="statusFilter"><option value="all">All statuses</option><option value="available">Canadian listing verified</option><option value="unresolved">Unresolved</option></select><select id="industryFilter"><option value="all">All industries</option></select><button id="export">Export coverage CSV</button></div><div class="tablewrap"><table><thead><tr><th>Symbol</th><th>Company</th><th>Industry</th><th>Provider symbol</th><th>Exchange</th><th>Currency</th><th>Latest price</th><th>1Y return</th><th>2Y return</th><th>3Y return</th><th>5Y return</th><th>52-Week High</th><th>52-Week Low</th><th>Diff from Current Price (High)</th><th>Diff from Current Price (Low)</th><th>Volatility</th><th>RSI14</th><th>Short-term condition</th><th>Valuation screen</th><th>Trend</th><th>Fundamentals</th><th>Status</th></tr></thead><tbody id="rows"><tr><td colspan="22">Loading…</td></tr></tbody></table></div><p class="muted small">Select a row to inspect its technical and fundamental metrics. Returns use date-aligned adjusted closes; unavailable periods show -. 52-week values require a full trailing year of listing history.</p></section>
<section class="panel" id="detail"><h2>Stock detail</h2><p class="muted">Choose a verified stock above. Fundamentals are shown only when the provider returns usable values.</p></section>
<section class="panel"><h2>Methodology & limitations</h2><ul id="notes"></ul><p class="muted small">The dashboard retrieves up to five years of daily adjusted closes; 1Y/2Y/3Y/5Y returns use calendar-year lookbacks matched to nearby trading sessions. Unavailable return periods display -. They are not filled from a different ticker. The valuation screen is a PEG-only heuristic (Potential value / Moderate / Premium) and is not a DCF or proof of overpricing/underpricing. Annualized risk statistics use the available history, with rolling volatility calculated over its stated window. Sharpe/Sortino assume a 4% annual risk-free rate. Historical VaR/CVaR and scenario ranges are estimates based on the observed sample and are not guarantees. RSI/MACD/SMA/EMA are descriptive signals, not forecasts. Exchange suffix, provider exchange metadata, exact provider symbol, and CAD currency must agree for a listing to pass. Fundamental values are provider-reported, may use different source currencies and reporting periods, and should not be compared without checking source metadata.</p></section>
<script>
let DATA=null;const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const num=(v,d=2)=>v===null||v===undefined||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:d});const money=(v,c='CAD')=>v===null||v===undefined?'—':(c==='CAD'?'CA$':c==='USD'?'US$':c+' ')+num(v,2);const pct=v=>v===null||v===undefined?'—':(v>0?'+':'')+num(v,2)+'%';const signedNum=(v,d=2)=>v===null||v===undefined||!Number.isFinite(Number(v))?'—':(Number(v)>0?'+':'')+num(v,d);const sign=v=>v>0?'positive':v<0?'negative':'';
function returnCell(p,years){const key='change_'+years+'y_pct',v=p[key],anchor=p['return_'+years+'y_anchor_date'],gap=p['return_'+years+'y_anchor_gap_days'];if(v!==null&&v!==undefined&&Number.isFinite(Number(v))){const detail=anchor?`Reference close: ${anchor}; ${gap} day(s) from target date.`:'Reference trading date recorded by the calculation.';return `<span class="${sign(Number(v))}" title="${years}-year adjusted-price return. ${esc(detail)} History begins ${esc(p.history_start_date||'unknown')}.">${pct(Number(v))}</span>`;}const observed=Number(p.history_years);const reason=Number.isFinite(observed)&&observed+0.02<years?`Insufficient listing history: ${observed.toFixed(2)} years observed; ${years} years required.`:`No reliable trading-session anchor near the ${years}-year lookback date. Closest candidate: ${anchor||'none'}; gap: ${gap===null||gap===undefined?'unknown':gap+' day(s)'}.`;return `<span class="muted" title="${esc(reason)}">-</span>`;}
function technicalCondition(p){const v=Number(p.rsi14);if(p.rsi14===null||p.rsi14===undefined||!Number.isFinite(v))return 'Unavailable';return v>=70?'Overbought':v<=30?'Oversold':'Neutral';}
function valuationScreen(f){const v=Number(f.peg_ratio);if(f.peg_ratio===null||f.peg_ratio===undefined||!Number.isFinite(v))return 'Unavailable';if(v<=0)return 'Not meaningful';if(v<1)return 'Potential value';if(v<=2)return 'Moderate';return 'Premium';}
function screenClass(value){return value==='Overbought'?'overbought':value==='Oversold'?'oversold':value==='Potential value'?'positive':value==='Premium'?'negative':'';}function fmtLarge(v,c=''){if(v===null||v===undefined)return '—';let n=Number(v);if(!Number.isFinite(n))return '—';let a=Math.abs(n),body=(a>=1e12?(a/1e12).toFixed(2)+'T':a>=1e9?(a/1e9).toFixed(2)+'B':a>=1e6?(a/1e6).toFixed(2)+'M':a>=1e3?(a/1e3).toFixed(2)+'K':num(n,2));let prefix=c==='USD'?'US$':c==='CAD'?'CA$':c==='EUR'?'€':c==='CHF'?'CHF ':c==='DKK'?'DKK ':'';return (n<0?'-':'')+prefix+body}
async function load(force=false){$('rows').innerHTML='<tr><td colspan="22">Loading and verifying Canadian listings… this can take a little while.</td></tr>';$('stamp').textContent='Requesting provider coverage report…';try{let r=await fetch('/api/stock-analysis'+(force?'?refresh=1':''));let d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');DATA=d;render();}catch(e){$('rows').innerHTML='<tr><td colspan="22">Load failed: '+esc(e.message)+'</td></tr>';$('stamp').textContent='Check that the isolated Python server is still running.'}}
function render(){let s=DATA.summary;$('requested').textContent=s.requested;$('priceok').textContent=s.price_available;$('pricebad').textContent=s.price_unavailable;$('fundok').textContent=s.fundamentals_available;$('stamp').textContent='Retrieved: '+s.retrieved_at+' · Cache '+s.cache_seconds+' seconds · Provider data can be delayed or incomplete';$('notes').innerHTML=DATA.notes.map(x=>'<li>'+esc(x)+'</li>').join('');let inds=[...new Set(DATA.assets.map(a=>a.industry))].sort();$('industryFilter').innerHTML='<option value="all">All industries</option>'+inds.map(x=>'<option value="'+esc(x)+'">'+esc(shortIndustry(x))+'</option>').join('');renderRows();}
function renderRows(){let q=$('search').value.toLowerCase(),st=$('statusFilter').value,ind=$('industryFilter').value;let list=DATA.assets.filter(a=>(st==='all'||(st==='available'?a.status==='available':a.status!=='available'))&&(ind==='all'||a.industry===ind)&&[a.symbol,a.name,a.industry,a.provider_symbol||'',a.listing_exchange||''].join(' ').toLowerCase().includes(q));$('rows').innerHTML=list.map(a=>{let p=a.price_data||{},f=a.fundamentals||{},tc=a.status==='available'?technicalCondition(p):'Unavailable',vs=a.status==='available'?valuationScreen(f):'Unavailable';const current=Number(p.price),high=Number(p.fifty_two_week_high),low=Number(p.fifty_two_week_low),rangeOK=a.status==='available'&&Number.isFinite(current)&&current>0&&p.fifty_two_week_high!==null&&p.fifty_two_week_high!==undefined&&p.fifty_two_week_low!==null&&p.fifty_two_week_low!==undefined&&Number.isFinite(high)&&Number.isFinite(low)&&high>low;const highDiff=rangeOK?(high-current)/current*100:null,lowDiff=rangeOK?(low-current)/current*100:null;return `<tr data-id="${esc(a.symbol)}"><td><strong>${esc(a.symbol)}</strong></td><td>${esc(a.name)}</td><td>${esc(shortIndustry(a.industry))}</td><td>${esc(a.provider_symbol||'—')}</td><td>${esc(a.listing_exchange||'—')}</td><td>${esc(a.currency||'—')}</td><td>${a.status==='available'?money(p.price,a.currency):'—'}</td><td>${a.status==='available'?returnCell(p,1):'-'}</td><td>${a.status==='available'?returnCell(p,2):'-'}</td><td>${a.status==='available'?returnCell(p,3):'-'}</td><td>${a.status==='available'?returnCell(p,5):'-'}</td><td>${rangeOK?money(high,a.currency):'-'}</td><td>${rangeOK?money(low,a.currency):'-'}</td><td class="${sign(highDiff)}">${rangeOK?pct(highDiff):'-'}</td><td class="${sign(lowDiff)}">${rangeOK?pct(lowDiff):'-'}</td><td class="${sign(p.annualized_volatility_pct)}">${a.status==='available'?pct(p.annualized_volatility_pct):'—'}</td><td>${a.status==='available'?num(p.rsi14):'—'}</td><td class="${screenClass(tc)}" title="RSI(14): below 30 oversold; above 70 overbought">${esc(tc)}</td><td class="${screenClass(vs)}" title="Heuristic screen based on provider PEG only; not an intrinsic-value estimate">${esc(vs)}</td><td>${esc(p.trend_status||'—')}</td><td>${esc(f.status==='available'?'Available':'Unavailable')}</td><td><span class="badge">${esc(a.status==='available'?'Verified':'Unresolved')}</span></td></tr>`}).join('')||'<tr><td colspan="22">No matches.</td></tr>';document.querySelectorAll('tr[data-id]').forEach(tr=>tr.addEventListener('click',()=>showDetail(tr.dataset.id)));}
function shortIndustry(s){return String(s||'').replace('Semiconductors & Semiconductor Equipment','Semiconductors').replace('Computers, Phones & Household Electronics','Consumer Technology').replace('Machinery, Equipment & Components','Machinery & Equipment').replace('Communications & Networking','Networking').replace('Automobiles & Auto Parts','Automobiles').replace('Telecommunications Services','Telecom Services')}
function smartWord(label,value){if(value===null||value===undefined||String(value).trim()==='—')return 'Unavailable';let l=String(label).toLowerCase(),v=String(value).trim();if(l.includes('bearish scenario'))return v.startsWith('-')?'Downside':'Resilient';if(l.includes('bullish scenario'))return v.startsWith('+')?'Upside':'Cautious';if(l.includes('base scenario'))return v.startsWith('+')?'Positive':v.startsWith('-')?'Negative':'Stable';if(l.includes('rsi')){let n=parseFloat(v);return n>=70?'Overbought':n<=30?'Oversold':'Neutral'}if(l.includes('macd'))return v.startsWith('-')?'Bearish':v.startsWith('+')?'Bullish':'Neutral';if(l.includes('volatility')||l.includes('deviation')){let n=parseFloat(v);return Number.isFinite(n)?(n>=60?'Elevated':n>=30?'High':'Moderate'):'Observed'}if(l.includes('sharpe')||l.includes('sortino'))return v.startsWith('-')?'Weak':v.startsWith('+')?'Favorable':'Neutral';if(l.includes('drawdown')||l.includes('var')||l.includes('cvar'))return v.startsWith('-')?'Loss':(l.includes('var')||l.includes('cvar'))?'Risk':'Contained';if(l.includes('p/e')||l.includes('peg')){let n=parseFloat(v);return !Number.isFinite(n)?'Reported':n<=1?'Low':n<=2?'Moderate':'High'}if(l.includes('earnings date'))return 'Scheduled';if(l.includes('return')||l.includes('growth')||l.includes('margin')||l.includes('yield')||l.includes('cagr')||l.includes('scenario'))return v.startsWith('+')?'Gain':v.startsWith('-')?'Loss':'Flat';if(v.startsWith('+'))return 'Positive';if(v.startsWith('-'))return 'Negative';return 'Reported'}
function metric(label,value,comment){let val=String(value??'—'),cls=val.trim().startsWith('+')?'positive':val.trim().startsWith('-')?'negative':'';let word=comment||smartWord(label,value);return `<div class="metric" title="${esc(label)}: ${esc(value)} — ${esc(word)}"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}<span class="comment">${esc(word)}</span></div></div>`}
function dateLabel(iso){const d=new Date(iso+'T12:00:00');if(!Number.isFinite(d.getTime()))return iso;return String(d.getDate()).padStart(2,'0')+'-'+d.toLocaleString('en-CA',{month:'short'}).toUpperCase()+'-'+d.getFullYear();}
function chartSvg(bars,currency='CAD'){
 if(!bars||bars.length<2)return '<p class="muted">No price chart available.</p>';
 bars=bars.slice(-252);const w=900,h=300,p=58;
 const priceValues=bars.map(b=>Number(b.adjclose));
 const sma=(n)=>bars.map((b,i)=>i+1<n?null:bars.slice(i-n+1,i+1).reduce((z,v)=>z+Number(v.adjclose),0)/n);
 const s20=sma(20),s50=sma(50);
 const meanX=(bars.length-1)/2,meanY=priceValues.reduce((a,b)=>a+b,0)/priceValues.length;
 const den=priceValues.reduce((a,v,i)=>a+(i-meanX)*(i-meanX),0);
 const slope=den?priceValues.reduce((a,v,i)=>a+(i-meanX)*(v-meanY),0)/den:0;
 const trendValues=priceValues.map((_,i)=>meanY+slope*(i-meanX));
 // Scale to every visible series so trend/SMA endpoints cannot be clipped outside the plot.
 const allValues=[...priceValues,...s20,...s50,...trendValues].filter(Number.isFinite);
 let lo=Math.min(...allValues),hi=Math.max(...allValues);if(hi===lo){hi+=1;lo-=1}
 const pad=(hi-lo)*.08;lo-=pad;hi+=pad;
 const x=i=>p+i*(w-2*p)/(bars.length-1),y=v=>h-p-(v-lo)*(h-2*p)/(hi-lo);
 const pathFor=values=>values.map((v,i)=>v===null||v===undefined||!Number.isFinite(Number(v))?null:`${i?'L':'M'}${x(i)},${y(Number(v))}`).filter(Boolean).join(' ');
 const pricePath=pathFor(priceValues),s20Path=pathFor(s20),s50Path=pathFor(s50);
 const trend=`M${x(0)},${y(trendValues[0])} L${x(bars.length-1)},${y(trendValues[trendValues.length-1])}`;
 const ticks=Array.from({length:6},(_,k)=>{let i=Math.round(k*(bars.length-1)/5);return {i,label:dateLabel(bars[i].date)}});
 const tickSvg=ticks.map((t,k)=>`<line x1="${x(t.i)}" y1="${h-p}" x2="${x(t.i)}" y2="${h-p+5}" stroke="currentColor" opacity=".5"/><text x="${x(t.i)}" y="${h-10}" text-anchor="${k===0?'start':k===5?'end':'middle'}" font-size="12" font-weight="600" fill="currentColor">${esc(t.label)}</text>`).join('');
 return `<div class="chart-wrap" title="One-year adjusted closing price, linear trend line, SMA20 and SMA50."><div class="chart-help"><strong>Historical Price & Trend</strong> — adjusted close, linear trend line, SMA20 and SMA50. Trend-line endpoints: ${esc(money(trendValues[0],currency))} → ${esc(money(trendValues[trendValues.length-1],currency))}.</div><svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="One-year adjusted price, linear trend line, SMA20, SMA50, and six date labels"><title>One-Year Price Trend, Trend Line and Moving Averages</title><desc>Historical daily adjusted prices with linear trend line and 20-day and 50-day simple moving averages. Exactly six date labels are shown on the x-axis.</desc><line x1="${p}" y1="${h-p}" x2="${w-p}" y2="${h-p}" stroke="currentColor" opacity=".25"/>${ticks.map(t=>`<line x1="${x(t.i)}" y1="${p}" x2="${x(t.i)}" y2="${h-p}" stroke="currentColor" opacity=".08"/>`).join('')}<path d="${pricePath}" fill="none" stroke="#2563eb" stroke-width="2.5"/><path d="${trend}" fill="none" stroke="#7c3aed" stroke-width="2" stroke-dasharray="6 4"/><path d="${s20Path}" fill="none" stroke="#16a34a" stroke-width="2"/><path d="${s50Path}" fill="none" stroke="#d97706" stroke-width="2"/>${tickSvg}<text x="${p}" y="18" font-size="12" font-weight="600" fill="currentColor">${esc(num(hi-pad,2))}</text><text x="${w-p}" y="18" text-anchor="end" font-size="12" fill="#2563eb">Price</text><text x="${w-p-55}" y="18" text-anchor="end" font-size="12" fill="#7c3aed">Trend</text><text x="${w-p-115}" y="18" text-anchor="end" font-size="12" fill="#16a34a">SMA20</text><text x="${w-p-175}" y="18" text-anchor="end" font-size="12" fill="#d97706">SMA50</text></svg></div>`;
}
function forecastSvg(bars,months){if(!bars||bars.length<30)return '<p class="muted">Not enough history to estimate a forecast range.</p>';const prices=bars.map(b=>Number(b.adjclose)).filter(v=>Number.isFinite(v)&&v>0),logs=[];for(let i=1;i<prices.length;i++)logs.push(Math.log(prices[i]/prices[i-1]));if(logs.length<20)return '<p class="muted">Not enough valid daily returns.</p>';const n=months*21,mu=logs.reduce((a,b)=>a+b,0)/logs.length,variance=logs.reduce((a,b)=>a+(b-mu)*(b-mu),0)/(logs.length-1),sigma=Math.sqrt(Math.max(0,variance)),last=prices[prices.length-1],steps=Array.from({length:n+1},(_,i)=>({i,mid:last*Math.exp(mu*i),low:last*Math.exp(mu*i-1.28*sigma*Math.sqrt(i)),high:last*Math.exp(mu*i+1.28*sigma*Math.sqrt(i))}));let w=720,h=230,p=42,vals=steps.flatMap(v=>[v.low,v.mid,v.high]),lo=Math.min(...vals),hi=Math.max(...vals);if(hi===lo)hi=lo+1;const x=i=>p+i*(w-2*p)/n,y=v=>h-p-(v-lo)*(h-2*p)/(hi-lo),path=k=>steps.map((v,i)=>`${i?'L':'M'}${x(i)},${y(v[k])}`).join(' '),start=bars[bars.length-1].date,baseDate=new Date(start+'T12:00:00'),targetMonth=baseDate.getMonth()+months,targetYear=baseDate.getFullYear()+Math.floor(targetMonth/12),monthIndex=((targetMonth%12)+12)%12,day=Math.min(baseDate.getDate(),new Date(targetYear,monthIndex+1,0).getDate()),end=new Date(targetYear,monthIndex,day,12),endDate=end.getFullYear()+'-'+String(end.getMonth()+1).padStart(2,'0')+'-'+String(end.getDate()).padStart(2,'0'),forecastTicks=Array.from({length:6},(_,k)=>{const idx=Math.round(k*n/5),d=new Date(baseDate.getTime()+(end.getTime()-baseDate.getTime())*k/5),iso=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');return {idx,iso}}),tickSvg=forecastTicks.map((t,k)=>`<text x="${x(t.idx)}" y="${h-8}" text-anchor="${k===0?'start':k===5?'end':'middle'}" font-size="10" fill="currentColor">${esc(dateLabel(t.iso))}</text>`).join('');return `<div class="forecast-card"><h4>${months}-Month Illustrative Forecast</h4><p class="forecast-summary">Central path: ${money(steps[n].mid,'CAD')} · Illustrative range: ${money(steps[n].low,'CAD')}–${money(steps[n].high,'CAD')}</p><svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${months}-month illustrative forecast range"><title>${months}-Month Historical-Distribution Scenario</title><desc>Modelled central path and 1.28 standard-deviation range using historical daily log returns. Not a guaranteed forecast.</desc><line x1="${p}" y1="${h-p}" x2="${w-p}" y2="${h-p}" stroke="currentColor" opacity=".25"/><path d="${path('high')}" fill="none" stroke="#d97706" stroke-width="1.5" stroke-dasharray="5 4"/><path d="${path('low')}" fill="none" stroke="#d97706" stroke-width="1.5" stroke-dasharray="5 4"/><path d="${path('mid')}" fill="none" stroke="#2563eb" stroke-width="2.4"/>${tickSvg}<text x="${p}" y="14" font-size="11" fill="currentColor">${esc(money(hi,'CAD'))}</text><text x="${w-p}" y="14" text-anchor="end" font-size="11" fill="currentColor">${esc(money(lo,'CAD'))}</text></svg><p class="muted small">Central path and dashed illustrative range use historical daily log-return drift and volatility; these are scenarios, not guaranteed price targets.</p></div>`}
function renderForecasts(sym){const target=$('forecastCharts');if(!target)return;const a=DATA?.assets?.find(x=>x.symbol===sym),bars=a?.price_data?.bars||[];const selected=[...document.querySelectorAll('.forecast-period:checked')].map(x=>Number(x.value)).filter(x=>[1,3,6].includes(x));target.innerHTML=selected.length?selected.map(m=>forecastSvg(bars,m)).join(''):'<p class="muted">Select at least one forecast horizon.</p>';document.querySelectorAll('.forecast-period').forEach(c=>c.onchange=()=>{renderForecasts(sym);document.querySelectorAll('#detail svg.chart').forEach(s=>{s.dataset.drawReady='0';s.parentElement.querySelectorAll(':scope > .chart-draw-overlay').forEach(o=>o.remove())});initChartDrawTools(sym)});}
function drawdownSvg(bars){if(!bars||bars.length<2)return '<p class="muted">No drawdown chart available.</p>';let peak=0,dds=bars.map(b=>{let v=Number(b.adjclose);peak=Math.max(peak,v);return peak>0?(v/peak-1)*100:0}),w=900,h=190,p=54,lo=Math.min(-1,...dds),y=v=>12+(v/lo)*(h-35);let x=i=>p+i*(w-2*p)/(dds.length-1),points=dds.map((v,i)=>`${x(i)},${y(v)}`).join(' '),ticks=Array.from({length:6},(_,k)=>{let i=Math.round(k*(bars.length-1)/5);return {i,label:dateLabel(bars[i].date)}}),labels=ticks.map((t,k)=>`<text x="${x(t.i)}" y="${h-8}" text-anchor="${k===0?'start':k===5?'end':'middle'}" font-size="11" fill="currentColor">${esc(t.label)}</text>`).join('');let area=`${p},12 ${points} ${w-p},12`;return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Historical drawdown chart with six date labels"><line x1="${p}" y1="12" x2="${w-p}" y2="12" stroke="currentColor" opacity=".45"/><polygon points="${area}" fill="#dc2626" opacity=".18"/><polyline points="${points}" fill="none" stroke="#dc2626" stroke-width="2.8"/>${labels}<text x="${p}" y="${h-28}" font-size="13" font-weight="700" fill="#dc2626">${esc(num(lo,1))}% maximum observed drawdown</text></svg>`}
function supportResistanceSvg(bars,currency='CAD'){
 if(!bars||bars.length<30)return '<p class="muted">Insufficient historical observations to estimate zones.</p>';
 const recent=bars.slice(-252),values=recent.map(b=>Number(b.adjclose)).filter(v=>Number.isFinite(v)&&v>0);
 if(values.length<30)return '<p class="muted">Insufficient valid prices to estimate zones.</p>';
 const sorted=[...values].sort((a,b)=>a-b),quantile=q=>sorted[Math.max(0,Math.min(sorted.length-1,Math.round((sorted.length-1)*q)))];
 // Use 10th/20th and 80th/90th percentiles as broad candidate zones, not precise signals.
 const supLo=quantile(.10),supHi=quantile(.20),resLo=quantile(.80),resHi=quantile(.90),last=values[values.length-1];
 const w=900,h=220,p=64,lo=Math.min(...values,supLo),hi=Math.max(...values,resHi);const y=v=>h-32-(v-lo)/(Math.max(hi-lo,1e-9))*(h-64),x=i=>p+i*(w-2*p)/(values.length-1);
 const path=values.map((v,i)=>`${i?'L':'M'}${x(i)},${y(v)}`).join(' ');
 return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Estimated support and resistance zones"><rect x="${p}" y="${y(supHi)}" width="${w-2*p}" height="${Math.max(2,y(supLo)-y(supHi))}" fill="#16a34a" opacity=".16"/><rect x="${p}" y="${y(resHi)}" width="${w-2*p}" height="${Math.max(2,y(resLo)-y(resHi))}" fill="#dc2626" opacity=".16"/><path d="${path}" fill="none" stroke="#2563eb" stroke-width="2"/><line x1="${p}" y1="${y(supLo)}" x2="${w-p}" y2="${y(supLo)}" stroke="#16a34a" stroke-dasharray="5 4"/><line x1="${p}" y1="${y(supHi)}" x2="${w-p}" y2="${y(supHi)}" stroke="#16a34a" stroke-dasharray="5 4"/><line x1="${p}" y1="${y(resLo)}" x2="${w-p}" y2="${y(resLo)}" stroke="#dc2626" stroke-dasharray="5 4"/><line x1="${p}" y1="${y(resHi)}" x2="${w-p}" y2="${y(resHi)}" stroke="#dc2626" stroke-dasharray="5 4"/><text x="${p+6}" y="${Math.max(14,y(supHi)-5)}" fill="#16a34a" font-size="13" font-weight="700">Support zone ${esc(money(supLo,currency))}–${esc(money(supHi,currency))}</text><text x="${p+6}" y="${Math.max(14,y(resHi)-5)}" fill="#dc2626" font-size="13" font-weight="700">Resistance zone ${esc(money(resLo,currency))}–${esc(money(resHi,currency))}</text><text x="${w-p}" y="16" text-anchor="end" fill="currentColor" font-size="12">Latest ${esc(money(last,currency))}</text></svg>`;
}
const drawingStore=new Map();let activeDrawingSymbol='';let selectedDrawing=null;
function drawingControlsHtml(){return `<div class="drawing-toolbar" id="drawingToolbar" aria-label="Chart drawing tools"><strong>Chart drawings</strong><label>Tool <select id="drawMode"><option value="none">Off</option><option value="select">Select / move</option><option value="hline">Horizontal line</option><option value="vline">Vertical line</option><option value="line">Diagonal line</option><option value="rect">Rectangle</option><option value="circle">Circle</option><option value="erase">Erase / delete</option></select></label><label>Color <input id="drawColor" type="color" value="#7c3aed"></label><label>Thickness <select id="drawWidth"><option value="1">1 px</option><option value="2" selected>2 px</option><option value="3">3 px</option><option value="4">4 px</option><option value="6">6 px</option></select></label><button type="button" id="deleteDrawing">Delete selected</button><button type="button" id="clearDrawings">Clear this stock's drawings</button><span class="muted small">Drawings stay for this browser session and remain aligned when the chart resizes.</span></div>`}
function initChartDrawTools(symbol){
 activeDrawingSymbol=symbol;
 const modeEl=document.getElementById('drawMode'),colorEl=document.getElementById('drawColor'),widthEl=document.getElementById('drawWidth');if(!modeEl||!colorEl||!widthEl)return;
 const currentMode=()=>modeEl.value,keyFor=(svg,i)=>symbol+'|'+(svg.getAttribute('aria-label')||svg.querySelector('title')?.textContent||('chart-'+i));
 document.querySelectorAll('#detail svg.chart').forEach((svg,i)=>{
  if(svg.dataset.drawReady==='1')return;
  const vb=(svg.getAttribute('viewBox')||'0 0 900 300').split(/\s+/).map(Number),[vx,vy,vw,vh]=vb.length===4?vb:[0,0,900,300];
  const parent=svg.parentElement;if(!parent)return;let wrap=parent;
  if(!parent.classList.contains('chart-interactive')){wrap=document.createElement('div');wrap.className='chart-interactive';parent.insertBefore(wrap,svg);wrap.appendChild(svg)}
  const key=keyFor(svg,i); // Define the key before assigning it to the overlay (the old order threw a TDZ error).
  wrap.querySelectorAll(':scope > .chart-draw-overlay').forEach(old=>old.remove());
  const overlay=document.createElementNS('http://www.w3.org/2000/svg','svg');overlay.setAttribute('class','chart-draw-overlay');overlay.dataset.drawingKey=key;overlay.setAttribute('viewBox',`${vx} ${vy} ${vw} ${vh}`);overlay.setAttribute('preserveAspectRatio',svg.getAttribute('preserveAspectRatio')||'xMidYMid meet');overlay.setAttribute('aria-label','Editable chart drawing overlay');overlay.dataset.mode=currentMode();wrap.appendChild(overlay);svg.dataset.drawReady='1';
  if(!drawingStore.has(key))drawingStore.set(key,[]);const shapes=drawingStore.get(key);let selected=-1,drag=null,start=null,preview=null;
  const pos=e=>{const pt=overlay.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;const m=overlay.getScreenCTM();if(!m)return {x:0,y:0};const q=pt.matrixTransform(m.inverse());return {x:q.x,y:q.y}};
  const mk=(shape,previewing=false)=>{const isLine=['line','hline','vline'].includes(shape.type),el=document.createElementNS('http://www.w3.org/2000/svg',isLine?'line':shape.type==='rect'?'rect':'ellipse');el.classList.add('draw-shape');if(!previewing)el.dataset.drawIndex=String(shapes.indexOf(shape));el.setAttribute('stroke',shape.color);el.setAttribute('stroke-width',shape.width);el.setAttribute('fill','none');el.setAttribute('vector-effect','non-scaling-stroke');el.setAttribute('pointer-events',isLine?'stroke':'all');if(shape.type==='hline'){el.setAttribute('x1',vx);el.setAttribute('x2',vx+vw);el.setAttribute('y1',shape.y1);el.setAttribute('y2',shape.y1)}else if(shape.type==='vline'){el.setAttribute('x1',shape.x1);el.setAttribute('x2',shape.x1);el.setAttribute('y1',vy);el.setAttribute('y2',vy+vh)}else if(shape.type==='line'){el.setAttribute('x1',shape.x1);el.setAttribute('y1',shape.y1);el.setAttribute('x2',shape.x2);el.setAttribute('y2',shape.y2)}else if(shape.type==='rect'){el.setAttribute('x',Math.min(shape.x1,shape.x2));el.setAttribute('y',Math.min(shape.y1,shape.y2));el.setAttribute('width',Math.abs(shape.x2-shape.x1));el.setAttribute('height',Math.abs(shape.y2-shape.y1));el.setAttribute('fill',shape.color);el.setAttribute('fill-opacity','.08')}else if(shape.type==='circle'){el.setAttribute('cx',(shape.x1+shape.x2)/2);el.setAttribute('cy',(shape.y1+shape.y2)/2);el.setAttribute('rx',Math.abs(shape.x2-shape.x1)/2);el.setAttribute('ry',Math.abs(shape.y2-shape.y1)/2);el.setAttribute('fill',shape.color);el.setAttribute('fill-opacity','.08')}if(!previewing&&selected===shapes.indexOf(shape))el.classList.add('selected');return el};
  const render=()=>{overlay.replaceChildren();shapes.forEach(sh=>overlay.appendChild(mk(sh)));if(preview)overlay.appendChild(mk(preview,true));overlay.dataset.mode=currentMode();overlay.style.pointerEvents=currentMode()==='none'?'none':'auto'};
  overlay.addEventListener('pointerdown',e=>{const p=pos(e),mode=currentMode(),hit=e.target.closest('.draw-shape');if(mode==='none')return;if(mode==='erase'){if(hit){const erased=Number(hit.dataset.drawIndex);if(Number.isInteger(erased)&&erased>=0&&erased<shapes.length)shapes.splice(erased,1);selected=-1;selectedDrawing=null;render()}return}if(mode==='select'&&hit){selected=Number(hit.dataset.drawIndex);if(!Number.isInteger(selected)||!shapes[selected])return;selectedDrawing={key,index:selected};drag={start:p,shape:JSON.parse(JSON.stringify(shapes[selected]))};overlay.setPointerCapture(e.pointerId);render();return}if(mode==='select'){selected=-1;selectedDrawing=null;render();return}start=p;preview={type:mode,x1:p.x,y1:p.y,x2:p.x,y2:p.y,color:colorEl.value,width:Number(widthEl.value)};overlay.setPointerCapture(e.pointerId);render()});
  overlay.addEventListener('pointermove',e=>{const p=pos(e);if(drag){const dx=p.x-drag.start.x,dy=p.y-drag.start.y,sh=shapes[selected],orig=drag.shape;if(!sh)return;if(sh.type==='hline')sh.y1=orig.y1+dy;else if(sh.type==='vline')sh.x1=orig.x1+dx;else{sh.x1=orig.x1+dx;sh.y1=orig.y1+dy;sh.x2=orig.x2+dx;sh.y2=orig.y2+dy}render();return}if(start&&preview){preview.x2=p.x;preview.y2=p.y;if(preview.type==='hline')preview.y1=preview.y2=p.y;if(preview.type==='vline')preview.x1=preview.x2=p.x;render()}});
  const finish=()=>{if(drag){drag=null;return}if(start&&preview){if(Math.hypot(preview.x2-preview.x1,preview.y2-preview.y1)>2){shapes.push({...preview});selected=shapes.length-1;selectedDrawing={key,index:selected}}preview=null;start=null;render()}};
  overlay.addEventListener('pointerup',finish);overlay.addEventListener('pointercancel',finish);render();
 });
 modeEl.onchange=()=>document.querySelectorAll('.chart-draw-overlay').forEach(o=>{o.dataset.mode=modeEl.value;o.style.pointerEvents=modeEl.value==='none'?'none':'auto'});
 colorEl.onchange=()=>{if(!selectedDrawing)return;const arr=drawingStore.get(selectedDrawing.key)||[],shape=arr[selectedDrawing.index];if(shape){shape.color=colorEl.value;const overlay=[...document.querySelectorAll('.chart-draw-overlay')].find(o=>o.dataset.drawingKey===selectedDrawing.key);if(overlay){const el=overlay.querySelector(`[data-draw-index="${selectedDrawing.index}"]`);if(el)el.setAttribute('stroke',colorEl.value);if(el&&(shape.type==='rect'||shape.type==='circle'))el.setAttribute('fill',colorEl.value)}}};
 widthEl.onchange=()=>{if(!selectedDrawing)return;const arr=drawingStore.get(selectedDrawing.key)||[],shape=arr[selectedDrawing.index];if(shape){shape.width=Number(widthEl.value);const overlay=[...document.querySelectorAll('.chart-draw-overlay')].find(o=>o.dataset.drawingKey===selectedDrawing.key);const el=overlay?.querySelector(`[data-draw-index="${selectedDrawing.index}"]`);if(el)el.setAttribute('stroke-width',widthEl.value)}};
 document.getElementById('deleteDrawing').onclick=()=>{if(!selectedDrawing)return;const arr=drawingStore.get(selectedDrawing.key)||[];arr.splice(selectedDrawing.index,1);selectedDrawing=null;document.querySelectorAll('#detail svg.chart').forEach(s=>s.dataset.drawReady='0');initChartDrawTools(symbol)};
 document.getElementById('clearDrawings').onclick=()=>{for(const [k,arr] of drawingStore)if(k.startsWith(symbol+'|'))arr.length=0;selectedDrawing=null;document.querySelectorAll('.chart-draw-overlay').forEach(o=>o.replaceChildren())};
}
function makeConclusion(a){let p=a.price_data||{},f=a.fundamentals||{},parts=[];if(a.status!=='available')return 'Price history is unresolved, so a technical conclusion cannot be drawn.';parts.push('Historical trend: '+(p.trend_status||'insufficient history')+'.');if(p.rsi14!==null&&p.rsi14!==undefined)parts.push('RSI(14) is '+num(p.rsi14)+ (p.rsi14>=70?', in the commonly used overbought zone.':p.rsi14<=30?', in the commonly used oversold zone.':', between conventional overbought/oversold thresholds.'));if(p.macd_histogram!==null&&p.macd_histogram!==undefined)parts.push('MACD momentum is '+(p.macd_histogram>0?'positive':p.macd_histogram<0?'negative':'neutral')+'.');if(f.status==='available'){if(f.trailing_pe!==null&&f.trailing_pe!==undefined)parts.push('Trailing P/E: '+num(f.trailing_pe)+'.');if(f.profit_margin_pct!==null&&f.profit_margin_pct!==undefined)parts.push('Profit margin: '+pct(f.profit_margin_pct)+'.');if(f.debt_to_equity!==null&&f.debt_to_equity!==undefined)parts.push('Provider-reported debt/equity: '+num(f.debt_to_equity)+'.');if(f.eps_ttm===null||f.eps_ttm===undefined)parts.push('TTM EPS is not available from the selected fundamentals response.');}else parts.push('Fundamentals unavailable; do not infer company financial health from price data alone.');parts.push('Overall, signals can conflict; review the full metrics, source currency, reporting period, and risk before making a decision.');return parts.join(' ')}
async function loadNews(sym){let target=$('news-'+sym);if(!target)return;try{let r=await fetch('/api/news?symbol='+encodeURIComponent(sym));let d=await r.json();if(!r.ok)throw Error(d.reason||'News request failed');const renderItems=items=>(items||[]).map(n=>'<div class="news-item"><a href="'+esc(n.link)+'" target="_blank" rel="noopener noreferrer">'+esc(n.title)+'</a><div class="muted small">'+esc(n.publisher||'Source not specified')+' · '+esc(n.published_at||'Publication time unavailable')+'</div></div>').join('');let company=d.items||[],context=d.context_items||[];target.innerHTML=(company.length?renderItems(company):'<p class="muted">No clearly company-specific headlines were identified in the current provider results.</p>')+(context.length?'<div class="analysis-section"><h4>Related market context</h4><p class="muted small">These headlines are broader market or industry context, not confirmed company-specific news.</p>'+renderItems(context)+'</div>':'')+'<p class="muted small">'+esc(d.note||'News may be delayed; headlines do not establish causation.')+'</p>';if(!company.length&&!context.length)target.textContent=d.reason||'No recent headlines were returned by the news provider.';}catch(e){target.textContent='News unavailable: '+e.message;}}
function showDetail(sym){let a=DATA.assets.find(x=>x.symbol===sym);if(!a)return;let p=a.price_data||{},f=a.fundamentals||{};let pmetrics=a.status==='available'?[metric('Latest price',money(p.price,a.currency)),metric('1 day',pct(p.change_1d_pct)),metric('1 month',pct(p.change_1m_pct)),metric('3 months',pct(p.change_3m_pct)),metric('6 months',pct(p.change_6m_pct)),metric('1 year return',p.change_1y_pct===null||p.change_1y_pct===undefined?'-':pct(p.change_1y_pct)),metric('2 year return',p.change_2y_pct===null||p.change_2y_pct===undefined?'-':pct(p.change_2y_pct)),metric('3 year return',p.change_3y_pct===null||p.change_3y_pct===undefined?'-':pct(p.change_3y_pct)),metric('5 year return',p.change_5y_pct===null||p.change_5y_pct===undefined?'-':pct(p.change_5y_pct)),metric('Available price-history years',num(p.history_years,2)),metric('History starts',p.history_start_date||'—'),metric('1Y reference date / gap',p.return_1y_anchor_date?`${p.return_1y_anchor_date} / ${p.return_1y_anchor_gap_days} day(s)`:'Unavailable'),metric('2Y reference date / gap',p.return_2y_anchor_date?`${p.return_2y_anchor_date} / ${p.return_2y_anchor_gap_days} day(s)`:'Unavailable'),metric('3Y reference date / gap',p.return_3y_anchor_date?`${p.return_3y_anchor_date} / ${p.return_3y_anchor_gap_days} day(s)`:'Unavailable'),metric('5Y reference date / gap',p.return_5y_anchor_date?`${p.return_5y_anchor_date} / ${p.return_5y_anchor_gap_days} day(s)`:'Unavailable'),metric('Short-term RSI condition',technicalCondition(p)),metric('SMA20',money(p.sma20,a.currency)),metric('SMA50',money(p.sma50,a.currency)),metric('Annualized volatility',pct(p.annualized_volatility_pct)),metric('Maximum drawdown',pct(p.max_drawdown_pct)),metric('52-week high',p.fifty_two_week_high===null||p.fifty_two_week_high===undefined?'-':money(p.fifty_two_week_high,a.currency)),metric('52-week low',p.fifty_two_week_low===null||p.fifty_two_week_low===undefined?'-':money(p.fifty_two_week_low,a.currency)),metric('Latest volume',num(p.volume_latest,0)),metric('Observations',num(p.observations,0))].join(''):'<div class="warn">No verified Canadian price history. '+esc((a.errors||[]).join(' '))+'</div>';let fc=f.currency||'Currency unverified';let fmetrics=f.status==='available'?[metric('Market cap ('+fc+')',fmtLarge(f.market_cap,fc)),metric('Trailing P/E',num(f.trailing_pe)),metric('Forward P/E',num(f.forward_pe)),metric('PEG ratio',num(f.peg_ratio)),metric('Valuation screen',valuationScreen(f)),metric('Next earnings date',f.next_earnings_date||'—'),metric('Price/book',num(f.price_to_book)),metric('EPS (TTM, '+fc+')',money(f.eps_ttm,fc)),metric('Forward EPS ('+fc+')',money(f.eps_forward,fc)),metric('Revenue ('+fc+')',fmtLarge(f.revenue_latest,fc)),metric('Revenue growth',pct(f.revenue_growth_pct)),metric('Net income ('+fc+')',fmtLarge(f.net_income_latest,fc)),metric('Net income growth',pct(f.net_income_growth_pct)),metric('Profit margin',pct(f.profit_margin_pct)),metric('Operating margin',pct(f.operating_margin_pct)),metric('Free cash flow ('+fc+')',fmtLarge(f.free_cash_flow,fc)),metric('Total debt ('+fc+')',fmtLarge(f.total_debt,fc)),metric('Cash ('+fc+')',fmtLarge(f.total_cash,fc)),metric('Debt/equity',num(f.debt_to_equity)),metric('Gross margin',pct(f.gross_margin_pct)),metric('Current ratio',num(f.current_ratio)),metric('Quick ratio',num(f.quick_ratio)),metric('Return on assets',pct(f.return_on_assets_pct)),metric('Return on equity',pct(f.return_on_equity_pct)),metric('Dividend yield',pct(f.dividend_yield_pct)),metric('Payout ratio',pct(f.payout_ratio_pct))].join(''):`<div class="warn"><strong>Fundamentals unavailable.</strong> ${esc(f.reason||'Provider did not return the required metrics.')}</div>`;const current=Number(p.price),high=Number(p.fifty_two_week_high),low=Number(p.fifty_two_week_low),rangeOK=a.status==='available'&&Number.isFinite(current)&&current>0&&p.fifty_two_week_high!==null&&p.fifty_two_week_high!==undefined&&p.fifty_two_week_low!==null&&p.fifty_two_week_low!==undefined&&Number.isFinite(high)&&Number.isFinite(low)&&high>low;const highPct=rangeOK?(high-current)/current*100:null,lowPct=rangeOK?(low-current)/current*100:null,position=rangeOK?Math.max(0,Math.min(100,(current-low)/(high-low)*100)):null,positionLabel=!rangeOK?'-':position>=90?'Near 52W High':position<=10?'Near 52W Low':'Middle';const rangePanel=rangeOK?`<div class="range-card"><strong>52-Week Range</strong><div class="details" style="grid-template-columns:1fr 1fr;margin-top:8px"><div><div class="label muted">52-Week High</div><div class="value positive">${money(high,a.currency)} (${pct(highPct)})</div></div><div><div class="label muted">52-Week Low</div><div class="value negative">${money(low,a.currency)} (${pct(lowPct)})</div></div></div><div class="range-line"><span class="range-marker" style="left:${position}%"></span></div><div class="range-labels"><span>${money(low,a.currency)} · Low</span><span>${money(high,a.currency)} · High</span></div><p><strong>Current position:</strong> ${positionLabel} (${num(position,1)}% of range)</p></div>`:`<div class="range-card"><strong>52-Week Range</strong><p class="muted">- · A full trailing 52-week history is not available.</p></div>`;$('detail').innerHTML=`<div class="detail-head"><div><h2>${esc(a.name)} <span class="muted">(${esc(a.symbol)})</span></h2><p class="muted">${esc(a.industry)} · Provider: ${esc(a.provider_symbol||'unresolved')} · Exchange: ${esc(a.listing_exchange||'unverified')} · Currency: ${esc(a.currency||'—')}</p></div>${rangePanel}</div><div class="twocol"><div><h3>Technical analysis</h3><div class="details">${pmetrics}</div></div><div><h3>Fundamental analysis</h3><div class="details">${fmetrics}</div><p class="muted small">Fundamentals use the issuer source symbol shown in the source metadata, separate from Canadian-listing technical prices. Currency is taken from that fundamentals response; verify it and the reporting period before comparing issuers.</p></div></div><h3 style="margin-top:18px">${esc(a.name)} (${esc(a.symbol)}) — One-Year Price Trend, Linear Trend Line & Moving Averages</h3>${drawingControlsHtml()}<div class="chart-layout"><aside class="chart-side-indicators" aria-label="Key price indicators">${metric('Current Price',a.status==='available'?money(p.price,a.currency):'-','Latest verified Canadian listing price')}${metric('52-Week High',p.fifty_two_week_high!==null&&p.fifty_two_week_high!==undefined?money(p.fifty_two_week_high,a.currency):'-','Full trailing-year history required')}${metric('52-Week Low',p.fifty_two_week_low!==null&&p.fifty_two_week_low!==undefined?money(p.fifty_two_week_low,a.currency):'-','Full trailing-year history required')}</aside>${chartSvg(p.bars,a.currency)}</div><p class="muted small">Latest daily observation: ${esc(p.last_date||'—')}. Prices and moving averages use the verified Canadian listing; unavailable values are shown as “-”.</p><div class="analysis-section"><h3>Illustrative Forecast Scenarios — Select Horizons</h3><p class="muted small">Select one or more horizons. Each selected horizon is shown as a separate chart with a historical-return-based central path and illustrative range, not a guaranteed price target.</p><div class="forecast-controls"><label><input type="checkbox" class="forecast-period" value="1" checked> 1 month</label><label><input type="checkbox" class="forecast-period" value="3"> 3 months</label><label><input type="checkbox" class="forecast-period" value="6"> 6 months</label></div><div id="forecastCharts" class="forecast-grid"></div></div><div class="analysis-section"><h3>Historical Peak-to-Trough Drawdown</h3>${drawdownSvg(p.bars)}<p class="muted small">The red filled area shows percentage decline from each preceding historical peak; this is separate from support/resistance analysis.</p></div><div class="analysis-section"><h3>Potential Support & Resistance Zones</h3>${supportResistanceSvg(p.bars,a.currency)}<p class="muted small">Zones use historical closing-price distribution quantiles as broad technical estimates, not guaranteed barriers or trading instructions.</p></div><div class="analysis-section"><h3>Advanced statistics & technical signals</h3><div class="details">${metric('RSI (14)',num(p.rsi14))}${metric('MACD',signedNum(p.macd,4))}${metric('MACD signal',signedNum(p.macd_signal,4))}${metric('MACD histogram',signedNum(p.macd_histogram,4))}${metric('EMA12',money(p.ema12,a.currency))}${metric('EMA26',money(p.ema26,a.currency))}${metric('Annualized CAGR (approx.)',pct(p.annualized_cagr_approx_pct))}${metric('20-day rolling volatility',pct(p.rolling_volatility_20d_pct))}${metric('60-day rolling volatility',pct(p.rolling_volatility_60d_pct))}${metric('Sharpe ratio',signedNum(p.sharpe_ratio,3))}${metric('Sortino ratio',signedNum(p.sortino_ratio,3))}${metric('Downside deviation (annual)',pct(p.downside_deviation_pct))}${metric('Daily historical VaR 95% loss magnitude',pct(p.historical_var_95_daily_pct))}${metric('Daily historical CVaR 95% loss magnitude',pct(p.historical_cvar_95_daily_pct))}${metric('Current drawdown',pct(p.current_drawdown_pct))}${metric('Skewness',signedNum(p.daily_return_skewness,3))}${metric('Excess kurtosis',signedNum(p.daily_return_excess_kurtosis,3))}${metric('Average volume (20d)',num(p.average_volume_20d,0))}</div><p><strong>Trend:</strong> ${esc(p.trend_status||'Insufficient history')}</p><div class="signal-list">${(p.technical_signals||[]).map(x=>'<span class="badge">'+esc(x)+'</span>').join('')}</div><p class="muted small">Sharpe/Sortino use an assumed 4% annual risk-free rate. VaR/CVaR are historical daily estimates, not worst-case limits.</p></div><div class="analysis-section"><h3>Illustrative 1-month scenarios</h3><p class="muted small">Historical daily log-return drift ±1.28 standard deviations over 21 trading days. These are modelled scenarios, not price targets or investment advice.</p><div class="scenario-grid">${metric('Bearish scenario',pct(p.scenario_1m_bear_pct))}${metric('Base scenario',pct(p.scenario_1m_base_pct))}${metric('Bullish scenario',pct(p.scenario_1m_bull_pct))}</div></div><div class="analysis-section"><h3>Fundamental health & conclusion</h3><p>${esc(makeConclusion(a))}</p><p class="muted small">Conclusion is generated from the displayed historical signals and available provider fundamentals. It is rule-based and may be incomplete when inputs are missing.</p></div><div class="analysis-section"><h3>Recent company news</h3><div id="news-${esc(a.symbol)}" class="muted">Loading recent news…</div></div>`;$('detail').scrollIntoView({behavior:'smooth',block:'start'});renderForecasts(a.symbol);initChartDrawTools(a.symbol);loadNews(a.symbol);}
$('search').addEventListener('input',renderRows);$('statusFilter').addEventListener('change',renderRows);$('industryFilter').addEventListener('change',renderRows);$('refresh').addEventListener('click',()=>load(true));$('export').addEventListener('click',()=>{if(!DATA)return;let cols=['symbol','name','industry','provider_symbol','listing_exchange','currency','status','latest_price','return_1y_pct','return_2y_pct','return_3y_pct','return_5y_pct','return_1y_anchor_date','return_1y_anchor_gap_days','return_2y_anchor_date','return_2y_anchor_gap_days','return_3y_anchor_date','return_3y_anchor_gap_days','return_5y_anchor_date','return_5y_anchor_gap_days','fifty_two_week_high','fifty_two_week_low','fifty_two_week_diff_high_pct','fifty_two_week_diff_low_pct','history_years','history_start_date','volatility_pct','rsi14','short_term_condition','valuation_screen','macd_histogram','sharpe_ratio','sortino_ratio','max_drawdown_pct','trend_status','fundamentals_status','reason'];let rows=DATA.assets.map(a=>[a.symbol,a.name,a.industry,a.provider_symbol||'',a.listing_exchange||'',a.currency||'',a.status,a.price_data?.price??'',a.price_data?.change_1y_pct??'',a.price_data?.change_2y_pct??'',a.price_data?.change_3y_pct??'',a.price_data?.change_5y_pct??'',a.price_data?.return_1y_anchor_date??'',a.price_data?.return_1y_anchor_gap_days??'',a.price_data?.return_2y_anchor_date??'',a.price_data?.return_2y_anchor_gap_days??'',a.price_data?.return_3y_anchor_date??'',a.price_data?.return_3y_anchor_gap_days??'',a.price_data?.return_5y_anchor_date??'',a.price_data?.return_5y_anchor_gap_days??'',a.price_data?.fifty_two_week_high??'',a.price_data?.fifty_two_week_low??'',(a.price_data?.price&&a.price_data?.fifty_two_week_high!==null&&a.price_data?.fifty_two_week_high!==undefined?((a.price_data.fifty_two_week_high-a.price_data.price)/a.price_data.price*100):''),(a.price_data?.price&&a.price_data?.fifty_two_week_low!==null&&a.price_data?.fifty_two_week_low!==undefined?((a.price_data.fifty_two_week_low-a.price_data.price)/a.price_data.price*100):''),a.price_data?.history_years??'',a.price_data?.history_start_date??'',a.price_data?.annualized_volatility_pct??'',a.price_data?.rsi14??'',a.status==='available'?technicalCondition(a.price_data||{}):'Unavailable',a.status==='available'?valuationScreen(a.fundamentals||{}):'Unavailable',a.price_data?.macd_histogram??'',a.price_data?.sharpe_ratio??'',a.price_data?.sortino_ratio??'',a.price_data?.max_drawdown_pct??'',a.price_data?.trend_status??'',a.fundamentals?.status||'',(a.errors||[]).join(' ')]);let csv=[cols,...rows].map(r=>r.map(v=>'"'+String(v??'').replace(/"/g,'""')+'"').join(',')).join('\r\n');let u=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));let x=document.createElement('a');x.href=u;x.download='PNDCC_Blue_Chips_Coverage.csv';x.click();URL.revokeObjectURL(u)});load();
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): print("[blue-chips] " + (fmt % args))
    def send_body(self, code, ctype, body):
        raw=body.encode("utf-8") if isinstance(body,str) else body
        self.send_response(code); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        path=urlparse(self.path).path
        if path in ("/","/stock-analysis"):
            self.send_body(200,"text/html; charset=utf-8",HTML); return
        if path=="/api/stock-analysis":
            try:
                force=parse_qs(urlparse(self.path).query).get("refresh",["0"])[0]=="1"
                self.send_body(200,"application/json; charset=utf-8",json.dumps(_load_all(force),ensure_ascii=False,separators=(",",":")))
            except Exception as e:
                self.send_body(500,"application/json; charset=utf-8",json.dumps({"error":str(e)[:250]}))
            return
        if path=="/api/news":
            try:
                params=parse_qs(urlparse(self.path).query)
                symbol=(params.get("symbol",[""])[0] or "").upper()
                stock=next((x for x in STOCKS if x[0]==symbol),None)
                if not stock:
                    self.send_body(400,"application/json; charset=utf-8",json.dumps({"status":"error","reason":"Unknown stock symbol"})); return
                self.send_body(200,"application/json; charset=utf-8",json.dumps(_news(stock[1],stock[0]),ensure_ascii=False,separators=(",",":")))
            except Exception as e:
                self.send_body(500,"application/json; charset=utf-8",json.dumps({"status":"error","reason":str(e)[:200]}))
            return
        self.send_body(404,"text/plain; charset=utf-8","Not found")

def main():
    print("ISOLATION: Blue Chips Stock Analysis v2.8.1 focused repairs for Tasks 01, 02 and 07; PNDCC master is not imported or started")
    print(f"URL: http://{HOST}:{PORT}/stock-analysis")
    print("Universe: 39 requested instruments; excludes XNDU, APTX and BRK")
    print("v2.8.1: focused return-anchor, historical-chart and interactive drawing repairs; other task areas remain unchanged")
    print("Provider: Yahoo Finance chart and quoteSummary endpoints; internet required")
    print("Note: first coverage refresh may take several minutes due to per-symbol exchange verification. Validate live provider coverage and chart interactions in the browser before production use.")
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    try: server.serve_forever(poll_interval=.4)
    except KeyboardInterrupt: print("\nBlue Chips trial server stopped")
    finally: server.server_close()
if __name__=="__main__": main()
