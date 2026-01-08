"""
Human-in-the-Loop (HITL) manager for debug agent.
Handles approval workflows, timeouts, and failure injection during pauses.
"""
import asyncio
import time
from enum import Enum
from typing import Optional, Dict, Any
from logging_config import get_logger

logger = get_logger(__name__)

class ApprovalStatus(str, Enum):
    """Status of HITL approval"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"

class HITLManager:
    """
    Manages Human-in-the-Loop approval workflows.
    Stores pending approvals and handles timeouts.
    """
    
    def __init__(self):
        # Store pending approvals: {job_id: {status, timestamp, timeout, action}}
        self.pending_approvals: Dict[str, Dict[str, Any]] = {}
        logger.info("HITLManager initialized")
    
    def create_approval_request(
        self, 
        job_id: str, 
        action: str, 
        timeout_seconds: int = 300,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a new approval request.
        
        Args:
            job_id: Job identifier
            action: Description of action requiring approval
            timeout_seconds: Timeout in seconds (default 5 minutes)
            metadata: Additional metadata about the approval request
            
        Returns:
            Approval request info
        """
        approval_info = {
            "status": ApprovalStatus.PENDING,
            "timestamp": time.time(),
            "timeout": timeout_seconds,
            "action": action,
            "metadata": metadata or {}
        }
        
        self.pending_approvals[job_id] = approval_info
        logger.info(f"Created approval request for job {job_id}: {action} (timeout: {timeout_seconds}s)")
        
        return {
            "job_id": job_id,
            "action": action,
            "status": ApprovalStatus.PENDING,
            "timeout_seconds": timeout_seconds,
            "approval_url": f"/approve?job_id={job_id}"
        }
    
    async def wait_for_approval(
        self, 
        job_id: str,
        check_interval: float = 1.0
    ) -> ApprovalStatus:
        """
        Wait for approval with timeout checking.
        
        Args:
            job_id: Job identifier
            check_interval: How often to check for approval (seconds)
            
        Returns:
            Final approval status
        """
        if job_id not in self.pending_approvals:
            logger.warning(f"No approval request found for job {job_id}")
            return ApprovalStatus.REJECTED
        
        approval_info = self.pending_approvals[job_id]
        start_time = approval_info["timestamp"]
        timeout = approval_info["timeout"]
        
        logger.info(f"Waiting for approval on job {job_id} (timeout: {timeout}s)")
        
        while True:
            # Check if approved/rejected
            current_status = approval_info["status"]
            if current_status != ApprovalStatus.PENDING:
                logger.info(f"Approval resolved for job {job_id}: {current_status}")
                return current_status
            
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                approval_info["status"] = ApprovalStatus.TIMEOUT
                logger.warning(f"Approval timeout for job {job_id} after {elapsed:.1f}s")
                return ApprovalStatus.TIMEOUT
            
            # Wait before next check
            await asyncio.sleep(check_interval)
    
    def approve(self, job_id: str, operator: Optional[str] = None) -> bool:
        """
        Approve a pending request.
        
        Args:
            job_id: Job identifier
            operator: Optional operator identifier
            
        Returns:
            True if approved, False if not found
        """
        if job_id not in self.pending_approvals:
            logger.warning(f"Approval request not found for job {job_id}")
            return False
        
        approval_info = self.pending_approvals[job_id]
        if approval_info["status"] != ApprovalStatus.PENDING:
            logger.warning(f"Job {job_id} already resolved: {approval_info['status']}")
            return False
        
        approval_info["status"] = ApprovalStatus.APPROVED
        approval_info["operator"] = operator
        approval_info["resolved_at"] = time.time()
        logger.info(f"Job {job_id} approved by {operator or 'unknown operator'}")
        return True
    
    def reject(self, job_id: str, reason: Optional[str] = None, operator: Optional[str] = None) -> bool:
        """
        Reject a pending request.
        
        Args:
            job_id: Job identifier
            reason: Optional rejection reason
            operator: Optional operator identifier
            
        Returns:
            True if rejected, False if not found
        """
        if job_id not in self.pending_approvals:
            logger.warning(f"Approval request not found for job {job_id}")
            return False
        
        approval_info = self.pending_approvals[job_id]
        if approval_info["status"] != ApprovalStatus.PENDING:
            logger.warning(f"Job {job_id} already resolved: {approval_info['status']}")
            return False
        
        approval_info["status"] = ApprovalStatus.REJECTED
        approval_info["reason"] = reason
        approval_info["operator"] = operator
        approval_info["resolved_at"] = time.time()
        logger.info(f"Job {job_id} rejected by {operator or 'unknown operator'}: {reason or 'no reason'}")
        return True
    
    def get_approval_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get current approval status for a job"""
        return self.pending_approvals.get(job_id)
    
    def cleanup(self, job_id: str):
        """Clean up approval data for a job"""
        if job_id in self.pending_approvals:
            del self.pending_approvals[job_id]
            logger.debug(f"Cleaned up approval data for job {job_id}")

