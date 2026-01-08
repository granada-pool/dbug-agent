import asyncio
from crewai import Agent, Crew, Task
from logging_config import get_logger

class DebugCrew:
    """
    Debug agent crew for testing Sokosumi platform interfaces.
    This agent can be hired through Sokosumi and will execute debug commands.
    """
    def __init__(self, verbose=True, logger=None):
        self.verbose = verbose
        self.logger = logger or get_logger(__name__)
        self.crew = self.create_crew()
        self.logger.info("DebugCrew initialized")

    def create_crew(self):
        self.logger.info("Creating debug crew")
        
        debug_agent = Agent(
            role='Debug Testing Agent',
            goal='Execute debug commands to test Sokosumi platform error handling',
            backstory='A specialized agent designed for internal testing of the Sokosumi marketplace platform. Can simulate various failure scenarios.',
            verbose=self.verbose,
            allow_delegation=False
        )

        crew = Crew(
            agents=[debug_agent],
            tasks=[
                Task(
                    description='Execute debug command: {text}',
                    expected_output='Debug execution result with test outcome',
                    agent=debug_agent
                )
            ]
        )
        self.logger.info("Debug crew setup completed")
        return crew

class ServiceResult:
    """Result object that wraps CrewAI output for compatibility"""
    def __init__(self, crew_output, input_text: str = ""):
        """
        Initialize ServiceResult from CrewAI output
        
        Args:
            crew_output: CrewAI CrewOutput object
            input_text: Original input text for reference
        """
        self.crew_output = crew_output
        self.input_text = input_text
        
        # Extract raw result from CrewAI output
        if hasattr(crew_output, 'raw'):
            self.raw = crew_output.raw
        elif hasattr(crew_output, 'tasks_output'):
            # Fallback: get output from first task
            self.raw = crew_output.tasks_output[0] if crew_output.tasks_output else str(crew_output)
        else:
            self.raw = str(crew_output)
        
        # Create JSON-compatible dictionary
        self.json_dict = {
            "result": self.raw,
            "input_text": input_text,
            "task": "debug_execution"
        }

class AgenticService:
    """Debug agent service using CrewAI DebugCrew"""
    
    def __init__(self, logger=None):
        self.logger = logger
    
    async def execute_task(self, input_data: dict) -> ServiceResult:
        """
        Execute debug task using DebugCrew
        
        Args:
            input_data: Dictionary containing 'input_string' or 'text' key
                       Can also contain debug commands: 'command', 'failure_point', 'failure_type'
            
        Returns:
            ServiceResult with CrewAI output
            
        Raises:
            ValueError: If no valid input text is provided
            Exception: If CrewAI execution fails
        """
        # Extract text input - support both new format (input_string) and old format (text)
        text_input = input_data.get("input_string", "")
        if not text_input:
            text_input = input_data.get("text", "")
        
        # If no text but has command, create text from command
        if not text_input and "command" in input_data:
            text_input = f"Execute command: {input_data.get('command', 'debug_test')}"
        
        # Validate that we have some input
        if not text_input or not text_input.strip():
            error_msg = "No valid input text provided. Provide 'input_string', 'text', or 'command' in input_data."
            if self.logger:
                self.logger.error(error_msg)
            raise ValueError(error_msg)
        
        if self.logger:
            truncated = text_input[:100] + "..." if len(text_input) > 100 else text_input
            self.logger.info(f"Starting debug task with input: '{truncated}'")
        
        try:
            # Initialize DebugCrew
            crew = DebugCrew(logger=self.logger)
            inputs = {"text": text_input}
            
            # CrewAI's kickoff() is synchronous, so we need to run it in a thread pool
            # to avoid blocking the async event loop
            if self.logger:
                self.logger.info("Executing CrewAI task in thread pool...")
            
            # Run synchronous CrewAI kickoff in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: crew.crew.kickoff(inputs)
            )
            
            if self.logger:
                self.logger.info("Debug task completed successfully")
            
            return ServiceResult(result, text_input)
            
        except Exception as e:
            error_msg = f"CrewAI execution failed: {str(e)}"
            if self.logger:
                self.logger.error(error_msg, exc_info=True)
            raise Exception(error_msg) from e

def get_agentic_service(logger=None):
    """
    Factory function to get the appropriate service for this branch.
    This enables easy switching between different implementations across branches.
    
    Main branch: Debug agent service using CrewAI DebugCrew
    Other branches: LangChain, n8n implementations
    """
    return AgenticService(logger) 