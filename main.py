import os
import uvicorn
import uuid
import secrets
import asyncio
import time
import json
import hashlib
from typing import Optional, Any
from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException, Request, Body
from pydantic import BaseModel, Field, field_validator, model_validator
from masumi.config import Config
from masumi.payment import Payment, Amount
from logging_config import setup_logging
from failure_injector import FailureInjector, FailurePoint, FailureType
from hitl_manager import HITLManager, ApprovalStatus

# Configure logging
logger = setup_logging()

# Load environment variables
load_dotenv(override=True)

# Retrieve API Keys and URLs
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL")
PAYMENT_API_KEY = os.getenv("PAYMENT_API_KEY")
NETWORK = os.getenv("NETWORK")

logger.info("Starting application with configuration:")
logger.info(f"PAYMENT_SERVICE_URL: {PAYMENT_SERVICE_URL}")

# Initialize FastAPI
app = FastAPI(
    title="API following the Masumi API Standard",
    description="API for running Agentic Services tasks with Masumi payment integration",
    version="1.0.0"
)

# ─────────────────────────────────────────────────────────────────────────────
# Temporary in-memory job store (DO NOT USE IN PRODUCTION)
# ─────────────────────────────────────────────────────────────────────────────
jobs = {}
payment_instances = {}

# ─────────────────────────────────────────────────────────────────────────────
# Initialize Masumi Payment Config
# ─────────────────────────────────────────────────────────────────────────────
config = Config(
    payment_service_url=PAYMENT_SERVICE_URL,
    payment_api_key=PAYMENT_API_KEY
)

# ─────────────────────────────────────────────────────────────────────────────
# Initialize Failure Injector for Debug Agent
# ─────────────────────────────────────────────────────────────────────────────
failure_injector = FailureInjector()

# ─────────────────────────────────────────────────────────────────────────────
# Initialize HITL Manager
# ─────────────────────────────────────────────────────────────────────────────
hitl_manager = HITLManager()

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
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
    identifier_from_purchaser: Optional[str] = None
    input_data: list[InputDataItem]  # Must be a list of InputDataItem
    
    @model_validator(mode='before')
    @classmethod
    def normalize_input_data(cls, data):
        """Normalize input_data from different formats to our internal format"""
        if isinstance(data, dict):
            # Handle identifier_from_purchaser (optional)
            if 'identifier_from_purchaser' not in data:
                data['identifier_from_purchaser'] = None
            
            if 'input_data' in data:
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
                    data['input_data'] = [{"key": "text", "value": str(input_data_value)}]
        return data
    
    class Config:
        json_schema_extra = {
            "example": {
                "input_data": [
                    {"key": "text", "value": "Hello World"}
                ]
            }
        }

class ProvideInputRequest(BaseModel):
    job_id: str
    status_id: Optional[str] = None
    input_data: dict[str, Any]  # Accept dict format directly as Sokosumi sends it
    
    @model_validator(mode='before')
    @classmethod
    def normalize_request(cls, data: Any):
        """Normalize request from different formats"""
        logger.info(f"PROVIDE_INPUT normalize: Received data type: {type(data)}, data: {data}")
        
        # Handle Sokosumi format: [{"input": {"jobId": "...", "inputData": {...}}}]
        if isinstance(data, list) and len(data) > 0:
            first_item = data[0]
            if isinstance(first_item, dict) and "input" in first_item:
                input_obj = first_item["input"]
                logger.info(f"PROVIDE_INPUT normalize: Found Sokosumi array format with input object: {input_obj}")
                
                normalized_data = {}
                # Extract jobId -> job_id
                if "jobId" in input_obj:
                    normalized_data["job_id"] = input_obj["jobId"]
                elif "job_id" in input_obj:
                    normalized_data["job_id"] = input_obj["job_id"]
                else:
                    raise ValueError("Missing jobId or job_id in input")
                
                # Extract statusId -> status_id (optional)
                if "statusId" in input_obj:
                    normalized_data["status_id"] = input_obj["statusId"]
                elif "status_id" in input_obj:
                    normalized_data["status_id"] = input_obj["status_id"]
                
                # Keep inputData as dict (not converting to list)
                if "inputData" in input_obj:
                    normalized_data["input_data"] = input_obj["inputData"]
                elif "input_data" in input_obj:
                    normalized_data["input_data"] = input_obj["input_data"]
                else:
                    normalized_data["input_data"] = {}
                
                logger.info(f"PROVIDE_INPUT normalize: Normalized to: {normalized_data}")
                return normalized_data
        
        # Handle standard format: {"job_id": "...", "status_id": "...", "input_data": {...}}
        elif isinstance(data, dict):
            normalized_data = dict(data)  # Create a copy
            # Handle camelCase fields from Sokosumi
            if "jobId" in normalized_data:
                normalized_data["job_id"] = normalized_data.pop("jobId")
            if "statusId" in normalized_data:
                normalized_data["status_id"] = normalized_data.pop("statusId")
            if "inputData" in normalized_data:
                normalized_data["input_data"] = normalized_data.pop("inputData")
            
            logger.info(f"PROVIDE_INPUT normalize: Standard format normalized to: {normalized_data}")
            return normalized_data
        
        logger.warning(f"PROVIDE_INPUT normalize: Unknown format, returning as-is: {data}")
        return data

class ApprovalRequest(BaseModel):
    job_id: str
    action: Optional[str] = "task_execution"
    approve: bool = True
    reason: Optional[str] = None
    operator: Optional[str] = None

# ─────────────────────────────────────────────────────────────────────────────
# Test Execution and Summary Generation
# ─────────────────────────────────────────────────────────────────────────────
def format_test_summary_as_agent_response(summary_dict: dict) -> str:
    """Format test summary as a natural language agent response"""
    test_result = summary_dict.get("test_result", "UNKNOWN")
    duration = summary_dict.get("duration_seconds", 0)
    tested_interfaces = summary_dict.get("tested_interfaces", [])
    triggered_failures = summary_dict.get("triggered_failures", [])
    failure_count = summary_dict.get("failure_count", 0)
    error = summary_dict.get("error")
    
    # Format duration nicely
    if duration < 1:
        duration_str = f"{int(duration * 1000)}ms"
    elif duration < 60:
        duration_str = f"{duration:.2f}s"
    else:
        minutes = int(duration // 60)
        seconds = duration % 60
        duration_str = f"{minutes}m {seconds:.1f}s"
    
    # Start building the response
    lines = []
    lines.append("TEST EXECUTION REPORT")
    lines.append("")
    
    # Test result status
    if test_result == "PASSED":
        lines.append("✅ Test Status: PASSED")
        lines.append("")
        lines.append("All interfaces were tested successfully and no failures were detected.")
    elif test_result == "COMPLETED_WITH_FAILURES":
        lines.append("⚠️  Test Status: COMPLETED WITH INTENTIONAL FAILURES")
        lines.append("")
        lines.append(f"The test completed with {failure_count} intentional failure(s) injected for testing purposes.")
        if error:
            lines.append(f"Error encountered: {error}")
    elif test_result == "FAILED":
        if error:
            lines.append("❌ Test Status: FAILED")
            lines.append("")
            lines.append(f"Error encountered: {error}")
        else:
            lines.append("⚠️  Test Status: COMPLETED WITH INTENTIONAL FAILURES")
            lines.append("")
            lines.append(f"The test completed successfully with {failure_count} intentional failure(s) injected for testing purposes.")
    else:
        lines.append(f"❓ Test Status: {test_result}")
    
    lines.append("-" * 70)
    lines.append("EXECUTION SUMMARY")
    lines.append("-" * 70)
    lines.append(f"⏱️  Duration: {duration_str}")
    lines.append("")
    
    # Tested interfaces
    lines.append("📋 Interfaces Tested:")
    interface_names = {
        "start_job": "Start Job Endpoint",
        "payment_creation": "Payment Creation",
        "payment_monitoring": "Payment Monitoring",
        "task_execution": "Task Execution",
        "payment_completion": "Payment Completion",
        "status_check": "Status Check Endpoint"
    }
    
    for interface in tested_interfaces:
        display_name = interface_names.get(interface, interface.replace("_", " ").title())
        lines.append(f"   • {display_name}")
    
    lines.append("")
    
    # Failure details if any
    if triggered_failures:
        lines.append("-" * 70)
        lines.append("INJECTED FAILURES (Intentional)")
        lines.append("-" * 70)
        for failure in triggered_failures:
            interface = failure.get("interface", "unknown")
            failure_type = failure.get("failure_type", "unknown")
            display_name = interface_names.get(interface, interface.replace("_", " ").title())
            failure_type_display = failure_type.replace("_", " ").upper()
            lines.append(f"   • {display_name}: {failure_type_display}")
        lines.append("")
        lines.append("Note: These failures were intentionally injected for testing purposes.")
        lines.append("")
    
    # Final summary
    if test_result == "PASSED":
        lines.append("✅ All tests completed successfully!")
    elif test_result == "COMPLETED_WITH_FAILURES":
        lines.append("✅ Test execution completed with intentional failure injection.")
    elif error:
        lines.append("❌ Test execution encountered an error.")
    else:
        lines.append("✅ Test execution completed with intentional failure injection.")
    
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# 1) Start Job (MIP-003: /start_job)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/start_job")
async def start_job(data: StartJobRequest):
    """ Initiates a job and creates a payment request """
    print(f"Received data: {data}")
    print(f"Received data.input_data: {data.input_data}")
    try:
        # Convert input_data array to dict for internal processing
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
                            # Index 0 = "fail_on_start_job", index 1 = "fail_on_payment_creation", etc.
                            # "none" option removed - empty/missing values behave like "none"
                            failure_points = ["fail_on_start_job", "fail_on_payment_creation", 
                                            "fail_on_payment_monitoring", "fail_on_task_execution",
                                            "fail_on_payment_completion", "fail_on_status_check",
                                            "fail_on_availability",
                                            "fail_on_hitl_approval", "fail_on_hitl_timeout", "fail_on_hitl_rejection"]
                            if 0 <= idx < len(failure_points):
                                value = failure_points[idx]
                            else:
                                value = None  # Invalid index means no failure
                        elif item.key == "failure_type":
                            # Index 0 = "http_400", index 1 = "http_404", etc.
                            # "none" option removed - empty/missing values behave like "none"
                            failure_types = ["http_400", "http_404", "http_500", "http_503",
                                           "timeout", "exception", "invalid_response"]
                            if 0 <= idx < len(failure_types):
                                value = failure_types[idx]
                            else:
                                value = None  # Invalid index means no failure
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
            if value is None:
                input_data_dict[item.key] = ""
            else:
                input_data_dict[item.key] = value
        
        # Initialize test tracking early (before any failure injection checks)
        test_start_time = time.time()
        tested_interfaces = []
        injected_failures = []
        
        job_id = str(uuid.uuid4())
        agent_identifier = os.getenv("AGENT_IDENTIFIER")
        
        # Check for failure injection at start_job (before job creation)
        tested_interfaces.append("start_job")
        failure_type = failure_injector.should_fail(
            FailurePoint.START_JOB, 
            input_data_dict
        )
        if failure_type:
            injected_failures.append({
                "interface": "start_job",
                "failure_type": failure_type.value,
                "status": "triggered"
            })
            failure_injector.inject_failure(
                FailurePoint.START_JOB, 
                failure_type,
                "Debug: Intentional failure on start_job"
            )
        else:
            injected_failures.append({
                "interface": "start_job",
                "failure_type": "none",
                "status": "passed"
            })
        
        logger.info(f"Starting test job {job_id} with agent {agent_identifier}")

        # Use identifier_from_purchaser from request, or generate if not provided
        identifier_from_purchaser = data.identifier_from_purchaser
        if not identifier_from_purchaser:
            # Generate identifier_from_purchaser internally - must be a valid hex string
            # Payment service requires max 26 characters, so generate 13 bytes (26 hex characters)
            identifier_from_purchaser = secrets.token_hex(13)
            logger.info(f"Generated identifier_from_purchaser: {identifier_from_purchaser}")
        else:
            logger.info(f"Using identifier_from_purchaser from request: {identifier_from_purchaser}")

        # Define payment amounts
        payment_amount = os.getenv("PAYMENT_AMOUNT", "10000000")  # Default 10 ADA
        payment_unit = os.getenv("PAYMENT_UNIT", "lovelace") # Default lovelace

        amounts = [Amount(amount=payment_amount, unit=payment_unit)]
        logger.info(f"Using payment amount: {payment_amount} {payment_unit}")
        
        # Check for failure on payment creation
        tested_interfaces.append("payment_creation")
        failure_type = failure_injector.should_fail(
            FailurePoint.PAYMENT_CREATION,
            input_data_dict
        )
        if failure_type:
            injected_failures.append({
                "interface": "payment_creation",
                "failure_type": failure_type.value,
                "status": "triggered"
            })
            failure_injector.inject_failure(
                FailurePoint.PAYMENT_CREATION,
                failure_type,
                "Debug: Intentional failure on payment creation"
            )
        else:
            injected_failures.append({
                "interface": "payment_creation",
                "failure_type": "none",
                "status": "passed"
            })
        
        # Create a payment request using Masumi
        # For input_hash calculation, we need to match how orig_main.py handles dict[str, str]
        # orig_main.py passes data.input_data directly which is dict[str, str] with Pydantic validation
        # Pydantic's dict[str, str] coerces all values to strings automatically
        # The Payment class likely calculates hash from JSON-serialized sorted dict
        # So we need to:
        # 1. Convert all values to strings (matching Pydantic's dict[str, str] coercion)
        # 2. Sort keys alphabetically for consistent hashing (like JSON serialization)
        # 3. Only include fields that are actually in the input (no dummy fields)
        
        payment_input_data = {}
        for k, v in input_data_dict.items():
            # Convert to string matching Pydantic's dict[str, str] coercion behavior
            if v is None:
                payment_input_data[k] = ""
            elif isinstance(v, bool):
                # Pydantic converts bool to lowercase string: True -> "true", False -> "false"
                payment_input_data[k] = str(v).lower()
            elif isinstance(v, (int, float)):
                # Pydantic converts numbers to string representation
                payment_input_data[k] = str(v)
            else:
                # Already a string or convert to string
                payment_input_data[k] = str(v)
        
        # Sort keys alphabetically to ensure consistent hash calculation
        # This matches how JSON serialization would order keys for hashing
        payment_input_data = dict(sorted(payment_input_data.items()))
        
        payment = Payment(
            agent_identifier=agent_identifier,
            #amounts=amounts,
            config=config,
            identifier_from_purchaser=identifier_from_purchaser,
            input_data=payment_input_data,
            network=NETWORK
        )
        
        logger.info("Creating payment request...")
        payment_request = await payment.create_payment_request()
        blockchain_identifier = payment_request["data"]["blockchainIdentifier"]
        payment.payment_ids.add(blockchain_identifier)
        logger.info(f"Created payment request with ID: {blockchain_identifier}")

        # Store job info (Awaiting payment)
        jobs[job_id] = {
            "status": "awaiting_payment",
            "payment_status": "pending",
            "payment_id": blockchain_identifier,
            "input_data": input_data_dict,
            "result": None,
            "identifier_from_purchaser": identifier_from_purchaser,
            "test_start_time": test_start_time,
            "tested_interfaces": tested_interfaces,
            "injected_failures": injected_failures
        }

        async def payment_callback(blockchain_identifier: str):
            await handle_payment_status(job_id, blockchain_identifier)

        # Start monitoring the payment status
        payment_instances[job_id] = payment
        logger.info(f"Starting payment status monitoring for job {job_id}")
        await payment.start_status_monitoring(payment_callback)

        # Return the response in the required format (matching orig_main.py exactly)
        return {
            "status": "success",
            "id": job_id,
            "blockchainIdentifier": blockchain_identifier,
            "submitResultTime": payment_request["data"]["submitResultTime"],
            "unlockTime": payment_request["data"]["unlockTime"],
            "externalDisputeUnlockTime": payment_request["data"]["externalDisputeUnlockTime"],
            "agentIdentifier": agent_identifier,
            "sellerVKey": os.getenv("SELLER_VKEY"),
            "identifierFromPurchaser": identifier_from_purchaser,
            "amounts": amounts,
            "input_hash": payment.input_hash,
            "payByTime": payment_request["data"]["payByTime"],
        }
    except KeyError as e:
        logger.error(f"Missing required field in request: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail="Bad Request: If input_data is missing, invalid, or does not adhere to the schema."
        )
    except Exception as e:
        logger.error(f"Error in start_job: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail="Input_data is missing, invalid, or does not adhere to the schema."
        )

# ─────────────────────────────────────────────────────────────────────────────
# 2) Process Payment and Execute AI Task
# payment_id is the blockchain identifier of the payment
# ─────────────────────────────────────────────────────────────────────────────
async def handle_payment_status(job_id: str, payment_id: str) -> None: 
    """ Executes test after payment confirmation """
    try:
        logger.info(f"Payment {payment_id} completed for job {job_id}, executing test...")
        
        # Update job status to running
        jobs[job_id]["status"] = "running"
        input_data = jobs[job_id]["input_data"]
        logger.info(f"Input data: {input_data}")

        # Track payment monitoring interface
        jobs[job_id]["tested_interfaces"].append("payment_monitoring")

        # HITL: Check if HITL is enabled for this job
        # Handle both string and boolean values for enable_hitl
        enable_hitl_value = input_data.get("enable_hitl", False)
        if isinstance(enable_hitl_value, bool):
            enable_hitl = enable_hitl_value
        elif isinstance(enable_hitl_value, str):
            enable_hitl = enable_hitl_value.lower() == "true"
        else:
            enable_hitl = False

        # Handle hitl_timeout - convert to int if string
        hitl_timeout_value = input_data.get("hitl_timeout", 300)
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
            action_description = f"Execute test task for job {job_id}"
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
            # Use "awaiting_human_approval" internally, will be mapped to "awaiting_input" in /status for MIP-003
            jobs[job_id]["status"] = "awaiting_human_approval"
            jobs[job_id]["approval_info"] = approval_info
            
            # Track HITL interface
            jobs[job_id]["tested_interfaces"].append("hitl_approval")
            
            # Check for failure injection during HITL approval wait
            hitl_failure_type = failure_injector.should_fail(
                FailurePoint.HITL_APPROVAL,
                input_data
            )
            if hitl_failure_type:
                jobs[job_id]["injected_failures"].append({
                    "interface": "hitl_approval",
                    "failure_type": hitl_failure_type.value,
                    "status": "triggered"
                })
                logger.warning(f"Injecting failure during HITL approval wait for job {job_id}")
                failure_injector.inject_failure(
                    FailurePoint.HITL_APPROVAL,
                    hitl_failure_type,
                    "Debug: Intentional failure during HITL approval"
                )
            else:
                jobs[job_id]["injected_failures"].append({
                    "interface": "hitl_approval",
                    "failure_type": "none",
                    "status": "passed"
                })
            
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
                
                # Track timeout
                if hitl_timeout_failure:
                    jobs[job_id]["injected_failures"].append({
                        "interface": "hitl_timeout",
                        "failure_type": hitl_timeout_failure.value,
                        "status": "triggered"
                    })
                    failure_injector.inject_failure(
                        FailurePoint.HITL_TIMEOUT,
                        hitl_timeout_failure,
                        "Debug: Intentional failure on HITL timeout"
                    )
                else:
                    error_result = {"error": "HITL approval timeout - task execution cancelled", "status": "failed"}
                    await payment_instances[job_id].complete_payment(payment_id, json.dumps(error_result))
                    return
            
            elif approval_status == ApprovalStatus.REJECTED:
                jobs[job_id]["status"] = "rejected"
                jobs[job_id]["error"] = "Task execution rejected by operator"
                
                # Track rejection
                rejection_failure = failure_injector.should_fail(
                    FailurePoint.HITL_REJECTION,
                    input_data
                )
                if rejection_failure:
                    jobs[job_id]["injected_failures"].append({
                        "interface": "hitl_rejection",
                        "failure_type": rejection_failure.value,
                        "status": "triggered"
                    })
                    failure_injector.inject_failure(
                        FailurePoint.HITL_REJECTION,
                        rejection_failure,
                        "Debug: Intentional failure on HITL rejection"
                    )
                else:
                    error_result = {"error": "Task execution rejected by operator", "status": "rejected"}
                    await payment_instances[job_id].complete_payment(payment_id, json.dumps(error_result))
                    return
            
            elif approval_status == ApprovalStatus.APPROVED:
                logger.info(f"Job {job_id} approved, proceeding with task execution...")
                jobs[job_id]["status"] = "running"
            else:
                error_result = {"error": f"Unexpected approval status: {approval_status}", "status": "failed"}
                await payment_instances[job_id].complete_payment(payment_id, json.dumps(error_result))
                return

        # Check for failure injection before task execution
        jobs[job_id]["tested_interfaces"].append("task_execution")
        failure_type = failure_injector.should_fail(
            FailurePoint.TASK_EXECUTION,
            input_data
        )
        if failure_type:
            jobs[job_id]["injected_failures"].append({
                "interface": "task_execution",
                "failure_type": failure_type.value,
                "status": "triggered"
            })
            failure_injector.inject_failure(
                FailurePoint.TASK_EXECUTION,
                failure_type,
                "Debug: Intentional failure during task execution"
            )
        else:
            jobs[job_id]["injected_failures"].append({
                "interface": "task_execution",
                "failure_type": "none",
                "status": "passed"
            })

        # Check for failure on payment completion
        jobs[job_id]["tested_interfaces"].append("payment_completion")
        failure_type = failure_injector.should_fail(
            FailurePoint.PAYMENT_COMPLETION,
            input_data
        )
        if failure_type:
            jobs[job_id]["injected_failures"].append({
                "interface": "payment_completion",
                "failure_type": failure_type.value,
                "status": "triggered"
            })
            failure_injector.inject_failure(
                FailurePoint.PAYMENT_COMPLETION,
                failure_type,
                "Debug: Intentional failure on payment completion"
            )
        else:
            jobs[job_id]["injected_failures"].append({
                "interface": "payment_completion",
                "failure_type": "none",
                "status": "passed"
            })
        
        # Calculate test duration
        test_end_time = time.time()
        test_duration_seconds = test_end_time - jobs[job_id]["test_start_time"]
        
        # Generate test summary
        tested_interfaces_list = jobs[job_id]["tested_interfaces"]
        injected_failures_list = jobs[job_id]["injected_failures"]
        
        # Determine test result
        triggered_failures = [f for f in injected_failures_list if f["status"] == "triggered"]
        test_passed = len(triggered_failures) == 0
        
        test_summary = {
            "test_result": "PASSED" if test_passed else "FAILED",
            "summary": f"Test {'completed successfully' if test_passed else 'failed with intentional failures'}",
            "duration_seconds": round(test_duration_seconds, 2),
            "tested_interfaces": tested_interfaces_list,
            "injected_failures": injected_failures_list,
            "failure_count": len(triggered_failures)
        }
        
        if triggered_failures:
            test_summary["triggered_failures"] = triggered_failures
            test_summary["summary"] = f"Test completed with {len(triggered_failures)} intentional failure(s) injected"
        
        # Format as natural language agent response
        formatted_result = format_test_summary_as_agent_response(test_summary)
        
        # For payment completion, use JSON (required by Masumi)
        result_string = json.dumps(test_summary, indent=2)
        
        # Create a result object with .raw attribute containing formatted agent response
        # Match orig_main.py's CrewOutput pattern
        class TestResult:
            def __init__(self, formatted_text, summary_dict):
                self.raw = formatted_text  # Formatted agent response
                self._summary_dict = summary_dict  # Keep dict for reference
        
        result = TestResult(formatted_result, test_summary)
        
        # Mark payment as completed on Masumi
        await payment_instances[job_id].complete_payment(payment_id, result_string)
        logger.info(f"Payment completed for job {job_id}")

        # Update job status - store result object like orig_main.py does
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["payment_status"] = "completed"
        jobs[job_id]["result"] = result
        
        # Generate and store a new completion_id for this job
        # This ensures Sokosumi detects the completion with a new id
        completion_id = str(uuid.uuid4())
        jobs[job_id]["completion_id"] = completion_id
        logger.info(f"JOB_COMPLETE: Generated completion_id {completion_id} for job {job_id}")

        # Stop monitoring payment status
        if job_id in payment_instances:
            payment_instances[job_id].stop_status_monitoring()
            del payment_instances[job_id]
        
        # Clean up HITL approval data
        hitl_manager.cleanup(job_id)
    except Exception as e:
        print(f"Error processing payment {payment_id} for job {job_id}: {str(e)}")
        
        # Generate error test summary
        test_end_time = time.time()
        test_duration_seconds = test_end_time - jobs[job_id].get("test_start_time", test_end_time)
        
        tested_interfaces_list = jobs[job_id].get("tested_interfaces", [])
        injected_failures_list = jobs[job_id].get("injected_failures", [])
        
        # Extract triggered failures
        triggered_failures = [f for f in injected_failures_list if f.get("status") == "triggered"]
        
        # Check if this was an intentional failure or an actual error
        error_message = str(e)
        is_intentional_failure = (
            len(triggered_failures) > 0 or
            "Intentional failure" in error_message or 
            "Debug:" in error_message
        )
        
        # Determine test result based on whether failures were intentional
        if is_intentional_failure and len(triggered_failures) > 0:
            test_result = "COMPLETED_WITH_FAILURES"
            summary = f"Test completed with {len(triggered_failures)} intentional failure(s) injected"
        else:
            test_result = "FAILED"
            summary = f"Test failed with error: {error_message}"
        
        test_summary = {
            "test_result": test_result,
            "summary": summary,
            "duration_seconds": round(test_duration_seconds, 2),
            "tested_interfaces": tested_interfaces_list,
            "injected_failures": injected_failures_list,
            "triggered_failures": triggered_failures,
            "failure_count": len(triggered_failures),
            "error": error_message,
            "is_intentional_failure": is_intentional_failure
        }
        
        # Format as natural language agent response
        formatted_result = format_test_summary_as_agent_response(test_summary)
        
        # Create a result object with .raw attribute containing formatted agent response
        class TestResult:
            def __init__(self, formatted_text, summary_dict):
                self.raw = formatted_text  # Formatted agent response
                self._summary_dict = summary_dict  # Keep dict for reference
        
        result = TestResult(formatted_result, test_summary)
        
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["result"] = result
        
        # Still stop monitoring to prevent repeated failures
        if job_id in payment_instances:
            payment_instances[job_id].stop_status_monitoring()
            del payment_instances[job_id]
        
        # Clean up HITL approval data
        hitl_manager.cleanup(job_id)

# ─────────────────────────────────────────────────────────────────────────────
# 3) Check Job and Payment Status (MIP-003: /status)
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
    
    # Track status_check interface if not already tracked
    if "status_check" not in job.get("tested_interfaces", []):
        if "tested_interfaces" not in job:
            job["tested_interfaces"] = []
        job["tested_interfaces"].append("status_check")
    
    failure_type = failure_injector.should_fail(
        FailurePoint.STATUS_CHECK,
        input_data
    )
    if failure_type:
        if "injected_failures" not in job:
            job["injected_failures"] = []
        job["injected_failures"].append({
            "interface": "status_check",
            "failure_type": failure_type.value,
            "status": "triggered"
        })
        failure_injector.inject_failure(
            FailurePoint.STATUS_CHECK,
            failure_type,
            "Debug: Intentional failure on status check"
        )
    else:
        if "injected_failures" not in job:
            job["injected_failures"] = []
        # Only add if not already added
        existing_status_check = [f for f in job["injected_failures"] if f.get("interface") == "status_check"]
        if not existing_status_check:
            job["injected_failures"].append({
                "interface": "status_check",
                "failure_type": "none",
                "status": "passed"
            })

    # Check latest payment status if payment instance exists
    if job_id in payment_instances:
        try:
            status = await payment_instances[job_id].check_payment_status()
            job["payment_status"] = status.get("data", {}).get("status")
            logger.info(f"Updated payment status for job {job_id}: {job['payment_status']}")
        except ValueError as e:
            logger.warning(f"Error checking payment status: {str(e)}")
            job["payment_status"] = "unknown"
        except Exception as e:
            logger.error(f"Error checking payment status: {str(e)}", exc_info=True)
            job["payment_status"] = "error"


    result_data = job.get("result")
    logger.info(f"Result data: {result_data}")
    # Match orig_main.py: extract .raw attribute if available, otherwise None
    result = result_data.raw if result_data and hasattr(result_data, "raw") else None

    # Map internal status to MIP-003 compliant status values
    internal_status = job["status"]
    mip_status = internal_status
    if internal_status == "awaiting_human_approval":
        mip_status = "awaiting_input"  # Map HITL approval to awaiting_input for MIP-003 compliance
    elif internal_status == "awaiting_payment":
        mip_status = "pending"  # Map awaiting_payment to pending
    elif internal_status == "running":
        mip_status = "working"  # Map running to working for MIP-003 compliance
    elif internal_status == "rejected":
        mip_status = "failed"  # Map rejected to failed for MIP-003 compliance

    # Determine which id to use for status response
    # For HITL workflows: use status_id when transitioning from awaiting_input to running
    # For completion: always use completion_id (new id) so Sokosumi detects the change
    status_id = job.get("status_id")
    completion_id = job.get("completion_id")
    
    if mip_status == "completed" and completion_id:
        # Use completion_id when completed - this ensures Sokosumi detects the change
        response_id = completion_id
        logger.info(f"STATUS: Job {job_id} completed, using completion_id {completion_id} for status response")
    elif mip_status == "completed":
        # Fallback: generate new id if completion_id not set
        response_id = str(uuid.uuid4())
        jobs[job_id]["completion_id"] = response_id
        logger.info(f"STATUS: Job {job_id} completed, generated new id {response_id} for status response")
    elif status_id and mip_status in ["working", "running"]:
        # Use the status_id from HITL request when transitioning to running
        response_id = status_id
        logger.info(f"STATUS: Using status_id {status_id} for job {job_id} in {mip_status} status")
    else:
        # Default to job_id for other cases
        response_id = job_id
        logger.info(f"STATUS: Using job_id {job_id} for status response")

    # MIP-003 compliant status response - MUST start with "id" and "status"
    status_response = {
        "id": response_id,  # Use status_id for HITL, new id for completion, job_id otherwise
        "status": mip_status  # MIP-003 compliant status value
    }
    
    # Add result if job is completed
    if mip_status == "completed" and result:
        status_response["result"] = result
    
    # Add input_required and input_schema if job is awaiting input (MIP-003 requirement)
    if mip_status == "awaiting_input":
        status_response["input_required"] = True
        # Provide input schema for HITL approval
        approval_info = job.get("approval_info", {})
        status_response["input_schema"] = {
            "input_data": [
                {
                    "id": "approve",
                    "type": "boolean",
                    "name": "Approve Task Execution",
                    "data": {
                        "description": approval_info.get("action", "Approve or reject task execution"),
                        "placeholder": "true"
                    }
                },
                {
                    "id": "reason",
                    "type": "string",
                    "name": "Rejection Reason (optional)",
                    "data": {
                        "description": "Reason for rejection (only required if approve is false)",
                        "placeholder": "Enter reason for rejection"
                    },
                    "validations": [
                        { "validation": "optional", "value": "true" }
                    ]
                }
            ]
        }
    
    # Add additional fields for backward compatibility (non-standard but useful)
    # These are added AFTER the MIP-003 required fields
    status_response["job_id"] = job_id
    status_response["payment_status"] = job.get("payment_status")
    if result and "result" not in status_response:
        status_response["result"] = result

    return status_response

# ─────────────────────────────────────────────────────────────────────────────
# 3b) Provide Additional Input (MIP-003: /provide_input)
# ─────────────────────────────────────────────────────────────────────────────
def calculate_input_hash(input_data: dict[str, Any]) -> str:
    """
    Calculate input_hash from input_data dict using the same logic as Payment class.
    Converts all values to strings, sorts keys alphabetically, and hashes.
    """
    # Convert all values to strings (matching Payment class behavior)
    stringified_data = {}
    for k, v in input_data.items():
        if v is None:
            stringified_data[k] = ""
        elif isinstance(v, bool):
            # Convert bool to lowercase string: True -> "true", False -> "false"
            stringified_data[k] = str(v).lower()
        elif isinstance(v, (int, float)):
            # Convert numbers to string representation
            stringified_data[k] = str(v)
        else:
            # Already a string or convert to string
            stringified_data[k] = str(v)
    
    # Sort keys alphabetically to ensure consistent hash calculation
    sorted_data = dict(sorted(stringified_data.items()))
    
    # Convert to JSON string and hash using SHA256
    json_str = json.dumps(sorted_data, separators=(',', ':'))
    hash_obj = hashlib.sha256(json_str.encode('utf-8'))
    return hash_obj.hexdigest()

@app.post("/provide_input")
async def provide_input(request_data: ProvideInputRequest):
    """
    Allows users to provide additional input for a job that is in the "awaiting_input" status.
    For HITL, this handles approval/rejection input.
    Fulfills MIP-003 /provide_input endpoint.
    
    Expected request format:
    {
        "job_id": "agent_job_123",
        "status_id": "status_456",
        "input_data": {
            "approve": false,
            "reason": "test"
        }
    }
    
    Returns:
    {
        "input_hash": "string",
        "signature": "string"
    }
    """
    try:
        logger.info(f"PROVIDE_INPUT: Received request: {request_data}")
        logger.info(f"PROVIDE_INPUT: Request job_id: {request_data.job_id}")
        logger.info(f"PROVIDE_INPUT: Request input_data: {request_data.input_data}")
        
        job_id = request_data.job_id
        
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        
        job = jobs[job_id]
        
        # Check if job is in awaiting_input status (which includes awaiting_human_approval)
        if job["status"] not in ["awaiting_input", "awaiting_human_approval"]:
            raise HTTPException(
                status_code=400,
                detail=f"Job is not awaiting input. Current status: {job['status']}"
            )
        
        # input_data is already a dict, use it directly
        input_data_dict = request_data.input_data
        
        logger.info(f"PROVIDE_INPUT: Processing input_data: {input_data_dict}")
        
        # Calculate input_hash from input_data
        input_hash = calculate_input_hash(input_data_dict)
        logger.info(f"PROVIDE_INPUT: Calculated input_hash: {input_hash}")
        
        # Handle HITL approval input
        if "approve" in input_data_dict:
            approve_value = input_data_dict["approve"]
            if isinstance(approve_value, str):
                approve = approve_value.lower() == "true"
            else:
                approve = bool(approve_value)
            
            reason = input_data_dict.get("reason")
            # Handle empty string reason (Sokosumi sends " " for empty)
            if reason and isinstance(reason, str):
                reason = reason.strip()
                if not reason:
                    reason = None
            operator = input_data_dict.get("operator")
            
            logger.info(f"PROVIDE_INPUT: Processing approval - approve={approve}, reason={reason}, operator={operator}")
            
            # Check if approval request exists
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
            
            # Store status_id for HITL workflow (Sokosumi uses this to track status changes)
            if request_data.status_id:
                jobs[job_id]["status_id"] = request_data.status_id
                logger.info(f"PROVIDE_INPUT: Stored status_id {request_data.status_id} for job {job_id}")
            
            # Process approval or rejection
            if approve:
                success = hitl_manager.approve(job_id, operator)
                if success:
                    # Don't update status here - we'll do it after returning the response
                    # This ensures Sokosumi gets the input_hash/signature first
                    logger.info(f"PROVIDE_INPUT: Job {job_id} approved, will update status after response")
                else:
                    raise HTTPException(status_code=400, detail="Failed to approve job")
            else:
                success = hitl_manager.reject(job_id, reason, operator)
                if success:
                    # For rejection, update status immediately since we're not continuing
                    jobs[job_id]["status"] = "rejected"
                    jobs[job_id]["error"] = f"Task execution rejected: {reason or 'No reason provided'}"
                    logger.info(f"PROVIDE_INPUT: Job {job_id} rejected, status updated to rejected")
                else:
                    raise HTTPException(status_code=400, detail="Failed to reject job")
        
        # Return response with input_hash and signature ONLY
        # Important: Do not include status in this response
        response = {
            "input_hash": input_hash,
            "signature": ""  # TODO: Implement proper signature generation if required
        }
        
        logger.info(f"PROVIDE_INPUT: Returning response: {response}")
        
        # After returning the response, update status to "running" if approved
        # This ensures Sokosumi gets the input_hash/signature first, then sees status change
        if approve and success:
            # Use asyncio.create_task to update status after response is sent
            async def update_status_after_response():
                await asyncio.sleep(0.1)  # Small delay to ensure response is sent
                jobs[job_id]["status"] = "running"
                logger.info(f"PROVIDE_INPUT: Job {job_id} status updated to running after response sent")
            
            asyncio.create_task(update_status_after_response())
        
        return response
        
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error(f"PROVIDE_INPUT: Error processing request: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=422,
            detail=f"Invalid request format: {str(e)}"
        )

# ─────────────────────────────────────────────────────────────────────────────
# 4) HITL Approval Endpoints (Non-standard, for direct API access)
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
async def get_approval_status(job_id: str = Query(..., description="Job ID to check approval status")):
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
# 5) Check Server Availability (MIP-003: /availability)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/availability")
async def check_availability():
    """ Checks if the server is operational """

    return {"status": "available", "type": "masumi-agent", "message": "Server operational."}
    # Commented out for simplicity sake but its recommended to include the agentIdentifier
    #return {"status": "available","agentIdentifier": os.getenv("AGENT_IDENTIFIER"), "message": "The server is running smoothly."}

# ─────────────────────────────────────────────────────────────────────────────
# 6) Retrieve Input Schema (MIP-003: /input_schema)
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
                "id": "failure_point",
                "type": "option",
                "name": "Failure Point (optional)",
                "data": {
                    "description": "Point to inject failure during execution. If not provided or empty, execution proceeds normally.",
                    "values": [
                        "fail_on_start_job",
                        "fail_on_payment_creation",
                        "fail_on_payment_monitoring",
                        "fail_on_task_execution",
                        "fail_on_payment_completion",
                        "fail_on_status_check",
                        "fail_on_availability",
                        "fail_on_hitl_approval",
                        "fail_on_hitl_timeout",
                        "fail_on_hitl_rejection"
                    ]
                },
                "validations": [
                    { "validation": "optional","value": "true" },
                    { "validation": "min", "value": "0" },
                    { "validation": "max", "value": "1" }
                ]
            },
            {
                "id": "failure_type",
                "type": "option",
                "name": "Failure Type (optional)",
                "data": {
                    "description": "Type of failure to inject. Required if failure_point is specified. If not provided or empty, execution proceeds normally.",
                    "values": [
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
                    { "validation": "optional","value": "true" },
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
                },
                "validations": [
                    { "validation": "optional","value": "true" }
                ]
            },
            {
                "id": "hitl_timeout",
                "type": "number",
                "name": "HITL Timeout (optional)",
                "data": {
                    "description": "Timeout in seconds for HITL approval (default: 300). Only used if HITL is enabled.",
                    "placeholder": "300"
                },
                "validations": [
                    { "validation": "optional","value": "true" }
                ]
            }
        ]
    }

# ─────────────────────────────────────────────────────────────────────────────
# 7) Health Check
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
# Main Logic if Called as a Script
# ─────────────────────────────────────────────────────────────────────────────
def main():
    """Run the standalone agent flow without the API"""
    import os
    # Disable execution traces to avoid terminal issues
    os.environ['CREWAI_DISABLE_TELEMETRY'] = 'true'
    
    print("\n" + "=" * 70)
    print("🚀 Running CrewAI agents locally (standalone mode)...")
    print("=" * 70 + "\n")
    
    # Define test input
    input_data = {"text": "The impact of AI on the job market"}
    
    print(f"Input: {input_data['text']}")
    print("\nProcessing with CrewAI agents...\n")
    
    # Initialize and run the crew
    crew = ResearchCrew(verbose=True)
    result = crew.crew.kickoff(inputs=input_data)
    
    # Display the result
    print("\n" + "=" * 70)
    print("✅ Crew Output:")
    print("=" * 70 + "\n")
    print(result)
    print("\n" + "=" * 70 + "\n")
    
    # Ensure terminal is properly reset after CrewAI execution
    sys.stdout.flush()
    sys.stderr.flush()

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "api":
        # Run API mode
        port = int(os.environ.get("API_PORT", 8080))
        # Set host from environment variable, default to 0.0.0.0 for cloud/Docker deployments.
        # This allows external connections. Use API_HOST=127.0.0.1 for localhost-only access.
        host = os.environ.get("API_HOST", "0.0.0.0")
        
        # Display localhost for clarity when binding to all interfaces
        display_host = "127.0.0.1" if host == "0.0.0.0" else host

        print("\n" + "=" * 70)
        print("🚀 Starting FastAPI server with Masumi integration...")
        print("=" * 70)
        print(f"API Documentation:        http://{display_host}:{port}/docs")
        print(f"Availability Check:       http://{display_host}:{port}/availability")
        print(f"Status Check:             http://{display_host}:{port}/status")
        print(f"Input Schema:             http://{display_host}:{port}/input_schema\n")
        print("=" * 70 + "\n")

        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        # Run standalone mode
        main()
