#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enhanced Daily Market Hotspot Report Generator
Deep analysis, actionable insights, quality commentary.
"""

import argparse, json, logging, os, re, sys, time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
os.environ.setdefault('TQDM_DISABLE', '1')  # 禁用tqdm，避免非TTY BrokenPipeError
import akshare as ak
import pandas as pd

logger = logging.getLogger("daily_hotspot")

DEEPSEEK_BASE = "https://api.deepseek.com/anthropic"
MAX_INPUT_CHARS = 25000

# ---------------------------------------------------------------------------
# Enhanced LLM Prompt
# ---------------------------------------------------------------------------
HOTSPOT_SYSTEM_PROMPT = """You are a senior A-share market strategist writing a daily hotspot deep-dive for WeChat.

Your report must help readers understand:
1. WHAT happened — which sectors/stocks are hot
2. WHY it happened — the driving logic (policy, earnings, capital flows, supply-demand)
3. WHAT NEXT — actionable watch points for tomorrow

## Analysis Rules
- Connect concepts to specific stocks: e.g., "算力概念爆发 → 太极实业(600667)封板9亿、长电科技(600584)封板8.8亿"
- Explain sector rotation logic: which sectors gained, which lost, and the capital flow narrative
- Analyze limit-up patterns: first-time vs consecutive boards,封板资金 quality, retail vs institutional
- Identify hidden risks: over-concentration, low sustainability signals
- Keep all numbers, codes, percentages ABSOLUTELY unchanged

## Output Format (JSON only, no markdown wrapper)
{
  "title": "10-25 Chinese chars, highlight the hottest theme with specific data",
  "hotspot_overview": "200-300 words: comprehensive analysis — main themes, driving catalysts, capital flow patterns, risk alerts. Use specific stock names and numbers.",
  "concept_deep_dive": "100-200 words: deep analysis of the #1 concept — why it's leading, which stocks benefited, sustainability assessment",
  "limit_up_analysis": "100-200 words:涨停 quality assessment — 封板资金 concentration,连板 structure, institutional vs retail participation signals",
  "sector_rotation": "100-200 words: capital flow between sectors — who gained, who lost, what it means for tomorrow",
  "tomorrow_watch": "A SINGLE STRING containing 3-5 action items separated by newlines. Use format: - item1\\n- item2\\n- item3. DO NOT use a JSON array."
}"""


def call_deepseek(prompt: str, api_key: str, timeout: int = 120) -> Optional[dict]:
    headers = {"Content-Type": "application/json", "x-api-key": api_key}
    payload = {
        "model": "deepseek-chat",
        "max_tokens": 4096, "temperature": 0.9,
        "system": HOTSPOT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(f"{DEEPSEEK_BASE}/v1/messages", headers=headers, json=payload, timeout=timeout)
        if r.status_code != 200:
            logger.error(f"DeepSeek API: {r.status_code}")
            return None
        data = r.json()
        content = data.get("content", [])
        if isinstance(content, list):
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        else:
            text = str(content)
        m = re.search(r'\{.*\}', text, re.DOTALL)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return None


# ---------------------------------------------------------------------------
# Data Fetching with Retry
# ---------------------------------------------------------------------------

def fetch_with_retry(name, fn, max_retries=2):
    """Fetch data with retry on connection errors"""
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                time.sleep(2 * attempt)
            df = fn()
            if df is not None and len(df) > 0:
                logger.info(f"{name}: {len(df)} rows")
                return df
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"{name} attempt {attempt+1} failed, retrying...")
            else:
                logger.warning(f"{name} failed after {max_retries+1} attempts: {e}")
    return None


def fetch_hot_stocks():
    return fetch_with_retry("Hot stocks", lambda: ak.stock_hot_rank_em())

def fetch_hot_keywords():
    time.sleep(0.5)
    return fetch_with_retry("Hot keywords", lambda: ak.stock_hot_keyword_em())

def fetch_limit_up_pool(today_str):
    time.sleep(0.5)
    return fetch_with_retry("Limit-up pool", lambda: ak.stock_zt_pool_em(date=today_str))

def fetch_board_changes():
    time.sleep(0.5)
    return fetch_with_retry("Board changes", lambda: ak.stock_board_change_em())

def fetch_fund_flow_sector():
    """Try to get sector fund flow"""
    time.sleep(0.5)
    try:
        df = ak.stock_sector_fund_flow_rank(indicator='今日')
        return df
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Report Building
# ---------------------------------------------------------------------------

def build_report(hot_stocks, hot_keywords, limit_up, board_changes, ai) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []

    # Title
    title = ai.get("title", f"每日股市热点 · {today}") if ai else f"每日股市热点 · {today}"
    lines.append(f"# {title}\n")
    lines.append(f"> 数据时间：{today} 收盘后 | AI策略分析\n\n")

    # === Hotspot Overview ===
    if ai and ai.get("hotspot_overview"):
        lines.append(f"## 一、热点深度解读\n\n")
        lines.append(f"{ai['hotspot_overview']}\n\n")

    # === Hot Concepts ===
    lines.append(f"## 二、热门概念排名\n\n")
    if hot_keywords is not None and len(hot_keywords) > 0:
        top_kw = hot_keywords.head(8)
        lines.append(f"| 排名 | 概念 | 热度 |\n")
        lines.append(f"|------|------|------|\n")
        for i, row in enumerate(top_kw.iterrows()):
            r = row[1]
            kw = str(r.get("概念名称", r.iloc[2] if len(r) > 2 else "-"))
            hot_val = str(r.get("热度", r.iloc[4] if len(r) > 4 else "-"))
            lines.append(f"| {i+1} | {kw} | {hot_val} |\n")
        lines.append(f"\n")
        if ai and ai.get("concept_deep_dive"):
            lines.append(f"{ai['concept_deep_dive']}\n\n")

    # === Hot Stocks ===
    lines.append(f"## 三、热门个股\n\n")
    if hot_stocks is not None and len(hot_stocks) > 0:
        top15 = hot_stocks.head(15)
        lines.append(f"| 排名 | 代码 | 名称 | 最新价 | 涨跌幅 |\n")
        lines.append(f"|------|------|------|--------|--------|\n")
        for i, row in enumerate(top15.iterrows()):
            r = row[1]
            code = str(r.get("代码", "")).replace("sz","").replace("sh","").replace("bj","").zfill(6)
            name = str(r.get("名称", ""))
            price = r.get("最新价", "-")
            chg = r.get("涨跌幅", 0)
            if isinstance(price, (int, float)):
                price = f"{price:.2f}"
            if isinstance(chg, (int, float)):
                chg = f"{chg:+.2f}%"
            lines.append(f"| {i+1} | {code} | {name} | {price} | {chg} |\n")
        lines.append(f"\n")
    else:
        lines.append(f"> 今日热门个股数据暂不可用，可参考下方涨停板分析\n\n")

    # === Limit-Up Analysis ===
    lines.append(f"## 四、涨停板深度分析\n\n")
    if limit_up is not None and len(limit_up) > 0:
        total = len(limit_up)
        lines.append(f"今日涨停 **{total} 只**\n\n")

        # Board distribution
        if "连板数" in limit_up.columns:
            lb_counts = limit_up["连板数"].value_counts().sort_index()
            lines.append(f"| 连板数 | 数量 | 特征 |\n")
            lines.append(f"|--------|------|------|\n")
            for lb, cnt in lb_counts.items():
                if lb == 1: feat = "首板（新进资金）"
                elif lb <= 3: feat = "连板（强势延续）"
                else: feat = "高位板（注意风险）"
                lines.append(f"| {lb}板 | {cnt} 只 | {feat} |\n")
            lines.append(f"\n")

        # Top封板资金
        if "封板资金" in limit_up.columns:
            top_zt = limit_up.nlargest(10, "封板资金")
            lines.append(f"### 封板资金 Top 10（资金认可度排名）\n\n")
            lines.append(f"| 代码 | 名称 | 封板资金(亿) | 连板数 | 资金信号 |\n")
            lines.append(f"|------|------|-------------|--------|----------|\n")
            for _, row in top_zt.iterrows():
                code = str(row.get("代码", "")).replace("sz","").replace("sh","").replace("bj","").zfill(6)
                name = str(row.get("名称", ""))
                fb = row.get("封板资金", 0) / 1e8 if row.get("封板资金", 0) > 0 else 0
                lb = row.get("连板数", "-")
                if fb >= 5: signal = "强认可"
                elif fb >= 2: signal = "中等"
                else: signal = "偏弱"
                lines.append(f"| {code} | {name} | {fb:.2f} | {lb} | {signal} |\n")
            lines.append(f"\n")

        if ai and ai.get("limit_up_analysis"):
            lines.append(f"{ai['limit_up_analysis']}\n\n")

    # === Sector Changes ===
    lines.append(f"## 五、板块轮动\n\n")
    if board_changes is not None and len(board_changes) > 0:
        if "涨跌幅" in board_changes.columns:
            top_up = board_changes.nlargest(8, "涨跌幅")
            lines.append(f"### 涨幅居前\n\n")
            lines.append(f"| 板块 | 涨跌幅 |\n")
            lines.append(f"|------|--------|\n")
            for _, row in top_up.iterrows():
                bname = str(row.get("板块名称", row.get("名称", "-")))
                bchg = row.get("涨跌幅", 0)
                if isinstance(bchg, (int, float)): bchg = f"{bchg:+.2f}%"
                lines.append(f"| {bname} | {bchg} |\n")
            lines.append(f"\n")

            top_down = board_changes.nsmallest(5, "涨跌幅")
            lines.append(f"### 跌幅居前\n\n")
            lines.append(f"| 板块 | 涨跌幅 |\n")
            lines.append(f"|------|--------|\n")
            for _, row in top_down.iterrows():
                bname = str(row.get("板块名称", row.get("名称", "-")))
                bchg = row.get("涨跌幅", 0)
                if isinstance(bchg, (int, float)): bchg = f"{bchg:.2f}%"
                lines.append(f"| {bname} | {bchg} |\n")
            lines.append(f"\n")

        if ai and ai.get("sector_rotation"):
            lines.append(f"{ai['sector_rotation']}\n\n")

    # === Tomorrow Watch ===
    if ai and ai.get("tomorrow_watch"):
        lines.append(f"## 六、明日关注\n\n")
        tw = ai.get('tomorrow_watch', '')
        if isinstance(tw, list):
            for item in tw:
                lines.append(f"{item}\n")
        elif isinstance(tw, str):
            lines.append(f"{tw}\n")
        lines.append("\n")

    # Footer
    lines.append(f"---\n\n")
    lines.append(f"*数据来源：东方财富 | AI策略分析仅供参考，不构成投资建议*\n")

    return "".join(lines)


def auto_publish(report_path, project_root):
    import subprocess
    polish = project_root / "scripts" / "polish_report.py"
    publish = project_root / "scripts" / "wechat_mp_publish.py"
    if polish.exists():
        logger.info("Polishing...")
        subprocess.run(["python3", str(polish), "--type", "hotspot"], capture_output=True, text=True, timeout=120, cwd=str(project_root))
    if publish.exists():
        logger.info("Publishing...")
        subprocess.run(["python3", str(publish), "--report", report_path], capture_output=True, text=True, timeout=120, cwd=str(project_root))
        logger.info("Published! Check mp.weixin.qq.com drafts")


def main():
    parser = argparse.ArgumentParser(description="Enhanced Daily Hotspot Report")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent if script_dir.name == "scripts" else Path.cwd()
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_dir / "hotspot.log", encoding="utf-8")])

    logger.info("=" * 50)
    logger.info("Enhanced Hotspot Report Generator")
    logger.info("=" * 50)

    # Load .env
    env_file = project_root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

    api_key = args.api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
    today_str = datetime.now().strftime("%Y%m%d")
    today_display = datetime.now().strftime("%Y-%m-%d")

    # Fetch data
    logger.info("Fetching data...")
    hot_keywords = fetch_hot_keywords()
    limit_up = fetch_limit_up_pool(today_str)
    board_changes = fetch_board_changes()
    hot_stocks = fetch_hot_stocks()  # May fail, that's ok

    if all(x is None for x in [hot_stocks, hot_keywords, limit_up, board_changes]):
        logger.error("All data sources failed!")
        sys.exit(1)

    # LLM Analysis
    ai = None
    if api_key and not args.no_llm:
        prompt_parts = [f"Analyze A-share market hotspots for {today_display}:\n\n"]

        if hot_keywords is not None:
            prompt_parts.append("## Hot Concepts:\n")
            for _, r in hot_keywords.head(8).iterrows():
                kw = str(r.get("概念名称", ""))
                hot = str(r.get("热度", ""))
                prompt_parts.append(f"- {kw}: heat={hot}\n")
            prompt_parts.append("\n")

        if hot_stocks is not None:
            prompt_parts.append("## Hot Stocks Top 15:\n")
            for _, r in hot_stocks.head(15).iterrows():
                code = str(r.get("代码", "")).replace("sz","").replace("sh","").replace("bj","").zfill(6)
                name = str(r.get("名称", ""))
                chg = r.get("涨跌幅", 0)
                prompt_parts.append(f"- {code} {name}: chg={chg}\n")
            prompt_parts.append("\n")

        if limit_up is not None:
            prompt_parts.append(f"## Limit-Up: {len(limit_up)} stocks\n")
            if "连板数" in limit_up.columns:
                lb = limit_up["连板数"].value_counts().to_dict()
                prompt_parts.append(f"Board distribution: {lb}\n")
            if "封板资金" in limit_up.columns:
                top5 = limit_up.nlargest(5, "封板资金")
                prompt_parts.append("Top 5 by封板资金:\n")
                for _, r in top5.iterrows():
                    prompt_parts.append(f"  {r.get('名称','')} {r.get('代码','')}: {r.get('封板资金',0)/1e8:.1f}亿,连板{r.get('连板数','-')}\n")
            prompt_parts.append("\n")

        if board_changes is not None:
            prompt_parts.append("## Sector Leaders:\n")
            if "涨跌幅" in board_changes.columns:
                for _, r in board_changes.nlargest(5, "涨跌幅").iterrows():
                    prompt_parts.append(f"  {r.get('板块名称','')}: {r.get('涨跌幅',0)}%\n")
            prompt_parts.append("## Sector Laggards:\n")
            if "涨跌幅" in board_changes.columns:
                for _, r in board_changes.nsmallest(5, "涨跌幅").iterrows():
                    prompt_parts.append(f"  {r.get('板块名称','')}: {r.get('涨跌幅',0)}%\n")
            prompt_parts.append("\n")

        prompt = "".join(prompt_parts)[:MAX_INPUT_CHARS]

        if args.dry_run:
            logger.info("DRY RUN prompt (%d chars)", len(prompt))
        else:
            logger.info("Calling DeepSeek for deep analysis...")
            start = time.time()
            ai = call_deepseek(prompt, api_key)
            logger.info(f"LLM: {time.time()-start:.1f}s")
            if ai:
                logger.info(f"Title: {ai.get('title', 'N/A')}")

    # Build & Save
    report = build_report(hot_stocks, hot_keywords, limit_up, board_changes, ai)
    reports_dir = project_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"hotspot_{today_str}.md"
    report_path.write_text(report, encoding="utf-8")

    title = ai.get("title", f"每日股市热点 · {today_display}") if ai else f"每日股市热点 · {today_display}"
    (reports_dir / ".today_hotspot_title.txt").write_text(title, encoding="utf-8")

    logger.info(f"Report: {report_path} ({len(report)} chars)")
    logger.info(f"Title: {title}")

    if args.dry_run:
        print(report[:1500])
        return

    logger.info("Done!")
    if args.publish:
        auto_publish(str(report_path), project_root)


if __name__ == "__main__":
    main()
