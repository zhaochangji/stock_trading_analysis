"""
证券 App 持仓截图 OCR 与解析，提取 code/name/cost/shares。

OCR 优先级：
1. macOS Vision 的 Python/pyobjc 绑定（如果环境已有）；
2. macOS 原生 Swift Vision（系统通常自带，适用于没有 pyobjc 的 Python）；
3. tesseract CLI（如果用户自行安装）。

不同券商的持仓页不一定展示股票代码和股数。对于“名称 + 市值 + 成本/现价”
布局，解析器支持通过名称补全代码，并按“市值 / 现价”推算股数；结果仍需在网页中人工确认。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

_VISION_SCRIPT = r"""
import sys
try:
    from Foundation import NSURL
    import Vision, Quartz, AppKit
    path = sys.argv[1]
    url = NSURL.fileURLWithPath_(path)
    ci = Quartz.CIImage.imageWithContentsOfURL_(url)
    if ci is None:
        ns = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
        reps = ns.representations() if ns else []
        ci = Quartz.CIImage.imageWithBitmapImageRep_(reps[0]) if reps else None
    if ci is None:
        sys.exit("no ci image")
    handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(ci, {})
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
    req.setUsesLanguageCorrection_(True)
    handler.performRequests_error_([req], None)
    lines = []
    for obs in (req.results() or []):
        c = obs.topCandidates_(1)
        if c:
            lines.append(c[0].string())
    print("\n".join(lines))
except Exception as e:
    sys.exit(str(e))
"""

# 纯系统 API，不依赖 Python 的 Foundation/PyObjC。输出顺序按屏幕从上到下、从左到右。
# Vision 的坐标原点在左下，所以 y 越大越靠上。
_SWIFT_VISION_SCRIPT = r'''
import Foundation
import Vision
import ImageIO
import CoreGraphics

let path = CommandLine.arguments[1]
let url = URL(fileURLWithPath: path)
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("cannot load image\\n", stderr)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true
try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])

let observations = (request.results ?? []).sorted { a, b in
    let ay = a.boundingBox.midY
    let by = b.boundingBox.midY
    if abs(ay - by) > 0.012 { return ay > by }
    return a.boundingBox.minX < b.boundingBox.minX
}
for observation in observations {
    if let candidate = observation.topCandidates(1).first {
        print(candidate.string)
    }
}
'''


def _run_swift_vision(image_path: str) -> str | None:
    """调用 macOS 原生 Vision；不要求 Python 安装 pyobjc。"""
    if not shutil.which("swift"):
        return None
    try:
        result = subprocess.run(
            ["swift", "-", image_path],
            input=_SWIFT_VISION_SCRIPT,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def ocr_image(image_path: str) -> str | None:
    """返回 OCR 文本，识别失败返回 None。"""
    if not image_path or not os.path.exists(image_path):
        return None

    # --- 方法1:macOS Vision 的 Python 绑定(已有环境直接复用) ---
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-c", _VISION_SCRIPT, image_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    # --- 方法2:macOS 原生 Swift Vision(无需 pyobjc/tesseract) ---
    text = _run_swift_vision(image_path)
    if text:
        return text

    # --- 方法3:tesseract CLI(用户安装后作为备用) ---
    if shutil.which("tesseract"):
        try:
            result = subprocess.run(
                ["tesseract", image_path, "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    return None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"\b([036]\d{5}|00[0-9]\d{3})\b")
_PRICE_RE = re.compile(r"^-?\d{1,7}\.\d{1,4}$")
_SHARES_RE = re.compile(r"^(\d+)$")
_CN_RE = re.compile(r"[一-鿿]")
_NAME_RE = re.compile(r"^[一-鿿·（）()]{2,12}$")

# 常见列名/导航词，避免把它们当成股票名称。
_HEADER_LINES = {
    "买入", "卖出", "撤单", "持仓", "查询", "持仓股", "市值", "成本/现价",
    "当日盈亏", "个股仓位", "总资产", "总市值", "总盈亏", "可用", "可取",
    "盈亏", "持仓/可用", "持仓股", "仓位", "可用资金", "可取资金", "今日参考盈亏",
    "持仓管理", "批量买入", "批量卖出", "止盈止损", "持仓资讯", "资产分析",
    "首页", "行情", "自选", "交易", "资讯", "理财", "人民币账户 CNY A股",
    "华宝证券", "同花顺App",
}


def _normalise_line(line: str) -> str:
    return (str(line or "").strip()
            .replace("，", ",")
            .replace("．", ".")
            .replace("。", ".")
            .replace("０", "0").replace("１", "1").replace("２", "2")
            .replace("３", "3").replace("４", "4").replace("５", "5")
            .replace("６", "6").replace("７", "7").replace("８", "8")
            .replace("９", "9"))


def _number(line: str) -> float | None:
    """取一行中的首个普通数字；百分比也能取出，但调用方会先判断。"""
    s = _normalise_line(line).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _is_valid_code(code: str) -> bool:
    """粗滤:主板/中小板/创业板/科创板代码。"""
    return bool(re.match(r"^(0[0-3]\d{4}|[36]\d{5})$", code))


def _is_name_line(line: str, known_names: set[str] | None = None) -> bool:
    s = _normalise_line(line)
    if s in _HEADER_LINES or _CODE_RE.search(s) or "%" in s:
        return False
    if known_names and s in known_names:
        return True
    return bool(_NAME_RE.fullmatch(s))


def _parse_code_holdings(lines: list[str]) -> list[dict]:
    """解析截图中直接展示六位代码的常见布局。"""
    holdings: list[dict] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        match = _CODE_RE.search(line)
        if not match:
            continue
        code = match.group(1)
        if not _is_valid_code(code) or code in seen:
            continue
        seen.add(code)

        name = ""
        for j in range(i - 1, max(i - 4, -1), -1):
            candidate = lines[j]
            if _is_name_line(candidate) and not _CODE_RE.search(candidate):
                name = candidate
                break

        tail = lines[i + 1:min(i + 10, len(lines))]
        floats, ints = [], []
        for item in tail:
            if _is_name_line(item) and floats:
                break
            value = _number(item)
            if value is None or "%" in item:
                continue
            if _PRICE_RE.match(_normalise_line(item)):
                floats.append(value)
            elif _SHARES_RE.match(_normalise_line(item)):
                ivalue = int(value)
                if ivalue >= 100 and ivalue % 100 == 0:
                    ints.append(ivalue)

        holdings.append({
            "code": code,
            "name": name or code,
            "cost": str(floats[1]) if len(floats) >= 2 else "",
            "shares": str(ints[0]) if ints else "",
        })
    return holdings


def _parse_name_holdings(lines: list[str], name_code_map: dict[str, str] | None = None) -> list[dict]:
    """解析仅展示名称的持仓行，兼容按市值/现价推算股数的 App 布局。"""
    known_names = set(name_code_map or {})
    holdings: list[dict] = []
    i = 0
    while i < len(lines):
        name = _normalise_line(lines[i])
        if not _is_name_line(name, known_names):
            i += 1
            continue
        if name in {"华宝证券", "同花顺App"}:
            i += 1
            continue

        row: list[str] = []
        j = i + 1
        while j < len(lines) and not _is_name_line(lines[j], known_names):
            row.append(_normalise_line(lines[j]))
            j += 1
        i = j

        # 按行保留位置；逗号分隔的千位数先归一化，百分比不进入金额候选。
        tokens: list[tuple[int, float, str]] = []
        for row_index, item in enumerate(row):
            if "%" in item:
                continue
            normalized = item.replace(",", "")
            for match in re.finditer(r"(?<![\w.])-?\d+(?:\.\d+)?", normalized):
                try:
                    tokens.append((row_index, float(match.group(0)), item))
                except ValueError:
                    continue
        if not tokens:
            continue

        # 截图可能先显示浮亏，再显示持仓数、成本、市值，不能再把首个数值
        # 当成本。成本/现价应是相近的价格，市值/现价则应接近整手股数。
        price_tokens = [token for token in tokens
                        if 0 < token[1] <= 1000
                        and "." in token[2].replace(",", "")]
        best_pair = None
        for cost_index, cost, _ in price_tokens:
            for price_index, price, _ in price_tokens:
                if cost_index == price_index or not (cost * 0.4 <= price <= cost * 2.5):
                    continue
                for amount_index, amount, _ in tokens:
                    if amount <= max(cost, price) * 50:
                        continue
                    estimated_shares = amount / price
                    rounded_shares = int(round(estimated_shares / 100.0) * 100)
                    if rounded_shares < 100 or abs(estimated_shares - rounded_shares) > max(
                            2.0, rounded_shares * 0.03):
                        continue
                    score = (
                        abs(estimated_shares - rounded_shares) / rounded_shares,
                        0 if price_index > cost_index else 1,
                        abs(price - cost) / cost,
                        cost_index,
                        -price_index,
                        amount_index,
                    )
                    if best_pair is None or score < best_pair[0]:
                        best_pair = (score, cost, price, amount, rounded_shares)

        if best_pair:
            _, cost, _, _, shares = best_pair
        else:
            # 部分券商仍会展示已清仓股票；成本列明确为 0 且没有有效市值时，
            # 不把盈亏或现价误当成新持仓成本。
            first_value = _number(row[0]) if row else None
            if first_value == 0:
                continue
            cost = next((value for _, value, line in price_tokens if value > 0), None)
            shares = None
        if cost is None:
            continue

        holdings.append({
            "code": (name_code_map or {}).get(name, ""),
            "name": name,
            "cost": f"{cost:.4f}".rstrip("0").rstrip("."),
            "shares": str(shares) if shares else "",
        })
    return holdings


def _amount_tokens(line: str) -> list[float]:
    """提取非百分比的金额/盈亏数值，兼容 24,424.70 与全角数字。"""
    normalized = _normalise_line(line).replace(",", "")
    values = []
    for match in re.finditer(r"(?<![\w.])-?\d+(?:\.\d+)?", normalized):
        if normalized[match.end():].lstrip().startswith("%"):
            continue
        try:
            values.append(float(match.group(0)))
        except ValueError:
            continue
    return values


def _summary_values(lines: list[str], start: int, end: int, label: str) -> list[float]:
    values = []
    for index in range(start, min(end, len(lines))):
        line = lines[index]
        if index == start and label in line:
            line = line.split(label, 1)[1]
        values.extend(_amount_tokens(line))
    return values


def parse_account_summary(text: str) -> dict:
    """识别券商持仓页账户汇总字段；缺失值保留为 None，不猜测。"""
    empty = {
        "total_assets": None,
        "total_market_value": None,
        "total_profit": None,
        "today_profit": None,
        "today_profit_pct": None,
        "available_funds": None,
        "withdrawable_funds": None,
    }
    if not text:
        return empty
    lines = [_normalise_line(line) for line in str(text).splitlines() if _normalise_line(line)]
    asset_index = next((i for i, line in enumerate(lines) if "总资产" in line), None)
    market_index = next((i for i, line in enumerate(lines)
                         if "总市值" in line and (asset_index is None or i > asset_index)), None)
    holdings_index = next((i for i, line in enumerate(lines)
                           if "持仓股" in line and (market_index is None or i > market_index)), len(lines))
    result = dict(empty)

    if asset_index is not None and market_index is not None and market_index > asset_index:
        values = _summary_values(lines, asset_index, market_index, "总资产")
        if len(values) >= 1:
            result["total_assets"] = values[0]
        if len(values) >= 2:
            result["total_profit"] = values[1]
        if len(values) >= 3:
            result["today_profit"] = values[2]
        percentages = []
        for line in lines[asset_index:market_index]:
            percentages.extend(float(value) for value in re.findall(
                r"(-?\d+(?:\.\d+)?)\s*%", line.replace(",", "")))
        if percentages:
            result["today_profit_pct"] = percentages[-1]

    if market_index is not None and holdings_index > market_index:
        values = _summary_values(lines, market_index, holdings_index, "总市值")
        if len(values) >= 1:
            result["total_market_value"] = values[0]
        if len(values) >= 2:
            result["available_funds"] = values[1]
        if len(values) >= 3:
            result["withdrawable_funds"] = values[2]
    return result


def parse_portfolio_screenshot(text: str, name_code_map: dict[str, str] | None = None) -> dict:
    """组合解析账户汇总和持仓明细，供上传预览统一展示。"""
    return {
        "account": parse_account_summary(text),
        "holdings": parse_ths_holdings(text, name_code_map=name_code_map),
    }


def parse_ths_holdings(text: str, name_code_map: dict[str, str] | None = None) -> list[dict]:
    """解析持仓截图，返回 code/name/cost/shares，无法确定的字段留空。"""
    if not text:
        return []
    lines = [_normalise_line(line) for line in str(text).splitlines() if _normalise_line(line)]
    code_holdings = _parse_code_holdings(lines)
    if code_holdings:
        return code_holdings
    return _parse_name_holdings(lines, name_code_map=name_code_map)


__all__ = ["ocr_image", "parse_account_summary", "parse_portfolio_screenshot", "parse_ths_holdings"]
