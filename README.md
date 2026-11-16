# 🍊 大橙子推荐的美股 Option 策略

**Sell Put 抄底排行榜** —— 像巴菲特、段永平一样，用卖出现金担保 Put 的方式抄底优质美股。

🌐 网站：https://0xverybigorange.github.io/us-option-strategy/
🐦 作者：[@0xVeryBigOrange](https://x.com/0xVeryBigOrange)

## 内容
- 每只标的的 **权利金年化收益**、行权价、到期日、权利金、价外幅度、接货价、所需现金
- 入选理由 + 主要风险，综合推荐 / 年化 / 跌幅 / 估值 / IV 溢价 五种排序，一键避开财报

## 方法
- 候选池约 50 只美股大盘优质公司，剔除自由现金流为负、股东权益为负、亏损的公司
- 30–45 天到期（优先月度合约），Black-Scholes 计算 Delta，选 Delta ≈ −0.25 的 Put，权利金取买卖中间价
- 年化 = 权利金 ÷（行权价 − 权利金）× 365 ÷ 剩余天数
- IV 溢价 = 平值隐含波动率 ÷ 过去一年实际波动率
- 综合分 = 质量 30 + 估值 30 + 波动率 20 + 跌幅 20

## 自动更新
GitHub Actions（`.github/workflows/update.yml`）在美股交易日盘中每小时、收盘后各运行一次 `scripts/update.py`，用 yfinance 拉取数据生成 `data.json` 并提交。拉取失败时保留旧数据。
可在 `notes.json` 里为某只股票手写理由/风险覆盖自动生成的句子：`{"MU": {"why": "...", "risk": "..."}}`

本地运行：`pip install -r requirements.txt && python scripts/update.py`

> ⚠️ 仅供学习参考，不构成投资建议。数据来自 Yahoo Finance，可能存在延迟或错误。
