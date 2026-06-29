#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日金股推荐 v3 —— 全市场扫描 + ZGNB 四策 + 丰富数据报告
============================================================

改进：
  1. 全市场 5000+ 扫描（非仅自选股）
  2. 多维度筛选：活跃度、技术形态、战法信号
  3. LLM 报告包含更丰富的依据
  4. 自适应数据源（stock_zh_a_daily 优先，stock_zh_a_hist_tx 兜底）

用法：
  python scripts/zgnb_daily_picks.py                    # 正常扫描
  python scripts/zgnb_daily_picks.py --dry-run          # 仅筛选不调LLM
  python scripts/zgnb_daily_picks.py --candidates 100   # 候选股数量
  python scripts/zgnb_daily_picks.py --top 5            # 推荐 Top N
"""

from __future__ import annotations

os.environ.setdefault('TQDM_DISABLE', '1')  # 禁用tqdm，避免非TTY BrokenPipeError

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

try:
    from scipy.signal import find_peaks as _scipy_find_peaks
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

logger = logging.getLogger("zgnb_picks")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEEPSEEK_BASE = "https://api.deepseek.com/anthropic"
MAX_CANDIDATES = 200       # 进入策略扫描的最大候选股
MIN_PRICE = 3.0            # 最低股价
MIN_AMOUNT = 50_000_000    # 最低成交额 5000万

# ---------------------------------------------------------------------------
# ZGNB 技术指标（移植自 Selector.py）
# ---------------------------------------------------------------------------

def _compute_kdj(df: pd.DataFrame, n: int = 9) -> pd.DataFrame:
    if df.empty:
        return df.assign(K=np.nan, D=np.nan, J=np.nan)
    low_n = df["low"].rolling(window=n, min_periods=1).min()
    high_n = df["high"].rolling(window=n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n + 1e-9) * 100
    K, D = np.zeros_like(rsv, dtype=float), np.zeros_like(rsv, dtype=float)
    for i in range(len(df)):
        if i == 0:
            K[i] = D[i] = 50.0
        else:
            K[i] = 2 / 3 * K[i - 1] + 1 / 3 * rsv.iloc[i]
            D[i] = 2 / 3 * D[i - 1] + 1 / 3 * K[i]
    J = 3 * K - 2 * D
    return df.assign(K=K, D=D, J=J)


def _compute_bbi(df: pd.DataFrame) -> pd.Series:
    return (df["close"].rolling(3).mean() + df["close"].rolling(6).mean() +
            df["close"].rolling(12).mean() + df["close"].rolling(24).mean()) / 4


def _compute_rsv(df: pd.DataFrame, n: int) -> pd.Series:
    low_n = df["low"].rolling(window=n, min_periods=1).min()
    high_close_n = df["close"].rolling(window=n, min_periods=1).max()
    return (df["close"] - low_n) / (high_close_n - low_n + 1e-9) * 100.0


def _compute_dif(df: pd.DataFrame, fast: int = 12, slow: int = 26) -> pd.Series:
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    return ema_fast - ema_slow


def _compute_rsi(df: pd.DataFrame, n: int = 14) -> float:
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(n).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(n).mean()
    rs = gain.iloc[-1] / (loss.iloc[-1] + 1e-9)
    return float(100 - (100 / (1 + rs)))


def _bbi_deriv_uptrend(bbi: pd.Series, *, min_window: int, max_window: Optional[int] = None,
                        q_threshold: float = 0.0) -> bool:
    bbi = bbi.dropna()
    if len(bbi) < min_window:
        return False
    longest = min(len(bbi), max_window or len(bbi))
    for w in range(longest, min_window - 1, -1):
        seg = bbi.iloc[-w:]
        norm = seg / seg.iloc[0]
        diffs = np.diff(norm.values)
        if np.quantile(diffs, q_threshold) >= 0:
            return True
    return False


# ---------------------------------------------------------------------------
# 四大战法
# ---------------------------------------------------------------------------

class BBIKDJSelector:
    """少妇战法"""
    def __init__(self, j_threshold=1, bbi_min_window=20, max_window=60,
                 price_range_pct=0.5, bbi_q_threshold=0.1, j_q_threshold=0.10):
        self.j_threshold = j_threshold; self.bbi_min_window = bbi_min_window
        self.max_window = max_window; self.price_range_pct = price_range_pct
        self.bbi_q_threshold = bbi_q_threshold; self.j_q_threshold = j_q_threshold

    def check(self, hist: pd.DataFrame) -> bool:
        hist = hist.copy()
        hist["BBI"] = _compute_bbi(hist)
        win = hist.tail(self.max_window)
        high, low = win["close"].max(), win["close"].min()
        if low <= 0 or (high / low - 1) > self.price_range_pct:
            return False
        if not _bbi_deriv_uptrend(hist["BBI"], min_window=self.bbi_min_window,
                                   max_window=self.max_window, q_threshold=self.bbi_q_threshold):
            return False
        kdj = _compute_kdj(hist)
        j_today = float(kdj.iloc[-1]["J"])
        j_window = kdj["J"].tail(self.max_window).dropna()
        if j_window.empty: return False
        if not (j_today < self.j_threshold or j_today <= float(j_window.quantile(self.j_q_threshold))):
            return False
        hist["DIF"] = _compute_dif(hist)
        return bool(hist["DIF"].iloc[-1] > 0)


class PeakKDJSelector:
    """填坑战法"""
    def __init__(self, j_threshold=10, max_window=100, fluc_threshold=0.03,
                 j_q_threshold=0.10, gap_threshold=0.2):
        if not HAS_SCIPY:
            raise ImportError("需要 scipy")
        self.j_threshold = j_threshold; self.max_window = max_window
        self.fluc_threshold = fluc_threshold; self.gap_threshold = gap_threshold
        self.j_q_threshold = j_q_threshold

    def check(self, hist: pd.DataFrame) -> bool:
        if hist.empty: return False
        hist = hist.copy().sort_values("date")
        hist["oc_max"] = hist[["open", "close"]].max(axis=1)
        y = hist["oc_max"].to_numpy()
        indices, _ = _scipy_find_peaks(y, distance=6, prominence=0.5)
        if len(indices) < 2: return False
        peaks_df = hist.iloc[indices].copy()
        date_today = hist.iloc[-1]["date"]
        peaks_df = peaks_df[peaks_df["date"] < date_today]
        if len(peaks_df) < 2: return False
        peak_t = peaks_df.iloc[-1]; peaks_list = peaks_df.reset_index(drop=True)
        oc_t = peak_t.oc_max; total_peaks = len(peaks_list)
        target_peak = None
        for idx in range(total_peaks - 2, -1, -1):
            peak_prev = peaks_list.loc[idx]; oc_prev = peak_prev.oc_max
            if oc_t <= oc_prev: continue
            if total_peaks >= 3 and idx < total_peaks - 2:
                inter_oc = peaks_list.loc[idx+1:total_peaks-2, "oc_max"]
                if not (inter_oc < oc_prev).all(): continue
            date_prev = peak_prev.date
            mask = (hist["date"] > date_prev) & (hist["date"] < peak_t.date)
            min_close = hist.loc[mask, "close"].min()
            if pd.isna(min_close): continue
            if oc_prev <= min_close * (1 + self.gap_threshold): continue
            target_peak = peak_prev; break
        if target_peak is None: return False
        close_today = hist.iloc[-1]["close"]
        fluc_pct = abs(close_today - target_peak.close) / target_peak.close
        if fluc_pct > self.fluc_threshold: return False
        kdj = _compute_kdj(hist)
        j_today = float(kdj.iloc[-1]["J"])
        j_window = kdj["J"].tail(self.max_window).dropna()
        if j_window.empty: return False
        if not (j_today < self.j_threshold or j_today <= float(j_window.quantile(self.j_q_threshold))):
            return False
        return True


class BBIShortLongSelector:
    """补票战法"""
    def __init__(self, n_short=3, n_long=21, m=3, bbi_min_window=2,
                 max_window=60, bbi_q_threshold=0.2):
        self.n_short = n_short; self.n_long = n_long; self.m = m
        self.bbi_min_window = bbi_min_window; self.max_window = max_window
        self.bbi_q_threshold = bbi_q_threshold

    def check(self, hist: pd.DataFrame) -> bool:
        hist = hist.copy()
        hist["BBI"] = _compute_bbi(hist)
        if not _bbi_deriv_uptrend(hist["BBI"], min_window=self.bbi_min_window,
                                   max_window=self.max_window, q_threshold=self.bbi_q_threshold):
            return False
        hist["RSV_short"] = _compute_rsv(hist, self.n_short)
        hist["RSV_long"] = _compute_rsv(hist, self.n_long)
        if len(hist) < self.m: return False
        win = hist.iloc[-self.m:]
        long_ok = (win["RSV_long"] >= 80).all()
        ss = win["RSV_short"]
        if not (long_ok and ss.iloc[0] >= 80 and ss.iloc[-1] >= 80 and (ss < 20).any()):
            return False
        hist["DIF"] = _compute_dif(hist)
        return bool(hist["DIF"].iloc[-1] > 0)


class BreakoutVolumeKDJSelector:
    """TePu战法（放量突破）"""
    def __init__(self, j_threshold=1, up_threshold=3.0, volume_threshold=0.6667,
                 offset=15, max_window=60, price_range_pct=0.5, j_q_threshold=0.10):
        self.j_threshold = j_threshold; self.up_threshold = up_threshold
        self.volume_threshold = volume_threshold; self.offset = offset
        self.max_window = max_window; self.price_range_pct = price_range_pct
        self.j_q_threshold = j_q_threshold

    def check(self, hist: pd.DataFrame) -> bool:
        if len(hist) < self.offset + 2: return False
        hist = hist.tail(self.max_window).copy()
        high, low = hist["close"].max(), hist["close"].min()
        if low <= 0 or (high / low - 1) > self.price_range_pct: return False
        hist = _compute_kdj(hist)
        hist["pct_chg"] = hist["close"].pct_change() * 100
        hist["DIF"] = _compute_dif(hist)
        j_today = float(hist["J"].iloc[-1])
        j_window = hist["J"].tail(self.max_window).dropna()
        if j_window.empty: return False
        j_quantile = float(j_window.quantile(self.j_q_threshold))
        if not (j_today < self.j_threshold or j_today <= j_quantile): return False
        if hist["DIF"].iloc[-1] <= 0: return False
        n = len(hist); wnd_start = max(0, n - self.offset - 1); last_idx = n - 1
        # Use amount/close as volume proxy if volume column missing
        vol_col = "volume" if "volume" in hist.columns else "amount"
        for t_idx in range(wnd_start, last_idx):
            row = hist.iloc[t_idx]
            if row["pct_chg"] < self.up_threshold: continue
            vol_T = row[vol_col]
            if vol_T <= 0: continue
            vols_except_T = hist[vol_col].drop(index=hist.index[t_idx])
            if not (vols_except_T <= self.volume_threshold * vol_T).all(): continue
            if row["close"] <= hist["close"].iloc[:t_idx].max(): continue
            if not (hist["J"].iloc[t_idx:last_idx] > hist["J"].iloc[-1] - 10).all(): continue
            return True
        return False


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------

def fetch_kline(code: str, name: str = "") -> Optional[pd.DataFrame]:
    """多源获取K线数据，stock_zh_a_hist_tx(腾讯)优先，stock_zh_a_daily(新浪)兜底"""
    import akshare as ak

    today_str = datetime.now().strftime("%Y%m%d")
    tx_symbol = f"sz{code}" if code.startswith(("0", "3", "2")) else f"sh{code}"
    sina_symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"

    # 源1: stock_zh_a_hist_tx (腾讯源，稳定，但无volume列)
    try:
        df = ak.stock_zh_a_hist_tx(symbol=tx_symbol, start_date="2026-01-01",
                                   end_date=f"{today_str[:4]}-{today_str[4:6]}-{today_str[6:8]}", adjust="qfq")
        if df is not None and not df.empty and "close" in df.columns:
            df = df.tail(120).copy()
            if "volume" not in df.columns and "amount" in df.columns:
                df["volume"] = df["amount"] / df["close"]
            elif "volume" not in df.columns:
                df["volume"] = df["close"] * 100  # fallback estimate
            # Ensure required columns exist
            for col in ["date", "open", "high", "low", "close", "volume"]:
                if col not in df.columns:
                    if col == "volume":
                        df[col] = 0
                    else:
                        return None  # missing essential column
            return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e1:
        pass  # tx source failed, try next

    # 源2: stock_zh_a_daily (新浪源，有volume)
    try:
        df = ak.stock_zh_a_daily(symbol=sina_symbol, adjust="qfq")
        if df is not None and not df.empty and "close" in df.columns:
            df = df.tail(120).copy()
            if "volume" not in df.columns and "amount" in df.columns:
                df["volume"] = df["amount"] / df["close"]
            return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e2:
        pass  # sina source failed

    return None


def get_market_snapshot() -> pd.DataFrame:
    """获取全市场实时行情快照"""
    import akshare as ak
    df = ak.stock_zh_a_spot()
    # 列名统一
    for col in ['代码', '名称', '最新价', '涨跌幅', '成交量', '成交额']:
        if col not in df.columns:
            raise KeyError(f"缺少列: {col}")
    return df


@dataclass
class StockSignal:
    """单只股票的策略扫描结果"""
    code: str
    name: str
    close: float = 0
    chg_pct: float = 0
    amount: float = 0           # 成交额
    turnover: float = 0          # 换手率(若有)
    bbi_kdj: bool = False
    peak_kdj: bool = False
    bbi_short_long: bool = False
    breakout_vol: bool = False
    hit_count: int = 0
    score: float = 0
    ma_trend: str = ""
    ma5: float = 0; ma10: float = 0; ma20: float = 0
    rsi: float = 50
    vol_ratio: float = 1.0
    chg_5d: float = 0
    near_ma_support: bool = False  # 是否在均线附近

    @property
    def strategy_names(self) -> str:
        hits = []
        if self.bbi_kdj: hits.append("少妇战法")
        if self.peak_kdj: hits.append("填坑战法")
        if self.bbi_short_long: hits.append("补票战法")
        if self.breakout_vol: hits.append("TePu战法")
        return "+".join(hits) if hits else "无"


# ---------------------------------------------------------------------------
# 主扫描逻辑
# ---------------------------------------------------------------------------

def scan_market(candidates: int = MAX_CANDIDATES, dry_run: bool = False) -> List[StockSignal]:
    """全市场扫描主函数"""

    logger.info("1/4 获取全市场行情快照...")
    t0 = time.time()
    snapshot = get_market_snapshot()
    logger.info("    %d 只股票, %.1fs", len(snapshot), time.time() - t0)

    # 过滤
    logger.info("2/4 初筛过滤...")
    df = snapshot.copy()
    df['最新价'] = pd.to_numeric(df['最新价'], errors='coerce')
    df['涨跌幅'] = pd.to_numeric(df['涨跌幅'], errors='coerce')
    df['成交量'] = pd.to_numeric(df['成交量'], errors='coerce')
    df['成交额'] = pd.to_numeric(df['成交额'], errors='coerce')

    mask = (
        ~df['名称'].str.contains('ST|退|N', na=True) &
        (df['最新价'] >= MIN_PRICE) &
        (df['最新价'] <= 300) &
        (df['成交额'] >= MIN_AMOUNT) &
        (df['成交量'] > 0)
    )
    pool = df[mask].copy(); filter_count = len(pool)
    logger.info("    过滤后: %d 只 (价格>%.0f, 成交额>%.0f万, 排除ST)",
                len(pool), MIN_PRICE, MIN_AMOUNT / 10000)

    # 按成交额排序，取活跃股
    pool = pool.sort_values('成交额', ascending=False).head(candidates)
    logger.info("    Top %d 活跃股进入策略扫描", len(pool))

    # 逐个获取K线并扫描
    logger.info("3/4 获取K线 + ZGNB四策扫描...")
    signals: List[StockSignal] = []
    strategies = [
        ("少妇", BBIKDJSelector()),
        ("补票", BBIShortLongSelector()),
        ("TePu", BreakoutVolumeKDJSelector()),
    ]
    if HAS_SCIPY:
        strategies.append(("填坑", PeakKDJSelector()))

    scanned = 0
    for _, row in pool.iterrows():
        code = str(row['代码']).zfill(6)
        name = str(row['名称'])
        scanned += 1

        df_kline = fetch_kline(code, name)
        if df_kline is None or len(df_kline) < 30:
            continue

        df_kline = df_kline.sort_values("date").reset_index(drop=True)
        close = df_kline["close"].astype(float)

        sig = StockSignal(
            code=code, name=name,
            close=float(close.iloc[-1]),
            chg_pct=float(row['涨跌幅']),
            amount=float(row['成交额']),
        )

        # 技术指标
        sig.ma5 = float(close.rolling(5).mean().iloc[-1])
        sig.ma10 = float(close.rolling(10).mean().iloc[-1])
        sig.ma20 = float(close.rolling(20).mean().iloc[-1])
        sig.chg_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else 0

        if sig.ma5 > sig.ma10 > sig.ma20:
            sig.ma_trend = "多头排列"
        elif sig.ma5 < sig.ma10 < sig.ma20:
            sig.ma_trend = "空头排列"
        else:
            sig.ma_trend = "均线缠绕"

        try:
            sig.rsi = _compute_rsi(df_kline)
        except Exception:
            sig.rsi = 50

        # 量比
        vol = df_kline["volume"].astype(float)
        vol_ma5 = vol.rolling(5).mean().iloc[-1]
        sig.vol_ratio = float(vol.iloc[-1] / vol_ma5) if vol_ma5 > 0 else 1.0

        # 均线支撑判断
        bias = abs((sig.close - sig.ma5) / sig.ma5 * 100) if sig.ma5 > 0 else 100
        sig.near_ma_support = bias < 3

        # 跑策略
        for label, strategy in strategies:
            try:
                passed = strategy.check(df_kline)
            except Exception:
                passed = False
            if label == "少妇": sig.bbi_kdj = passed
            elif label == "填坑": sig.peak_kdj = passed
            elif label == "补票": sig.bbi_short_long = passed
            elif label == "TePu": sig.breakout_vol = passed

        sig.hit_count = sum([sig.bbi_kdj, sig.peak_kdj, sig.bbi_short_long, sig.breakout_vol])

        # 评分
        sig.score = 30 + sig.hit_count * 15
        if sig.ma_trend == "多头排列": sig.score += 10
        elif sig.ma_trend == "空头排列": sig.score -= 5
        if sig.near_ma_support: sig.score += 5
        if 40 < sig.rsi < 65: sig.score += 5
        if sig.chg_5d > 5: sig.score += 5
        elif sig.chg_5d < -5: sig.score -= 5
        sig.score = max(0, min(100, sig.score))

        # 只保留有信号或评分较高的
        signals.append(sig)  # Keep all for ranking

        # Progress
        if dry_run and scanned <= 5:
            logger.info("    %s %s 评%.0f 战法:%s 趋势:%s",
                        code, name, sig.score, sig.strategy_names, sig.ma_trend)

        if scanned % 5 == 0:
            time.sleep(1.0)  # 每5只暂停避免限流

    signals.sort(key=lambda x: (x.hit_count, x.score), reverse=True)
    passed = sum(1 for s in signals if s.hit_count >= 1)
    logger.info("    扫描 %d 只, %d 只触发战法, %d 只评分≥50", scanned, passed, len(signals))
    # Also return pool for richer fallback when no triggers
    pool_top = pool.head(20).copy()
    total_stocks = len(snapshot)
    return signals, pool_top, total_stocks, filter_count


# ---------------------------------------------------------------------------
# LLM 报告生成
# ---------------------------------------------------------------------------

REPORT_PROMPT = """你是一位资深证券分析师，为微信公众号撰写"每日金股量化扫描"栏目。

## 任务
从全市场5000+只A股中，基于ZGNB四套量化战法（少妇/填坑/补票/TePu）扫描结果，
撰写一篇专业的选股推荐报告。

## 报告结构
```
# [吸引力标题，15-25字]
> 一句话：今日扫描概况（多少只被扫描、多少只触发信号、市场适配策略）

## 🔥 今日最强信号
（如有触发多套战法的股票，重点展开，每只150-200字）
- 触发了几套战法、分别是什么
- 技术面解读（趋势、量能、关键位、催化剂）
- 操作参考区间

## 📊 高评分潜力股
（评分≥50但未触发战法的，简要说明，每只80-120字）

## 🧠 今日选股逻辑
- 今日市场环境适配什么策略
- 为什么这些股票被选出
- 策略信号分布统计

## 📋 全市场扫描概要
- 扫描范围：全A股排除ST/低价/无成交
- 进入策略池：X只
- 触发战法：X只
- 高评分：X只

## ⚠️ 风险提示
- 量化筛选基于历史数据，不构成投资建议
- 市场有风险，投资需谨慎
```

## 要求
- **数据务必真实**：只能使用提供的数据，不编造
- 语言专业但不枯燥
- 详实的数据和推理过程
- 总字数 1000-1500

只输出JSON：{"title": "...", "content": "..."}"""


def call_llm(prompt: str, api_key: str, timeout: int = 120) -> Optional[dict]:
    headers = {"Content-Type": "application/json", "x-api-key": api_key}
    payload = {
        "model": "deepseek-chat", "max_tokens": 16384, "temperature": 0.7,
        "system": REPORT_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(f"{DEEPSEEK_BASE}/v1/messages", headers=headers,
                          json=payload, timeout=timeout)
        if r.status_code != 200:
            logger.error("API %d: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        content = data.get("content", [])
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text") if isinstance(content, list) else str(content)
        m = re.search(r'\{.*"title".*"content".*\}', text, re.DOTALL) or re.search(r'\{.*\}', text, re.DOTALL)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        logger.error("API异常: %s", e)
        return None


def build_prompt(signals: List[StockSignal], top_n: int, total_scanned: int) -> str:
    """构建丰富的LLM prompt"""
    signaled = [s for s in signals if s.hit_count >= 1]
    high_score = [s for s in signals if s.hit_count == 0 and s.score >= 50]

    lines = [f"## 全市场扫描结果", f"扫描范围: 全A股(排除ST/低价/无成交)",
             f"进入策略池: {total_scanned}只", f"触发战法: {len(signaled)}只",
             f"高评分(≥50): {len(high_score)}只\n"]

    # 信号分布
    strategy_hits = {"少妇战法": 0, "填坑战法": 0, "补票战法": 0, "TePu战法": 0}
    for s in signaled:
        if s.bbi_kdj: strategy_hits["少妇战法"] += 1
        if s.peak_kdj: strategy_hits["填坑战法"] += 1
        if s.bbi_short_long: strategy_hits["补票战法"] += 1
        if s.breakout_vol: strategy_hits["TePu战法"] += 1
    lines.append("## 战法触发统计")
    for name, count in strategy_hits.items():
        lines.append(f"- {name}: {count}只" if count > 0 else f"- {name}: 0只")
    lines.append("")

    # 最强信号
    if signaled:
        lines.append(f"## 🔥 触发战法股票详情 (共{len(signaled)}只)")
        for i, s in enumerate(signaled[:top_n]):
            lines.append(f"### {i+1}. {s.name}({s.code}) | {s.strategy_names}")
            lines.append(f"- 收盘价: {s.close:.2f} | 今日涨跌: {s.chg_pct:+.2f}% | 5日涨跌: {s.chg_5d:+.2f}%")
            lines.append(f"- 评分: {s.score:.0f}/100 | 趋势: {s.ma_trend} | RSI: {s.rsi:.1f}")
            lines.append(f"- MA5/MA10/MA20: {s.ma5:.2f}/{s.ma10:.2f}/{s.ma20:.2f} | 量比: {s.vol_ratio:.2f}")
            if s.near_ma_support: lines.append(f"- ⚡ 股价贴近MA5均线支撑位")
            lines.append(f"- 成交额: {s.amount/1e8:.2f}亿")
            lines.append("")

    # 高评分无信号
    if high_score:
        lines.append(f"## 📊 高评分潜力股 (评分≥50, 无战法信号, {len(high_score)}只)")
        lines.append("| 排名 | 股票 | 代码 | 评分 | 涨跌 | 趋势 | RSI | 量比 |")
        lines.append("|------|------|------|------|------|------|------|------|")
        for i, s in enumerate(high_score[:15]):
            lines.append(f"| {i+1} | {s.name} | {s.code} | {s.score:.0f} | {s.chg_pct:+.2f}% | {s.ma_trend} | {s.rsi:.1f} | {s.vol_ratio:.2f} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------



def _auto_publish(report_path: str, project_root, skip_polish: bool = False):
    """Polish report then publish to WeChat MP drafts.

    Steps:
      1. Run polish_report.py --type picks (unless skip_polish=True)
      2. Run wechat_mp_publish.py --report <report_path>
    """
    import subprocess

    # Step 1: Polish
    if not skip_polish:
        polish_script = project_root / "scripts" / "polish_report.py"
        if polish_script.exists():
            logger.info("✨ 第1步：AI润色报告...")
            try:
                result = subprocess.run(
                    ["python3", str(polish_script), "--type", "picks"],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(project_root),
                )
                if result.returncode == 0:
                    logger.info("✅ 润色完成")
                else:
                    logger.warning("⚠️ 润色失败 (exit=%d)，继续用原文发布", result.returncode)
            except Exception as e:
                logger.warning("⚠️ 润色异常: %s，继续用原文发布", e)
        else:
            logger.warning("⚠️ 润色脚本不存在: %s", polish_script)

    # Step 2: Publish
    publish_script = project_root / "scripts" / "wechat_mp_publish.py"
    if not publish_script.exists():
        logger.warning("⚠️ 发布脚本不存在: %s", publish_script)
        return
    logger.info("📤 第2步：发布到微信公众号草稿箱...")
    try:
        result = subprocess.run(
            ["python3", str(publish_script), "--report", report_path],
            capture_output=True, text=True, timeout=120,
            cwd=str(project_root),
        )
        if result.returncode == 0:
            logger.info("✅ 发布成功！请到 mp.weixin.qq.com 草稿箱查看")
        else:
            logger.warning("⚠️ 发布失败 (exit=%d): %s", result.returncode, result.stderr[-200:])
    except Exception as e:
        logger.warning("⚠️ 发布异常: %s", e)
def main():
    parser = argparse.ArgumentParser(description="全市场 ZGNB 四策扫描")
    parser.add_argument("--dry-run", action="store_true", help="仅扫描不调LLM")
    parser.add_argument("--candidates", type=int, default=MAX_CANDIDATES, help="候选股池大小")
    parser.add_argument("--top", type=int, default=5, help="推荐数量")
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--publish", action="store_true",
                        help="生成后自动发布到微信公众号草稿箱")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent if script_dir.name == "scripts" else Path.cwd()
    reports_dir = project_root / "reports"
    log_dir = project_root / "logs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(log_dir / "zgnb_daily_picks.log", encoding="utf-8")],
    )
    logger.info("=" * 50)
    logger.info("ZGNB 全市场扫描引擎 v3")

    # .env
    env_file = project_root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v

    api_key = args.api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()

    # 扫描
    signals, pool_top, total_stocks, filter_count = scan_market(candidates=args.candidates, dry_run=args.dry_run)

    if not signals:
        logger.warning("⚠️ 今日无符合条件的股票，生成降级报告")
        today_str = datetime.now().strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y%m%d")
        report_path = reports_dir / f"daily_picks_{today}.md"
        # Build richer fallback with pool data
        pool_lines = []
        pool_lines.append(f"# 今日全市场量化扫描：{today_str}\n")
        # total_stocks and pool_size now passed from scan_market()
        pool_lines.append(f"> 全市场 **{total_stocks} 只** A股经 ZGNB 四策扫描，Top {args.candidates} 活跃股均未触发战法信号。\n\n")
        pool_lines.append(f"市场整体偏弱，所有战法要求的BBI上升趋势或放量突破条件今日无一满足，短线机会稀缺。\n\n")
        pool_lines.append(f"## 扫描概要\n\n")
        pool_lines.append(f"| 指标 | 数值 |\n")
        pool_lines.append(f"|------|------|\n")
        pool_lines.append(f"| 全市场 | {total_stocks} 只 |\n")
        # filter_count passed from scan_market()
        pool_lines.append(f"| 初筛通过 | {filter_count} 只 |\n")
        pool_lines.append(f"| 策略扫描 | {args.candidates} 只 |\n")
        pool_lines.append(f"| 触发战法 | **0 只** 🚫 |\n\n")
        pool_lines.append(f"## 今日活跃股 Top 10（按成交额）\n\n")
        pool_lines.append(f"以下为全市场成交最活跃的股票，可作为后续观察池：\n\n")
        pool_lines.append(f"| 排名 | 代码 | 名称 | 最新价 | 涨跌幅 | 成交额(亿) |\n")
        pool_lines.append(f"|------|------|------|--------|--------|------------|\n")
        try:
            for i, (_, row) in enumerate(pool_top.head(10).iterrows()):
                code_val = str(row.get("代码", "")).replace("sz","").replace("sh","").replace("bj","").zfill(6)
                name_val = str(row.get("名称", ""))
                price_val = float(row.get("最新价", 0))
                chg_val = float(row.get("涨跌幅", 0))
                amt_val = float(row.get("成交额", 0)) / 1e8
                pool_lines.append(f"| {i+1} | {code_val} | {name_val} | {price_val:.2f} | {chg_val:+.2f}% | {amt_val:.2f} |\n")
        except Exception as e:
            pool_lines.append(f"| - | - | 数据获取异常 | - | - | - |\n")
            logger.warning(f"Pool data extraction failed: {{e}}")
        # Enhanced Top 10 analysis
        up_n = 0
        dn_n = 0
        lim_up = 0
        for _, r in pool_top.head(10).iterrows():
            try:
                chg_data = float(r.get("涨跌幅", 0))
                if chg_data > 0:
                    up_n += 1
                else:
                    dn_n += 1
                if chg_data >= 9.5:
                    lim_up += 1
            except Exception:
                pass

        pool_lines.append(f"\n## 活跃股涨跌分析\n\n")
        pool_lines.append(f"Top 10 活跃股: **{up_n}涨{dn_n}跌**")
        if lim_up > 0:
            pool_lines.append(f"，**{lim_up}只涨停**")
        pool_lines.append(f"。\n\n")
        pool_lines.append(f"活跃股表现不等于战法信号，战法对BBI趋势、KDJ/RSV位置、\n")
        pool_lines.append(f"成交量有严格要求，单日涨幅不构成触发条件。\n\n")
        pool_lines.append(f"\n## 策略解读\n\n")
        pool_lines.append(f"四套战法今日均无触发：\n\n")
        pool_lines.append(f"- **少妇战法**（BBI+KDJ）：BBI上升趋势 + KDJ的J值低位金叉 -> 无一满足\n")
        pool_lines.append(f"- **补票战法**（BBI+RSV）：短期均线上穿长期 + RSV超卖回升 -> 无一满足\n")
        pool_lines.append(f"- **TePu战法**（放量突破）：放量突破关键阻力位 -> 无一满足\n")
        pool_lines.append(f"- **填坑战法**（双峰形态）：W形双峰洗盘形态 -> 无一满足\n\n")
        pool_lines.append(f"零信号反映当前市场处于弱势存量博弈阶段，主力资金观望情绪浓厚。\n\n")
        pool_lines.append(f"## 明日观察要点\n\n")
        pool_lines.append(f"- 若市场放量企稳，优先关注上述活跃股中率先站上BBI的标的\n")
        pool_lines.append(f"- 关注资源股是否止跌、创业板能否守住关键支撑位\n")
        pool_lines.append(f"- 零信号持续超过3个交易日，建议空仓等待趋势明朗\n\n")
        pool_lines.append(f"## ⚠️ 风险提示\n\n")
        pool_lines.append(f"- 量化筛选基于历史数据和技术指标，不构成投资建议\n")
        pool_lines.append(f"- 活跃股列表仅为成交额排名，不代表买卖建议\n")
        pool_lines.append(f"- 市场有风险，投资需谨慎\n")
        fallback = "".join(pool_lines)
        report_path.write_text(fallback, encoding="utf-8")
        title = f"今日全市场扫描：{args.candidates}只活跃股无战法信号，建议观望"
        (reports_dir / ".today_picks_title.txt").write_text(title, encoding="utf-8")
        logger.info("降级报告: %s | %s", report_path, title)
        print(f"PICKS_TITLE:{title}")
        # Auto-publish if --publish flag set
        if args.publish:
            _auto_publish(str(report_path), project_root)
        sys.exit(0)

    top_signals = [s for s in signals if s.hit_count >= 1][:args.top] or signals[:args.top]

    logger.info("4/4 Top %d:", min(args.top, len(signals)))
    for i, s in enumerate(top_signals):
        logger.info("  %d. %s(%s) 评%.0f 战法:%s 趋势:%s 涨跌:%+.2f%%",
                    i+1, s.name, s.code, s.score, s.strategy_names, s.ma_trend, s.chg_pct)

    if args.dry_run:
        logger.info("✅ 试运行完成")
        return

    # LLM 生成
    prompt = build_prompt(signals, args.top, args.candidates)
    logger.info("调用 DeepSeek 生成报告...")
    t0 = time.time()
    result = call_llm(prompt, api_key)
    logger.info("API耗时: %.1fs", time.time() - t0)

    if not result:
        logger.error("❌ 生成失败")
        sys.exit(1)

    title = result.get("title", "每日金股推荐").strip()
    content = result.get("content", "").strip()
    if not content:
        logger.error("❌ 空内容")
        sys.exit(1)
    if not content.startswith("#"):
        content = f"# {title}\n\n{content}"

    today = datetime.now().strftime("%Y%m%d")
    report_path = reports_dir / f"daily_picks_{today}.md"
    report_path.write_text(content, encoding="utf-8")
    (reports_dir / ".today_picks_title.txt").write_text(title, encoding="utf-8")
    logger.info("报告: %s | 标题: %s", report_path, title)
    print(f"PICKS_TITLE:{title}")
    # Auto-publish if --publish flag set
    if args.publish:
        _auto_publish(str(report_path), project_root)
    logger.info("✅ 完成")


if __name__ == "__main__":
    main()
