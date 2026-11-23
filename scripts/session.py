#!/usr/bin/env python3
"""单次刷新器（由 GitHub Actions 调用）。

每次触发只做“该不该刷新 → 刷新一次 → 告诉工作流下一次什么时候再来”，几十秒就结束：
  - 盘中：数据超过 20 分钟就刷新；
  - 收盘后：收盘 10 分钟后补一次收盘数据；
  - 周末/假日：只有数据超过 20 小时（比如上次收盘那次被丢了）才补一次；
  - 盘前 / 不需要刷新：直接退出。
触发来源（任何一个准时就行，多个同时来也只会刷新一次）：
  1) 外部定时器（cron-job.org）每 30 分钟调 workflow_dispatch —— 主力；
  2) 本工作流的“接力”：盘中每次跑完会自己再派发一次，等 ~30 分钟后再刷新；
  3) GitHub 自带的 schedule —— 备份。

输出（写入 $GITHUB_OUTPUT）：next_wait=N 表示 N 分钟后应再刷新一次（0 = 今天不用再接力）。
退出码：刷新失败 → 1（工作流标红）；其余 → 0。
"""
import datetime as dt, json, os, subprocess, sys, time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import market_session, DATA  # noqa: E402

ET = ZoneInfo("America/New_York")
INTERVAL = 30          # 分钟：盘中刷新间隔
FRESH = 20             # 分钟：数据比这更新就不重复刷新（多个触发撞车时只刷一次）
PRE_CHAIN = 90         # 分钟：开盘前这么久以内被触发，就接力到开盘后


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
        if subprocess.run(["git", "pull", "--rebase", "-q", "-X", "theirs", "origin", "main"]).returncode == 0 and \
                subprocess.run(["git", "push", "-q", "origin", "HEAD:main"]).returncode == 0:
            return True
        subprocess.run(["git", "rebase", "--abort"], capture_output=True)
        time.sleep(10 * (i + 1))
    print("::error::推送失败", flush=True)
    return False


def decide(now_utc, lu, force=False):
    """返回 (要不要刷新, 刷新后/不刷新时的接力等待分钟数, 说明)。纯函数，便于测试。"""
    n = now_utc.astimezone(ET)
    sess, close = market_session(n)
    age = (now_utc - lu).total_seconds() / 60 if lu else 1e9
    if sess == "open":
        close_dt = dt.datetime.combine(n.date(), close, ET)
        to_close = (close_dt - n).total_seconds() / 60
        if force or age >= FRESH:
            return True, max(min(INTERVAL, to_close + 12), 5), "盘中"
        return False, max(min(INTERVAL - age, to_close + 12), 5), f"盘中，数据 {age:.0f} 分钟前刚更新"
    if sess == "pre":
        to_open = (dt.datetime.combine(n.date(), dt.time(9, 35), ET) - n).total_seconds() / 60
        return force, (to_open if to_open <= PRE_CHAIN else 0), "盘前"
    if sess == "post":
        close_dt = dt.datetime.combine(n.date(), close, ET)
        done = lu and lu >= (close_dt + dt.timedelta(minutes=10)).astimezone(dt.timezone.utc)
        if done and not force:
            return False, 0, "已有收盘后数据"
        if n < close_dt + dt.timedelta(minutes=10):
            return force, (close_dt + dt.timedelta(minutes=12) - n).total_seconds() / 60, "刚收盘，等收盘报价"
        return True, 0, "收盘后补一次"
    # 周末/假日
    return (force or age >= 20 * 60), 0, "休市"


def main():
    args = sys.argv[1:]
    force = "--force" in args
    extra = [a for a in args if a != "--force"]
    # 排队的运行可能拿到的是旧代码/旧数据：先同步到最新
    sh("git", "pull", "-q", "--rebase", "origin", "main", check=False)
    do, wait, why = decide(dt.datetime.now(dt.timezone.utc), last_update(), force)
    print(f"{why}：{'刷新' if do else '不刷新'}；接力等待 {wait:.0f} 分钟", flush=True)
    ok = refresh(extra) if do else True
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"next_wait={int(round(wait))}\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
