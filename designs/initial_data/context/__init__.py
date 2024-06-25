from nautobot_design_builder.context import Context, DesignValidationError


class InitialDesignContext(Context):
    """Render context for basic design"""

    routers_per_site: int

    def validate_something(self):
        # testing, raise DesignValidationError
        raise DesignValidationError("test failure")
