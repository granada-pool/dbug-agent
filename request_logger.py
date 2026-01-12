"""
Request logging system for debugging agent issues.
Stores request/response/error details in a JSON file for retrieval via API.
"""
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
from logging_config import get_logger

logger = get_logger(__name__)

class RequestLogger:
    """Logs requests, responses, and errors to a JSON file for debugging"""
    
    def __init__(self, log_file: str = "request_logs.json", max_entries: int = 1000):
        """
        Initialize the request logger.
        
        Args:
            log_file: Path to the JSON log file
            max_entries: Maximum number of log entries to keep (FIFO)
        """
        self.log_file = Path(log_file)
        self.max_entries = max_entries
        self.ensure_log_file()
    
    def ensure_log_file(self):
        """Ensure the log file exists and is valid JSON"""
        if not self.log_file.exists():
            with open(self.log_file, 'w') as f:
                json.dump([], f)
        else:
            # Validate it's valid JSON
            try:
                with open(self.log_file, 'r') as f:
                    json.load(f)
            except json.JSONDecodeError:
                # Reset if corrupted
                with open(self.log_file, 'w') as f:
                    json.dump([], f)
    
    def _load_logs(self) -> List[Dict[str, Any]]:
        """Load logs from file"""
        try:
            with open(self.log_file, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
    
    def _save_logs(self, logs: List[Dict[str, Any]]):
        """Save logs to file"""
        # Trim to max_entries (keep most recent)
        if len(logs) > self.max_entries:
            logs = logs[-self.max_entries:]
        
        with open(self.log_file, 'w') as f:
            json.dump(logs, f, indent=2, default=str)
    
    def log_request(
        self,
        endpoint: str,
        method: str,
        request_data: Optional[Dict[str, Any]] = None,
        request_body: Optional[str] = None,
        validation_errors: Optional[List[Dict[str, Any]]] = None,
        response_data: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[float] = None
    ):
        """
        Log a request with all relevant details.
        
        Args:
            endpoint: The API endpoint path
            method: HTTP method (GET, POST, etc.)
            request_data: Parsed request data (if available)
            request_body: Raw request body as string
            validation_errors: List of validation errors (if any)
            response_data: Response data (if successful)
            error: Error message (if failed)
            status_code: HTTP status code
            duration_ms: Request duration in milliseconds
        """
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "endpoint": endpoint,
            "method": method,
            "request_data": request_data,
            "request_body": request_body,
            "validation_errors": validation_errors,
            "response_data": response_data,
            "error": error,
            "status_code": status_code,
            "duration_ms": duration_ms
        }
        
        logs = self._load_logs()
        logs.append(log_entry)
        self._save_logs(logs)
        
        logger.info(f"Logged request to {endpoint} - Status: {status_code}")
    
    def get_logs(
        self,
        endpoint: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        status_code: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Retrieve logs with filtering and pagination.
        
        Args:
            endpoint: Filter by endpoint (optional)
            limit: Maximum number of logs to return
            offset: Number of logs to skip
            status_code: Filter by status code (optional)
            
        Returns:
            Dictionary with logs and metadata
        """
        logs = self._load_logs()
        
        # Filter logs
        if endpoint:
            logs = [log for log in logs if log.get("endpoint") == endpoint]
        if status_code is not None:
            logs = [log for log in logs if log.get("status_code") == status_code]
        
        # Reverse to get most recent first
        logs.reverse()
        
        # Paginate
        total = len(logs)
        paginated_logs = logs[offset:offset + limit]
        
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "logs": paginated_logs
        }
    
    def get_latest_error(self) -> Optional[Dict[str, Any]]:
        """Get the most recent error log"""
        logs = self._load_logs()
        error_logs = [log for log in logs if log.get("error") or log.get("validation_errors")]
        if error_logs:
            return error_logs[-1]
        return None
    
    def clear_logs(self):
        """Clear all logs"""
        with open(self.log_file, 'w') as f:
            json.dump([], f)
        logger.info("Cleared all request logs")

# Global instance
request_logger = RequestLogger()
