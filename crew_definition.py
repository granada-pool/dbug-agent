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