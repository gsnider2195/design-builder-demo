import logging
from nautobot_design_builder.context import Context, DesignValidationError


logger = logging.getLogger(__name__)

class InitialDesignContext(Context):
    """Render context for basic design"""

    routers_per_site: int

    def validate_something(self):
        # testing, raise DesignValidationError
        logger.debug("RUNNING VALIDATION")
        print(this_is_a_syntax_error)
        pass
