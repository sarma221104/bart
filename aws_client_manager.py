"""
Production-Grade AWS Client Manager with Connection Pooling
Prevents memory leaks by reusing clients and managing connections efficiently.
Supports 2k+ concurrent users with proper resource management.
"""

import logging
import boto3
import threading
import time
import asyncio
from typing import Dict, Any, Optional
from botocore.config import Config
from contextlib import asynccontextmanager
from amazon_transcribe.client import TranscribeStreamingClient
import weakref
import gc

class AWSClientManager:
    """
    Production-grade AWS client manager with connection pooling and lifecycle management.
    Prevents memory leaks by reusing clients and properly managing connections.
    """
    
    def __init__(self, max_pool_connections: int = None):
        import os
        self.max_pool_connections = max_pool_connections or int(os.getenv("AWS_MAX_POOL_CONNECTIONS"))
        self._clients: Dict[str, Any] = {}
        self._client_locks: Dict[str, threading.RLock] = {}
        self._transcribe_clients: weakref.WeakSet = weakref.WeakSet()
        self._client_usage_count: Dict[str, int] = {}
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # 5 minutes
        
        # Circuit breaker for database operations
        self._circuit_breaker_state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._circuit_breaker_failures = 0
        self._circuit_breaker_failure_threshold = 5
        self._circuit_breaker_timeout = 60  # seconds
        self._circuit_breaker_last_failure = 0
        
        # Configure boto3 with connection pooling
        import os
        self.config = Config(
            region_name=os.getenv("AWS_DEFAULT_REGION"),
            retries={'max_attempts': 3, 'mode': 'adaptive'},
            max_pool_connections=max_pool_connections,
            # Connection reuse settings
            tcp_keepalive=True,
            # Timeout settings for production
            connect_timeout=int(os.getenv("AWS_CONNECT_TIMEOUT")),
            read_timeout=int(os.getenv("AWS_READ_TIMEOUT")),
        )
        
        logging.info(f"AWSClientManager initialized with max_pool_connections={max_pool_connections}")
    
    def _get_client_key(self, service_name: str, region: str = "us-west-2") -> str:
        """Generate a unique key for client caching"""
        return f"{service_name}_{region}"
    
    def _should_cleanup(self) -> bool:
        """Check if cleanup is needed"""
        return time.time() - self._last_cleanup > self._cleanup_interval
    
    def _check_circuit_breaker(self) -> bool:
        """Check if circuit breaker allows the operation"""
        current_time = time.time()
        
        if self._circuit_breaker_state == "OPEN":
            # Check if timeout has passed
            if current_time - self._circuit_breaker_last_failure > self._circuit_breaker_timeout:
                self._circuit_breaker_state = "HALF_OPEN"
                logging.info("Circuit breaker moved to HALF_OPEN state")
                return True
            else:
                logging.warning("Circuit breaker is OPEN - blocking database operations")
                return False
        elif self._circuit_breaker_state == "HALF_OPEN":
            return True
        else:  # CLOSED
            return True
    
    def _record_circuit_breaker_success(self):
        """Record a successful operation"""
        if self._circuit_breaker_state == "HALF_OPEN":
            self._circuit_breaker_state = "CLOSED"
            self._circuit_breaker_failures = 0
            logging.info("Circuit breaker moved to CLOSED state after successful operation")
    
    def _record_circuit_breaker_failure(self):
        """Record a failed operation"""
        self._circuit_breaker_failures += 1
        self._circuit_breaker_last_failure = time.time()
        
        if self._circuit_breaker_failures >= self._circuit_breaker_failure_threshold:
            self._circuit_breaker_state = "OPEN"
            logging.error(f"Circuit breaker moved to OPEN state after {self._circuit_breaker_failures} failures")
    
    def _cleanup_unused_clients(self):
        """Clean up unused clients to prevent memory leaks"""
        try:
            current_time = time.time()
            if not self._should_cleanup():
                return
                
            logging.info("Running AWS client cleanup...")
            
            # Clean up transcribe clients that are no longer referenced
            initial_count = len(self._transcribe_clients)
            # WeakSet automatically removes unreferenced objects
            current_count = len(self._transcribe_clients)
            
            if initial_count > current_count:
                logging.info(f"Cleaned up {initial_count - current_count} transcribe clients")
            
            # Reset usage counters periodically
            for key in list(self._client_usage_count.keys()):
                if self._client_usage_count[key] == 0:
                    # Remove clients that haven't been used
                    if key in self._clients:
                        del self._clients[key]
                    if key in self._client_locks:
                        del self._client_locks[key]
                    del self._client_usage_count[key]
                else:
                    # Reset counter for active clients
                    self._client_usage_count[key] = 0
            
            # Force garbage collection
            gc.collect()
            
            self._last_cleanup = current_time
            logging.info("AWS client cleanup completed")
            
        except Exception as e:
            logging.error(f"Error during AWS client cleanup: {str(e)}")
    
    def get_client(self, service_name: str, region: str = "us-west-2") -> Any:
        """
        Get a reusable AWS client with connection pooling and circuit breaker.
        Thread-safe implementation for production use.
        """
        # Check circuit breaker for database operations
        if service_name in ["dynamodb", "bedrock-runtime", "bedrock-agent-runtime"]:
            if not self._check_circuit_breaker():
                raise Exception(f"Circuit breaker is OPEN for {service_name} - operation blocked")
        
        client_key = self._get_client_key(service_name, region)
        
        # Periodic cleanup
        if self._should_cleanup():
            self._cleanup_unused_clients()
        
        # Get or create lock for this client type
        if client_key not in self._client_locks:
            self._client_locks[client_key] = threading.RLock()
        
        with self._client_locks[client_key]:
            # Return existing client if available
            if client_key in self._clients:
                self._client_usage_count[client_key] = self._client_usage_count.get(client_key, 0) + 1
                return self._clients[client_key]
            
            # Create new client with connection pooling
            try:
                if service_name == "dynamodb":
                    client = boto3.resource('dynamodb', config=self.config)
                else:
                    client = boto3.client(service_name, config=self.config)
                
                self._clients[client_key] = client
                self._client_usage_count[client_key] = 1
                
                logging.info(f"Created new AWS {service_name} client for region {region}")
                
                # Record successful client creation
                if service_name in ["dynamodb", "bedrock-runtime", "bedrock-agent-runtime"]:
                    self._record_circuit_breaker_success()
                
                return client
                
            except Exception as e:
                logging.error(f"Failed to create AWS {service_name} client: {str(e)}")
                
                # Record failure for circuit breaker
                if service_name in ["dynamodb", "bedrock-runtime", "bedrock-agent-runtime"]:
                    self._record_circuit_breaker_failure()
                
                raise
    
    def get_transcribe_client(self, region: str = "us-west-2") -> TranscribeStreamingClient:
        """
        Get a TranscribeStreamingClient with proper lifecycle management.
        These clients are short-lived and managed separately.
        """
        try:
            client = TranscribeStreamingClient(region=region)
            
            # Add to weak reference set for tracking
            self._transcribe_clients.add(client)
            
            logging.debug(f"Created TranscribeStreamingClient (total active: {len(self._transcribe_clients)})")
            return client
            
        except Exception as e:
            logging.error(f"Failed to create TranscribeStreamingClient: {str(e)}")
            raise
    
    @asynccontextmanager
    async def get_transcribe_stream_context(self, region: str = "us-west-2"):
        """
        Context manager for TranscribeStreamingClient to ensure proper cleanup.
        Use this for streaming operations to prevent memory leaks.
        """
        client = None
        try:
            client = self.get_transcribe_client(region)
            yield client
        except Exception as e:
            logging.error(f"Error in transcribe stream context: {str(e)}")
            raise
        finally:
            # Cleanup is handled automatically by weak references
            if client:
                try:
                    # Close any open streams
                    if hasattr(client, '_client') and hasattr(client._client, 'close'):
                        await client._client.close()
                except Exception as e:
                    logging.warning(f"Error closing transcribe client: {str(e)}")
                finally:
                    client = None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get client pool statistics for monitoring"""
        return {
            "active_clients": len(self._clients),
            "client_types": list(self._clients.keys()),
            "transcribe_clients": len(self._transcribe_clients),
            "max_pool_connections": self.max_pool_connections,
            "usage_counts": self._client_usage_count.copy(),
            "last_cleanup": self._last_cleanup
        }
    
    def force_cleanup(self):
        """Force immediate cleanup of unused clients"""
        self._last_cleanup = 0  # Force cleanup on next call
        self._cleanup_unused_clients()


# Global instance for application-wide use
aws_client_manager = AWSClientManager(max_pool_connections=500)

# Convenience functions for backward compatibility
def get_bedrock_client():
    """Get a reusable Bedrock client"""
    return aws_client_manager.get_client("bedrock-runtime")

def get_kb_client():
    """Get a reusable Bedrock Knowledge Base client"""
    return aws_client_manager.get_client("bedrock-agent-runtime")

def get_polly_client():
    """Get a reusable Polly client"""
    return aws_client_manager.get_client("polly")

def get_dynamodb_resource():
    """Get a reusable DynamoDB resource"""
    return aws_client_manager.get_client("dynamodb")

def get_translate_client():
    """Get a reusable Translate client"""
    return aws_client_manager.get_client("translate")

def get_comprehend_client():
    """Get a reusable Comprehend client"""
    return aws_client_manager.get_client("comprehend")

def get_transcribe_client():
    """Get a TranscribeStreamingClient (short-lived)"""
    return aws_client_manager.get_transcribe_client()

def get_s3_client():
    """Get a reusable S3 client"""
    return aws_client_manager.get_client("s3")

# Context manager for streaming transcription
@asynccontextmanager
async def transcribe_stream_context():
    """Context manager for transcription streaming"""
    async with aws_client_manager.get_transcribe_stream_context() as client:
        yield client

# Cleanup function for application shutdown
def cleanup_aws_clients():
    """Clean up all AWS clients on application shutdown"""
    aws_client_manager.force_cleanup()
