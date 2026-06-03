"""
Automatic Position Exit Management Module

This module continuously monitors active positions and automatically places SELL orders
when stop loss or target prices are reached.

Features:
- Real-time LTP monitoring
- Stop loss and target hit detection
- Automatic SELL market order placement
- Duplicate order prevention
- Single exit guarantee (only one exit per position)
- Comprehensive logging
- Position cleanup after successful exits
"""

import threading
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Callable
from enum import Enum

# Configure logging
logger = logging.getLogger("position_exit_manager")
logger.setLevel(logging.INFO)


class ExitReason(Enum):
    """Enum for exit reasons"""
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    NONE = "NONE"


class Position:
    """
    Represents an active trading position with exit management state.
    
    Attributes:
        instrument_key: Upstox instrument key
        symbol: Trading symbol
        quantity: Number of shares/contracts
        buy_price: Price at which position was opened (optional)
        stop_loss: Stop loss price
        target: Target price
        entry_time: When position was opened
        is_processing: Flag to prevent duplicate exit orders
        exit_reason: Why the position exited (if exited)
        exit_time: When position exited (if exited)
        exit_order_id: Order ID of the exit order (if exited)
    """
    
    def __init__(self, instrument_key: str, symbol: str, quantity: int, 
                 stop_loss: float, target: float, buy_price: Optional[float] = None):
        self.instrument_key = instrument_key
        self.symbol = symbol
        self.quantity = quantity
        self.buy_price = buy_price
        self.stop_loss = stop_loss
        self.target = target
        self.entry_time = datetime.now(timezone.utc)
        
        # Exit management
        self.is_processing = False  # Lock to prevent duplicate orders
        self.exit_reason = ExitReason.NONE
        self.exit_time = None
        self.exit_order_id = None
        self.has_exited = False
    
    def check_exit_condition(self, ltp: float) -> ExitReason:
        """
        Check if position should be exited based on current LTP.
        
        Returns:
            ExitReason.STOP_LOSS if stop loss is hit
            ExitReason.TARGET if target is hit
            ExitReason.NONE if no exit condition is met
        """
        if self.has_exited:
            return ExitReason.NONE
        
        # Check stop loss first (lower priority in terms of checking order, but checked first)
        if ltp <= self.stop_loss:
            logger.info(
                "STOP LOSS HIT | instrument_key=%s symbol=%s ltp=%s stop_loss=%s quantity=%s",
                self.instrument_key, self.symbol, ltp, self.stop_loss, self.quantity
            )
            return ExitReason.STOP_LOSS
        
        # Check target (higher priority)
        if ltp >= self.target:
            logger.info(
                "TARGET HIT | instrument_key=%s symbol=%s ltp=%s target=%s quantity=%s",
                self.instrument_key, self.symbol, ltp, self.target, self.quantity
            )
            return ExitReason.TARGET
        
        return ExitReason.NONE
    
    def mark_as_exited(self, exit_reason: ExitReason, exit_order_id: str):
        """Mark position as successfully exited."""
        self.has_exited = True
        self.exit_reason = exit_reason
        self.exit_time = datetime.now(timezone.utc)
        self.exit_order_id = exit_order_id
        self.is_processing = False
        
        logger.info(
            "POSITION CLOSED | instrument_key=%s symbol=%s exit_reason=%s exit_order_id=%s "
            "entry_time=%s exit_time=%s duration_seconds=%s",
            self.instrument_key, self.symbol, exit_reason.value, exit_order_id,
            self.entry_time.isoformat(), self.exit_time.isoformat(),
            (self.exit_time - self.entry_time).total_seconds()
        )
    
    def to_dict(self) -> dict:
        """Convert position to dictionary for API responses."""
        return {
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "buy_price": self.buy_price,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "entry_time": self.entry_time.isoformat(),
            "has_exited": self.has_exited,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_order_id": self.exit_order_id,
            "is_processing": self.is_processing,
        }


class PositionExitManager:
    """
    Manages automatic position exits by monitoring LTP and triggering SELL orders.
    
    The manager continuously checks active positions against real-time market prices
    and automatically places exit orders when stop loss or target is hit.
    """
    
    def __init__(self, place_sell_order_callback: Callable, 
                 active_positions_dict: Dict = None,
                 subscribe_callback: Callable = None,
                 poll_interval: float = 0.5):
        """
        Initialize the position exit manager.
        
        Args:
            place_sell_order_callback: Function to call for placing SELL orders.
                                      Signature: func(instrument_key, symbol, quantity) -> dict
            active_positions_dict: Reference to the ACTIVE_POSITIONS dict from server.py
            subscribe_callback: Optional callback to subscribe to real-time updates.
                               Signature: func(symbols: list) -> None
            poll_interval: How often to check for exit conditions (seconds)
        """
        self.place_sell_order = place_sell_order_callback
        self.active_positions_dict = active_positions_dict or {}
        self.subscribe_callback = subscribe_callback
        self.poll_interval = poll_interval
        
        # Track managed positions separately with Position objects
        self.managed_positions: Dict[str, Position] = {}
        self.symbol_to_instrument_keys: Dict[str, set] = {}
        self.instrument_key_to_symbol: Dict[str, str] = {}
        self.lock = threading.RLock()
        
        # Run state
        self._stop = False
        self._thread = None
        self._monitoring = False
        
        # Market data callback
        self.current_ltp: Dict[str, float] = {}
    
    def add_position(self, instrument_key: str, symbol: str, quantity: int,
                    stop_loss: float, target: float, buy_price: Optional[float] = None):
        """
        Add a new position to be monitored.
        
        Args:
            instrument_key: Upstox instrument key
            symbol: Trading symbol
            quantity: Position size
            stop_loss: Stop loss price
            target: Target price
            buy_price: Entry price (optional, for logging)
        """
        with self.lock:
            logger.info(
                "POSITION OPENED | instrument_key=%s symbol=%s quantity=%s "
                "buy_price=%s stop_loss=%s target=%s",
                instrument_key, symbol, quantity, buy_price, stop_loss, target
            )
            
            position = Position(
                instrument_key=instrument_key,
                symbol=symbol,
                quantity=quantity,
                stop_loss=stop_loss,
                target=target,
                buy_price=buy_price
            )
            self.managed_positions[instrument_key] = position
            self.instrument_key_to_symbol[instrument_key] = symbol
            self.symbol_to_instrument_keys.setdefault(symbol, set()).add(instrument_key)
            
            # Subscribe to real-time updates for this position
            if self.subscribe_callback:
                try:
                    self.subscribe_callback([instrument_key])
                    logger.info(
                        "SUBSCRIBED_TO_REALTIME | instrument_key=%s symbol=%s",
                        instrument_key, symbol
                    )
                except Exception as e:
                    logger.warning(
                        "SUBSCRIBE_TO_REALTIME_FAILED | instrument_key=%s symbol=%s error=%s",
                        instrument_key, symbol, str(e)
                    )
    
    def update_ltp(self, instrument_key: str, ltp: float):
        """Update the current LTP for an instrument or symbol alias."""
        with self.lock:
            if instrument_key in self.managed_positions:
                self.current_ltp[instrument_key] = ltp

            if instrument_key in self.symbol_to_instrument_keys:
                for mapped_key in self.symbol_to_instrument_keys[instrument_key]:
                    self.current_ltp[mapped_key] = ltp

            symbol = self.instrument_key_to_symbol.get(instrument_key)
            if symbol:
                self.current_ltp[symbol] = ltp

            # Also preserve the raw update key if not already tracked
            self.current_ltp[instrument_key] = ltp
    
    def _check_and_execute_exits(self):
        """Check all positions and execute exits if conditions are met."""
        with self.lock:
            positions_to_check = list(self.managed_positions.items())
        
        for instrument_key, position in positions_to_check:
            # Skip already exited positions
            if position.has_exited:
                continue
            
            # Skip if currently processing (duplicate protection)
            if position.is_processing:
                logger.debug(
                    "POSITION_SKIP_PROCESSING | instrument_key=%s symbol=%s",
                    instrument_key, position.symbol
                )
                continue
            
            # Get current LTP
            ltp = self.current_ltp.get(instrument_key)
            if ltp is None:
                logger.debug(
                    "POSITION_SKIP_NO_LTP | instrument_key=%s symbol=%s",
                    instrument_key, position.symbol
                )
                continue
            
            # Check exit condition
            exit_reason = position.check_exit_condition(ltp)
            
            if exit_reason != ExitReason.NONE:
                # Mark as processing to prevent duplicate orders
                with self.lock:
                    position.is_processing = True
                
                # Place exit order
                self._execute_exit(position, exit_reason)
    
    def _execute_exit(self, position: Position, exit_reason: ExitReason):
        """
        Execute an exit order for a position.
        
        Args:
            position: Position object to exit
            exit_reason: Reason for exit (STOP_LOSS or TARGET)
        """
        try:
            logger.info(
                "AUTO SELL EXECUTING | instrument_key=%s symbol=%s quantity=%s "
                "exit_reason=%s ltp=%s",
                position.instrument_key, position.symbol, position.quantity,
                exit_reason.value, self.current_ltp.get(position.instrument_key)
            )
            
            # Call the sell order callback
            result = self.place_sell_order(
                instrument_key=position.instrument_key,
                symbol=position.symbol,
                quantity=position.quantity,
                exit_reason=exit_reason
            )
            
            # Extract order ID from result
            order_id = self._extract_order_id(result)
            
            if order_id:
                logger.info(
                    "AUTO SELL EXECUTED | instrument_key=%s symbol=%s order_id=%s "
                    "exit_reason=%s",
                    position.instrument_key, position.symbol, order_id,
                    exit_reason.value
                )
                
                # Mark position as exited
                with self.lock:
                    position.mark_as_exited(exit_reason, order_id)
                
                # Remove from active tracking
                self._remove_position(position.instrument_key)
            else:
                logger.error(
                    "AUTO SELL FAILED_NO_ORDER_ID | instrument_key=%s symbol=%s "
                    "exit_reason=%s result=%s",
                    position.instrument_key, position.symbol, exit_reason.value, result
                )
                
                # Reset processing flag so we can retry
                with self.lock:
                    position.is_processing = False
        
        except Exception as e:
            logger.error(
                "AUTO SELL ERROR | instrument_key=%s symbol=%s exit_reason=%s error=%s",
                position.instrument_key, position.symbol, exit_reason.value, str(e),
                exc_info=True
            )
            
            # Reset processing flag so we can retry
            with self.lock:
                position.is_processing = False
    
    def _extract_order_id(self, result: dict) -> Optional[str]:
        """Extract order ID from sell order callback result."""
        if not isinstance(result, dict):
            return None
        
        # Try common key names
        for key in ['order_id', 'id', 'data', 'order_number']:
            value = result.get(key)
            if isinstance(value, str):
                return value
            elif isinstance(value, dict):
                # Try to extract from nested data
                nested_id = value.get('order_id') or value.get('id')
                if nested_id:
                    return nested_id
        
        return None
    
    def _remove_position(self, instrument_key: str):
        """Remove a position from tracking after successful exit."""
        with self.lock:
            symbol = self.instrument_key_to_symbol.get(instrument_key)
            if instrument_key in self.managed_positions:
                del self.managed_positions[instrument_key]
                logger.debug(
                    "POSITION_REMOVED_FROM_TRACKING | instrument_key=%s",
                    instrument_key
                )

            if symbol:
                mapped = self.symbol_to_instrument_keys.get(symbol)
                if mapped and instrument_key in mapped:
                    mapped.discard(instrument_key)
                    if not mapped:
                        self.symbol_to_instrument_keys.pop(symbol, None)
                self.instrument_key_to_symbol.pop(instrument_key, None)

            if self.active_positions_dict and instrument_key in self.active_positions_dict:
                del self.active_positions_dict[instrument_key]
                logger.debug(
                    "POSITION_REMOVED_FROM_ACTIVE | instrument_key=%s",
                    instrument_key
                )
    
    def _monitoring_loop(self):
        """Main monitoring loop that runs in a background thread."""
        logger.info("MONITORING_STARTED | poll_interval=%s", self.poll_interval)
        self._monitoring = True
        
        while not self._stop:
            try:
                # Check all positions and execute exits if needed
                self._check_and_execute_exits()
                
                # Sleep before next check
                time.sleep(self.poll_interval)
            
            except Exception as e:
                logger.error(
                    "MONITORING_LOOP_ERROR | error=%s",
                    str(e),
                    exc_info=True
                )
                time.sleep(self.poll_interval)
        
        self._monitoring = False
        logger.info("MONITORING_STOPPED")
    
    def start(self):
        """Start the background monitoring thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("MONITORING_ALREADY_RUNNING")
            return
        
        self._stop = False
        self._thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self._thread.start()
        logger.info("POSITION_EXIT_MANAGER_STARTED")
    
    def stop(self):
        """Stop the background monitoring thread."""
        self._stop = True
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("POSITION_EXIT_MANAGER_STOPPED")
    
    def get_status(self) -> dict:
        """Get current status of position exit manager."""
        with self.lock:
            active_positions = []
            exited_positions = []
            
            for instrument_key, position in self.managed_positions.items():
                pos_dict = position.to_dict()
                if position.has_exited:
                    exited_positions.append(pos_dict)
                else:
                    pos_dict['current_ltp'] = self.current_ltp.get(instrument_key)
                    active_positions.append(pos_dict)
        
        return {
            "is_running": self._monitoring,
            "active_positions_count": len(active_positions),
            "exited_positions_count": len(exited_positions),
            "active_positions": active_positions,
            "exited_positions": exited_positions,
        }
    
    def get_position(self, instrument_key: str) -> Optional[dict]:
        """Get details for a specific position."""
        with self.lock:
            position = self.managed_positions.get(instrument_key)
            if position:
                pos_dict = position.to_dict()
                pos_dict['current_ltp'] = self.current_ltp.get(instrument_key)
                return pos_dict
        return None


# Global instance
_manager: Optional[PositionExitManager] = None


def initialize_manager(place_sell_order_callback: Callable, 
                       active_positions_dict: Dict = None,
                       subscribe_callback: Callable = None,
                       poll_interval: float = 0.5) -> PositionExitManager:
    """
    Initialize the global position exit manager instance.
    
    Should be called once at server startup.
    
    Args:
        place_sell_order_callback: Function to place SELL orders
        active_positions_dict: Reference to ACTIVE_POSITIONS dict
        subscribe_callback: Optional callback to subscribe to real-time updates
        poll_interval: How often to check exit conditions
    
    Returns:
        The initialized PositionExitManager instance
    """
    global _manager
    _manager = PositionExitManager(
        place_sell_order_callback=place_sell_order_callback,
        active_positions_dict=active_positions_dict,
        subscribe_callback=subscribe_callback,
        poll_interval=poll_interval
    )
    return _manager


def get_manager() -> Optional[PositionExitManager]:
    """Get the global position exit manager instance."""
    return _manager


def add_external_subscriptions(instrument_keys: list):
    """Subscribe the realtime system to active position instruments."""
    if not _manager or not _manager.subscribe_callback:
        raise RuntimeError("Position exit manager not initialized or subscribe callback missing")
    return _manager.subscribe_callback(instrument_keys)


def add_position(instrument_key: str, symbol: str, quantity: int,
                stop_loss: float, target: float, buy_price: Optional[float] = None):
    """Add a new position to be monitored."""
    if _manager:
        _manager.add_position(instrument_key, symbol, quantity, stop_loss, target, buy_price)


def update_ltp(instrument_key: str, ltp: float):
    """Update LTP for an instrument."""
    if _manager:
        _manager.update_ltp(instrument_key, ltp)


def get_status() -> dict:
    """Get current status."""
    if _manager:
        return _manager.get_status()
    return {"is_running": False, "error": "Manager not initialized"}
