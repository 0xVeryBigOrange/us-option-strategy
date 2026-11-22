#!/usr/bin/env python3
"""交易时段刷新器（由 GitHub Actions 调用）。

GitHub 的定时任务在高峰期经常延迟甚至丢弃，单靠“每小时一个 cron”盘中实际只跑了一两次。
所以改为：一次 Actions 运行在盘中循环刷新（约每 30 分钟一次），收盘后再刷一次就退出。
多个 cron 只是“启动/接力”，任何一个准时触发都能覆盖剩下的交易时段。

退出码：至少成功刷新一次或本来就不需要刷新 → 0；所有尝试都失败 → 1。
"""
import datetime as dt, json, os, subprocess, sys, time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import market_session, DATA  # noqa: E402

ET = ZoneInfo("America/New_York")
INTERVAL = 30 * 60                       # 盘中刷新间隔
BUDGET = int(os.environ.get("SESSION_BUDGET_MIN", "330")) * 60   # 单次运行最长时间（Actions 上限 6 小时）
PRE_WAIT = 80 * 60                       # 开盘前 80 分钟内启动的，就等到开盘
START = time.time()


def now_et():
    return dt.datetime.now(dt.timezone.utc).astimezone(ET)


def last_update():
    try:
        u = json.load(open(DATA))["updated_utc"]
        return dt.datetime.fromisoformat(u.replace("Z", "+00:00"))
    except Exception:
        return None


def sh(*cmd, check=True):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check)


def refresh(extra):
    """跑一次 update.py；成功就提交推送。返回是否成功。"""
    ok = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "update.py"), *extra]).returncode == 0
    if not ok:
        print("::warning::本次数据更新失败（可能被 Yahoo 限流），保留旧数据", flush=True)
        sh("git", "checkout", "--", "data.json", "fundamentals.json", check=False)
        return False
    sh("git", "add", "data.json", "fundamentals.json")
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        print("no changes", flush=True)
        return True
    sh("git", "commit", "-q", "-m", f"data: update {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC")
    for i in range(3):
        if subprocess.run(["git", "pull", "--rebase", "-q", "origin", "main"]).returncode == 0 and \
                subprocess.run(["git", "push", "-q", "origin", "HEAD:main"]).returncode == 0:
            return True
        time.sleep(10 * (i + 1))
    print("::error::推送失败", flush=True)
    return False


def main():
    extra = sys.argv[1:]
    force = "--force" in extra
    extra = [a for a in extra if a != "--force"]
    tries = oks = 0
    while True:
        n = now_et()
        sess, close = market_session(n)
        lu = last_update()
        age = (dt.datetime.now(dt.timezone.utc) - lu).total_seconds() if lu else 1e9
        elapsed = time.time() - START
        if force:
            pass
        elif sess == "pre":
            wait = (dt.datetime.combine(n.date(), dt.time(9, 35), ET) - n).total_seconds()
            if wait > PRE_WAIT or elapsed + wait > BUDGET:
                print(f"盘前，距开盘 {wait/60:.0f} 分钟，不等待，退出"); break
            print(f"盘前，等 {wait/60:.0f} 分钟到开盘后", flush=True); time.sleep(max(wait, 0)); continue
        elif sess == "closed":
            # 周末/假日：只有在上一个交易日收盘后的数据缺失时才补一次（例如周五收盘后那次被丢了）
            if lu and age < 20 * 3600:
                print("休市且数据已是最新，退出"); break
            if tries:
                break
        elif sess == "post":
            close_dt = dt.datetime.combine(n.date(), close, ET)
            if lu and lu >= close_dt.astimezone(dt.timezone.utc) + dt.timedelta(minutes=10):
                print("已有收盘后数据，退出"); break
            if n < close_dt + dt.timedelta(minutes=15):   # 收盘后等 15 分钟，拿到收盘报价
                time.sleep((close_dt + dt.timedelta(minutes=15) - n).total_seconds()); continue
        elif sess == "open" and age < INTERVAL - 120 and not tries:
            # 刚刷新过（比如上一个运行刚结束），等到下一个时间点
            time.sleep(min(INTERVAL - age, max(BUDGET - elapsed, 0))); 
            if time.time() - START >= BUDGET:
                break
            continue

        tries += 1
        oks += refresh(extra)
        if force or sess in ("closed", "post"):
            break
        if time.time() - START + INTERVAL > BUDGET:
            print("达到单次运行时长上限，交给下一个定时任务接力"); break
        time.sleep(INTERVAL)

    print(f"done: {oks}/{tries} 次刷新成功")
    sys.exit(0 if (tries == 0 or oks > 0) else 1)


if __name__ == "__main__":
    main()
