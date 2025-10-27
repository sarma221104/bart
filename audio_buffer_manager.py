"""
Production-Grade Audio Buffer Manager
Prevents memory leaks by properly managing audio buffers and streams.
Supports high-concurrency audio processing without memory accumulation.
"""

import logging
import gc
import threading
import time
import weakref
from typing import Dict, Any, Optional, Set
from collections import deque
import asyncio
from contextlib import asynccontextmanager

class AudioBufferManager:
    """
    Production-grade audio buffer manager with automatic cleanup and memory monitoring.
    Prevents memory leaks from accumulating audio buffers and streams.
    """
    
    def __init__(self, max_buffer_size: int = None, cleanup_interval: int = None):  # Will use environment variables
        import os
        self.max_buffer_size = max_buffer_size or int(os.getenv("WEBSOCKET_MAX_SIZE"))
        self.cleanup_interval = cleanup_interval or int(os.getenv("CLEANUP_INTERVAL"))
        
        # Track active buffers using weak references
        self._active_buffers: Set[weakref.ReferenceType] = set()
        self._buffer_lock = threading.RLock()
        self._last_cleanup = time.time()
        
        # Statistics for monitoring
        self._stats = {
            'buffers_created': 0,
            'buffers_cleaned': 0,
            'total_bytes_processed': 0,
            'max_concurrent_buffers': 0
        }
        
        logging.info(f"AudioBufferManager initialized - max_buffer_size={max_buffer_size}, cleanup_interval={cleanup_interval}s")
    
    def _should_cleanup(self) -> bool:
        """Check if cleanup is needed"""
        return time.time() - self._last_cleanup > self.cleanup_interval
    
    def _cleanup_expired_buffers(self):
        """Clean up expired buffer references"""
        try:
            with self._buffer_lock:
                initial_count = len(self._active_buffers)
                
                # Remove dead references
                self._active_buffers = {ref for ref in self._active_buffers if ref() is not None}
                
                cleaned_count = initial_count - len(self._active_buffers)
                if cleaned_count > 0:
                    self._stats['buffers_cleaned'] += cleaned_count
                    logging.info(f"Cleaned up {cleaned_count} expired audio buffer references")
                
                # Force garbage collection to free memory
                gc.collect()
                
                self._last_cleanup = time.time()
                
        except Exception as e:
            logging.error(f"Error during audio buffer cleanup: {str(e)}")
    
    def create_managed_buffer(self, initial_data: bytes = None) -> 'ManagedAudioBuffer':
        """
        Create a managed audio buffer with automatic cleanup tracking.
        """
        if self._should_cleanup():
            self._cleanup_expired_buffers()
        
        buffer = ManagedAudioBuffer(
            manager=self,
            max_size=self.max_buffer_size,
            initial_data=initial_data
        )
        
        # Track the buffer using weak reference
        with self._buffer_lock:
            buffer_ref = weakref.ref(buffer, self._on_buffer_deleted)
            self._active_buffers.add(buffer_ref)
            
            self._stats['buffers_created'] += 1
            current_count = len(self._active_buffers)
            if current_count > self._stats['max_concurrent_buffers']:
                self._stats['max_concurrent_buffers'] = current_count
        
        logging.debug(f"Created managed audio buffer (total active: {len(self._active_buffers)})")
        return buffer
    
    def _on_buffer_deleted(self, buffer_ref):
        """Callback when a buffer is garbage collected"""
        with self._buffer_lock:
            self._active_buffers.discard(buffer_ref)
        logging.debug(f"Audio buffer garbage collected (remaining: {len(self._active_buffers)})")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get buffer manager statistics for monitoring"""
        with self._buffer_lock:
            return {
                **self._stats,
                'active_buffers': len(self._active_buffers),
                'last_cleanup': self._last_cleanup
            }
    
    def force_cleanup(self):
        """Force immediate cleanup of all expired buffers"""
        self._last_cleanup = 0
        self._cleanup_expired_buffers()


class ManagedAudioBuffer:
    """
    Managed audio buffer with automatic memory management and size limits.
    """
    
    def __init__(self, manager: AudioBufferManager, max_size: int, initial_data: bytes = None):
        self.manager = manager
        self.max_size = max_size
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self._closed = False
        
        if initial_data:
            self.extend(initial_data)
    
    def extend(self, data: bytes) -> bool:
        """
        Extend buffer with new data, respecting size limits.
        Returns False if buffer is full or closed.
        """
        if self._closed:
            logging.warning(f"Attempted to extend closed buffer with {len(data)} bytes")
            return False
            
        with self._lock:
            current_size = len(self._buffer)
            new_size = current_size + len(data)
            
            if new_size > self.max_size:
                # Buffer is full - prevent memory overflow
                logging.warning(f"Audio buffer full ({current_size} bytes), dropping {len(data)} bytes (would exceed max {self.max_size})")
                # Clear buffer to make room for new data if it's getting too full
                if current_size > self.max_size * 0.8:  # If buffer is 80% full, clear it
                    self._buffer.clear()
                    logging.info("Cleared audio buffer due to overflow prevention")
                    return False
                return False
            
            self._buffer.extend(data)
            self.manager._stats['total_bytes_processed'] += len(data)
            logging.debug(f"Extended audio buffer: {current_size} -> {len(self._buffer)} bytes")
            return True
    
    def get_data(self) -> bytes:
        """Get a copy of buffer data"""
        with self._lock:
            return bytes(self._buffer)
    
    def is_healthy(self) -> bool:
        """Check if buffer is in a healthy state"""
        with self._lock:
            return not self._closed and len(self._buffer) < self.max_size * 0.9
    
    def get_usage_percentage(self) -> float:
        """Get buffer usage as a percentage"""
        with self._lock:
            return (len(self._buffer) / self.max_size) * 100.0
    
    def clear(self):
        """Clear buffer contents"""
        with self._lock:
            if not self._closed:
                self._buffer.clear()
                logging.debug("Audio buffer cleared")
    
    def close(self):
        """Close and cleanup buffer"""
        with self._lock:
            if not self._closed:
                self._buffer.clear()
                self._closed = True
                logging.debug("Audio buffer closed")
    
    def __len__(self) -> int:
        """Get buffer length"""
        with self._lock:
            return len(self._buffer)
    
    def __del__(self):
        """Ensure cleanup on deletion"""
        self.close()


class AudioStreamManager:
    """
    Manager for PCM and other audio streams with proper lifecycle management.
    """
    
    def __init__(self):
        self._active_streams: Set[weakref.ReferenceType] = set()
        self._stream_lock = threading.RLock()
        
        logging.info("AudioStreamManager initialized")
    
    @asynccontextmanager
    async def managed_pcm_stream(self, audio_data: bytes):
        """
        Context manager for PCM streams with automatic cleanup.
        """
        stream = None
        try:
            # Import here to avoid circular imports
            from main import PCMStream
            stream = PCMStream(audio_data)
            
            # Track the stream
            with self._stream_lock:
                stream_ref = weakref.ref(stream)
                self._active_streams.add(stream_ref)
            
            logging.debug(f"Created managed PCM stream ({len(audio_data)} bytes)")
            yield stream
            
        except Exception as e:
            logging.error(f"Error in managed PCM stream: {str(e)}")
            raise
        finally:
            # Cleanup
            if stream:
                try:
                    # Clean up stream resources
                    if hasattr(stream, 'close'):
                        stream.close()
                    elif hasattr(stream, 'audio_bytes'):
                        stream.audio_bytes = None
                except Exception as e:
                    logging.warning(f"Error cleaning up PCM stream: {str(e)}")
                finally:
                    stream = None
            
            # Remove from tracking
            with self._stream_lock:
                # Clean up dead references
                self._active_streams = {ref for ref in self._active_streams if ref() is not None}
            
            # Force garbage collection
            gc.collect()
            logging.debug("PCM stream cleaned up")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get stream manager statistics"""
        with self._stream_lock:
            # Clean up dead references before reporting
            self._active_streams = {ref for ref in self._active_streams if ref() is not None}
            return {
                'active_streams': len(self._active_streams)
            }


# Global instances
audio_buffer_manager = AudioBufferManager()
audio_stream_manager = AudioStreamManager()

# Convenience functions
def create_managed_buffer(initial_data: bytes = None) -> ManagedAudioBuffer:
    """Create a managed audio buffer"""
    return audio_buffer_manager.create_managed_buffer(initial_data)

@asynccontextmanager
async def managed_pcm_stream(audio_data: bytes):
    """Context manager for PCM streams"""
    async with audio_stream_manager.managed_pcm_stream(audio_data) as stream:
        yield stream

def get_audio_stats() -> Dict[str, Any]:
    """Get comprehensive audio management statistics"""
    return {
        'buffer_manager': audio_buffer_manager.get_stats(),
        'stream_manager': audio_stream_manager.get_stats()
    }

def cleanup_audio_resources():
    """Clean up all audio resources"""
    audio_buffer_manager.force_cleanup()
    logging.info("Audio resources cleaned up")
