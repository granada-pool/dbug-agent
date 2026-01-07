"""
Failure injection module for debug agent.
Allows intentional failures at different stages of the agent lifecycle.
"""
from enum import Enum
from typing import Optional, Dict, Any
from logging_config import get_logger

logger = get_logger(__name__)

class FailurePoint(str, Enum):
    """Points where failures can be injected"""
    START_JOB = "fail_on_start_job"
    PAYMENT_CREATION = "fail_on_payment_creation"
    PAYMENT_MONITORING = "fail_on_payment_monitoring"
    TASK_EXECUTION = "fail_on_task_execution"
    PAYMENT_COMPLETION = "fail_on_payment_completion"
    STATUS_CHECK = "fail_on_status_check"
    AVAILABILITY = "fail_on_availability"
    INPUT_SCHEMA = "fail_on_input_schema"

class FailureType(str, Enum):
    """Types of failures to simulate"""
    HTTP_400 = "http_400"  # Bad Request
    HTTP_404 = "http_404"  # Not Found
    HTTP_500 = "http_500"  # Internal Server Error
    HTTP_503 = "http_503"  # Service Unavailable
    TIMEOUT = "timeout"
    EXCEPTION = "exception"
    INVALID_RESPONSE = "invalid_response"

class FailureInjector:
    """
    Handles intentional failure injection for testing error handling.
    Commands are passed via input_data in the format:
    {
        "command": "test_scenario",
        "failure_point": "fail_on_start_job",
        "failure_type": "http_500"
    }
    """
    
    def __init__(self):
        self.active_failures: Dict[str, FailureType] = {}
        logger.info("FailureInjector initialized")
    
    def parse_command(self, input_data: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        Parse debug command from input_data.
        Returns failure config if command is present, None otherwise.
        """
        if not isinstance(input_data, dict):
            return None
            
        # Check for explicit debug command
        if "command" in input_data:
            command = input_data.get("command", "")
            failure_point = input_data.get("failure_point", "")
            failure_type = input_data.get("failure_type", "exception")
            
            if command.startswith("debug_") or failure_point:
                return {
                    "command": command,
                    "failure_point": failure_point,
                    "failure_type": failure_type
                }
        
        # Check for legacy format: text field with command
        text = input_data.get("text", "")
        if isinstance(text, str) and text.startswith("DEBUG:"):
            parts = text.split(":")
            if len(parts) >= 2:
                failure_point = parts[1].strip()
                failure_type = parts[2].strip() if len(parts) > 2 else "exception"
                return {
                    "command": "debug",
                    "failure_point": failure_point,
                    "failure_type": failure_type
                }
        
        return None
    
    def should_fail(self, failure_point: FailurePoint, input_data: Dict[str, Any]) -> Optional[FailureType]:
        """
        Check if a failure should be injected at the given point.
        Returns FailureType if failure should occur, None otherwise.
        """
        command_config = self.parse_command(input_data)
        
        if not command_config:
            return None
        
        requested_point = command_config.get("failure_point", "")
        requested_type = command_config.get("failure_type", "exception")
        
        # Check if this is the point where we should fail
        if failure_point.value == requested_point or requested_point in failure_point.value:
            try:
                return FailureType(requested_type)
            except ValueError:
                logger.warning(f"Unknown failure type: {requested_type}, defaulting to exception")
                return FailureType.EXCEPTION
        
        return None
    
    def inject_failure(self, failure_point: FailurePoint, failure_type: FailureType, message: str = ""):
        """
        Inject a failure based on the failure type.
        Raises appropriate exception or returns error response.
        """
        from fastapi import HTTPException
        
        error_message = message or f"Debug agent: Intentional failure at {failure_point.value}"
        logger.warning(f"Injecting failure: {failure_point.value} - {failure_type.value}")
        
        if failure_type == FailureType.HTTP_400:
            raise HTTPException(status_code=400, detail=error_message)
        elif failure_type == FailureType.HTTP_404:
            raise HTTPException(status_code=404, detail=error_message)
        elif failure_type == FailureType.HTTP_500:
            raise HTTPException(status_code=500, detail=error_message)
        elif failure_type == FailureType.HTTP_503:
            raise HTTPException(status_code=503, detail=error_message)
        elif failure_type == FailureType.TIMEOUT:
            import asyncio
            raise asyncio.TimeoutError(error_message)
        elif failure_type == FailureType.INVALID_RESPONSE:
            # Return invalid response structure
            return {"invalid": "response", "error": error_message}
        else:  # EXCEPTION
            raise Exception(error_message)

