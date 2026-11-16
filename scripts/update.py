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
        "LULU CMG SBUX REGN BKNG ADP COST").split()
TOP_N = 20
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


# ---------------- 基本面（每天刷新一次，缓存到 fundamentals.json） ----------------
def load_fund(force=False):
    cache = {}
    if os.path.exists(FUND):
        cache = json.load(open(FUND))
    fresh_cut = time.time() - 20 * 3600
    for t in POOL:
        c = cache.get(t)
        if c and c.get("_ts", 0) > fresh_cut and not force:
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
    cand = [e for e in exps if 30 <= (dt.date.fromisoformat(e) - today).days <= 45]
    if not cand:
        cand = [e for e in exps if 25 <= (dt.date.fromisoformat(e) - today).days <= 52]
    if not cand:
        return None
    monthly = [e for e in cand if dt.date.fromisoformat(e).weekday() == 4 and 15 <= dt.date.fromisoformat(e).day <= 21]
    e = (monthly or cand)[0]
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
    q = p[(p.bid > 0) & (p.ask > 0) & (p.strike < S)].copy()
    quote = "mid"
    if q.empty:  # 非交易时段 bid/ask 可能为 0，退而用最近成交价
        q = p[(p.lastPrice > 0) & (p.strike < S)].copy()
        quote = "last"
        if q.empty:
            return None
    q["prem"] = (q.bid + q.ask) / 2 if quote == "mid" else q.lastPrice
    def sig(iv):
        if 0.05 < iv < 5:
            return iv
        return atm
    q["sig"] = [sig(v) for v in q.impliedVolatility]
    q = q[q.sig.notna()]
    if q.empty:
        return None
    q["delta"] = [bs_put_delta(S, k, Ty, s) for k, s in zip(q.strike, q.sig)]
    row = q.iloc[(q.delta - TARGET_DELTA).abs().argsort()[:1]].iloc[0]
    K, prem = float(row.strike), float(row.prem)
    if prem <= 0 or prem >= K:
        return None
    return dict(exp=e, dte=dte, k=K, bid=num(row.bid), ask=num(row.ask), prem=num(prem), quote=quote,
                dl=num(row.delta), piv=num(row.sig * 100, 1), iv=num(atm * 100, 1) if atm else None,
                otm=num((1 - K / S) * 100, 1), ann=num(prem / (K - prem) * 365 / dte * 100, 1),
                cost=num(K - prem), cash=int(round(K * 100)),
                oi=None if pd.isna(row.openInterest) else int(row.openInterest),
                vol=None if pd.isna(row.volume) else int(row.volume))


# ---------------- 理由 / 风险模板 ----------------
def reasons(r, pct):
    cands = []
    if r["ivhv"] is not None:
        cands.append((pct["ivhv"], f"IV 是过去一年实际波动的 {r['ivhv']:.2f} 倍，期权偏贵，卖方占优"
                       if r["ivhv"] >= 1 else f"隐含波动率 {r['iv']:.0f}%，权利金厚"))
    if r["dd"] is not None:
        cands.append((pct["dd"], f"较一年高点回撤 {abs(r['dd']):.0f}%，价格已打折"))
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


def risks(r):
    out = []
    if r["ein"] and r["earn"]:
        d = dt.date.fromisoformat(r["earn"])
        out.append(f"到期前 {d.month}/{d.day} 发财报，可能跳空")
    if r["hv"] and r["hv"] >= 55:
        out.append(f"过去一年实际波动 {r['hv']:.0f}%，股价大起大落")
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
        out.append(f"若跌破 ${r['cost']:,} 接货价将出现浮亏")
    return "；".join(out[:2])


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(ET).date()
    fund = load_fund(force="--refresh-fund" in sys.argv)

    idx = ["SPY", "QQQ", "SMH", "^VIX"]
    px = retry(lambda: yf.download(POOL + idx, period="13mo", auto_adjust=True, progress=False, threads=True))["Close"]
    asof = px.dropna(how="all").index[-1].date()

    def chg(s, n):
        s = s.dropna()
        return num((s.iloc[-1] / s.iloc[-1 - n] - 1) * 100, 1) if len(s) > n else None

    rows = []
    for t in POOL:
        f = fund.get(t)
        s = px[t].dropna() if t in px else pd.Series(dtype=float)
        if not f or len(s) < 200:
            print("skip(no data)", t); continue
        # 质量过滤：FCF 为负 / 股东权益为负 / 亏损 —— 剔除
        if (f.get("fcf") is not None and f["fcf"] <= 0) or (f.get("book") is not None and f["book"] <= 0) \
                or (f.get("ni") is not None and f["ni"] <= 0) or (f.get("eps") is not None and f["eps"] <= 0):
            print("filtered", t); continue
        S = float(s.iloc[-1])
        last = s.iloc[-252:]
        lr = np.log(last).diff().dropna()
        hv = float(lr.std() * math.sqrt(252) * 100)
        try:
            o = pick_put(t, S, today)
        except Exception as e:
            print("opt ERR", t, e); o = None
        if not o:
            print("skip(no option)", t); continue
        mcap = f.get("mcap")
        nc = ((f.get("cash") or 0) - (f.get("debt") or 0)) if f.get("cash") is not None else None
        earn = None
        if f.get("earn"):
            ed = dt.datetime.fromtimestamp(int(f["earn"]), dt.timezone.utc).astimezone(ET).date()
            earn = ed.isoformat() if ed >= today else None
        r = dict(t=t, n=f.get("name"), sec=f.get("sector"), px=num(S), w1=chg(s, 5), m1=chg(s, 21),
                 dd=num((S / float(last.max()) - 1) * 100, 1), tpe=num(f.get("tpe"), 1), fpe=num(f.get("fpe"), 1),
                 fcfy=num(f["fcf"] / mcap * 100, 1) if f.get("fcf") and mcap else None,
                 nc=num(nc / 1e8, 0) if nc is not None else None,  # 单位：亿美元
                 ncr=(nc / mcap) if nc is not None and mcap else None,
                 roe=num((f.get("roe") or 0) * 100, 0) if f.get("roe") is not None else None,
                 om=num(f["om"] * 100, 0) if f.get("om") is not None else None,
                 rg=num(f["rg"] * 100, 0) if f.get("rg") is not None else None,
                 hv=num(hv, 1), tgt=num(f.get("tgt")), rec=f.get("rec"), earn=earn, mcap=mcap, **o)
        r["ivhv"] = num(r["iv"] / r["hv"], 2) if r["iv"] and r["hv"] else None
        r["ein"] = bool(earn and earn <= o["exp"])
        r["upside"] = num((r["tgt"] / S - 1) * 100, 1) if r["tgt"] else None
        rows.append(r)
        print("ok", t, r["k"], r["prem"], r["ann"], flush=True)
        time.sleep(0.3)

    old = json.load(open(DATA)) if os.path.exists(DATA) else None
    old_n = len(old["items"]) if old else 0
    need = max(10, int(min(old_n, TOP_N) * 0.6))
    if len(rows) < need:
        print(f"FAIL: only {len(rows)} rows (< {need}); keep old data.json"); sys.exit(2)

    d = pd.DataFrame(rows)
    pr = lambda x, asc=True: x.rank(pct=True, ascending=asc).fillna(0.5)
    qual = (pr(d.roe.clip(upper=100)) + pr(d.om) + pr(d.ncr) + pr(d.rg.clip(upper=100))) / 4
    val = (pr(d.fpe, False) + pr(d.fcfy)) / 2
    ivs = (pr(d.ivhv) + pr(d.iv)) / 2
    ddp = pr(-d.dd)
    d["sq"], d["sv"], d["si"], d["sd"] = (qual * 30).round(1), (val * 30).round(1), (ivs * 20).round(1), (ddp * 20).round(1)
    d["score"] = (d.sq + d.sv + d.si + d.sd).round(0).astype(int)
    d = d.sort_values("score", ascending=False).head(TOP_N).reset_index(drop=True)

    notes = json.load(open(NOTES)) if os.path.exists(NOTES) else {}
    items = []
    for i, r in d.iterrows():
        r = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in r.to_dict().items()}
        pct = dict(ivhv=qual_pct(d, "ivhv", i), dd=qual_pct(d, "dd", i, asc=False), fpe=qual_pct(d, "fpe", i, asc=False),
                   fcfy=qual_pct(d, "fcfy", i), q=float(r["sq"] / 30),
                   nc=qual_pct(d, "ncr", i))
        r["why"] = notes.get(r["t"], {}).get("why") or reasons(r, pct)
        r["risk"] = notes.get(r["t"], {}).get("risk") or risks(r)
        r["rank"] = i + 1
        r.pop("ncr", None); r.pop("mcap", None)
        items.append(r)

    mk = {k: dict(px=num(px[k].dropna().iloc[-1]), w1=chg(px[k], 5), m1=chg(px[k], 21),
                  dd=num((px[k].dropna().iloc[-1] / px[k].dropna().iloc[-252:].max() - 1) * 100, 1)) for k in idx}
    et_now = now.astimezone(ET)
    session = "盘中" if et_now.weekday() < 5 and dt.time(9, 30) <= et_now.time() <= dt.time(16, 0) else "非交易时段"
    ctx = (f"标普500 近5日 {mk['SPY']['w1']:+.1f}%（距一年高点 {mk['SPY']['dd']:.1f}%），"
           f"纳指100 近5日 {mk['QQQ']['w1']:+.1f}%，半导体 SMH 近一月 {mk['SMH']['m1']:+.1f}%（距高点 {mk['SMH']['dd']:.1f}%），"
           f"VIX {mk['^VIX']['px']:.1f}。")
    out = dict(updated_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
               updated_sgt=now.astimezone(SGT).strftime("%Y-%m-%d %H:%M"),
               price_asof=str(asof), session=session, market=mk, context=ctx,
               pool_size=len(POOL), passed=len(rows),
               avg_ann=num(np.mean([x["ann"] for x in items]), 1),
               avg_ann_top5=num(np.mean([x["ann"] for x in items[:5]]), 1),
               method=dict(delta=TARGET_DELTA, dte="30-45", r=R), items=items)
    tmp = DATA + ".tmp"
    json.dump(out, open(tmp, "w"), ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, DATA)
    print(f"wrote {len(items)} items; top5:", [(x['t'], x['ann']) for x in items[:5]])


def qual_pct(d, col, i, asc=True):
    v = d[col].rank(pct=True, ascending=asc).iloc[i]
    return 0.0 if pd.isna(v) else float(v)


if __name__ == "__main__":
    main()
