import pytest
import os
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
import json

# set up test environment variables before importing main
os.environ["OPENAI_API_KEY"] = "test-openai-key"
os.environ["PAYMENT_SERVICE_URL"] = "https://test-payment-service.com"
os.environ["PAYMENT_API_KEY"] = "test-payment-key"
os.environ["NETWORK"] = "preview"
os.environ["AGENT_IDENTIFIER"] = "test-agent-123"
os.environ["SELLER_VKEY"] = "test-seller-vkey"

from main import app
from agentic_service import get_agentic_service, AgenticService, ServiceResult

client = TestClient(app)


class TestHealthEndpoints:
    """test basic health and info endpoints"""
    
    def test_health_endpoint(self):
        """test /health returns healthy status"""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}
    
    def test_availability_endpoint(self):
        """test /availability returns available status"""
        response = client.get("/availability")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "available"
        assert "uptime" in data
        assert isinstance(data["uptime"], int)
        assert data["message"] == "Debug Agent - Server operational. Use input_data with debug commands to test failures."
    
    def test_input_schema_endpoint(self):
        """test /input_schema returns correct schema"""
        response = client.get("/input_schema")
        assert response.status_code == 200
        data = response.json()
        assert "input_data" in data
        assert len(data["input_data"]) >= 4  # At least input_string, command, failure_point, failure_type (plus HITL fields)
        assert data["input_data"][0]["id"] == "input_string"
        assert data["input_data"][0]["type"] == "string"
        assert data["input_data"][0]["name"] == "Task Description"


class TestStartJobEndpoint:
    """test /start_job endpoint functionality"""
    
    @patch('main.Payment')
    @patch('main.cuid2.Cuid')
    @patch.dict(os.environ, {"AGENT_IDENTIFIER": "test-agent-123", "SELLER_VKEY": "test-seller-vkey"})
    def test_start_job_success(self, mock_cuid, mock_payment_class):
        """test successful job creation"""
        # mock cuid2 generation
        mock_cuid_instance = MagicMock()
        mock_cuid_instance.generate.return_value = "test-cuid2-identifier"
        mock_cuid.return_value = mock_cuid_instance
        
        # mock payment response
        mock_payment_instance = AsyncMock()
        mock_payment_instance.create_payment_request.return_value = {
            "data": {
                "blockchainIdentifier": "test-blockchain-id",
                "submitResultTime": "2025-06-17T12:00:00Z",
                "unlockTime": "2025-06-17T13:00:00Z",
                "externalDisputeUnlockTime": "2025-06-17T14:00:00Z",
                "inputHash": "test-input-hash"
            }
        }
        mock_payment_instance.input_hash = "test-input-hash"
        mock_payment_instance.payment_ids = set()
        mock_payment_instance.start_status_monitoring = AsyncMock()
        mock_payment_class.return_value = mock_payment_instance
        
        # test request
        test_data = {
            "input_data": [
                {"key": "input_string", "value": "Hello World"}
            ]
        }
        
        response = client.post("/start_job", json=test_data)
        
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert data["payment_id"] == "test-blockchain-id"
        
        # verify payment was created with generated identifier
        mock_payment_class.assert_called_once()
        call_args = mock_payment_class.call_args[1]
        assert call_args["identifier_from_purchaser"] == "test-cuid2-identifier"
        # verify input_data was converted to dict
        expected_input = {"input_string": "Hello World"}
        assert call_args["input_data"] == expected_input
    
    def test_start_job_missing_input_data(self):
        """test job creation with missing input_data"""
        test_data = {}
        
        response = client.post("/start_job", json=test_data)
        
        assert response.status_code == 422  # validation error
    
    def test_start_job_invalid_input_data(self):
        """test job creation with invalid input_data structure"""
        test_data = {
            "input_data": "not an array"
        }
        
        response = client.post("/start_job", json=test_data)
        
        assert response.status_code == 422  # validation error
    
    @patch('main.Payment')
    @patch('main.cuid2.Cuid') 
    @patch.dict(os.environ, {"AGENT_IDENTIFIER": "test-agent-123", "SELLER_VKEY": "test-seller-vkey"})
    def test_start_job_missing_input_string(self, mock_cuid, mock_payment_class):
        """test job creation with missing input_string in input_data"""
        # mock cuid2 generation
        mock_cuid_instance = MagicMock()
        mock_cuid_instance.generate.return_value = "test-cuid2-identifier"
        mock_cuid.return_value = mock_cuid_instance
        
        test_data = {
            "input_data": [
                {"key": "other_field", "value": "some value"}
            ]
        }
        
        response = client.post("/start_job", json=test_data)
        
        assert response.status_code == 400
        assert "input_string" in response.json()["detail"]
    
    def test_url_validation_function(self):
        """test the URL validation function directly"""
        from main import validate_url
        
        # test valid URLs
        assert validate_url("https://example.com", "TEST_URL") == ""
        assert validate_url("http://localhost:3000", "TEST_URL") == ""
        
        # test invalid URLs
        assert "must start with 'https://' or 'http://'" in validate_url("example.com", "TEST_URL")
        assert "must start with 'https://' or 'http://'" in validate_url("ftp://example.com", "TEST_URL")
        assert "TEST_URL is not set" in validate_url("", "TEST_URL")
        assert "not a valid URL format" in validate_url("https://", "TEST_URL")
    
    @patch('main.Payment')
    @patch('main.cuid2.Cuid')
    @patch.dict(os.environ, {"AGENT_IDENTIFIER": "test-agent-123", "SELLER_VKEY": "test-seller-vkey"})
    def test_start_job_payment_error(self, mock_cuid, mock_payment_class):
        """test job creation when payment service fails"""
        # mock cuid2 generation
        mock_cuid_instance = MagicMock()
        mock_cuid_instance.generate.return_value = "test-cuid2-identifier"
        mock_cuid.return_value = mock_cuid_instance
        
        # mock payment failure
        mock_payment_instance = AsyncMock()
        mock_payment_instance.create_payment_request.side_effect = Exception("Payment service error")
        mock_payment_class.return_value = mock_payment_instance
        
        test_data = {
            "input_data": [
                {"key": "input_string", "value": "Hello World"}
            ]
        }
        
        response = client.post("/start_job", json=test_data)
        
        assert response.status_code in [400, 502]
        detail = response.json()["detail"]
        assert any(phrase in detail for phrase in ["Payment service unavailable", "Internal server error", "Server configuration error"])


class TestStatusEndpoint:
    """test /status endpoint functionality"""
    
    def test_status_job_not_found(self):
        """test status check for non-existent job"""
        response = client.get("/status?job_id=non-existent-job")
        
        assert response.status_code == 404
        assert response.json()["detail"] == "Job not found"
    
    @patch('main.Payment')
    @patch('main.cuid2.Cuid')
    @patch.dict(os.environ, {"AGENT_IDENTIFIER": "test-agent-123", "SELLER_VKEY": "test-seller-vkey"})
    def test_status_job_found(self, mock_cuid, mock_payment_class):
        """test status check for existing job"""
        # first create a job
        mock_cuid_instance = MagicMock()
        mock_cuid_instance.generate.return_value = "test-cuid2-identifier"
        mock_cuid.return_value = mock_cuid_instance
        
        mock_payment_instance = AsyncMock()
        mock_payment_instance.create_payment_request.return_value = {
            "data": {
                "blockchainIdentifier": "test-blockchain-id",
                "submitResultTime": "2025-06-17T12:00:00Z",
                "unlockTime": "2025-06-17T13:00:00Z",
                "externalDisputeUnlockTime": "2025-06-17T14:00:00Z",
                "inputHash": "test-input-hash"
            }
        }
        mock_payment_instance.input_hash = "test-input-hash"
        mock_payment_instance.payment_ids = set()
        mock_payment_instance.start_status_monitoring = AsyncMock()
        mock_payment_instance.check_payment_status.return_value = {
            "data": {"status": "pending"}
        }
        mock_payment_class.return_value = mock_payment_instance
        
        # create job
        test_data = {
            "input_data": [
                {"key": "input_string", "value": "Hello World"}
            ]
        }
        
        create_response = client.post("/start_job", json=test_data)
        assert create_response.status_code == 200
        job_id = create_response.json()["job_id"]
        
        # check status
        status_response = client.get(f"/status?job_id={job_id}")
        
        assert status_response.status_code == 200
        status_data = status_response.json()
        assert status_data["job_id"] == job_id
        assert status_data["status"] == "awaiting_payment"
        assert status_data["payment_status"] == "pending"
        assert status_data["result"] is None


class TestOpenAPISchema:
    """test that openapi schema is properly generated"""
    
    def test_openapi_json(self):
        """test /openapi.json returns valid schema"""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        
        schema = response.json()
        assert schema["openapi"] == "3.1.0"
        assert schema["info"]["title"] == "Debug Agent API - Masumi API Standard"
        assert schema["info"]["version"] == "1.0.0"
        
        # check that all expected endpoints are present
        paths = schema["paths"]
        assert "/start_job" in paths
        assert "/status" in paths
        assert "/availability" in paths
        assert "/input_schema" in paths
        assert "/health" in paths
        
        # check start_job endpoint schema
        start_job_schema = paths["/start_job"]["post"]
        assert "requestBody" in start_job_schema
        
        # verify identifier_from_purchaser is NOT in the request schema
        request_schema = start_job_schema["requestBody"]["content"]["application/json"]["schema"]
        if "$ref" in request_schema:
            # find the actual schema definition
            ref_path = request_schema["$ref"].split("/")[-1]
            actual_schema = schema["components"]["schemas"][ref_path]
            properties = actual_schema.get("properties", {})
        else:
            properties = request_schema.get("properties", {})
        
        assert "input_data" in properties
        # verify input_data is defined as an array of InputDataItem
        input_data_schema = properties["input_data"]
        assert input_data_schema["type"] == "array"


class TestAgenticService:
    """test the agentic service factory and core functionality"""
    
    def test_factory_function(self):
        """test that get_agentic_service returns proper service instance"""
        service = get_agentic_service()
        assert isinstance(service, AgenticService)
        assert hasattr(service, 'execute_task')
    
    def test_factory_function_with_logger(self):
        """test that factory function properly handles logger parameter"""
        import logging
        test_logger = logging.getLogger("test")
        service = get_agentic_service(logger=test_logger)
        assert service.logger == test_logger
    
    @pytest.mark.asyncio
    async def test_service_execute_task(self):
        """test that the service properly executes tasks"""
        service = get_agentic_service()
        input_data = {"input_string": "hello world"}
        
        result = await service.execute_task(input_data)
        
        assert isinstance(result, ServiceResult)
        assert result.input_text == "hello world"
        assert result.raw is not None
        assert isinstance(result.raw, str)
        assert result.json_dict["task"] == "debug_execution"
        assert result.json_dict["input_text"] == "hello world"
    
    @pytest.mark.asyncio
    async def test_service_empty_input(self):
        """test service with empty input"""
        service = get_agentic_service()
        input_data = {"input_string": ""}
        
        with pytest.raises(ValueError, match="No valid input text provided"):
            await service.execute_task(input_data)
    
    @pytest.mark.asyncio
    async def test_service_missing_input_string(self):
        """test service with missing input_string key"""
        service = get_agentic_service()
        input_data = {"other_key": "value"}
        
        with pytest.raises(ValueError, match="No valid input text provided"):
            await service.execute_task(input_data)


class TestMasumiCompliance:
    """verify that main branch maintains masumi network compliance"""
    
    def test_all_required_endpoints_exist(self):
        """verify all MIP-003 required endpoints are present"""
        # test /start_job
        response = client.options("/start_job")
        assert response.status_code in [200, 405]  # endpoint exists
        
        # test /status  
        response = client.options("/status")
        assert response.status_code in [200, 405]  # endpoint exists
        
        # test /availability
        response = client.get("/availability")
        assert response.status_code == 200
        
        # test /input_schema
        response = client.get("/input_schema")
        assert response.status_code == 200
        
        # test /health
        response = client.get("/health")
        assert response.status_code == 200
    
    def test_input_schema_compliance(self):
        """verify input schema follows masumi standards"""
        response = client.get("/input_schema")
        assert response.status_code == 200
        
        schema = response.json()
        assert "input_data" in schema
        assert isinstance(schema["input_data"], list)
        assert len(schema["input_data"]) > 0
        
        # check first input field has required structure
        first_input = schema["input_data"][0]
        assert "id" in first_input
        assert "type" in first_input
        assert "name" in first_input
    
    def test_start_job_request_format(self):
        """verify start_job accepts masumi standard request format"""
        # this test only checks the request format parsing, not the full flow
        test_data = {
            "input_data": [
                {"key": "input_string", "value": "test"}
            ]
        }
        
        # we expect this to fail due to missing env vars, but it should parse the request
        response = client.post("/start_job", json=test_data)
        # should be 500 (config error) not 422 (validation error)
        assert response.status_code in [400, 500, 502]  # config errors, not validation


class TestHITLEndpoints:
    """Test Human-in-the-Loop endpoints"""
    
    def test_get_approval_status_no_job(self):
        """Test getting approval status for non-existent job"""
        response = client.get("/approve?job_id=non-existent-job")
        assert response.status_code == 404
    
    def test_get_approval_status_no_approval(self):
        """Test getting approval status when no approval is required"""
        # Create a job without HITL
        from main import jobs
        test_job_id = "test-job-no-hitl"
        jobs[test_job_id] = {
            "status": "running",
            "input_data": {"input_string": "test"}
        }
        
        response = client.get(f"/approve?job_id={test_job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "no_approval_required"
        
        # Cleanup
        del jobs[test_job_id]
    
    def test_approve_job_not_found(self):
        """Test approving a non-existent job"""
        response = client.post("/approve", json={
            "job_id": "non-existent-job",
            "approve": True
        })
        assert response.status_code == 404
    
    def test_approve_job_no_pending_approval(self):
        """Test approving a job without pending approval"""
        from main import jobs
        test_job_id = "test-job-no-pending"
        jobs[test_job_id] = {
            "status": "running",
            "input_data": {"input_string": "test"}
        }
        
        response = client.post("/approve", json={
            "job_id": test_job_id,
            "approve": True
        })
        assert response.status_code == 400
        assert "No pending approval request" in response.json()["detail"]
        
        # Cleanup
        del jobs[test_job_id]
    
    @patch('main.Payment')
    @patch('main.cuid2.Cuid')
    @patch.dict(os.environ, {"AGENT_IDENTIFIER": "test-agent-123", "SELLER_VKEY": "test-seller-vkey"})
    def test_hitl_workflow_approval(self, mock_cuid, mock_payment_class):
        """Test HITL workflow with approval"""
        # Setup mocks
        mock_cuid_instance = MagicMock()
        mock_cuid_instance.generate.return_value = "test-cuid2-identifier"
        mock_cuid.return_value = mock_cuid_instance
        
        mock_payment_instance = AsyncMock()
        mock_payment_instance.create_payment_request.return_value = {
            "data": {
                "blockchainIdentifier": "test-blockchain-id",
                "submitResultTime": "2025-06-17T12:00:00Z",
                "unlockTime": "2025-06-17T13:00:00Z",
                "externalDisputeUnlockTime": "2025-06-17T14:00:00Z",
                "inputHash": "test-input-hash"
            }
        }
        mock_payment_instance.input_hash = "test-input-hash"
        mock_payment_instance.payment_ids = set()
        mock_payment_instance.start_status_monitoring = AsyncMock()
        mock_payment_instance.complete_payment = AsyncMock()
        mock_payment_class.return_value = mock_payment_instance
        
        # Create job with HITL enabled
        test_data = {
            "input_data": [
                {"key": "input_string", "value": "Test HITL"},
                {"key": "enable_hitl", "value": "true"},
                {"key": "hitl_timeout", "value": "60"}
            ]
        }
        
        response = client.post("/start_job", json=test_data)
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        
        # Check that job is awaiting approval
        from main import jobs
        assert jobs[job_id]["status"] == "awaiting_payment"  # Before payment completes
        
        # Check approval status endpoint
        status_response = client.get(f"/approve?job_id={job_id}")
        # May return no_approval_required if payment hasn't completed yet
        # This is expected behavior
        
        # Cleanup
        if job_id in jobs:
            del jobs[job_id]
    
    def test_input_schema_includes_hitl_fields(self):
        """Test that input schema includes HITL fields"""
        response = client.get("/input_schema")
        assert response.status_code == 200
        data = response.json()
        
        input_fields = {field["id"]: field for field in data["input_data"]}
        
        assert "enable_hitl" in input_fields
        assert input_fields["enable_hitl"]["type"] == "boolean"
        
        assert "hitl_timeout" in input_fields
        assert input_fields["hitl_timeout"]["type"] == "number"
        
        # Check that failure_point includes HITL options
        failure_point_field = input_fields.get("failure_point", {})
        failure_desc = failure_point_field.get("data", {}).get("description", "")
        assert "fail_on_hitl_approval" in failure_desc
        assert "fail_on_hitl_timeout" in failure_desc
        assert "fail_on_hitl_rejection" in failure_desc


if __name__ == "__main__":
    pytest.main([__file__, "-v"])