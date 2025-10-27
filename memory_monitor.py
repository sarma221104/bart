"""
Production-Grade Memory Monitoring System
Lightweight monitoring and alerting for memory leaks and resource usage.
Designed for high-concurrency environments supporting 2k+ users.
"""

import logging
import asyncio
import psutil
import gc
import time
import threading
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import weakref

@dataclass
class MemoryStats:
    """Memory statistics snapshot"""
    timestamp: datetime
    total_memory_mb: float
    available_memory_mb: float
    used_memory_mb: float
    memory_percent: float
    cpu_percent: float
    active_connections: int
    gc_collections: Dict[int, int] = field(default_factory=dict)

class MemoryMonitor:
    """
    Production-grade memory monitor with alerting and automatic cleanup.
    Prevents memory leaks and provides early warning for resource exhaustion.
    """
    
    def __init__(self, 
                 memory_threshold: float = None,  # Will use environment variable
                 cpu_threshold: float = None,     # Will use environment variable
                 check_interval: int = None,
                 alert_interval: int = None):
        import os
        self.memory_threshold = memory_threshold or float(os.getenv("MAX_MEMORY_PERCENT"))
        self.cpu_threshold = cpu_threshold or float(os.getenv("MAX_CPU_PERCENT"))
        self.check_interval = check_interval or int(os.getenv("MEMORY_CHECK_INTERVAL"))
        self.alert_interval = alert_interval or int(os.getenv("ALERT_INTERVAL"))
        
        # Monitoring state
        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._last_alert = 0
        self._stats_history = []
        self._max_history = 100  # Keep last 100 readings
        
        # Connection tracking
        self._connection_count = 0
        self._connection_lock = threading.RLock()
        
        # Alert callbacks
        self._alert_callbacks = []
        
        # GC tracking
        self._last_gc_stats = {0: 0, 1: 0, 2: 0}
        
        logging.info(f"MemoryMonitor initialized - thresholds: memory={memory_threshold}%, cpu={cpu_threshold}%")
    
    def add_alert_callback(self, callback):
        """Add a callback function for memory alerts"""
        self._alert_callbacks.append(callback)
    
    def increment_connections(self):
        """Increment active connection count"""
        with self._connection_lock:
            self._connection_count += 1
    
    def decrement_connections(self):
        """Decrement active connection count"""
        with self._connection_lock:
            self._connection_count = max(0, self._connection_count - 1)
    
    def get_current_stats(self) -> MemoryStats:
        """Get current system memory and resource statistics"""
        try:
            # Memory statistics
            memory = psutil.virtual_memory()
            
            # CPU statistics
            cpu_percent = psutil.cpu_percent(interval=0.1)
            
            # GC statistics
            gc_stats = {}
            for generation in range(3):
                gc_stats[generation] = gc.get_count()[generation]
            
            # Connection count
            with self._connection_lock:
                connections = self._connection_count
            
            return MemoryStats(
                timestamp=datetime.utcnow(),
                total_memory_mb=memory.total / (1024 * 1024),
                available_memory_mb=memory.available / (1024 * 1024),
                used_memory_mb=memory.used / (1024 * 1024),
                memory_percent=memory.percent,
                cpu_percent=cpu_percent,
                active_connections=connections,
                gc_collections=gc_stats
            )
        except Exception as e:
            logging.error(f"Error getting memory stats: {str(e)}")
            return MemoryStats(
                timestamp=datetime.utcnow(),
                total_memory_mb=0,
                available_memory_mb=0,
                used_memory_mb=0,
                memory_percent=0,
                cpu_percent=0,
                active_connections=0
            )
    
    def _should_alert(self, stats: MemoryStats) -> bool:
        """Check if an alert should be sent"""
        current_time = time.time()
        
        # Don't alert too frequently
        if current_time - self._last_alert < self.alert_interval:
            return False
        
        # Check thresholds
        memory_critical = stats.memory_percent > self.memory_threshold
        cpu_critical = stats.cpu_percent > self.cpu_threshold
        
        return memory_critical or cpu_critical
    
    async def _send_alert(self, stats: MemoryStats, message: str):
        """Send alert through registered callbacks"""
        try:
            alert_data = {
                'timestamp': stats.timestamp.isoformat(),
                'message': message,
                'memory_percent': stats.memory_percent,
                'cpu_percent': stats.cpu_percent,
                'active_connections': stats.active_connections,
                'available_memory_mb': stats.available_memory_mb
            }
            
            # Call registered alert callbacks
            for callback in self._alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(alert_data)
                    else:
                        callback(alert_data)
                except Exception as e:
                    logging.error(f"Error in alert callback: {str(e)}")
            
            self._last_alert = time.time()
            logging.warning(f"Memory alert sent: {message}")
            
        except Exception as e:
            logging.error(f"Error sending memory alert: {str(e)}")
    
    async def _perform_emergency_cleanup(self):
        """Perform emergency cleanup when memory is critically low"""
        try:
            logging.warning("Performing emergency memory cleanup...")
            
            # Force garbage collection
            collected = gc.collect()
            logging.info(f"Emergency GC collected {collected} objects")
            
            # Clean up AWS clients
            try:
                from aws_client_manager import cleanup_aws_clients
                cleanup_aws_clients()
            except Exception as e:
                logging.warning(f"Error cleaning AWS clients: {str(e)}")
            
            # Clean up audio resources
            try:
                from audio_buffer_manager import cleanup_audio_resources
                cleanup_audio_resources()
            except Exception as e:
                logging.warning(f"Error cleaning audio resources: {str(e)}")
            
            logging.info("Emergency cleanup completed")
            
        except Exception as e:
            logging.error(f"Error during emergency cleanup: {str(e)}")
    
    async def _monitor_loop(self):
        """Main monitoring loop"""
        while self._monitoring:
            try:
                # Get current statistics
                stats = self.get_current_stats()
                
                # Add to history
                self._stats_history.append(stats)
                if len(self._stats_history) > self._max_history:
                    self._stats_history.pop(0)
                
                # Check for critical conditions
                critical_memory = stats.memory_percent > self.memory_threshold
                critical_cpu = stats.cpu_percent > self.cpu_threshold
                
                if critical_memory or critical_cpu:
                    alert_message = f"Resource usage critical: Memory {stats.memory_percent:.1f}%, CPU {stats.cpu_percent:.1f}%"
                    
                    if self._should_alert(stats):
                        await self._send_alert(stats, alert_message)
                    
                    # Perform emergency cleanup if memory is very high
                    if stats.memory_percent > 90:
                        await self._perform_emergency_cleanup()
                
                # Check for memory leaks (increasing trend)
                if len(self._stats_history) >= 10:
                    recent_stats = self._stats_history[-10:]
                    memory_trend = recent_stats[-1].memory_percent - recent_stats[0].memory_percent
                    
                    if memory_trend > 10:  # Memory increased by more than 10% in recent history
                        leak_message = f"Potential memory leak detected: {memory_trend:.1f}% increase in recent monitoring"
                        if self._should_alert(stats):
                            await self._send_alert(stats, leak_message)
                
                await asyncio.sleep(self.check_interval)
                
            except Exception as e:
                logging.error(f"Error in memory monitor loop: {str(e)}")
                await asyncio.sleep(self.check_interval)
    
    async def start_monitoring(self):
        """Start the memory monitoring task"""
        if self._monitoring:
            return
        
        self._monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logging.info("Memory monitoring started")
    
    async def stop_monitoring(self):
        """Stop the memory monitoring task"""
        if not self._monitoring:
            return
        
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        logging.info("Memory monitoring stopped")
    
    def get_stats_summary(self) -> Dict[str, Any]:
        """Get summary of monitoring statistics"""
        if not self._stats_history:
            return {"error": "No monitoring data available"}
        
        current = self._stats_history[-1]
        
        # Calculate averages over last hour (120 readings at 30s intervals)
        recent_count = min(120, len(self._stats_history))
        recent_stats = self._stats_history[-recent_count:]
        
        avg_memory = sum(s.memory_percent for s in recent_stats) / len(recent_stats)
        avg_cpu = sum(s.cpu_percent for s in recent_stats) / len(recent_stats)
        max_memory = max(s.memory_percent for s in recent_stats)
        max_connections = max(s.active_connections for s in recent_stats)
        
        return {
            "current": {
                "memory_percent": current.memory_percent,
                "cpu_percent": current.cpu_percent,
                "active_connections": current.active_connections,
                "available_memory_mb": current.available_memory_mb,
                "timestamp": current.timestamp.isoformat()
            },
            "averages": {
                "memory_percent": avg_memory,
                "cpu_percent": avg_cpu,
                "period_minutes": recent_count * (self.check_interval / 60)
            },
            "peaks": {
                "max_memory_percent": max_memory,
                "max_connections": max_connections
            },
            "monitoring": {
                "is_active": self._monitoring,
                "total_readings": len(self._stats_history),
                "memory_threshold": self.memory_threshold,
                "cpu_threshold": self.cpu_threshold
            }
        }


# Global memory monitor instance
memory_monitor = MemoryMonitor()

# Convenience functions
def get_memory_stats() -> MemoryStats:
    """Get current memory statistics"""
    return memory_monitor.get_current_stats()

def increment_connections():
    """Increment connection count"""
    memory_monitor.increment_connections()

def decrement_connections():
    """Decrement connection count"""
    memory_monitor.decrement_connections()

async def start_memory_monitoring():
    """Start memory monitoring"""
    await memory_monitor.start_monitoring()

async def stop_memory_monitoring():
    """Stop memory monitoring"""
    await memory_monitor.stop_monitoring()

def get_monitoring_summary() -> Dict[str, Any]:
    """Get monitoring summary"""
    return memory_monitor.get_stats_summary()

# Alert callback for logging
def log_memory_alert(alert_data: Dict[str, Any]):
    """Default alert callback that logs to the application log"""
    logging.critical(
        f"MEMORY ALERT: {alert_data['message']} - "
        f"Memory: {alert_data['memory_percent']:.1f}%, "
        f"CPU: {alert_data['cpu_percent']:.1f}%, "
        f"Connections: {alert_data['active_connections']}, "
        f"Available: {alert_data['available_memory_mb']:.0f}MB"
    )

# Register default alert callback
memory_monitor.add_alert_callback(log_memory_alert)
