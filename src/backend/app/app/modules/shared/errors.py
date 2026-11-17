class BusinessRuleError(Exception):
    """
    Se lanza cuando una operación está bien escrita pero rompe una regla del
    negocio (ej.: sacar más unidades de las que hay). La API la responde con
    HTTP 409 Conflict.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
