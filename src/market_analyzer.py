# -*- coding: utf-8 -*-
"""
===================================
大盘复盘分析模块
===================================

职责：
1. 获取大盘指数数据（上证、深证、创业板）
2. 搜索市场新闻形成复盘情报
3. 使用大模型生成每日大盘复盘报告
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List

import pandas as pd

from src.config import get_config
from src.report_language import normalize_report_language
from src.search_service import SearchService
from src.core.market_profile import get_profile, MarketProfile
from src.core.market_strategy import get_market_strategy_blueprint
from data_provider.base import DataFetcherManager

logger = logging.getLogger(__name__)


_ENGLISH_SECTION_PATTERNS = {
    "market_summary": r"###\s*(?:1\.\s*)?Market Summary",
    "index_commentary": r"###\s*(?:2\.\s*)?(?:Index Commentary|Major Indices)",
    "sector_highlights": r"###\s*(?:4\.\s*)?(?:Sector Highlights|Sector/Theme Highlights)",
    "hot_stocks": r"###\s*(?:5\.\s*)?(?:Hot Stocks|Hot Stocks & Limit-up Ladder|Limit-up Ladder)",
}

_CHINESE_SECTION_PATTERNS = {
    "market_summary": r"###\s*一、(?:盘面总览|市场总结)",
    "index_commentary": r"###\s*二、(?:指数结构|指数点评|主要指数)",
    "sector_highlights": r"###\s*三、(?:板块主线|热点解读|板块表现)",
    "hot_stocks": r"###\s*四、(?:热门股票与连板|热门个股与连板|涨停连板|情绪梯队)",
    "funds_sentiment": r"###\s*[四五]、(?:资金与情绪|资金动向)",
    "news_catalysts": r"###\s*[五六]、(?:消息催化|后市展望)",
}


@dataclass
class MarketIndex:
    """大盘指数数据"""
    code: str                    # 指数代码
    name: str                    # 指数名称
    current: float = 0.0         # 当前点位
    change: float = 0.0          # 涨跌点数
    change_pct: float = 0.0      # 涨跌幅(%)
    open: float = 0.0            # 开盘点位
    high: float = 0.0            # 最高点位
    low: float = 0.0             # 最低点位
    prev_close: float = 0.0      # 昨收点位
    volume: float = 0.0          # 成交量（手）
    amount: float = 0.0          # 成交额（元）
    amplitude: float = 0.0       # 振幅(%)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'code': self.code,
            'name': self.name,
            'current': self.current,
            'change': self.change,
            'change_pct': self.change_pct,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'volume': self.volume,
            'amount': self.amount,
            'amplitude': self.amplitude,
        }


@dataclass
class MarketOverview:
    """市场概览数据"""
    date: str                           # 日期
    indices: List[MarketIndex] = field(default_factory=list)  # 主要指数
    up_count: int = 0                   # 上涨家数
    down_count: int = 0                 # 下跌家数
    flat_count: int = 0                 # 平盘家数
    limit_up_count: int = 0             # 涨停家数
    limit_down_count: int = 0           # 跌停家数
    total_amount: float = 0.0           # 两市成交额（亿元）
    # north_flow: float = 0.0           # 北向资金净流入（亿元）- 已废弃，接口不可用
    
    # 板块涨幅榜
    top_sectors: List[Dict] = field(default_factory=list)     # 涨幅前5板块
    bottom_sectors: List[Dict] = field(default_factory=list)  # 跌幅前5板块
    top_concepts: List[Dict] = field(default_factory=list)     # 涨幅前5概念/题材
    bottom_concepts: List[Dict] = field(default_factory=list)  # 跌幅前5概念/题材
    hot_stocks: List[Dict] = field(default_factory=list)       # 市场人气股
    limit_up_stocks: List[Dict] = field(default_factory=list)  # 涨停池/连板梯队


class MarketAnalyzer:
    """
    大盘复盘分析器
    
    功能：
    1. 获取大盘指数实时行情
    2. 获取市场涨跌统计
    3. 获取板块涨跌榜
    4. 搜索市场新闻
    5. 生成大盘复盘报告
    """
    
    def __init__(
        self,
        search_service: Optional[SearchService] = None,
        analyzer=None,
        region: str = "cn",
    ):
        """
        初始化大盘分析器

        Args:
            search_service: 搜索服务实例
            analyzer: AI分析器实例（用于调用LLM）
            region: 市场区域 cn=A股 us=美股
        """
        self.config = get_config()
        self.search_service = search_service
        self.analyzer = analyzer
        self.data_manager = DataFetcherManager()
        self.region = region if region in ("cn", "us", "hk") else "cn"
        self.profile: MarketProfile = get_profile(self.region)
        self.strategy = get_market_strategy_blueprint(self.region)

    def _get_review_language(self) -> str:
        configured = normalize_report_language(
            getattr(getattr(self, "config", None), "report_language", "zh")
        )
        if self.region == "us":
            return "en"
        return configured

    def _get_template_review_language(self) -> str:
        return normalize_report_language(
            getattr(getattr(self, "config", None), "report_language", "zh")
        )

    def _get_market_scope_name(self, review_language: str | None = None) -> str:
        review_language = review_language or self._get_review_language()
        if self.region == "us":
            return "US market"
        if self.region == "hk":
            return "Hong Kong market" if review_language == "en" else "港股市场"
        if review_language == "en":
            return "A-share market"
        return "A股市场"

    def _get_turnover_unit_label(self) -> str:
        """Return the turnover unit label for the current market/language."""
        if self.region == "us":
            return "USD bn" if self._get_review_language() == "en" else "十亿美元"
        if self.region == "hk":
            return "HKD bn" if self._get_review_language() == "en" else "十亿港元"
        return "CNY 100m" if self._get_review_language() == "en" else "亿"

    def _format_turnover_value(self, amount_raw: float) -> str:
        """Format raw turnover according to market-specific units."""
        if amount_raw == 0.0:
            return "N/A"
        if self.region in ("us", "hk"):
            return f"{amount_raw / 1e9:.2f}"
        if amount_raw > 1e6:
            return f"{amount_raw / 1e8:.0f}"
        return f"{amount_raw:.0f}"

    def _get_review_title(self, date: str) -> str:
        if self._get_review_language() == "en":
            market_names = {"us": "US Market Recap", "hk": "HK Market Recap"}
            market_name = market_names.get(self.region, "A-share Market Recap")
            return f"## {date} {market_name}"
        return f"## {date} 大盘复盘"

    def _get_index_hint(self) -> str:
        if self._get_review_language() == "en":
            if self.region == "us":
                return "Analyze the key moves in the S&P 500, Nasdaq, Dow, and other major indices."
            if self.region == "hk":
                return "Analyze the key moves in the HSI, Hang Seng Tech, HSCEI, and other major indices."
            return "Analyze the price action in the SSE, SZSE, ChiNext, and other major indices."
        return self.profile.prompt_index_hint

    def _get_strategy_prompt_block(self) -> str:
        if self.region == "hk" and self._get_review_language() == "en":
            return """## Strategy Blueprint: Hong Kong Market Regime Strategy
Focus on HSI trend, southbound flow dynamics, and sector rotation to define next-session risk posture.

### Strategy Principles
- Read market regime from HSI, HSTECH, and HSCEI alignment first.
- Track southbound capital flow as a key sentiment driver.
- Translate recap into actionable risk-on/risk-off stance with clear invalidation points.

### Analysis Dimensions
- Trend Regime: Classify the market as momentum, range, or risk-off.
  - Are HSI/HSTECH/HSCEI directionally aligned
  - Did volume confirm the move
  - Are key index levels reclaimed or lost
- Capital Flows: Map southbound flow and macro narrative into equity risk appetite.
  - Southbound net flow direction and magnitude
  - USD/HKD and China policy implications
  - Breadth and leadership concentration
- Sector Themes: Identify persistent leaders and vulnerable laggards.
  - Tech/internet platform trend persistence
  - Financials/property sensitivity to policy shifts
  - Defensive vs growth factor rotation

### Action Framework
- Risk-on: broad index breakout with expanding southbound participation.
- Neutral: mixed index signals; focus on selective relative strength.
- Risk-off: failed breakouts and rising volatility; prioritize capital preservation."""
        if not (self.region == "cn" and self._get_review_language() == "en"):
            return self.strategy.to_prompt_block()
        return """## Strategy Blueprint: A-share Three-Phase Recap Strategy
Focus on index trend, liquidity, and sector rotation to shape the next-session trading plan.

### Strategy Principles
- Read index direction first, then confirm liquidity structure, and finally test sector persistence.
- Every conclusion must map to position sizing, trading pace, and risk-control actions.
- Base judgments on today's data and the latest 3-day news flow without inventing unverified information.

### Analysis Dimensions
- Trend Structure: Determine whether the market is in an uptrend, range, or defensive phase.
  - Are the SSE, SZSE, and ChiNext moving in the same direction
  - Is the market advancing on expanding volume or slipping on contracting volume
  - Have key support or resistance levels been reclaimed or broken
- Liquidity & Sentiment: Identify near-term risk appetite and market temperature.
  - Advance/decline breadth and limit-up/limit-down structure
  - Whether turnover is expanding or fading
  - Whether high-beta leaders are showing divergence
- Leading Themes: Distill tradable leadership and areas to avoid.
  - Whether leading sectors have clear event catalysts
  - Whether sector leaders are pulling the group higher
  - Whether weakness is broadening across lagging sectors

### Action Framework
- Offensive: indices rise in sync, turnover expands, and core themes strengthen.
- Balanced: index divergence or low-volume consolidation; keep sizing controlled and wait for confirmation.
- Defensive: indices weaken and laggards broaden; prioritize risk control and de-risking."""

    def _get_strategy_markdown_block(self, review_language: str | None = None) -> str:
        review_language = review_language or self._get_review_language()
        if self.region == "hk" and review_language == "en":
            return """### 6. Strategy Framework
- **Trend Regime**: Classify the market as momentum, range, or risk-off based on HSI/HSTECH/HSCEI alignment.
- **Capital Flows**: Track southbound flow direction and macro narrative for risk appetite signals.
- **Sector Themes**: Focus on tech/internet platform persistence and financials/property policy sensitivity.
"""
        if not (self.region == "cn" and review_language == "en"):
            return self.strategy.to_markdown_block()
        return """### 6. Strategy Framework
- **Trend Structure**: Determine whether the market is in an uptrend, range, or defensive phase.
- **Liquidity & Sentiment**: Track breadth, turnover expansion, and whether leaders are diverging.
- **Leading Themes**: Focus on sectors with catalysts and sustained leadership while avoiding broadening weakness.
"""

    def _get_market_mood_text(self, mood_key: str, review_language: str | None = None) -> str:
        review_language = review_language or self._get_review_language()
        if review_language == "en":
            mapping = {
                "strong_up": "strong gains",
                "mild_up": "moderate gains",
                "mild_down": "mild losses",
                "strong_down": "clear weakness",
                "range": "range-bound trading",
            }
        else:
            mapping = {
                "strong_up": "强势上涨",
                "mild_up": "小幅上涨",
                "mild_down": "小幅下跌",
                "strong_down": "明显下跌",
                "range": "震荡整理",
            }
        return mapping[mood_key]

    def get_market_overview(self) -> MarketOverview:
        """
        获取市场概览数据
        
        Returns:
            MarketOverview: 市场概览数据对象
        """
        today = datetime.now().strftime('%Y-%m-%d')
        overview = MarketOverview(date=today)
        
        # 1. 获取主要指数行情（按 region 切换 A 股/美股）
        overview.indices = self._get_main_indices()

        # 2. 获取涨跌统计（A 股有，美股无等效数据）
        if self.profile.has_market_stats:
            self._get_market_statistics(overview)

        # 3. 获取板块涨跌榜（A 股有，美股暂无）
        if self.profile.has_sector_rankings:
            self._get_sector_rankings(overview)

        # 4. 获取 A 股概念热度、人气股和涨停连板，用于校验真正交易主线
        if self.region == "cn":
            self._get_concept_rankings(overview)
            self._get_hot_stock_sections(overview)
        
        # 5. 获取北向资金（可选）
        # self._get_north_flow(overview)
        
        return overview

    
    def _get_main_indices(self) -> List[MarketIndex]:
        """获取主要指数实时行情"""
        indices = []

        try:
            logger.info("[大盘] 获取主要指数实时行情...")

            # 使用 DataFetcherManager 获取指数行情（按 region 切换）
            data_list = self.data_manager.get_main_indices(region=self.region)

            if data_list:
                for item in data_list:
                    index = MarketIndex(
                        code=item['code'],
                        name=item['name'],
                        current=item['current'],
                        change=item['change'],
                        change_pct=item['change_pct'],
                        open=item['open'],
                        high=item['high'],
                        low=item['low'],
                        prev_close=item['prev_close'],
                        volume=item['volume'],
                        amount=item['amount'],
                        amplitude=item['amplitude']
                    )
                    indices.append(index)

            if not indices:
                logger.warning("[大盘] 所有行情数据源失败，将依赖新闻搜索进行分析")
            else:
                logger.info(f"[大盘] 获取到 {len(indices)} 个指数行情")

        except Exception as e:
            logger.error(f"[大盘] 获取指数行情失败: {e}")

        return indices

    def _get_market_statistics(self, overview: MarketOverview):
        """获取市场涨跌统计"""
        try:
            logger.info("[大盘] 获取市场涨跌统计...")

            stats = self.data_manager.get_market_stats()

            if stats:
                overview.up_count = stats.get('up_count', 0)
                overview.down_count = stats.get('down_count', 0)
                overview.flat_count = stats.get('flat_count', 0)
                overview.limit_up_count = stats.get('limit_up_count', 0)
                overview.limit_down_count = stats.get('limit_down_count', 0)
                overview.total_amount = stats.get('total_amount', 0.0)

                logger.info(f"[大盘] 涨:{overview.up_count} 跌:{overview.down_count} 平:{overview.flat_count} "
                          f"涨停:{overview.limit_up_count} 跌停:{overview.limit_down_count} "
                          f"成交额:{overview.total_amount:.0f}亿")

        except Exception as e:
            logger.error(f"[大盘] 获取涨跌统计失败: {e}")

    def _get_sector_rankings(self, overview: MarketOverview):
        """获取板块涨跌榜"""
        try:
            logger.info("[大盘] 获取板块涨跌榜...")

            top_sectors, bottom_sectors = self.data_manager.get_sector_rankings(5)

            if top_sectors or bottom_sectors:
                overview.top_sectors = top_sectors
                overview.bottom_sectors = bottom_sectors

                logger.info(f"[大盘] 领涨板块: {[s['name'] for s in overview.top_sectors]}")
                logger.info(f"[大盘] 领跌板块: {[s['name'] for s in overview.bottom_sectors]}")

        except Exception as e:
            logger.error(f"[大盘] 获取板块涨跌榜失败: {e}")

    def _get_concept_rankings(self, overview: MarketOverview):
        """获取概念/题材涨跌榜。"""
        try:
            logger.info("[大盘] 获取概念题材涨跌榜...")

            top_concepts, bottom_concepts = self.data_manager.get_concept_rankings(5)

            if top_concepts or bottom_concepts:
                overview.top_concepts = top_concepts
                overview.bottom_concepts = bottom_concepts
                logger.info(f"[大盘] 热门概念: {[s['name'] for s in overview.top_concepts]}")

        except Exception as e:
            logger.error(f"[大盘] 获取概念题材涨跌榜失败: {e}")

    def _get_hot_stock_sections(self, overview: MarketOverview):
        """获取人气股与涨停连板数据。"""
        try:
            logger.info("[大盘] 获取人气股与涨停池...")
            overview.hot_stocks = self.data_manager.get_hot_stocks(8)
            query_date = datetime.strptime(overview.date, "%Y-%m-%d").strftime("%Y%m%d")
            overview.limit_up_stocks = self.data_manager.get_limit_up_pool(
                date=query_date,
                n=12,
            )
            logger.info(
                "[大盘] 人气股 %d 只，涨停池样本 %d 只",
                len(overview.hot_stocks),
                len(overview.limit_up_stocks),
            )
        except Exception as e:
            logger.error(f"[大盘] 获取人气股与涨停池失败: {e}")
    
    # def _get_north_flow(self, overview: MarketOverview):
    #     """获取北向资金流入"""
    #     try:
    #         logger.info("[大盘] 获取北向资金...")
    #         
    #         # 获取北向资金数据
    #         df = ak.stock_hsgt_north_net_flow_in_em(symbol="北上")
    #         
    #         if df is not None and not df.empty:
    #             # 取最新一条数据
    #             latest = df.iloc[-1]
    #             if '当日净流入' in df.columns:
    #                 overview.north_flow = float(latest['当日净流入']) / 1e8  # 转为亿元
    #             elif '净流入' in df.columns:
    #                 overview.north_flow = float(latest['净流入']) / 1e8
    #                 
    #             logger.info(f"[大盘] 北向资金净流入: {overview.north_flow:.2f}亿")
    #             
    #     except Exception as e:
    #         logger.warning(f"[大盘] 获取北向资金失败: {e}")
    
    def search_market_news(self) -> List[Dict]:
        """
        搜索市场新闻
        
        Returns:
            新闻列表
        """
        if not self.search_service:
            logger.warning("[大盘] 搜索服务未配置，跳过新闻搜索")
            return []
        
        all_news = []

        # 按 region 使用不同的新闻搜索词
        search_queries = self.profile.news_queries
        
        try:
            logger.info("[大盘] 开始搜索市场新闻...")
            
            # 根据 region 设置搜索上下文名称，避免美股搜索被解读为 A 股语境
            market_names = {"cn": "大盘", "us": "US market", "hk": "HK market"}
            market_name = market_names.get(self.region, "大盘")
            for query in search_queries:
                response = self.search_service.search_stock_news(
                    stock_code="market",
                    stock_name=market_name,
                    max_results=3,
                    focus_keywords=query.split()
                )
                if response and response.results:
                    all_news.extend(response.results)
                    logger.info(f"[大盘] 搜索 '{query}' 获取 {len(response.results)} 条结果")
            
            logger.info(f"[大盘] 共获取 {len(all_news)} 条市场新闻")
            
        except Exception as e:
            logger.error(f"[大盘] 搜索市场新闻失败: {e}")
        
        return all_news
    
    def generate_market_review(self, overview: MarketOverview, news: List) -> str:
        """
        使用大模型生成大盘复盘报告
        
        Args:
            overview: 市场概览数据
            news: 市场新闻列表 (SearchResult 对象列表)
            
        Returns:
            大盘复盘报告文本
        """
        if not self.analyzer or not self.analyzer.is_available():
            logger.warning("[大盘] AI分析器未配置或不可用，使用模板生成报告")
            return self._generate_template_review(overview, news)
        
        # 构建 Prompt
        prompt = self._build_review_prompt(overview, news)
        
        logger.info("[大盘] 调用大模型生成复盘报告...")
        # Use the public generate_text() entry point — never access private analyzer attributes.
        review = self.analyzer.generate_text(prompt, max_tokens=8192, temperature=0.7)

        if review:
            logger.info("[大盘] 复盘报告生成成功，长度: %d 字符", len(review))
            # Inject structured data tables into LLM prose sections
            return self._inject_data_into_review(review, overview, news)
        else:
            logger.warning("[大盘] 大模型返回为空，使用模板报告")
            return self._generate_template_review(overview, news)
    
    def _inject_data_into_review(
        self,
        review: str,
        overview: MarketOverview,
        news: Optional[List] = None,
    ) -> str:
        """Inject structured data tables into the corresponding LLM prose sections."""
        # Build data blocks
        stats_block = self._build_stats_block(overview)
        indices_block = self._build_indices_block(overview)
        sector_block = self._build_sector_block(overview)
        hot_stock_block = self._build_hot_stock_block(overview)
        news_block = self._build_news_block(news or [])
        patterns = (
            _ENGLISH_SECTION_PATTERNS
            if self._get_review_language() == "en"
            else _CHINESE_SECTION_PATTERNS
        )

        if stats_block:
            review = self._insert_after_section(
                review,
                patterns["market_summary"],
                stats_block,
            )

        if indices_block:
            review = self._insert_after_section(
                review,
                patterns["index_commentary"],
                indices_block,
            )

        if sector_block:
            review = self._insert_after_section(
                review,
                patterns["sector_highlights"],
                sector_block,
            )

        if hot_stock_block:
            hot_pattern = patterns.get("hot_stocks")
            if hot_pattern and self._has_section(review, hot_pattern):
                review = self._insert_after_section(
                    review,
                    hot_pattern,
                    hot_stock_block,
                )
            else:
                review = self._insert_after_section(
                    review,
                    patterns["sector_highlights"],
                    hot_stock_block,
                )

        if news_block and "news_catalysts" in patterns:
            review = self._insert_after_section(
                review,
                patterns["news_catalysts"],
                news_block,
            )

        return review

    @staticmethod
    def _has_section(text: str, heading_pattern: str) -> bool:
        import re
        return re.search(heading_pattern, text) is not None

    @staticmethod
    def _insert_after_section(text: str, heading_pattern: str, block: str) -> str:
        """Insert a data block at the end of a markdown section (before the next ### heading)."""
        import re
        # Find the heading
        match = re.search(heading_pattern, text)
        if not match:
            return text
        start = match.end()
        # Find the next ### heading after this one
        next_heading = re.search(r'\n###\s', text[start:])
        if next_heading:
            insert_pos = start + next_heading.start()
        else:
            # No next heading — append at end
            insert_pos = len(text)
        # Insert the block before the next heading, with spacing
        return text[:insert_pos].rstrip() + '\n\n' + block + '\n\n' + text[insert_pos:].lstrip('\n')

    def _build_stats_block(self, overview: MarketOverview) -> str:
        """Build market statistics block."""
        has_stats = overview.up_count or overview.down_count or overview.total_amount
        if not has_stats:
            return ""
        if self._get_review_language() == "en":
            scorecard = self.build_market_score_snapshot(overview)
            return "\n".join(
                [
                    f"> **Market Score**: **{scorecard['score']}/100** "
                    f"({scorecard['temperature_label']}, {scorecard['label']})",
                    f"> **Reasons**: {'; '.join(scorecard['reasons'])}",
                    f"> **Trading Pace**: {scorecard['guidance']}",
                    "",
                    f"> 📈 Advancers **{overview.up_count}** / Decliners **{overview.down_count}** / "
                    f"Flat **{overview.flat_count}** | "
                    f"Limit-up **{overview.limit_up_count}** / Limit-down **{overview.limit_down_count}** | "
                    f"Turnover **{overview.total_amount:.0f}** ({self._get_turnover_unit_label()})",
                ]
            )
        scorecard = self.build_market_score_snapshot(overview)
        score, label = scorecard["score"], scorecard["temperature_label"]
        participation = overview.up_count + overview.down_count
        up_ratio = overview.up_count / participation if participation else 0.0
        limit_spread = overview.limit_up_count - overview.limit_down_count
        lines = [
            f"> **盘面评分**：**{score}/100**（{label}，{scorecard['label']}）",
            f"> **评分依据**：{'；'.join(scorecard['reasons'])}",
            f"> **操作节奏**：{scorecard['guidance']}",
            "",
            "| 指标 | 数值 | 观察 |",
            "|------|------|------|",
            f"| 上涨/下跌/平盘 | {overview.up_count} / {overview.down_count} / {overview.flat_count} | 上涨占比(不含平盘) {up_ratio:.1%} |",
            f"| 涨停/跌停 | {overview.limit_up_count} / {overview.limit_down_count} | 涨跌停差 {limit_spread:+d} |",
            f"| 两市成交额 | {overview.total_amount:.0f} 亿 | {self._describe_turnover(overview.total_amount)} |",
        ]
        return "\n".join(lines)

    def build_market_score_snapshot(self, overview: MarketOverview) -> Dict[str, Any]:
        """Build a deterministic market score snapshot from structured breadth data."""
        score, temperature_label = self._build_market_temperature(overview)
        if score >= 60:
            status = "green"
        elif score >= 40:
            status = "yellow"
        else:
            status = "red"

        if self._get_review_language() == "en":
            label_map = {
                "green": "constructive",
                "yellow": "watch",
                "red": "defensive",
            }
            guidance_map = {
                "green": "Risk appetite is acceptable; focus on leading themes and position discipline.",
                "yellow": "Signals are mixed; keep position sizing moderate and wait for confirmation.",
                "red": "Risk is elevated; prioritize drawdown control and avoid chasing weak rebounds.",
            }
            reasons = self._build_market_light_reasons_en(overview, score)
        else:
            label_map = {
                "green": "可进攻",
                "yellow": "需观察",
                "red": "偏防守",
            }
            guidance_map = {
                "green": "风险偏好尚可，关注主线延续与仓位纪律。",
                "yellow": "信号分化，控制仓位并等待量价确认。",
                "red": "风险偏高，优先控制回撤，避免追高弱反弹。",
            }
            reasons = self._build_market_light_reasons_zh(overview, score)

        return {
            "status": status,
            "label": label_map[status],
            "score": score,
            "temperature_label": temperature_label,
            "reasons": reasons,
            "guidance": guidance_map[status],
        }

    def build_market_light_snapshot(self, overview: MarketOverview) -> Dict[str, Any]:
        """Backward-compatible alias for older callers/tests."""
        return self.build_market_score_snapshot(overview)

    def _build_market_light_reasons_zh(self, overview: MarketOverview, score: int) -> List[str]:
        participation = overview.up_count + overview.down_count
        up_ratio = overview.up_count / participation if participation else None
        reasons: List[str] = [f"盘面温度 {score}/100"]
        if up_ratio is not None:
            if up_ratio >= 0.6:
                reasons.append(f"上涨家数占比 {up_ratio:.0%}，赚钱效应扩散")
            elif up_ratio <= 0.4:
                reasons.append(f"上涨家数占比 {up_ratio:.0%}，亏钱效应较强")
            else:
                reasons.append(f"上涨家数占比 {up_ratio:.0%}，市场分化")
        if overview.indices:
            avg_change = sum(idx.change_pct for idx in overview.indices) / len(overview.indices)
            reasons.append(f"主要指数平均涨跌幅 {avg_change:+.2f}%")
        if overview.limit_up_count or overview.limit_down_count:
            reasons.append(f"涨跌停差 {overview.limit_up_count - overview.limit_down_count:+d}")
        return reasons[:4]

    def _build_market_light_reasons_en(self, overview: MarketOverview, score: int) -> List[str]:
        participation = overview.up_count + overview.down_count
        up_ratio = overview.up_count / participation if participation else None
        reasons: List[str] = [f"market temperature {score}/100"]
        if up_ratio is not None:
            if up_ratio >= 0.6:
                reasons.append(f"advancers ratio {up_ratio:.0%}, breadth is expanding")
            elif up_ratio <= 0.4:
                reasons.append(f"advancers ratio {up_ratio:.0%}, downside pressure dominates")
            else:
                reasons.append(f"advancers ratio {up_ratio:.0%}, breadth is mixed")
        if overview.indices:
            avg_change = sum(idx.change_pct for idx in overview.indices) / len(overview.indices)
            reasons.append(f"average major-index change {avg_change:+.2f}%")
        if overview.limit_up_count or overview.limit_down_count:
            reasons.append(f"limit-up/down spread {overview.limit_up_count - overview.limit_down_count:+d}")
        return reasons[:4]

    def _build_indices_block(self, overview: MarketOverview) -> str:
        """构建指数行情表格"""
        if not overview.indices:
            return ""
        if self._get_review_language() == "en":
            lines = [
                f"| Index | Last | Change % | Open | High | Low | Amplitude | Turnover ({self._get_turnover_unit_label()}) |",
                "|-------|------|----------|------|------|-----|-----------|-----------------|",
            ]
        else:
            lines = [
                "| 指数 | 最新 | 涨跌幅 | 开盘 | 最高 | 最低 | 振幅 | 成交额(亿) |",
                "|------|------|--------|------|------|------|------|-----------|",
            ]
        for idx in overview.indices:
            arrow = "🔴" if idx.change_pct < 0 else "🟢" if idx.change_pct > 0 else "⚪"
            amount_raw = idx.amount or 0.0
            amount_str = self._format_turnover_value(amount_raw)
            lines.append(
                f"| {idx.name} | {idx.current:.2f} | {arrow} {idx.change_pct:+.2f}% | "
                f"{self._format_optional_number(idx.open)} | {self._format_optional_number(idx.high)} | "
                f"{self._format_optional_number(idx.low)} | {self._format_optional_pct(idx.amplitude)} | {amount_str} |"
            )
        return "\n".join(lines)

    def _build_sector_block(self, overview: MarketOverview) -> str:
        """Build sector ranking block."""
        if (
            not overview.top_sectors
            and not overview.bottom_sectors
            and not overview.top_concepts
            and not overview.bottom_concepts
        ):
            return ""
        lines = []
        if overview.top_sectors:
            if self._get_review_language() == "en":
                lines.extend([
                    "#### Leading Industries",
                    "| Rank | Sector | Change |",
                    "|------|--------|--------|",
                ])
            else:
                lines.extend([
                    "#### 行业涨跌 Top 5",
                    "| 排名 | 板块 | 涨跌幅 |",
                    "|------|------|--------|",
                ])
            for rank, sector in enumerate(overview.top_sectors[:5], 1):
                lines.append(
                    f"| {rank} | {sector.get('name', '-')} | {self._format_signed_pct(sector.get('change_pct'))} |"
                )
        if overview.bottom_sectors:
            if lines:
                lines.append("")
            if self._get_review_language() == "en":
                lines.extend([
                    "#### Lagging Industries",
                    "| Rank | Sector | Change |",
                    "|------|--------|--------|",
                ])
            else:
                lines.extend([
                    "#### 领跌板块 Top 5",
                    "| 排名 | 板块 | 涨跌幅 |",
                    "|------|------|--------|",
                ])
            for rank, sector in enumerate(overview.bottom_sectors[:5], 1):
                lines.append(
                    f"| {rank} | {sector.get('name', '-')} | {self._format_signed_pct(sector.get('change_pct'))} |"
                )
        if overview.top_concepts:
            if lines:
                lines.append("")
            if self._get_review_language() == "en":
                lines.extend([
                    "#### Leading Concept Themes",
                    "| Rank | Theme | Change |",
                    "|------|-------|--------|",
                ])
            else:
                lines.extend([
                    "#### 热门概念 Top 5",
                    "| 排名 | 概念/题材 | 涨跌幅 |",
                    "|------|-----------|--------|",
                ])
            for rank, concept in enumerate(overview.top_concepts[:5], 1):
                lines.append(
                    f"| {rank} | {concept.get('name', '-')} | {self._format_signed_pct(concept.get('change_pct'))} |"
                )
        if overview.bottom_concepts:
            if lines:
                lines.append("")
            if self._get_review_language() == "en":
                lines.extend([
                    "#### Lagging Concept Themes",
                    "| Rank | Theme | Change |",
                    "|------|-------|--------|",
                ])
            else:
                lines.extend([
                    "#### 低迷概念 Top 5",
                    "| 排名 | 概念/题材 | 涨跌幅 |",
                    "|------|-----------|--------|",
                ])
            for rank, concept in enumerate(overview.bottom_concepts[:5], 1):
                lines.append(
                    f"| {rank} | {concept.get('name', '-')} | {self._format_signed_pct(concept.get('change_pct'))} |"
                )
        return "\n".join(lines)

    def _build_hot_stock_block(self, overview: MarketOverview) -> str:
        """Build hot-stock and limit-up ladder block."""
        if not overview.hot_stocks and not overview.limit_up_stocks:
            return ""
        lines: List[str] = []
        if overview.hot_stocks:
            if self._get_review_language() == "en":
                lines.extend([
                    "#### Hot Stocks",
                    "| Rank | Code | Name | Change | Last | Source |",
                    "|------|------|------|--------|------|--------|",
                ])
            else:
                lines.extend([
                    "#### 人气股票 Top 8",
                    "| 排名 | 代码 | 名称 | 涨跌幅 | 最新价 | 来源 |",
                    "|------|------|------|--------|--------|------|",
                ])
            for stock in overview.hot_stocks[:8]:
                lines.append(
                    f"| {stock.get('rank') or '-'} | {stock.get('code', '-')} | "
                    f"{stock.get('name', '-')} | {self._format_signed_pct(stock.get('change_pct'))} | "
                    f"{self._format_optional_number(stock.get('price'))} | {stock.get('source', '-')} |"
                )
        if overview.limit_up_stocks:
            if lines:
                lines.append("")
            chain_summary = self._build_limit_chain_summary(overview.limit_up_stocks)
            if chain_summary:
                lines.append(chain_summary)
                lines.append("")
            if self._get_review_language() == "en":
                lines.extend([
                    "#### Limit-up Ladder",
                    "| Code | Name | Boards | Industry | First Seal | Turnover |",
                    "|------|------|--------|----------|------------|----------|",
                ])
            else:
                lines.extend([
                    "#### 涨停连板梯队",
                    "| 代码 | 名称 | 连板 | 行业 | 首封 | 成交额(亿) |",
                    "|------|------|------|------|------|------------|",
                ])
            for stock in overview.limit_up_stocks[:10]:
                lines.append(
                    f"| {stock.get('code', '-')} | {stock.get('name', '-')} | "
                    f"{stock.get('consecutive_boards') or 1} | {stock.get('industry') or '-'} | "
                    f"{self._format_limit_time(stock.get('first_limit_time'))} | "
                    f"{self._format_amount_yi(stock.get('amount'))} |"
                )
        return "\n".join(lines)

    def _build_news_block(self, news: List) -> str:
        """Build a source-aware news catalyst table for the rendered report."""
        if not news:
            return ""
        if self._get_review_language() == "en":
            lines = [
                "#### News Catalysts",
                "| # | Headline | Snippet / Lead | Source |",
                "|---|----------|----------------|--------|",
            ]
        else:
            lines = [
                "#### 近三日催化线索",
                "| 序号 | 事件/标题 | 摘要/线索片段 | 来源 |",
                "|------|-----------|----------------|------|",
            ]

        for idx, item in enumerate(news[:5], 1):
            title = self._escape_table_cell(
                self._compact_news_text(self._get_news_field(item, "title"), limit=80) or "-"
            )
            snippet = self._escape_table_cell(
                self._compact_news_text(self._get_news_field(item, "snippet"), limit=180) or "-"
            )
            source = self._escape_table_cell(self._format_news_source_cell(item) or "-")
            lines.append(f"| {idx} | {title} | {snippet} | {source} |")
        return "\n".join(lines)

    @staticmethod
    def _get_news_field(item: Any, field: str) -> str:
        if hasattr(item, field):
            value = getattr(item, field, "") or ""
        elif isinstance(item, dict):
            value = item.get(field, "") or ""
        else:
            value = ""
        return str(value).strip()

    @classmethod
    def _format_news_source_cell(cls, item: Any) -> str:
        source = cls._compact_news_text(cls._get_news_field(item, "source"), limit=40)
        date_text = cls._compact_news_text(cls._get_news_field(item, "published_date"), limit=24)
        url = cls._compact_news_text(cls._get_news_field(item, "url"), limit=0)
        label_parts = [part for part in (source, date_text) if part]
        label = " / ".join(label_parts)
        if url:
            return f"[{label or 'URL'}]({url})"
        return label

    @staticmethod
    def _compact_news_text(value: str, *, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if limit <= 0 or len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    @staticmethod
    def _format_optional_number(value: float) -> str:
        try:
            if value is None:
                return "N/A"
            numeric_value = float(value)
            if numeric_value == 0:
                return "N/A"
            return f"{numeric_value:.2f}"
        except (TypeError, ValueError):
            return "N/A"

    @staticmethod
    def _format_optional_pct(value: float) -> str:
        try:
            if value is None:
                return "N/A"
            numeric_value = float(value)
            if numeric_value == 0:
                return "N/A"
            return f"{numeric_value:.2f}%"
        except (TypeError, ValueError):
            return "N/A"

    @staticmethod
    def _format_signed_pct(value: Any) -> str:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return "N/A"
        return f"{numeric_value:+.2f}%"

    @staticmethod
    def _escape_table_cell(value: str) -> str:
        return value.replace("|", "\\|")

    @staticmethod
    def _format_amount_yi(value: Any) -> str:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return "N/A"
        if numeric_value == 0:
            return "N/A"
        return f"{numeric_value / 1e8:.2f}"

    @staticmethod
    def _format_limit_time(value: Any) -> str:
        raw = str(value or "").strip()
        if len(raw) == 6 and raw.isdigit():
            return f"{raw[:2]}:{raw[2:4]}"
        if len(raw) == 4 and raw.isdigit():
            return f"{raw[:2]}:{raw[2:]}"
        return raw or "-"

    def _build_limit_chain_summary(self, limit_up_stocks: List[Dict]) -> str:
        counts: Dict[int, int] = {}
        for stock in limit_up_stocks:
            boards = stock.get("consecutive_boards") or 1
            try:
                boards_int = int(boards)
            except (TypeError, ValueError):
                boards_int = 1
            counts[boards_int] = counts.get(boards_int, 0) + 1
        if not counts:
            return ""

        parts = [
            f"{boards}板 {counts[boards]}只" if self._get_review_language() != "en" else f"{boards}x {counts[boards]}"
            for boards in sorted(counts, reverse=True)[:5]
        ]
        if self._get_review_language() == "en":
            return f"> **Limit-up ladder**: {'; '.join(parts)}"
        return f"> **连板结构**：{'；'.join(parts)}"

    @staticmethod
    def _build_temperature_bar(score: int) -> str:
        filled = max(0, min(10, round(score / 10)))
        return "█" * filled + "░" * (10 - filled)

    @staticmethod
    def _describe_turnover(total_amount: float) -> str:
        if total_amount >= 15000:
            return "高活跃度"
        if total_amount >= 9000:
            return "中等活跃"
        if total_amount > 0:
            return "缩量观望"
        return "暂无数据"

    def _build_market_temperature(self, overview: MarketOverview) -> tuple[int, str]:
        participants = overview.up_count + overview.down_count
        breadth_score = 50
        if participants:
            breadth_score = int(overview.up_count / participants * 100)

        index_changes = [idx.change_pct for idx in overview.indices if idx.change_pct is not None]
        index_score = 50
        if index_changes:
            avg_change = sum(index_changes) / len(index_changes)
            index_score = int(max(0, min(100, 50 + avg_change * 12)))

        limit_total = overview.limit_up_count + overview.limit_down_count
        limit_score = 50
        if limit_total:
            limit_score = int(overview.limit_up_count / limit_total * 100)

        score = int(round(breadth_score * 0.45 + index_score * 0.35 + limit_score * 0.20))
        if self._get_review_language() == "en":
            if score >= 70:
                label = "risk-on"
            elif score >= 55:
                label = "constructive"
            elif score >= 40:
                label = "mixed"
            else:
                label = "defensive"
        else:
            if score >= 70:
                label = "强势"
            elif score >= 55:
                label = "偏暖"
            elif score >= 40:
                label = "震荡"
            else:
                label = "偏弱"
        return score, label

    def _build_review_prompt(self, overview: MarketOverview, news: List) -> str:
        """构建复盘报告 Prompt"""
        review_language = self._get_review_language()
        is_a_share_review = self.region == "cn"

        # 指数行情信息（简洁格式，不用emoji）
        indices_text = ""
        for idx in overview.indices:
            direction = "↑" if idx.change_pct > 0 else "↓" if idx.change_pct < 0 else "-"
            indices_text += f"- {idx.name}: {idx.current:.2f} ({direction}{abs(idx.change_pct):.2f}%)\n"
        
        # 板块信息
        top_sectors_text = ", ".join([f"{s['name']}({s['change_pct']:+.2f}%)" for s in overview.top_sectors[:3]])
        bottom_sectors_text = ", ".join([f"{s['name']}({s['change_pct']:+.2f}%)" for s in overview.bottom_sectors[:3]])
        top_concepts_text = ", ".join([f"{s['name']}({s['change_pct']:+.2f}%)" for s in overview.top_concepts[:5]])
        bottom_concepts_text = ", ".join([f"{s['name']}({s['change_pct']:+.2f}%)" for s in overview.bottom_concepts[:3]])
        hot_stocks_text = ""
        limit_up_text = ""
        if is_a_share_review:
            hot_stocks_text = "\n".join(
                [
                    f"- {s.get('rank', '-')}. {s.get('name', '-')}"
                    f"({s.get('code', '-')}) {self._format_signed_pct(s.get('change_pct'))}"
                    f" 来源:{s.get('source', '-')}"
                    for s in overview.hot_stocks[:8]
                ]
            )
            limit_up_text = "\n".join(
                [
                    f"- {s.get('name', '-')}({s.get('code', '-')}): "
                    f"{s.get('consecutive_boards') or 1}连板, {s.get('industry') or '-'}, "
                    f"首封 {self._format_limit_time(s.get('first_limit_time'))}"
                    for s in overview.limit_up_stocks[:10]
                ]
            )
        
        # 新闻信息 - 支持 SearchResult 对象或字典
        news_text = ""
        for i, n in enumerate(news[:6], 1):
            # 兼容 SearchResult 对象和字典
            title = self._compact_news_text(self._get_news_field(n, "title"), limit=90)
            snippet = self._compact_news_text(self._get_news_field(n, "snippet"), limit=220)
            source = self._compact_news_text(self._get_news_field(n, "source"), limit=60)
            published_date = self._compact_news_text(self._get_news_field(n, "published_date"), limit=30)
            url = self._compact_news_text(self._get_news_field(n, "url"), limit=180)
            meta_parts = [part for part in (source, published_date) if part]
            meta = f" ({' / '.join(meta_parts)})" if meta_parts else ""
            url_line = f"\n   URL: {url}" if url else ""
            news_text += f"{i}. {title}{meta}\n   {snippet or '-'}{url_line}\n"
        
        # 按 region 组装市场概况与板块区块（美股无涨跌家数、板块数据）
        stats_block = ""
        sector_block = ""
        if review_language == "en":
            if self.profile.has_market_stats:
                stats_block = f"""## Market Breadth
- Advancers: {overview.up_count} | Decliners: {overview.down_count} | Flat: {overview.flat_count}
- Limit-up: {overview.limit_up_count} | Limit-down: {overview.limit_down_count}
- Turnover: {overview.total_amount:.0f} ({self._get_turnover_unit_label()})"""
            else:
                stats_block = "## Market Breadth\n(No equivalent advance/decline statistics are available for this market.)"

            if self.profile.has_sector_rankings:
                sector_block = f"""## Sector Performance
Leading: {top_sectors_text if top_sectors_text else "N/A"}
Lagging: {bottom_sectors_text if bottom_sectors_text else "N/A"}
Concept leaders: {top_concepts_text if top_concepts_text else "N/A"}
Concept laggards: {bottom_concepts_text if bottom_concepts_text else "N/A"}"""
            else:
                sector_block = "## Sector Performance\n(Sector data not available for this market.)"
        else:
            if self.profile.has_market_stats:
                stats_block = f"""## 市场概况
- 上涨: {overview.up_count} 家 | 下跌: {overview.down_count} 家 | 平盘: {overview.flat_count} 家
- 涨停: {overview.limit_up_count} 家 | 跌停: {overview.limit_down_count} 家
- 两市成交额: {overview.total_amount:.0f} 亿元"""
            else:
                stats_block = "## 市场概况\n（该市场暂无涨跌家数等统计）"

            if self.profile.has_sector_rankings:
                sector_block = f"""## 板块表现
领涨: {top_sectors_text if top_sectors_text else "暂无数据"}
领跌: {bottom_sectors_text if bottom_sectors_text else "暂无数据"}
热门概念: {top_concepts_text if top_concepts_text else "暂无数据"}
低迷概念: {bottom_concepts_text if bottom_concepts_text else "暂无数据"}"""
            else:
                sector_block = "## 板块表现\n（该市场暂无板块涨跌数据）"

        hot_stock_context = ""
        if is_a_share_review and review_language == "en":
            hot_stock_context = f"""## Hot Stocks and Limit-up Ladder
Hot stocks:
{hot_stocks_text if hot_stocks_text else "N/A"}

Limit-up ladder:
{limit_up_text if limit_up_text else "N/A"}"""
        elif is_a_share_review:
            hot_stock_context = f"""## 热门个股与涨停梯队
人气股:
{hot_stocks_text if hot_stocks_text else "暂无数据"}

涨停连板:
{limit_up_text if limit_up_text else "暂无数据"}"""

        data_no_indices_hint = (
            "注意：由于行情数据获取失败，请主要根据【市场新闻】进行定性分析和总结，不要编造具体的指数点位。"
            if not indices_text
            else ""
        )
        if review_language == "en":
            data_no_indices_hint = (
                "Note: Market data fetch failed. Rely mainly on [Market News] for qualitative analysis. Do not invent index levels."
                if not indices_text
                else ""
            )
            indices_placeholder = indices_text if indices_text else "No index data (API error)"
            news_placeholder = news_text if news_text else "No relevant news"
        else:
            indices_placeholder = indices_text if indices_text else "暂无指数数据（接口异常）"
            news_placeholder = news_text if news_text else "暂无相关新闻"

        if review_language == "en":
            report_title = self._get_review_title(overview.date).removeprefix("## ").strip()
            leadership_requirement = (
                "- Separate industry rankings from tradable themes: use concept themes, hot stocks, and limit-up ladder to validate the real market leadership."
                if is_a_share_review
                else "- Separate index action, available sector signals, and news catalysts; do not invent unsupported A-share-only short-term sentiment signals for this market."
            )
            tail_template = (
                """### 5. Hot Stocks & Limit-up Ladder
(Summarize hot stocks, limit-up clusters, consecutive-board leaders, and what they confirm or contradict about leadership.)

### 6. Outlook
(Provide the near-term outlook based on price action and news; classify catalysts as tailwinds, disturbances, or unconfirmed signals.)

### 7. Risk Alerts
(List 3-5 concrete risks to monitor.)

### 8. Strategy Plan
(Provide an offensive/balanced/defensive stance, a position-sizing guideline, one invalidation trigger, and end with “For reference only, not investment advice.”)
"""
                if is_a_share_review
                else """### 5. Outlook
(Provide the near-term outlook based on price action and news; classify catalysts as tailwinds, disturbances, or unconfirmed signals.)

### 6. Risk Alerts
(List 3-5 concrete risks to monitor.)

### 7. Strategy Plan
(Provide an offensive/balanced/defensive stance, a position-sizing guideline, one invalidation trigger, and end with “For reference only, not investment advice.”)
"""
            )
            return f"""You are a professional US/A/H market analyst. Please produce a concise market recap report based on the data below.

[Requirements]
- Output pure Markdown only
- No JSON
- No code blocks
- Use emoji sparingly in headings (at most one per heading)
- The entire fixed shell, headings, guidance, and conclusion must be in English
{leadership_requirement}
- Do not make the report too thin: target 900-1300 English words; each section should include either 2-4 sentences or 3 concrete bullets.
- Fund flows, news catalysts, strategy, and risk alerts must contain actionable interpretation, not generic one-liners.

---

# Today's Market Data

## Date
{overview.date}

## Major Indices
{indices_placeholder}

{stats_block}

{sector_block}

{hot_stock_context}

## Market News
{news_placeholder}

{data_no_indices_hint}

{self._get_strategy_prompt_block()}

---

# Output Template (follow this structure)

## {report_title}

### 1. Market Summary
(3-4 sentences summarizing overall market tone, index moves, liquidity, and the next confirmation signal.)

### 2. Index Commentary
({self._get_index_hint()})

### 3. Fund Flows
(Interpret what turnover, participation, and flow signals imply; state whether the market is broad-based, theme-led, or divergent.)

### 4. Sector Highlights
(Analyze the drivers behind the leading industries and concept themes. State if industry rankings and tradable themes diverge.)

{tail_template}

---

Output the report content directly, no extra commentary.
"""

        # A 股场景使用中文提示语
        leadership_requirement = (
            "- 必须区分“行业涨幅榜”和“真实交易主线”：用热门概念、人气股、涨停连板去校验板块判断，不能把行业涨幅第一直接等同于核心主线"
            if is_a_share_review
            else f"- 必须按{self._get_market_scope_name('zh')}可得数据分析，不要要求或编造该市场未提供的 A 股专属短线情绪数据"
        )
        tail_template = (
            """### 三、板块主线
（分析行业涨跌与概念题材背后的逻辑、持续性；说明二者是否一致，真正主线是谁；给出主线扩散/分歧观察点）

### 四、热门股票与连板
（概括人气股、涨停个股、连板高度和涨停原因聚集方向，用来验证或修正板块主线判断；说明高标与中军是否共振）

### 五、资金与情绪
（解读成交额、涨跌停结构、市场宽度和风险偏好；说明是普涨、结构性行情还是分化行情）

### 六、消息催化
（结合近三日新闻，提炼真正影响明日交易的催化或扰动；按“利好/扰动/待验证”分类）

### 七、明日交易计划
（给出进攻/均衡/防守结论、仓位区间、关注方向、回避方向、观察锚点和一个触发失效条件）

### 八、风险提示
（列出 3-5 个需要关注的风险点；最后补充“建议仅供参考，不构成投资建议”。）
"""
            if is_a_share_review
            else """### 三、板块与主题线索
（结合可得板块、主题和新闻线索分析市场主线；没有板块数据时不要编造，用指数分化和新闻催化说明方向）

### 四、资金与情绪
（解读成交活跃度、指数承接和风险偏好；说明是普涨、结构性行情还是分化行情，不使用 A 股涨跌停指标）

### 五、消息催化
（结合近三日新闻，提炼真正影响明日交易的催化或扰动；按“利好/扰动/待验证”分类）

### 六、明日交易计划
（给出进攻/均衡/防守结论、仓位区间、关注方向、回避方向、观察锚点和一个触发失效条件）

### 七、风险提示
（列出 3-5 个需要关注的风险点；最后补充“建议仅供参考，不构成投资建议”。）
"""
        )
        return f"""你是一位专业的A/H/美股市场分析师，请根据以下数据生成一份结构化的{self._get_market_scope_name('zh')}大盘复盘报告。

【重要】输出要求：
- 必须输出纯 Markdown 文本格式
- 禁止输出 JSON 格式
- 禁止输出代码块
- emoji 仅在标题处少量使用（每个标题最多1个）
- 报告要像交易员盘后工作台：先给结论，再按数据表、主线、催化、计划展开
- 不要重复列出已由系统注入的表格数据；正文负责解释表格背后的含义
- 正文不能过短：整篇建议 1200-1800 个中文字符；每个二级小节至少 2-4 句或 3 条要点
- 资金与情绪、消息催化、交易计划、风险提示必须给出可执行判断，不能只写一句泛泛提示
{leadership_requirement}

---

# 今日市场数据

## 日期
{overview.date}

## 主要指数
{indices_placeholder}

{stats_block}

{sector_block}

{hot_stock_context}

## 市场新闻
{news_placeholder}

{data_no_indices_hint}

{self._get_strategy_prompt_block()}

---

# 输出格式模板（请严格按此格式输出）

## {overview.date} 大盘复盘

> 一句话给出今日市场状态、核心矛盾和明日优先观察方向。

### 一、盘面总览
（3-4句话概括指数、涨跌家数、成交额和情绪温度，明确“强势/偏暖/震荡/偏弱”判断，并说明明日最关键确认信号）

### 二、指数结构
（{self._get_index_hint()}，说明谁在护盘、谁在拖累，以及关键支撑/压力；至少比较两个指数的强弱）

{tail_template}

---

请直接输出复盘报告内容，不要输出其他说明文字。
"""
    
    def _generate_template_review(self, overview: MarketOverview, news: List) -> str:
        """使用模板生成复盘报告（无大模型时的备选方案）"""
        template_language = self._get_template_review_language()
        is_a_share_review = self.region == "cn"
        mood_code = self.profile.mood_index_code
        # 根据 mood_index_code 查找对应指数
        # cn: mood_code="000001"，idx.code 可能为 "sh000001"（以 mood_code 结尾）
        # us: mood_code="SPX"，idx.code 直接为 "SPX"
        mood_index = next(
            (
                idx
                for idx in overview.indices
                if idx.code == mood_code or idx.code.endswith(mood_code)
            ),
            None,
        )
        if mood_index:
            if mood_index.change_pct > 1:
                market_mood = self._get_market_mood_text("strong_up", template_language)
            elif mood_index.change_pct > 0:
                market_mood = self._get_market_mood_text("mild_up", template_language)
            elif mood_index.change_pct > -1:
                market_mood = self._get_market_mood_text("mild_down", template_language)
            else:
                market_mood = self._get_market_mood_text("strong_down", template_language)
        else:
            market_mood = self._get_market_mood_text("range", template_language)
        
        # 指数行情（简洁格式）
        indices_text = ""
        for idx in overview.indices[:4]:
            direction = "↑" if idx.change_pct > 0 else "↓" if idx.change_pct < 0 else "-"
            indices_text += f"- **{idx.name}**: {idx.current:.2f} ({direction}{abs(idx.change_pct):.2f}%)\n"
        
        # 板块信息
        separator = ", " if template_language == "en" else "、"
        top_text = separator.join([s['name'] for s in overview.top_sectors[:3]])
        bottom_text = separator.join([s['name'] for s in overview.bottom_sectors[:3]])
        top_concept_text = separator.join([s['name'] for s in overview.top_concepts[:3]])
        hot_stock_block = self._build_hot_stock_block(overview) if is_a_share_review else ""

        if template_language == "en":
            stats_section = ""
            if self.profile.has_market_stats:
                stats_section = f"""
### 3. Breadth & Liquidity
| Metric | Value |
|--------|-------|
| Advancers | {overview.up_count} |
| Decliners | {overview.down_count} |
| Limit-up | {overview.limit_up_count} |
| Limit-down | {overview.limit_down_count} |
| Turnover ({self._get_turnover_unit_label()}) | {overview.total_amount:.0f} |
"""
            sector_section = ""
            if self.profile.has_sector_rankings and (top_text or bottom_text):
                sector_section = f"""
### 4. Sector Highlights
- **Industry leaders**: {top_text or "N/A"}
- **Laggards**: {bottom_text or "N/A"}
- **Concept leaders**: {top_concept_text or "N/A"}
"""
            hot_section = ""
            if hot_stock_block:
                hot_section = f"""
### 5. Hot Stocks & Limit-up Ladder
{hot_stock_block}
"""
            market_names = {"us": "US Market Recap", "hk": "HK Market Recap"}
            market_name = market_names.get(self.region, "A-share Market Recap")
            report = f"""## {overview.date} {market_name}

### 1. Market Summary
Today's {self._get_market_scope_name(template_language)} showed **{market_mood}**.

### 2. Major Indices
{indices_text or "- No index data available"}
{stats_section}
{sector_section}
{hot_section}
### 6. Risk Alerts
Market conditions can change quickly. The data above is for reference only and does not constitute investment advice.

{self._get_strategy_markdown_block(template_language)}

---
*Review Time: {datetime.now().strftime('%H:%M')}*
"""
            return report

        market_labels = {"cn": "A股", "us": "美股", "hk": "港股"}
        market_label = market_labels.get(self.region, "A股")
        dashboard_block = self._build_stats_block(overview)
        indices_block = self._build_indices_block(overview)
        sector_block = self._build_sector_block(overview)
        scorecard = self.build_market_score_snapshot(overview) if self.profile.has_market_stats else None
        participants = overview.up_count + overview.down_count
        up_ratio_text = f"{overview.up_count / participants:.1%}" if participants else "暂无"
        limit_spread = overview.limit_up_count - overview.limit_down_count
        turnover_desc = self._describe_turnover(overview.total_amount)
        index_leader = max(overview.indices, key=lambda item: item.change_pct, default=None)
        index_laggard = min(overview.indices, key=lambda item: item.change_pct, default=None)
        leader_text = (
            f"{index_leader.name}({index_leader.change_pct:+.2f}%)"
            if index_leader
            else "暂无指数强弱数据"
        )
        laggard_text = (
            f"{index_laggard.name}({index_laggard.change_pct:+.2f}%)"
            if index_laggard
            else "暂无拖累指数"
        )
        hot_names = ""
        limit_chain_text = ""
        if is_a_share_review:
            hot_names = separator.join(
                [
                    s.get("name", "-")
                    for s in overview.hot_stocks[:5]
                    if s.get("name")
                ]
            )
            limit_chain = self._build_limit_chain_summary(overview.limit_up_stocks)
            limit_chain_text = (
                limit_chain.replace("> **连板结构**：", "")
                if limit_chain
                else "暂无连板梯队数据"
            )
        news_block = self._build_news_block(news)
        if news:
            news_titles = separator.join(
                [
                    (getattr(item, "title", "") if hasattr(item, "title") else item.get("title", ""))
                    for item in news[:3]
                    if (getattr(item, "title", "") if hasattr(item, "title") else item.get("title", ""))
                ]
            )
        else:
            news_titles = "暂无可用新闻，需降低题材持续性的确定性判断"
        summary_mood = scorecard["temperature_label"] if scorecard else market_mood

        if is_a_share_review:
            overview_commentary = (
                f"今日盘面核心不是单一指数涨跌，而是宽度、量能和涨跌停结构是否形成共振。"
                f"上涨占比为 {up_ratio_text}，涨跌停差为 {limit_spread:+d}，"
                f"成交状态为“{turnover_desc}”，说明短线风险偏好需要结合主线承接来判断。"
                "若次日量能保持且高标不明显退潮，行情更容易沿强势方向延续；"
                "若量能回落而跌停扩散，则需从进攻转为观察。"
            )
            sector_heading = "### 三、板块主线"
            sector_commentary = (
                f"行业涨跌榜只能说明资金流向的表层，真正主线还要看概念题材、人气股和涨停梯队是否相互验证。"
                f"当前行业强项集中在 {top_text or '暂无行业领涨数据'}，"
                f"概念侧关注 {top_concept_text or '暂无概念领涨数据'}；若两者方向一致，主线持续性更强，"
                "若明显背离，则应优先相信人气股和涨停池反馈。"
                f"弱势方向集中在 {bottom_text or '暂无明显领跌方向'}，短线不宜把被动反弹当成主线切换。"
            )
            sector_fallback = "- 暂无板块涨跌榜数据。"
            hot_stock_section = f"""### 四、热门股票与连板
人气股可以观察资金审美，涨停连板则反映短线情绪高度。当前人气前排包括 {hot_names or "暂无人气榜数据"}，连板结构为 {limit_chain_text}。如果人气中军、题材弹性股和连板高标指向同一方向，说明主线可信度更高；如果高标独强但中军不跟，追高风险会明显增加。

{hot_stock_block or "- 暂无人气股与涨停池数据。"}

"""
            sentiment_heading = "### 五、资金与情绪"
            sentiment_observation = (
                f"- **情绪观察**：涨跌停差 {limit_spread:+d}，"
                "若涨停数量保持但跌停同步增加，意味着高位分歧正在放大。"
            )
            news_heading = "### 六、消息催化"
            plan_heading = "### 七、明日交易计划"
            plan_anchor = "- **观察锚点**：领涨指数是否继续强于大盘、人气股是否维持前排、连板高度是否继续打开。"
            plan_invalidation = "- **失效条件**：若成交额明显萎缩、跌停数量扩散，或人气前排集体冲高回落，应从进攻转为防守。"
            risk_heading = "### 八、风险提示"
            risk_lines = """- **量能透支风险**：放量大涨后若无法继续承接，容易出现冲高回落。
- **主线误判风险**：行业涨幅第一不等于真实交易主线，需要持续用概念、人气股和涨停池校验。
- **高位股分歧风险**：连板高度提升时，若中位股掉队，短线亏钱效应可能快速扩散。
- **消息扰动风险**：外部市场、政策和产业消息变化可能影响风险偏好。
- 建议仅供参考，不构成投资建议。"""
        else:
            stats_context = (
                f"上涨占比为 {up_ratio_text}"
                if self.profile.has_market_stats
                else "当前未提供涨跌家数统计"
            )
            overview_commentary = (
                "今日盘面核心不是单一指数涨跌，而是主要指数承接、成交活跃度和消息催化是否形成共振。"
                f"{stats_context}，成交状态为“{turnover_desc}”，"
                "说明风险偏好需要结合指数结构和新闻线索判断。"
                "若次日量能保持且领涨指数不明显回吐，行情更容易沿强势方向延续；"
                "若量能回落且关键指数跌破短期支撑，则需从进攻转为观察。"
            )
            sector_heading = "### 三、板块与主题线索"
            sector_focus = top_text or top_concept_text or "暂无板块/主题数据"
            sector_weak = bottom_text or "暂无明显走弱方向"
            sector_commentary = (
                "可得板块和主题线索需要结合指数分化与新闻催化判断。"
                f"当前强项集中在 {sector_focus}；若板块信号和新闻方向一致，主线持续性更强，"
                "若数据不足，则优先使用指数结构和催化兑现情况校验方向。"
                f"弱势方向集中在 {sector_weak}，不宜把被动反弹当成主线切换。"
            )
            sector_fallback = "- 暂无可用板块/主题数据。"
            hot_stock_section = ""
            sentiment_heading = "### 四、资金与情绪"
            sentiment_observation = (
                "- **情绪观察**：若领涨指数扩散、回调时承接稳定，风险偏好更健康；"
                "若只有单一指数拉动，仍需防范分化。"
            )
            news_heading = "### 五、消息催化"
            plan_heading = "### 六、明日交易计划"
            plan_anchor = "- **观察锚点**：领涨指数是否继续强于大盘、成交是否维持、新闻催化是否兑现。"
            plan_invalidation = "- **失效条件**：若成交额明显萎缩、关键指数跌破短期支撑，或主要催化被证伪，应从进攻转为防守。"
            risk_heading = "### 七、风险提示"
            risk_lines = """- **量能回落风险**：放量反弹后若无法继续承接，容易出现冲高回落。
- **主线误判风险**：指数或板块强弱不等于可交易主线，需要结合新闻催化、成交变化和承接情况持续验证。
- **消息扰动风险**：外部市场、政策和产业消息变化可能影响风险偏好。
- 建议仅供参考，不构成投资建议。"""

        return f"""## {overview.date} 大盘复盘

> 今日{market_label}市场整体呈现**{market_mood}**态势，盘面状态偏向**{summary_mood}**，优先观察指数承接、成交额变化和板块持续性。

### 一、盘面总览
{overview_commentary}

{dashboard_block or "暂无市场宽度数据。"}

### 二、指数结构
指数层面最强的是 {leader_text}，相对偏弱的是 {laggard_text}。强弱分化能帮助判断资金是在做全面修复，还是集中抱团某一类弹性资产。若领涨指数继续放量，而权重指数不拖后腿，盘面容错率会更高；反之则要警惕指数红盘但个股分化加剧。

{indices_block or indices_text or "暂无指数数据。"}

{sector_heading}
{sector_commentary}

{sector_block or sector_fallback}

{hot_stock_section}{sentiment_heading}
- **量能观察**：成交额处于“{turnover_desc}”状态，若继续放量且指数不冲高回落，资金承接仍可看作积极。
- **宽度观察**：上涨占比 {up_ratio_text}，说明赚钱效应的扩散程度需要和主线强度一起验证，不能只看指数涨跌。
{sentiment_observation}

{news_heading}
{news_block or "- 暂无可用新闻时，应降低对题材持续性的确定性判断。"}

- **利好线索**：重点观察与 {top_concept_text or top_text or "强势主线"} 相关的政策、产业订单、业绩和海外映射是否继续发酵。
- **扰动线索**：若外围市场、汇率、商品价格或监管消息出现反向变化，可能削弱风险偏好。
- **待验证线索**：{news_titles}。

{plan_heading}
- **结论**：{scorecard['label'] if scorecard else '均衡观察'}，优先等指数、成交额和主线方向形成共振。
- **仓位**：控制在中性到积极区间，强势日不盲目满仓，分歧日保留机动仓位。
- **关注方向**：{top_concept_text or top_text or "强于指数的主线板块"}。
- **回避方向**：{bottom_text or "连续走弱且缺少修复信号的方向"}。
{plan_anchor}
{plan_invalidation}

{risk_heading}
{risk_lines}

---
*复盘时间: {datetime.now().strftime('%H:%M')}*
"""
    
    def run_daily_review(self) -> str:
        """
        执行每日大盘复盘流程
        
        Returns:
            复盘报告文本
        """
        logger.info("========== 开始大盘复盘分析 ==========")
        
        # 1. 获取市场概览
        overview = self.get_market_overview()
        
        # 2. 搜索市场新闻
        news = self.search_market_news()
        
        # 3. 生成复盘报告
        report = self.generate_market_review(overview, news)
        
        logger.info("========== 大盘复盘分析完成 ==========")
        
        return report


# 测试入口
if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
    )
    
    analyzer = MarketAnalyzer()
    
    # 测试获取市场概览
    overview = analyzer.get_market_overview()
    print(f"\n=== 市场概览 ===")
    print(f"日期: {overview.date}")
    print(f"指数数量: {len(overview.indices)}")
    for idx in overview.indices:
        print(f"  {idx.name}: {idx.current:.2f} ({idx.change_pct:+.2f}%)")
    print(f"上涨: {overview.up_count} | 下跌: {overview.down_count}")
    print(f"成交额: {overview.total_amount:.0f}亿")
    
    # 测试生成模板报告
    report = analyzer._generate_template_review(overview, [])
    print(f"\n=== 复盘报告 ===")
    print(report)
