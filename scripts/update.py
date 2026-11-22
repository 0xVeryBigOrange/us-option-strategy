#!/usr/bin/env python3
"""生成 data.json：Sell Put 抄底候选排行。

数据源：Yahoo Finance（yfinance）。所有数字来自真实行情/财报，拿不到的字段为 null（页面显示“暂无”）。
失败容错：如果本次成功的标的太少（被限流等），不覆盖旧的 data.json，脚本以非零码退出。
"""
import json, math, os, sys, time, datetime as dt
from statistics import NormalDist
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd, yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data.json")
FUND = os.path.join(ROOT, "fundamentals.json")
NOTES = os.path.join(ROOT, "notes.json")
ET, SGT = ZoneInfo("America/New_York"), ZoneInfo("Asia/Singapore")

# 候选池：大盘优质股（盈利、现金流为正为主），按需增减
POOL = ("NVDA GOOGL META MSFT AMZN AAPL NOW CRM ADBE CSCO ASML MU CAT NFLX MELI FTNT AMAT KLAC LRCX "
        "AVGO AMD ORCL DELL MRVL CMI INTU QCOM TXN PANW TSM EXPE GEV ETN V MA UNH ACN IBM PYPL NKE "
        "LULU CMG SBUX REGN BKNG ADP COST SKHY").split()
# 美股上市不足一年的标的：允许 ≥60 个交易日，波动率/回撤用上市以来的数据，页面标注“上市不足一年”
SHORT_HISTORY = {"SKHY"}
# ADR：计算相对本土股价的溢价（ratio = 1 份 ADR 对应的本土股数）
ADR = {"SKHY": dict(local="000660.KS", fx="KRW=X", ratio=0.1, local_name="首尔")}
# 行业特征的固定说明（数字部分仍由模板实时生成）
EXTRA_WHY = {"SKHY": "HBM/AI 存储龙头"}
EXTRA_RISK = {"SKHY": "存储是强周期行业，和 MU 一样低PE可能出现在盈利高点"}
# Crypto 板块：不套用价值股剔除规则。kind: etf / treasury / exchange / stablecoin / broker / miner
CRYPTO = {
    "IBIT": dict(kind="etf", coin="BTC", label="比特币 ETF"),
    "ETHA": dict(kind="etf", coin="ETH", label="以太坊 ETF"),
    "MSTR": dict(kind="treasury", coin="BTC", label="BTC 财库"),
    "BMNR": dict(kind="treasury", coin="ETH", label="ETH 财库"),
    "COIN": dict(kind="exchange", coin="BTC", label="加密交易所"),
    "HOOD": dict(kind="broker", coin="BTC", label="券商+加密"),
    "CRCL": dict(kind="stablecoin", coin=None, label="USDC 稳定币"),
    "MARA": dict(kind="miner", coin="BTC", label="BTC 矿企"),
    "IREN": dict(kind="miner", coin="BTC", label="矿企/AI 算力"),
}
HOLDINGS = os.path.join(ROOT, "holdings.json")
TOP_N = 20
# 美股休市日 / 提前收盘日（NYSE，2026-2027）。每年底更新一次。
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
            "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
HALF_DAYS = {"2026-11-27", "2026-12-24", "2027-11-26"}
MAX_SPREAD = 0.50   # 买卖价差超过 50% 的报价视为无效（中间价不可信）
MAX_ANN = 400.0     # 年化超过 400% 视为异常报价
CARRY_DAYS = 3      # 本次拿不到有效报价时，沿用上次数据的最长天数


def market_session(now_et):
    """返回 ('open'|'pre'|'post'|'closed', 当日收盘时间)。"""
    day = now_et.date().isoformat()
    if now_et.weekday() >= 5 or day in HOLIDAYS:
        return "closed", None
    close = dt.time(13, 0) if day in HALF_DAYS else dt.time(16, 0)
    t = now_et.time()
    if t < dt.time(9, 30):
        return "pre", close
    if t < close:
        return "open", close
    return "post", close
R = 0.04            # 无风险利率（Black-Scholes 用）
TARGET_DELTA = -0.25
N = NormalDist()


def retry(fn, tries=3, wait=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa
            if i == tries - 1:
                raise
            time.sleep(wait * (i + 1))


def num(x, nd=2):
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else round(x, nd)
    except Exception:
        return None


def bs_put_delta(S, K, T, s):
    d1 = (math.log(S / K) + (R + s * s / 2) * T) / (s * math.sqrt(T))
    return N.cdf(d1) - 1


def nasdaq_earn(t):
    """第二数据源：Nasdaq 的下一次财报日期（带 * 为预计）。拿不到返回 None。"""
    import re, urllib.request
    try:
        req = urllib.request.Request(f"https://api.nasdaq.com/api/analyst/{t}/earnings-date",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        txt = json.load(urllib.request.urlopen(req, timeout=10))["data"]["announcement"]
        m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", txt)
        if not m:
            return None
        d = dt.datetime.strptime(" ".join(m.groups()), "%b %d %Y").date()
        return dict(date=d.isoformat(), est="*" in txt)
    except Exception:
        return None


def bs_pitm(S, K, T, s):
    """到期被行权（股价低于行权价）的风险中性概率 N(-d2)。"""
    d2 = (math.log(S / K) + (R - s * s / 2) * T) / (s * math.sqrt(T))
    return N.cdf(-d2)


# ---------------- 基本面（每天刷新一次，缓存到 fundamentals.json） ----------------
def load_fund(force=False):
    cache = {}
    if os.path.exists(FUND):
        cache = json.load(open(FUND))
    fresh_cut = time.time() - 20 * 3600
    for t in POOL + list(CRYPTO):
        c = cache.get(t)
        if c and c.get("_ts", 0) > fresh_cut and "eps0" in c and not force:
            continue
        try:
            T = yf.Ticker(t)
            i = retry(lambda: T.info)
            fcf = None
            try:
                q = retry(lambda: T.quarterly_cashflow)
                ocf, capex = q.loc["Operating Cash Flow"].iloc[:4], q.loc["Capital Expenditure"].iloc[:4]
                if ocf.notna().sum() == 4:
                    fcf = float(ocf.sum() + capex.fillna(0).sum())
            except Exception:
                pass
            cur = i.get("financialCurrency") or "USD"
            fx = 1.0
            if cur != "USD":
                try:
                    fx = float(retry(lambda: yf.Ticker(f"{cur}USD=X").history(period="5d"))["Close"].iloc[-1])
                except Exception:
                    fx = None
            if fcf is None and i.get("freeCashflow") is not None:
                fcf = float(i["freeCashflow"])
            usd = (lambda v: None if v is None or fx is None else v * fx)
            cache[t] = dict(
                _ts=time.time(), name=i.get("shortName"), sector=i.get("sector"), mcap=i.get("marketCap"),
                fcf=usd(fcf), cash=usd(i.get("totalCash")), debt=usd(i.get("totalDebt")),
                ni=usd(i.get("netIncomeToCommon")), book=i.get("bookValue"), eps=i.get("trailingEps"),
                tpe=i.get("trailingPE"), fpe=i.get("forwardPE"), roe=i.get("returnOnEquity"),
                om=i.get("operatingMargins"), rg=i.get("revenueGrowth"),
                earn=i.get("earningsTimestampStart") or i.get("earningsTimestamp"),
                earn_est=i.get("isEarningsDateEstimate"), nq_earn=nasdaq_earn(t) if i.get("quoteType") != "ETF" else None,
                eps0=i.get("epsCurrentYear"), eps1=i.get("forwardEps"), nfye=i.get("nextFiscalYearEnd"),
                tgt=i.get("targetMeanPrice"), rec=i.get("recommendationKey"))
            print("fund ok", t, flush=True)
        except Exception as e:
            print("fund ERR", t, e, flush=True)
        time.sleep(0.3)
    json.dump(cache, open(FUND, "w"), indent=1, ensure_ascii=False)
    return cache


# ---------------- 期权：30-45 天、Delta≈-0.25 的 Put ----------------
def pick_put(t, S, today):
    T_ = yf.Ticker(t)
    exps = retry(lambda: T_.options)
    days = lambda e: (dt.date.fromisoformat(e) - today).days
    is_monthly = lambda e: dt.date.fromisoformat(e).weekday() == 4 and 15 <= dt.date.fromisoformat(e).day <= 21
    # 优先月度合约（流动性最好）：25–52 天内的月度合约；没有再用 30–45 天的周度合约
    monthly = [e for e in exps if 25 <= days(e) <= 52 and is_monthly(e)]
    weekly = [e for e in exps if 30 <= days(e) <= 45] or [e for e in exps if 25 <= days(e) <= 52]
    if not (monthly or weekly):
        return None
    e = min(monthly, key=lambda x: abs(days(x) - 37)) if monthly else weekly[0]
    dte = (dt.date.fromisoformat(e) - today).days
    ch = retry(lambda: T_.option_chain(e))
    p, c = ch.puts.copy(), ch.calls
    k0 = p.iloc[(p.strike - S).abs().argsort()[:1]].strike.values[0]
    ivs = []
    for df in (p, c):
        row = df[df.strike == k0]
        if len(row) and 0.03 < row.impliedVolatility.values[0] < 5:
            ivs.append(float(row.impliedVolatility.values[0]))
    atm = float(np.mean(ivs)) if ivs else None
    Ty = dte / 365
    # 只用有效双边报价：买一 > 0、卖一 ≥ 买一、价差 ≤ 50%。盘前/假日的 0 买价或离谱报价一律不用
    q = p[(p.bid > 0) & (p.ask >= p.bid) & (p.strike < S)].copy()
    quote = "mid"
    if q.empty:
        return None
    q["prem"] = (q.bid + q.ask) / 2
    q = q[(q.ask - q.bid) / q.prem <= MAX_SPREAD]
    if q.empty:
        return None
    def sig(iv):
        if 0.05 < iv < 5:
            return iv
        return atm
    q["sig"] = [sig(v) for v in q.impliedVolatility]
    q = q[q.sig.notna()]
    if q.empty:
        return None
    q["delta"] = [bs_put_delta(S, k, Ty, s) for k, s in zip(q.strike, q.sig)]
    # 在 Delta -0.20 ~ -0.30 之间优先选买卖价差 ≤15% 的合约（中间价更可能成交），再取最接近 -0.25 的
    q["spr_"] = (q.ask - q.bid) / q.prem
    band = q[(q.delta <= -0.20) & (q.delta >= -0.30)]
    good = band[band.spr_ <= 0.15]
    pool = good if len(good) else q
    row = pool.iloc[(pool.delta - TARGET_DELTA).abs().argsort()[:1]].iloc[0]
    K, prem = float(row.strike), float(row.prem)
    if prem <= 0 or prem >= K or prem / (K - prem) * 365 / dte * 100 > MAX_ANN:
        return None
    return dict(exp=e, dte=dte, k=K, bid=num(row.bid), ask=num(row.ask), prem=num(prem, 3), quote=quote,
                pitm=num(bs_pitm(S, K, Ty, float(row.sig)) * 100, 0),
                per=num(prem / (K - prem) * 100, 2),
                dl=num(row.delta), piv=num(row.sig * 100, 1), iv=num(atm * 100, 1) if atm else None,
                otm=num((1 - K / S) * 100, 1), ann=num(prem / (K - prem) * 365 / dte * 100, 1),
                cost=num(K - prem, 3), cash=int(round(K * 100)),
                oi=None if pd.isna(row.openInterest) else int(row.openInterest),
                spr=num((row.ask - row.bid) / ((row.ask + row.bid) / 2) * 100, 1) if quote == "mid" else None,
                vol=None if pd.isna(row.volume) else int(row.volume))


# ---------------- 理由 / 风险模板 ----------------
def reasons(r, pct):
    cands = []
    if r["ivhv"] is not None:
        cands.append((pct["ivhv"], f"IV 是{'上市以来' if r.get('short_hist') else '过去一年'}实际波动的 {r['ivhv']:.2f} 倍，期权偏贵，卖方占优"
                       if r["ivhv"] >= 1 else f"隐含波动率 {r['iv']:.0f}%，权利金厚"))
    if r["dd"] is not None:
        cands.append((pct["dd"], f"较{'上市以来' if r.get('short_hist') else '一年'}高点回撤 {abs(r['dd']):.0f}%，价格已打折"))
    if r["fpe"]:
        cands.append((pct["fpe"], f"远期市盈率仅 {r['fpe']:.1f} 倍，估值便宜"))
    if r["fcfy"] is not None:
        cands.append((pct["fcfy"], f"自由现金流收益率 {r['fcfy']:.1f}%，现金牛"))
    if r["roe"] is not None and r["roe"] >= 20 and (r["om"] or 0) >= 20:
        cands.append((pct["q"], f"ROE {r['roe']:.0f}%、营业利润率 {r['om']:.0f}%，生意质量高"))
    if r["nc"] is not None and r["nc"] > 0:
        cands.append((pct["nc"] * 0.9, f"账上净现金约 {r['nc']:,.0f} 亿美元，资产负债表稳"))
    cands.sort(key=lambda x: -x[0])
    return "；".join(x[1] for x in cands[:2])


def earn_txt(r):
    d = dt.date.fromisoformat(r["earn"])
    return f"到期前{'预计' if r.get('earn_est') else ''} {d.month}/{d.day} 发财报，可能跳空"


def risks(r):
    out = []
    if r["ein"] and r["earn"]:
        out.append(earn_txt(r))
    if r.get("spr") is not None and r["spr"] > 15:
        out.append(f"买卖价差 {r['spr']:.0f}%，中间价不一定成交，实际权利金可能更少")
    if r["fpe"] and r["tpe"] and r["fpe"] < r["tpe"] * 0.5:
        out.append(f"远期PE {r['fpe']:.1f} 倍建立在分析师预期盈利大增上（当前PE {r['tpe']:.0f} 倍），预期落空估值会被重估")
    if r["hv"] and r["hv"] >= 55:
        out.append(f"{'上市以来' if r.get('short_hist') else '过去一年'}实际波动 {r['hv']:.0f}%，股价大起大落")
    if r["m1"] is not None and r["m1"] <= -15:
        out.append(f"近一月仍跌 {abs(r['m1']):.0f}%，下跌趋势未止")
    if r["oi"] is not None and r["oi"] < 300:
        out.append("该行权价持仓量偏低，成交可能有滑点")
    if r["fpe"] and r["fpe"] > 35:
        out.append(f"远期市盈率 {r['fpe']:.0f} 倍，估值不便宜")
    if r["rg"] is not None and r["rg"] < 0:
        out.append("营收同比下滑")
    if r["cash"] >= 50000:
        out.append(f"一张需备 ${r['cash']:,} 现金，资金门槛高")
    if not out:
        out.append(f"若跌破接货价 ${r['cost']:,.2f} 将出现浮亏")
    return "；".join(out[:2])


# ---------------- Crypto 板块的理由 / 风险 ----------------
COIN_NAME = {"BTC": "比特币", "ETH": "以太坊"}


def reasons_crypto(r, pct):
    c = []
    if r.get("mnav") is not None and r["mnav"] < 1:
        c.append((1.1, f"股价低于所持{COIN_NAME[r['coin']]}价值（mNAV {r['mnav']:.2f}×），相当于打折买币"))
    if r["ivhv"] is not None and r["ivhv"] >= 1:
        c.append((pct["ivhv"], f"IV 是过去一年实际波动的 {r['ivhv']:.2f} 倍，期权偏贵，卖方占优"))
    if r.get("iv") is not None and r.get("ann") is not None:
        c.append((pct["ann"], f"隐含波动率 {r['iv']:.0f}%，Delta 0.25 的 Put 年化就有 {r['ann']:.0f}%"))
    if r["dd"] is not None:
        c.append((pct["dd"], f"较一年高点回撤 {abs(r['dd']):.0f}%，已大幅出清"))
    if r.get("coin_dd") is not None and r["kind"] in ("etf", "treasury"):
        c.append((pct["dd"] * 0.95, f"{COIN_NAME[r['coin']]}已从高点回撤 {abs(r['coin_dd']):.0f}%"))
    if r.get("spr") is not None and r["spr"] <= 5 and (r["oi"] or 0) >= 1000:
        c.append((pct["liq"], f"期权流动性好：买卖价差 {r['spr']:.1f}%、持仓量 {r['oi']:,}"))
    c.sort(key=lambda x: -x[0])
    return "；".join(x[1] for x in c[:2])


KIND_RISK = {
    "etf": "{coin} 24 小时交易，周末大跌周一开盘可能直接跳空穿过行权价",
    "treasury": "{coin}的杠杆代理：靠增发股票/发债买币，持续稀释，币价下跌时跌幅通常更大",
    "exchange": "收入跟交易量和币价高度相关，熊市时利润下滑快",
    "broker": "加密和期权交易收入随市场情绪波动，估值不便宜",
    "stablecoin": "利润主要来自 USDC 储备利息，降息会直接压缩收入；上市时间短、波动大",
    "miner": "挖矿收入随币价和全网算力波动，资本开支大、常需增发",
}


def risks_crypto(r):
    out = [KIND_RISK[r["kind"]].format(coin=COIN_NAME.get(r["coin"], ""))]
    if r["ein"] and r["earn"]:
        out.insert(0, earn_txt(r))
    if r.get("spr") is not None and (r["spr"] > 6 or (r["oi"] or 0) < 300):
        out.append(f"该行权价持仓量 {r['oi'] or 0}、价差 {r['spr']:.1f}%，成交可能有滑点，用限价单")
    if r["hv"] and r["hv"] >= 90:
        out.append(f"过去一年实际波动 {r['hv']:.0f}%，可能一个月跌三成")
    return "；".join(out[:2])


def chg(s, n):
    s = s.dropna()
    return num((s.iloc[-1] / s.iloc[-1 - n] - 1) * 100, 1) if len(s) > n else None


def base_row(t, f, s, today, n_hv=252):
    S = float(s.iloc[-1])
    last = s.iloc[-n_hv:]
    lr = np.log(last).diff().dropna()
    hv = float(lr.std() * math.sqrt(252) * 100)
    try:
        o = pick_put(t, S, today)
    except Exception as e:
        print("opt ERR", t, e); o = None
    if not o:
        print("skip(no option)", t); return None
    f = f or {}
    mcap = f.get("mcap")
    nc = ((f.get("cash") or 0) - (f.get("debt") or 0)) if f.get("cash") is not None else None
    # 财报日：Yahoo + Nasdaq 两个来源，取最早的未来日期（保守）；任一来源标“预计”或两者不一致 → 标为预计
    srcs = []
    if f.get("earn"):
        ed = dt.datetime.fromtimestamp(int(f["earn"]), dt.timezone.utc).astimezone(ET).date()
        if ed >= today:
            srcs.append((ed.isoformat(), bool(f.get("earn_est"))))
    nq = f.get("nq_earn")
    if nq and nq["date"] >= today.isoformat():
        srcs.append((nq["date"], nq["est"]))
    earn, earn_est, earn_alt = None, False, None
    if srcs:
        srcs.sort()
        earn, earn_est = srcs[0]
        if len({x[0] for x in srcs}) > 1:
            earn_est, earn_alt = True, srcs[-1][0]
    # 远期 PE：按财年剩余时间混合“本财年 + 下财年”EPS，近似未来 12 个月（NTM）
    fpe = f.get("fpe")
    e0, e1, nfye = f.get("eps0"), f.get("eps1"), f.get("nfye")
    if e0 is not None and e1 is not None and nfye:
        w = min(max((nfye - time.time()) / (365 * 86400), 0), 1)
        ntm = w * e0 + (1 - w) * e1
        fpe = S / ntm if ntm > 0 else None
    r = dict(t=t, n=f.get("name"), sec=f.get("sector"), px=num(S), w1=chg(s, 5), m1=chg(s, 21),
             dd=num((S / float(last.max()) - 1) * 100, 1), tpe=num(f.get("tpe"), 1), fpe=num(fpe, 1),
             fcfy=num(f["fcf"] / mcap * 100, 1) if f.get("fcf") and mcap else None,
             nc=num(nc / 1e8, 0) if nc is not None else None,  # 单位：亿美元
             ncr=(nc / mcap) if nc is not None and mcap else None,
             roe=num(f["roe"] * 100, 0) if f.get("roe") is not None else None,
             om=num(f["om"] * 100, 0) if f.get("om") is not None else None,
             rg=num(f["rg"] * 100, 0) if f.get("rg") is not None else None,
             hv=num(hv, 1), tgt=num(f.get("tgt")), rec=f.get("rec"), earn=earn, earn_est=earn_est, earn_alt=earn_alt, mcap=mcap, **o)
    r["ivhv"] = num(r["iv"] / r["hv"], 2) if r["iv"] and r["hv"] else None
    r["ein"] = bool(earn and earn <= o["exp"])
    r["upside"] = num((r["tgt"] / S - 1) * 100, 1) if r["tgt"] else None
    return r


SCORE_KEYS = {"why", "risk", "rank", "score", "sq", "sv", "si", "sd", "sa", "sl", "liq"}


def carry(t, old_items, old_upd, today):
    """本次拿不到有效报价时，沿用上次（≤3 天、合约未临近到期）的整行数据，并标记“报价沿用”。"""
    o = old_items.get(t)
    if not o or not old_upd:
        return None
    upd = dt.datetime.fromisoformat(old_upd.replace("Z", "+00:00"))
    if (dt.datetime.now(dt.timezone.utc) - upd).days >= CARRY_DAYS:
        return None
    if (dt.date.fromisoformat(o["exp"]) - today).days < 21:
        return None
    r = {k: v for k, v in o.items() if k not in SCORE_KEYS}
    r["stale_q"] = r.get("stale_q") or upd.astimezone(SGT).strftime("%m-%d %H:%M")
    print("carry", t, "from", r["stale_q"])
    return r


def pctl(d, col, i, asc=True):
    v = d[col].rank(pct=True, ascending=asc).iloc[i]
    return 0.0 if pd.isna(v) else float(v)


def clean(r):
    r = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in r.items()}
    r.pop("ncr", None); r.pop("mcap", None)
    return r


def build_value(px, fund, today, notes, old_items=None, old_upd=None):
    old_items = old_items or {}
    rows = []
    for t in POOL:
        f = fund.get(t)
        s = px[t].dropna() if t in px else pd.Series(dtype=float)
        min_bars = 60 if t in SHORT_HISTORY else 200
        if not f or len(s) < min_bars:
            print("skip(no data)", t, len(s)); continue
        # 质量过滤：FCF 为负 / 股东权益为负 / 亏损 —— 剔除
        if (f.get("fcf") is not None and f["fcf"] <= 0) or (f.get("book") is not None and f["book"] <= 0) \
                or (f.get("ni") is not None and f["ni"] <= 0) or (f.get("eps") is not None and f["eps"] <= 0):
            print("filtered", t); continue
        r = base_row(t, f, s, today) or carry(t, old_items, old_upd, today)
        if r and t in SHORT_HISTORY and len(s) < 252:
            r["short_hist"], r["listed"] = True, str(s.index[0].date())
        if r and t in ADR:
            a = ADR[t]
            try:
                loc, fx = px[a["local"]].dropna(), px[a["fx"]].dropna()
                parity = float(loc.iloc[-1]) * a["ratio"] / float(fx.iloc[-1])
                r["adr_prem"] = num((float(s.iloc[-1]) / parity - 1) * 100, 1)
                r["adr_local"] = a["local_name"]
                if r.get("fpe"):
                    r["fpe_local"] = num(r["fpe"] / (1 + r["adr_prem"] / 100), 1)  # 按本土股价折算的远期PE
            except Exception as e:
                print("adr ERR", t, e)
        if r:
            rows.append(r); print("ok", t, r["k"], r["prem"], r["ann"], flush=True)
        time.sleep(0.3)
    if not rows:
        return [], 0
    d = pd.DataFrame(rows)
    pr = lambda x, asc=True: x.rank(pct=True, ascending=asc).fillna(0.5)
    qual = (pr(d.roe.clip(upper=100)) + pr(d.om) + pr(d.ncr) + pr(d.rg.clip(upper=100))) / 4
    val = (pr(d.fpe, False) + pr(d.fcfy)) / 2
    ivs = (pr(d.ivhv) + pr(d.iv)) / 2
    d["sq"], d["sv"], d["si"], d["sd"] = (qual * 30).round(1), (val * 30).round(1), (ivs * 20).round(1), (pr(-d.dd) * 20).round(1)
    d["score"] = (d.sq + d.sv + d.si + d.sd).round(0).astype(int)
    d = d.sort_values("score", ascending=False).head(TOP_N).reset_index(drop=True)
    items = []
    for i, r in d.iterrows():
        r = r.to_dict()
        pct = dict(ivhv=pctl(d, "ivhv", i), dd=pctl(d, "dd", i, asc=False), fpe=pctl(d, "fpe", i, asc=False),
                   fcfy=pctl(d, "fcfy", i), q=float(r["sq"] / 30), nc=pctl(d, "ncr", i))
        r = clean(r)
        r["why"] = notes.get(r["t"], {}).get("why") or reasons(r, pct)
        r["risk"] = notes.get(r["t"], {}).get("risk") or risks(r)
        t = r["t"]
        if t in EXTRA_WHY and not notes.get(t, {}).get("why"):
            r["why"] = EXTRA_WHY[t] + "；" + r["why"]
        if t in EXTRA_RISK and not notes.get(t, {}).get("risk"):
            extra = [EXTRA_RISK[t]]
            if r.get("adr_prem") is not None and r["adr_prem"] > 10:
                extra.append(f"ADR 比{r['adr_local']}股价贵 {r['adr_prem']:.0f}%，溢价收窄会额外下跌")
            # 去掉模板里与周期股提示重复的“远期PE…重估”
            rest = [x for x in r["risk"].split("；") if "预期落空估值" not in x]
            r["risk"] = "；".join((extra + rest)[:3])
        r["rank"] = i + 1
        items.append(r)
    return items, len(rows)


def build_crypto(px, fund, today, notes, coins, old_items=None, old_upd=None):
    old_items = old_items or {}
    hold = json.load(open(HOLDINGS)) if os.path.exists(HOLDINGS) else {}
    rows = []
    for t, meta in CRYPTO.items():
        s = px[t].dropna() if t in px else pd.Series(dtype=float)
        if len(s) < 60:
            print("skip(no data)", t); continue
        r = base_row(t, fund.get(t), s, today)
        if not r:
            r = carry(t, old_items, old_upd, today)
            if r:
                rows.append(r)
            continue
        r.update(kind=meta["kind"], coin=meta["coin"], label=meta["label"])
        if meta["kind"] == "etf":
            for k in ("tpe", "fpe", "fcfy", "nc", "roe", "om", "rg"):
                r[k] = None
        if meta["coin"]:
            c = coins[meta["coin"]]
            r.update(coin_px=c["px"], coin_d7=c["d7"], coin_d30=c["d30"], coin_dd=c["dd"])
        # mNAV = 市值 ÷（持币数量 × 当日币价）；持币数量来自公司公告（holdings.json）
        h = hold.get(t)
        r["mnav"] = None
        if h and r.get("mcap"):
            cs = px[h["coin"] + "-USD"].dropna()
            sd = s.index[-1]
            cpx = float(cs[cs.index <= sd].iloc[-1]) if (cs.index <= sd).any() else float(cs.iloc[-1])
            r["mnav"] = num(r["mcap"] / (h["amount"] * cpx), 2)
            r["hold_amt"], r["hold_asof"], r["hold_src"] = h["amount"], h["asof"], h["src"]
            r["hold_age"] = (today - dt.date.fromisoformat(h["asof"])).days
        rows.append(r); print("ok", t, r["k"], r["prem"], r["ann"], r.get("mnav"), flush=True)
        time.sleep(0.3)
    if not rows:
        return []
    d = pd.DataFrame(rows)
    pr = lambda x, asc=True: x.rank(pct=True, ascending=asc).fillna(0.5)
    d["liq"] = (pr(d.spr, False) + pr(np.log1p(d.oi.fillna(0)))) / 2
    d["sa"], d["si"], d["sd"], d["sl"] = (pr(d.ann) * 30).round(1), (pr(d.ivhv) * 20).round(1), (pr(-d.dd) * 25).round(1), (d.liq * 25).round(1)
    d["score"] = (d.sa + d.si + d.sd + d.sl).round(0).astype(int)
    d = d.sort_values("score", ascending=False).reset_index(drop=True)
    items = []
    for i, r in d.iterrows():
        r = r.to_dict()
        pct = dict(ivhv=pctl(d, "ivhv", i), dd=pctl(d, "dd", i, asc=False), ann=pctl(d, "ann", i), liq=pctl(d, "liq", i))
        r = clean(r)
        r["why"] = notes.get(r["t"], {}).get("why") or reasons_crypto(r, pct)
        r["risk"] = notes.get(r["t"], {}).get("risk") or risks_crypto(r)
        r["rank"] = i + 1
        items.append(r)
    return items


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(ET).date()
    fund = load_fund(force="--refresh-fund" in sys.argv)
    notes = json.load(open(NOTES)) if os.path.exists(NOTES) else {}
    old = json.load(open(DATA)) if os.path.exists(DATA) else None

    idx = ["SPY", "QQQ", "SMH", "^VIX"]
    cc = ["BTC-USD", "ETH-USD"]
    adr_ex = sorted({a["local"] for a in ADR.values()} | {a["fx"] for a in ADR.values()})
    px = retry(lambda: yf.download(POOL + list(CRYPTO) + idx + cc + adr_ex, period="13mo", auto_adjust=True,
                                   progress=False, threads=True))["Close"]
    asof = px["SPY"].dropna().index[-1].date()

    coins = {}
    for c in ("BTC", "ETH"):
        s = px[c + "-USD"].dropna()
        coins[c] = dict(px=num(s.iloc[-1]), d7=chg(s, 7), d30=chg(s, 30),
                        dd=num((s.iloc[-1] / s.iloc[-365:].max() - 1) * 100, 1),
                        asof=str(s.index[-1].date()))

    old_upd = (old or {}).get("updated_utc")
    old_v = {x["t"]: x for x in (old or {}).get("items", [])}
    old_c = {x["t"]: x for x in ((old or {}).get("crypto") or {}).get("items", [])}
    items, passed = build_value(px, fund, today, notes, old_v, old_upd)
    old_n = len(old["items"]) if old else 0
    need = max(10, int(min(old_n, TOP_N) * 0.6))
    if len(items) < need:
        print(f"FAIL: only {len(items)} rows (< {need}); keep old data.json"); sys.exit(2)

    crypto = build_crypto(px, fund, today, notes, coins, old_c, old_upd)
    if len(crypto) < 4:
        print(f"WARN: crypto only {len(crypto)} rows; keep old crypto section")
        old_c = (old or {}).get("crypto")
        if old_c:
            crypto_out = old_c
        else:
            crypto_out = dict(items=crypto, coins=coins, avg_ann=None, avg_ann_top5=None)
    else:
        crypto_out = dict(items=crypto, coins=coins, pool_size=len(CRYPTO),
                          avg_ann=num(np.mean([x["ann"] for x in crypto]), 1),
                          avg_ann_top5=num(np.mean(sorted([x["ann"] for x in crypto], reverse=True)[:5]), 1))

    mk = {k: dict(px=num(px[k].dropna().iloc[-1]), w1=chg(px[k], 5), m1=chg(px[k], 21),
                  dd=num((px[k].dropna().iloc[-1] / px[k].dropna().iloc[-252:].max() - 1) * 100, 1)) for k in idx}
    et_now = now.astimezone(ET)
    sess, _ = market_session(et_now)
    session = "盘中" if sess == "open" else "非交易时段"
    ctx = (f"标普500 近5日 {mk['SPY']['w1']:+.1f}%（距一年高点 {mk['SPY']['dd']:.1f}%），"
           f"纳指100 近5日 {mk['QQQ']['w1']:+.1f}%，半导体 SMH 近一月 {mk['SMH']['m1']:+.1f}%（距高点 {mk['SMH']['dd']:.1f}%），"
           f"VIX {mk['^VIX']['px']:.1f}。")
    b, e = coins["BTC"], coins["ETH"]
    cctx = (f"比特币 ${b['px']:,.0f}，近7日 {b['d7']:+.1f}%、近30日 {b['d30']:+.1f}%（距一年高点 {b['dd']:.1f}%）；"
            f"以太坊 ${e['px']:,.0f}，近7日 {e['d7']:+.1f}%、近30日 {e['d30']:+.1f}%（距高点 {e['dd']:.1f}%）。")
    gaps = []
    for c in ("BTC", "ETH"):
        cs = px[c + "-USD"].dropna()
        base = cs[cs.index.date <= asof]
        if len(base) and cs.index[-1].date() > asof:
            g = (cs.iloc[-1] / base.iloc[-1] - 1) * 100
            coins[c]["since_close"] = num(g, 1)
            if abs(g) >= 1:
                gaps.append(f"{COIN_NAME[c]} {g:+.1f}%")
    if gaps:
        cctx += f"美股上次收盘（{asof.month}/{asof.day}）以来 " + "、".join(gaps) + "，期权报价是收盘时的，开盘后会变化。"
    crypto_out["context"] = cctx
    out = dict(updated_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
               updated_sgt=now.astimezone(SGT).strftime("%Y-%m-%d %H:%M"),
               price_asof=str(asof), session=session, market=mk, context=ctx,
               pool_size=len(POOL), passed=passed,
               avg_ann=num(np.mean([x["ann"] for x in items]), 1),
               avg_ann_top5=num(np.mean([x["ann"] for x in items[:5]]), 1),
               method=dict(delta=TARGET_DELTA, dte="30-45", r=R), items=items, crypto=crypto_out)
    tmp = DATA + ".tmp"
    json.dump(out, open(tmp, "w"), ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, DATA)
    print(f"wrote {len(items)} value + {len(crypto)} crypto; top5:", [(x['t'], x['ann']) for x in items[:5]],
          "crypto:", [(x['t'], x['ann']) for x in crypto])


if __name__ == "__main__":
    main()
