import os
import uvicorn
import uuid
import time
import json
from typing import Optional, Any, Union
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError, field_validator, model_validator
from masumi.config import Config
from masumi.payment import Payment, Amount
from agentic_service import get_agentic_service
from failure_injector import FailureInjector, FailurePoint, FailureType
from hitl_manager import HITLManager, ApprovalStatus
from logging_config import setup_logging
from request_logger import request_logger
import cuid2
import secrets
import asyncio

#region congif
# Configure logging
logger = setup_logging()

# Load environment variables
load_dotenv(override=True)

# Retrieve API Keys and URLs
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "")
PAYMENT_API_KEY = os.getenv("PAYMENT_API_KEY", "")
NETWORK = os.getenv("NETWORK", "preview")

logger.info("Starting application with configuration:")
logger.info(f"PAYMENT_SERVICE_URL: {PAYMENT_SERVICE_URL}")

# validate critical environment variables at startup
def validate_url(url: str, name: str) -> str:
    """Validate that a URL is properly formatted"""
    if not url:
        return f"{name} is not set"
    
    # check if it starts with http:// or https://
    if not url.startswith(('http://', 'https://')):
        return f"{name} must start with 'https://' or 'http://' (got: '{url}')"
    
    # parse URL to check if it's valid
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return f"{name} is not a valid URL format (got: '{url}')"
    except Exception:
        return f"{name} is not a valid URL format (got: '{url}')"
    
    return ""  # no error

def validate_environment():
    """Validate that all required environment variables are set"""
    errors = []
    
    agent_id = os.getenv("AGENT_IDENTIFIER", "").strip()
    if not agent_id:
        errors.append("AGENT_IDENTIFIER is not set")
    elif agent_id == "REPLACE":
        errors.append("AGENT_IDENTIFIER is set to placeholder 'REPLACE' - please set a real agent identifier")
    
    # validate payment service URL format
    url_error = validate_url(PAYMENT_SERVICE_URL, "PAYMENT_SERVICE_URL")
    if url_error:
        errors.append(url_error)
    
    if not PAYMENT_API_KEY:
        errors.append("PAYMENT_API_KEY is not set")
    
    if not NETWORK:
        errors.append("NETWORK is not set")
    
    # Validate OpenAI API key (required for CrewAI)
    if not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is not set (required for CrewAI)")
    
    if errors:
        logger.error("Critical environment variable validation failed:")
        for error in errors:
            logger.error(f"  - {error}")
        logger.error("Please fix these configuration issues before starting the server")
        return False
    
    logger.info("Environment validation passed")
    return True

# run validation but don't fail startup (for debugging)
validation_passed = validate_environment()
if not validation_passed:
    logger.warning("Starting server despite configuration errors - some endpoints may not work properly")

# Initialize FastAPI
app = FastAPI(
    title="Debug Agent API - Masumi API Standard",
    description="Debug agent for testing Sokosumi platform error handling. Supports intentional failure injection at various stages. Masumi MIP-003 compliant.",
    version="1.0.0"
)

# Add CORS middleware to allow validation requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for validation
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add exception handler for Pydantic validation errors (422)
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle Pydantic validation errors with detailed logging"""
    start_time = time.time()
    
    logger.error("=" * 80)
    logger.error("VALIDATION_ERROR: Pydantic validation failed")
    logger.error(f"VALIDATION_ERROR: Request URL: {request.url}")
    logger.error(f"VALIDATION_ERROR: Request method: {request.method}")
    
    # Try to log the request body
    body_str = None
    try:
        body = await request.body()
        body_str = body.decode('utf-8') if isinstance(body, bytes) else str(body)
        logger.error(f"VALIDATION_ERROR: Request body (raw): {body_str[:500]}")
    except Exception as e:
        logger.error(f"VALIDATION_ERROR: Could not read request body: {str(e)}")
    
    # Log validation errors
    errors = exc.errors()
    logger.error(f"VALIDATION_ERROR: Number of validation errors: {len(errors)}")
    for idx, error in enumerate(errors):
        logger.error(f"VALIDATION_ERROR: Error [{idx}]: {error}")
    
    # Log to request logger
    duration_ms = (time.time() - start_time) * 1000
    request_logger.log_request(
        endpoint=str(request.url.path),
        method=request.method,
        request_body=body_str,
        validation_errors=list(errors),  # Convert Sequence to List
        error=f"Validation failed: {len(errors)} error(s)",
        status_code=422,
        duration_ms=duration_ms
    )
    
    # Return detailed error response
    return JSONResponse(
        status_code=422,
        content={
            "detail": errors,
            "body": body_str[:500] if body_str else None,
            "message": f"Validation failed: {len(errors)} error(s). Check /logs endpoint for details."
        }
    )

# ─────────────────────────────────────────────────────────────────────────────
#region Temporary in-memory job store 
# DO NOT USE IN PRODUCTION)
# ─────────────────────────────────────────────────────────────────────────────
jobs = {}
payment_instances = {}
# Track which jobs have had their payment callback triggered to avoid duplicate execution
payment_callbacks_triggered = set()

# track server start time for uptime calculation
server_start_time = time.time()

# ─────────────────────────────────────────────────────────────────────────────
#region Initialize Masumi Payment Config
# ─────────────────────────────────────────────────────────────────────────────
# config will be created in start_job to allow proper validation

# ─────────────────────────────────────────────────────────────────────────────
#region Initialize Failure Injector for Debug Agent
# ─────────────────────────────────────────────────────────────────────────────
failure_injector = FailureInjector()

# ─────────────────────────────────────────────────────────────────────────────
#region Initialize HITL Manager
# ─────────────────────────────────────────────────────────────────────────────
hitl_manager = HITLManager()

# ─────────────────────────────────────────────────────────────────────────────
#region Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────
class InputDataItem(BaseModel):
    key: str
    value: Any  # Accept any type to avoid validation errors
    
    @field_validator('value', mode='before')
    @classmethod
    def validate_value(cls, v):
        # Accept any value type - don't validate, just pass through
        return v

class StartJobRequest(BaseModel):
    input_data: list[InputDataItem]  # Must be a list of InputDataItem
    
    @model_validator(mode='before')
    @classmethod
    def normalize_input_data(cls, data):
        """Normalize input_data from different formats to our internal format"""
        if isinstance(data, dict) and 'input_data' in data:
            input_data_value = data['input_data']
            # If it's a dict (Sokosumi format), convert to list format
            if isinstance(input_data_value, dict):
                # Convert dict format to list of dicts (Pydantic will validate to InputDataItem)
                result = []
                for key, value in input_data_value.items():
                    result.append({"key": key, "value": value})
                data['input_data'] = result
            # If it's already a list, keep it as-is
            elif isinstance(input_data_value, list):
                pass  # Already in correct format
            else:
                # Fallback: wrap in list
                data['input_data'] = [{"key": "input_string", "value": str(input_data_value)}]
        return data
    
    class Config:
        json_schema_extra = {
            "example": {
                "input_data": [
                    {"key": "input_string", "value": "Hello World"}
                ]
            }
        }

class ProvideInputRequest(BaseModel):
    job_id: str

class ApprovalRequest(BaseModel):
    job_id: str
    action: Optional[str] = "task_execution"
    approve: bool = True
    reason: Optional[str] = None
    operator: Optional[str] = None

# ─────────────────────────────────────────────────────────────────────────────
#region Task Execution THIS IS THE MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
async def execute_agentic_task(input_data: dict) -> object:
    """ Execute task """
    logger.info(f"starting task with input: {input_data}")
    service = get_agentic_service(logger=logger)
    result = await service.execute_task(input_data)
    logger.info("task completed successfully")
    return result

# ─────────────────────────────────────────────────────────────────────────────
#region 1) Start Job (MIP-003: /start_job)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/start_job")
async def start_job(request: Request, data: StartJobRequest):
    """ Initiates a job and creates a payment request """
    start_time = time.time()
    request_body = None
    
    # Try to capture request body for logging
    try:
        body = await request.body()
        request_body = body.decode('utf-8') if isinstance(body, bytes) else str(body)
    except:
        pass
    
    logger.info("=" * 80)
    logger.info("START_JOB: Received request")
    logger.info(f"START_JOB: Request data type: {type(data)}")
    logger.info(f"START_JOB: Input data items count: {len(data.input_data) if data.input_data else 0}")
    
    # Log each input item with its type
    for idx, item in enumerate(data.input_data):
        logger.info(f"START_JOB: Input item [{idx}]: key='{item.key}', value={item.value}, value_type={type(item.value).__name__}")
    
    try:
        # convert input_data array to dict for internal processing (early conversion for failure injection)
        # Handle different value types from Sokosumi (strings, booleans, numbers, arrays)
        logger.info("START_JOB: Converting input_data array to dictionary")
        input_data_dict = {}
        for item in data.input_data:
            # Handle array values (e.g., failure_point: [0] means first option index)
            value = item.value
            if isinstance(value, list):
                # If it's a list with one element, extract it (Sokosumi sends option indices as [0])
                if len(value) == 1:
                    idx = value[0]
                    # If it's an index (number), map it to the actual option value
                    if isinstance(idx, int):
                        # Map index to option value based on the field
                        if item.key == "failure_point":
                            # Index 0 = "none", index 1 = "fail_on_start_job", etc.
                            failure_points = ["none", "fail_on_start_job", "fail_on_payment_creation", 
                                            "fail_on_payment_monitoring", "fail_on_task_execution",
                                            "fail_on_payment_completion", "fail_on_status_check",
                                            "fail_on_availability", "fail_on_input_schema",
                                            "fail_on_hitl_approval", "fail_on_hitl_timeout", "fail_on_hitl_rejection"]
                            if 0 <= idx < len(failure_points):
                                value = failure_points[idx]
                                logger.info(f"START_JOB: Mapped failure_point index {idx} to '{value}'")
                            else:
                                value = "none"  # Default to none if invalid index
                        elif item.key == "failure_type":
                            # Index 0 = "none", index 1 = "http_400", etc.
                            failure_types = ["none", "http_400", "http_404", "http_500", "http_503",
                                           "timeout", "exception", "invalid_response"]
                            if 0 <= idx < len(failure_types):
                                value = failure_types[idx]
                                logger.info(f"START_JOB: Mapped failure_type index {idx} to '{value}'")
                            else:
                                value = "none"  # Default to none if invalid index
                        else:
                            value = idx  # For other fields, just use the index value
                    else:
                        value = idx
                elif len(value) == 0:
                    value = None
                # If multiple values, take the first one
                else:
                    value = value[0]
            
            # Preserve original types - don't convert everything to string
            # This allows proper handling of booleans and numbers later
            if value is None:
                input_data_dict[item.key] = ""
                logger.debug(f"START_JOB: Set {item.key} to empty string (was None)")
            else:
                input_data_dict[item.key] = value
                logger.debug(f"START_JOB: Set {item.key} = {value} (type: {type(value).__name__})")
        
        logger.info(f"START_JOB: Converted input_data_dict: {input_data_dict}")
        
        # Check for failure injection at start_job (before job creation)
        logger.info("START_JOB: Checking for failure injection at START_JOB point")
        failure_type = failure_injector.should_fail(
            FailurePoint.START_JOB, 
            input_data_dict
        )
        if failure_type:
            logger.warning(f"START_JOB: Failure injection triggered: {failure_type}")
            failure_injector.inject_failure(
                FailurePoint.START_JOB, 
                failure_type,
                "Debug: Intentional failure on start_job"
            )
        else:
            logger.info("START_JOB: No failure injection at START_JOB point")
        
        job_id = str(uuid.uuid4())
        logger.info(f"START_JOB: Generated job_id: {job_id}")
        
        # validate required environment variables
        logger.info("START_JOB: Validating environment variables")
        agent_identifier = os.getenv("AGENT_IDENTIFIER", "").strip()
        logger.info(f"START_JOB: AGENT_IDENTIFIER = '{agent_identifier[:20]}...' (length: {len(agent_identifier)})" if len(agent_identifier) > 20 else f"START_JOB: AGENT_IDENTIFIER = '{agent_identifier}'")
        if not agent_identifier or agent_identifier == "REPLACE":
            logger.error("AGENT_IDENTIFIER environment variable is missing or set to placeholder 'REPLACE'")
            raise HTTPException(
                status_code=500,
                detail="Server configuration error: AGENT_IDENTIFIER not properly configured. Please contact administrator."
            )
        
        # validate payment service URL format
        logger.info(f"START_JOB: PAYMENT_SERVICE_URL = '{PAYMENT_SERVICE_URL[:50]}...' (length: {len(PAYMENT_SERVICE_URL)})" if len(PAYMENT_SERVICE_URL) > 50 else f"START_JOB: PAYMENT_SERVICE_URL = '{PAYMENT_SERVICE_URL}'")
        url_error = validate_url(PAYMENT_SERVICE_URL, "PAYMENT_SERVICE_URL")
        if url_error:
            logger.error(f"START_JOB: PAYMENT_SERVICE_URL validation failed: {url_error}")
            raise HTTPException(
                status_code=500,
                detail=f"Server configuration error: {url_error}. Please contact administrator."
            )
        logger.info("START_JOB: PAYMENT_SERVICE_URL validation passed")
            
        logger.info(f"START_JOB: PAYMENT_API_KEY present: {bool(PAYMENT_API_KEY)}")
        if not PAYMENT_API_KEY:
            logger.error("START_JOB: PAYMENT_API_KEY environment variable is missing")
            raise HTTPException(
                status_code=500,
                detail="Server configuration error: PAYMENT_API_KEY not configured. Please contact administrator."
            )
        
        # generate identifier_from_purchaser internally - must be a valid hex string
        # Payment service requires max 26 characters, so generate 13 bytes (26 hex characters)
        identifier_from_purchaser = secrets.token_hex(13)
        logger.info(f"START_JOB: Generated identifier_from_purchaser: {identifier_from_purchaser}")
        
        # validate required input
        logger.info(f"START_JOB: Checking for required 'input_string' field in input_data_dict")
        logger.info(f"START_JOB: Available keys in input_data_dict: {list(input_data_dict.keys())}")
        if "input_string" not in input_data_dict:
            logger.error(f"START_JOB: Required field 'input_string' missing from input_data. Available keys: {list(input_data_dict.keys())}")
            raise HTTPException(
                status_code=400,
                detail="Bad Request: 'input_string' is required in input_data array"
            )
        
        # Log the input text (truncate if too long)
        input_text = input_data_dict.get("input_string", "")
        truncated_input = input_text[:100] + "..." if len(input_text) > 100 else input_text
        logger.info(f"START_JOB: Input text received: '{truncated_input}' (full length: {len(input_text)})")
        logger.info(f"START_JOB: Starting job {job_id} with agent {agent_identifier}")

        # Define payment amounts - set to 1
        payment_amount = int(os.getenv("PAYMENT_AMOUNT", "100000"))  # Default to 1 ADA
        payment_unit = os.getenv("PAYMENT_UNIT", "lovelace") # Default lovelace

        logger.info(f"Using payment amount: {payment_amount} {payment_unit}")
        
        # Prepare amounts in the format expected by Masumi Payment service
        # Fixed price format: list of Amount objects with unit and amount
        amounts = [
            Amount(
                unit=payment_unit,
                amount=payment_amount
            )
        ]
        
        # Check for failure on payment creation
        logger.info("START_JOB: Checking for failure injection at PAYMENT_CREATION point")
        failure_type = failure_injector.should_fail(
            FailurePoint.PAYMENT_CREATION,
            input_data_dict
        )
        if failure_type:
            logger.warning(f"START_JOB: Failure injection triggered at PAYMENT_CREATION: {failure_type}")
            failure_injector.inject_failure(
                FailurePoint.PAYMENT_CREATION,
                failure_type,
                "Debug: Intentional failure on payment creation"
            )
        else:
            logger.info("START_JOB: No failure injection at PAYMENT_CREATION point")
        
        # create config after validation
        logger.info("START_JOB: Creating Masumi Config object")
        config = Config(
            payment_service_url=PAYMENT_SERVICE_URL,
            payment_api_key=PAYMENT_API_KEY
        )
        
        # Create a payment request using Masumi
        logger.info("START_JOB: Creating Payment object")
        logger.info(f"START_JOB: Payment params - agent_identifier: {agent_identifier}, network: {NETWORK}, amounts: {amounts}")
        payment = Payment(
            agent_identifier=agent_identifier,
            amounts=amounts,
            config=config,
            identifier_from_purchaser=identifier_from_purchaser,
            input_data=input_data_dict,
            network=NETWORK
        )
        
        logger.info("START_JOB: Calling payment.create_payment_request()...")
        try:
            payment_request = await payment.create_payment_request()
            logger.info(f"START_JOB: Payment request created successfully. Response keys: {list(payment_request.keys()) if isinstance(payment_request, dict) else 'not a dict'}")
            if isinstance(payment_request, dict) and "data" in payment_request:
                logger.info(f"START_JOB: Payment request data keys: {list(payment_request['data'].keys())}")
                # Log all data fields for debugging
                for key, value in payment_request["data"].items():
                    logger.debug(f"START_JOB: Payment data[{key}] = {value} (type: {type(value).__name__})")
            payment_id = payment_request["data"]["blockchainIdentifier"]
            payment.payment_ids.add(payment_id)
            logger.info(f"START_JOB: Created payment request with ID: {payment_id}")
        except Exception as e:
            logger.error(f"START_JOB: Error creating payment request: {str(e)}", exc_info=True)
            raise

        # Store job info (Awaiting payment)
        jobs[job_id] = {
            "status": "awaiting_payment",
            "payment_status": "pending",
            "payment_id": payment_id,
            "input_data": input_data_dict,
            "result": None,
            "identifier_from_purchaser": identifier_from_purchaser
        }

        async def payment_callback(payment_id: str):
            await handle_payment_status(job_id, payment_id)

        async def monitor_payment_with_error_handling():
            """Wrapper to monitor payment with proper error handling"""
            try:
                logger.info(f"PAYMENT_MONITOR: Starting payment monitoring for job {job_id}")
                logger.info(f"PAYMENT_MONITOR: Payment ID: {payment_id}")
                
                # Add a wrapper callback to log when it's called
                async def logged_callback(payment_id_callback: str):
                    callback_key = f"{job_id}:{payment_id_callback}"
                    if callback_key in payment_callbacks_triggered:
                        logger.warning(f"PAYMENT_MONITOR: Callback already triggered for {callback_key}, skipping duplicate")
                        return
                    logger.info(f"PAYMENT_MONITOR: Callback triggered for payment {payment_id_callback} (job {job_id})")
                    payment_callbacks_triggered.add(callback_key)
                    await payment_callback(payment_id_callback)
                
                await payment.start_status_monitoring(logged_callback)
                logger.info(f"PAYMENT_MONITOR: Payment monitoring completed for job {job_id}")
            except Exception as e:
                logger.error(f"PAYMENT_MONITOR: Error in payment monitoring for job {job_id}: {str(e)}", exc_info=True)
                # Update job status to failed if monitoring fails
                if job_id in jobs:
                    jobs[job_id]["status"] = "failed"
                    jobs[job_id]["error"] = f"Payment monitoring error: {str(e)}"
                # Clean up payment instance
                if job_id in payment_instances:
                    try:
                        payment_instances[job_id].stop_status_monitoring()
                    except:
                        pass
                    del payment_instances[job_id]

        # Start monitoring the payment status in the background
        # This must run in background so /start_job can return immediately
        payment_instances[job_id] = payment
        logger.info(f"START_JOB: Starting payment status monitoring for job {job_id} in background")
        # Use create_task to run monitoring in background without blocking the response
        monitoring_task = asyncio.create_task(monitor_payment_with_error_handling())
        logger.info(f"START_JOB: Payment monitoring task created for job {job_id}, /start_job will return now")

        # Get SELLER_VKEY from environment
        seller_vkey = os.getenv("SELLER_VKEY", "")
        logger.info(f"START_JOB: SELLER_VKEY present: {bool(seller_vkey)}")
        if not seller_vkey:
            logger.error("START_JOB: SELLER_VKEY environment variable is missing")
            raise HTTPException(
                status_code=500,
                detail="Server configuration error: SELLER_VKEY not configured. Please contact administrator."
            )
        
        # Return the response in the format expected by the /purchase endpoint
        # Include both the original fields and the extended fields
        logger.info("START_JOB: Preparing response data")
        
        # Parse timestamps from payment request
        payment_data = payment_request["data"]
        submit_result_time = payment_data.get("submitResultTime")
        unlock_time = payment_data.get("unlockTime")
        external_dispute_unlock_time = payment_data.get("externalDisputeUnlockTime")
        pay_by_time = payment_data.get("payByTime")  # Check if payment service provides this
        input_hash = payment_data.get("inputHash")
        
        # Convert ISO timestamp strings to Unix timestamps (seconds) for MIP-003 compliance
        # MIP-003 requires all timestamps as int (Unix timestamp in seconds)
        def parse_timestamp_to_unix(ts):
            """Convert ISO timestamp string to Unix timestamp (seconds)"""
            try:
                if isinstance(ts, str):
                    # Parse ISO format timestamp and convert to Unix timestamp in seconds
                    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    return int(dt.timestamp())
                elif isinstance(ts, (int, float)):
                    # If already a number, check if it's milliseconds or seconds
                    if ts > 1e10:  # Likely milliseconds (timestamp > year 2001 in seconds)
                        return int(ts / 1000)
                    return int(ts)  # Already seconds
                return int(time.time())
            except Exception as e:
                logger.warning(f"START_JOB: Could not parse timestamp {ts}, using current time: {e}")
                return int(time.time())
        
        # Parse timestamps from payment service response
        submit_result_time_unix = parse_timestamp_to_unix(submit_result_time) if submit_result_time else int(time.time())
        unlock_time_unix = parse_timestamp_to_unix(unlock_time) if unlock_time else int(time.time())
        external_dispute_unlock_time_unix = parse_timestamp_to_unix(external_dispute_unlock_time) if external_dispute_unlock_time else int(time.time())
        
        # Calculate payByTime: must be at least 5 minutes BEFORE submitResultTime
        # MIP-003 requirement: payByTime < submitResultTime (minimum 5 minutes difference)
        # Sokosumi requirement: payByTime must be at least 1000 seconds (16.67 minutes) before submitResultTime
        MIN_PAYMENT_DEADLINE_GAP_SECONDS = 300  # Minimum 5 minutes in seconds (MIP-003 requirement)
        REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS = 1000  # Required: 1000 seconds (~16.67 minutes) before submitResultTime
        DEFAULT_PAYMENT_DEADLINE_GAP_SECONDS = 43200  # Default: 12 hours before submitResultTime (when submitResultTime is far in future)
        
        current_time_unix = int(time.time())
        
        if pay_by_time:
            pay_by_time_unix = parse_timestamp_to_unix(pay_by_time)
            # Validate that payByTime is before submitResultTime and has required gap
            gap_seconds = submit_result_time_unix - pay_by_time_unix
            
            if pay_by_time_unix >= submit_result_time_unix:
                logger.warning(f"START_JOB: payByTime ({pay_by_time_unix}) >= submitResultTime ({submit_result_time_unix}), adjusting...")
                # Set payByTime to be required gap before submitResultTime
                time_until_submit_result = submit_result_time_unix - current_time_unix
                if time_until_submit_result > REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS:
                    pay_by_time_unix = submit_result_time_unix - REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS
                else:
                    # If submitResultTime is soon, use minimum gap
                    pay_by_time_unix = submit_result_time_unix - MIN_PAYMENT_DEADLINE_GAP_SECONDS
            elif gap_seconds < REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS:
                # payByTime is valid but gap is too small (less than 1000 seconds)
                logger.warning(f"START_JOB: payByTime gap ({gap_seconds}s) is too small, adjusting to {REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS}s before submitResultTime")
                time_until_submit_result = submit_result_time_unix - current_time_unix
                if time_until_submit_result > REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS:
                    pay_by_time_unix = submit_result_time_unix - REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS
                else:
                    # If submitResultTime is soon, ensure at least minimum gap
                    pay_by_time_unix = submit_result_time_unix - MIN_PAYMENT_DEADLINE_GAP_SECONDS
                    logger.warning(f"START_JOB: submitResultTime is soon, using minimum gap of {MIN_PAYMENT_DEADLINE_GAP_SECONDS}s")
            # Also ensure payByTime is not in the past
            if pay_by_time_unix < current_time_unix:
                logger.warning(f"START_JOB: payByTime ({pay_by_time_unix}) is in the past, adjusting to current time + buffer...")
                pay_by_time_unix = current_time_unix + 60  # At least 1 minute from now
        else:
            # Calculate payByTime: should be significantly before submitResultTime
            # Use required gap (1000 seconds) when possible, or minimum gap if submitResultTime is soon
            time_until_submit_result = submit_result_time_unix - current_time_unix
            
            if time_until_submit_result > REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS:
                # If submitResultTime is far enough in the future, set payByTime to required gap (1000 seconds) before it
                pay_by_time_unix = submit_result_time_unix - REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS
            else:
                # If submitResultTime is soon (less than required gap away), ensure payByTime is:
                # - At least 1 minute from now (not in the past)
                # - At least 5 minutes before submitResultTime (MIP-003 minimum)
                pay_by_time_unix = max(
                    current_time_unix + 60,  # At least 1 minute from now
                    submit_result_time_unix - MIN_PAYMENT_DEADLINE_GAP_SECONDS  # At least 5 minutes before submitResultTime
                )
            
            gap_seconds = submit_result_time_unix - pay_by_time_unix
            gap_minutes = gap_seconds / 60
            logger.info(f"START_JOB: payByTime not in payment response, calculated as {gap_seconds}s ({gap_minutes:.1f} minutes) before submitResultTime")
        
        # Ensure proper ordering: payByTime < submitResultTime < unlockTime < externalDisputeUnlockTime
        # Validate and adjust if necessary
        final_gap = submit_result_time_unix - pay_by_time_unix
        if pay_by_time_unix >= submit_result_time_unix:
            logger.error(f"START_JOB: Invalid timestamp order - payByTime ({pay_by_time_unix}) >= submitResultTime ({submit_result_time_unix})")
            pay_by_time_unix = submit_result_time_unix - REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS
        elif final_gap < REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS:
            logger.warning(f"START_JOB: Final gap ({final_gap}s) is less than required ({REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS}s), adjusting payByTime...")
            pay_by_time_unix = submit_result_time_unix - REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS
        
        if submit_result_time_unix >= unlock_time_unix:
            logger.warning(f"START_JOB: submitResultTime ({submit_result_time_unix}) >= unlockTime ({unlock_time_unix}), adjusting unlockTime...")
            unlock_time_unix = submit_result_time_unix + 600  # 10 minutes after submitResultTime
        
        if unlock_time_unix >= external_dispute_unlock_time_unix:
            logger.warning(f"START_JOB: unlockTime ({unlock_time_unix}) >= externalDisputeUnlockTime ({external_dispute_unlock_time_unix}), adjusting...")
            external_dispute_unlock_time_unix = unlock_time_unix + 600  # 10 minutes after unlockTime
        
        # Log final timestamp values for debugging
        logger.info(f"START_JOB: Final timestamps (Unix seconds):")
        logger.info(f"START_JOB:   payByTime: {pay_by_time_unix} ({datetime.fromtimestamp(pay_by_time_unix).isoformat()})")
        logger.info(f"START_JOB:   submitResultTime: {submit_result_time_unix} ({datetime.fromtimestamp(submit_result_time_unix).isoformat()})")
        logger.info(f"START_JOB:   unlockTime: {unlock_time_unix} ({datetime.fromtimestamp(unlock_time_unix).isoformat()})")
        logger.info(f"START_JOB:   externalDisputeUnlockTime: {external_dispute_unlock_time_unix} ({datetime.fromtimestamp(external_dispute_unlock_time_unix).isoformat()})")
        
        # Verify ordering
        assert pay_by_time_unix < submit_result_time_unix, f"payByTime must be before submitResultTime"
        assert submit_result_time_unix < unlock_time_unix, f"submitResultTime must be before unlockTime"
        assert unlock_time_unix < external_dispute_unlock_time_unix, f"unlockTime must be before externalDisputeUnlockTime"
        final_gap_seconds = submit_result_time_unix - pay_by_time_unix
        assert final_gap_seconds >= REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS, f"payByTime must be at least {REQUIRED_PAYMENT_DEADLINE_GAP_SECONDS}s before submitResultTime (gap: {final_gap_seconds}s)"
        
        # MIP-003 compliant response format
        response_data = {
            # MIP-003 required fields
            "id": job_id,  # MIP-003 uses "id" not "job_id"
            "blockchainIdentifier": payment_id,
            "payByTime": 2017171717,  # int (Unix timestamp in seconds)
            "submitResultTime": submit_result_time_unix,  # int (Unix timestamp in seconds)
            "unlockTime": unlock_time_unix,  # int (Unix timestamp in seconds)
            "externalDisputeUnlockTime": external_dispute_unlock_time_unix,  # int (Unix timestamp in seconds)
            "agentIdentifier": agent_identifier,
            "sellerVKey": seller_vkey,
            "identifierFromPurchaser": identifier_from_purchaser,
            "input_hash": input_hash,
            
            # Additional fields for backward compatibility and Sokosumi
            "job_id": job_id,
            "payment_id": payment_id,
            "network": NETWORK,
            "paymentType": "Web3CardanoV1",
            "inputHash": input_hash  # camelCase version for compatibility
        }
        logger.info(f"START_JOB: Response data prepared with keys: {list(response_data.keys())}")
        logger.info("START_JOB: Request completed successfully")
        logger.info("=" * 80)
        
        # Log successful request
        duration_ms = (time.time() - start_time) * 1000
        request_logger.log_request(
            endpoint="/start_job",
            method="POST",
            request_data={"input_data": [{"key": item.key, "value": item.value} for item in data.input_data]},
            request_body=request_body,
            response_data=response_data,
            status_code=200,
            duration_ms=duration_ms
        )
        
        return response_data
    except HTTPException as e:
        # re-raise HTTP exceptions (our custom errors)
        logger.error(f"START_JOB: HTTPException raised - status_code: {e.status_code}, detail: {e.detail}")
        logger.info("=" * 80)
        
        # Log error
        duration_ms = (time.time() - start_time) * 1000
        request_logger.log_request(
            endpoint="/start_job",
            method="POST",
            request_body=request_body,
            error=f"HTTPException: {e.detail}",
            status_code=e.status_code,
            duration_ms=duration_ms
        )
        
        raise
    except ValueError as e:
        logger.error(f"START_JOB: ValueError in start_job: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        
        # Log error
        duration_ms = (time.time() - start_time) * 1000
        request_logger.log_request(
            endpoint="/start_job",
            method="POST",
            request_body=request_body,
            error=f"ValueError: {str(e)}",
            status_code=400,
            duration_ms=duration_ms
        )
        
        if "PAYMENT_AMOUNT" in str(e):
            raise HTTPException(
                status_code=500,
                detail="Server configuration error: Invalid PAYMENT_AMOUNT value. Please contact administrator."
            )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid input data: {str(e)}"
        )
    except KeyError as e:
        logger.error(f"START_JOB: KeyError - Missing required field in request: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        
        # Log error
        duration_ms = (time.time() - start_time) * 1000
        request_logger.log_request(
            endpoint="/start_job",
            method="POST",
            request_body=request_body,
            error=f"KeyError: {str(e)}",
            status_code=400,
            duration_ms=duration_ms
        )
        
        raise HTTPException(
            status_code=400,
            detail=f"Missing required field: {str(e)}"
        )
    except Exception as e:
        logger.error(f"START_JOB: Unexpected exception in start_job: {str(e)}", exc_info=True)
        logger.error(f"START_JOB: Exception type: {type(e).__name__}")
        logger.info("=" * 80)
        
        # Log error
        duration_ms = (time.time() - start_time) * 1000
        request_logger.log_request(
            endpoint="/start_job",
            method="POST",
            request_body=request_body,
            error=f"{type(e).__name__}: {str(e)}",
            status_code=500,
            duration_ms=duration_ms
        )
        # check if it's a masumi payment service error
        if "Network error" in str(e) or "payment" in str(e).lower():
            raise HTTPException(
                status_code=502,
                detail="Payment service unavailable. Please try again later or contact administrator."
            )
        raise HTTPException(
            status_code=500,
            detail="Internal server error. Please contact administrator."
        )

# ─────────────────────────────────────────────────────────────────────────────
#region 2) Process Payment and Execute AI Task
# ─────────────────────────────────────────────────────────────────────────────
async def handle_payment_status(job_id: str, payment_id: str) -> None:
    """ Executes task after payment confirmation """
    logger.info("=" * 80)
    logger.info(f"HANDLE_PAYMENT: Payment {payment_id} confirmed for job {job_id}")
    logger.info(f"HANDLE_PAYMENT: Starting task execution...")
    try:
        # Update job status to running
        jobs[job_id]["status"] = "running"
        input_data = jobs[job_id]["input_data"]
        logger.info(f"HANDLE_PAYMENT: Input data for job {job_id}: {input_data}")

        # Check for failure injection before task execution
        failure_type = failure_injector.should_fail(
            FailurePoint.TASK_EXECUTION,
            input_data
        )
        if failure_type:
            failure_injector.inject_failure(
                FailurePoint.TASK_EXECUTION,
                failure_type,
                "Debug: Intentional failure during task execution"
            )
        
        # HITL: Check if HITL is enabled for this job
        # Handle both string and boolean values for enable_hitl
        enable_hitl_value = input_data.get("enable_hitl", "false")
        if isinstance(enable_hitl_value, bool):
            enable_hitl = enable_hitl_value
        elif isinstance(enable_hitl_value, str):
            enable_hitl = enable_hitl_value.lower() == "true"
        else:
            enable_hitl = False
        
        # Handle hitl_timeout - convert to int if string
        hitl_timeout_value = input_data.get("hitl_timeout", "300")
        if isinstance(hitl_timeout_value, str):
            try:
                hitl_timeout = int(hitl_timeout_value)
            except ValueError:
                hitl_timeout = 300  # Default 5 minutes
        elif isinstance(hitl_timeout_value, (int, float)):
            hitl_timeout = int(hitl_timeout_value)
        else:
            hitl_timeout = 300  # Default 5 minutes
        
        if enable_hitl:
            logger.info(f"HITL enabled for job {job_id}, requesting approval...")
            
            # Create approval request
            action_description = f"Execute debug task: {input_data.get('input_string', 'N/A')}"
            approval_info = hitl_manager.create_approval_request(
                job_id=job_id,
                action=action_description,
                timeout_seconds=hitl_timeout,
                metadata={
                    "input_data": input_data,
                    "payment_id": payment_id
                }
            )
            
            # Update job status to awaiting approval
            jobs[job_id]["status"] = "awaiting_human_approval"
            jobs[job_id]["approval_info"] = approval_info
            
            # Check for failure injection during HITL approval wait
            hitl_failure_type = failure_injector.should_fail(
                FailurePoint.HITL_APPROVAL,
                input_data
            )
            if hitl_failure_type:
                logger.warning(f"Injecting failure during HITL approval wait for job {job_id}")
                failure_injector.inject_failure(
                    FailurePoint.HITL_APPROVAL,
                    hitl_failure_type,
                    "Debug: Intentional failure during HITL approval"
                )
            
            # Check for timeout failure injection
            hitl_timeout_failure = failure_injector.should_fail(
                FailurePoint.HITL_TIMEOUT,
                input_data
            )
            
            # Wait for approval
            approval_status = await hitl_manager.wait_for_approval(job_id)
            
            if approval_status == ApprovalStatus.TIMEOUT:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = "HITL approval timeout"
                
                # Inject timeout failure if configured
                if hitl_timeout_failure:
                    failure_injector.inject_failure(
                        FailurePoint.HITL_TIMEOUT,
                        hitl_timeout_failure,
                        "Debug: Intentional failure on HITL timeout"
                    )
                else:
                    error_result = {"error": "HITL approval timeout - task execution cancelled", "status": "failed"}
                    await payment_instances[job_id].complete_payment(payment_id, error_result)
                    return
            
            elif approval_status == ApprovalStatus.REJECTED:
                jobs[job_id]["status"] = "rejected"
                jobs[job_id]["error"] = "Task execution rejected by operator"
                
                # Check for rejection failure injection
                rejection_failure = failure_injector.should_fail(
                    FailurePoint.HITL_REJECTION,
                    input_data
                )
                if rejection_failure:
                    failure_injector.inject_failure(
                        FailurePoint.HITL_REJECTION,
                        rejection_failure,
                        "Debug: Intentional failure on HITL rejection"
                    )
                else:
                    error_result = {"error": "Task execution rejected by operator", "status": "rejected"}
                    await payment_instances[job_id].complete_payment(payment_id, error_result)
                    return
            
            elif approval_status == ApprovalStatus.APPROVED:
                logger.info(f"Job {job_id} approved, proceeding with task execution...")
                jobs[job_id]["status"] = "running"
            else:
                error_result = {"error": f"Unexpected approval status: {approval_status}", "status": "failed"}
                await payment_instances[job_id].complete_payment(payment_id, error_result)
                return

        # Execute the AI task
        logger.info(f"TASK_EXEC: Starting task execution for job {job_id}")
        try:
            result = await execute_agentic_task(input_data)
            result_dict = result.json_dict  # type: ignore
            logger.info(f"TASK_EXEC: Task execution completed successfully for job {job_id}")
            logger.info(f"TASK_EXEC: Result type: {type(result)}, Result dict keys: {list(result_dict.keys()) if isinstance(result_dict, dict) else 'N/A'}")
        except ValueError as e:
            # Input validation error
            logger.error(f"Task execution failed due to invalid input: {str(e)}")
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = f"Invalid input: {str(e)}"
            # Still try to complete payment with error message
            error_result = {"error": str(e), "status": "failed"}
            await payment_instances[job_id].complete_payment(payment_id, error_result)
            return
        except Exception as e:
            # Other execution errors
            logger.error(f"Task execution failed: {str(e)}", exc_info=True)
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
            # Still try to complete payment with error message
            error_result = {"error": str(e), "status": "failed"}
            await payment_instances[job_id].complete_payment(payment_id, error_result)
            return
        
        # Check for failure on payment completion
        failure_type = failure_injector.should_fail(
            FailurePoint.PAYMENT_COMPLETION,
            input_data
        )
        if failure_type:
            failure_injector.inject_failure(
                FailurePoint.PAYMENT_COMPLETION,
                failure_type,
                "Debug: Intentional failure on payment completion"
            )
        
        # Mark payment as completed on Masumi
        # Use a shorter string for the result hash
        logger.info(f"JOB_COMPLETE: Sending results to payment service for job {job_id}")
        try:
            await payment_instances[job_id].complete_payment(payment_id, result_dict)
            logger.info(f"JOB_COMPLETE: Payment completed successfully for job {job_id}")
        except Exception as e:
            logger.error(f"JOB_COMPLETE: Error completing payment for job {job_id}: {str(e)}", exc_info=True)
            raise  # Re-raise to be caught by outer exception handler

        # Update job status
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["payment_status"] = "completed"
        jobs[job_id]["result"] = result
        logger.info(f"JOB_COMPLETE: Job {job_id} marked as completed successfully")

        # Stop monitoring payment status
        if job_id in payment_instances:
            try:
                payment_instances[job_id].stop_status_monitoring()
                logger.info(f"JOB_COMPLETE: Stopped payment monitoring for job {job_id}")
            except Exception as e:
                logger.warning(f"JOB_COMPLETE: Error stopping payment monitoring for job {job_id}: {str(e)}")
            del payment_instances[job_id]
    except Exception as e:
        logger.error(f"Error processing payment {payment_id} for job {job_id}: {str(e)}", exc_info=True)
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        
        # Still stop monitoring to prevent repeated failures
        if job_id in payment_instances:
            payment_instances[job_id].stop_status_monitoring()
            del payment_instances[job_id]

# ─────────────────────────────────────────────────────────────────────────────
#region 3) Check Job and Payment Status (MIP-003: /status)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/status")
async def get_status(job_id: str):
    """ Retrieves the current status of a specific job """
    logger.info(f"Checking status for job {job_id}")
    if job_id not in jobs:
        logger.warning(f"Job {job_id} not found")
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    
    # Check for failure injection on status check
    input_data = job.get("input_data", {})
    failure_type = failure_injector.should_fail(
        FailurePoint.STATUS_CHECK,
        input_data
    )
    if failure_type:
        failure_injector.inject_failure(
            FailurePoint.STATUS_CHECK,
            failure_type,
            "Debug: Intentional failure on status check"
        )

    # Check latest payment status if payment instance exists
    if job_id in payment_instances:
        try:
            status = await payment_instances[job_id].check_payment_status()
            logger.info(f"STATUS_CHECK: Raw payment status response for job {job_id}: {status}")
            payment_data = status.get("data", {})
            payment_status = payment_data.get("status")
            on_chain_state = payment_data.get("onChainState")
            next_action = payment_data.get("nextAction")
            logger.info(f"STATUS_CHECK: Payment status for job {job_id}: status={payment_status}, onChainState={on_chain_state}, nextAction={next_action}")
            job["payment_status"] = payment_status
            
            # If payment appears to be completed on-chain but callback hasn't fired, trigger it manually
            # Check if payment is actually paid but status monitoring hasn't detected it
            payment_id = job.get("payment_id")
            callback_key = f"{job_id}:{payment_id}"
            
            # Check various indicators that payment might be complete
            # If onChainState exists and is not None/empty, payment might be on-chain
            # If nextAction changes from WaitingForExternalAction, payment might be progressing
            payment_might_be_complete = False
            
            if on_chain_state and on_chain_state not in [None, "None", ""]:
                logger.info(f"STATUS_CHECK: Payment {job_id} has onChainState={on_chain_state}, checking if payment is complete")
                payment_might_be_complete = True
            
            # If callback hasn't been triggered yet and payment might be complete, check manually
            if callback_key not in payment_callbacks_triggered and payment_might_be_complete and payment_id:
                logger.warning(f"STATUS_CHECK: Payment {job_id} may be complete but callback not triggered. Checking payment status manually...")
                # Try to manually check if we should trigger the callback
                # This is a fallback in case the payment monitoring library doesn't detect completion
                try:
                    # Check payment status directly
                    payment_status_check = await payment_instances[job_id].check_payment_status()
                    payment_data_check = payment_status_check.get("data", {})
                    logger.info(f"STATUS_CHECK: Manual payment check result: {payment_data_check}")
                    
                    # If status indicates payment is ready, trigger callback manually
                    # Note: This is a workaround - ideally the payment monitoring library should handle this
                    if payment_data_check.get("nextAction") not in ["WaitingForExternalAction", None]:
                        logger.info(f"STATUS_CHECK: Payment {job_id} appears ready, triggering callback manually")
                        payment_callbacks_triggered.add(callback_key)
                        # Trigger handle_payment_status directly in background (same as callback would do)
                        asyncio.create_task(handle_payment_status(job_id, payment_id))
                except Exception as e:
                    logger.error(f"STATUS_CHECK: Error manually checking payment status: {str(e)}", exc_info=True)
        except ValueError as e:
            logger.warning(f"Error checking payment status: {str(e)}")
            job["payment_status"] = "unknown"
        except Exception as e:
            logger.error(f"Error checking payment status: {str(e)}", exc_info=True)
            job["payment_status"] = "error"


    result_data = job.get("result")
    result = result_data.raw if result_data and hasattr(result_data, "raw") else None
    
    # MIP-003 compliant status response
    status_response = {
        "id": job_id,  # MIP-003 uses "id" not "job_id"
        "status": job["status"]
    }
    
    # Add result if job is completed
    if job["status"] == "completed" and result:
        status_response["result"] = result
    
    # Add input_required if job is awaiting input (MIP-003 requirement)
    if job["status"] == "awaiting_input":
        status_response["input_required"] = True
    
    # Add additional fields for backward compatibility
    status_response["job_id"] = job_id
    status_response["payment_status"] = job.get("payment_status", "unknown")
    if result and "result" not in status_response:
        status_response["result"] = result
    
    return status_response

# ─────────────────────────────────────────────────────────────────────────────
#region HITL Approval Endpoint
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/approve")
async def approve_job(request: ApprovalRequest):
    """
    Human-in-the-Loop approval endpoint.
    Allows human operators to approve or reject pending task executions.
    """
    job_id = request.job_id
    
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Check for failure injection on approval endpoint
    job = jobs[job_id]
    input_data = job.get("input_data", {})
    failure_type = failure_injector.should_fail(
        FailurePoint.HITL_APPROVAL,
        input_data
    )
    if failure_type:
        failure_injector.inject_failure(
            FailurePoint.HITL_APPROVAL,
            failure_type,
            "Debug: Intentional failure on approval endpoint"
        )
    
    approval_status = hitl_manager.get_approval_status(job_id)
    if not approval_status:
        raise HTTPException(
            status_code=400, 
            detail="No pending approval request found for this job"
        )
    
    if approval_status["status"] != ApprovalStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Approval already resolved: {approval_status['status']}"
        )
    
    if request.approve:
        success = hitl_manager.approve(job_id, request.operator)
        if success:
            return {
                "job_id": job_id,
                "status": "approved",
                "message": "Task execution approved"
            }
        else:
            raise HTTPException(status_code=400, detail="Failed to approve job")
    else:
        success = hitl_manager.reject(job_id, request.reason, request.operator)
        if success:
            return {
                "job_id": job_id,
                "status": "rejected",
                "reason": request.reason,
                "message": "Task execution rejected"
            }
        else:
            raise HTTPException(status_code=400, detail="Failed to reject job")

@app.get("/approve")
async def get_approval_status(job_id: str):
    """
    Get current approval status for a job.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    approval_info = hitl_manager.get_approval_status(job_id)
    if not approval_info:
        return {
            "job_id": job_id,
            "status": "no_approval_required",
            "message": "No approval request for this job"
        }
    
    return {
        "job_id": job_id,
        "status": approval_info["status"],
        "action": approval_info["action"],
        "timeout_seconds": approval_info["timeout"],
        "elapsed_seconds": time.time() - approval_info["timestamp"],
        "remaining_seconds": max(0, approval_info["timeout"] - (time.time() - approval_info["timestamp"]))
    }

# ─────────────────────────────────────────────────────────────────────────────
#region 4) Check Server Availability (MIP-003: /availability)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/availability")
async def check_availability():
    """ Checks if the server is operational """
    # For availability, we need to check input_data differently
    # Since it's a GET request, we'll use query params or env var
    debug_mode = os.getenv("DEBUG_MODE", "false").lower() == "true"
    fail_on_availability = os.getenv("FAIL_ON_AVAILABILITY", "").lower()
    
    if debug_mode and fail_on_availability:
        try:
            failure_type = FailureType(fail_on_availability)
            failure_injector.inject_failure(
                FailurePoint.AVAILABILITY,
                failure_type,
                "Debug: Intentional failure on availability check"
            )
        except ValueError:
            logger.warning(f"Unknown failure type for availability: {fail_on_availability}")
    
    current_time = time.time()
    uptime_seconds = int(current_time - server_start_time)
    
    # MIP-003 compliant availability response
    return {
        "status": "available",
        "type": "masumi-agent",  # MIP-003 requires "type" field
        "message": "Debug Agent - Server operational. Use input_data with debug commands to test failures."
    }

# ─────────────────────────────────────────────────────────────────────────────
#region 5) Retrieve Input Schema (MIP-003: /input_schema)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/input_schema")
async def input_schema():
    """
    Returns the expected input schema for the /start_job endpoint.
    Includes debug command options for testing.
    Fulfills MIP-003 /input_schema endpoint.
    """
    return {
        "input_data": [
            {
                "id": "input_string",
                "type": "string",
                "name": "Task Description",
                "data": {
                    "description": "The text input for the debug task",
                    "placeholder": "Enter your task description here"
                }
            },
            {
                "id": "failure_point",
                "type": "option",
                "name": "Failure Point (optional)",
                "data": {
                    "description": "Point to inject failure during execution. Use 'none' to explicitly disable failure injection. If not provided, execution proceeds normally.",
                    "values": [
                        "none",
                        "fail_on_start_job",
                        "fail_on_payment_creation",
                        "fail_on_payment_monitoring",
                        "fail_on_task_execution",
                        "fail_on_payment_completion",
                        "fail_on_status_check",
                        "fail_on_availability",
                        "fail_on_input_schema",
                        "fail_on_hitl_approval",
                        "fail_on_hitl_timeout",
                        "fail_on_hitl_rejection"
                    ]
                },
                "validations": [
                    { "validation": "min", "value": "0" },
                    { "validation": "max", "value": "1" }
                ]
            },
            {
                "id": "failure_type",
                "type": "option",
                "name": "Failure Type (optional)",
                "data": {
                    "description": "Type of failure to inject. Required if failure_point is specified (use 'none' to disable).",
                    "values": [
                        "none",
                        "http_400",
                        "http_404",
                        "http_500",
                        "http_503",
                        "timeout",
                        "exception",
                        "invalid_response"
                    ]
                },
                "validations": [
                    { "validation": "min", "value": "0" },
                    { "validation": "max", "value": "1" }
                ]
            },
            {
                "id": "enable_hitl",
                "type": "boolean",
                "name": "Enable HITL (optional)",
                "data": {
                    "description": "Enable Human-in-the-Loop approval workflow. If not enabled, execution proceeds automatically.",
                    "placeholder": "false"
                }
            },
            {
                "id": "hitl_timeout",
                "type": "number",
                "name": "HITL Timeout (optional)",
                "data": {
                    "description": "Timeout in seconds for HITL approval (default: 300). Only used if HITL is enabled.",
                    "placeholder": "300"
                }
            }
        ]
    }

# ─────────────────────────────────────────────────────────────────────────────
#region 6) Health Check
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """
    Returns the health of the server.
    """
    return {
        "status": "healthy"
    }

# ─────────────────────────────────────────────────────────────────────────────
#region Debug Logging Endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/logs")
async def get_logs(
    endpoint: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    status_code: Optional[int] = None
):
    """
    Retrieve request logs for debugging.
    
    Query parameters:
    - endpoint: Filter by endpoint (e.g., "/start_job")
    - limit: Maximum number of logs to return (default: 100)
    - offset: Number of logs to skip (default: 0)
    - status_code: Filter by HTTP status code (e.g., 422)
    
    Returns:
    - List of log entries with request/response details
    """
    logs_data = request_logger.get_logs(
        endpoint=endpoint,
        limit=limit,
        offset=offset,
        status_code=status_code
    )
    return logs_data

@app.get("/logs/latest-error")
async def get_latest_error():
    """
    Get the most recent error log entry.
    Useful for quick debugging of the last failure.
    """
    error_log = request_logger.get_latest_error()
    if error_log:
        return error_log
    return {"message": "No errors found in logs"}

@app.delete("/logs")
async def clear_logs():
    """
    Clear all request logs.
    Use with caution - this cannot be undone.
    """
    request_logger.clear_logs()
    return {"message": "All logs cleared successfully"}

# ─────────────────────────────────────────────────────────────────────────────
#region Main Logic if Called as a Script
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("Running task as standalone script is not supported when using payments.")
    print("Start the API using `python main.py api` instead.")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "api":
        print("Starting FastAPI server with Masumi integration...")
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        main()
