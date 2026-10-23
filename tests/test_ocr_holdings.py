"""持仓截图 OCR 解析回归测试；不依赖数据库或外部行情。"""

import unittest

from quant.datasource.ocr_holdings import (
    parse_account_summary,
    parse_portfolio_screenshot,
    parse_ths_holdings,
)


NAME_CODES = {
    "崇达技术": "002815",
    "格尔软件": "603232",
    "新华文轩": "601811",
    "中国电影": "600977",
    "长电科技": "600584",
    "元隆雅图": "002878",
    "东百集团": "600693",
    "金种子酒": "600199",
}

# macOS Vision 对一张已有华宝持仓截图识别出的行。
HUABAO_OCR = """华宝证券
买入
卖出
持仓
持仓股
市值
成本/现价
当日盈亏
个股仓位
崇达技术
24.311
-86.24
4,776.00
23.880
-1.774%
18.9%
格尔软件
17.210
0.00
1,725.00
17.250
0.000%
6.8%
新华文轩
26.101
163.87
27.740
6.281%
11.0%
2,774.00
中国电影
14.720
1.00
14.190
0.071%
5.6%
1,419.00
长电科技
0.000
50.00
0.00
57.160
0.870%
0.0%
元隆雅图
0.000
24.00
0.00
13.780
1.740%
0.0%
查看已清仓股票〉
持仓管理
"""

HUABAO_ACCOUNT_OCR = """19:20
同花顺App
22
华宝证券
兔5万0点741
买入
卖出
撤单
持仓
查询
人民币账户 CNY A股
仓位 26.7%
总资产
总盈亏
当日参考盈亏
24,424.70
-351.07
-716.00 -2.85%
总市值
可用
逆回购
可取 转账
6,519.00
16,520.59
11,735.06
持仓股
目
市值
盈亏
持仓/可用
成本/现价
东百集团
-199.02
200
9.875
1,776.00
-10.077%
200
8.880
新华文轩
-77.03
100
26.100
2,533.00
-2.951%
100
25.330
中国电影
-40.01
100
14.720
1,432.00
-2.718%
100
14.320
金种子酒
-35.01
100
8.130
778.00
-4.306%
100
7.780
查看已清仓股票〉
持仓管理
"""


class TestHoldingsOCR(unittest.TestCase):
    def test_huabao_without_codes_does_not_infer_shares_from_pnl(self):
        rows = parse_ths_holdings(HUABAO_OCR, NAME_CODES)
        self.assertEqual(rows, [
            {"code": "002815", "name": "崇达技术", "cost": "24.311", "shares": "200"},
            {"code": "603232", "name": "格尔软件", "cost": "17.21", "shares": "100"},
            {"code": "601811", "name": "新华文轩", "cost": "26.101", "shares": "100"},
            {"code": "600977", "name": "中国电影", "cost": "14.72", "shares": "100"},
        ])

    def test_no_stock_catalog_leaves_code_for_human_review(self):
        rows = parse_ths_holdings(HUABAO_OCR)
        self.assertEqual([row["name"] for row in rows], ["崇达技术", "格尔软件", "新华文轩", "中国电影"])
        self.assertTrue(all(not row["code"] for row in rows))

    def test_missing_market_value_leaves_shares_blank(self):
        rows = parse_ths_holdings(
            "持仓股\n中国电影\n14.720\n1.00\n14.190\n0.071%\n持仓管理", NAME_CODES)
        self.assertEqual(rows, [{"code": "600977", "name": "中国电影",
                                 "cost": "14.72", "shares": ""}])

    def test_stock_code_layout_still_works(self):
        rows = parse_ths_holdings("中国电影\n600977\n14.19\n14.72\n100\n", NAME_CODES)
        self.assertEqual(rows, [{"code": "600977", "name": "中国电影",
                                 "cost": "14.72", "shares": "100"}])

    def test_full_huabao_account_summary_fields(self):
        self.assertEqual(parse_account_summary(HUABAO_ACCOUNT_OCR), {
            "total_assets": 24424.70,
            "total_market_value": 6519.00,
            "total_profit": -351.07,
            "today_profit": -716.00,
            "today_profit_pct": -2.85,
            "available_funds": 16520.59,
            "withdrawable_funds": 11735.06,
        })

    def test_full_screenshot_returns_account_and_open_positions(self):
        result = parse_portfolio_screenshot(HUABAO_ACCOUNT_OCR, NAME_CODES)
        self.assertEqual(result["account"]["available_funds"], 16520.59)
        self.assertEqual(len(result["holdings"]), 4)
        self.assertEqual(result["holdings"][0], {
            "code": "600693", "name": "东百集团", "cost": "9.875", "shares": "200"})
        self.assertEqual(result["holdings"][3]["code"], "600199")


if __name__ == "__main__":
    unittest.main()
